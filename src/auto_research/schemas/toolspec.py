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
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]

# Whether the author should extend an existing ``<domain>_utils`` package or scaffold a
# brand-new one. The proposer/lead decides this (not the weaker Sonnet coder, §6/§9.3).
Disposition = Literal["extend_existing", "new_package"]


class ValidationDataset(MessageModel):
    """A real (or marked-synthetic) input the tool's checks run against.

    ``synthetic=True`` flags a ``synth…`` ObsID generated as *data* under the mission
    ``obs_db_path`` (§9.5) — never as toolkit code; provenance then flags any science
    result computed on it.
    """

    mission: NonEmptyStr  # e.g. "IXPE", "NICER", "NuSTAR", or a source name for lit tools
    obsid: NonEmptyStr
    synthetic: bool = False


class ProposedCheck(MessageModel):
    """One *advisory* verification check from §9.4's open menu.

    ``kind`` is a free-text hint (e.g. "known-input", "metamorphic", "differential",
    or a novel family), **not** a closed enum — novel tools may need check families
    nobody listed. The author/lead may revise or drop a check the worker proposed.
    """

    kind: NonEmptyStr
    description: NonEmptyStr
    params: dict[str, Any] = Field(default_factory=dict)
    tolerance: float | None = None  # optional; lead-finalised


class ToolSpec(MessageModel):
    """An analyst's brief proposing a new (or extended) ``research_toolkit`` tool."""

    id: NonEmptyStr = Field(default_factory=lambda: f"toolspec-{uuid4().hex[:12]}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    science_context: NonEmptyStr  # what is being measured; why the toolkit can't do it
    expected_behavior: NonEmptyStr  # prose signature + I/O the tool should provide
    proposed_tool_name: NonEmptyStr  # the tool/function name to author

    # Where the two halves land. ``target_domain`` is an existing ``<domain>_utils``
    # package (extend) or the proposed new package name (new). ``disposition`` is the
    # proposer/lead's architectural call; the Sonnet author executes it.
    target_domain: NonEmptyStr
    disposition: Disposition

    # The core of verifiability: at least one real input the checks run against (§9.1).
    validation_data: list[ValidationDataset] = Field(min_length=1)

    # Advisory and revisable — the committed tests are authoritative (§9.4).
    proposed_checks: list[ProposedCheck] = Field(default_factory=list)

    # Tolerance for the human-half (``lib/``) ↔ ai-half (``skills/``) replication at the
    # human merge (§9.2/§9.9). Optional here — lead-finalised.
    replication_tolerance: float | None = None

    notes: str | None = None
