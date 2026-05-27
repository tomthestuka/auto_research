"""Analysis task and report messages."""

from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef
from auto_research.schemas.search import SearchResult


NonEmptyStr = Annotated[str, Field(min_length=1)]


class AnalystReport(MessageModel):
    """Worker or lead analysis output."""

    summary: NonEmptyStr
    findings: list[str] = Field(default_factory=list)
    figures: list[FigureRef] = Field(default_factory=list)
    uncertainty: str | None = None


class AnalysisTask(MessageModel):
    """Work item sent to analyst workers."""

    question: NonEmptyStr
    instructions: str | None = None
    literature: list[SearchResult] = Field(default_factory=list)
    previous_report: AnalystReport | None = None
