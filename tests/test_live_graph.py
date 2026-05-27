"""Opt-in live check for step-10 orchestration.

Run with:
    AUTO_RESEARCH_LIVE_GRAPH_TESTS=1 pytest tests/test_live_graph.py -s

Drives the compiled LangGraph end-to-end on one real research request with the
default configured roles. Writes per-call traces under ``logs/dev/step-10/``.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.graph import build_graph
from auto_research.models import load_registry
from auto_research.retrieval import SqliteRetrievalBackend
from auto_research.schemas import ResearchRequest, SearchRequest, SearchResult
from auto_research.storage import Storage
from tests.dev_logs import dev_log_path, summarize_graph_log


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_GRAPH_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_GRAPH_TESTS=1 to spend provider tokens",
)


SAMPLE_QUESTION = (
    "What photon index is observed for GX 339-4 in the low/hard state during its "
    "RXTE-era outbursts, and what does the literature say about its stability across "
    "outbursts?"
)


def test_live_graph_end_to_end_gx339_question():
    load_dotenv()
    registry = load_registry()
    events: list[dict] = []
    storage = Storage()

    reviewer = LoggingReviewer(
        ReviewerAgent(model=registry.resolve("reviewer").handles[0]), events
    )
    io_agent = LoggingIOAgent(
        LibrarianIOAgent(
            model=registry.resolve("librarian_io").handles[0],
            storage=storage,
        ),
        events,
    )
    retrieval = SqliteRetrievalBackend(storage)
    worker_handles = registry.resolve("analyst_worker").handles[:2]
    workers = [
        LoggingWorker(AnalystWorkerAgent(model=handle), events, f"worker_{index}")
        for index, handle in enumerate(worker_handles, start=1)
    ]
    lead = LoggingLead(
        AnalystLeadAgent(model=registry.resolve("analyst_lead").handles[0]), events
    )

    graph = build_graph(
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
        workers=workers,
        lead=lead,
        max_iterations=3,
    )

    log_path = dev_log_path("step-10", "live_graph_gx339", ".jsonl")
    request = ResearchRequest(question=SAMPLE_QUESTION)

    async def run():
        state = await graph.ainvoke(
            {
                "request": request,
                "literature": [],
                "report": None,
                "verdict": None,
                "lit_iterations": 0,
                "analysis_iterations": 0,
                "max_iterations": 3,
            }
        )
        events.append(
            {
                "event": "final_state",
                "role": None,
                "model_member": None,
                "status": "ok",
                "input_tokens": 0,
                "output_tokens": 0,
                "output": {
                    "lit_iterations": state["lit_iterations"],
                    "analysis_iterations": state["analysis_iterations"],
                    "verdict": state["verdict"].model_dump(mode="json")
                    if state.get("verdict")
                    else None,
                    "report": state["report"].model_dump(mode="json")
                    if state.get("report")
                    else None,
                    "literature_count": len(state.get("literature", [])),
                },
                "error": None,
            }
        )
        with log_path.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, sort_keys=True))
                stream.write("\n")
        return state

    state = asyncio.run(run())
    md_path = summarize_graph_log(log_path)

    assert state["verdict"] is not None
    assert state["report"] is not None
    assert state["lit_iterations"] >= 1
    assert state["analysis_iterations"] >= 1
    print(f"wrote dev log {log_path}", flush=True)
    print(f"wrote summary {md_path}", flush=True)


class LoggingReviewer:
    def __init__(self, agent: ReviewerAgent, events: list[dict]):
        self.agent = agent
        self.events = events
        self.calls = 0

    async def review(self, request, *, literature=(), report=None):
        self.calls += 1
        verdict = await self.agent.review(request, literature=literature, report=report)
        stage = "review_analysis" if report is not None else "review_literature"
        self.events.append(
            _event(
                f"{stage}_{self.calls}",
                self.agent,
                output=verdict.model_dump(mode="json"),
            )
        )
        return verdict


class LoggingIOAgent:
    """Wraps LibrarianIOAgent, delegates all methods and logs key calls."""

    def __init__(self, agent: LibrarianIOAgent, events: list[dict]):
        self.agent = agent
        self.events = events
        self.compose_calls = 0
        self.ingest_calls = 0

    async def compose_search_strategy(self, request, *, follow_up=None) -> SearchRequest:
        self.compose_calls += 1
        strategy = await self.agent.compose_search_strategy(request, follow_up=follow_up)
        self.events.append(
            _event(
                f"io_compose_{self.compose_calls}",
                self.agent,
                output=strategy.model_dump(mode="json"),
            )
        )
        return strategy

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        return await self.agent.search(request)

    async def ingest_results(self, results: list[SearchResult]) -> None:
        self.ingest_calls += 1
        await self.agent.ingest_results(results)
        self.events.append(
            {
                "event": f"io_ingest_{self.ingest_calls}",
                "role": "librarian_io",
                "model_member": "io_agent",
                "status": "ok",
                "input_tokens": 0,
                "output_tokens": 0,
                "output": {"n_ingested": len(results)},
                "error": None,
            }
        )


class LoggingWorker:
    def __init__(self, agent: AnalystWorkerAgent, events: list[dict], label: str):
        self.agent = agent
        self.events = events
        self.label = label
        self.calls = 0

    async def analyze(self, task, *, peer_reports=()):
        self.calls += 1
        report = await self.agent.analyze(task, peer_reports=peer_reports)
        self.events.append(
            _event(
                f"{self.label}_analyze_{self.calls}",
                self.agent,
                output=report.model_dump(mode="json"),
            )
        )
        return report


class LoggingLead:
    def __init__(self, agent: AnalystLeadAgent, events: list[dict]):
        self.agent = agent
        self.events = events

    async def judge_convergence(self, task, reports, *, round_index, max_rounds):
        verdict = await self.agent.judge_convergence(
            task, reports, round_index=round_index, max_rounds=max_rounds
        )
        self.events.append(
            _event(
                f"lead_judge_round_{round_index}",
                self.agent,
                output=verdict.model_dump(mode="json"),
            )
        )
        return verdict

    async def adjudicate(self, task, reports):
        report = await self.agent.adjudicate(task, reports)
        self.events.append(
            _event("lead_adjudicate", self.agent, output=report.model_dump(mode="json"))
        )
        return report


def _event(event: str, agent, *, output=None):
    handle = agent.model
    response = agent.last_response
    usage = response.usage if response else None
    return {
        "event": event,
        "role": handle.role,
        "model_member": handle.config.name,
        "provider": handle.config.provider,
        "model": handle.config.model,
        "status": "ok" if response else "not_called",
        "input_tokens": usage.input_tokens if usage else 0,
        "output_tokens": usage.output_tokens if usage else 0,
        "output": output,
        "error": None,
    }
