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
        description=(
            "The literature re-loop switch. The graph always runs an initial literature "
            "pass before the first verdict, so set this true only when the gathered papers "
            "leave a genuine literature gap that another librarian pass would close. Leave "
            "it false to advance to analysis — analysis is the automatic next step once "
            "literature is adequate, including when dispatching the analysts for the first time."
        ),
    )
    needs_analysis_rework: bool = Field(
        default=False,
        description=(
            "The analysis re-loop switch. After reviewing an analysis report, set this true "
            "to send follow_up to the analyst team for more data work or revision."
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
    cited_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Absolute paths of the local PDFs you opened via open_paper AND actually used "
            "as evidence for this verdict. These become the report's References — the "
            "report links to exactly these files, so list only papers you genuinely read."
        ),
    )
