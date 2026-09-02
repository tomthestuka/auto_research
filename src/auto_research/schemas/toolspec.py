"""Phase-2 tool-authoring brief (``ToolSpec``) and its sub-models.

A ``ToolSpec`` is a **brief, not a frozen contract** (CLAUDE.md §9.3): an analyst
worker emits it (on its :class:`AnalystReport`) when the toolkit lacks a capability
the analysis needs. It is deliberately small (~2–4K tokens) — it carries the science
context, the expected behaviour, a *real* validation dataset, a proposed disposition,
and *advisory* verification checks. The author (a Sonnet coding sub-agent the analyst
lead spawns) and the lead then refine it with full ``lib/`` sight; the **committed
tests, not this brief, are authoritative** (§9.4). Tolerances and the final check set
are lead-finalised, so they are optional here.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]

# Whether the author should extend an existing ``<domain>_utils`` package or scaffold a
# brand-new one. The proposer/lead decides this (not the weaker Sonnet coder, §6/§9.3).
Disposition = Literal["extend_existing", "new_package"]

# Why the brief is raised (CLAUDE.md §9.3 / L5):
# - ``new_capability`` — the toolkit lacks something the analysis needs.
# - ``fix_existing``   — an already-merged tool gave wrong/surprising output in use; the
#   analyst *reports* it (the lead later triages "wrongly-called" vs "genuinely buggy").
RequestKind = Literal["new_capability", "fix_existing"]


class ValidationDataset(MessageModel):
    """A real (or marked-synthetic) input the tool's checks run against.

    ``synthetic=True`` flags a ``synth…`` ObsID generated as *data* under the mission
    ``obs_db_path`` (§9.5) — never as toolkit code; provenance then flags any science
    result computed on it.
    """

    mission: NonEmptyStr = Field(
        description='The mission this input belongs to, e.g. "IXPE", "NICER", "NuSTAR" — or a '
                    "source name for a literature tool."
    )
    obsid: NonEmptyStr = Field(
        description="The observation identifier, exactly as it appears in the archive."
    )
    synthetic: bool = Field(
        default=False,
        description=(
            "True only for a marked synth… ObsID generated as DATA under the mission path — never "
            "for anything a toolkit function fabricates, which is banned outright."
        ),
    )


class ProposedCheck(MessageModel):
    """One *advisory* verification check from §9.4's open menu.

    ``kind`` is a free-text hint (e.g. "known-input", "metamorphic", "differential",
    or a novel family), **not** a closed enum — novel tools may need check families
    nobody listed. The author/lead may revise or drop a check the worker proposed.

    Every field is a closed scalar (no open map) so the enclosing ``ToolSpec`` — which
    rides inside the lead's ``AuthoringDecision`` response schema — stays valid under any
    provider's strict structured output (OpenAI rejects every open map, §7).
    """

    kind: NonEmptyStr = Field(
        description=(
            'Free-text family hint, e.g. "known-input", "metamorphic", "differential", '
            '"injection-recovery" — or a family nobody has listed, when the tool needs one.'
        )
    )
    description: NonEmptyStr = Field(
        description="What the check asserts, concretely enough for the coder to write it."
    )
    tolerance: float | None = Field(
        default=None, description="Optional numeric tolerance for this check."
    )


class AnalystToolRequest(MessageModel):
    """A worker → lead message raising a toolkit gap or defect (CLAUDE.md §9.3).

    This is what an analyst **worker** emits on its ``AnalystReport.tool_request`` — only
    what the worker is qualified to state: the science context, the behaviour it expects, a
    *suggested* tool name, and the real ObsID that exposed the gap. The architectural calls —
    ``disposition`` (extend vs. new package), the target ``<domain>_utils`` package, and the
    verification ``proposed_checks`` — are **not** here: §9.3 makes them the lead's call, and
    the worker is not the domain expert. The analyst **lead** reads these requests in
    ``decide_authoring`` and finalises a single :class:`ToolSpec` (adding disposition,
    target_domain, and checks) for the coder.

    Keeping the worker's message free of those fields also keeps the worker's ``AnalystReport``
    response schema free of any open map, so a GPT-backed worker can compose its report under
    strict structured output (§7).
    """

    request_kind: RequestKind = Field(
        default="new_capability",
        description=(
            "'new_capability' when the toolkit cannot do this at all; 'fix_existing' when a tool "
            "you called misbehaved. The lead decides the cause; you report which of the two you "
            "are raising."
        ),
    )

    science_context: NonEmptyStr = Field(
        description="What is being measured; why the toolkit can't do it."
    )
    expected_behavior: NonEmptyStr = Field(
        description="Prose signature + I/O the tool should provide."
    )
    proposed_tool_name: NonEmptyStr = Field(
        description="A suggested tool/function name (the lead may rename)."
    )

    observed_issue: str | None = Field(
        default=None,
        description="For 'fix_existing': the wrong/surprising behaviour observed.",
    )

    validation_data: list[ValidationDataset] = Field(
        min_length=1,
        description=(
            "At least one real ObsID the tool's checks run against — the input that exposed the "
            "gap becomes the regression input."
        ),
    )

    notes: str | None = Field(
        default=None,
        description="Optional: suggested check ideas the lead may adopt or drop.",
    )


class ToolSpec(MessageModel):
    """One link of the authoring chain — **the opening prompt of a coding session**.

    Not a classification of how novel a gap is. This object is handed straight to
    ``drive_authoring``, which seeds a coder session with it: what is written here is what an
    engineer reads before touching the toolkit. Write it as instructions to that engineer.

    Every field below carries a ``description``, and that is load-bearing rather than tidy: a
    ``#`` comment never reaches the JSON Schema the model is given, so a field explained only in
    a comment is a field the model must fill blind. A run died on exactly that (2026-09-01).
    """

    id: NonEmptyStr = Field(
        default_factory=lambda: f"toolspec-{uuid4().hex[:12]}",
        description="Set automatically; leave it alone.",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Set automatically; leave it alone.",
    )

    request_kind: RequestKind = Field(
        default="new_capability",
        description=(
            "'new_capability' when the toolkit cannot do this at all; 'fix_existing' when a tool "
            "that already exists misbehaves. Choose deliberately — this is not a default to "
            "accept: a capability that exists in lib/ but is not exposed as a tool is still "
            "'new_capability' with disposition 'extend_existing'."
        ),
    )

    science_context: NonEmptyStr = Field(
        description="What is being measured; why the toolkit can't do it."
    )
    expected_behavior: NonEmptyStr = Field(
        description=(
            "The brief itself, and the coder builds only what this says. Algorithm-level: the "
            "signature, the exact procedure, the edge cases, the tests to add. Write it as a "
            "general capability serving the whole class of question, not this run's instance."
        )
    )
    proposed_tool_name: NonEmptyStr = Field(
        description="The tool/function name to author (or fix)."
    )

    observed_issue: str | None = Field(
        default=None,
        description=(
            "For 'fix_existing': the wrong or surprising behaviour observed — the symptom only. "
            "The ObsID that exposed it goes in validation_data, where it becomes the regression "
            "input."
        ),
    )

    target_domain: NonEmptyStr = Field(
        description=(
            "Which <domain>_utils package the work lands in — an existing one when extending "
            "(e.g. 'xspec_utils'), or the name you propose for a new one. Yours to decide: read "
            "the relevant lib/ first."
        )
    )
    disposition: Disposition = Field(
        description=(
            "'extend_existing' when the capability already exists in lib/ and only needs "
            "surfacing as a skills/ tool, or when an existing package gains a function — prefer "
            "this. 'new_package' only when the capability is genuinely absent and no existing "
            "package is the right home. Your architectural call; the coder executes it."
        )
    )

    validation_data: list[ValidationDataset] = Field(
        min_length=1,
        description=(
            "At least one REAL input the tool's checks run against — this is what makes the tool "
            "verifiable rather than merely tested."
        ),
    )

    proposed_checks: list[ProposedCheck] = Field(
        default_factory=list,
        description=(
            "The must-have tests, which you own: the implementer must not be the sole author of "
            "its own grader. At minimum a capability test that fails if the tool produced only "
            "well-shaped metadata without doing its job, and an independent differential against "
            "already-trusted code rather than the tool's own new helper."
        ),
    )

    replication_tolerance: float | None = Field(
        default=None,
        description=(
            "Optional. Tolerance within which a human re-running the lib/ half must reproduce "
            "the tool's result at merge. Omit when no numeric tolerance applies."
        ),
    )

    notes: str | None = Field(
        default=None, description="Anything else the coder should know. Optional."
    )


class AuthoringDecision(MessageModel):
    """The authoring decision, **constructed by the harness from a filed form** (§9.3).

    No model fills this object any more. The lead files either ``author_chain`` (an ordered chain
    of :class:`ToolSpec`, at least one, enforced by the tool's own schema) or ``hold_authoring``
    (a reason), and the handler builds this from whichever arrived. The consumer — the consensus
    strategy — is unchanged.

    That indirection is the fix for a real failure. While this WAS a structured-output schema the
    model filled, ``tool_specs`` was optional in the JSON Schema it received, the rule making it
    mandatory lived in the validator below where the model could not see it, and no field carried
    a description at all. A lead that concluded "the capability exists in lib/ but is unexposed"
    had no way to say so: ``should_author=false`` was described only as the verdict for a
    mis-called request. It answered ``true`` with no specs — schema-valid — and the validator
    killed the run (2026-09-01, $19.43). The validator is kept below as an assertion on the
    harness's own construction; from a filed form the state it rejects is unreachable.

    Formerly returned by ``AnalystLeadAgent.decide_authoring`` after the lead browsed the full
    ``lib/`` source (read-only) and reads the analysis prescription for the gap(s) a worker
    raised. The lead — not the weaker Sonnet coder — owns the architectural call, so each
    ``ToolSpec`` it returns carries the finalised ``disposition``/``target_domain`` (extend an
    existing package vs scaffold a new one) and any tightened checks. Consensus
    (``DebateToConverge``), not the LLM, then issues the actual ``merge_flow.propose_and_pr``
    calls (CLAUDE.md §9.9: code issues git, never the model).

    ``tool_specs`` is the **ordered dependency chain** (acquire → reduce → analyse → plot): the
    lead plans the whole chain the analysis needs up front, and the strategy authors the tools
    **sequentially, one session at a time** — each coder sees the prior tool's committed code in
    the worktree — then reruns the workers **once**, after the whole chain (A8). A worker
    surfaces only the first gap it hits; the lead expands to the full chain from the prescription.
    """

    should_author: bool = Field(
        description=(
            "Harness-set: True when the lead filed author_chain, False when it filed "
            "hold_authoring."
        )
    )
    tool_specs: list[ToolSpec] = Field(
        default_factory=list,
        description="The ordered chain the lead filed; empty when it held.",
    )
    rationale: str = Field(
        default="", description="The lead's stated reasoning, carried through from the form."
    )

    @model_validator(mode="after")
    def _require_specs_when_authoring(self) -> "AuthoringDecision":
        """Authoring implies at least one brief — a ``should_author`` decision with an empty
        chain is a contradiction the consumer cannot act on (partially closes [I3])."""
        if self.should_author and not self.tool_specs:
            raise ValueError("should_author is True but tool_specs is empty")
        return self
