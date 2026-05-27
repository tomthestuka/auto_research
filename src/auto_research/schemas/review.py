"""Reviewer verdict messages."""

from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]


class ReviewVerdict(MessageModel):
    """Reviewer decision after comparing analysis against literature."""

    accepted: bool
    rationale: NonEmptyStr
    needs_more_literature: bool = False
    needs_analysis_rework: bool = False
    follow_up: str | None = None
