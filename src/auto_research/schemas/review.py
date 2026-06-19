"""Reviewer verdict messages."""

from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]


class ReviewVerdict(MessageModel):
    """Reviewer decision after comparing analysis against literature.

    The boolean flags route work: set them to send ``follow_up`` to a team. They are
    the reviewer's dispatch mechanism — the graph hands ``follow_up`` verbatim to each
    flagged team as their task.
    """

    accepted: bool = Field(
        description=(
            "True when the analysis answers the request and agrees with the retrieved "
            "literature. False keeps the loop open for more literature or analysis work."
        )
    )
    rationale: NonEmptyStr = Field(
        description="The evidence-grounded basis for the decision."
    )
    needs_more_literature: bool = Field(
        default=False,
        description="Set true to send follow_up to the librarian team for a literature pass.",
    )
    needs_analysis_rework: bool = Field(
        default=False,
        description=(
            "Set true to send follow_up to the analyst team for data work or revision. "
            "Set this together with needs_more_literature on a fresh request to start both "
            "teams at once."
        ),
    )
    follow_up: str | None = Field(
        default=None,
        description=(
            "The dispatch text handed verbatim to the flagged team(s): the literature "
            "questions to answer and the analysis steps to run. For a capability the "
            "toolkit lacks, state what to measure and on which data in plain terms and "
            "leave the build choice to the analyst team."
        ),
    )
