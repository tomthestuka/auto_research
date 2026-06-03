"""Deterministic tests for step-10 orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langgraph.graph import END

from auto_research.graph import build_graph
from auto_research.retrieval import RetrievalBackend
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
)


class FakeIOAgent:
    """Fake IO agent: composes a trivial search strategy and records calls."""

    def __init__(self):
        self.compose_calls: int = 0
        self.ingest_calls: int = 0
        self._search_count: int = 0

    async def compose_search_strategy(
        self, request: ResearchRequest, *, follow_up=None
    ) -> SearchRequest:
        self.compose_calls += 1
        query = request.question
        if follow_up:
            query = f"{query} | reviewer follow-up: {follow_up}"
        return SearchRequest(query=query)

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        self._search_count += 1
        return [SearchResult(source="arxiv", title=f"Paper {self._search_count}")]

    async def ingest_results(self, results: list[SearchResult]) -> None:
        self.ingest_calls += 1


class FakeRetrieval:
    """Fake retrieval backend: returns a synthetic local path per call."""

    def __init__(self):
        self._call_count: int = 0

    async def text_search(self, query: str, *, limit: int = 10):
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
    """Reviewer fake: gates literature/analysis on a configurable schedule."""

    def __init__(self, *, lit_pass_after: int = 1, analysis_pass_after: int = 1):
        self.lit_pass_after = lit_pass_after
        self.analysis_pass_after = analysis_pass_after
        self.lit_calls = 0
        self.analysis_calls = 0

    async def review(self, request, *, literature=(), report=None):
        if report is None:
            self.lit_calls += 1
            need_more = self.lit_calls < self.lit_pass_after
            return ReviewVerdict(
                accepted=False,
                rationale=f"literature pass {self.lit_calls}",
                needs_more_literature=need_more,
                follow_up="search more" if need_more else None,
            )
        self.analysis_calls += 1
        need_rework = self.analysis_calls < self.analysis_pass_after
        return ReviewVerdict(
            accepted=not need_rework,
            rationale=f"analysis pass {self.analysis_calls}",
            needs_analysis_rework=need_rework,
            follow_up="address X" if need_rework else None,
        )


class FakeWorker:
    def __init__(self, name: str = "w"):
        self.name = name
        self.calls = 0

    async def analyze(self, task: AnalysisTask, *, peer_reports=()):
        self.calls += 1
        return AnalystReport(summary=f"{self.name} round {self.calls}", findings=["f"])


class FakeLead:
    def __init__(self):
        self.adjudications = 0

    async def judge_convergence(self, task, reports, *, round_index, max_rounds):
        return ReviewVerdict(accepted=True, rationale="converged")

    async def adjudicate(self, task, reports):
        self.adjudications += 1
        return AnalystReport(summary=f"final {self.adjudications}", findings=["final"])


def _invoke(graph, request: ResearchRequest, *, max_iterations: int = 3):
    return asyncio.run(
        graph.ainvoke(
            {
                "request": request,
                "literature": [],
                "report": None,
                "verdict": None,
                "lit_iterations": 0,
                "analysis_iterations": 0,
                "max_iterations": max_iterations,
            }
        )
    )


def test_graph_runs_full_loop_in_single_pass():
    io_agent = FakeIOAgent()
    retrieval = FakeRetrieval()
    reviewer = FakeReviewer()
    lead = FakeLead()
    graph = build_graph(
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
        workers=[FakeWorker("w1"), FakeWorker("w2")],
        lead=lead,
        report_formats=[],
    )

    state = _invoke(graph, ResearchRequest(question="What is X?"))

    assert state["lit_iterations"] == 1
    assert state["analysis_iterations"] == 1
    assert state["verdict"].accepted is True
    assert state["report"].summary == "final 1"
    assert reviewer.lit_calls == 1
    assert reviewer.analysis_calls == 1
    assert io_agent.compose_calls == 1
    assert io_agent.ingest_calls == 1
    # literature is a list of Paths from the retrieval backend
    assert state["literature"] == [Path("/fake/paper_1.pdf")]


def test_graph_literature_feedback_capped_at_three():
    io_agent = FakeIOAgent()
    retrieval = FakeRetrieval()
    reviewer = FakeReviewer(lit_pass_after=99, analysis_pass_after=1)
    graph = build_graph(
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
        workers=[FakeWorker()],
        lead=FakeLead(),
        report_formats=[],
    )

    state = _invoke(graph, ResearchRequest(question="What is X?"))

    assert state["lit_iterations"] == 3
    assert io_agent.compose_calls == 3
    assert io_agent.ingest_calls == 3
    assert state["analysis_iterations"] == 1
    assert state["verdict"].accepted is True


def test_graph_analysis_feedback_capped_at_three():
    io_agent = FakeIOAgent()
    reviewer = FakeReviewer(lit_pass_after=1, analysis_pass_after=99)
    lead = FakeLead()
    workers = [FakeWorker("w1")]
    graph = build_graph(
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=FakeRetrieval(),
        workers=workers,
        lead=lead,
    )

    state = _invoke(graph, ResearchRequest(question="What is X?"))

    assert state["analysis_iterations"] == 3
    assert state["lit_iterations"] == 1
    assert state["verdict"].needs_analysis_rework is True
    assert state["verdict"].accepted is False
    assert lead.adjudications == 3


def test_graph_respects_custom_max_iterations():
    reviewer = FakeReviewer(lit_pass_after=99, analysis_pass_after=1)
    graph = build_graph(
        reviewer=reviewer,
        io_agent=FakeIOAgent(),
        retrieval=FakeRetrieval(),
        workers=[FakeWorker()],
        lead=FakeLead(),
        max_iterations=2,
        report_formats=[],
    )

    state = _invoke(graph, ResearchRequest(question="q"), max_iterations=2)

    assert state["lit_iterations"] == 2


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
