"""Tool providers and outcome types for the agent ReAct loop.

`AgentRole._react` ([roles.py](roles.py)) drives a provider-agnostic inner loop:
it calls the model with `tool_definitions()`, executes each returned call via
`execute(...)`, and feeds the results back so the model chains dependent calls to
completion. A `ToolProvider` is the uniform interface behind which each role's tool
surface lives — the analyst's science sandbox here (`SandboxToolProvider`); the
reviewer/lead read-only tools and the librarian's sources are added alongside.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, get_args

from pydantic import ValidationError

from auto_research.provenance import check_report_fields
from auto_research.provenance.capture import inputs_before, receipt_call
from auto_research.env import coder_wait_limits_s, tool_search_config
from auto_research.observations import load_observations_config
from auto_research.sandbox.fs_guard import GOVERNANCE_BASENAMES, PathGuard
from auto_research.sandbox.runner import normalize_tool_raw, parse_tool_figures, tool_text


def _jail_supersedes_command_check() -> bool:
    """Whether an OS jail is actually running, making the shell-command path scan redundant.

    Both halves are required: configured **and** available. A host without ``bwrap``, or with user
    namespaces denied, keeps the old best-effort scan rather than losing confinement entirely --
    that host has no kernel enforcement to fall back on.
    """
    try:
        from auto_research.sandbox.jail import jail_available, jail_enabled

        return bool(jail_enabled()) and bool(jail_available()[0])
    except Exception:  # noqa: BLE001 — a guard that cannot decide keeps the stricter behaviour
        return False
from auto_research.schemas import (
    AuthoringDecision,
    MergeFlowResult,
    DocumentRef,
    FigureCitation,
    FigureRef,
    FiguredReport,
    ProposedCheck,
    ToolResult,
    ToolSpec,
    ValidationDataset,
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


#: A role-specific tool plugged into a provider without editing it: ``(definition, executor)``.
#: The reviewer's and the librarian's own tools arrive this way (board 2026-09-24).
ExtraTool = tuple[dict, Callable[[Mapping[str, Any]], Awaitable[ToolOutcome]]]


async def _run_extra_tool(
    extra_tools: Sequence[ExtraTool], name: str, arguments: Mapping[str, Any]
) -> ToolOutcome | None:
    for definition, executor in extra_tools:
        if definition.get("name") == name:
            return await executor(arguments)
    return None


def _receipted(
    receipts: Any | None,
    source: str | None,
    name: str,
    arguments: Mapping[str, Any],
    outcome: ToolOutcome,
    *,
    kind: str = "result",
    fields: Mapping[str, Any] | None = None,
) -> ToolOutcome:
    """Mint a receipt for an in-process tool's outcome and label the text the model sees with it.

    The blob is the outcome text as the tool produced it, before the label, so a quote the model
    copies from the text it read is checked against exactly that text.
    """
    if receipts is None or source is None:
        return outcome
    minted = receipt_call(
        receipts, source=source, produced_by=name, args=arguments, text=outcome.text,
        failed=outcome.is_error, kind=kind, fields=fields,
    )
    return replace(outcome, text=f"{minted.header()}\n{outcome.text}")


async def _web_receipted(
    receipts: Any | None, source: str | None, arguments: Mapping[str, Any], outcome: ToolOutcome
) -> ToolOutcome:
    """A fetched page becomes a ``web`` receipt minted the librarian's way, for every role."""
    if receipts is None or source is None or outcome.is_error:
        return outcome
    from auto_research.sources.receipts import mint_web_receipt

    receipt = await asyncio.to_thread(
        mint_web_receipt, receipts, str(arguments.get("url", "")), outcome.text, source=source
    )
    return replace(outcome, text=f"[receipt {receipt.handle}]\n{outcome.text}")


# ----------------------------------------------------------------------------
# The quick channel (E12) — one `message` tool on every graph seat, the human one address.
# ----------------------------------------------------------------------------

MESSAGE_TOOL = "message"
VIEW_RECEIPT_TOOL = "view_receipt"
WAIT_FOR_REPLY_TOOL = "wait_for_reply"
LIST_PEERS_TOOL = "list_peers"
#: The tools a seat's channel layer adds to its provider (see `agents.roles.ChannelTools`).
CHANNEL_TOOLS: frozenset[str] = frozenset(
    {MESSAGE_TOOL, VIEW_RECEIPT_TOOL, WAIT_FOR_REPLY_TOOL, LIST_PEERS_TOOL}
)
#: What each seat does, for list_peers.
_PEER_ROLES = {
    "reviewer": "reviews the research and assigns the team's work",
    "librarian_io": "searches and reads the literature",
    "analyst_lead": "judges the analysts' work and authors missing tools",
    "analyst_worker": "runs the analysis with the toolkit",
}
_MESSAGE_KINDS = ("clarify_science", "clarify_code", "blocked")
#: A text receipt shown by view_receipt past this many characters is cut, with a note.
_VIEW_TEXT_CAP = 20_000


def message_tool_def(directory: Sequence[str]) -> dict:
    """The provider-neutral definition of ``message``; ``directory`` is who this seat can reach."""
    reach = ", ".join(directory) or "nobody"
    human_note = (
        " The human reads it on a phone: lead with the one question, then the few facts needed "
        "to answer it."
        if "human" in directory
        else ""
    )
    coder_note = (
        " To 'coder' — the coding agent writing the tool you briefed — a text is its instruction: "
        "give concrete, algorithm-level steps (signature, the exact procedure, edge cases, the "
        "tests to add), not a restated goal. It keeps the working files across your texts, so "
        "iterate: inspect its diff with read_file, then text it again. An idle coder starts on "
        "your text at once; a working one reads it as its next turn, and an answer to a question "
        "it asked you reaches it inside the turn it is running. When the tool and its tests are "
        "right, call submit_for_review."
        if "coder" in directory
        else ""
    )
    properties: dict[str, Any] = {}
    if "coder" in directory:
        properties["mid_turn"] = {
            "type": "boolean",
            "description": (
                "Coder only: true delivers the text inside the turn the coder is running now, to "
                "change its course before it finishes — for a correction that cannot wait. "
                "Default false: the text waits for the coder's next turn."
            ),
        }
    return {
        "name": MESSAGE_TOOL,
        "description": (
            "Send a short text to another member of this team, or to the human, and keep "
            "working: it returns at once. Any reply arrives later as a message at your next step; "
            "call wait_for_reply when you need it before going on. A member that has finished its "
            "turn is resumed to read your text and can answer, though its filed report stays as "
            "filed. Texts are informal: rest every claim in your report on your own evidence. "
            "To show a receipt, name its handle in attachments — the human receives the file, and "
            f"a team member can open it with view_receipt.{human_note}{coder_note} "
            f"You can reach: {reach}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "enum": list(directory),
                    "description": "Who receives the text.",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "The text, in Markdown: the question or point first, then the facts it "
                        "rests on."
                    ),
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Receipt handles to show with the text, e.g. a figure you made.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(_MESSAGE_KINDS),
                    "description": (
                        "For the human: 'clarify_science' for an expert science point, "
                        "'clarify_code' for a code or convention point, 'blocked' for a hard block "
                        "such as missing data or a contradictory request."
                    ),
                },
                **properties,
            },
            "required": ["to", "text"],
        },
    }


def list_peers_tool_def() -> dict:
    """Definition of ``list_peers`` — who this seat can message, and what each is doing now."""
    return {
        "name": LIST_PEERS_TOOL,
        "description": (
            "List who you can message: each team member's address, role and model, and whether "
            "it is working now, has finished its turn (a text resumes it), or has not started; "
            "and the human, when one is attending. Free: calling it costs no tool budget."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }


def execute_list_peers(router: Any, sender: str | None, label: Callable[[Any], str]) -> ToolOutcome:
    """The directory with each peer's role, model and state, from the router's live view."""
    lines = []
    for address in router.directory(sender):
        if address == "human":
            lines.append("- human — the person running this study; replies arrive as messages.")
            continue
        seat = router.seat(address)
        role = getattr(seat, "role", "") or ""
        model = label(getattr(seat, "model", None)) or "unknown model"
        state = router.state(address)
        lines.append(
            f"- {address} — {_PEER_ROLES.get(role, role)} ({model}); {state}."
        )
    body = "\n".join(lines) or "Nobody else is on this run's channel."
    return ToolOutcome(text=f"You can message:\n{body}")


def view_receipt_tool_def() -> dict:
    """Definition of ``view_receipt`` — show one receipt's content by handle."""
    return {
        "name": VIEW_RECEIPT_TOOL,
        "description": (
            "Show one receipt by its handle: a figure as an image, a text receipt (a tool output, "
            "a page) as its text. Works for your own receipts, the ones you may cite, and any a "
            "message you received named. Viewing shows you the content; your report still cites "
            "only your own evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "The receipt handle, e.g. 'analyst_worker_0:f3'.",
                },
            },
            "required": ["handle"],
        },
    }


async def execute_view_receipt(
    lookup: Any | None,
    viewable: Iterable[str],
    store: Any | None,
    arguments: Mapping[str, Any],
    *,
    images_ok: bool = True,
) -> ToolOutcome:
    """Show a receipt this seat may see: in its citable scope, or named by a message it received.

    Mints nothing and admits nothing — a peer's figure seen here stays the peer's evidence.
    """
    handle = str(arguments.get("handle") or "").strip()
    visible = bool(handle) and (
        (lookup is not None and lookup.visible(handle)) or handle in set(viewable)
    )
    store = store if store is not None else getattr(lookup, "store", None)
    if not visible or store is None:
        return ToolOutcome(
            text=f"{handle or '(no handle)'} is not a receipt you can view: view your own receipts, "
            "the ones you may cite, and any a message you received named.",
            is_error=True,
        )
    receipt = store.get(handle)
    problem = store.verify(handle) if receipt is not None else f"{handle} is not in this run."
    if receipt is None or problem:
        return ToolOutcome(text=problem or f"{handle} is not in this run.", is_error=True)
    if receipt.kind == "figure":
        caption = receipt.caption or ""
        if not images_ok:
            return ToolOutcome(
                text=f"{handle} is a figure ({caption}); this seat reads text, so it is named "
                "rather than shown."
            )
        return ToolOutcome(text=f"{handle}: {caption}", figures=[receipt.to_figure_ref()])
    if receipt.kind == "paper":
        return ToolOutcome(
            text=f"{handle} is a paper ({receipt.title or 'untitled'}); open it with the paper "
            "reader where you have one, or ask its owner to quote what you need."
        )
    if receipt.kind in ("data", "commit"):
        return ToolOutcome(
            text=f"{handle} is a {receipt.kind} receipt produced by {receipt.produced_by}; its "
            "bytes are not text to show here."
        )
    text = store.text(handle) or ""
    note = ""
    if len(text) > _VIEW_TEXT_CAP:
        text, note = text[:_VIEW_TEXT_CAP], f"\n\n[… cut at {_VIEW_TEXT_CAP} characters]"
    return ToolOutcome(text=f"{handle} ({receipt.produced_by}):\n{text}{note}")


async def execute_wait_for_reply(
    run_control: Any,
    agent_key: str | None,
    arguments: Mapping[str, Any],
    *,
    outstanding: Callable[[], list[str]],
) -> ToolOutcome:
    """Park until a message reaches this seat, or ``timeout_s`` passes (a seat with no coder).

    Returns at once when a message is already waiting, and when nothing is outstanding — no open
    question to the human, no resumed seat still in its turn — so a wait can never be the idle
    loop ``check_coder`` once was (A22).
    """
    timeout, note = _wait_timeout(arguments)
    if run_control is None or not agent_key:
        return ToolOutcome(text="Nothing can reach this seat: it has no inbox.", is_error=True)
    started = time.monotonic()
    event = run_control.steer_event(agent_key)
    pending = outstanding()
    if not run_control.has_inbox(agent_key) and pending:
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            pass
    waited = time.monotonic() - started
    header = f"[waited {_fmt_s(waited)} of {_fmt_s(timeout)}{note}]"
    if run_control.has_inbox(agent_key):
        body = "A message for you has arrived; it is delivered right after this result."
    elif not pending:
        body = (
            "Nothing is outstanding: no question to the human is open and no member you resumed "
            "is still working, so there is nothing to wait for. Carry on with your turn."
        )
    else:
        body = (
            f"No reply yet from {', '.join(pending)}. Carry on with other work, or call "
            "wait_for_reply again — a longer wait costs no more than a short one."
        )
    return ToolOutcome(text=f"{header}\n\n{body}")


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


#: Which report fields each role's ``edit_report`` declares (A3).
#:
#: One report class serves both roles, and the SURFACE is what keeps them apart: a worker is never
#: offered the lead's decision fields, and the lead is never offered ``tool_request`` (it authors
#: instead of asking). The alternative — a subclass per role — would have widened ``status`` in a
#: subtype, so anything typed as the worker's report could receive a value the worker may not file.
#:
#: Every name here must be a real field on the report model, and every field's prose is read from
#: that model's own ``Field(description=...)``; ``test_report_authoring.py`` pins both, so the two
#: roles' view of one object cannot drift the way it did before the forms carried descriptions.
WORKER_REPORT_FIELDS: tuple[str, ...] = (
    "summary", "findings", "uncertainty", "status", "tool_request",
)
LEAD_REPORT_FIELDS: tuple[str, ...] = (
    "summary", "findings", "uncertainty", "status", "rework", "rationale", "what_to_probe",
)

#: The ``status`` values each surface offers. ``needs_human`` is a cycle decision the lead may
#: file and a worker may not (F9): a worker that needs a person says so in ``summary``.
WORKER_STATUS_VALUES: tuple[str, ...] = ("succeeded", "failed")
LEAD_STATUS_VALUES: tuple[str, ...] = ("succeeded", "failed", "needs_human")


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
    # Fields carried through edit_report from the tool args onto the report model, and the
    # `status` values this role may file: together they ARE the role's report surface (A3).
    # `edit_report_tool_def` is handed the same pair, so what the model is offered and what the
    # handler copies cannot drift. cited_figures/figures are handled separately (FiguredReport).
    prose_fields: tuple[str, ...] = WORKER_REPORT_FIELDS
    status_values: tuple[str, ...] = WORKER_STATUS_VALUES
    # Provenance (P2): the receipts this role may cite (a `ReceiptLookup`, usually a
    # `ScopedLookup` over the run's store) and the source `self:` stands for in its prose. With
    # `receipts` unset the report's numbers are not checked — only a caller with no run (Studio).
    receipts: Any | None = None
    receipt_source: str | None = None
    # Fields whose items are grounded only by what each item cites itself (a finding).
    itemised: tuple[str, ...] = ("findings",)
    # A role whose prose is nested (the reviewer's sections and table rows) supplies its own
    # check, ``report -> problems``; it then replaces the default field walk, both in edit_report's
    # notes and at turn_end, so the report is checked once.
    provenance_check: Callable[[Any], list[str]] | None = None
    # How review_report shows this role's own draft, when the default (summary / findings /
    # numbered figures) does not fit its report — the reviewer shows its rendered pages.
    draft_view: Callable[[Any], tuple[str, list[FigureRef]]] | None = None
    # edit_report's description, when the analysts' wording (cited_figures, [source:index])
    # does not describe this role's report.
    edit_description: str | None = None


#: The report fields the provenance check reads: prose a claim can be made in. Decision and
#: request fields (`status`, `rework`, `tool_request`, `what_to_probe`) are instructions, not claims.
_CLAIM_FIELDS: tuple[str, ...] = ("summary", "findings", "uncertainty", "rationale")


def report_provenance_problems(workspace: "ReportWorkspace | None", report: Any) -> list[str]:
    """The numbers in ``report``'s prose that its cited receipts do not carry, plus stale files.

    The shared half of every unit's ``turn_end``: each cited receipt resolves, is re-hashed, and
    every number in a finding appears in a receipt that finding cites (scoped grounding, human
    ruling 2026-09-24). A cited figure whose file is gone is refused here too, for every role.
    """
    if workspace is None or report is None:
        return []
    problems: list[str] = []
    for index, figure in enumerate(getattr(report, "figures", None) or [], 1):
        if not hasattr(figure, "path"):
            continue  # a figure *handle* (the reviewer's); its receipt is re-hashed by its own check
        path = Path(str(figure.path))
        if not path.is_file():
            problems.append(
                f"Figure {index} ({getattr(figure, 'caption', '')}) points at {path}, which is not a file "
                "on disk. Re-cite it from your catalog with review_report, or drop it."
            )
    custom = getattr(workspace, "provenance_check", None)
    if custom is not None:
        return problems + list(custom(report))
    receipts = getattr(workspace, "receipts", None)
    if receipts is None:
        return problems
    prose_fields = getattr(workspace, "prose_fields", WORKER_REPORT_FIELDS)
    fields = {
        name: getattr(report, name, None)
        for name in _CLAIM_FIELDS
        if name in prose_fields or name in ("summary", "findings")
    }
    result = check_report_fields(
        fields, receipts, own_source=getattr(workspace, "receipt_source", None),
        itemised=getattr(workspace, "itemised", ("findings",)),
    )
    return problems + result.problems


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
    if status and status != "succeeded":
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


FILE_EDIT_TOOL = "file_edit"


def file_edit_tool_def(path: Path) -> dict:
    """Definition for ``file_edit`` — reactive editing of **one** file (F5e).

    The path is baked into the description and the provider holds it, so the model never names a
    file: the tool cannot reach anything else, which is what makes giving a formerly read-only
    role a write capability bounded rather than open (F10 in miniature — a role writes to its own
    work product and nowhere else).
    """
    return {
        "name": FILE_EDIT_TOOL,
        "description": (
            f"Edit your report at {path.name}. Two modes: pass `content` to write the whole file, "
            "or pass `old_text` and `new_text` to replace one exact, unique passage in place. "
            "Prefer the targeted replacement once the document exists — it keeps every earlier "
            "fix, where rewriting the whole file can silently undo one. Read the file back with "
            "read_file to see your current draft. Free: editing costs no tool budget."
        ),
        "strict": True,
        "input_schema": _strictify({
            "type": "object",
            "properties": {
                "content": {
                    "type": ["string", "null"],
                    "description": "The complete new file content. Use for the first draft.",
                },
                "old_text": {
                    "type": ["string", "null"],
                    "description": (
                        "The exact passage to replace, copied from the current file. Must appear "
                        "exactly once."
                    ),
                },
                "new_text": {
                    "type": ["string", "null"],
                    "description": "What replaces `old_text`. Empty string deletes the passage.",
                },
            },
            "required": ["content", "old_text", "new_text"],
        }),
    }


async def execute_file_edit(
    path: Path | None, arguments: Mapping[str, Any], guard: Any | None = None
) -> ToolOutcome:
    """Write or patch the one file this role owns (F5e), inside its writable scope (F10).

    Two independent bounds, because either alone is weaker than it looks. The **tool** never
    takes a path from the model, so there is nothing for a model to redirect; the **guard** then
    checks the path the provider was configured with, so a mis-wired provider cannot hand a role
    a write outside its own work folder either. The second is the one that survives a future
    caller who does pass a path.
    """
    if path is None:
        return ToolOutcome(text="file_edit is not available for this role.", is_error=True)
    if guard is not None and getattr(guard, "active", False) and not guard.is_writable(path):
        return ToolOutcome(
            text=(f"file_edit: {path} is outside this role's writable scope — a role writes only "
                  "within its own work folder."),
            is_error=True,
        )
    content = arguments.get("content")
    old_text = arguments.get("old_text")
    new_text = arguments.get("new_text")

    if content is not None and str(content).strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        return ToolOutcome(text=f"Wrote {len(str(content))} characters to {path.name}.")

    if old_text is None or not str(old_text):
        return ToolOutcome(
            text="file_edit: pass either `content` (whole file) or `old_text` + `new_text`.",
            is_error=True,
        )
    if not path.is_file():
        return ToolOutcome(
            text=f"file_edit: {path.name} does not exist yet — write it with `content` first.",
            is_error=True,
        )
    current = path.read_text(encoding="utf-8")
    hits = current.count(str(old_text))
    if hits == 0:
        return ToolOutcome(
            text=("file_edit: `old_text` does not appear in the file. Read it back with "
                  "read_file and copy the passage exactly."),
            is_error=True,
        )
    if hits > 1:
        return ToolOutcome(
            text=(f"file_edit: `old_text` appears {hits} times — include enough surrounding "
                  "text to make it unique."),
            is_error=True,
        )
    path.write_text(current.replace(str(old_text), str(new_text or "")), encoding="utf-8")
    return ToolOutcome(text=f"Replaced one passage in {path.name}.")


TURN_END_TOOL = "turn_end"


def turn_end_tool_def() -> dict:
    """Definition for ``turn_end`` — the universal terminal, validated before it is accepted (F5c).

    Every graph role that produces terminal material ends its turn by calling this. The harness
    then checks the material and either accepts (the loop ends) or **rejects with the reason**,
    which puts the model back in the loop with its remaining tools to fix what was named.

    Why a tool and not the absence of tool calls: a tool call means the same thing on all six
    vendors, and a finish-reason field does not — Z.AI reports a dropped inference as
    `network_error`, which the ecosystem maps to `stop`. It is also the design a research harness
    already ships (Inspect AI's `react()` runs "until the model calls a special `submit()` tool",
    injects a continuation when the model stops calling tools, and validates the submission).
    """
    return {
        "name": TURN_END_TOOL,
        "description": (
            "End your turn: you have finished the work and your material is complete. The harness "
            "checks it before accepting — if anything required is missing or malformed you get the "
            "specific reason back and stay in the loop with your tools, so fix what is named and "
            "call this again. Write your material with the tools first; this call submits it, it "
            "does not carry it. Free: calling it costs no tool budget."
        ),
        "strict": True,
        "input_schema": _strictify({
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "One or two sentences on what you concluded and what a reader should do "
                        "with it. Recorded on the run ledger beside the accepted material."
                    ),
                },
            },
            "required": ["summary"],
        }),
    }


async def execute_turn_end(
    validator: Callable[[], list[str]] | None, arguments: Mapping[str, Any]
) -> ToolOutcome:
    """Accept the turn, or reject it naming exactly what is missing (F5c).

    ``validator`` returns the problems with the role's current material; empty means acceptable.
    A rejection is an ``is_error`` outcome, which ``_react`` treats as a correction rather than a
    finish, so the model keeps its tools and its context.

    **Free by ruling** (2026-09-09): the *editing* burns tool budget, so the loop is bounded by
    the work rather than by the validation. Residual, recorded rather than guarded: a model that
    calls this repeatedly without editing anything spins for free, bounded only by
    ``react_loop_absolute_cap``. The rejection always names a reason, which is what makes that
    unlikely for a cooperating model.
    """
    problems = list(validator() if validator is not None else [])
    if problems:
        return ToolOutcome(
            text=(
                "Your turn cannot be ended yet:\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\nFix these with your tools, then call turn_end again."
            ),
            is_error=True,
        )
    summary = str(arguments.get("summary") or "").strip()
    return ToolOutcome(text=f"Turn ended. {summary}".strip())


def worker_report_problems(workspace: ReportWorkspace | None) -> list[str]:
    """What is wrong with an analyst worker's material, for :func:`execute_turn_end`.

    The worker's report is whatever ``edit_report`` persisted. Before F5, a loop that ended
    without ever calling it got a **structured compose on the model's behalf** — the only path
    that sent the model the runtime-owned ``figures``/``FigureRef`` tree, which
    ``_sanitize_strict`` then promoted to *required*, which is how four invented
    ``placeholder_*`` digests killed slot 5 one line before they would have been discarded.
    There is no compose now: an unwritten report is a rejection with a reason.
    """
    if workspace is None:
        return []
    report = workspace.written_report
    if report is None:
        return [
            "No report has been written. Call edit_report to write it — that call is the only "
            "way your report is saved, and nothing is composed on your behalf."
        ]
    problems: list[str] = []
    if not str(getattr(report, "summary", "") or "").strip():
        problems.append("The report's summary is empty.")
    if getattr(report, "status", None) is None:
        problems.append("The report declares no status; state whether the work succeeded.")
    uncited = sum(len(figs) for figs in (workspace.catalog or {}).values())
    if uncited and not getattr(report, "figures", None):
        problems.append(
            f"{uncited} figure(s) are in your catalog and none is cited. Cite the ones your "
            "prose refers to with edit_report's cited_figures, or say why none apply."
        )
    return problems + report_provenance_problems(workspace, report)


def _written_report(workspace: "ReportWorkspace | None") -> Any | None:
    """The report ``edit_report`` persisted for this round, or ``None`` if none was written.

    The single reader of a workspace's result, so "did this role produce a report" has one
    answer everywhere. ``None`` is the honest failure the caller acts on: nothing is composed
    on a model's behalf ([F6]).
    """
    return getattr(workspace, "written_report", None) if workspace is not None else None


def lead_turn_problems(cycle: "LeadCycle | None", workspace: "ReportWorkspace | None") -> list[str]:
    """What is wrong with the analyst lead's turn, for :func:`execute_turn_end` (A6).

    The lead's report **is** its decision, so completeness is checked here rather than by a schema:
    ``edit_report`` stays permissive so a partial write mid-turn is legal, and this is the door.
    Each problem is returned as a sentence the model can act on, because a rejection puts it back
    in the loop with its tools rather than ending anything.

    What it refuses, and why each one is here:

    * **No report.** Nothing is composed on the lead's behalf. The one path that ever did sent the
      model the runtime-owned ``figures`` tree, which strict mode promoted to *required*, which is
      how four invented digests killed a slot one line before they would have been discarded.
    * **``rework`` with no probe**, or with no rounds left. The probe reaches every worker verbatim
      as their next instruction, so "go again" with nothing to go on is not a decision; and the cap
      is Python's, named here rather than discovered by the model.
    * **A conclusion with no findings.** ``rework=False`` means this report IS the analysis result.
    * **A cited figure whose file is gone, or a number no cited receipt carries.** Shared with
      every unit through :func:`report_provenance_problems`: the path is stat'ed, each cited
      receipt is re-hashed, and each number must appear in a receipt its text cites.
    * **A coding session still open.** Walking away from a running coder leaves the tree mid-edit
      and the outcome unrecorded; finishing or abandoning it is a decision the lead has to make.
    """
    problems: list[str] = []
    report = getattr(workspace, "written_report", None) if workspace is not None else None
    if workspace is not None and report is None:
        return [
            "No report has been written. Call edit_report to write it — that call is the only way "
            "your report is saved, and nothing is composed on your behalf."
        ]
    if report is not None:
        if getattr(report, "rework", False):
            if not str(getattr(report, "what_to_probe", "") or "").strip():
                problems.append(
                    "Your report asks for another round but what_to_probe is empty. That text is "
                    "handed to every worker verbatim as their next instruction, so it has to say "
                    "what to do."
                )
            if not str(getattr(report, "rationale", "") or "").strip():
                problems.append(
                    "Your report asks for another round but rationale is empty. Say what evidence "
                    "gap or disagreement one more pass would close."
                )
            if cycle is not None and cycle.rounds_left <= 0:
                problems.append(
                    "No debate rounds remain, so another round cannot be granted. Conclude on the "
                    "evidence you have (rework=false) — an honest 'this is not settled' is a "
                    "correct outcome."
                )
        else:
            if not str(getattr(report, "summary", "") or "").strip():
                problems.append("The report's summary is empty.")
            if not (getattr(report, "findings", None) or []):
                problems.append(
                    "Your report concludes the analysis but lists no findings. State what the "
                    "work established, or set rework=true if it did not establish anything yet."
                )
        problems.extend(report_provenance_problems(workspace, report))
    if cycle is not None and cycle.session_open:
        problems.append(
            "A coding session is still open. If the coder is still working, call wait_for_reply (a "
            "blocking wait that returns when the coder stops); then finish the session with "
            "submit_for_review, or let its budget run out, before ending your turn."
        )
    return problems


def _model_field_schema(model: type, name: str) -> dict:
    """``model``'s field ``name`` as a tool property: its own JSON Schema, references inlined.

    How a role whose report is not an ``AnalystReport`` (the reviewer's, the librarian's) gets an
    ``edit_report`` without forking it: the property and its description come from the model's
    ``Field(description=…)``, the same single source the lead forms read.
    """
    full = model.model_json_schema()
    defs = full.get("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {k: inline(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(full["properties"][name])


def edit_report_tool_def(
    prose_fields: tuple[str, ...] = WORKER_REPORT_FIELDS,
    status_values: tuple[str, ...] = WORKER_STATUS_VALUES,
    report_model: type | None = None,
    description: str | None = None,
) -> dict:
    """Definition for ``edit_report`` — the disciplined write path for an ``AnalystReport``.

    The agent supplies the whole report each call (a call overwrites the last), citing figures by
    their ``[source:index]`` catalog handle in ``cited_figures``; the runtime resolves and places
    exactly those, in order, and the agent refers to the k-th as "Figure k" in its prose.

    ``prose_fields`` selects which of the report's fields this role may write, and must match the
    workspace's own ``prose_fields`` — the handler copies exactly those from the arguments, so a
    property declared here and absent there is a field the model can fill and the runtime drops.
    ``cited_figures`` is always declared: it is how figures are placed, for every role.
    """
    declared = {
        "name": EDIT_REPORT_TOOL,
        "description": (
            "Write (or rewrite) your report — this is the ONLY way it is saved; you never write "
            "files yourself. Provide the full report each call (a call overwrites the last), so "
            "refine across calls. Cite figures in 'cited_figures' by the [source:index] handle "
            "from review_report, IN THE ORDER you want them shown — the runtime places exactly "
            "those and you refer to the k-th as 'Figure k' in your prose; a figure you omit is "
            "dropped. After writing, call review_report to see the placement and refine until "
            "every figure is in the right place. Every number in your prose comes from a receipt: "
            "each tool result is labelled [receipt <handle>], and a finding cites the receipts its "
            "numbers come from as [handle] (or pins one exact span as {{handle \"copied text\"}}). "
            "Copy numbers exactly as the receipt prints them, sign and digits included; compute "
            "any derived value with a tool so the calculation has its own receipt. turn_end "
            "refuses a number that no receipt the text cites carries, naming it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "The report summary (prose). Refer to figures as 'Figure 1', 'Figure 2', "
                        "… matching the order of cited_figures. Its numbers are grounded by every "
                        "receipt the report cites."
                    ),
                },
                "findings": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Key findings, each a self-contained prose string that cites the receipts "
                        "its numbers come from as [handle]."
                    ),
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
                    "enum": ["succeeded", "failed"],
                    "description": (
                        "Declare the outcome of YOUR work: 'succeeded' when you completed it, "
                        "'failed' when you could not. An honest 'failed' is a correct, "
                        "penalty-free answer — never fabricate a result to force 'succeeded'. Say "
                        "in the summary what blocked you and what a person would need to do."
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
                # `status` is REQUIRED, not defaulted (F9). The schema default is `failed`, on the
                # principle that silence is never success — but a default only makes sense for a
                # report the RUNTIME constructs. For a report the agent writes, leaving it
                # optional would mark a genuinely successful analysis as failed for a missing
                # field, the opposite of "the agent declares its own outcome". Caught by
                # `test_monitor.py::test_run_research_returns_monitor_with_fake_dependencies`,
                # whose fake omitted it and turned an accepted run into an honest failure.
                #
                # The three below are the LEAD's surface only (A3); a worker's call never
                # declares them, so `WORKER_REPORT_FIELDS` filters them straight back out.
                "rework": {
                    "type": "boolean",
                    "description": (
                        "Set true to send the analysts back for one more round instead of "
                        "concluding. When true, say why in rationale and what to look at in "
                        "what_to_probe; when false, this report is the analysis result and needs "
                        "summary, findings and status."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "Why another round is warranted — the evidence gap or disagreement that "
                        "one more pass would close. Required when rework is true."
                    ),
                },
                "what_to_probe": {
                    "type": "string",
                    "description": (
                        "What the analysts should do next, in their own terms. This text reaches "
                        "every worker verbatim as their next instruction. Required when rework "
                        "is true."
                    ),
                },
            },
            # `summary` and `status` are always required: a report with neither is not a report,
            # and leaving `status` optional marked a genuinely successful analysis as failed for a
            # missing field. The rework-conditional requirements are deliberately NOT expressed
            # here — the schema stays permissive so a partial write mid-turn is legal, and
            # completeness is `turn_end`'s job (A3/A6). Conditional required-fields is also not
            # portable across the six vendors.
            "required": ["summary", "status"],
        },
    }
    if description is not None:
        declared["description"] = description
    if report_model is not None and not _is_figured(report_model):
        # A report that is not the analysts': every property, and which are required, come from
        # the model itself; there is no figure placement to declare.
        declared["input_schema"]["properties"] = {
            name: _model_field_schema(report_model, name) for name in prose_fields
        }
        if "status" in prose_fields:
            declared["input_schema"]["properties"]["status"]["enum"] = list(status_values)
        declared["input_schema"]["required"] = [
            name for name in prose_fields if report_model.model_fields[name].is_required()
        ]
        return declared
    properties = declared["input_schema"]["properties"]
    keep = set(prose_fields) | {"cited_figures"}
    if report_model is not None:
        for name in prose_fields:
            if name not in properties and name in report_model.model_fields:
                properties[name] = _model_field_schema(report_model, name)
    unknown = keep - set(properties) - {"cited_figures"}
    if unknown:  # pragma: no cover — a typo in a surface constant, caught at import by the tests
        raise ValueError(f"edit_report has no property for {sorted(unknown)}")
    declared["input_schema"]["properties"] = {
        name: spec for name, spec in properties.items() if name in keep
    }
    if "status" in properties and "status" in keep:
        declared["input_schema"]["properties"]["status"] = {
            **properties["status"], "enum": list(status_values),
        }
    declared["input_schema"]["required"] = [
        name for name in ("summary", "status") if name in keep
    ]
    return declared


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


def _is_figured(model: type) -> bool:
    """Whether a report model places figures by ``cited_figures`` (the analysts' reports)."""
    return isinstance(model, type) and issubclass(model, FiguredReport)


async def _edit_plain_report(workspace: ReportWorkspace, arguments: Mapping[str, Any]) -> ToolOutcome:
    """``edit_report`` for a report that is not a ``FiguredReport`` (the reviewer's).

    Exactly the surface fields are copied onto the model; figures, when the model has them, are
    whatever it declares (for the reviewer, figure receipt handles checked by its own validator).
    """
    fields = {
        key: arguments[key] for key in workspace.prose_fields
        if key in arguments and arguments[key] is not None
    }
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
    lines = ["Report saved."]
    provenance = report_provenance_problems(workspace, report)
    if provenance:
        lines.append("Provenance — turn_end will refuse until these are fixed:")
        lines.extend(f"  - {problem}" for problem in provenance)
    return ToolOutcome(text="\n".join(lines))


async def execute_edit_report(
    workspace: ReportWorkspace | None, arguments: Mapping[str, Any]
) -> ToolOutcome:
    """Validate the report args, resolve figure citations, persist the per-round JSONL (V-4).

    The single disciplined write path: the agent never touches disk. A shape/validation error
    degrades to an error outcome so the model self-corrects in the loop.
    """
    if workspace is None:
        return ToolOutcome(text="edit_report is not available for this role.", is_error=True)
    if not _is_figured(workspace.report_model):
        return await _edit_plain_report(workspace, arguments)
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
    # The same check turn_end runs, reported here as a note while the draft is still being
    # written, so the fix happens now rather than as a refusal at the end.
    provenance = report_provenance_problems(workspace, report)
    if provenance:
        lines.append("Provenance — turn_end will refuse until these are fixed:")
        lines.extend(f"  - {problem}" for problem in provenance)
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
    if draft is not None and workspace.draft_view is not None:
        draft_text, draft_images = workspace.draft_view(draft)
        text = "Your current report draft:\n\n" + draft_text
        receipts_text = _receipt_menu(workspace.receipts)
        if receipts_text:
            text += "\n\n--- receipts your prose can cite as [handle] ---\n" + receipts_text
        return ToolOutcome(text=text, figures=draft_images)
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
    receipts_text = _receipt_menu(workspace.receipts)
    if receipts_text:
        text += (
            "\n\n--- receipts your prose can cite as [handle] (numbers must come from these) ---\n"
            + receipts_text
        )
    return ToolOutcome(text=text, figures=menu_images)


_RECEIPT_MENU_CAP = 60


def _receipt_menu(lookup: Any | None) -> str:
    """One line per citable receipt: handle, what produced it, and the start of its text."""
    handles = getattr(lookup, "handles", None)
    if not callable(handles):
        return ""
    listed = handles()
    lines: list[str] = []
    for handle in listed[-_RECEIPT_MENU_CAP:]:
        receipt = lookup.get(handle)
        if receipt is None:
            continue
        marks = f" [{', '.join(receipt.flags)}]" if receipt.flags else ""
        if receipt.kind == "figure":
            gist = receipt.caption or ""
        elif receipt.kind == "data":
            gist = str(receipt.path)
        else:
            gist = " ".join((lookup.text(handle) or "").split())[:100]
        lines.append(f"  [{handle}] {receipt.kind} from {receipt.produced_by}{marks}: {gist}")
    if len(listed) > _RECEIPT_MENU_CAP:
        lines.insert(0, f"  (showing the latest {_RECEIPT_MENU_CAP} of {len(listed)})")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Lead-only tool-authoring tools (§9.3, B-topology). The analyst lead drives a live
# AuthoringSession: it steers the coder (message to the coder, E12), submits to the deterministic
# gate + provider-decorrelated co-lead review (submit_for_review), and — slice 4 — debates a review challenge
# (respond_to_review). Masked to the lead alone (a session is wired only on its provider).
# ----------------------------------------------------------------------------

SUBMIT_TOOL = "submit_for_review"
RESPOND_TO_REVIEW_TOOL = "respond_to_review"


def submit_tool_def() -> dict:
    """Lead-only: run the gate + co-lead review over the coder's current work; commit on
    gate-pass ∧ review-pass (the lead never self-approves)."""
    return {
        "name": SUBMIT_TOOL,
        "description": (
            "Submit the coder's current work to the deterministic gate and the independent "
            "co-lead review. The gate is mechanical (determinism, no-fabrication, structure, the "
            "tool's own tests) — you can only satisfy it, by fixing the code through message to the coder. The "
            "review judges whether the plan and its tests are right and strict enough. On gate-pass "
            "AND review-pass the tool is committed automatically — you never self-approve. If the "
            "gate fails, fix and submit again; if the review rejects, either message the coder to address "
            "it or respond_to_review to debate it. This is the ONLY way to finish: your authored "
            "tool is committed solely through this call — if your turn ends without calling it (or "
            "respond_to_review), the work is discarded as a failure. When the tool and its tests are "
            "right, call it; if after your best effort it genuinely cannot be made mergeable, say so "
            "explicitly in one line rather than stopping silently. While the coder is still "
            "working, this first waits for it to stop, then runs the gate. Takes no arguments."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }


def _fmt_s(seconds: float | None) -> str:
    """``95`` → ``1m35s``; the unit a lead reads to pace itself."""
    if seconds is None:
        return "?"
    seconds = int(round(seconds))
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


async def execute_coder_message(
    session: Any, text: str, *, mid_turn: bool = False
) -> ToolOutcome:
    """Deliver the lead's ``message(to="coder")`` and **return at once** — the one lead->coder
    path (B3; ``drive_coder`` until E12 folded it into ``message``).

    The coder works beside the lead rather than under it. A real link's turn takes minutes, and
    the lead used to spend all of them blocked inside this call — unable to read the diff it had
    just asked for, or to think about the next link. It now gets the text acknowledged and keeps
    its turn; `wait_for_reply` parks until the coder has something to say, and
    `submit_for_review` stays **blocking** by ruling (the gate runs pytest twice through a
    blocking `subprocess.run`, and the CI framing says you wait for the ticket).

    **A working coder is reached too**, and the session picks the primitive. By default the text is
    **queued** for the coder's next turn; ``mid_turn`` steers it into the running one (Codex
    `turn_steer`, or a second `query()` on the Claude SDK's live client), and so does any text sent
    while the coder has an open question, because that text is most likely the answer. Queued is
    the default because slot 6 (2026-09-15) steered its coder 47 times mid-turn: steering had
    become the lead's way of staying busy while it had no way to wait.

    A session that predates B — a test double's — has no `send`, so this falls back to the
    blocking call.
    """
    instructions = (text or "").strip()
    if not instructions:
        return ToolOutcome(
            text="message to the coder needs text: what the coder should do next.", is_error=True
        )
    if not hasattr(session, "send"):
        response = await session.drive(instructions)
        return ToolOutcome(
            text=(response or "").strip()
            or "(coder produced no narration; inspect the worktree with read_file, or "
            "submit_for_review to run the gate.)"
        )
    outcome = await session.send(instructions, mid_turn=bool(mid_turn))
    if outcome == "answered":
        text = (
            "Delivered inside the turn the coder is running, as the answer to its question. "
            "Call wait_for_reply to hear back."
        )
    elif outcome == "steered":
        text = (
            "Delivered inside the turn the coder is running; it changes course without stopping. "
            "Call wait_for_reply to hear back."
        )
    elif outcome == "queued":
        text = (
            "Queued: the coder is mid-turn, so this becomes its next turn when the current one "
            "ends. Call wait_for_reply to hear when that turn ends."
        )
    else:
        text = (
            "Sent; the coder is working on it now and you keep your turn. Read the library, plan "
            "the next link or draft your report, and call wait_for_reply when you are ready to hear "
            "back — it returns as soon as the coder stops or asks you something."
        )
    return ToolOutcome(text=text)


def wait_for_reply_tool_def() -> dict:
    """Definition for ``wait_for_reply`` — park until something reaches this seat (E12).

    One wait for every seat. For the lead while a coder is working it also returns when the
    coder's turn ends, which is what ``wait_coder`` was before the channel generalised it.
    """
    floor, default, maximum = coder_wait_limits_s()
    return {
        "name": WAIT_FOR_REPLY_TOOL,
        "description": (
            "Wait for a reply. Returns as soon as a message reaches you — the human's answer, "
            "another member's text, a notice that a member you resumed ended its turn, a human "
            "steer — or, while a coder is working for you, when its turn ends (with its "
            "narration); otherwise after timeout_s, saying what is still outstanding. Waiting "
            "costs nothing while it waits, so prefer long waits: the default is "
            f"{default}s, anything under {floor}s is raised to {floor}s, and the longest is "
            f"{maximum}s. Call it when you have nothing else to do, and read the time it reports "
            "to pace yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "timeout_s": {
                    "type": "integer",
                    "description": (
                        f"Longest to wait, in seconds (default {default}, raised to at least "
                        f"{floor}, at most {maximum}). It returns earlier on any event."
                    ),
                },
            },
            "required": [],
        },
    }


def _wait_timeout(arguments: Mapping[str, Any]) -> tuple[int, str]:
    """The timeout a wait actually uses, and a note when it differs from the one asked for."""
    floor, default, maximum = coder_wait_limits_s()
    raw = arguments.get("timeout_s")
    try:
        requested = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        requested = default
    if requested < floor:
        return floor, f" (asked {requested}s, raised to the {floor}s minimum)"
    if requested > maximum:
        return maximum, f" (asked {requested}s, lowered to the {maximum}s maximum)"
    return requested, ""


def _coder_clock(session: Any) -> str:
    """How long the coder has been on its current turn, for the lead to pace itself."""
    elapsed = getattr(session, "current_instruction_elapsed_s", lambda: None)()
    if elapsed is None:
        return f"coder {getattr(session, 'drive_status', 'idle')}"
    return f"coder working, {_fmt_s(elapsed)} into its current turn"


def _read_coder_news(session: Any) -> tuple[list[str], bool, bool]:
    """Everything the coder has for the lead right now, consumed once.

    Returns ``(parts, failed, turn_read)`` — ``turn_read`` is whether a finished turn's narration
    was among them, which is what decides the closing line a wait adds.
    """
    parts: list[str] = []
    failed = False
    turn_read = False
    if getattr(session, "has_unread_result", lambda: False)():
        turn_read = True
        text, error = session.take_drive_result()
        if error is not None:
            failed = True
            # The coder DIED and the lead is told so. On 2026-09-09 a Claude-harness session
            # whose credential ran out returned the CLI's own billing string with `is_error`
            # discarded, and the lead spent three drives steering a corpse at $0 before giving
            # up — then recorded the failure with a confidently wrong cause.
            parts.append(
                f"The coder session FAILED and is not usable: {error}\n"
                "This is a harness or credential fault, not something to steer around: write "
                "your report and say what happened."
            )
        else:
            parts.append(
                "The coder's turn ended. Its narration:\n\n"
                + ((text or "").strip() or "(no narration; inspect the worktree with read_file)")
            )
    # The coder's questions no longer ride this result: they reach the lead's inbox as messages
    # (E12) and are delivered at its next boundary, like every other text a seat receives.
    return parts, failed, turn_read


async def execute_wait_coder(session: Any, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Park inside the tool call until the coder has something for the lead (replaces polling).

    ``check_coder`` answered at once and was rate-limited, and a refusal costs a model turn as
    surely as an answer: slot 6 (2026-09-15) made 134 checks (114 refused), 47 mid-turn steers,
    31 refused submits and 9 refused ``turn_end`` calls at a 3-10 s cadence, because a loop with
    no way to let time pass emits a tool call to do it. The fix every harness that solved this
    converged on (Codex ``wait_agent``) is a **blocking wait with early wake**: parked, it costs
    nothing; it returns the moment the coder's turn ends or a message lands (the coder's question
    reaches the lead's inbox, E12);
    and a short timeout is raised to a floor instead of being refused, so no calling pattern can
    reintroduce a tight loop. Every return states elapsed time, which is the lead's clock.
    """
    if not hasattr(session, "wait_for_event"):
        return ToolOutcome(
            text="This coder session does not run in the background; a message to the coder returns "
            "its narration directly.",
            is_error=True,
        )
    timeout, note = _wait_timeout(arguments)
    started = time.monotonic()
    reason = await session.wait_for_event(timeout)
    waited = time.monotonic() - started
    header = f"[waited {_fmt_s(waited)} of {_fmt_s(timeout)}{note}; {_coder_clock(session)}]"
    parts, failed, turn_read = _read_coder_news(session)
    if reason == "steer":
        parts.append(
            "A message for you has arrived (the coder's question, the human, or another member); "
            "it is delivered right after this result."
        )
    elif reason == "timeout":
        parts.append(
            f"No news in {_fmt_s(timeout)}: the coder is still working. Carry on with other work, "
            "or call wait_for_reply again — a longer wait costs no more than a short one."
        )
    elif reason == "idle" and not parts:
        parts.append(
            "The coder is not working and has nothing unread, so there is nothing to wait for. "
            "Give it work with message to the coder, inspect the tree with read_file, or submit_for_review."
        )
    elif turn_read and not failed and getattr(session, "drive_status", None) == "working":
        parts.append("It has already started its next turn on the message(s) you queued.")
    elif turn_read and not failed:
        parts.append(
            "It has stopped. Inspect the tree with read_file, message the coder the next change, or "
            "submit_for_review."
        )
    return ToolOutcome(text=header + "\n\n" + "\n\n".join(parts), is_error=failed)


async def _wait_before_submit(session: Any) -> tuple[str, ToolOutcome | None]:
    """Hold a submit until the coder stops, so the gate never judges a half-written tree.

    Returns ``(narration_prefix, None)`` to proceed, or ``("", outcome)`` when the lead needs
    to act first (a message arrived — the coder's question among them — it failed, or it is still working
    at the maximum wait). Nothing here spends a submission.
    """
    _, _, maximum = coder_wait_limits_s()
    started = time.monotonic()
    narration: list[str] = []
    while getattr(session, "drive_status", None) == "working":
        remaining = maximum - (time.monotonic() - started)
        if remaining <= 0:
            return "", ToolOutcome(
                text=(
                    f"Not submitted: the coder is still working after {_fmt_s(maximum)} "
                    f"({_coder_clock(session)}). Call wait_for_reply, then submit once it stops."
                    + ("\n\n" + "\n\n".join(narration) if narration else "")
                )
            )
        reason = await session.wait_for_event(remaining)
        parts, failed, _ = _read_coder_news(session)
        if failed or reason == "steer":
            if reason == "steer":
                parts.append(
                    "A message for you has arrived (the coder's question, the human, or another "
                    "member); it is delivered right after this."
                )
            return "", ToolOutcome(
                text=(
                    "Not submitted yet — something needs you first:\n\n"
                    + "\n\n".join(narration + parts)
                    + ("" if failed else "\n\nsubmit_for_review again once that is settled.")
                ),
                is_error=failed,
            )
        narration.extend(parts)
    waited = time.monotonic() - started
    if not narration and waited < 1:
        return "", None
    prefix = f"[waited {_fmt_s(waited)} for the coder to stop before running the gate]\n\n"
    if narration:
        prefix += "\n\n".join(narration) + "\n\n"
    return prefix, None


async def execute_submit(session: Any, cycle: "LeadCycle | None" = None) -> ToolOutcome:
    """Run the gate, then (gate-pass) the co-lead review, over the session's current tree; commit
    on gate-pass ∧ review-pass (§9.9 commit ⇔ gate ∧ review — the lead never self-approves).
    Returns a verdict the lead diagnoses: a gate failure (fix by messaging the coder), a review
    rejection (message the coder, or respond_to_review), or a successful commit. Bounded by ``session.submit_cap``
    (each submit runs the gate twice + a co-lead review — expensive).

    **Waits for a working coder first.** The gate reads the WORKING TREE, so a gate run while
    the coder is mid-write would run pytest twice and a paid co-lead review over a half-written
    tool and reject it for a defect nobody wrote. It used to *refuse* instead, and a refusal
    returns at once and costs a model turn: slot 6 (2026-09-15) was refused 31 times. Waiting is
    what the lead meant by submitting, and nothing is spent until the gate actually runs."""
    prefix = ""
    if getattr(session, "drive_status", None) == "working":
        if not hasattr(session, "wait_for_event"):
            return ToolOutcome(
                text=(
                    "The coder is still working, and the gate reads the working tree. Submit "
                    "once it has stopped."
                ),
                is_error=True,
            )
        prefix, blocked = await _wait_before_submit(session)
        if blocked is not None:
            return blocked
    outcome = await _submit_now(session, cycle)
    if prefix:
        outcome.text = prefix + outcome.text
    return outcome


async def _submit_now(session: Any, cycle: "LeadCycle | None") -> ToolOutcome:
    """The gate + review + commit half of :func:`execute_submit`, over a tree nobody is writing."""
    if getattr(session, "last_commit_sha", None) is not None and not _has_pending_changes(session):
        # The tool already landed and nothing has changed since. Submitting again would scan an
        # empty changeset and then ask `approve_edit` to commit nothing, which exits non-zero and
        # raises out of this tool call ([F6]: `_react` does not catch tool exceptions). Observed
        # live before the base ref advanced on commit: the lead submitted after its own commit
        # and the §9.9 backstop reverted the approved commit, so the cycle silently repeated —
        # three times on one link, at a full gate + co-lead review each. Budget-free, because
        # answering a question is not a submission.
        return ToolOutcome(
            text=(
                f"{session.tool_spec.proposed_tool_name} is already committed "
                f"({(session.last_commit_sha or '')[:10]}) and nothing has changed since. "
                "Authoring for this tool is done — move on to the next link, or message the coder "
                "first if you want to change something and then submit that."
            )
        )
    if session.submits_used >= session.submit_cap:
        # The CI budget for THIS tool is spent. Before A7 this was where a chain died: the only
        # other actions open were whole-cycle terminals, so a lead holding 11 unspent author
        # credits ended the entire analysis (slot 1, 2026-09-11), and a lead in the identical
        # position paid a debate round and a full worker rerun to say the same thing (slot 2).
        # Now the link closes, the next one opens, and the lead is still in its turn.
        return ToolOutcome(
            text=(
                f"CI BUDGET SPENT — all {session.submit_cap} review submissions for "
                f"{session.tool_spec.proposed_tool_name} are used and it did not commit. This "
                "attempt is over; reconsider the plan and the wiring rather than resubmitting. "
                "An honest failure is an acceptable, correct outcome — never fabricate to force "
                "a pass." + await _advance(cycle)
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
                "GATE FAILED — mechanical checks you can only satisfy. Fix the underlying cause by "
                f"messaging the coder (never weaken a check to pass), then submit_for_review again:\n{body}"
            )
        )
    review = await session.run_review_step(gate)
    if not review.approved:
        issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
        return ToolOutcome(
            text=(
                "GATE PASSED, but the independent co-lead REVIEW REJECTED the tool:\n"
                f"{issues}\n\n{review.rationale}\n\nEither message the coder to address these, or "
                "respond_to_review to defend the design if you believe a challenge is unfounded."
            )
        )
    sha = await session.commit(
        review.commit_summary.strip() or session.tool_spec.proposed_tool_name
    )
    return ToolOutcome(
        text=(
            f"COMMITTED {session.tool_spec.proposed_tool_name} → {sha[:10]}. Gate passed and the "
            "co-lead approved — the tool is on the authoring branch. Authoring for this tool is "
            "done." + await _advance(cycle)
        )
    )


async def _advance(cycle: "LeadCycle | None") -> str:
    """Close this link and open the next, as a line appended to the tool result.

    The chain advances on a *tool result* rather than by ending the round, which is the whole of
    A7: the lead reads "that one is done, here is the next" in the same conversation and keeps
    working. Returns "" when there is no cycle (``drive_authoring``'s standalone path, which owns
    its own single session and must be left byte-identical — it backs published measurements).
    """
    if cycle is None or cycle.advance_chain is None:
        return ""
    return "\n\n" + await cycle.advance_chain()


def _has_pending_changes(session: Any) -> bool:
    """Whether the session holds unlanded work; True for a session that cannot answer.

    A stub session in a test (or any object without the method) is treated as having work, so
    the guard above never changes behaviour for a caller that does not model a tree.
    """
    check = getattr(session, "pending_changes", None)
    return True if check is None else bool(check())


def respond_to_review_tool_def() -> dict:
    """Lead-only: debate the co-lead's most recent rejection (§9.3 — the lead persuades, never
    overrides). Budget-free in the lead's loop; bounded by ``session.debate_cap``."""
    return {
        "name": RESPOND_TO_REVIEW_TOOL,
        "description": (
            "Respond to the co-lead's most recent review rejection — defend the design or a specific "
            "test choice when you believe the challenge is unfounded or already addressed. The "
            "independent co-lead reconsiders your argument and re-issues its verdict: it concedes "
            "only if genuinely persuaded (a sound argument, or evidence the issue is already handled) "
            "and holds firm against pressure. If it is persuaded and the gate still passes, the tool "
            "commits. Use this for a genuine disagreement; when the co-lead is right, fix the code by messaging "
            "the coder instead. Write 'message' as Markdown."
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
                "by messaging the coder, or conclude — an honest failure is an acceptable outcome."
            )
        )
    session.debates_used += 1
    review = await session.run_review_step(session.last_gate, lead_rebuttal=message)
    if not review.approved:
        issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
        return ToolOutcome(
            text=(
                "The co-lead HELD its position after your rebuttal:\n"
                f"{issues}\n\n{review.rationale}\n\nAddress these by messaging the coder, or accept the "
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
                f"{failed}\nFix it by messaging the coder, then submit_for_review."
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
# The analyst lead's cycle forms (§9.3). The lead runs ONE ReAct loop per analysis cycle
# and states each decision by filing a form; the branch it picks IS the decision, and every
# question on that form carries its own description — the channel a Pydantic ``#`` comment
# never reached (a run died on that, 2026-09-01).
#
# Every form is DECLARED for the whole cycle and the **handler** enforces the stage: a
# declared set that shrinks mid-conversation is the "Tool reference not found" run-killer
# (2026-07-29), so an out-of-stage call comes back as a correction to act on rather than as a
# tool that vanished. The honest terminal is reachable from every stage (§9.6).
# ----------------------------------------------------------------------------

AUTHOR_CHAIN_TOOL = "author_chain"

#: The lead's only form. It is a **brief**, not a decision: a task handed to a coding subagent,
#: the way a ``ToolSpec`` always was. Filing it does not end anything — the loop continues, and
#: the lead ends its turn with ``turn_end`` once its report is written (A4/A7).
#:
#: What used to live here: ``hold_authoring``, ``request_another_round``, ``submit_adjudication``
#: and ``submit_terminal_report``, plus a three-stage desk deciding which were callable when. All
#: five are gone. The report carries the decision now, which is the same contract an analyst
#: already writes — and two of the four handlers validated nothing anyway, copying an
#: already-validated report onto the cycle and differing from each other by one field the report
#: itself carries.
#:
#: The defect that forced this: a decision tool ended the round the instant it was accepted, and
#: while authoring the only two the lead could call were both whole-cycle terminals. So "this
#: link is dead, try the next" was inexpressible. Slot 1 of the 2026-09-11 batch ended a whole
#: analysis with 11 of 12 author credits unspent; slot 2, from the identical position, paid a
#: debate round and a full worker rerun to say the same thing. Same missing action, twice.
LEAD_FORM_TOOLS: frozenset[str] = frozenset({AUTHOR_CHAIN_TOOL})


@dataclass
class LeadCycle:
    """One analysis cycle's state, wired onto the lead's provider for the whole cycle.

    **No stage.** One cycle is one ReAct loop, and the loop ends on ``turn_end`` when the lead's
    report is complete (A4). The stage desk it replaced was a second mechanism saying what the
    report already says, and its author stage had no instruction text of its own.

    ``authoring_enabled`` is fixed at construction — it decides whether the authoring branch is
    DECLARED at all, so the declared tool set stays constant while the live ``authoring_session``
    is swapped per chain link. A declared set that shrinks mid-conversation is the "Tool reference
    not found" run-killer (2026-07-29). ``rounds_left`` is the debate rounds the cap still allows,
    checked at ``turn_end`` so a report asking for rework with no rounds left is refused with the
    cap named: Python owns the caps, the loop owns the choice.

    ``brief`` is whatever ``author_chain`` last wrote, read by ``run_cycle`` after the loop stops.
    """

    authoring_enabled: bool = False
    rounds_left: int = 0
    brief: AuthoringDecision | None = None

    #: Opens the next unfinished link's coding session and returns what to tell the lead, or a
    #: note saying the chain is finished. Supplied by ``run_cycle``, which owns every session and
    #: every git operation (§9.9); the handlers only ask for the next one.
    #:
    #: This callback is what lets ONE ReAct loop span a whole chain. Before A7 each link ran its
    #: own nested loop with the decision tools as terminals, so the only actions open while
    #: authoring both ended the cycle — and "this link is dead, try the next" could not be said.
    advance_chain: Any | None = None

    #: True while a coding session is open, so ``turn_end`` can refuse a turn that walks away from
    #: a running coder rather than finishing or abandoning it deliberately.
    session_open: bool = False
@dataclass
class LeadCycleOutcome:
    """What one turn of the lead's cycle loop concluded (``AnalystLeadAgent.run_cycle``).

    ``report`` is the decision (A3): the lead writes it with ``edit_report`` and ends its turn on
    ``turn_end``, and its ``rework`` flag says whether the analysts go again. ``None`` means the
    lead never wrote one, which is the failure the caller treats as such — nothing is composed on
    its behalf ([F6]). ``filed`` records how the turn ended, for the ledger.

    ``decision`` (the brief it wrote, if any), ``authored``, ``committed`` and the remaining
    ``author_budget`` record what the chain did, so the caller can reload discovery, restart the
    sandboxes and rerun the workers (A8/A11) without asking the model anything.
    """

    filed: str | None
    report: Any | None = None
    decision: AuthoringDecision | None = None
    authored: list[MergeFlowResult] = field(default_factory=list)
    committed: int = 0
    author_budget: int = 0

    @property
    def authored_chain(self) -> bool:
        """Whether this cycle briefed a chain at all.

        A committed tool is new evidence the current reports predate, so [A8]'s rerun-once happens
        between cycles: the caller reruns the workers and the lead judges a fresh set next cycle.
        """
        return self.decision is not None

    @property
    def wants_another_round(self) -> bool:
        """The lead's report asks for one more debate round."""
        return bool(getattr(self.report, "rework", False))


def _nullable(schema: dict) -> dict:
    """A schema that also permits ``null``, as ``anyOf`` with a literal type on every branch.

    Strict mode requires **every** non-nullable property to appear in ``required``, so a field the
    Pydantic model treats as optional has to be *nullable* rather than absent. ``anyOf`` is chosen
    over a type array because it is what all four dialects are measured or documented to take: it
    is on Anthropic's supported list, Qwen accepts it 5/5, and DeepSeek's validator requires each
    branch to carry a literal ``type`` — which both branches here do.
    """
    description = schema.pop("description", "")
    return {"anyOf": [schema, {"type": "null"}], "description": description}


def _strictify(schema: dict) -> dict:
    """Make an ``input_schema`` satisfy strict mode's two structural requirements, recursively.

    Strict mode (``strict: true``) is what turns a declared schema into an enforced one — the
    provider constrains sampling so the arguments cannot violate it. Both providers require the
    same two things of every object: ``additionalProperties: false``, and **every** property
    listed in ``required``. Optionality is then expressed by nullability (see :func:`_nullable`),
    not by omission from ``required``.

    Applied at definition time rather than written by hand at each level, because the requirement
    is "every object, however deep" and a hand-maintained version drifts the moment a field is
    added. A schema that keeps an unsupported keyword earns a 400 with details, so this is
    checked by construction rather than hoped for.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    for key in ("anyOf", "allOf"):
        if isinstance(out.get(key), list):
            out[key] = [_strictify(branch) for branch in out[key]]
    if isinstance(out.get("items"), dict):
        out["items"] = _strictify(out["items"])
    properties = out.get("properties")
    if isinstance(properties, dict):
        out["properties"] = {name: _strictify(sub) for name, sub in properties.items()}
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


def _field_desc(model: type, name: str) -> str:
    """The ``description`` Pydantic carries for ``model``'s field ``name``.

    One source of prose for both the schema and the form: the field descriptions added to
    ``schemas/toolspec.py`` are what the model reads here, so the worker's view and the lead's
    view of the same object cannot drift apart.
    """
    field_info = model.model_fields[name]
    return str(field_info.description or "")


def _field_enum(model: type, name: str) -> list[str]:
    """The allowed values of a ``Literal`` field on ``model`` (e.g. ``disposition``)."""
    return [str(v) for v in get_args(model.model_fields[name].annotation)]


def _validation_data_schema() -> dict:
    """The ``validation_data`` array — real inputs a tool's checks run against."""
    return {
        "type": "array",
        "minItems": 1,
        "description": _field_desc(ToolSpec, "validation_data"),
        "items": {
            "type": "object",
            "properties": {
                "mission": {
                    "type": "string",
                    "description": _field_desc(ValidationDataset, "mission"),
                },
                "obsid": {
                    "type": "string",
                    "description": _field_desc(ValidationDataset, "obsid"),
                },
                "synthetic": {
                    "type": "boolean",
                    "description": _field_desc(ValidationDataset, "synthetic"),
                },
            },
        },
    }


def _proposed_checks_schema() -> dict:
    """The ``proposed_checks`` array — the must-have tests the lead owns (§9.4 open menu)."""
    return {
        "type": "array",
        "description": _field_desc(ToolSpec, "proposed_checks"),
        "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": _field_desc(ProposedCheck, "kind")},
                "description": {
                    "type": "string",
                    "description": _field_desc(ProposedCheck, "description"),
                },
                "tolerance": _nullable({
                    "type": "number",
                    "description": _field_desc(ProposedCheck, "tolerance"),
                }),
            },
        },
    }


def _tool_spec_schema() -> dict:
    """One chain link — the opening prompt of one coding session.

    Hand-written rather than ``ToolSpec.model_json_schema()`` on purpose: that emits
    ``$defs``/``$ref``, and reference resolution inside a nested position is measured to be
    per-vendor (2026-08-24). A flat, literal object array is the shape all five vendors were
    probed to accept and fill (2026-09-02). ``id``/``created_at`` are set by the runtime and
    are absent here.
    """
    return {
        "type": "object",
        "properties": {
            "science_context": {
                "type": "string",
                "description": _field_desc(ToolSpec, "science_context"),
            },
            "expected_behavior": {
                "type": "string",
                "description": _field_desc(ToolSpec, "expected_behavior"),
            },
            "proposed_tool_name": {
                "type": "string",
                "description": _field_desc(ToolSpec, "proposed_tool_name"),
            },
            "target_domain": {
                "type": "string",
                "description": _field_desc(ToolSpec, "target_domain"),
            },
            "disposition": {
                "type": "string",
                "enum": _field_enum(ToolSpec, "disposition"),
                "description": _field_desc(ToolSpec, "disposition"),
            },
            "request_kind": {
                "type": "string",
                "enum": _field_enum(ToolSpec, "request_kind"),
                "description": _field_desc(ToolSpec, "request_kind"),
            },
            "observed_issue": _nullable({
                "type": "string",
                "description": _field_desc(ToolSpec, "observed_issue"),
            }),
            "validation_data": _validation_data_schema(),
            "proposed_checks": _proposed_checks_schema(),
            "replication_tolerance": _nullable({
                "type": "number",
                "description": _field_desc(ToolSpec, "replication_tolerance"),
            }),
            "notes": _nullable(
                {"type": "string", "description": _field_desc(ToolSpec, "notes")}
            ),
        },
        # `required` is deliberately absent: `_strictify` sets it to EVERY property, because
        # strict mode requires exactly that and expresses optionality through nullability instead
        # (see `_nullable`). Listing a subset here would be overwritten and would misdescribe the
        # contract to anyone reading it.
    }


def author_chain_tool_def() -> dict:
    """Definition for ``author_chain`` — file the ordered chain and open the coding session.

    **The first link is a required property, not the first element of a non-empty array**, and
    that distinction is the whole point. "Author, with nothing to author" is the state that killed
    a live run ($19.43, 2026-09-01) and it must be *ungeneratable*, not merely rejected.

    An earlier version of this tool expressed the rule as ``tool_specs`` with ``minItems: 1``, and
    a lead filed an empty array through it anyway (observed live 2026-09-02) — because cardinality
    keywords are enforced on **no** path. Anthropic's strict mode is opt-in per tool and its
    compilable subset drops range/cardinality constraints (the SDKs strip `minimum`/`maxLength`
    and friends outright), so `minItems` was documentation the model could read and ignore. What
    *is* enforced, once strict mode is on, is `required` — so the rule is expressed as a required
    property here, which also degrades correctly on every vendor that enforces nothing: a model
    filling the schema at all fills `first_link`.

    The handler still checks, because "degrades correctly" is not "cannot happen" until strict
    mode is on for every seat.
    """
    return {
        "name": AUTHOR_CHAIN_TOOL,
        "description": (
            "File the ordered chain of tool briefs to author now, and open the coding session on "
            "its first link. Each link IS the opening prompt of one coding session: a coder reads "
            "it and writes that tool, so write it as instructions to an engineer — the signature, "
            "the exact procedure, the edge cases, the tests it must add. A worker surfaces only "
            "the FIRST gap it hits, while you read the whole analysis prescription: file the "
            "COMPLETE dependency chain in dependency order (acquire, then reduce, then analyse, "
            "then plot), keeping a data/analysis tool and its companion plotter as separate links "
            "in the same chain. Read the target lib/ source with read_file/list_dir before filing, "
            "so each link's target_domain and disposition reflect what is already there. The links "
            "are authored back-to-back, each coder seeing the previous link's committed code; you "
            "then steer each one with message to the coder and finish it with submit_for_review. File "
            "If the request resolves without new code, write your report and end your turn."
        ),
        "strict": True,
        "input_schema": _strictify({
            "type": "object",
            "properties": {
                "first_link": {
                    **_tool_spec_schema(),
                    "description": (
                        "The FIRST tool to author — the one nothing else in the chain depends on "
                        "(acquire before reduce before analyse before plot). Filing this form "
                        "means authoring this tool, so it is required."
                    ),
                },
                "additional_links": {
                    "type": "array",
                    "description": (
                        "The rest of the chain after first_link, still in dependency order. Omit "
                        "it when one tool is the whole chain."
                    ),
                    "items": _tool_spec_schema(),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "Optional: why this chain, in a line or two — what you read in lib/ and "
                        "what the analysis still needs."
                    ),
                },
            },
        }),
    }


async def execute_author_chain(cycle: LeadCycle, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Write the brief. Each link is validated into a real :class:`ToolSpec`, so a malformed
    brief comes back as a correction the lead can fix in-loop ([F11]).

    This does **not** end the turn (A4/A7). Writing a brief is an action inside the cycle, like
    writing the report: the lead can write one, watch it fail, and write another without the
    round unwinding under it.
    """
    # `first_link` is a required property and `additional_links` the optional tail, so the chain
    # is assembled here rather than sent as one array whose emptiness no provider checks. The
    # guard below is the backstop for a vendor enforcing neither `required` nor anything else.
    first = arguments.get("first_link")
    rest = arguments.get("additional_links") or []
    raw = ([first] if isinstance(first, dict) else []) + (list(rest) if isinstance(rest, list) else [])
    if not raw:
        return ToolOutcome(
            text=(
                "author_chain needs 'first_link': the brief for the first tool to author (and "
                "'additional_links' for the rest of the chain, in dependency order). If the "
                "request resolves without new code, write your report and end your turn instead."
            ),
            is_error=True,
        )
    specs: list[ToolSpec] = []
    problems: list[str] = []
    for position, item in enumerate(raw, 1):
        try:
            specs.append(ToolSpec.model_validate(item))
        except ValidationError as exc:
            fields = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
            )
            label = "first_link" if position == 1 else f"additional_links[{position - 2}]"
            problems.append(f"{label}: {fields}")
        except (TypeError, AttributeError) as exc:
            label = "first_link" if position == 1 else f"additional_links[{position - 2}]"
            problems.append(f"{label}: {type(exc).__name__}: {exc}")
    if problems:
        return ToolOutcome(
            text="These chain links are incomplete — fix them and file again:\n"
            + "\n".join(f"- {p}" for p in problems),
            is_error=True,
        )
    cycle.brief = AuthoringDecision(
        should_author=True,
        tool_specs=specs,
        rationale=str(arguments.get("rationale") or "").strip(),
    )
    names = ", ".join(spec.proposed_tool_name for spec in specs)
    opened = ""
    if cycle.advance_chain is not None:
        opened = "\n\n" + await cycle.advance_chain()
    return ToolOutcome(
        text=(
            f"Brief accepted — {len(specs)} link(s), in order: {names}. Your turn continues; "
            "when the chain is done, or you decide it is not worth continuing, write your report "
            "and call turn_end." + opened
        )
    )


# ----------------------------------------------------------------------------
# Analyst science tools — the existing ToolkitRegistry + PersistentSandbox.
# ----------------------------------------------------------------------------

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


class SandboxToolProvider:
    """Analyst tool surface: ``ToolkitRegistry`` tools run in a ``PersistentSandbox``.

    Mirrors the per-call routing/argument-resolution the old ``execute_tools`` did:
    map the provider-safe function name back to the dotted ``kind``, auto-inject
    ``cwd`` for ``bash_exec`` and instrument data paths for toolkit tools, then call
    the sandbox. The model self-corrects on errors via the ReAct loop (it sees the
    error text and re-issues), so there is no separate retry here. Every executed
    ``ToolResult`` accumulates in ``results`` for memory/peer-summary use.
    """

    # Class-level defaults so a provider built without __init__ (tests) reads as unreceipted.
    receipts: Any | None = None
    receipt_source: str | None = None
    extra_tools: Sequence[ExtraTool] = ()

    def __init__(
        self,
        registry: Any,  # ToolkitRegistry — avoid import cycle
        sandbox: Any,  # PersistentSandbox
        *,
        cwd: Path | None = None,
        timeout_s: int = 300,
        web_source: Any | None = None,
        figure_sink: list[FigureRef] | None = None,
        report_workspace: ReportWorkspace | None = None,
        receipts: Any | None = None,
        receipt_source: str | None = None,
        extra_tools: Sequence[ExtraTool] = (),
    ) -> None:
        self.registry = registry
        self.sandbox = sandbox
        self.cwd = cwd
        # Provenance (P1): the run's ReceiptStore and this agent's source key. Every tool call
        # mints receipts; the model sees each result labelled with its handle.
        self.receipts = receipts
        self.receipt_source = receipt_source
        self.extra_tools = list(extra_tools)
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
        if self.web_source is not None:
            defs.append(web_search_tool_def())
            defs.append(web_fetch_tool_def())
        if self.report_workspace is not None:
            defs.append(
                edit_report_tool_def(
                    self.report_workspace.prose_fields, self.report_workspace.status_values,
                    self.report_workspace.report_model, self.report_workspace.edit_description,
                )
            )
            defs.append(review_report_tool_def())
            # The universal terminal (F5c). Declared with the report tools because those are what
            # it validates: without it the loop ended on "no tool calls", and an unwritten report
            # was composed on the model's behalf.
            defs.append(turn_end_tool_def())
        defs.extend(definition for definition, _ in getattr(self, "extra_tools", ()))
        return defs

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        # ``data_catalog`` is a registry tool here — it falls through to the sandbox path below,
        # not a special in-process branch (only the sandbox-less lead runs it in-process).
        extra = await _run_extra_tool(getattr(self, "extra_tools", ()), name, arguments)
        if extra is not None:
            return extra
        if name == WEB_SEARCH_TOOL:
            return _receipted(self.receipts, self.receipt_source, name, arguments,
                              await execute_web_search(self.web_source, arguments))
        if name == WEB_FETCH_TOOL:
            return await _web_receipted(
                self.receipts, self.receipt_source, arguments,
                await execute_web_fetch(self.web_source, arguments),
            )
        if name == EDIT_REPORT_TOOL:
            return await execute_edit_report(self.report_workspace, arguments)
        if name == REVIEW_REPORT_TOOL:
            return await execute_review_report(self.report_workspace, arguments)
        if name == TURN_END_TOOL:
            return await execute_turn_end(
                lambda: worker_report_problems(self.report_workspace), arguments
            )
        kind = self.registry.kind_for_function(name)
        args = dict(arguments)
        if kind == "mcp.builtin.bash_exec" and "cwd" not in args and self.cwd is not None:
            args["cwd"] = str(self.cwd)

        # Filesystem allow-list on the builtin file tools (the raw read/list/bash surface).
        # read_file/list_dir are checked by their target and always: they run IN THIS PROCESS, so
        # no OS jail can reach them, and this guard is the only thing standing between the lead or
        # the reviewer and the filesystem (§2/§4). bash_exec is different -- see below.
        denied = None
        if kind == "mcp.builtin.bash_exec":
            # Superseded by the OS jail where one is running. The command check scans a command
            # STRING for absolute-looking substrings, which enforces nothing the kernel jail does
            # not already enforce -- and the jail also catches the implicit reads a string scan
            # cannot see. What the string scan does add is false positives: a python glob
            # `f'{root}/{o}/event_l2/ixpe{o}_det*'` reads as a path `/event_l2/ixpe`, and a path
            # relative to a `cwd` reads as `/pcube_timeseg_01002901.csv`. Measured 2026-09-01: four
            # rejections in one run across two actors, three of them consecutive on one worker,
            # each costing a loop iteration. A model that exhausts its budget fighting a path
            # checker is indistinguishable from one that could not do the work.
            # `None` means "nothing denied" here -- an empty list would be a *rejection* with an
            # empty reason, i.e. every shell command blocked. The suite caught exactly that.
            denied = None if _jail_supersedes_command_check() else \
                self._guard.forbidden_in_command(args.get("command", ""))
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
        state = None
        if not self.registry.is_known(kind):
            raw = {"exit_code": 127, "stderr": f"Tool not registered: {kind}", "stdout": ""}
        elif self.registry.is_mcp(kind):
            state = await self._inputs_before(args)
            raw = await self.sandbox.call_mcp_tool(kind, args, timeout_s=call_timeout)
        else:
            spec = self.registry.spec(kind)
            schema_props = (spec.get("args_schema") or {}).get("properties", {})
            for arg_name, path in _observation_path_args().items():
                if arg_name in schema_props and not args.get(arg_name):
                    args[arg_name] = path
            self._redirect_figure_args(spec["fn"], args, schema_props)
            state = await self._inputs_before(args)
            raw = await self.sandbox.call_tool(
                spec["module"], spec["fn"], args, timeout_s=call_timeout
            )
        elapsed = time.monotonic() - t0

        raw = normalize_tool_raw(raw)
        figures, figure_notes = parse_tool_figures(raw.get("figures"))
        text, exit_code = tool_text(raw)
        header = ""
        provenance_notes: list[str] = []
        if self.receipts is not None and self.receipt_source is not None:
            # The runtime's receipts, not the tool's word: figures come back carrying the hash of
            # the file on disk, and a figure whose file is absent is dropped here.
            minted = await asyncio.to_thread(
                receipt_call, self.receipts, source=self.receipt_source, produced_by=kind,
                args=args, text=text, failed=exit_code != 0, state=state, figures=figures,
                cwd=self.cwd, toolkit_root=getattr(self.registry, "_root", None),
            )
            figures = minted.figures
            provenance_notes = minted.notes
            header = minted.header() + "\n"
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
            exit_code=exit_code,
            figures=figures,
            elapsed_s=elapsed,
        )
        self.results.append(result)
        text = header + text
        # Surface any dropped figures so the analyst notices its plotter emitted a bad
        # FigureRef (rather than the figure silently vanishing) — never fatal (F4).
        if figure_notes:
            text += f"\n[figure capture: {len(figure_notes)} dropped] " + " | ".join(figure_notes)
        if provenance_notes:
            text += "\n[provenance] " + " | ".join(provenance_notes)
        return ToolOutcome(text=text, is_error=result.exit_code != 0, figures=figures, result=result)

    async def _inputs_before(self, args: Mapping[str, Any]) -> Any:
        """Hash what the call's arguments name, before it runs (data roots and outputs skipped)."""
        if self.receipts is None or self.receipt_source is None:
            return None
        skip = set(_observation_path_args()) | set(_FIGURE_PATH_ARGS)
        return await asyncio.to_thread(
            inputs_before, self.receipts, args, cwd=self.cwd, skip_keys=skip
        )

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

    receipts: Any | None = None
    receipt_source: str | None = None
    extra_tools: Sequence[ExtraTool] = ()

    def __init__(
        self,
        *,
        roots: Sequence[Path],
        retrieval: Any | None = None,
        web_source: Any | None = None,
        include_text_search: bool = True,
        include_open_paper: bool = True,
        include_data_catalog: bool = False,
        authoring_session: Any | None = None,
        cycle: LeadCycle | None = None,
        report_workspace: ReportWorkspace | None = None,
        editable_file: Path | None = None,
        turn_end_validator: Callable[[], list[str]] | None = None,
        receipts: Any | None = None,
        receipt_source: str | None = None,
        extra_tools: Sequence[ExtraTool] = (),
    ) -> None:
        # Provenance (P1): read results are receipted like any tool output, as the record of where
        # a role looked; only a toolkit tool's return or a commit is evidence (`is_evidence`, human
        # ruling 2026-09-25). Role-specific tools arrive through `extra_tools`.
        self.receipts = receipts
        self.receipt_source = receipt_source
        self.extra_tools = list(extra_tools)
        self.roots = [Path(r).resolve() for r in roots]
        # F5e: the ONE file this role may write, and what must hold before its turn can end.
        # Set only for the reviewer, on its own `report.tex`. `file_edit` never takes a path from
        # the model, so the write surface is this file and nothing else — the bounded form of
        # "no pure read-only roles: a role writes to its own work product only" (F10).
        self.editable_file = Path(editable_file) if editable_file is not None else None
        self.turn_end_validator = turn_end_validator
        # F10: reads follow the shared allow-list; writes are scoped to this role's own work
        # folder — the directory holding its editable file, and nothing when it has none. The
        # guard is the second bound, independent of the tool never accepting a model-supplied
        # path, so a mis-wired provider cannot widen the write surface either.
        self._guard = PathGuard.from_config(
            writable=[self.editable_file.parent] if self.editable_file is not None else [],
        )
        self.retrieval = retrieval
        # V-4: the analyst lead's per-round report-authoring workspace (edit_report/review_report).
        # Set per adjudication by the lead (its per-worker figure catalog + subordinate reports);
        # None outside adjudication (so decide_authoring/drive_authoring never see the report tools).
        self.report_workspace = report_workspace
        # The analyst lead (only) drives tool authoring via a live AuthoringSession (§9.3
        # B-topology): message to the coder / submit_for_review. Masked to the lead alone — a session is
        # wired here only on the lead's provider; no other role sees the tools. None ⇒ disabled.
        self.authoring_session = authoring_session
        # The analyst lead's one-cycle form desk (§9.3). When set, the five cycle forms are
        # declared for the WHOLE cycle and their handlers enforce the stage — so the declared set
        # stays constant while `authoring_session` is swapped per chain link, and the drive tools
        # are declared from the start (`cycle.authoring_enabled`) rather than appearing with the
        # first session. None ⇒ the pre-forms surface (every other role, and the lead's own
        # single-action calls).
        self.cycle = cycle
        # The analyst lead (not the reviewer) gets the read-only data_catalog tool so it can
        # check local data availability before deciding to author an acquire tool (§9.10).
        self.include_data_catalog = include_data_catalog
        # Optional Tavily/web QuerySource — when supplied (analyst lead), exposes a
        # web_search tool so the lead can consult authoritative documentation (mission
        # threads, calibration memos) before triaging a tool request or answering the
        # author, rather than guessing (CLAUDE.md §9.3 escalation ladder).
        self.web_source = web_source
        # The papers knowledge base is irrelevant to pure code review, so the §9.3
        # co-lead reviewer omits text_search (roots = the toolkit worktree instead).
        # text_search + open_paper are gated together as the knowledge-base toolset.
        self.include_text_search = include_text_search
        # The reviewer reads papers as receipted text through its own read_paper (extra_tools);
        # the native-PDF open_paper stays off for it (human ruling, 2026-09-24).
        self.include_open_paper = include_open_paper
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
        if self.include_text_search and self.include_open_paper:
            definitions.append(
                {
                    "name": "open_paper",
                    "description": (
                        "Open a local PDF (text + figures) to read it before citing. Pass a path "
                        "from text_search. The papers you open and actually use are exactly the "
                        "ones to list in the verdict's cited_paths; the report's References "
                        "section links to those files. A paper too large for the model's provider "
                        "is reported back with the reason, so you can cite it from its "
                        "text_search metadata instead."
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
        if self.authoring_session is not None or self._authoring_declared():
            definitions.append(wait_for_reply_tool_def())
            definitions.append(submit_tool_def())
            definitions.append(respond_to_review_tool_def())
        if self.cycle is not None and self.cycle.authoring_enabled:
            # Gated on the CYCLE's flag, never on a live session: the session is swapped per chain
            # link, and a declared set that shrinks mid-conversation is the "Tool reference not
            # found" run-killer (2026-07-29).
            definitions.append(author_chain_tool_def())
        if self.report_workspace is not None:
            definitions.append(
                edit_report_tool_def(
                    self.report_workspace.prose_fields, self.report_workspace.status_values,
                    self.report_workspace.report_model, self.report_workspace.edit_description,
                )
            )
            definitions.append(review_report_tool_def())
        if self.editable_file is not None:
            definitions.append(file_edit_tool_def(self.editable_file))
        if self.turn_end_validator is not None:
            definitions.append(turn_end_tool_def())
        definitions.extend(definition for definition, _ in getattr(self, "extra_tools", ()))
        return definitions

    def _authoring_declared(self) -> bool:
        """Whether the drive tools are declared for the whole cycle.

        Declared from the cycle's start, not from the first live session: the chain's links each
        get their own ``AuthoringSession``, and a set that gained then lost the ``coder`` address between
        links would replay a trajectory naming an undeclared tool (the 2026-07-29 run-killer).
        """
        return self.cycle is not None and self.cycle.authoring_enabled

    def _no_session_text(self) -> str:
        """Why a drive tool has nothing to drive — the stage, when a cycle is running."""
        if self.cycle is None:
            return "authoring is not enabled for this role."
        if self.cycle.authoring_enabled:
            return (
                "No coding session is open. File author_chain first: it opens the session on the "
                "chain's first link, and the drive tools act on whichever link is open."
            )
        return "authoring is not enabled for this run."

    def _allowed(self, path: Path) -> bool:
        resolved = path.resolve()
        # §4: build-time governance files never enter runtime context, even inside a root
        # (research_toolkit carries its own CLAUDE.md/DOCS.md). Closes the reviewer/lead leak.
        if resolved.name in GOVERNANCE_BASENAMES:
            return False
        return any(resolved == root or root in resolved.parents for root in self.roots)

    #: What a read-only role reads is evidence it may cite; its harness tools are not.
    _RECEIPTED: dict[str, str] = {
        "read_file": "result", "list_dir": "result", "text_search": "result",
        DATA_CATALOG_TOOL: "result", WEB_SEARCH_TOOL: "result", WEB_FETCH_TOOL: "web",
    }

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        extra = await _run_extra_tool(getattr(self, "extra_tools", ()), name, arguments)
        if extra is not None:
            return extra
        session = self.authoring_session if name == SUBMIT_TOOL else None
        committed_before = getattr(session, "last_commit_sha", None)
        outcome = await self._execute(name, arguments)
        if session is not None:
            return self._commit_receipted(session, committed_before, outcome)
        kind = self._RECEIPTED.get(name)
        if kind is None or outcome.is_error:
            return outcome
        if kind == "web":
            return await _web_receipted(self.receipts, self.receipt_source, arguments, outcome)
        # A toolkit tool the lead runs in-process is receipted under its registry kind, so it is
        # evidence like the same tool run in a worker's sandbox (`provenance.store.is_evidence`).
        producer = _CATALOG_UTILS_KIND if name == DATA_CATALOG_TOOL else name
        return _receipted(self.receipts, self.receipt_source, producer, arguments, outcome, kind=kind)

    def _commit_receipted(self, session: Any, before: str | None, outcome: ToolOutcome) -> ToolOutcome:
        """A submit that committed becomes a ``commit`` receipt the runtime writes from the session.

        The coding pipeline's outcome is evidence the lead may cite (human ruling 2026-09-25): the
        SHA, the tool, and that the gate and the co-lead review both passed — which is what a
        commit means (§9.9). Written from the session's own state, never from anything the model
        typed; a submit that did not commit mints nothing.
        """
        sha = getattr(session, "last_commit_sha", None)
        if sha is None or sha == before or self.receipts is None or self.receipt_source is None:
            return outcome
        spec = getattr(session, "tool_spec", None)
        record = {
            "tool": getattr(spec, "proposed_tool_name", None), "commit_sha": sha,
            "branch": getattr(session, "branch", None), "gate_passed": True, "review_approved": True,
            "submissions_used": getattr(session, "submits_used", None),
            "submit_cap": getattr(session, "submit_cap", None),
        }
        minted = self.receipts.mint(
            source=self.receipt_source, kind="commit", produced_by="approve_edit",
            data=json.dumps(record, sort_keys=True),
        )
        return replace(outcome, text=f"[receipt {minted.handle}] (commit)\n{outcome.text}")

    async def _execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == DATA_CATALOG_TOOL:
            return await execute_data_catalog(arguments)
        if name in (WAIT_FOR_REPLY_TOOL, SUBMIT_TOOL, RESPOND_TO_REVIEW_TOOL):
            if self.authoring_session is None:
                return ToolOutcome(text=self._no_session_text(), is_error=True)
            if name == WAIT_FOR_REPLY_TOOL:
                return await execute_wait_coder(self.authoring_session, arguments)
            if name == SUBMIT_TOOL:
                return await execute_submit(self.authoring_session, self.cycle)
            return await execute_respond_to_review(self.authoring_session, arguments)
        if name == AUTHOR_CHAIN_TOOL and self.cycle is not None:
            return await execute_author_chain(self.cycle, arguments)
        if name == EDIT_REPORT_TOOL:
            return await execute_edit_report(self.report_workspace, arguments)
        if name == REVIEW_REPORT_TOOL:
            return await execute_review_report(self.report_workspace, arguments)
        if name == FILE_EDIT_TOOL:
            return await execute_file_edit(self.editable_file, arguments, self._guard)
        if name == TURN_END_TOOL:
            return await execute_turn_end(self.turn_end_validator, arguments)
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
            except (ValueError, TypeError) as exc:
                # A malformed argument (live: offset='150, ') is the model's slip to correct,
                # never a crash: surface it on the tool result like every other tool error.
                return ToolOutcome(
                    text=(
                        f"invalid argument: {type(exc).__name__}: {exc}. "
                        "offset and limit must be plain integers."
                    ),
                    is_error=True,
                )
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

        if name == "open_paper" and self.include_text_search and self.include_open_paper:
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
                # No size cap here. Whether a PDF fits is a *provider* question — bytes, pages,
                # and how much other media the request already carries — and only the adapter sees
                # all of that (`models/providers/_media.py`). A blanket 8 MB rule refused papers
                # every provider would have accepted, and still missed the limits that matter.
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
