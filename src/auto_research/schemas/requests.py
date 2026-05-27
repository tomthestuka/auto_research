"""Top-level research request messages."""

from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]


class ResearchRequest(MessageModel):
    """Human request entering the reviewer-led process loop."""

    question: NonEmptyStr
    context: str | None = None
    constraints: list[str] = Field(default_factory=list)
