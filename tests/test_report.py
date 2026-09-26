"""Publishing the reviewer's accepted report, the TeX log reader, and report_formats handling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_research.agents.react import TURN_END_TOOL, LeadCycleOutcome
from auto_research.graph import build_graph
from auto_research.provenance import ReceiptStore
from auto_research.report import cited_handles, publish_report, receipt_closure
from auto_research.report.writers import _errors_with_context
from auto_research.schemas import AnalystReport, ResearchRequest
from auto_research.schemas.literature import LiteratureReport
from auto_research.schemas.report import ResearchReport
from auto_research.schemas.review import ReviewerReport, ReviewerSection, ReviewerTable


@pytest.fixture
def store(tmp_path: Path) -> ReceiptStore:
    s = ReceiptStore(tmp_path / "receipts")
    raw = s.mint(source="analyst_worker_0", kind="result", produced_by="research_toolkit.xspec_utils.fit", data="PD = 4.02 ± 0.21 %")
    s.mint(source="analyst_lead", kind="result", produced_by="research_toolkit.t.summarise", data="PD = 4.02", parents=[raw.handle])
    csv = tmp_path / "t.csv"
    csv.write_text("a,b\n1,2\n")
    s.mint(source="analyst_lead", kind="data", produced_by="research_toolkit.t.compare", path=csv)
    s.mint(source="lib", kind="paper", produced_by="read_paper", data="PD = 4.0 %", title="Paper")
    return s


def _report() -> ReviewerReport:
    return ReviewerReport(
        accepted=True,
        rationale="Agrees [lib:p1].",
        assign_to="human",
        title="T",
        abstract='We find {{analyst_lead:r1 "PD = 4.02"}}.',
        sections=[ReviewerSection(heading="R", body="See [analyst_lead:r1].")],
        key_findings=["Holds [lib:p1]."],
        tables=[ReviewerTable(caption="c", receipt="analyst_lead:d1")],
    )


def test_cited_handles_cover_prose_figures_and_table_receipts():
    assert cited_handles(_report(), own_source="rev") == ["lib:p1", "analyst_lead:r1", "analyst_lead:d1"]


def test_the_closure_carries_every_ancestor_once(store):
    handles = [r.handle for r in receipt_closure(store, ["analyst_lead:r1", "lib:p1"])]
    assert handles == ["analyst_lead:r1", "lib:p1", "analyst_worker_0:r1"]


def test_published_files_become_receipts_whose_parents_are_the_citations(store, tmp_path):
    tex = tmp_path / "report" / "report.tex"
    tex.parent.mkdir()
    tex.write_text("\\documentclass{article}")
    result = publish_report(
        _report(), store=store, run_id="r1", request_question="Q?", report_dir=tmp_path / "report",
        formats=["jsonl", "tex"], tex_path=tex,
    )
    assert set(result.artifacts) == {"tex", "jsonl"}
    tex_receipt = store.get(result.artifacts["tex"])
    assert tex_receipt.handle.startswith("rev:d") and tex_receipt.produced_by == "report_renderer"
    assert tex_receipt.parents == ["lib:p1", "analyst_lead:r1", "analyst_lead:d1"]
    machine = ResearchReport.model_validate_json(result.jsonl_path.read_text())
    assert machine.report == _report() and machine.artifacts == {"tex": result.artifacts["tex"]}
    assert machine.paper_attributions == []  # this report attributes no paper value
    assert {r.handle for r in machine.receipts} >= {"analyst_lead:r1", "analyst_worker_0:r1"}


def test_the_jsonl_lists_every_value_attributed_to_a_paper(store, tmp_path):
    report = _report().model_copy(update={"key_findings": ['The paper gives {{lib:p1 "PD = 4.0 %" Table 1}}.']})
    result = publish_report(report, store=store, run_id="r1", request_question="Q?",
                            report_dir=tmp_path / "report", formats=["jsonl"])
    [item] = result.research_report.paper_attributions
    assert (item.handle, item.value, item.locator) == ("lib:p1", "PD = 4.0 %", "Table 1")


def test_the_jsonl_is_written_only_when_configured(store, tmp_path):
    result = publish_report(
        _report(), store=store, run_id="r1", request_question="Q?", report_dir=tmp_path / "report",
        formats=["tex"],
    )
    assert result.jsonl_path is None and result.research_report is None
    assert not (tmp_path / "report" / "report.jsonl").exists()


def test_a_flag_on_a_cited_receipt_marks_the_published_report(store, tmp_path):
    store.mint(source="analyst_lead", kind="result", produced_by="research_toolkit.xspec_utils.fit", data="PD = 9.9", failed=True)
    report = _report().model_copy(update={"key_findings": ["Failed run [analyst_lead:r2]."]})
    result = publish_report(
        report, store=store, run_id="r1", request_question="Q?", report_dir=tmp_path / "report",
        formats=["jsonl"],
    )
    assert "failed" in store.get(result.artifacts["jsonl"]).flags


# -- the graph publishes what the reviewer filed ----------------------------------------------


class _Reviewer:
    def __init__(self, store):
        self.receipts = store

    async def review(self, request, *, arrived="request", report=None, **_):
        if arrived == "request":
            return ReviewerReport(rationale="ok [lib:p1]", assign_to="analysts", follow_up="analyse")
        return _report()


class _IO:
    last_receipts: dict = {}

    async def research(self, request, *, follow_up=None):
        return LiteratureReport(summary="s", status="succeeded")


class _Worker:
    async def analyze(self, task, *, peer_reports=()):
        return AnalystReport(summary="w", findings=["f"])


class _Lead:
    async def run_cycle(self, task, reports, *, sources=None, **kwargs):
        return LeadCycleOutcome(filed=TURN_END_TOOL, report=AnalystReport(summary="l", findings=["f"]))


def test_the_graph_publishes_the_accepted_report_as_jsonl(store, tmp_path, monkeypatch):
    monkeypatch.setattr("auto_research.graph.builder._report_dir", lambda run_id: tmp_path / "rep")
    graph = build_graph(
        reviewer=_Reviewer(store), io_agent=_IO(), workers=[_Worker()], lead=_Lead(),
        run_id="r1", receipts=store, report_formats=["jsonl"],
    )
    state = asyncio.run(graph.ainvoke({
        "request": ResearchRequest(question="Q?"), "report": None, "verdict": None,
        "arrived": "request", "human_rounds": 0,
        "lit_iterations": 0, "analysis_iterations": 0, "max_iterations": 2,
    }))
    assert state["verdict"] == _report()
    assert state["research_report"].report == _report()
    # Each report to the human has its own folder, so an earlier version keeps its bytes.
    assert state["report_jsonl"] == tmp_path / "rep" / "v1" / "report.jsonl"


# -- TeX log reading --------------------------------------------------------------------------

# An undefined macro is the one compile error a retry cannot fix from the `!` line alone:
# TeX names the macro only on the `l.<n>` context line. Observed live on Q21 2026-08-10,
# where two attempts failed identically on `\lesssim` and the run finished with no PDF.
_TEX_UNDEFINED_MACRO_LOG = r"""
This is pdfTeX, Version 3.14159265
! Undefined control sequence.
l.110 ...restrict the peak search to $|\tau| \lesssim
                                                     $ half the sub-observation
Some recovery noise here.
! Missing $ inserted.
<inserted text>
                $
l.204 \end{document}
!  ==> Fatal error occurred, no output PDF file produced!
"""


def test_compile_errors_carry_the_offending_source_line():
    """The error names *which* macro failed, not merely that something did."""
    errors = _errors_with_context(_TEX_UNDEFINED_MACRO_LOG)
    assert len(errors) == 3
    assert errors[0].startswith("! Undefined control sequence.")
    assert r"\lesssim" in errors[0] and "l.110" in errors[0]
    assert "l.204" in errors[1]


def test_compile_error_without_context_stays_bare():
    """TeX does not always print a context line; the bare message must not absorb a later one."""
    errors = _errors_with_context("! Emergency stop.\nno context here\nl.999 unrelated\n", lookahead=1)
    assert errors == ["! Emergency stop."]


def test_compile_error_context_does_not_leak_across_errors():
    errors = _errors_with_context("! First.\n! Second.\nl.7 belongs to second\n")
    assert errors[0] == "! First."
    assert "l.7" in errors[1]


# -- report_formats ---------------------------------------------------------------------------


def test_report_formats_empty_skips_report():
    from auto_research.graph.runtime import _normalize_report_formats

    assert _normalize_report_formats([], ["jsonl", "tex"]) == []


def test_report_formats_normalization():
    from auto_research.graph.runtime import _normalize_report_formats

    assert _normalize_report_formats(None, ["jsonl"]) == ["jsonl"]
    assert _normalize_report_formats("tex", ["jsonl"]) == ["tex"]
    assert _normalize_report_formats(["jsonl", "tex"], []) == ["jsonl", "tex"]
    with pytest.raises(ValueError, match="Unknown report_formats"):
        _normalize_report_formats(["pdf"], [])
