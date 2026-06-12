"""Analysis task and report messages."""

from typing import Annotated, Literal

from pydantic import Field

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef
from auto_research.schemas.search import SearchResult
from auto_research.schemas.toolspec import ToolSpec


NonEmptyStr = Annotated[str, Field(min_length=1)]


class AnalystReport(MessageModel):
    """Worker or lead analysis output."""

    summary: NonEmptyStr
    findings: list[str] = Field(default_factory=list)
    figures: list[FigureRef] = Field(default_factory=list)
    uncertainty: str | None = None
    # Phase 2: set when the worker hits a capability the toolkit lacks and proposes a
    # new/extended tool (CLAUDE.md §9.3). A brief the analyst lead routes to the author.
    tool_request: ToolSpec | None = None
    # Phase 2 (§9.6): a first-class terminal state so an honest failure (tool authoring
    # exhausted its rework caps, blocked on a human, etc.) is a *correct* outcome with no
    # quality penalty — never a reason to fabricate a result on the last retry.
    status: Literal["ok", "failed", "needs_human"] = "ok"


class AnalysisTask(MessageModel):
    """Work item sent to analyst workers."""

    question: NonEmptyStr
    instructions: str | None = None
    literature: list[SearchResult] = Field(default_factory=list)
    previous_report: AnalystReport | None = None
