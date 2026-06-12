"""Validation tests for the step-1 inter-agent protocol."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    DocumentRef,
    FigureRef,
    GateReport,
    MergeFlowResult,
    ResearchRequest,
    ReviewVerdict,
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
        ReviewVerdict(accepted=True, rationale="Looks sound.", surprise="nope")


def test_review_verdict_flags_feedback_edges():
    verdict = ReviewVerdict(
        accepted=False,
        rationale="Literature is stale.",
        needs_more_literature=True,
        follow_up="Search papers from 2025 onward.",
    )

    assert verdict.needs_more_literature is True
    assert verdict.needs_analysis_rework is False


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
    report = AnalystReport(
        summary="Blocked: toolkit lacks retBB.",
        tool_request={
            "science_context": "Need retBB.",
            "expected_behavior": "add_retbb(indices).",
            "proposed_tool_name": "fit_retbb_hss",
            "target_domain": "xspec_utils",
            "disposition": "extend_existing",
            "validation_data": [{"mission": "IXPE", "obsid": "02008401"}],
        },
    )

    assert isinstance(report.tool_request, ToolSpec)
    assert AnalystReport(summary="No tool needed.").tool_request is None


def test_analyst_report_status_defaults_ok_and_accepts_terminal_states():
    assert AnalystReport(summary="done").status == "ok"
    assert AnalystReport(summary="gave up honestly", status="failed").status == "failed"
    assert AnalystReport(summary="blocked", status="needs_human").status == "needs_human"
    with pytest.raises(ValidationError):
        AnalystReport(summary="x", status="vibes")


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
