"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.builder import IOAgentLike, ReviewerLike, build_graph
from auto_research.models import resolve
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend
from auto_research.schemas import ResearchRequest, ReviewVerdict
from auto_research.storage import Storage


DEFAULT_WORKER_COUNT = 2
DEFAULT_ORCHESTRATION_CONFIG = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"


def load_orchestration_config(path: str | Path = DEFAULT_ORCHESTRATION_CONFIG) -> dict:
    """Load iteration caps from orchestration.yaml."""
    with Path(path).open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


async def run_request(
    request: ResearchRequest,
    *,
    reviewer: ReviewerLike | None = None,
    io_agent: IOAgentLike | None = None,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike] | None = None,
    lead: LeadLike | None = None,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int | None = None,
) -> ReviewVerdict:
    """Run the research-loop graph for one request and return the verdict.

    ``max_iterations`` and ``debate_max_rounds`` are read from
    ``config/orchestration.yaml`` when not supplied explicitly.

    Missing agent dependencies are constructed from ``config/models.yaml``.
    Local retrieval uses ``SqliteRetrievalBackend`` by default (no LLM call).
    """

    orch = load_orchestration_config()
    if max_iterations is None:
        max_iterations = int(orch.get("max_iterations", 3))
    debate_max_rounds = int(orch.get("debate_max_rounds", 3))

    storage = Storage()
    reviewer = reviewer or ReviewerAgent()
    io_agent = io_agent or LibrarianIOAgent(storage=storage)
    retrieval = retrieval or SqliteRetrievalBackend(storage)
    lead = lead or AnalystLeadAgent()
    if workers is None:
        worker_group = resolve("analyst_worker")
        worker_handles = worker_group.handles[:DEFAULT_WORKER_COUNT]
        workers = [AnalystWorkerAgent(model=handle) for handle in worker_handles]
    if consensus is None:
        consensus = DebateToConverge(max_rounds=debate_max_rounds)

    graph = build_graph(
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
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
