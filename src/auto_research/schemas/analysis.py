"""Analysis task and report messages."""

from typing import Annotated, Literal

from pydantic import Field

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FiguredReport
from auto_research.schemas.search import SearchResult
from auto_research.schemas.toolspec import AnalystToolRequest


NonEmptyStr = Annotated[str, Field(min_length=1)]


class AnalystReport(FiguredReport):
    """Worker or lead analysis output.

    Extends :class:`FiguredReport`, so it carries ``cited_figures`` (the catalog handles the
    author selects) and the runtime-resolved, ordered ``figures`` (Figure 1 = ``figures[0]``);
    the prose refers to figures as "Figure 1", "Figure 2", … in ``cited_figures`` order (V-4).
    """

    summary: NonEmptyStr = Field(
        description=(
            "The report's own abstract: what was done and what it found, in prose. Refer to "
            'figures as "Figure 1", "Figure 2", … in cited_figures order.'
        )
    )
    findings: list[str] = Field(
        default_factory=list,
        description="Key findings, each a self-contained prose string.",
    )
    uncertainty: str | None = Field(
        default=None, description="The dominant uncertainties / caveats (optional)."
    )
    tool_request: AnalystToolRequest | None = Field(
        default=None,
        description=(
            "Raise ONLY when the toolkit lacks a capability the task needs, or an existing tool "
            "misbehaves; otherwise omit. The analyst lead finalises the tool brief from this."
        ),
    )
    status: Literal["ok", "failed", "needs_human"] = Field(
        default="ok",
        description=(
            "'ok' when you completed the work; 'failed' / 'needs_human' are honest, penalty-free "
            "outcomes — never fabricate a result to force 'ok'."
        ),
    )


class AnalysisTask(MessageModel):
    """Work item sent to analyst workers."""

    question: NonEmptyStr
    instructions: str | None = None
    literature: list[SearchResult] = Field(default_factory=list)
    previous_report: AnalystReport | None = None
