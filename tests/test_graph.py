"""Deterministic tests for step-10 orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.graph import END

from auto_research.graph import build_graph
from auto_research.retrieval import RetrievalBackend
from auto_research.schemas.literature import LiteratureReport
from auto_research.agents.react import TURN_END_TOOL, LeadCycleOutcome
from auto_research.provenance import ReceiptStore
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewerReport,
)
from auto_research.schemas.hitl import HumanResponse
from auto_research.schemas.review import ReviewerSection


class FakeIOAgent:
    """Fake librarian: files a trivial literature report per pass and records calls."""

    def __init__(self):
        self.research_calls: int = 0
        self.follow_ups: list[str | None] = []
        self.last_receipts: dict = {}

    async def research(self, request: ResearchRequest, *, follow_up=None) -> LiteratureReport:
        self.research_calls += 1
        self.follow_ups.append(follow_up)
        self.last_receipts = {f"lib:p{self.research_calls}": object()}
        return LiteratureReport(summary=f"Pass {self.research_calls}", status="succeeded")


class FakeRetrieval:
    """Fake retrieval backend: returns a synthetic local path per call."""

    def __init__(self):
        self._call_count: int = 0

    async def text_search(self, query: str, *, limit: int = 10, require_local_path: bool = False):
        from auto_research.retrieval import MemoryHit

        self._call_count += 1
        return [
            MemoryHit(
                kind="paper",
                id=self._call_count,
                score=1,
                title=f"FakePaper {self._call_count}",
                local_path=Path(f"/fake/paper_{self._call_count}.pdf"),
            )
        ]

    async def vector_search(self, query: str, *, limit: int = 10):
        raise NotImplementedError


class FakeReviewer:
    """Reviewer fake: files the next assignment of a script, and records what each turn was given.

    A report to the human carries the parts a real ``turn_end`` would require. Past the script's
    end it keeps sending the report to the human.
    """

    def __init__(self, script=("librarian", "analysts", "human")):
        self.script = list(script)
        self.turns: list[dict] = []

    async def review(self, request, *, arrived="request", literature=(), literature_receipts=None,
                     report=None, previous=(), human_nudges=(), human_files=(), budgets=None,
                     human_attended=False, render=None):
        n = len(self.turns)
        self.turns.append({
            "arrived": arrived, "budgets": dict(budgets or {}), "literature": len(literature),
            "documents": sorted(literature_receipts or {}), "previous": len(previous),
            "human_nudges": list(human_nudges), "human_files": list(human_files),
            "human_attended": human_attended,
        })
        target = self.script[n] if n < len(self.script) else "human"
        if target == "human":
            return ReviewerReport(
                accepted=report is not None and report.status == "succeeded",
                rationale=f"turn {n + 1}", assign_to="human", title="T", abstract="A",
                sections=[ReviewerSection(heading="H", body="B")], key_findings=["K"],
            )
        return ReviewerReport(rationale=f"turn {n + 1}", assign_to=target, follow_up=f"task {n + 1}")

    @property
    def arrivals(self) -> list[str]:
        return [t["arrived"] for t in self.turns]


class FakeWorker:
    def __init__(self, name: str = "w"):
        self.name = name
        self.calls = 0

    async def analyze(self, task: AnalysisTask, *, peer_reports=()):
        self.calls += 1
        return AnalystReport(summary=f"{self.name} round {self.calls}", findings=["f"])


class FakeLead:
    """One cycle, one filed form — the lead interface consensus needs."""

    def __init__(self):
        self.adjudications = 0

    async def run_cycle(self, task, reports, *, sources=None, **kwargs):
        self.adjudications += 1
        return LeadCycleOutcome(
            filed=TURN_END_TOOL,
            report=AnalystReport(summary=f"final {self.adjudications}", findings=["final"],
                                 status="succeeded"),
        )


def _invoke(graph, request: ResearchRequest, *, max_iterations: int = 3):
    return asyncio.run(
        graph.ainvoke(
            {
                "request": request,
                "report": None,
                "verdict": None,
                "arrived": "request",
                "human_rounds": 0,
                "lit_iterations": 0,
                "analysis_iterations": 0,
                "max_iterations": max_iterations,
            }
        )
    )


def _graph(reviewer, *, io_agent=None, consensus=None, max_iterations=3, **kwargs):
    return build_graph(
        reviewer=reviewer,
        io_agent=io_agent or FakeIOAgent(),
        retrieval=FakeRetrieval(),
        workers=[FakeWorker("w1")],
        lead=FakeLead(),
        consensus=consensus,
        max_iterations=max_iterations,
        report_formats=[],
        **kwargs,
    )


class ScriptedHuman:
    """A human handler that answers from a script and records what it was sent."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.queries = []

    async def __call__(self, query):
        self.queries.append(query)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return HumanResponse(query_id=query.id, answer=answer)


def test_the_run_starts_at_the_reviewer_and_every_team_returns_to_it():
    reviewer = FakeReviewer(("librarian", "analysts", "human"))
    state = _invoke(_graph(reviewer), ResearchRequest(question="What is X?"))

    assert reviewer.arrivals == ["request", "librarian", "analysts"]
    assert state["lit_iterations"] == 1 and state["analysis_iterations"] == 1
    assert state["verdict"].assign_to == "human" and state["verdict"].accepted is True
    assert state["report"].summary == "final 1"
    # Each turn is told the passes left per team, and sees its own earlier reports.
    assert [t["budgets"] for t in reviewer.turns] == [
        {"librarian": 3, "analysts": 3}, {"librarian": 2, "analysts": 3},
        {"librarian": 2, "analysts": 2},
    ]
    assert [t["previous"] for t in reviewer.turns] == [0, 1, 2]


def test_the_reviewer_may_skip_the_librarian_and_report_straight_away():
    reviewer = FakeReviewer(("human",))
    io_agent = FakeIOAgent()
    state = _invoke(_graph(reviewer, io_agent=io_agent), ResearchRequest(question="q"))
    assert io_agent.research_calls == 0 and state["human_rounds"] == 1
    assert reviewer.arrivals == ["request"]


def test_the_librarian_can_be_assigned_again_and_its_reports_accumulate():
    reviewer = FakeReviewer(("librarian", "librarian", "analysts", "human"))
    io_agent = FakeIOAgent()
    state = _invoke(_graph(reviewer, io_agent=io_agent), ResearchRequest(question="q"))

    # The reviewer's own questions reach the librarian each pass, never the raw request.
    assert io_agent.follow_ups == ["task 1", "task 2"]
    assert [lit.summary for lit in state["literature"]] == ["Pass 1", "Pass 2"]
    assert sorted(state["literature_receipts"]) == ["lib:p1", "lib:p2"]
    # The second search's turn sees both passes and both passes' documents.
    assert reviewer.turns[2]["literature"] == 2
    assert reviewer.turns[2]["documents"] == ["lib:p1", "lib:p2"]


def test_an_assignment_past_its_cap_is_a_harness_defect_not_a_silent_reroute():
    # A real reviewer's turn_end refuses this; a report that slipped through must not be routed.
    reviewer = FakeReviewer(("librarian", "librarian"))
    with pytest.raises(RuntimeError, match="cannot be routed"):
        _invoke(_graph(reviewer, max_iterations=1), ResearchRequest(question="q"), max_iterations=1)


class FailConsensus:
    """Consensus that returns an honest-failure report (§9.6)."""

    def __init__(self, status: str = "failed"):
        self.status = status

    async def run(self, workers, lead, task):
        return AnalystReport(
            summary="blocked on missing data", status=self.status, uncertainty="needs ObsID"
        )


def test_an_honest_failure_is_adjudicated_by_the_reviewer():
    # §9.6: the reviewer is the sole I/O — an honest failure returns to it, never bypasses it.
    reviewer = FakeReviewer(("analysts", "analysts", "human"))
    state = _invoke(_graph(reviewer, consensus=FailConsensus("failed")), ResearchRequest(question="q"))
    assert state["report"].status == "failed"
    assert reviewer.arrivals == ["request", "analysts", "analysts"]
    assert state["analysis_iterations"] == 2
    assert state["verdict"].accepted is False


def test_an_unattended_run_ends_once_the_report_is_published():
    reviewer = FakeReviewer(("human", "human", "human"))
    state = _invoke(_graph(reviewer), ResearchRequest(question="q"))
    assert len(reviewer.turns) == 1 and state["human_rounds"] == 1
    assert reviewer.turns[0]["human_attended"] is False


def test_a_human_reply_returns_to_the_reviewer_as_a_nudge_never_a_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_research.graph.builder._ledger_dir", lambda run_id: tmp_path / "ledger")
    store = ReceiptStore(tmp_path / "receipts")
    human = ScriptedHuman("Check the 3-6 keV band.", "done")
    reviewer = FakeReviewer(("analysts", "human", "analysts", "human"))
    state = _invoke(_graph(reviewer, human_handler=human, receipts=store), ResearchRequest(question="q"))

    assert reviewer.arrivals == ["request", "analysts", "human", "analysts"]
    assert reviewer.turns[2]["human_nudges"] == ["Check the 3-6 keV band."]
    assert reviewer.turns[2]["human_files"] == []
    # The reply's text is guidance: nothing in the store carries it, so it grounds nothing.
    assert not [r for r in store.receipts() if r.produced_by == "human_reply"]
    assert state["human_rounds"] == 2 and state["human_nudges"] == ["Check the 3-6 keV band."]
    assert human.queries[0].kind == "report" and human.queries[0].reply_dir.is_dir()
    assert all(t["human_attended"] for t in reviewer.turns)
    # The human is told how to answer, and the report's text travels when no PDF is attached.
    assert "reply `done`" in human.queries[0].message and "**T**" in human.queries[0].message
    assert human.queries[0].asked_by == "reviewer"


@pytest.mark.parametrize("answer,ends", [("done", True), ("  Done \n", True), ("DONE", True),
                                         ("DONE.", False), ("done please", False), ("", False)])
def test_only_the_word_done_ends_the_run(tmp_path, monkeypatch, answer, ends):
    monkeypatch.setattr("auto_research.graph.builder._ledger_dir", lambda run_id: tmp_path / "ledger")
    store = ReceiptStore(tmp_path / "receipts")
    human = ScriptedHuman(answer, "done")
    reviewer = FakeReviewer(("human", "human"))
    state = _invoke(_graph(reviewer, human_handler=human, receipts=store), ResearchRequest(question="q"))
    assert state["human_rounds"] == (1 if ends else 2)
    assert len(reviewer.turns) == (1 if ends else 2)


def test_an_unanswered_report_stands_and_the_run_ends(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_research.graph.builder._ledger_dir", lambda run_id: tmp_path / "ledger")
    human = ScriptedHuman(TimeoutError("12 h"))
    reviewer = FakeReviewer(("human",))
    state = _invoke(_graph(reviewer, human_handler=human, receipts=ReceiptStore(tmp_path / "r")),
                    ResearchRequest(question="q"))
    assert state["human_rounds"] == 1 and len(reviewer.turns) == 1


def test_build_graph_rejects_zero_max_iterations():
    with pytest.raises(ValueError, match="max_iterations"):
        build_graph(
            reviewer=FakeReviewer(),
            io_agent=FakeIOAgent(),
            retrieval=FakeRetrieval(),
            workers=[FakeWorker()],
            lead=FakeLead(),
            max_iterations=0,
        )


def test_a_checkpoint_from_the_graph_before_sole_io_is_refused_not_resumed():
    from auto_research.graph.runtime import _refuse_pre_sole_io_checkpoint

    old = SimpleNamespace(checkpoint={"channel_values": {"request": "q", "lit_iterations": 1}})
    with pytest.raises(RuntimeError, match="cannot resume"):
        _refuse_pre_sole_io_checkpoint(old)
    _refuse_pre_sole_io_checkpoint(SimpleNamespace(checkpoint={"channel_values": {"arrived": "librarian"}}))
    _refuse_pre_sole_io_checkpoint(SimpleNamespace(checkpoint={"channel_values": {}}))


def test_an_html_only_report_is_sent_to_the_human_as_the_page(tmp_path, monkeypatch):
    """With `report_formats: [html]` there is no PDF: the published page is the attachment."""
    from auto_research.report.html import HtmlResult

    monkeypatch.setattr("auto_research.graph.builder._ledger_dir", lambda run_id: tmp_path / "ledger")
    monkeypatch.setattr("auto_research.graph.builder._version_dir", lambda run_id, k: tmp_path / f"v{k}")

    class PageReviewer(FakeReviewer):
        async def review(self, request, *, render=None, **kwargs):
            report = await super().review(request, render=render, **kwargs)
            if report.assign_to == "human" and render is not None:
                render.report_dir.mkdir(parents=True, exist_ok=True)
                page = render.report_dir / "report.html"
                page.write_text("<!doctype html><title>T</title>")
                render.html = HtmlResult(html_path=page)  # what its turn_end's render leaves
            return report

    store = ReceiptStore(tmp_path / "receipts")
    human = ScriptedHuman("done")
    reviewer = PageReviewer(("human",))
    graph = build_graph(
        reviewer=reviewer, io_agent=FakeIOAgent(), retrieval=FakeRetrieval(), workers=[FakeWorker("w1")],
        lead=FakeLead(), max_iterations=3, report_formats=["html"], human_handler=human, receipts=store,
    )
    state = _invoke(graph, ResearchRequest(question="q"))
    (attachment,) = human.queries[0].attachments
    assert attachment.filename == "report.html" and "The report is attached." in human.queries[0].message
    assert state["report_html"] == tmp_path / "v1" / "report.html"
