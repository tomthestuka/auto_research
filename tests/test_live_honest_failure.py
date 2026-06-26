"""Opt-in live checks for the honest-authoring-failure edges (handoff #4, §9.6).

Two real-LLM, observational probes at the new edges introduced by the reviewer-as-sole-I/O
change. They feed the **exact payloads the production edges carry** and log the model output
for human inspection (LLM content is not asserted — only the structural contract):

  1. Analyst lead at a non-committed authoring loop → ``finalize_authoring_outcome``: what
     terminal report does the lead compose when ``propose_and_pr`` returns ``status="failed"``?
  2. Reviewer at a failed analysis → ``review``: how does the reviewer adjudicate a
     ``status="failed"`` ``AnalystReport`` (finalize-and-document vs. bounded rework)?

Run with:
    AUTO_RESEARCH_LIVE_HONEST_FAILURE_TESTS=1 pytest tests/test_live_honest_failure.py -s
"""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.agents import AnalystLeadAgent, ReviewerAgent
from auto_research.models import load_registry
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    AnalystToolRequest,
    MergeFlowResult,
    ProposedCheck,
    ResearchRequest,
    ReviewVerdict,
    ToolReviewVerdict,
    ToolSpec,
    ValidationDataset,
)
from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_HONEST_FAILURE_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_HONEST_FAILURE_TESTS=1 to spend provider tokens",
)


# --- Fixtures mirroring the real edge content -------------------------------------------------

def _task() -> AnalysisTask:
    return AnalysisTask(
        question=(
            "Measure the intra-observation stability of the X-ray polarization angle (PA) and "
            "degree (PD) for IXPE ObsID 02008401, splitting the exposure into equal time segments."
        ),
        instructions=(
            "The data is local. Use the toolkit's IXPE polarization tools; report PA/PD per time "
            "segment with uncertainties."
        ),
    )


def _worker_reports() -> list[AnalystReport]:
    request = AnalystToolRequest(
        request_kind="new_capability",
        science_context=(
            "Intra-observation PA/PD stability needs a time-segmented IXPE polarization-cube "
            "measurement; the toolkit only exposes a whole-observation polarization cube."
        ),
        expected_behavior=(
            "Compute the IXPE polarization cube over N equal-exposure GTI segments of one ObsID "
            "and return PA, PD, and their 1-sigma errors per segment."
        ),
        proposed_tool_name="ixpe_pcube_timeseg",
        validation_data=[ValidationDataset(mission="IXPE", obsid="02008401")],
    )
    return [
        AnalystReport(
            summary="Computed the whole-observation polarization but cannot segment it in time.",
            findings=["Whole-observation PD ~ 4% at PA ~ 45 deg (2-8 keV)."],
            tool_request=request,
            status="ok",
        ),
        AnalystReport(
            summary="Agree the time-segmented capability is missing from the toolkit.",
            findings=["No per-segment polarization-cube tool exists in the IXPE toolkit."],
            status="ok",
        ),
    ]


def _tool_spec() -> ToolSpec:
    return ToolSpec(
        request_kind="new_capability",
        science_context=(
            "Intra-observation PA/PD stability of an IXPE source; requires per-time-segment "
            "polarization cubes, which the toolkit lacks."
        ),
        expected_behavior=(
            "ixpe_pcube_timeseg(obsid, n_segments, energy_band) -> per-segment PA, PD, errors."
        ),
        proposed_tool_name="ixpe_pcube_timeseg",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="02008401")],
        proposed_checks=[
            ProposedCheck(
                kind="reproducibility",
                description="call twice on the same segments → per-segment PA/PD equal within 1 deg / 0.2%",
                tolerance=1.0,
            ),
            ProposedCheck(
                kind="metamorphic",
                description="segment ordering does not change the per-segment results",
            ),
        ],
    )


def _failed_result(spec: ToolSpec) -> MergeFlowResult:
    return MergeFlowResult(
        tool_spec_id=spec.id,
        status="failed",
        branch="ai",
        commit_sha=None,
        reason=(
            "Gate rework cap exhausted. The Layer-B reproducibility test (test_repro_pa_pd) was "
            "non-deterministic: the recovered PA varied by ~8 deg between the two gate runs, "
            "exceeding the 1 deg tolerance. Traced to a GTI-boundary edge case the coder could not "
            "stabilize within the rework budget."
        ),
        review=ToolReviewVerdict(
            approved=False,
            rationale=(
                "The reproducibility check is the right check, but the implementation is not stable; "
                "rejecting until per-segment PA/PD reproduce within tolerance."
            ),
            issues=["Per-segment GTI boundaries shift run-to-run; pin the segment edges deterministically."],
            checks_adequate=True,
        ),
    )


def _failed_analyst_report() -> AnalystReport:
    """A terminal report shaped like ``finalize_authoring_outcome``'s output — what the reviewer
    now receives at the analysis→review edge for a failed run."""
    return AnalystReport(
        summary=(
            "Could not complete the intra-observation PA/PD stability measurement for IXPE ObsID "
            "02008401: the required time-segmented polarization-cube capability could not be authored "
            "(its reproducibility check failed to stabilize within tolerance under the current "
            "construction). Partial result from the existing whole-observation tool: PD ~ 4% at PA ~ 45 deg."
        ),
        findings=[
            "Whole-observation polarization (2-8 keV): PD ~ 4%, PA ~ 45 deg (existing tool).",
            "Time-segmented PA/PD: unavailable — capability not authorable under current construction.",
        ],
        uncertainty="No per-segment values; the requested intra-observation stability is undetermined.",
        status="failed",
    )


def _write(log_path, record: dict) -> None:
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, default=str))
        stream.write("\n")


# --- Probe 1: the analyst lead at a failed authoring loop -------------------------------------

def test_live_lead_finalizes_failed_authoring():
    load_dotenv()
    registry = load_registry()
    lead = AnalystLeadAgent(model=registry.resolve("analyst_lead").handles[0])

    task, reports, spec = _task(), _worker_reports(), _tool_spec()
    result = _failed_result(spec)
    log_path = dev_log_path("honest-failure", "live_lead_finalize_failed_authoring", ".jsonl")

    report = asyncio.run(lead.finalize_authoring_outcome(task, reports, spec, result))

    _write(log_path, {"edge": "lead.finalize_authoring_outcome", "input": {
        "task": task.model_dump(mode="json"),
        "tool_spec": spec.model_dump(mode="json"),
        "authoring_result": result.model_dump(mode="json"),
        "worker_reports": [r.model_dump(mode="json") for r in reports],
    }})
    _write(log_path, {"edge": "lead.finalize_authoring_outcome", "output": report.model_dump(mode="json")})
    print(f"\n[lead.finalize_authoring_outcome] status={report.status}\nsummary={report.summary}\n→ {log_path}")

    # Structural contract only (LLM content is logged, not asserted, per the testing rules).
    assert isinstance(report, AnalystReport)
    assert report.status in ("ok", "failed", "needs_human")
    assert report.summary.strip()
    assert report.tool_request is None  # a terminal report does not re-request the tool


# --- Probe 2: the reviewer at a failed analysis ----------------------------------------------

def test_live_reviewer_adjudicates_failed_analysis():
    load_dotenv()
    registry = load_registry()
    reviewer = ReviewerAgent(model=registry.resolve("reviewer").handles[0])

    request = ResearchRequest(
        question=(
            "Is the X-ray polarization of the target in IXPE ObsID 02008401 stable within the "
            "observation, or does the polarization angle vary across the exposure?"
        )
    )
    report = _failed_analyst_report()
    log_path = dev_log_path("honest-failure", "live_reviewer_failed_analysis", ".jsonl")

    verdict = asyncio.run(reviewer.review(request, report=report))

    _write(log_path, {"edge": "reviewer.review", "input": {
        "request": request.model_dump(mode="json"),
        "analysis_report": report.model_dump(mode="json"),
    }})
    _write(log_path, {"edge": "reviewer.review", "output": verdict.model_dump(mode="json")})
    print(
        f"\n[reviewer.review] accepted={verdict.accepted} "
        f"needs_analysis_rework={verdict.needs_analysis_rework}\n"
        f"rationale={verdict.rationale}\nfollow_up={verdict.follow_up}\n→ {log_path}"
    )

    # Structural contract only.
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.rationale.strip()
    assert isinstance(verdict.accepted, bool)
    assert isinstance(verdict.needs_analysis_rework, bool)
