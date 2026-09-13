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
    rework: bool = Field(
        default=False,
        description=(
            "Set true to send the analysts back for one more round instead of concluding. "
            "When true, say why in rationale and what to look at in what_to_probe; when false, "
            "this report is the analysis result and needs summary, findings and status."
        ),
    )
    """Lead-only. The analysis lead's decision rides on its report rather than on a separate
    form: the report IS the decision, exactly as it is for an analyst (A3)."""
    rationale: str | None = Field(
        default=None,
        description=(
            "Why another round is warranted — the evidence gap or disagreement that one more "
            "pass would close. Required when rework is true."
        ),
    )
    what_to_probe: str | None = Field(
        default=None,
        description=(
            "What the analysts should do next, in their own terms. This text reaches every "
            "worker verbatim as their next instruction. Required when rework is true."
        ),
    )
    status: Literal["succeeded", "failed", "needs_human"] = Field(
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

    ``needs_human`` is **not offered to a worker**, and that is enforced by the tool surface rather
    than by the type (A3). It was removed from this schema entirely in F9 because headless —
    the condition every benchmark batch has run under — `ask_human` is not in a graph role's tool
    list, so the state was unreachable by the route it names, while [A20] shows the *label* being
    filed for a reason that was not a human block at all (the lead wanted a worker rerun, the only
    open form was a terminal, and the run recorded a human-request that never happened). A worker
    that genuinely needs a person says so in ``summary``, which is where a reader looks anyway.

    It returns here for the **lead**, whose terminal is a cycle decision rather than a worker's
    outcome, and whose report the reviewer reads as such. One class serves both roles and
    ``edit_report`` declares only the values that role may file, so a worker still cannot express
    it — the same belt-and-suspenders shape as ``author_chain``'s required first link: the surface
    makes it ungeneratable, and the handler validates anyway."""


class AnalysisTask(MessageModel):
    """Work item sent to analyst workers."""

    question: NonEmptyStr
    instructions: str | None = None
    literature: list[SearchResult] = Field(default_factory=list)
    previous_report: AnalystReport | None = None
