"""Phase-2 tool-authoring flow messages: the gate report, the co-lead review, and the
``propose_and_pr`` result.

These types make the rework loop legible end-to-end (CLAUDE.md §9.3/§9.4/§9.9). The
deterministic gate emits a :class:`GateReport` (machine-readable, fed back to the author
on rejection); the provider-decorrelated co-lead emits a :class:`ToolReviewVerdict`; and
``merge_flow.propose_and_pr`` returns a :class:`MergeFlowResult`. In step 2 the gate and
review hooks are stubs that pass; steps 3/4 fill them in — but the protocol is typed now.
"""

from typing import Annotated, Literal

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]


class CheckResult(MessageModel):
    """One gate check result. ``detail`` is the machine-readable reason the author reads
    when a check fails (Layer A = generic gate, Layer B = the tool's own committed tests).
    """

    name: NonEmptyStr
    layer: Literal["A", "B"]
    passed: bool
    detail: str = ""


class GateReport(MessageModel):
    """The deterministic gate's verdict (CLAUDE.md §9.4) — the arbiter, not an LLM opinion.

    ``report_hash`` seeds the §9.9 atomic-commit message (`ToolSpec` id + gate-report
    hash). The step-2 stub returns ``passed=True`` with empty layers.

    ``findings`` are *non-failing* observations the mechanical gate records but cannot
    decide on its own — the literal-data-array / ebin landmine and other intent-laden
    code patterns (§9.4). They do **not** flip ``passed``; the provider-decorrelated
    co-lead reviewer (step 4) judges their intent. Keeping them off ``passed`` is what
    makes the gate high-precision: it auto-fails only the unambiguous, and defers the
    rest to the second, independent check.
    """

    passed: bool
    layer_a: list[CheckResult] = Field(default_factory=list)
    layer_b: list[CheckResult] = Field(default_factory=list)
    findings: list[CheckResult] = Field(default_factory=list)
    reason: str = ""
    report_hash: str = ""


class ToolReviewVerdict(MessageModel):
    """The provider-decorrelated co-lead's code-review verdict (CLAUDE.md §9.3 step 4).

    Distinct from the research :class:`~auto_research.schemas.review.ReviewerReport`: this
    is code review only. ``checks_adequate`` is the co-lead's most leveraged judgement —
    are the Layer-B checks the right ones and strict enough (§9.4). ``issues`` are the
    specific fixes fed back to the author on rejection. ``commit_summary`` is the one-line,
    imperative commit subject the co-lead drafts when approving — it becomes the §9.9 atomic
    commit message (``approve_edit`` falls back to the tool name if it is empty).
    """

    approved: bool
    rationale: NonEmptyStr
    issues: list[str] = Field(default_factory=list)
    checks_adequate: bool = True
    commit_summary: str = ""  # one-line imperative commit subject, set when approving (§9.9)


class MergeFlowResult(MessageModel):
    """Outcome of ``merge_flow.propose_and_pr`` (CLAUDE.md §9.3).

    ``status``:
    - ``committed`` — gate-pass ∧ co-lead review-pass; one atomic commit landed on
      ``branch`` (the commit itself is step 6; step 2 returns a placeholder ``commit_sha``).
    - ``failed`` — a rework cap was exhausted without a mergeable tool; the lead raises to
      the human for instructions (§9.7). **No commit, no fabrication.**
    - ``needs_human`` — the flow is blocked on a human clarification that is unanswered.
    """

    tool_spec_id: NonEmptyStr
    status: Literal["committed", "failed", "needs_human"]
    branch: str | None = None
    commit_sha: str | None = None
    gate_report: GateReport | None = None
    review: ToolReviewVerdict | None = None
    reason: str = ""
