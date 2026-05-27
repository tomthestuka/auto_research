"""LangGraph assembly for the Phase 1 research loop."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph

from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.state import ResearchState
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend, text_search_paths
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
)


class IOAgentLike(Protocol):
    """Minimal interface required of the librarian I/O agent."""

    async def compose_search_strategy(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> SearchRequest:
        """Compose a focused search strategy from a research request."""

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Query external sources and return raw results."""

    async def ingest_results(self, results: list[SearchResult]) -> None:
        """Download, abstract, and store each search result."""


class ReviewerLike(Protocol):
    """Minimal interface used by the review nodes."""

    async def review(
        self,
        request: ResearchRequest,
        *,
        literature: Sequence[Path] = (),
        report: AnalystReport | None = None,
    ) -> ReviewVerdict:
        """Return a review verdict for the supplied state."""


def build_graph(
    *,
    reviewer: ReviewerLike,
    io_agent: IOAgentLike,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike],
    lead: LeadLike,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int = 3,
) -> Any:
    """Compile the Phase 1 research-loop graph.

    Two feedback edges, each hard-capped at ``max_iterations``:

    - paper-search review: ``review_literature -> literature``
    - analysis review: ``review_analysis -> analysis``

    The literature node follows a four-step pipeline:
    IO agent composes search strategy → IO agent queries online sources →
    IO agent downloads and abstracts results → ``text_search_paths()`` queries
    local SQLite (pure Python, no LLM). The reviewer receives local PDF paths
    and loads them as full-document attachments.
    """

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    consensus_strategy = consensus or DebateToConverge(max_rounds=max_iterations)
    _retrieval = retrieval or SqliteRetrievalBackend()

    async def literature_node(state: ResearchState) -> dict[str, Any]:
        prior = state.get("verdict")
        follow_up = prior.follow_up if prior and prior.needs_more_literature else None
        search_request = await io_agent.compose_search_strategy(
            state["request"], follow_up=follow_up
        )
        raw = await io_agent.search(search_request)
        await io_agent.ingest_results(raw)
        paths = await text_search_paths(
            search_request.query,
            limit=search_request.max_results,
            backend=_retrieval,
        )
        return {
            "literature": paths,
            "lit_iterations": state.get("lit_iterations", 0) + 1,
        }

    async def review_literature_node(state: ResearchState) -> dict[str, Any]:
        verdict = await reviewer.review(
            state["request"],
            literature=state.get("literature", []),
            report=None,
        )
        return {"verdict": verdict}

    async def analysis_node(state: ResearchState) -> dict[str, Any]:
        request = state["request"]
        instructions_parts: list[str] = []
        if request.context:
            instructions_parts.append(request.context)
        prior = state.get("verdict")
        if prior and prior.needs_analysis_rework and prior.follow_up:
            instructions_parts.append(f"Reviewer feedback for revision: {prior.follow_up}")
        task = AnalysisTask(
            question=request.question,
            instructions="\n\n".join(instructions_parts) or None,
            previous_report=state.get("report"),
        )
        report = await consensus_strategy.run(workers, lead, task)
        return {
            "report": report,
            "analysis_iterations": state.get("analysis_iterations", 0) + 1,
        }

    async def review_analysis_node(state: ResearchState) -> dict[str, Any]:
        verdict = await reviewer.review(
            state["request"],
            literature=state.get("literature", []),
            report=state.get("report"),
        )
        return {"verdict": verdict}

    def after_review_literature(state: ResearchState) -> str:
        verdict = state["verdict"]
        if verdict.needs_more_literature and state.get("lit_iterations", 0) < max_iterations:
            return "literature"
        return "analysis"

    def after_review_analysis(state: ResearchState) -> str:
        verdict = state["verdict"]
        if verdict.needs_analysis_rework and state.get("analysis_iterations", 0) < max_iterations:
            return "analysis"
        return END

    builder = StateGraph(ResearchState)
    builder.add_node("literature", literature_node)
    builder.add_node("review_literature", review_literature_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("review_analysis", review_analysis_node)

    builder.add_edge(START, "literature")
    builder.add_edge("literature", "review_literature")
    builder.add_conditional_edges(
        "review_literature",
        after_review_literature,
        {"literature": "literature", "analysis": "analysis"},
    )
    builder.add_edge("analysis", "review_analysis")
    builder.add_conditional_edges(
        "review_analysis",
        after_review_analysis,
        {"analysis": "analysis", END: END},
    )

    return builder.compile()
