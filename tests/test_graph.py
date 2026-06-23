"""Deterministic tests for step-10 orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.graph import END

from auto_research.graph import build_graph
from auto_research.graph.builder import _resolve_cited_paths
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
    """Reviewer fake: gates literature/analysis on a configurable schedule."""

    def __init__(self, *, lit_pass_after: int = 1, analysis_pass_after: int = 1):
        self.lit_pass_after = lit_pass_after
        self.analysis_pass_after = analysis_pass_after
        self.lit_calls = 0
        self.analysis_calls = 0

    async def review(self, request, *, report=None):
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
            # On acceptance the reviewer cites a paper it read; flows to compose_report.
            cited_paths=[] if need_rework else ["/fake/cited.pdf"],
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
    # The literature node ingests but no longer pre-loads review docs; the reviewer's
    # accepted verdict carries cited_paths, which flow to compose_report.
    assert state["cited_paths"] == [Path("/fake/cited.pdf")]


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


class FailConsensus:
    """Consensus that returns an honest-failure report (§9.6)."""

    def __init__(self, status: str = "failed"):
        self.status = status

    async def run(self, workers, lead, task):
        return AnalystReport(
            summary="blocked on missing data", status=self.status, uncertainty="needs ObsID"
        )


def test_graph_routes_honest_failure_around_reviewer_to_end():
    reviewer = FakeReviewer()
    graph = build_graph(
        reviewer=reviewer,
        io_agent=FakeIOAgent(),
        retrieval=FakeRetrieval(),
        workers=[FakeWorker()],
        lead=FakeLead(),
        consensus=FailConsensus("failed"),
        report_formats=["jsonl"],
    )

    state = _invoke(graph, ResearchRequest(question="What is X?"))

    assert state["report"].status == "failed"
    # Reviewer never reviewed the analysis (failure routes straight to finalize, §9.6) …
    assert reviewer.analysis_calls == 0
    # … the verdict is non-accepted, so no report is composed (no fabrication).
    assert state["verdict"].accepted is False
    assert "research_report" not in state


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


def test_resolve_cited_paths_drops_unopened_citations(tmp_path):
    """A citation is kept only if the reviewer actually opened that paper."""
    opened = tmp_path / "read.pdf"
    other = tmp_path / "unread.pdf"
    reviewer = SimpleNamespace(tools=SimpleNamespace(opened_paths=[opened.resolve()]))
    verdict = ReviewVerdict(
        accepted=True, rationale="ok", cited_paths=[str(opened), str(other)]
    )

    cited = _resolve_cited_paths(verdict, reviewer)

    assert cited == [opened]  # the un-opened citation is dropped


def test_resolve_cited_paths_no_tools_takes_citations_as_is(tmp_path):
    """A reviewer with no KB-tool provider (e.g. a test double) keeps citations as given."""
    reviewer = SimpleNamespace()  # no .tools
    verdict = ReviewVerdict(accepted=True, rationale="ok", cited_paths=["/x/a.pdf"])

    assert _resolve_cited_paths(verdict, reviewer) == [Path("/x/a.pdf")]
