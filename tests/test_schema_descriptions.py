"""Every field a model must fill carries a description the model can actually read.

This is the guard for the defect that cost a run. `schemas/toolspec.py` explained each of its
fields in a `#` comment — `target_domain`, `disposition`, `request_kind`, all of them — and a
comment never reaches `model_json_schema()`. So the lead was handed thirteen field names with no
guidance, a `tool_specs` marked optional, and a rule that made it mandatory living in a validator
it could not see. It answered schema-validly and the validator killed the run (2026-09-01, $19.43).

The rule this pins: **if a model fills it, it is described in the schema.** Not in a comment, not
in a payload paragraph, not in the system prompt — in the JSON Schema, which is the one channel
that travels with the request whatever the caller does.

Fields the *harness* fills are exempt and named below, because describing them to a model would be
describing something it never sees.
"""

from __future__ import annotations

import pytest

from auto_research.schemas import AnalystReport, AnalysisTask, ResearchRequest, ReviewVerdict
from auto_research.schemas.figures import FigureCitation
from auto_research.schemas.toolspec import (
    AnalystToolRequest,
    AuthoringDecision,
    ProposedCheck,
    ToolSpec,
    ValidationDataset,
)

#: Nested objects the RUNTIME builds, not the model — exempt, because describing them would be
#: describing something no model ever sees. ``FigureRef`` is the case: a model cites a figure by
#: its catalog handle (:class:`FigureCitation`, which IS model-filled and IS described), and the
#: runtime resolves that handle into a ``FigureRef`` with a real path, sha256 and mime type. A
#: model never writes one.
RUNTIME_RESOLVED = frozenset({"FigureRef"})

#: Schemas a model fills, directly or as a nested object inside one it fills.
MODEL_FILLED = [
    AnalystReport,
    AnalystToolRequest,
    AuthoringDecision,
    FigureCitation,
    ProposedCheck,
    ReviewVerdict,
    ToolSpec,
    ValidationDataset,
]


@pytest.mark.parametrize("model", MODEL_FILLED, ids=lambda m: m.__name__)
def test_every_field_carries_a_description(model) -> None:
    schema = model.model_json_schema()
    undescribed = sorted(
        name for name, spec in (schema.get("properties") or {}).items()
        if not str(spec.get("description") or "").strip()
    )
    assert not undescribed, (
        f"{model.__name__} has field(s) the model must fill with no description in the JSON "
        f"Schema: {undescribed}. A `#` comment does not reach the model — put it in "
        f"Field(description=...). This is the defect that killed a run on 2026-09-01."
    )


@pytest.mark.parametrize("model", MODEL_FILLED, ids=lambda m: m.__name__)
def test_nested_definitions_are_described_too(model) -> None:
    """A nested object is filled by the same model and is just as blind without descriptions."""
    schema = model.model_json_schema()
    undescribed: list[str] = []
    for defname, spec in (schema.get("$defs") or {}).items():
        if defname in RUNTIME_RESOLVED:
            continue
        for name, prop in (spec.get("properties") or {}).items():
            if not str(prop.get("description") or "").strip():
                undescribed.append(f"{defname}.{name}")
    assert not undescribed, f"{model.__name__} nested field(s) undescribed: {sorted(undescribed)}"


def test_the_field_that_was_required_and_never_mentioned_is_described():
    """`target_domain` is required by ToolSpec and appeared in no channel the lead could read —
    not the payload, not the system prompt, not the schema. It is the sharpest single instance."""
    described = ToolSpec.model_json_schema()["properties"]["target_domain"]["description"]
    assert "domain" in described.lower() and len(described) > 40


def test_the_disposition_field_states_both_branches():
    """The launch-3 failure was a rule given in one direction only. `disposition` is where the
    exists-but-unexposed case has to be sayable, and the probe confirmed every vendor picks it
    correctly once it is described (2026-09-02, 5/5)."""
    described = ToolSpec.model_json_schema()["properties"]["disposition"]["description"]
    assert "extend_existing" in described and "new_package" in described
    assert "surfac" in described  # the case that had no home before


def test_a_task_the_model_reads_is_not_covered_by_this_rule():
    """Sanity: this guard is about fields a model FILLS. A task it merely reads is out of scope,
    so the list above is deliberately short rather than every schema in the package."""
    assert AnalysisTask not in MODEL_FILLED
    assert ResearchRequest not in MODEL_FILLED
