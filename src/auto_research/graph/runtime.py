"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.consensus import ConsensusStrategy, LeadLike, WorkerLike
from auto_research.graph.builder import LibrarianLike, ReviewerLike, build_graph
from auto_research.models import resolve
from auto_research.schemas import ResearchRequest, ReviewVerdict


DEFAULT_WORKER_COUNT = 2


async def run_request(
    request: ResearchRequest,
    *,
    reviewer: ReviewerLike | None = None,
    librarian: LibrarianLike | None = None,
    workers: Sequence[WorkerLike] | None = None,
    lead: LeadLike | None = None,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int = 3,
) -> ReviewVerdict:
    """Run the research-loop graph for one request and return the verdict.

    Missing dependencies are constructed from ``config/models.yaml``: a single
    librarian I/O agent, ``DEFAULT_WORKER_COUNT`` analyst workers (or as many as
    the registry exposes), one analyst lead, and one reviewer.
    """

    reviewer = reviewer or ReviewerAgent()
    librarian = librarian or LibrarianIOAgent()
    lead = lead or AnalystLeadAgent()
    if workers is None:
        worker_group = resolve("analyst_worker")
        worker_handles = worker_group.handles[:DEFAULT_WORKER_COUNT]
        workers = [AnalystWorkerAgent(model=handle) for handle in worker_handles]

    graph = build_graph(
        reviewer=reviewer,
        librarian=librarian,
        workers=workers,
        lead=lead,
        consensus=consensus,
        max_iterations=max_iterations,
    )
    final_state: dict[str, Any] = await graph.ainvoke(
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
    return final_state["verdict"]
