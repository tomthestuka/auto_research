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
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from auto_research.observations import load_observations_config
from auto_research.schemas import FigureRef, ToolResult


# Figure-output arguments, by naming convention → the extension the toolkit expects.
# A toolkit tool exposing one of these args has its figure written under the worker's
# figures_dir (see SandboxToolProvider._redirect_figure_args). ``""`` = base/prefix path
# (the tool appends its own suffix, e.g. ``_DU1.png``); ``.png`` = full file path.
# Convention-keyed (not tool-keyed) so new toolkits inherit it for free.
_FIGURE_PATH_ARGS: dict[str, str] = {"save_path": ".png", "save_path_prefix": ""}


@dataclass
class ToolOutcome:
    """Result of one tool execution, as fed back into the ReAct conversation.

    ``text`` is what the model sees next turn; ``result`` is the full record (for
    memory/figure propagation) when the provider produces one.
    """

    text: str
    is_error: bool = False
    figures: list[FigureRef] = field(default_factory=list)
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


def ask_human_tool_def() -> dict:
    """The provider-neutral definition for the shared ``ask_human`` tool."""
    return {
        "name": ASK_HUMAN_TOOL,
        "description": (
            "Escalate to a real human when blocked: a genuine expert-science/code question "
            "or a hard block (missing data, broken dependency, contradictory request) you "
            "cannot resolve yourself. Blocks until the human replies. Ask early and "
            "specifically rather than guessing or fabricating."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "kind": {"type": "string", "enum": list(_ASK_HUMAN_KINDS)},
                "context": {"type": "string"},
            },
            "required": ["question"],
        },
    }


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
        question=str(arguments.get("question") or "(no question provided)"),
        context=str(arguments.get("context") or ""),
        asked_by=asked_by or "agent",
        model=model or "",
    )
    response = await resolve_human(human_handler, query)
    return ToolOutcome(text=response.answer or "(human gave no answer)")


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
        asked_by: str = "analyst_worker",
        model: str = "",
    ) -> None:
        self.registry = registry
        self.sandbox = sandbox
        self.cwd = cwd
        # When a human_handler is wired, expose the shared ask_human tool (§9.7).
        self.human_handler = human_handler
        self.asked_by = asked_by
        self.model = model
        # Figures land in <worker scratch>/figures (under the runtime data dir),
        # never in /tmp or a repo. See _redirect_figure_args.
        self.figures_dir = (Path(cwd) / "figures") if cwd is not None else None
        self._figures_dir_ready = False
        self.timeout_s = timeout_s
        self.results: list[ToolResult] = []

    def tool_definitions(self) -> list[dict]:
        defs = list(self.registry.tool_definitions())
        if self.human_handler is not None:
            defs.append(ask_human_tool_def())
        return defs

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == ASK_HUMAN_TOOL:
            return await execute_ask_human(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        kind = self.registry.kind_for_function(name)
        args = dict(arguments)
        if kind == "mcp.builtin.bash_exec" and "cwd" not in args and self.cwd is not None:
            args["cwd"] = str(self.cwd)

        t0 = time.monotonic()
        if not self.registry.is_known(kind):
            raw = {"exit_code": 127, "stderr": f"Tool not registered: {kind}", "stdout": ""}
        elif self.registry.is_mcp(kind):
            raw = await self.sandbox.call_mcp_tool(kind, args, timeout_s=self.timeout_s)
        else:
            spec = self.registry.spec(kind)
            schema_props = (spec.get("args_schema") or {}).get("properties", {})
            for arg_name, path in _observation_path_args().items():
                if arg_name in schema_props and not args.get(arg_name):
                    args[arg_name] = path
            self._redirect_figure_args(spec["fn"], args, schema_props)
            raw = await self.sandbox.call_tool(
                spec["module"], spec["fn"], args, timeout_s=self.timeout_s
            )
        elapsed = time.monotonic() - t0

        raw = _normalize_tool_raw(raw)
        figures = [FigureRef.model_validate(f) for f in raw.get("figures", [])]
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
# Read-only inspection tools — reviewer / analyst-lead (in-process, no sandbox).
# ----------------------------------------------------------------------------


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
        human_handler: Any | None = None,
        asked_by: str = "agent",
        model: str = "",
        max_chars: int = 8000,
    ) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.retrieval = retrieval
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
        self.include_text_search = include_text_search
        self.max_chars = max_chars

    def tool_definitions(self) -> list[dict]:
        definitions = [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file under the permitted root directories.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": "List entries of a directory under the permitted root directories.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        ]
        if self.include_text_search:
            definitions.append(
                {
                    "name": "text_search",
                    "description": "Full-text search the local knowledge base; returns matching paper paths.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["query"],
                    },
                }
            )
        if self.web_source is not None:
            definitions.append(
                {
                    "name": "web_search",
                    "description": (
                        "Search the web for authoritative documentation (mission analysis "
                        "threads, calibration memos, official references). Use before "
                        "answering an uncertain science/code question rather than guessing."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                        "required": ["query"],
                    },
                }
            )
        if self.human_handler is not None:
            definitions.append(ask_human_tool_def())
        return definitions

    def _allowed(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == root or root in resolved.parents for root in self.roots)

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == ASK_HUMAN_TOOL:
            return await execute_ask_human(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        if name in ("read_file", "list_dir"):
            target = Path(str(arguments.get("path", "")))
            if not self._allowed(target):
                return ToolOutcome(text=f"path not permitted: {target}", is_error=True)
            try:
                if name == "read_file":
                    text = target.read_text(encoding="utf-8", errors="replace")[: self.max_chars]
                else:
                    text = json.dumps(sorted(p.name for p in target.iterdir()))
            except OSError as exc:
                return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)
            return ToolOutcome(text=text or "(empty)")

        if name == "text_search":
            from auto_research.retrieval import text_search_paths

            paths = await text_search_paths(
                str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 5)),
                backend=self.retrieval,
            )
            text = "\n".join(str(p) for p in paths) or "(no local matches)"
            return ToolOutcome(text=text)

        if name == "web_search":
            if self.web_source is None:
                return ToolOutcome(text="web_search not available", is_error=True)
            results = await self.web_source.search(
                str(arguments.get("query", "")), limit=int(arguments.get("limit", 5))
            )
            lines = [f"- {r.title} {r.url}\n  {(r.abstract or '')[:300]}" for r in results]
            return ToolOutcome(text="\n".join(lines)[: self.max_chars] or "(no results)")

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
                    "Search one external literature source for papers. Sources: "
                    + ", ".join(self.sources) + "."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "enum": list(self.sources)},
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["source", "query"],
                },
            },
            {
                "name": "text_search",
                "description": "Search the local knowledge base of already-ingested papers.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                },
            },
        ]

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == "search_literature":
            source = self.sources.get(str(arguments.get("source", "")))
            if source is None:
                return ToolOutcome(text=f"unknown source: {arguments.get('source')}", is_error=True)
            results = await source.search(
                str(arguments.get("query", "")), limit=int(arguments.get("limit", 10))
            )
            self.results.extend(results)
            lines = [f"- {r.title} ({r.source}) {r.url}\n  {(r.abstract or '')[:300]}" for r in results]
            return ToolOutcome(text="\n".join(lines)[: self.max_chars] or "(no results)")

        if name == "text_search":
            from auto_research.retrieval import text_search_paths

            paths = await text_search_paths(
                str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 5)),
                backend=self.retrieval,
            )
            return ToolOutcome(text="\n".join(str(p) for p in paths) or "(no local matches)")

        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)
