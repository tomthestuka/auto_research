"""LangGraph assembly for the Phase 1 research loop."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph

from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.state import ResearchState
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
)


class LibrarianLike(Protocol):
    """Minimal interface used by the literature node."""

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Return literature for one search request."""


class ReviewerLike(Protocol):
    """Minimal interface used by the review nodes."""

    async def review(
        self,
        request: ResearchRequest,
        *,
        literature: Sequence[SearchResult] = (),
        report: AnalystReport | None = None,
    ) -> ReviewVerdict:
        """Return a review verdict for the supplied state."""


def build_graph(
    *,
    reviewer: ReviewerLike,
    librarian: LibrarianLike,
    workers: Sequence[WorkerLike],
    lead: LeadLike,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int = 3,
) -> Any:
    """Compile the Phase 1 research-loop graph.

    Two feedback edges, each hard-capped at ``max_iterations``:

    - paper-search review: ``review_literature -> literature``
    - analysis review: ``review_analysis -> analysis``
    """

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    consensus_strategy = consensus or DebateToConverge(max_rounds=max_iterations)

    async def literature_node(state: ResearchState) -> dict[str, Any]:
        request = state["request"]
        query = request.question
        prior = state.get("verdict")
        if prior and prior.needs_more_literature and prior.follow_up:
            query = f"{query} | reviewer follow-up: {prior.follow_up}"
        search_request = SearchRequest(query=query)
        results = await librarian.search(search_request)
        return {
            "literature": list(results),
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
            literature=state.get("literature", []),
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
