"""Tool-authoring propose→review→merge flow — Phase 2 stub.

Phase 1: raises NotImplementedError. Tool execution now happens inside analyst
consensus; callers surface unavailable tool-authoring requests through the same
tool-error feedback path.

Phase 2 implementation (deferred): analyst worker emits a tool spec →
Claude Agent SDK writes and tests the code → analyst lead reviews →
human approves merge on GitHub. See CLAUDE.md §8 and §13.
"""

from __future__ import annotations

from auto_research.schemas.tools import ToolCallRequest


async def propose_and_pr(
    request: ToolCallRequest,
    *,
    lead: object,  # AnalystLeadAgent — typed loosely to avoid circular import
    toolkit_repo: str = "tomthestuka/research_toolkit",
) -> str:
    """Propose a new tool and open a GitHub PR on research_toolkit.

    Phase 2 only — raises NotImplementedError in Phase 1.
    """
    raise NotImplementedError(
        "tool authoring deferred to Phase 2 — implement via Claude Agent SDK"
    )
