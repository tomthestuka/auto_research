"""Provider-decorrelated co-lead review — Phase 2 step 4 (CLAUDE.md §9.3/§9.4).

``make_review_hook`` builds the default ``ReviewHook`` that ``merge_flow.propose_and_pr``
drives the L3 rework loop against. It is the **second, independent** check on a merge
candidate: the deterministic gate (``gate.run_gate``) is the door; this review is the
pre-filter the door also requires (commit ⇔ gate-pass ∧ co-lead review-pass).

The review is routed *from config* to an ``analyst_colead`` member whose **provider differs
from the Claude author's** (``select_decorrelated_member``) — so a tool is never approved by
its own model family. The co-lead (GPT) reads the author's diff + the gate report and, via
read-only worktree/web tools, investigates the surrounding code/conventions before issuing a
:class:`~auto_research.schemas.merge.ToolReviewVerdict` (code review only; never the science
finding, never a merge).

This module owns the worktree IO (the diff bundle) + the decorrelation policy; the LLM call
itself lives in :class:`~auto_research.agents.roles.AnalystColeadAgent`. Heavy imports
(agents/sources) are deferred into the functions, mirroring ``merge_flow``, so importing the
sandbox package never pulls the agent stack.
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from auto_research.sandbox.gate import collect_targets
from auto_research.schemas import GateReport, ToolReviewVerdict, ToolSpec

if TYPE_CHECKING:  # runtime imports are deferred to avoid an import cycle
    from auto_research.models import ModelGroupHandle, ModelHandle

ReviewHook = Callable[[Path, ToolSpec, GateReport], Awaitable[ToolReviewVerdict]]

# Per-file ceiling on the diff bundle so a large authored file cannot blow the co-lead's
# context; the rest is elided with a marker (the reviewer can read_file the full text).
_DIFF_FILE_CAP = 20_000


# ----------------------------------------------------------------------------
# Decorrelation policy (the trust boundary — author.provider ≠ reviewer.provider)
# ----------------------------------------------------------------------------


def select_decorrelated_member(group: "ModelGroupHandle", author_provider: str) -> "ModelHandle":
    """Pick the ``analyst_colead`` member whose provider differs from the author's (§9.3).

    The author is the Claude SDK, so the reviewer must not be Anthropic — two independent
    checks, not one provider checking its own family. Raises ``ValueError`` when no eligible
    member exists: a co-lead that shares the author's provider is a broken trust boundary and
    must fail loudly, not silently let Claude review Claude.
    """
    for handle in group.handles:
        if handle.config.provider != author_provider:
            return handle
    raise ValueError(
        f"analyst_colead has no member with provider != author provider {author_provider!r}; "
        "the co-lead review must be provider-decorrelated from the author (CLAUDE.md §9.3). "
        f"Fix: add a member whose provider is not {author_provider!r} to the analyst_colead role "
        "in config/models.yaml (e.g. a GPT member when the author/coder is Anthropic)."
    )


def check_author_reviewer_decorrelation() -> None:
    """Launch-time preflight: assert the tool author and co-lead reviewer are provider-
    decorrelated (CLAUDE.md §9.3). A pure config check — registered in
    ``auto_research.preflight`` and run before any LLM is constructed, so a broken trust
    boundary fails cheaply rather than after the author has burned tokens. Resolving the
    roles also validates they are present and well-formed (registry ``REQUIRED_ROLES``).
    """
    from auto_research.models import resolve

    author_provider = resolve("coder").handles[0].config.provider
    select_decorrelated_member(resolve("analyst_colead"), author_provider)  # raises if none


# ----------------------------------------------------------------------------
# Diff bundle — exactly the *_utils source the author added/changed in the worktree
# ----------------------------------------------------------------------------


def _git(worktree: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, check=True
    )
    return out.stdout


def _truncate(text: str) -> str:
    if len(text) <= _DIFF_FILE_CAP:
        return text
    return text[:_DIFF_FILE_CAP] + f"\n… [truncated — {len(text):,} chars total; use read_file]"


def build_diff_bundle(worktree: Path) -> str:
    """Render the new/changed ``*_utils`` files as a single review artifact.

    Reuses ``gate.collect_targets`` for the file set (every changed file under a ``*_utils``
    package — ``.py`` and other text such as shell scripts): a new (untracked) file is shown
    in full; a modified file is shown as its unified ``git diff``. Mirrors the gate's view, so
    the co-lead judges exactly the lines the gate scanned.
    """
    worktree = Path(worktree)
    targets = collect_targets(worktree)
    if not targets:
        return "(no toolkit changes detected in the worktree)"

    parts: list[str] = []
    for path in sorted(targets, key=lambda p: str(p)):
        rel = str(path.relative_to(worktree))
        added = targets[path]
        if added is None:  # new / untracked file → full content
            try:
                body = path.read_text(encoding="utf-8")
            except OSError as exc:
                body = f"<unreadable: {exc}>"
            lang = "python" if rel.endswith(".py") else ("bash" if rel.endswith(".sh") else "")
            parts.append(f"### NEW FILE: {rel}\n```{lang}\n{_truncate(body)}\n```")
        else:  # modified tracked file → unified diff
            diff = _git(worktree, "diff", "--unified=3", "--", rel).strip()
            parts.append(f"### MODIFIED: {rel}\n```diff\n{_truncate(diff)}\n```")
    return "\n\n".join(parts)


# ----------------------------------------------------------------------------
# The default ReviewHook
# ----------------------------------------------------------------------------


def make_review_hook(
    colead: Any | None = None,
    *,
    author_provider: str,
    web_source: Any | None = None,
    monitor: Any | None = None,
) -> ReviewHook:
    """Build the default ``run_review`` hook for ``merge_flow.propose_and_pr`` (§9.3 step 4).

    ``colead`` — an :class:`~auto_research.agents.roles.AnalystColeadAgent`; when ``None`` one
    is built from config, routed to a member decorrelated from ``author_provider``. An
    injected co-lead is still asserted to be decorrelated (the trust boundary holds either
    way). ``web_source`` backs the co-lead's optional ``web_search`` tool; when ``None`` a
    default :class:`~auto_research.sources.WebSource` is attempted (best-effort).
    """

    async def run_review(worktree: Path, tool_spec: ToolSpec, gate: GateReport) -> ToolReviewVerdict:
        from auto_research.agents import AnalystColeadAgent
        from auto_research.agents.react import ReadOnlyToolProvider

        agent = colead
        if agent is None:
            from auto_research.models import resolve

            handle = select_decorrelated_member(resolve("analyst_colead"), author_provider)
            agent = AnalystColeadAgent(model=handle, monitor=monitor)
        else:
            prov = getattr(getattr(agent, "model", None), "config", None)
            prov = getattr(prov, "provider", None)
            if prov is not None and prov == author_provider:
                raise ValueError(
                    "injected co-lead shares the author's provider "
                    f"{author_provider!r}; the review must be provider-decorrelated (§9.3)."
                )

        tools = ReadOnlyToolProvider(
            roots=[Path(worktree)],
            web_source=_resolve_web_source(web_source),
            include_text_search=False,  # the papers KB is irrelevant to code review
        )
        diff = build_diff_bundle(Path(worktree))
        return await agent.review_tool(tool_spec, diff=diff, gate=gate, tools=tools)

    return run_review


def _resolve_web_source(web_source: Any | None) -> Any | None:
    if web_source is not None:
        return web_source
    try:
        from auto_research.sources import WebSource

        return WebSource()
    except Exception:  # noqa: BLE001 — web search is a best-effort aid, never required
        return None
