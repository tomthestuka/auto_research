"""Tool providers and outcome types for the agent ReAct loop.

`AgentRole._react` ([roles.py](roles.py)) drives a provider-agnostic inner loop:
it calls the model with `tool_definitions()`, executes each returned call via
`execute(...)`, and feeds the results back so the model chains dependent calls to
completion. A `ToolProvider` is the uniform interface behind which each role's tool
surface lives — the analyst's science sandbox here (`SandboxToolProvider`); the
reviewer/lead read-only tools and the librarian's sources are added alongside.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from auto_research.env import tool_search_config
from auto_research.observations import load_observations_config
from auto_research.sandbox.fs_guard import GOVERNANCE_BASENAMES, PathGuard
from auto_research.schemas import (
    DocumentRef,
    FigureCitation,
    FigureRef,
    ToolResult,
    document_from_file,
)


# Figure-output arguments, by naming convention → the extension the toolkit expects.
# A toolkit tool exposing one of these args has its figure written under the worker's
# figures_dir (see SandboxToolProvider._redirect_figure_args). ``""`` = base/prefix path
# (the tool appends its own suffix, e.g. ``_DU1.png``); ``.png`` = full file path.
# Convention-keyed (not tool-keyed) so new toolkits inherit it for free.
_FIGURE_PATH_ARGS: dict[str, str] = {"save_path": ".png", "save_path_prefix": ""}

# Cap on distinct PDFs the reviewer may open in one ReAct loop. Each opened PDF is
# re-attached to every subsequent model call, so this bounds the re-sent token cost.
_OPEN_PAPER_CAP = 6

# Per-PDF inline size limit (raw MB). A PDF is attached by value (base64, ~1.33x) to
# every subsequent model call, so one ultra-high-resolution paper can exhaust a provider's
# whole request-size budget on its own (a 23.5 MB paper is ~31 MB base64, ~the 32 MB
# Anthropic cap) and hard-fail the call. Papers over this open from text_search metadata
# instead of inline. 8 MB clears every normal paper in the KB (p95 ~5.5 MB) and blocks only
# the handful of figure-bloated outliers.
_OPEN_PAPER_MAX_MB = 8

# read_file reads a file BY LINE RANGE (offset/limit), not head-only, so the reviewer/lead can
# page through long toolkit source rather than reading blind (F8). The line limit + per-line char
# cap bound what one call returns; "more" is fetched by re-calling with a higher offset.
_DEFAULT_LINE_LIMIT = 1000
_MAX_LINE_CHARS = 2000


@dataclass
class ToolOutcome:
    """Result of one tool execution, as fed back into the ReAct conversation.

    ``text`` is what the model sees next turn; ``result`` is the full record (for
    memory/figure propagation) when the provider produces one. ``documents`` are
    attachments (e.g. a PDF opened via ``open_paper``) that the loop accumulates and
    re-attaches to subsequent model calls so the model can read them multimodally.
    """

    text: str
    is_error: bool = False
    figures: list[FigureRef] = field(default_factory=list)
    documents: list[DocumentRef] = field(default_factory=list)
    result: ToolResult | None = None


class ToolProvider(Protocol):
    """Uniform tool surface for the ReAct loop: describe tools, execute one."""

    def tool_definitions(self) -> list[dict]:
        """Provider-neutral ``{name, description, input_schema}`` definitions."""

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Execute the named tool with ``arguments`` and return its outcome."""


# ----------------------------------------------------------------------------
# Raise-to-human — one ask_human tool, shared by every ReAct role (§9.7/§9.8).
# ----------------------------------------------------------------------------

ASK_HUMAN_TOOL = "ask_human"
_ASK_HUMAN_KINDS = ("clarify_science", "clarify_code", "blocked")

# Lead-only design channel (§5.5): the analyst lead settles a toolkit design WITH the human
# before authoring. It routes through the same human_handler as ask_human but is **budget-free**
# in the ReAct loop (see AgentRole._react free_tools), so the lead can debate the design across
# as many rounds as it needs to converge without starving its evidence-gathering iterations.
PLAN_AND_DEBATE_TOOL = "plan_and_debate"


def ask_human_tool_def() -> dict:
    """The provider-neutral definition for the shared ``ask_human`` tool."""
    return {
        "name": ASK_HUMAN_TOOL,
        "description": (
            "Reach a real human for one clarification: an expert science or code question, "
            "or a hard block such as missing data, a broken dependency, or a contradictory "
            "request. Blocks until the human replies, so ask early and specifically. The human "
            "reads this on a phone — lead with the one question, then the few facts needed to "
            "answer it. Write 'message' as Markdown (a heading, short bullets, or a fenced code "
            "block when it helps); keep it brief."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Your question to the human, in Markdown. Lead with the question itself, "
                        "then any supporting facts (what you observed, what you already tried)."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": list(_ASK_HUMAN_KINDS),
                    "description": (
                        "Why you are asking: 'clarify_science' for an expert science point, "
                        "'clarify_code' for a code or convention point, 'blocked' for a hard block."
                    ),
                },
            },
            "required": ["message"],
        },
    }


def plan_and_debate_tool_def() -> dict:
    """The definition for the lead-only, budget-free ``plan_and_debate`` design channel (§5.5).

    Distinct from ``ask_human`` so the ReAct loop can treat it as budget-free: the lead settles
    a toolkit design with the human before authoring, and these rounds do not count against the
    loop's iteration cap (the human ending the discussion is the stop).
    """
    return {
        "name": PLAN_AND_DEBATE_TOOL,
        "description": (
            "Debate a toolkit-design decision with the human to settle it before authoring — "
            "lay out the design report (proposal, options and their trade-offs, an interface "
            "sketch) and ask the one thing you need decided (which package to extend vs. start, "
            "the interface, the validation strategy, a trade-off). Blocks until the human "
            "replies. Use it as many rounds as you need to converge: it does not consume your "
            "tool-iteration budget. Write 'message' as Markdown — use headings, bullet lists, "
            "and fenced code blocks so a longer design reads cleanly. Settle the design here, "
            "then finalise the ToolSpec the coder will follow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Your design report and the decision you want settled, in Markdown — "
                        "proposal, options/trade-offs, an interface sketch, and the explicit ask."
                    ),
                },
            },
            "required": ["message"],
        },
    }


# ----------------------------------------------------------------------------
# Local data catalog — a read-only tool for the analyst worker + lead (§9.10 step 7).
# ----------------------------------------------------------------------------

# The local-data-catalog READ lives in research_toolkit's ``catalog_utils`` package (CLAUDE.md
# §2: domain reads belong to the toolkit). Workers reach it through ``ToolkitRegistry`` (it runs
# in the sandbox like any toolkit tool); the analyst lead, which has no sandbox, is handed this
# def and runs the same pure-standard-library read in-process. Its agent-facing name is the
# registry-sanitized manifest kind, so both paths present the identical tool.
_CATALOG_UTILS_KIND = "research_toolkit.catalog_utils.data_catalog"
DATA_CATALOG_TOOL = re.sub(r"[^A-Za-z0-9_-]", "_", _CATALOG_UTILS_KIND)
_catalog_read_fn = None  # memoized in-process catalog_utils.lib.catalog_read.data_catalog


def _import_from_toolkit(module_path: str, attr: str):
    """Import ``attr`` from a research_toolkit module with ``toolkit_root`` on sys.path.

    ``catalog_utils`` (and ``obsdb_catalog`` beneath it) live in ``research_toolkit``; the
    analyst lead runs the catalog read in-process, so we import it the transient way
    ``ToolkitRegistry`` does — prepend ``toolkit_root`` for the import, then remove it (the
    module stays cached in ``sys.modules``).
    """
    import importlib
    import sys

    from auto_research.env import toolkit_root

    root = str(toolkit_root())
    inserted = bool(root) and root not in sys.path
    if inserted:
        sys.path.insert(0, root)
    try:
        module = importlib.import_module(module_path)
    finally:
        if inserted:
            sys.path.remove(root)
    return getattr(module, attr)


def data_catalog_tool_def() -> dict | None:
    """Provider-neutral definition for the read-only ``data_catalog`` tool.

    The tool is defined in ``research_toolkit``'s ``catalog_utils`` manifest; its description
    + args_schema are sourced from there so orchestration never restates them. Returns None if
    ``catalog_utils`` cannot be imported (a misconfigured toolkit), so a role's tool set still
    builds — the preflight is where a missing toolkit fails loudly.
    """
    try:
        manifest = _import_from_toolkit("catalog_utils", "TOOL_MANIFEST")
        spec = manifest[_CATALOG_UTILS_KIND]
    except Exception:  # noqa: BLE001 — toolkit absent/misconfigured; skip the tool, don't crash
        return None
    return {
        "name": DATA_CATALOG_TOOL,
        "description": spec["description"],
        "input_schema": spec.get("args_schema") or {"type": "object", "properties": {}},
    }


async def execute_data_catalog(arguments: Mapping[str, Any]) -> ToolOutcome:
    """Run the ``catalog_utils`` local-data read in-process (the analyst-lead path).

    The mission's ``obs_db_path`` is supplied by the caller — it is listed in the analyst's
    local-observation-databases context, exactly as ``acquire`` receives it. Delegates the read
    + listing to ``catalog_utils.lib.catalog_read`` (the single domain read); a malformed catalog
    raises there and is surfaced as a recoverable error. (Workers run the same tool in the sandbox
    via ``ToolkitRegistry``; this in-process path serves the sandbox-less lead.)
    """
    global _catalog_read_fn
    obs_db_path = str(arguments.get("obs_db_path") or "").strip()
    if not obs_db_path:
        return ToolOutcome(
            text="data_catalog needs obs_db_path — a mission directory from your local "
            "observation databases list (e.g. the IXPE obs-db path).",
            is_error=True,
        )
    try:
        if _catalog_read_fn is None:
            _catalog_read_fn = _import_from_toolkit("catalog_utils.lib.catalog_read", "data_catalog")
        raw_min = arguments.get("min_level")
        text = _catalog_read_fn(obs_db_path, min_level=int(raw_min) if raw_min is not None else None)
    except Exception as exc:  # noqa: BLE001 — malformed catalog / missing toolkit → recoverable
        return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)
    return ToolOutcome(text=text)


async def execute_ask_human(
    human_handler: Any, asked_by: str, arguments: Mapping[str, Any], *, model: str = ""
) -> ToolOutcome:
    """Build a ``HumanQuery`` from a tool call and block on the injected handler.

    Routes through ``hitl.resolve_human``, which raises ``HumanInputRequired`` when no
    handler is wired (the §9.8 headless fail-safe) — propagated to fail the run loudly
    rather than hang or fabricate.
    """
    from auto_research.hitl import resolve_human
    from auto_research.schemas import HumanQuery

    kind = str(arguments.get("kind") or "blocked")
    if kind not in _ASK_HUMAN_KINDS:
        kind = "blocked"
    query = HumanQuery(
        kind=kind,
        message=str(arguments.get("message") or "(no message provided)"),
        asked_by=asked_by or "agent",
        model=model or "",
    )
    response = await resolve_human(human_handler, query)
    return ToolOutcome(text=response.answer or "(human gave no answer)")


async def execute_plan_and_debate(
    human_handler: Any, asked_by: str, arguments: Mapping[str, Any], *, model: str = ""
) -> ToolOutcome:
    """Route a lead design-debate turn to the human (§5.5).

    Delivers the lead's Markdown design report to the injected handler (same path as
    ``ask_human`` — ``resolve_human`` raises ``HumanInputRequired`` headless). The budget-free
    treatment lives in the ReAct loop, not here; this just delivers the turn. Reuses the
    ``clarify_science`` HumanQuery kind.
    """
    from auto_research.hitl import resolve_human
    from auto_research.schemas import HumanQuery

    query = HumanQuery(
        kind="clarify_science",
        message=str(arguments.get("message") or "(no message provided)"),
        asked_by=asked_by or "agent",
        model=model or "",
    )
    response = await resolve_human(human_handler, query)
    return ToolOutcome(text=response.answer or "(human gave no answer)")


WEB_SEARCH_TOOL = "web_search"


def web_search_tool_def() -> dict:
    """The provider-neutral definition for the shared ``web_search`` tool.

    Backed by a ``WebSource`` (Tavily). Attached to the reviewer, analyst lead, analyst
    workers, and the co-lead so any of them can confirm an uncertain science or code point
    against a primary source (mission analysis threads, calibration memos, demo slides,
    official docs) rather than guessing.
    """
    return {
        "name": WEB_SEARCH_TOOL,
        "description": (
            "Search the web for authoritative documentation — mission analysis threads, "
            "calibration memos, official references, demo slides. Returns the top matches, each "
            "with a title, URL, and a short relevance teaser. Use it to confirm an uncertain "
            "science or code point; call web_fetch on a result's URL to read that page in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords describing the documentation you need.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5).",
                },
            },
            "required": ["query"],
        },
    }


async def execute_web_search(web_source: Any | None, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Run one ``web_search`` via ``web_source`` (in-process Tavily).

    Returns the top-k results, each as a title + URL + the provider's query-aware snippet
    (verbatim — Tavily's snippets are already concise; the top-k ``limit`` is the only cap).
    Provider faults degrade to an error outcome so a hiccup never crashes the ReAct loop;
    ``TAVILY_API_KEY`` is a launch preflight requirement, so the only ``None`` case here is a
    role that was deliberately not given the tool.
    """
    if web_source is None:
        return ToolOutcome(text="web_search not available", is_error=True)
    try:
        results = await web_source.search(
            str(arguments.get("query", "")), limit=int(arguments.get("limit", 5))
        )
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome(text=f"web_search failed: {type(exc).__name__}: {exc}", is_error=True)
    lines = [f"- {r.title} {r.url}\n  {r.abstract or ''}" for r in results]
    return ToolOutcome(text="\n".join(lines) or "(no results)")


WEB_FETCH_TOOL = "web_fetch"


def web_fetch_tool_def() -> dict:
    """The provider-neutral definition for the shared ``web_fetch`` tool.

    Backed by ``WebSource.fetch`` (Tavily Extract → Markdown). The search→fetch second layer:
    ``web_search`` returns teasers, ``web_fetch`` reads one page in full. Attached to the same
    roles as ``web_search``.
    """
    return {
        "name": WEB_FETCH_TOOL,
        "description": (
            "Fetch and read one web page's full content as Markdown (e.g. a result from "
            "web_search, or a workshop/slide/reference URL). Reads BY LINE RANGE like read_file: "
            "when the page is longer than the window, the output ends with a note naming the "
            "offset to continue from — call again with that offset. Links in the page are "
            "preserved, so if the reference you need is behind a link, call web_fetch again on "
            "that link."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL of the page to read."},
                "offset": {
                    "type": "integer",
                    "description": "1-indexed line to start at (default 1).",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum lines to return (default {_DEFAULT_LINE_LIMIT}).",
                },
                "depth": {
                    "type": "string",
                    "enum": ["advanced", "basic"],
                    "description": (
                        "Extraction depth: 'advanced' (default, pulls tables/embedded content) "
                        "or 'basic' (faster, plain pages)."
                    ),
                },
            },
            "required": ["url"],
        },
    }


async def execute_web_fetch(web_source: Any | None, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Fetch one URL via ``web_source.fetch`` and return it as a paged ``cat -n`` line window.

    Faults (network, extraction error, no content) degrade to an error/soft outcome rather than
    crashing the ReAct loop.
    """
    if web_source is None:
        return ToolOutcome(text="web_fetch not available", is_error=True)
    url = str(arguments.get("url", "")).strip()
    if not url:
        return ToolOutcome(text="web_fetch needs a 'url'", is_error=True)
    depth = str(arguments.get("depth") or "advanced")
    try:
        content = await web_source.fetch(url, depth=depth)
    except Exception as exc:  # noqa: BLE001
        return ToolOutcome(text=f"web_fetch failed: {type(exc).__name__}: {exc}", is_error=True)
    if not content:
        return ToolOutcome(
            text=(
                f"(no extractable content at {url} — it may be a JavaScript-rendered viewer; "
                "try a linked source or the page's underlying document URL)"
            )
        )
    page = _page_text_lines(
        content,
        offset=int(arguments.get("offset", 1)),
        limit=int(arguments.get("limit", _DEFAULT_LINE_LIMIT)),
        resume_hint="web_fetch",
    )
    return ToolOutcome(text=page["content"])


# ----------------------------------------------------------------------------
# Inter-LLM report authoring — edit_report / review_report (V-4). Every analyst report
# (worker→lead, lead→reviewer) is a per-round JSONL the agent ReAct-refines through TOOLS,
# so analysts keep their read-only discipline (the runtime writes; the agent never touches
# disk). Figures are cited by a per-source catalog handle ([source:index]); the runtime
# resolves cited_figures → the ordered report.figures (Figure 1 = figures[0]). Wired onto
# both providers via a ReportWorkspace (report path + citable catalog + subordinate reports).
# ----------------------------------------------------------------------------

EDIT_REPORT_TOOL = "edit_report"
REVIEW_REPORT_TOOL = "review_report"


@dataclass
class ReportWorkspace:
    """Per-round report-authoring state wired onto a provider for edit_report/review_report.

    The disciplined write path: the agent never writes files — ``edit_report`` validates its
    args into ``report_model``, resolves ``cited_figures`` against ``catalog``, and writes the
    per-round JSONL to ``report_path``. ``review_report`` reads the draft back and shows the
    citable catalog (all its figures attached as images). ``catalog`` maps a source name
    (``"self"`` for a worker; worker keys for the lead) → that source's append-only figure list;
    ``source_reports`` holds subordinate reports for ``review_report(source)``; ``prior_report``
    is the agent's own last-round report (the base ``review_report(None)`` shows before any edit
    this round); ``written_report`` is set by ``edit_report`` so ``analyze``/``adjudicate`` read
    the persisted result back.
    """

    report_path: Path
    report_model: type  # e.g. AnalystReport — validated in execute_edit_report
    catalog: dict[str, list[FigureRef]]
    own_source: str = "self"
    source_reports: dict[str, Any] = field(default_factory=dict)
    prior_report: Any | None = None
    written_report: Any | None = None
    # Fields carried through edit_report from the tool args onto the report model (report-shape
    # specific; AnalystReport today). cited_figures/figures are handled separately (FiguredReport).
    prose_fields: tuple[str, ...] = ("summary", "findings", "uncertainty", "status", "tool_request")


# Matches a numbered figure reference in prose — "Figure 1", "Fig. 2", "Fig 3", "figs 4" —
# the convention the model is told to use ("refer to the k-th as 'Figure k'"). Word-boundary
# anchored so it never fires inside words like "configure".
_FIGURE_REF_RE = re.compile(r"\bfig(?:ure)?s?\.?\s*(\d+)", re.IGNORECASE)


def _referenced_figure_numbers(*texts: str | None) -> set[int]:
    """Figure numbers the prose refers to (e.g. {1, 2} for a report that says "Figure 1 … Fig. 2")."""
    numbers: set[int] = set()
    for text in texts:
        if not text:
            continue
        for match in _FIGURE_REF_RE.findall(text):
            try:
                numbers.add(int(match))
            except ValueError:  # pragma: no cover — regex guarantees digits
                pass
    return numbers


def resolve_figure_citations(
    cited: Sequence[FigureCitation], catalog: Mapping[str, list[FigureRef]]
) -> tuple[list[FigureRef], list[str]]:
    """Resolve ``{source, index}`` citations to real ``FigureRef``s against ``catalog``.

    Out-of-range / unknown-source citations are dropped with a note (never fatal); a figure
    cited twice keeps its first placement (first-wins). Returns ``(figures, drop_notes)`` — the
    ordered resolved list (report-local Figure 1..M) and human-readable drop reasons.
    """
    figures: list[FigureRef] = []
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for c in cited:
        lst = catalog.get(c.source)
        if lst is None:
            have = ", ".join(k for k, v in catalog.items() if v) or "none"
            notes.append(f"{c.source}:{c.index} — unknown source (citable sources: {have})")
            continue
        if not (1 <= c.index <= len(lst)):
            notes.append(f"{c.source}:{c.index} — out of range (source '{c.source}' has {len(lst)})")
            continue
        fig = lst[c.index - 1]
        key = (str(fig.path), fig.sha256)
        if key in seen:
            continue
        seen.add(key)
        figures.append(fig)
    return figures, notes


def _catalog_menu(catalog: Mapping[str, list[FigureRef]]) -> tuple[str, list[FigureRef]]:
    """Render the citable catalog as a per-source menu (text) + display-copy images.

    Each figure is addressed by its selection handle ``[source:index]`` — both in the text and
    on the image caption — so the model picks unambiguously. Empty sources are skipped.
    """
    lines: list[str] = []
    images: list[FigureRef] = []
    for source, figs in catalog.items():
        if not figs:
            continue
        lines.append(f"Source {source} ({len(figs)} figure(s)):")
        for i, fig in enumerate(figs, 1):
            lines.append(f"  [{source}:{i}] {fig.caption}  (from {fig.produced_by})")
            images.append(fig.model_copy(update={"caption": f"[{source}:{i}] {fig.caption}"}))
    return ("\n".join(lines) or "(no figures produced yet)"), images


def _report_view(report: Any) -> tuple[str, list[FigureRef]]:
    """Render a finished report as prose + a numbered ``Figure N`` manifest + display-copy images.

    The report-local numbering (Figure 1 = ``figures[0]``) matches the prose, and each attached
    image caption is prefixed ``Figure N —`` so the reader lines images up with the prose (V-4).
    """
    parts: list[str] = []
    summary = getattr(report, "summary", None)
    if summary:
        parts.append(f"summary: {summary}")
    findings = getattr(report, "findings", None)
    if findings:
        parts.append("findings:\n" + "\n".join(f"  - {x}" for x in findings))
    uncertainty = getattr(report, "uncertainty", None)
    if uncertainty:
        parts.append(f"uncertainty: {uncertainty}")
    status = getattr(report, "status", None)
    if status and status != "ok":
        parts.append(f"status: {status}")
    images: list[FigureRef] = []
    figures = getattr(report, "figures", None) or []
    if figures:
        manifest = ["figures in this report:"]
        for n, fig in enumerate(figures, 1):
            manifest.append(f"  Figure {n}: {fig.caption}  (from {fig.produced_by})")
            images.append(fig.model_copy(update={"caption": f"Figure {n} — {fig.caption}"}))
        parts.append("\n".join(manifest))
    return ("\n\n".join(parts) or "(empty report)"), images


def edit_report_tool_def() -> dict:
    """Definition for ``edit_report`` — the disciplined write path for an ``AnalystReport``.

    The agent supplies the whole report each call (a call overwrites the last), citing figures by
    their ``[source:index]`` catalog handle in ``cited_figures``; the runtime resolves and places
    exactly those, in order, and the agent refers to the k-th as "Figure k" in its prose.
    """
    return {
        "name": EDIT_REPORT_TOOL,
        "description": (
            "Write (or rewrite) your report — this is the ONLY way it is saved; you never write "
            "files yourself. Provide the full report each call (a call overwrites the last), so "
            "refine across calls. Cite figures in 'cited_figures' by the [source:index] handle "
            "from review_report, IN THE ORDER you want them shown — the runtime places exactly "
            "those and you refer to the k-th as 'Figure k' in your prose; a figure you omit is "
            "dropped. After writing, call review_report to see the placement and refine until "
            "every figure is in the right place."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "The report summary (prose). Refer to figures as 'Figure 1', 'Figure 2', "
                        "… matching the order of cited_figures."
                    ),
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key findings, each a self-contained prose string.",
                },
                "cited_figures": {
                    "type": "array",
                    "description": (
                        "Figures to place, in citation order. Pick from review_report's catalog by "
                        "its [source:index] handle; 'Figure k' in your prose is the k-th entry here."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "Catalog source, e.g. 'self' or 'analyst_worker_0'.",
                            },
                            "index": {
                                "type": "integer",
                                "description": "1-based slot within that source (its [source:index] number).",
                            },
                        },
                        "required": ["source", "index"],
                    },
                },
                "uncertainty": {
                    "type": "string",
                    "description": "The dominant uncertainties / caveats (optional).",
                },
                "status": {
                    "type": "string",
                    "enum": ["ok", "failed", "needs_human"],
                    "description": (
                        "'ok' when you completed the work; 'failed' / 'needs_human' are honest, "
                        "penalty-free outcomes — never fabricate a result to force 'ok'."
                    ),
                },
                "tool_request": {
                    "type": "object",
                    "description": (
                        "Raise ONLY when the toolkit lacks a capability the task needs (or an "
                        "existing tool misbehaves); otherwise omit. The analyst lead finalises the "
                        "tool from this."
                    ),
                    "properties": {
                        "request_kind": {
                            "type": "string",
                            "enum": ["new_capability", "fix_existing"],
                        },
                        "science_context": {
                            "type": "string",
                            "description": "What is being measured; why the toolkit can't do it.",
                        },
                        "expected_behavior": {
                            "type": "string",
                            "description": "Prose signature + I/O the tool should provide.",
                        },
                        "proposed_tool_name": {
                            "type": "string",
                            "description": "A suggested tool/function name (the lead may rename).",
                        },
                        "observed_issue": {
                            "type": "string",
                            "description": "For fix_existing: the wrong/surprising behaviour observed.",
                        },
                        "validation_data": {
                            "type": "array",
                            "description": "At least one real ObsID the tool's checks run against.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "mission": {"type": "string"},
                                    "obsid": {"type": "string"},
                                    "synthetic": {"type": "boolean"},
                                },
                                "required": ["mission", "obsid"],
                            },
                        },
                        "notes": {"type": "string"},
                    },
                    "required": [
                        "science_context",
                        "expected_behavior",
                        "proposed_tool_name",
                        "validation_data",
                    ],
                },
            },
            "required": ["summary"],
        },
    }


def review_report_tool_def() -> dict:
    """Definition for ``review_report`` — read a report back with its figures attached (V-4)."""
    return {
        "name": REVIEW_REPORT_TOOL,
        "description": (
            "Read a report back with its figures attached as images. Call with no argument to see "
            "your own current draft plus every figure you can cite (each labelled [source:index]) "
            "— use it to check that each figure is placed right, then edit_report again to refine. "
            "Pass 'source' (e.g. 'analyst_worker_0') to read a subordinate's finished report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": (
                        "Whose report to read: omit for your own draft + your citable catalog; a "
                        "worker key to read that worker's finished report."
                    ),
                },
            },
        },
    }


async def execute_edit_report(
    workspace: ReportWorkspace | None, arguments: Mapping[str, Any]
) -> ToolOutcome:
    """Validate the report args, resolve figure citations, persist the per-round JSONL (V-4).

    The single disciplined write path: the agent never touches disk. A shape/validation error
    degrades to an error outcome so the model self-corrects in the loop.
    """
    if workspace is None:
        return ToolOutcome(text="edit_report is not available for this role.", is_error=True)
    cited: list[FigureCitation] = []
    for entry in arguments.get("cited_figures") or []:
        try:
            cited.append(FigureCitation.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 — bad handle → tell the model to fix it
            return ToolOutcome(
                text=f"edit_report: bad cited_figures entry {entry!r}: {exc}", is_error=True
            )
    figures, drop_notes = resolve_figure_citations(cited, workspace.catalog)
    fields: dict[str, Any] = {
        key: arguments[key]
        for key in workspace.prose_fields
        if key in arguments and arguments[key] is not None
    }
    fields["cited_figures"] = [c.model_dump() for c in cited]
    fields["figures"] = [f.model_dump(mode="json") for f in figures]
    try:
        report = workspace.report_model.model_validate(fields)
    except Exception as exc:  # noqa: BLE001 — off-schema report → self-correct and call again
        return ToolOutcome(
            text=f"edit_report: the report did not validate — fix and call again: {exc}",
            is_error=True,
        )
    workspace.report_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.report_path.write_text(report.model_dump_json() + "\n", encoding="utf-8")
    workspace.written_report = report

    lines = [f"Report saved — {len(report.figures)} figure(s) placed:"]
    for n, fig in enumerate(report.figures, 1):
        lines.append(f"  Figure {n}: {fig.caption}")
    if drop_notes:
        lines.append("Citations dropped (fix or re-cite): " + "; ".join(drop_notes))
    placed = {(str(f.path), f.sha256) for f in report.figures}
    uncited = [
        f"[{source}:{i}] {fig.caption}"
        for source, figs in workspace.catalog.items()
        for i, fig in enumerate(figs, 1)
        if (str(fig.path), fig.sha256) not in placed
    ]
    if uncited:
        lines.append(
            "Not cited (dropped from the report, still citable): " + "; ".join(uncited)
        )

    # Figure-reference guard: if the prose refers to figures the report did not place, warn so the
    # model re-cites (a warning, not an error — the report is saved). Catches the failure where a
    # model narrates "Figure 1 … Figure 4" in the summary but leaves cited_figures empty/short, so
    # the referenced figures point at nothing (CLAUDE.md §13). Both tools are budget-free, so the
    # model can immediately review_report + edit_report again to fix placement.
    referenced = _referenced_figure_numbers(
        getattr(report, "summary", None),
        *(getattr(report, "findings", None) or []),
        getattr(report, "uncertainty", None),
    )
    n_placed = len(report.figures)
    if referenced and (n_placed == 0 or max(referenced) > n_placed):
        catalog_has = any(figs for figs in workspace.catalog.values())
        lines.append(
            f"Note: your report seems to reference Figure(s) {sorted(referenced)} but attached "
            f"only {n_placed}. Did you mean that? If you want those figures shown, add them to "
            "cited_figures"
            + (
                " — you can call review_report to see your catalog and what will be presented."
                if catalog_has
                else " (there are no figures in your catalog yet)."
            )
        )
    return ToolOutcome(text="\n".join(lines))


async def execute_review_report(
    workspace: ReportWorkspace | None, arguments: Mapping[str, Any]
) -> ToolOutcome:
    """Read a report back with figures attached (V-4).

    No ``source`` → the agent's own current draft + its citable catalog (every figure attached,
    ``[source:index]``-labelled). A ``source`` → that subordinate's finished report (numbered
    ``Figure N`` images). Always attaches figures, unified for self- and upper-review.
    """
    if workspace is None:
        return ToolOutcome(text="review_report is not available for this role.", is_error=True)
    source = str(arguments.get("source") or "").strip()
    if source and source not in ("self", workspace.own_source):
        report = workspace.source_reports.get(source)
        if report is None:
            have = ", ".join(workspace.source_reports) or "none"
            return ToolOutcome(
                text=f"review_report: no report for source '{source}' (available: {have})",
                is_error=True,
            )
        text, images = _report_view(report)
        return ToolOutcome(text=f"Report from {source}:\n\n{text}", figures=images)

    draft = workspace.written_report or workspace.prior_report
    if draft is not None:
        draft_text, _ = _report_view(draft)
        header = "Your current report draft:\n\n" + draft_text
    else:
        header = "You have not written a report yet — call edit_report to write one."
    menu_text, menu_images = _catalog_menu(workspace.catalog)
    text = (
        header
        + "\n\n--- figures you can cite (pick by [source:index], in the order you want them shown) ---\n"
        + menu_text
    )
    return ToolOutcome(text=text, figures=menu_images)


# ----------------------------------------------------------------------------
# Lead-only tool-authoring tools (§9.3, B-topology). The analyst lead drives a live
# AuthoringSession: it steers the Sonnet coder (drive_coder), submits to the deterministic
# gate + GPT co-lead review (submit_for_review), and — slice 4 — debates a review challenge
# (respond_to_review). Masked to the lead alone (a session is wired only on its provider).
# ----------------------------------------------------------------------------

DRIVE_CODER_TOOL = "drive_coder"
SUBMIT_TOOL = "submit_for_review"
RESPOND_TO_REVIEW_TOOL = "respond_to_review"


def drive_coder_tool_def() -> dict:
    """Lead-only: instruct the Sonnet coder and read back its work. The lead is the plan owner;
    the coder executes. Budget-free in the lead's authoring loop (set via ``free_tools``)."""
    return {
        "name": DRIVE_CODER_TOOL,
        "description": (
            "Instruct the coder (a Sonnet Claude-Code session) to write or revise the tool, then "
            "read back what it did. The coder executes your plan — give concrete, algorithm-level "
            "instructions (signature, the exact procedure, edge cases, the tests to add), not a "
            "restated goal. It keeps the working files across your instructions, so iterate: "
            "inspect its diff with read_file, then drive_coder again. When the tool and its tests "
            "are right, call submit_for_review. Write 'instructions' as Markdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instructions": {
                    "type": "string",
                    "description": (
                        "What the coder should do next, in Markdown — concrete and algorithm-level. "
                        "On the first call, seed the full brief; afterwards, the specific change."
                    ),
                },
            },
            "required": ["instructions"],
        },
    }


def submit_tool_def() -> dict:
    """Lead-only: run the gate + co-lead review over the coder's current work; commit on
    gate-pass ∧ review-pass (the lead never self-approves)."""
    return {
        "name": SUBMIT_TOOL,
        "description": (
            "Submit the coder's current work to the deterministic gate and the independent GPT "
            "co-lead review. The gate is mechanical (determinism, no-fabrication, structure, the "
            "tool's own tests) — you can only satisfy it, by fixing the code via drive_coder. The "
            "review judges whether the plan and its tests are right and strict enough. On gate-pass "
            "AND review-pass the tool is committed automatically — you never self-approve. If the "
            "gate fails, fix and submit again; if the review rejects, either drive_coder to address "
            "it or respond_to_review to debate it. This is the ONLY way to finish: your authored "
            "tool is committed solely through this call — if your turn ends without calling it (or "
            "respond_to_review), the work is discarded as a failure. When the tool and its tests are "
            "right, call it; if after your best effort it genuinely cannot be made mergeable, say so "
            "explicitly in one line rather than stopping silently. Takes no arguments."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }


async def execute_drive_coder(session: Any, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Drive one coder turn over the live ``AuthoringSession`` (B-topology: lead steers, the
    Sonnet coder executes). Returns the coder's narration for the lead to inspect."""
    instructions = str(arguments.get("instructions") or "").strip()
    if not instructions:
        return ToolOutcome(
            text="drive_coder needs 'instructions': what the coder should do next.", is_error=True
        )
    response = await session.drive(instructions)
    return ToolOutcome(
        text=(response or "").strip()
        or "(coder produced no narration; inspect the worktree with read_file, or submit_for_review "
        "to run the gate.)"
    )


async def execute_submit(session: Any) -> ToolOutcome:
    """Run the gate, then (gate-pass) the co-lead review, over the session's current tree; commit
    on gate-pass ∧ review-pass (§9.9 commit ⇔ gate ∧ review — the lead never self-approves).
    Returns a verdict the lead diagnoses: a gate failure (fix via drive_coder), a review rejection
    (drive_coder or respond_to_review), or a successful commit. Bounded by ``session.submit_cap``
    (each submit runs the gate twice + a GPT review — expensive)."""
    if session.submits_used >= session.submit_cap:
        return ToolOutcome(
            text=(
                f"You have used all {session.submit_cap} review submissions for this tool. Make a "
                "final call: if your last submit committed, you are done; otherwise this authoring "
                "attempt ends without a mergeable tool — an honest failure is an acceptable, "
                "correct outcome (do not fabricate to force a pass)."
            )
        )
    session.submits_used += 1
    gate = await session.run_gate_step()
    if not gate.passed:
        failed = [
            f"- [{c.layer}] {c.name}: {c.detail}"
            for c in (*gate.layer_a, *gate.layer_b)
            if not c.passed
        ]
        body = "\n".join(failed) or gate.reason
        return ToolOutcome(
            text=(
                "GATE FAILED — mechanical checks you can only satisfy. Fix the underlying cause via "
                f"drive_coder (never weaken a check to pass), then submit_for_review again:\n{body}"
            )
        )
    review = await session.run_review_step(gate)
    if not review.approved:
        issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
        return ToolOutcome(
            text=(
                "GATE PASSED, but the independent co-lead REVIEW REJECTED the tool:\n"
                f"{issues}\n\n{review.rationale}\n\nEither drive_coder to address these, or "
                "respond_to_review to defend the design if you believe a challenge is unfounded."
            )
        )
    sha = await session.commit(
        review.commit_summary.strip() or session.tool_spec.proposed_tool_name
    )
    return ToolOutcome(
        text=(
            f"COMMITTED {session.tool_spec.proposed_tool_name} → {sha[:10]}. Gate passed and the "
            "co-lead approved — the tool is on the authoring branch. Authoring for this tool is done."
        )
    )


def respond_to_review_tool_def() -> dict:
    """Lead-only: debate the co-lead's most recent rejection (§9.3 — the lead persuades, never
    overrides). Budget-free in the lead's loop; bounded by ``session.debate_cap``."""
    return {
        "name": RESPOND_TO_REVIEW_TOOL,
        "description": (
            "Respond to the co-lead's most recent review rejection — defend the design or a specific "
            "test choice when you believe the challenge is unfounded or already addressed. The "
            "independent GPT co-lead reconsiders your argument and re-issues its verdict: it concedes "
            "only if genuinely persuaded (a sound argument, or evidence the issue is already handled) "
            "and holds firm against pressure. If it is persuaded and the gate still passes, the tool "
            "commits. Use this for a genuine disagreement; when the co-lead is right, fix the code via "
            "drive_coder instead. Write 'message' as Markdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Your argument to the co-lead, in Markdown — address its specific issues, "
                        "citing where the code/tests already handle them or why the concern is wrong."
                    ),
                },
            },
            "required": ["message"],
        },
    }


async def execute_respond_to_review(session: Any, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Re-run the co-lead review with the lead's rebuttal in view (§9.3 debate). On a concede, the
    immovable gate is re-validated before committing (the tree may have changed); commit ⇔ gate ∧
    review still holds. Bounded by ``session.debate_cap``."""
    message = str(arguments.get("message") or "").strip()
    if not message:
        return ToolOutcome(
            text="respond_to_review needs 'message': your argument to the co-lead.", is_error=True
        )
    if session.last_gate is None or session.last_review is None:
        return ToolOutcome(
            text="There is no co-lead review to respond to yet — call submit_for_review first.",
            is_error=True,
        )
    if session.debates_used >= session.debate_cap:
        return ToolOutcome(
            text=(
                f"You have used all {session.debate_cap} debate turns. Either address the issues "
                "via drive_coder, or conclude — an honest failure is an acceptable outcome."
            )
        )
    session.debates_used += 1
    review = await session.run_review_step(session.last_gate, lead_rebuttal=message)
    if not review.approved:
        issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
        return ToolOutcome(
            text=(
                "The co-lead HELD its position after your rebuttal:\n"
                f"{issues}\n\n{review.rationale}\n\nAddress these via drive_coder, or accept the "
                "challenge and conclude (an honest failure is acceptable)."
            )
        )
    # Persuaded — re-validate the immovable gate (the tree may have changed) before committing.
    gate = await session.run_gate_step()
    if not gate.passed:
        failed = "\n".join(
            f"- [{c.layer}] {c.name}: {c.detail}"
            for c in (*gate.layer_a, *gate.layer_b)
            if not c.passed
        ) or gate.reason
        return ToolOutcome(
            text=(
                "The co-lead is persuaded, but the gate no longer passes (did the tree change?):\n"
                f"{failed}\nFix via drive_coder, then submit_for_review."
            )
        )
    sha = await session.commit(
        review.commit_summary.strip() or session.tool_spec.proposed_tool_name
    )
    return ToolOutcome(
        text=(
            f"The co-lead was PERSUADED and the gate passed — COMMITTED "
            f"{session.tool_spec.proposed_tool_name} → {sha[:10]}. Authoring for this tool is done."
        )
    )


# ----------------------------------------------------------------------------
# Analyst science tools — the existing ToolkitRegistry + PersistentSandbox.
# ----------------------------------------------------------------------------

_PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"stdout", "stderr", "exit_code", "figures", "console_tail"}
)


def _observation_path_args() -> dict[str, str]:
    """Map tool path-args to configured observation roots, e.g. ``ixpe_path`` →
    the IXPE ``obs_db_path`` from ``config/observations.yaml``.

    Convention: mission ``<m>`` in observations.yaml fills a tool argument named
    ``<m>_path``.  Used to auto-supply instrument data paths so the model need not.
    """
    try:
        cfg = load_observations_config()
    except Exception:  # noqa: BLE001
        return {}
    return {f"{mission}_path": str(c.obs_db_path) for mission, c in cfg.missions.items()}


def _normalize_tool_raw(raw: dict) -> dict:
    """Ensure every tool result has a non-empty ``stdout`` for the model to read.

    Tools that return structured payloads without a ``stdout`` key (e.g.
    ``list_dir`` → ``{path, entries}``, ``load_data`` → nested indices dict)
    are invisible to the model otherwise.  When ``stdout`` is absent or empty,
    serialize the non-protocol payload keys as compact JSON. Also folds any
    captured ``console_tail`` (XSPEC/C-level prints) into ``stdout``.
    """
    stdout = raw.get("stdout") or ""
    if not stdout:
        extra = {k: v for k, v in raw.items() if k not in _PROTOCOL_KEYS}
        if extra:
            stdout = json.dumps(extra, indent=2, default=str)

    console = raw.get("console_tail") or ""
    if console:
        sep = "\n--- console ---\n"
        stdout = (stdout + sep + console) if stdout else (sep.lstrip() + console)

    return {**raw, "stdout": stdout}


def _parse_tool_figures(raw_figures: Any) -> tuple[list[FigureRef], list[str]]:
    """Validate returned figure dicts into ``FigureRef``s, **dropping** (never crashing on)
    malformed entries.

    A tool — most often a freshly authored plotter — that emits a slightly-off figure dict
    (bad ``sha256``, an extra key under ``extra="forbid"``, a missing field, a bad
    ``created_at``) must not abort the whole run. Each bad entry is dropped and a short reason
    is returned so the analyst learns its plotter is malformed and can fix/re-author it — this
    is what ``coder.md`` already promises ("that figure is dropped"). Returns
    ``(figures, drop_notes)``.
    """
    figures: list[FigureRef] = []
    notes: list[str] = []
    if not isinstance(raw_figures, list):
        if raw_figures:
            notes.append(f"'figures' is not a list ({type(raw_figures).__name__}); ignored")
        return figures, notes
    for index, entry in enumerate(raw_figures):
        try:
            figures.append(FigureRef.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 — any malformed figure is dropped, never fatal
            notes.append(f"figure[{index}] dropped — invalid FigureRef: {exc}")
    return figures, notes


class SandboxToolProvider:
    """Analyst tool surface: ``ToolkitRegistry`` tools run in a ``PersistentSandbox``.

    Mirrors the per-call routing/argument-resolution the old ``execute_tools`` did:
    map the provider-safe function name back to the dotted ``kind``, auto-inject
    ``cwd`` for ``bash_exec`` and instrument data paths for toolkit tools, then call
    the sandbox. The model self-corrects on errors via the ReAct loop (it sees the
    error text and re-issues), so there is no separate retry here. Every executed
    ``ToolResult`` accumulates in ``results`` for memory/peer-summary use.
    """

    def __init__(
        self,
        registry: Any,  # ToolkitRegistry — avoid import cycle
        sandbox: Any,  # PersistentSandbox
        *,
        cwd: Path | None = None,
        timeout_s: int = 300,
        human_handler: Any | None = None,
        web_source: Any | None = None,
        asked_by: str = "analyst_worker",
        model: str = "",
        figure_sink: list[FigureRef] | None = None,
        report_workspace: ReportWorkspace | None = None,
    ) -> None:
        self.registry = registry
        self.sandbox = sandbox
        self.cwd = cwd
        # When a human_handler is wired, expose the shared ask_human tool (§9.7).
        self.human_handler = human_handler
        # V-4: the agent's append-only figure catalog (its "self" source). Figures produced by
        # tool calls are appended here (deduped by path+sha), so they persist across debate rounds
        # and the agent can cite them by [self:index] via review_report/edit_report.
        self.figure_sink = figure_sink
        # V-4: the per-round report-authoring workspace. When set, exposes edit_report/review_report
        # so the worker ReAct-refines its report through tools (never writing files itself).
        self.report_workspace = report_workspace
        # Optional Tavily/web QuerySource — when supplied, exposes the shared web_search tool so
        # the worker can confirm an authoritative-doc point (mission threads, calibration memos)
        # rather than guessing (its prompt promises this). In-process, never in the sandbox.
        self.web_source = web_source
        self.asked_by = asked_by
        self.model = model
        # Figures land in <worker scratch>/figures (under the runtime data dir),
        # never in /tmp or a repo. See _redirect_figure_args.
        self.figures_dir = (Path(cwd) / "figures") if cwd is not None else None
        self._figures_dir_ready = False
        self.timeout_s = timeout_s
        self.results: list[ToolResult] = []
        # Filesystem jail (CLAUDE.md §2/§12): the worker's builtin file tools are otherwise raw.
        # Inactive (allow-all) when agent_fs_allow is unset. Toolkit tools take obs/data paths and
        # run in the sandbox — not gated here (OS jail closes that, §12 / Loose ends).
        self._guard = PathGuard.from_config()

    def tool_definitions(self) -> list[dict]:
        # ``data_catalog`` (research_toolkit.catalog_utils) is one of the registry tools, so it
        # is already included and runs in the sandbox like any toolkit tool — no built-in add.
        #
        # Tool search (config ``tool_search_enabled``): every *registry* tool is marked
        # ``defer_loading``, so its schema stays out of the model's context until the model
        # discovers it through the provider's search tool; the harness tools appended below
        # stay resident, which also satisfies both providers' "at least one non-deferred
        # tool" requirement. No per-tool exceptions — deferring uniformly keeps the rule
        # legible and costs at most one search turn at the start of a run. The adapters read
        # the flag and drop it from the wire when the provider has no tool search.
        defer = tool_search_config().enabled
        defs = [
            ({**d, "defer_loading": True} if defer else d)
            for d in self.registry.tool_definitions()
        ]
        if self.human_handler is not None:
            defs.append(ask_human_tool_def())
        if self.web_source is not None:
            defs.append(web_search_tool_def())
            defs.append(web_fetch_tool_def())
        if self.report_workspace is not None:
            defs.append(edit_report_tool_def())
            defs.append(review_report_tool_def())
        return defs

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        # ``data_catalog`` is a registry tool here — it falls through to the sandbox path below,
        # not a special in-process branch (only the sandbox-less lead runs it in-process).
        if name == ASK_HUMAN_TOOL:
            return await execute_ask_human(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        if name == WEB_SEARCH_TOOL:
            return await execute_web_search(self.web_source, arguments)
        if name == WEB_FETCH_TOOL:
            return await execute_web_fetch(self.web_source, arguments)
        if name == EDIT_REPORT_TOOL:
            return await execute_edit_report(self.report_workspace, arguments)
        if name == REVIEW_REPORT_TOOL:
            return await execute_review_report(self.report_workspace, arguments)
        kind = self.registry.kind_for_function(name)
        args = dict(arguments)
        if kind == "mcp.builtin.bash_exec" and "cwd" not in args and self.cwd is not None:
            args["cwd"] = str(self.cwd)

        # Filesystem allow-list on the builtin file tools (the raw read/list/bash surface).
        # bash_exec is checked by the absolute paths it NAMES (best-effort); read_file/list_dir
        # by their target. Denies the auto_research repo + governance files (§2/§4).
        denied = None
        if kind == "mcp.builtin.bash_exec":
            denied = self._guard.forbidden_in_command(args.get("command", ""))
        elif kind in ("mcp.builtin.read_file", "mcp.builtin.list_dir"):
            target = args.get("path")
            if target is not None and not self._guard.is_allowed(target):
                denied = str(target)
        if denied is not None:
            return ToolOutcome(
                text=(
                    f"path not permitted: {denied} — outside the agent filesystem allow-list "
                    f"(research_toolkit / obsdb / analyst-env / HEASoft). The auto_research "
                    f"orchestration repo and governance files are off-limits (§2/§4)."
                ),
                is_error=True,
            )

        # Sandbox call timeout: the tool's declared ceiling (tool_config.yaml; default 300s),
        # the hard SIGTERM backstop that lets a long-running tool (e.g. a HEASARC download) run
        # beyond the protective 300s cap (calls over it die with exit 124) — see ToolkitRegistry.
        # It is fixed per tool, NOT model-tunable: a tool's *own* timeout argument (e.g. acquire's
        # astroquery timeout_s) is the caller's graceful runtime knob, and the ceiling is set a bit
        # above it so the tool times out cleanly before this hard kill.
        ceiling_fn = getattr(self.registry, "call_timeout_ceiling", None)
        call_timeout = ceiling_fn(kind) if ceiling_fn is not None else self.timeout_s

        t0 = time.monotonic()
        if not self.registry.is_known(kind):
            raw = {"exit_code": 127, "stderr": f"Tool not registered: {kind}", "stdout": ""}
        elif self.registry.is_mcp(kind):
            raw = await self.sandbox.call_mcp_tool(kind, args, timeout_s=call_timeout)
        else:
            spec = self.registry.spec(kind)
            schema_props = (spec.get("args_schema") or {}).get("properties", {})
            for arg_name, path in _observation_path_args().items():
                if arg_name in schema_props and not args.get(arg_name):
                    args[arg_name] = path
            self._redirect_figure_args(spec["fn"], args, schema_props)
            raw = await self.sandbox.call_tool(
                spec["module"], spec["fn"], args, timeout_s=call_timeout
            )
        elapsed = time.monotonic() - t0

        raw = _normalize_tool_raw(raw)
        figures, figure_notes = _parse_tool_figures(raw.get("figures"))
        # V-4: accumulate produced figures into the agent's append-only "self" catalog (deduped by
        # path+sha), so they persist across rounds and stay citable via review_report/edit_report.
        if self.figure_sink is not None and figures:
            existing = {(str(f.path), f.sha256) for f in self.figure_sink}
            for fig in figures:
                key = (str(fig.path), fig.sha256)
                if key not in existing:
                    existing.add(key)
                    self.figure_sink.append(fig)
        result = ToolResult(
            tool_name=kind,
            stdout=raw.get("stdout", ""),
            stderr=raw.get("stderr", ""),
            exit_code=raw.get("exit_code", 1),
            figures=figures,
            elapsed_s=elapsed,
        )
        self.results.append(result)
        if result.exit_code == 0:
            text = result.stdout or "(tool returned no output)"
        else:
            text = f"[exit {result.exit_code}]\n{result.stderr or result.stdout or '(no error detail)'}"
        # Surface any dropped figures so the analyst notices its plotter emitted a bad
        # FigureRef (rather than the figure silently vanishing) — never fatal (F4).
        if figure_notes:
            text += f"\n[figure capture: {len(figure_notes)} dropped] " + " | ".join(figure_notes)
        return ToolOutcome(text=text, is_error=result.exit_code != 0, figures=figures, result=result)

    def _redirect_figure_args(self, fn: str, args: dict, schema_props: Mapping) -> None:
        """Force figure-output paths under the worker's ``figures_dir``.

        Keyed on the **arg-name convention** (``_FIGURE_PATH_ARGS``), not on specific
        tool kinds, so any toolkit tool that follows the convention places its PNGs
        under the run's figures dir (inside the runtime data dir) instead of /tmp,
        the repo, or wherever the model happened to name. The model's chosen basename
        is preserved — only the directory and the extension convention are enforced —
        so captions/provenance stay meaningful. A no-op when no ``cwd`` is attached
        (e.g. Studio) or the tool exposes no figure arg.
        """
        if self.figures_dir is None:
            return
        touched = False
        for arg_name, ext in _FIGURE_PATH_ARGS.items():
            if arg_name not in schema_props:
                continue
            value = args.get(arg_name)
            base = os.path.basename(value.strip()) if isinstance(value, str) and value.strip() else ""
            if not base:
                base = f"{fn}_{int(time.time() * 1000)}"
            low = base.lower()
            for known in (".png", ".pdf", ".jpg", ".jpeg", ".svg"):
                if low.endswith(known):
                    base = base[: -len(known)]
                    break
            args[arg_name] = str(self.figures_dir / f"{base}{ext}")
            touched = True
        if touched and not self._figures_dir_ready:
            self.figures_dir.mkdir(parents=True, exist_ok=True)
            self._figures_dir_ready = True


# ----------------------------------------------------------------------------
# Monitor tools — the `/status` reporter reads the run's own ledger (§J1).
# ----------------------------------------------------------------------------

# Defaults chosen so every result is bounded *by construction* rather than by prompting. An
# agent reading its own run log is the one place a harness can feed itself without limit —
# openai/codex#27131 is exactly that, a session log re-ingested into runaway token growth — so
# no tool here can return an unbounded slice, whatever arguments the model passes.
_HISTORY_LIMIT = 80
_HISTORY_MAX_LIMIT = 400
_EVENT_WINDOW_CHARS = 8_000
_EVENT_MAX_WINDOW_CHARS = 32_000
_GREP_LIMIT = 40


def _read_ledger(path: Path | None) -> list[dict]:
    """Every event in the run's monitor JSONL, tolerant of a torn final line.

    Re-read per call rather than cached: the run is still appending while `/status` is asked, so
    a fresh read is both simpler and more current. A hard kill can leave a half-written last
    line, which is skipped (the ledger is append-only, so nothing earlier is affected).
    """
    if path is None:
        return []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _elapsed_seconds(event: dict, origin: str | None) -> float | None:
    """Seconds from the run's first event to this one (None when either stamp is unreadable)."""
    stamp = event.get("created_at")
    if not (isinstance(stamp, str) and isinstance(origin, str)):
        return None
    try:
        return (
            datetime.fromisoformat(stamp) - datetime.fromisoformat(origin)
        ).total_seconds()
    except ValueError:
        return None


def _history_row(event: dict, origin: str | None) -> str:
    """One ledger event as one line: ``seq · Lxx · $cost · +elapsed · team · actor · summary``.

    Cost and elapsed ride along because two of the three things `/status` is asked — where the
    run is, what it has produced, what it has spent — are answerable from the index alone; without
    them the model has to open an event to learn something the row could have told it.
    """
    elapsed = _elapsed_seconds(event, origin)
    cost = event.get("cost_usd_total")
    return " · ".join(
        [
            f"#{event.get('sequence', '?')}",
            f"L{event.get('level', '?')}",
            f"${float(cost):.2f}" if isinstance(cost, (int, float)) else "$-",
            f"+{int(elapsed // 60)}m" if elapsed is not None else "+?",
            str(event.get("team", "?")),
            str(event.get("actor") or "-"),
            f"{event.get('event', '?')} — {event.get('summary', '')}",
        ]
    )


class MonitorToolProvider:
    """Read-only tools over one run's monitor JSONL, for the `/status` reporter.

    The ledger is the run's single source of truth (§J1) — every node, verdict, tool call, gate
    result, HITL command and priced model call, in order, with its `data` payload. The old
    `/status` flattened the last 80 events into `[Lx] team · event — summary` strings and dropped
    every payload, so a question like "what did the reviewer decide?" could see *that* a verdict
    happened and never what it said. These three tools let the reporter search the index and then
    open what it found, instead of being handed a fixed window someone else chose.

    Read-only by construction, not by instruction: no tool takes a path, so there is nothing to
    confine — the provider is built with the run's own ledger path and can reach nothing else.
    """

    def __init__(self, jsonl_path: Path | None) -> None:
        self.jsonl_path = Path(jsonl_path) if jsonl_path is not None else None

    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "brief_history",
                "description": (
                    "Index of the run's event log: one line per event, newest last. Filter, then "
                    "open anything interesting with ledger_event. Levels run 1 (most important) "
                    "to 10 (chattiest), so max_lvl=4 means significant events only."
                ),
                "input_schema": {
                    "type": "object",
                    "description": (
                        "All filters combine (AND). Start broad (max_lvl=4) to see the shape of "
                        "the run, then narrow with event_prefix or actor."
                    ),
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Rows to return (default {_HISTORY_LIMIT}, max "
                                f"{_HISTORY_MAX_LIMIT}), counted from the end that `order` names."
                            ),
                        },
                        "order": {
                            "type": "string",
                            "enum": ["tail", "head"],
                            "description": (
                                "Which end to take: 'tail' (default) for what just happened, "
                                "'head' for how the run began. Rows always read oldest→newest."
                            ),
                        },
                        "min_lvl": {
                            "type": "integer",
                            "description": "Keep events at this level or chattier (1-10).",
                        },
                        "max_lvl": {
                            "type": "integer",
                            "description": (
                                "Keep events at this level or MORE important (1-10). 4 is the "
                                "significant-events line: node starts, verdicts, gate results, "
                                "commits, human questions."
                            ),
                        },
                        "min_seq": {
                            "type": "integer",
                            "description": "Keep events with sequence >= this.",
                        },
                        "max_seq": {
                            "type": "integer",
                            "description": "Keep events with sequence <= this.",
                        },
                        "event_prefix": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Keep events whose dotted name starts with any of these — event "
                                "names are hierarchical (`analyst_lead.drive_authoring."
                                "tool_result`), so a prefix selects one role's whole activity."
                            ),
                        },
                        "actor": {
                            "type": "string",
                            "description": (
                                "Keep one actor's events. Actors are indexed "
                                "(`analyst_worker_0`, `analyst_worker_1`), so this separates two "
                                "workers a team filter would merge."
                            ),
                        },
                        "team": {
                            "type": "string",
                            "description": "Keep one team's events (the ledger's own grouping).",
                        },
                        "contains": {
                            "type": "string",
                            "description": (
                                "Keep events whose summary contains this text "
                                "(case-insensitive). Searches summaries only — to search inside "
                                "event payloads use ledger_grep."
                            ),
                        },
                    },
                },
            },
            {
                "name": "ledger_event",
                "description": (
                    "One event in full, including its data payload — the verdict text, the gate "
                    "reasons, the tool arguments. Windowed: a payload can be very large."
                ),
                "input_schema": {
                    "type": "object",
                    "description": (
                        "Get the sequence number from brief_history or ledger_grep first. The "
                        "reply names the payload's total size and the next offset, so a large "
                        "event can be walked window by window."
                    ),
                    "properties": {
                        "sequence": {
                            "type": "integer",
                            "description": "The event's sequence number (the #N in a history row).",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": (
                                f"Payload characters to return (default {_EVENT_WINDOW_CHARS}, "
                                f"max {_EVENT_MAX_WINDOW_CHARS})."
                            ),
                        },
                        "bias": {
                            "type": "integer",
                            "description": (
                                "Character offset into the payload (default 0). Negative reads "
                                "from the end — where a long tool dump keeps its errors and "
                                "results."
                            ),
                        },
                    },
                    "required": ["sequence"],
                },
            },
            {
                "name": "ledger_grep",
                "description": (
                    "Search inside event payloads and return matching events as index rows — the "
                    "only way to find an event by its content rather than its name."
                ),
                "input_schema": {
                    "type": "object",
                    "description": (
                        "Returns index rows, never raw payload text: open a hit with ledger_event "
                        "to read it. Use for names, ObsIDs, error strings, paths."
                    ),
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Case-insensitive substring to find in the payload.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": f"Maximum matches (default {_GREP_LIMIT}).",
                        },
                        "max_lvl": {
                            "type": "integer",
                            "description": "Search only events at this level or more important.",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        ]

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        events = _read_ledger(self.jsonl_path)
        if not events:
            return ToolOutcome(text="The run's event log is empty or unreadable.")
        origin = events[0].get("created_at")
        if name == "brief_history":
            return ToolOutcome(text=self._history(events, origin, arguments))
        if name == "ledger_event":
            return ToolOutcome(text=self._event(events, arguments))
        if name == "ledger_grep":
            return ToolOutcome(text=self._grep(events, origin, arguments))
        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)

    def _history(self, events: list[dict], origin: str | None, args: Mapping[str, Any]) -> str:
        rows = [e for e in events if _ledger_match(e, args)]
        if not rows:
            return "No events match those filters."
        limit = min(max(1, int(args.get("limit") or _HISTORY_LIMIT)), _HISTORY_MAX_LIMIT)
        head = str(args.get("order") or "tail") == "head"
        window = rows[:limit] if head else rows[-limit:]
        body = "\n".join(_history_row(e, origin) for e in window)
        note = (
            f"\n\n[{len(window)} of {len(rows)} matching events "
            f"({'oldest' if head else 'newest'} {len(window)}); {len(events)} in the log]"
        )
        return body + note

    def _event(self, events: list[dict], args: Mapping[str, Any]) -> str:
        try:
            wanted = int(args.get("sequence"))
        except (TypeError, ValueError):
            return "ledger_event needs a numeric `sequence` (the #N from a history row)."
        match = next((e for e in events if e.get("sequence") == wanted), None)
        if match is None:
            return f"No event #{wanted} in this log (it holds {len(events)} events)."
        # ensure_ascii=False: payloads carry σ, ±, degree signs and unit strings, and \u03c3 is
        # both unreadable to the reporter and more tokens than the character it escapes.
        payload = json.dumps(
            match.get("data") or {}, indent=2, sort_keys=True, default=str, ensure_ascii=False
        )
        window = min(
            max(1, int(args.get("max_chars") or _EVENT_WINDOW_CHARS)), _EVENT_MAX_WINDOW_CHARS
        )
        bias = int(args.get("bias") or 0)
        start = max(0, len(payload) + bias) if bias < 0 else min(bias, len(payload))
        chunk = payload[start : start + window]
        header = "\n".join(
            f"{key}: {match.get(key)}"
            for key in ("sequence", "created_at", "level", "team", "actor", "event", "summary",
                        "cost_usd_total")
        )
        footer = f"\n\n[data chars {start}–{start + len(chunk)} of {len(payload)}"
        footer += (
            f"; continue with bias={start + len(chunk)}]"
            if start + len(chunk) < len(payload)
            else "; end of payload]"
        )
        return f"{header}\n\ndata:\n{chunk}{footer}"

    def _grep(self, events: list[dict], origin: str | None, args: Mapping[str, Any]) -> str:
        pattern = str(args.get("pattern") or "").lower()
        if not pattern:
            return "ledger_grep needs a `pattern`."
        limit = min(max(1, int(args.get("limit") or _GREP_LIMIT)), _HISTORY_MAX_LIMIT)
        ceiling = args.get("max_lvl")
        hits = []
        for event in events:
            if ceiling is not None and int(event.get("level", 10)) > int(ceiling):
                continue
            blob = json.dumps(
                event.get("data") or {}, default=str, ensure_ascii=False
            ).lower()  # match on the real characters, not their \uXXXX escapes
            if pattern in blob:
                hits.append(event)
                if len(hits) >= limit:
                    break
        if not hits:
            return f"No event payload contains {pattern!r}."
        body = "\n".join(_history_row(e, origin) for e in hits)
        return f"{body}\n\n[{len(hits)} match(es); open one with ledger_event]"


def _ledger_match(event: dict, args: Mapping[str, Any]) -> bool:
    """Whether one event passes every supplied `brief_history` filter (unset filters pass)."""
    level = int(event.get("level", 10))
    sequence = int(event.get("sequence", 0))
    checks = (
        (args.get("min_lvl") is None or level >= int(args["min_lvl"])),
        (args.get("max_lvl") is None or level <= int(args["max_lvl"])),
        (args.get("min_seq") is None or sequence >= int(args["min_seq"])),
        (args.get("max_seq") is None or sequence <= int(args["max_seq"])),
        (args.get("actor") is None or event.get("actor") == args["actor"]),
        (args.get("team") is None or event.get("team") == args["team"]),
        (
            not args.get("event_prefix")
            or any(
                str(event.get("event", "")).startswith(p)
                for p in _as_list(args["event_prefix"])
            )
        ),
        (
            args.get("contains") is None
            or str(args["contains"]).lower() in str(event.get("summary", "")).lower()
        ),
    )
    return all(checks)


def _as_list(value: Any) -> list[str]:
    """Accept a single string or a list — models pass either for an array-typed argument."""
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value or []]


# ----------------------------------------------------------------------------
# Read-only inspection tools — reviewer / analyst-lead (in-process, no sandbox).
# ----------------------------------------------------------------------------


def _page_text_lines(
    text: str, offset: int = 1, limit: int = _DEFAULT_LINE_LIMIT, *, resume_hint: str = "read_file"
) -> dict:
    """Format ``text`` as a ``cat -n`` line window (1-indexed line numbers + tab).

    Returns a window of at most ``limit`` lines starting at ``offset`` (1-indexed, clamped), each
    line capped to ``_MAX_LINE_CHARS`` so one pathological line can't blow the budget. The
    ``content`` carries a footer when more lines remain, naming the next ``offset`` and the tool
    (``resume_hint``) to continue with — so truncation is always signalled, never silent (F8).
    Shared by ``read_file`` (file text) and ``web_fetch`` (extracted page markdown).
    """
    lines = text.splitlines()
    total = len(lines)
    limit = max(1, limit)
    start = 1 if total == 0 else min(max(1, offset), total)
    window = lines[start - 1 : start - 1 + limit]
    body = "\n".join(
        f"{start + i:>6}\t{line[:_MAX_LINE_CHARS]}" for i, line in enumerate(window)
    )
    end = start + len(window) - 1
    truncated = end < total
    if truncated:
        body += (
            f"\n\n[read lines {start}–{end} of {total}; "
            f"call {resume_hint} with offset={end + 1} to continue]"
        )
    return {
        "content": body,
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "truncated": truncated,
    }


def _read_lines(path: Path, offset: int = 1, limit: int = _DEFAULT_LINE_LIMIT) -> dict:
    """Read a file by line range (``cat -n`` style). Thin wrapper over :func:`_page_text_lines`;
    caller handles OSError."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return _page_text_lines(text, offset, limit, resume_hint="read_file")


class ReadOnlyToolProvider:
    """Lightweight in-process tools for the reviewer/lead to investigate before deciding.

    ``read_file`` / ``list_dir`` are confined to ``roots`` (the run artifacts dir and
    the papers dir) so the agent cannot read arbitrary paths; ``text_search`` queries
    the local knowledge base via the retrieval backend. No science sandbox is involved.
    """

    def __init__(
        self,
        *,
        roots: Sequence[Path],
        retrieval: Any | None = None,
        web_source: Any | None = None,
        include_text_search: bool = True,
        include_data_catalog: bool = False,
        include_plan_and_debate: bool = False,
        authoring_session: Any | None = None,
        report_workspace: ReportWorkspace | None = None,
        human_handler: Any | None = None,
        asked_by: str = "agent",
        model: str = "",
    ) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.retrieval = retrieval
        # V-4: the analyst lead's per-round report-authoring workspace (edit_report/review_report).
        # Set per adjudication by the lead (its per-worker figure catalog + subordinate reports);
        # None outside adjudication (so decide_authoring/drive_authoring never see the report tools).
        self.report_workspace = report_workspace
        # The analyst lead (only) drives tool authoring via a live AuthoringSession (§9.3
        # B-topology): drive_coder / submit_for_review. Masked to the lead alone — a session is
        # wired here only on the lead's provider; no other role sees the tools. None ⇒ disabled.
        self.authoring_session = authoring_session
        # The analyst lead (not the reviewer) gets the read-only data_catalog tool so it can
        # check local data availability before deciding to author an acquire tool (§9.10).
        self.include_data_catalog = include_data_catalog
        # The analyst lead (only) gets the budget-free plan_and_debate design channel (§5.5),
        # so it can settle a toolkit design with the human before authoring. Needs human_handler.
        self.include_plan_and_debate = include_plan_and_debate
        # When a human_handler is wired, expose the shared ask_human tool (§9.7).
        self.human_handler = human_handler
        self.asked_by = asked_by
        self.model = model
        # Optional Tavily/web QuerySource — when supplied (analyst lead), exposes a
        # web_search tool so the lead can consult authoritative documentation (mission
        # threads, calibration memos) before triaging a tool request or answering the
        # author, rather than guessing (CLAUDE.md §9.3 escalation ladder).
        self.web_source = web_source
        # The papers knowledge base is irrelevant to pure code review, so the §9.3
        # co-lead reviewer omits text_search (roots = the toolkit worktree instead).
        # text_search + open_paper are gated together as the knowledge-base toolset.
        self.include_text_search = include_text_search
        # Paths opened via open_paper this session — the reviewer's cited_paths are
        # intersected with this so a citation can only name a paper actually read.
        self.opened_paths: list[Path] = []

    def _roots_text(self) -> str:
        return ", ".join(str(r) for r in self.roots)

    def tool_definitions(self) -> list[dict]:
        roots_text = self._roots_text()
        definitions = [
            {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file by line range. Give an absolute path inside one of the "
                    f"permitted roots: {roots_text}. Returns lines numbered cat -n style; when the "
                    "file is longer than the window, the output ends with a note naming the offset "
                    "to continue from — call again with that offset to read on. Text only; read a "
                    "local PDF with open_paper instead."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the file, inside a permitted root.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "1-indexed line to start at (default 1).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"Maximum lines to return (default {_DEFAULT_LINE_LIMIT})."
                            ),
                        },
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": (
                    "List the entries of a directory. Give an absolute path inside one of the "
                    f"permitted roots: {roots_text}."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the directory, inside a permitted root.",
                        }
                    },
                    "required": ["path"],
                },
            },
        ]
        if self.include_text_search:
            definitions.append(
                {
                    "name": "text_search",
                    "description": (
                        "Full-text search the local knowledge base of ingested papers; returns "
                        "the file paths of the best matches."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Keywords or a phrase to match against ingested papers.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of paper paths to return (default 5).",
                            },
                        },
                        "required": ["query"],
                    },
                }
            )
            definitions.append(
                {
                    "name": "open_paper",
                    "description": (
                        "Open a local PDF (text + figures) to read it before citing. Pass a path "
                        "from text_search. The papers you open and actually use are exactly the "
                        "ones to list in the verdict's cited_paths; the report's References "
                        f"section links to those files. Open papers up to {_OPEN_PAPER_MAX_MB} MB "
                        "inline; for a larger paper (ultra-high-resolution figures inflate the "
                        "file), cite it from its text_search metadata instead."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to a local PDF returned by text_search.",
                            }
                        },
                        "required": ["path"],
                    },
                }
            )
        if self.web_source is not None:
            definitions.append(web_search_tool_def())
            definitions.append(web_fetch_tool_def())
        if self.include_data_catalog:
            catalog_def = data_catalog_tool_def()  # sourced from research_toolkit.catalog_utils
            if catalog_def is not None:
                definitions.append(catalog_def)
        if self.human_handler is not None:
            definitions.append(ask_human_tool_def())
            if self.include_plan_and_debate:
                definitions.append(plan_and_debate_tool_def())
        if self.authoring_session is not None:
            definitions.append(drive_coder_tool_def())
            definitions.append(submit_tool_def())
            definitions.append(respond_to_review_tool_def())
        if self.report_workspace is not None:
            definitions.append(edit_report_tool_def())
            definitions.append(review_report_tool_def())
        return definitions

    def _allowed(self, path: Path) -> bool:
        resolved = path.resolve()
        # §4: build-time governance files never enter runtime context, even inside a root
        # (research_toolkit carries its own CLAUDE.md/DOCS.md). Closes the reviewer/lead leak.
        if resolved.name in GOVERNANCE_BASENAMES:
            return False
        return any(resolved == root or root in resolved.parents for root in self.roots)

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == DATA_CATALOG_TOOL:
            return await execute_data_catalog(arguments)
        if name == ASK_HUMAN_TOOL:
            return await execute_ask_human(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        if name == PLAN_AND_DEBATE_TOOL:
            return await execute_plan_and_debate(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        if name == DRIVE_CODER_TOOL:
            if self.authoring_session is None:
                return ToolOutcome(text="authoring is not enabled for this role.", is_error=True)
            return await execute_drive_coder(self.authoring_session, arguments)
        if name == SUBMIT_TOOL:
            if self.authoring_session is None:
                return ToolOutcome(text="authoring is not enabled for this role.", is_error=True)
            return await execute_submit(self.authoring_session)
        if name == RESPOND_TO_REVIEW_TOOL:
            if self.authoring_session is None:
                return ToolOutcome(text="authoring is not enabled for this role.", is_error=True)
            return await execute_respond_to_review(self.authoring_session, arguments)
        if name == EDIT_REPORT_TOOL:
            return await execute_edit_report(self.report_workspace, arguments)
        if name == REVIEW_REPORT_TOOL:
            return await execute_review_report(self.report_workspace, arguments)
        if name in ("read_file", "list_dir"):
            target = Path(str(arguments.get("path", "")))
            if not self._allowed(target):
                return ToolOutcome(
                    text=(
                        f"path not permitted: {target}. Use an absolute path inside one of the "
                        f"permitted roots: {self._roots_text()}"
                    ),
                    is_error=True,
                )
            try:
                if name == "read_file":
                    page = _read_lines(
                        target,
                        offset=int(arguments.get("offset", 1)),
                        limit=int(arguments.get("limit", _DEFAULT_LINE_LIMIT)),
                    )
                    text = page["content"]
                else:
                    text = json.dumps(sorted(p.name for p in target.iterdir()))
            except OSError as exc:
                return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)
            return ToolOutcome(text=text or "(empty)")

        if name == "text_search":
            from auto_research.retrieval import text_search_paths

            try:
                paths = await text_search_paths(
                    str(arguments.get("query", "")),
                    limit=int(arguments.get("limit", 5)),
                    backend=self.retrieval,
                )
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                return ToolOutcome(
                    text=f"text_search failed: {type(exc).__name__}: {exc}", is_error=True
                )
            text = "\n".join(str(p) for p in paths) or "(no local matches)"
            return ToolOutcome(text=text)

        if name == "open_paper":
            target = Path(str(arguments.get("path", "")))
            resolved = target.resolve()
            if not self._allowed(target):
                return ToolOutcome(
                    text=(
                        f"path not permitted: {target}. Open a paper path inside one of the "
                        f"permitted roots: {self._roots_text()}"
                    ),
                    is_error=True,
                )
            if resolved not in self.opened_paths and len(self.opened_paths) >= _OPEN_PAPER_CAP:
                return ToolOutcome(
                    text=(
                        f"open_paper limit reached ({_OPEN_PAPER_CAP} papers). Decide and cite "
                        "from the papers already opened."
                    ),
                    is_error=True,
                )
            try:
                # Size guard first (stat only, before any read): a PDF is attached by value to
                # every later model call, so an oversized one can blow the provider's request cap
                # and hard-fail the call. Refuse it here with the size named, so the model cites it
                # from text_search metadata instead of attaching it.
                size_mb = target.stat().st_size / (1024 * 1024) if target.is_file() else 0.0
                if size_mb > _OPEN_PAPER_MAX_MB:
                    return ToolOutcome(
                        text=(
                            f"paper too large to open inline: {target} is {size_mb:.1f} MB, over the "
                            f"{_OPEN_PAPER_MAX_MB} MB limit (ultra-high-resolution figures inflate the "
                            f"file without adding readable text). Cite it from its text_search "
                            f"metadata, or open a smaller paper."
                        ),
                        is_error=True,
                    )
                # Same %PDF- signature check the downloader uses, so only real PDFs attach.
                if not (target.is_file() and b"%PDF-" in target.read_bytes()[:1024]):
                    return ToolOutcome(
                        text=f"not a readable PDF: {target}", is_error=True
                    )
                doc = document_from_file(target)
            except OSError as exc:
                return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)
            if resolved not in self.opened_paths:
                self.opened_paths.append(resolved)
            return ToolOutcome(
                text=f"opened '{doc.title}' ({target}); the PDF is attached above.",
                documents=[doc],
            )

        if name == WEB_SEARCH_TOOL:
            return await execute_web_search(self.web_source, arguments)
        if name == WEB_FETCH_TOOL:
            return await execute_web_fetch(self.web_source, arguments)

        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)


# ----------------------------------------------------------------------------
# Literature source tools — librarian I/O (in-process QuerySources).
# ----------------------------------------------------------------------------


class SourceToolProvider:
    """Literature-search tools for the librarian: external sources + local knowledge base.

    ``search_literature`` runs one configured ``QuerySource`` (arxiv/nasa_ads/web);
    ``text_search`` queries already-ingested papers. Accumulated results are exposed
    on ``results`` for downstream ingestion by the graph node.
    """

    def __init__(self, sources: Mapping[str, Any], *, retrieval: Any | None = None, max_chars: int = 6000) -> None:
        self.sources = dict(sources)
        self.retrieval = retrieval
        self.max_chars = max_chars
        self.results: list[Any] = []  # SearchResult accumulator

    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "search_literature",
                "description": (
                    "Search one external literature source for papers and add the hits to this "
                    "run's results. Sources: " + ", ".join(self.sources) + "."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": list(self.sources),
                            "description": "The literature source to query.",
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "A short keyword query of 4–8 terms; arXiv and ADS match on "
                                "keywords."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of papers to return (default 10).",
                        },
                    },
                    "required": ["source", "query"],
                },
            },
            {
                "name": "text_search",
                "description": (
                    "Search the local knowledge base of already-ingested papers; returns "
                    "matching paper paths."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords or a phrase to match against ingested papers.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of paper paths to return (default 5).",
                        },
                    },
                    "required": ["query"],
                },
            },
        ]

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == "search_literature":
            source = self.sources.get(str(arguments.get("source", "")))
            if source is None:
                return ToolOutcome(text=f"unknown source: {arguments.get('source')}", is_error=True)
            # A source provider raising (network, rate limit, ADS 404, malformed
            # response) must not kill the whole ReAct loop/run — surface it as an
            # error outcome so the model can retry, switch source, or stop. Mirrors
            # the per-source guard in LibrarianIOAgent.search().
            try:
                results = await source.search(
                    str(arguments.get("query", "")), limit=int(arguments.get("limit", 10))
                )
            except Exception as exc:  # noqa: BLE001 — degrade one source, keep the loop alive
                return ToolOutcome(
                    text=f"source '{arguments.get('source')}' failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
            self.results.extend(results)
            lines = [f"- {r.title} ({r.source}) {r.url}\n  {(r.abstract or '')[:300]}" for r in results]
            return ToolOutcome(text="\n".join(lines)[: self.max_chars] or "(no results)")

        if name == "text_search":
            from auto_research.retrieval import text_search_paths

            try:
                paths = await text_search_paths(
                    str(arguments.get("query", "")),
                    limit=int(arguments.get("limit", 5)),
                    backend=self.retrieval,
                )
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                return ToolOutcome(
                    text=f"local text_search failed: {type(exc).__name__}: {exc}", is_error=True
                )
            return ToolOutcome(text="\n".join(str(p) for p in paths) or "(no local matches)")

        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)
