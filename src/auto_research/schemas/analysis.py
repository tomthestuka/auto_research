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
    status: Literal["succeeded", "failed"] = Field(
        default="failed",
        description=(
            "Declare the outcome of YOUR work: 'succeeded' when you completed it, 'failed' when "
            "you could not. An honest 'failed' is a correct, penalty-free answer — never "
            "fabricate a result to force 'succeeded'. Say in the summary what blocked you and "
            "what a person would need to do about it."
        ),
    )
    """The terminal state, **declared by the agent** (F9, 2026-09-09).

    Two values, and the default is ``failed``: a report nobody filled in has not succeeded at
    anything, and the harness must never infer success from silence. Before F9 the values were
    ``ok``/``failed``/``needs_human`` with ``ok`` as the default, so an unwritten report defaulted
    to claiming completion.

    ``needs_human`` is gone. It was the third value and it did not earn its place: headless — the
    condition every benchmark batch has run under — `ask_human` is not even in a graph role's tool
    list, so the state was unreachable by the route it names, while [A20] shows the *label* being
    filed for a reason that was not a human block at all (the lead wanted a worker rerun, the only
    open form was a terminal, and the run recorded a human-request that never happened). A role
    that genuinely needs a person says so in ``summary``, which is where a reader looks anyway."""


class AnalysisTask(MessageModel):
    """Work item sent to analyst workers."""

    question: NonEmptyStr
    instructions: str | None = None
    literature: list[SearchResult] = Field(default_factory=list)
    previous_report: AnalystReport | None = None
