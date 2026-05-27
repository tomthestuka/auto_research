"""Deterministic tests for step-10 orchestration."""

from __future__ import annotations

import asyncio

import pytest
from langgraph.graph import END

from auto_research.graph import build_graph
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
)


class FakeLibrarian:
    def __init__(self):
        self.queries: list[str] = []

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        self.queries.append(request.query)
        return [SearchResult(source="arxiv", title=f"Paper {len(self.queries)}")]


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
    librarian = FakeLibrarian()
    reviewer = FakeReviewer()
    lead = FakeLead()
    graph = build_graph(
        reviewer=reviewer,
        librarian=librarian,
        workers=[FakeWorker("w1"), FakeWorker("w2")],
        lead=lead,
    )

    state = _invoke(graph, ResearchRequest(question="What is X?"))

    assert state["lit_iterations"] == 1
    assert state["analysis_iterations"] == 1
    assert state["verdict"].accepted is True
    assert state["report"].summary == "final 1"
    assert reviewer.lit_calls == 1
    assert reviewer.analysis_calls == 1


def test_graph_literature_feedback_capped_at_three():
    librarian = FakeLibrarian()
    reviewer = FakeReviewer(lit_pass_after=99, analysis_pass_after=1)
    graph = build_graph(
        reviewer=reviewer,
        librarian=librarian,
        workers=[FakeWorker()],
        lead=FakeLead(),
    )

    state = _invoke(graph, ResearchRequest(question="What is X?"))

    assert state["lit_iterations"] == 3
    assert librarian.queries[0] == "What is X?"
    # Reviewer follow-up is fed back into the next query.
    assert "reviewer follow-up" in librarian.queries[1]
    assert state["analysis_iterations"] == 1
    assert state["verdict"].accepted is True


def test_graph_analysis_feedback_capped_at_three():
    librarian = FakeLibrarian()
    reviewer = FakeReviewer(lit_pass_after=1, analysis_pass_after=99)
    lead = FakeLead()
    workers = [FakeWorker("w1")]
    graph = build_graph(
        reviewer=reviewer,
        librarian=librarian,
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
    librarian = FakeLibrarian()
    reviewer = FakeReviewer(lit_pass_after=99, analysis_pass_after=1)
    graph = build_graph(
        reviewer=reviewer,
        librarian=librarian,
        workers=[FakeWorker()],
        lead=FakeLead(),
        max_iterations=2,
    )

    state = _invoke(graph, ResearchRequest(question="q"), max_iterations=2)

    assert state["lit_iterations"] == 2


def test_build_graph_rejects_zero_max_iterations():
    with pytest.raises(ValueError, match="max_iterations"):
        build_graph(
            reviewer=FakeReviewer(),
            librarian=FakeLibrarian(),
            workers=[FakeWorker()],
            lead=FakeLead(),
            max_iterations=0,
        )
