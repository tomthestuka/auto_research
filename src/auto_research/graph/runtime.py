"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
from auto_research.monitor import RunMonitor
from auto_research.models import resolve
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.schemas import ResearchRequest, ReviewVerdict
from auto_research.storage import Storage


DEFAULT_WORKER_COUNT = 2
DEFAULT_ORCHESTRATION_CONFIG = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"


@dataclass(frozen=True)
class ResearchRun:
    """Result object returned by the monitored library entry."""

    request: ResearchRequest
    verdict: ReviewVerdict
    final_state: dict[str, Any]
    monitor: RunMonitor

    def markdown(self) -> str:
        """Return the grouped monitor report."""

        return self.monitor.render_markdown(grouped=True)


def load_orchestration_config(path: str | Path = DEFAULT_ORCHESTRATION_CONFIG) -> dict:
    """Load iteration caps from orchestration.yaml."""
    with Path(path).open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _default_graph():
    """Build the compiled graph with all defaults from config.

    Synchronous; called at module import time by serve.py for LangGraph Studio.
    Reads config/orchestration.yaml and config/models.yaml. Requires API keys
    to be present in the environment (langgraph-cli loads .env before importing).
    """
    orch = load_orchestration_config()
    max_iterations = int(orch.get("max_iterations", 3))
    debate_max_rounds = int(orch.get("debate_max_rounds", 3))
    tool_retry_cap = int(orch.get("tool_retry_cap", 3))
    toolkit_root = Path(orch.get("toolkit_root", "/data/fzhou/research_toolkit"))

    storage = Storage()
    worker_group = resolve("analyst_worker")
    workers = [AnalystWorkerAgent(model=h) for h in worker_group.handles[:DEFAULT_WORKER_COUNT]]

    return build_graph(
        reviewer=ReviewerAgent(),
        io_agent=LibrarianIOAgent(storage=storage),
        retrieval=SqliteRetrievalBackend(storage),
        workers=workers,
        lead=AnalystLeadAgent(),
        consensus=DebateToConverge(max_rounds=debate_max_rounds),
        max_iterations=max_iterations,
        registry=ToolkitRegistry(toolkit_root),
        tool_retry_cap=tool_retry_cap,
        toolkit_root=toolkit_root,
    )


async def run_request(
    request: ResearchRequest | str,
    *,
    reviewer: ReviewerLike | None = None,
    io_agent: IOAgentLike | None = None,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike] | None = None,
    lead: LeadLike | None = None,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int | None = None,
    chatter: int = 10,
    monitor: RunMonitor | None = None,
    monitor_jsonl: str | Path | None = None,
) -> ReviewVerdict:
    """Run the research-loop graph for one request and return the verdict."""

    run = await run_research(
        request,
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
        workers=workers,
        lead=lead,
        consensus=consensus,
        max_iterations=max_iterations,
        chatter=chatter,
        monitor=monitor,
        monitor_jsonl=monitor_jsonl,
    )
    return run.verdict


async def run_research(
    request: ResearchRequest | str,
    *,
    reviewer: ReviewerLike | None = None,
    io_agent: IOAgentLike | None = None,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike] | None = None,
    lead: LeadLike | None = None,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int | None = None,
    chatter: int = 10,
    monitor: RunMonitor | None = None,
    monitor_jsonl: str | Path | None = None,
) -> ResearchRun:
    """Run the graph and return the verdict, final state, and monitor.

    ``max_iterations`` and ``debate_max_rounds`` are read from
    ``config/orchestration.yaml`` when not supplied explicitly.

    Missing agent dependencies are constructed from ``config/models.yaml``.
    Local retrieval uses ``SqliteRetrievalBackend`` by default (no LLM call).
    ``chatter`` follows the monitor's 1-10 verbosity scale; 10 records the full
    agent/tool/consensus narrative.
    """

    if isinstance(request, str):
        request = ResearchRequest(question=request)
    run_monitor = monitor or RunMonitor(chatter=chatter, jsonl_path=monitor_jsonl)
    orch = load_orchestration_config()
    if max_iterations is None:
        max_iterations = int(orch.get("max_iterations", 3))
    debate_max_rounds = int(orch.get("debate_max_rounds", 3))
    tool_retry_cap = int(orch.get("tool_retry_cap", 3))
    toolkit_root = Path(orch.get("toolkit_root", "/data/fzhou/research_toolkit"))

    storage = Storage()
    reviewer = reviewer or ReviewerAgent(monitor=run_monitor)
    io_agent = io_agent or LibrarianIOAgent(storage=storage, monitor=run_monitor)
    retrieval = retrieval or SqliteRetrievalBackend(storage)
    lead = lead or AnalystLeadAgent(monitor=run_monitor)
    if workers is None:
        worker_group = resolve("analyst_worker")
        worker_handles = worker_group.handles[:DEFAULT_WORKER_COUNT]
        workers = [AnalystWorkerAgent(model=handle, monitor=run_monitor) for handle in worker_handles]
    _attach_monitor(reviewer, run_monitor)
    _attach_monitor(io_agent, run_monitor)
    _attach_monitor(lead, run_monitor)
    for worker in workers:
        _attach_monitor(worker, run_monitor)
    if consensus is None:
        consensus = DebateToConverge(max_rounds=debate_max_rounds, monitor=run_monitor)

    registry = ToolkitRegistry(toolkit_root)

    await run_monitor.record(
        "run",
        "run.start",
        level=1,
        summary="Research run started",
        data={"request": request, "max_iterations": max_iterations, "tool_retry_cap": tool_retry_cap},
    )
    graph = build_graph(
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
        workers=workers,
        lead=lead,
        consensus=consensus,
        max_iterations=max_iterations,
        registry=registry,
        tool_retry_cap=tool_retry_cap,
        toolkit_root=toolkit_root,
        monitor=run_monitor,
    )
    try:
        final_state: dict[str, Any] = await graph.ainvoke(
            {
                "request": request,
                "literature": [],
                "report": None,
                "verdict": None,
                "verdicts": [],
                "lit_iterations": 0,
                "analysis_iterations": 0,
                "max_iterations": max_iterations,
                "tool_results": [],
                "tool_queue": [],
            }
        )
    except Exception as exc:
        await run_monitor.record(
            "run",
            "run.error",
            level=1,
            summary="Research run failed",
            data={"error": repr(exc)},
        )
        raise
    verdict = final_state["verdict"]
    await run_monitor.record(
        "run",
        "run.finish",
        level=1,
        summary="Research run finished",
        data={
            "verdict": verdict,
            "lit_iterations": final_state.get("lit_iterations", 0),
            "analysis_iterations": final_state.get("analysis_iterations", 0),
            "tool_result_count": len(final_state.get("tool_results") or []),
        },
    )
    return ResearchRun(request=request, verdict=verdict, final_state=final_state, monitor=run_monitor)


def _attach_monitor(target: object, monitor: RunMonitor) -> None:
    if hasattr(target, "monitor") and getattr(target, "monitor", None) is None:
        try:
            setattr(target, "monitor", monitor)
        except Exception:
            pass
