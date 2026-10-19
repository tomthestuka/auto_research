"""Validation tests for the step-1 inter-agent protocol."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    AnalystToolRequest,
    DocumentRef,
    FigureRef,
    GateReport,
    MergeFlowResult,
    ResearchRequest,
    ReviewerReport,
    SearchRequest,
    SearchResult,
    ToolCallRequest,
    ToolCallReview,
    ToolReviewVerdict,
    ToolSpec,
    document_from_file,
)


def test_research_request_rejects_empty_question():
    with pytest.raises(ValidationError):
        ResearchRequest(question="")


def test_search_request_bounds_and_source_values():
    request = SearchRequest(query="black hole spin", sources=["arxiv"], max_results=25)

    assert request.sources == ["arxiv"]
    assert request.max_results == 25

    with pytest.raises(ValidationError):
        SearchRequest(query="black hole spin", sources=["unknown"])

    with pytest.raises(ValidationError):
        SearchRequest(query="black hole spin", max_results=0)


def test_search_result_serializes_nested_types_to_json():
    result = SearchResult(
        source="nasa_ads",
        title="A useful paper",
        url="https://example.org/paper",
        published=date(2026, 5, 27),
        identifiers={"bibcode": "2026Example"},
    )

    dumped = result.model_dump(mode="json")

    assert dumped["url"] == "https://example.org/paper"
    assert dumped["published"] == "2026-05-27"


def test_analysis_task_accepts_nested_literature_results():
    task = AnalysisTask(
        question="What changed?",
        literature=[
            {
                "source": "web",
                "title": "Field update",
                "url": "https://example.org/update",
            }
        ],
    )

    assert isinstance(task.literature[0], SearchResult)


def test_figure_ref_is_json_serializable_metadata_only():
    figure = FigureRef(
        path=Path("/tmp/run/artifacts/figure.png"),
        caption="Residual plot",
        mime_type="image/png",
        produced_by="xspec_utils",
        created_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
        sha256="a" * 64,
    )
    report = AnalystReport(summary="Fit improved.", figures=[figure])

    dumped = report.model_dump(mode="json")

    assert dumped["figures"][0]["path"] == "/tmp/run/artifacts/figure.png"
    assert "thumbnail_b64" in dumped["figures"][0]
    assert set(dumped["figures"][0]) == {
        "path",
        "caption",
        "mime_type",
        "produced_by",
        "created_at",
        "sha256",
        "thumbnail_b64",
    }


def test_figure_ref_rejects_bad_hash():
    with pytest.raises(ValidationError):
        FigureRef(
            path="/tmp/run/artifacts/figure.png",
            caption="Residual plot",
            mime_type="image/png",
            produced_by="xspec_utils",
            created_at=datetime(2026, 5, 27, 12, 0, tzinfo=UTC),
            sha256="not-a-sha",
        )


def test_document_ref_is_attachment_metadata_only(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.5\n")

    document = document_from_file(pdf, title="Paper")
    dumped = document.model_dump(mode="json")

    assert isinstance(document, DocumentRef)
    assert dumped["path"] == str(pdf)
    assert dumped["title"] == "Paper"
    assert dumped["mime_type"] == "application/pdf"
    assert len(dumped["sha256"]) == 64


def test_protocol_rejects_extra_fields():
    with pytest.raises(ValidationError):
        ReviewerReport(accepted=True, rationale="Looks sound.", surprise="nope")


def test_review_verdict_names_who_receives_it():
    verdict = ReviewerReport(
        accepted=False,
        rationale="Literature is stale.",
        assign_to="librarian",
        follow_up="Search papers from 2025 onward.",
    )

    assert verdict.assign_to == "librarian"
    assert ReviewerReport().assign_to is None
    with pytest.raises(ValidationError):
        ReviewerReport(rationale="r", assign_to="lead")


def test_tool_call_request_is_report_independent():
    request = ToolCallRequest(
        requested_by="analyst_worker",
        kind="bash",
        purpose="List candidate observation files.",
        command="find . -maxdepth 2 -type f",
        working_dir="/tmp",
    )
    review = ToolCallReview(approved=False, rationale="Needs a confined working directory.")

    assert request.kind == "bash"
    assert request.network_required is False
    assert review.approved is False


def test_tool_spec_round_trips_and_auto_ids():
    spec = ToolSpec(
        science_context="Need a returning-radiation disk model; xspec_utils lacks retBB.",
        expected_behavior="add_retbb(indices) -> sets retBB component; returns fit handle.",
        proposed_tool_name="fit_retbb_hss",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
        proposed_checks=[
            {"kind": "metamorphic", "description": "Load-order independent", "tolerance": 0.01},
            {"kind": "sanity-bound", "description": "pol <= 1; chi2/dof finite"},
        ],
    )

    dumped = spec.model_dump(mode="json")
    restored = ToolSpec.model_validate(dumped)

    assert restored == spec
    assert restored.id.startswith("toolspec-")  # auto-generated when omitted
    assert restored.disposition == "extend_existing"
    assert restored.validation_data[0].synthetic is False
    assert restored.replication_tolerance is None  # optional, lead-finalised


def test_authoring_decision_round_trips():
    from auto_research.schemas import AuthoringDecision

    spec = ToolSpec(
        science_context="need a PCUBE polarization tool",
        expected_behavior="run xpbin PCUBE and return the Stokes table",
        proposed_tool_name="pcube",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[{"mission": "IXPE", "obsid": "01002901"}],
    )
    spec2 = spec.model_copy(update={"proposed_tool_name": "pcube_plot"})
    # A8: an ordered dependency chain (data tool → companion plotter), not a single tool.
    decision = AuthoringDecision(
        should_author=True, tool_specs=[spec, spec2], rationale="exists in lib/, surface it"
    )
    restored = AuthoringDecision.model_validate(decision.model_dump(mode="json"))
    assert restored.should_author is True
    assert [s.proposed_tool_name for s in restored.tool_specs] == ["pcube", "pcube_plot"]

    # Declining to author needs no tool_specs.
    no = AuthoringDecision(should_author=False, rationale="worker mis-called an existing tool")
    assert no.tool_specs == []

    # The validator rejects an authoring decision with no briefs (partially closes [I3]).
    with pytest.raises(ValidationError):
        AuthoringDecision(should_author=True, tool_specs=[], rationale="contradiction")


def test_tool_spec_requires_a_validation_dataset():
    with pytest.raises(ValidationError):
        ToolSpec(
            science_context="x",
            expected_behavior="y",
            proposed_tool_name="z",
            target_domain="xspec_utils",
            disposition="new_package",
            validation_data=[],
        )


def test_tool_spec_rejects_unknown_disposition():
    with pytest.raises(ValidationError):
        ToolSpec(
            science_context="x",
            expected_behavior="y",
            proposed_tool_name="z",
            target_domain="xspec_utils",
            disposition="rewrite_everything",
            validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
        )


def test_analyst_report_carries_tool_request():
    # The worker raises an AnalystToolRequest — only what it can state. The architectural
    # fields (target_domain/disposition/checks) are the lead's call and are NOT here.
    report = AnalystReport(
        summary="Blocked: toolkit lacks retBB.",
        tool_request={
            "science_context": "Need retBB.",
            "expected_behavior": "add_retbb(indices).",
            "proposed_tool_name": "fit_retbb_hss",
            "validation_data": [{"mission": "IXPE", "obsid": "02008401"}],
        },
    )

    assert isinstance(report.tool_request, AnalystToolRequest)
    assert AnalystReport(summary="No tool needed.").tool_request is None


def test_analyst_tool_request_round_trips_and_requires_validation_data():
    req = AnalystToolRequest(
        request_kind="fix_existing",
        science_context="add_retbb mis-tied the gain across data groups.",
        expected_behavior="add_retbb(indices) should leave gain untied unless asked.",
        proposed_tool_name="fit_retbb_hss",
        observed_issue="On obsid 02008401 the gain came back tied; chi2/dof inflated.",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
        notes="maybe a metamorphic load-order check would catch this.",
    )
    restored = AnalystToolRequest.model_validate(req.model_dump(mode="json"))
    assert restored == req
    assert restored.request_kind == "fix_existing"

    # default path stays new_capability; validation_data is required (>= 1).
    nc = AnalystToolRequest(
        science_context="x",
        expected_behavior="y",
        proposed_tool_name="z",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
    )
    assert nc.request_kind == "new_capability"
    assert nc.observed_issue is None
    with pytest.raises(ValidationError):
        AnalystToolRequest(
            science_context="x",
            expected_behavior="y",
            proposed_tool_name="z",
            validation_data=[],
        )


def test_analyst_report_strict_schema_has_no_open_maps():
    """Regression for the live GPT-worker 400: the AnalystReport response schema (which
    nests the worker's tool_request) must contain no open map — OpenAI strict rejects any
    object whose additionalProperties is not exactly false."""
    from auto_research.models.providers.openai import _strict_json_schema_format

    schema = _strict_json_schema_format(AnalystReport)

    offenders: list[str] = []

    def _walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                ap = node.get("additionalProperties", None)
                if ap is not False:  # truthy bool or a sub-schema => open map
                    offenders.append(f"{path}: additionalProperties={ap!r}")
            for key, value in node.items():
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")

    _walk(schema, "AnalystReport")
    assert not offenders, "open maps in AnalystReport strict schema:\n" + "\n".join(offenders)


def test_analyst_report_status_is_agent_declared_and_defaults_to_failed():
    assert AnalystReport(summary="gave up honestly", status="failed").status == "failed"
    assert AnalystReport(summary="did the work", status="succeeded").status == "succeeded"
    # F9 (2026-09-09): agent-declared, defaulting to `failed` — silence is never success.
    assert AnalystReport(summary="blocked", status="failed").status == "failed"
    assert AnalystReport(summary="unset").status == "failed", "silence is never success"
    with pytest.raises(ValidationError):
        AnalystReport(summary="done", status="ok")
    with pytest.raises(ValidationError):
        AnalystReport(summary="x", status="vibes")


def test_needs_human_is_barred_by_the_workers_surface_not_by_the_type():
    """A3: one report class serves both roles, so the TYPE admits the lead's third status value.

    F9 removed `needs_human` from this schema because a worker could reach it and did — filed for
    something that was not a human block at all ([A20]) — and headless the route it names does not
    exist. That reasoning is unchanged; what changed is where it is enforced. The lead's terminal
    is a cycle decision rather than a worker's outcome and legitimately carries the value, so the
    bar moved from the type to the surface: `edit_report` declares only the values that role may
    file, and a worker's declaration does not include this one.

    This is the belt-and-suspenders shape used elsewhere (`author_chain`'s required first link):
    the surface makes it ungeneratable, and the handler validates anyway.
    """
    from auto_research.agents.react import (
        LEAD_REPORT_FIELDS,
        LEAD_STATUS_VALUES,
        WORKER_REPORT_FIELDS,
        WORKER_STATUS_VALUES,
        edit_report_tool_def,
    )

    assert AnalystReport(summary="lead stopped", status="needs_human").status == "needs_human"

    worker = edit_report_tool_def(WORKER_REPORT_FIELDS, WORKER_STATUS_VALUES)
    lead = edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES)
    # A7: the surface is the draft document's layout, named in the tool description.
    assert "# status — the outcome, one word: succeeded or failed\n" in worker["description"]
    assert "succeeded or failed or needs_human" in lead["description"]
    # ...and the two surfaces are disjoint where they must be: a worker is never offered the
    # lead's decision fields, and the lead is never offered the worker's request-a-tool field.
    for block in ("# rework", "# rationale", "# what_to_probe"):
        assert block not in worker["description"] and block in lead["description"]
    assert "# tool_request" not in lead["description"]


def test_every_declared_report_property_is_a_real_field_on_the_model():
    """The anti-drift check. `edit_report`'s schema is hand-written prose, so a property that
    no longer matches a model field is a field the model can fill and the runtime silently drops —
    which is how the lead once received a chain schema with no field descriptions at all."""
    from auto_research.agents.react import (
        LEAD_REPORT_FIELDS,
        LEAD_STATUS_VALUES,
        WORKER_REPORT_FIELDS,
        WORKER_STATUS_VALUES,
        edit_report_tool_def,
    )

    model_fields = set(AnalystReport.model_fields)
    for fields, statuses in (
        (WORKER_REPORT_FIELDS, WORKER_STATUS_VALUES),
        (LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES),
    ):
        # A7: the surface is the draft document; every block names a real field on the model,
        # and every field the surface names has a block.
        from auto_research.agents.react import report_blocks

        blocks = report_blocks(fields)
        assert {b.field for b in blocks} <= model_fields
        assert {b.field for b in blocks} == set(fields) | {"cited_figures"}
        description = edit_report_tool_def(fields, statuses)["description"]
        assert all(f"# {b.name} —" in description for b in blocks)
    # The status enums are exactly the model's own Literal, split by role.
    from typing import get_args
    literal = set(get_args(AnalystReport.model_fields["status"].annotation))
    assert set(LEAD_STATUS_VALUES) == literal
    assert set(WORKER_STATUS_VALUES) < literal


def test_tool_spec_fix_existing_round_trips():
    spec = ToolSpec(
        request_kind="fix_existing",
        science_context="add_retbb mis-tied the gain across data groups.",
        expected_behavior="add_retbb(indices) should leave gain untied unless asked.",
        proposed_tool_name="fit_retbb_hss",
        target_domain="xspec_utils",
        disposition="extend_existing",
        observed_issue="On obsid 02008401 the gain came back tied; chi2/dof inflated.",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
    )

    restored = ToolSpec.model_validate(spec.model_dump(mode="json"))
    assert restored == spec
    assert restored.request_kind == "fix_existing"
    assert restored.observed_issue is not None
    # default path stays new_capability with no observed_issue
    nc = ToolSpec(
        science_context="x",
        expected_behavior="y",
        proposed_tool_name="z",
        target_domain="xspec_utils",
        disposition="new_package",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
    )
    assert nc.request_kind == "new_capability"
    assert nc.observed_issue is None


def test_tool_spec_rejects_unknown_request_kind():
    with pytest.raises(ValidationError):
        ToolSpec(
            request_kind="please_fix",
            science_context="x",
            expected_behavior="y",
            proposed_tool_name="z",
            target_domain="xspec_utils",
            disposition="extend_existing",
            validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
        )


def test_gate_report_round_trips_with_check_results():
    report = GateReport(
        passed=False,
        layer_a=[{"name": "no_fabrication", "layer": "A", "passed": True}],
        layer_b=[
            {
                "name": "injection_recovery",
                "layer": "B",
                "passed": False,
                "detail": "recovered pol 0.07 > tol 0.02",
            }
        ],
        reason="Layer-B injection-recovery failed.",
        report_hash="deadbeef",
    )

    restored = GateReport.model_validate(report.model_dump(mode="json"))
    assert restored == report
    assert restored.passed is False
    assert restored.layer_b[0].detail.startswith("recovered pol")
    # check layer is constrained to A/B
    with pytest.raises(ValidationError):
        GateReport(passed=True, layer_a=[{"name": "x", "layer": "C", "passed": True}])


def test_tool_review_verdict_defaults_and_validation():
    verdict = ToolReviewVerdict(approved=False, rationale="metamorphic check too loose")
    assert verdict.issues == []
    assert verdict.checks_adequate is True
    with pytest.raises(ValidationError):
        ToolReviewVerdict(approved=True, rationale="")  # rationale must be non-empty


def test_merge_flow_result_round_trips_each_status():
    committed = MergeFlowResult(
        tool_spec_id="toolspec-abc123",
        status="committed",
        branch="tool/fit_retbb_hss",
        commit_sha="placeholder",
    )
    restored = MergeFlowResult.model_validate(committed.model_dump(mode="json"))
    assert restored == committed

    failed = MergeFlowResult(
        tool_spec_id="toolspec-abc123",
        status="failed",
        reason="gate rework cap exhausted on injection-recovery",
    )
    assert failed.branch is None and failed.commit_sha is None

    with pytest.raises(ValidationError):
        MergeFlowResult(tool_spec_id="toolspec-abc123", status="merged_to_master")
