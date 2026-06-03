"""Deterministic tests for step-12 run monitoring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_research.consensus import DebateToConverge
from auto_research.graph.runtime import run_research
from auto_research.monitor import RunMonitor
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
)


def test_monitor_filters_by_chatter_and_groups(tmp_path: Path) -> None:
    monitor = RunMonitor(chatter=5, jsonl_path=tmp_path / "run.jsonl")

    asyncio.run(
        monitor.record(
            "analyst",
            "analyst.visible",
            level=5,
            summary="visible",
            data={"path": tmp_path},
        )
    )
    asyncio.run(
        monitor.record(
            "analyst",
            "analyst.hidden",
            level=6,
            summary="hidden",
        )
    )

    assert [event.event for event in monitor.events] == ["analyst.visible"]
    assert (tmp_path / "run.jsonl").read_text(encoding="utf-8").count("\n") == 1
    report = monitor.render_markdown()
    assert "## Analysts" in report
    assert "analyst.visible" in report
    assert "analyst.hidden" not in report


def test_monitor_records_async_events_in_sequence(tmp_path: Path) -> None:
    monitor = RunMonitor(chatter=10, jsonl_path=tmp_path / "async.jsonl")

    async def _record_many() -> None:
        await asyncio.gather(
            *[
                monitor.record("tooling", f"tooling.event_{index}", level=10, summary=str(index))
                for index in range(20)
            ]
        )

    asyncio.run(_record_many())

    sequences = [event.sequence for event in monitor.events]
    assert sequences == list(range(1, 21))
    assert (tmp_path / "async.jsonl").read_text(encoding="utf-8").count("\n") == 20


def test_consensus_records_worker_and_lead_events() -> None:
    monitor = RunMonitor(chatter=10)

    class Worker:
        async def analyze(self, task, *, peer_reports=()):
            return AnalystReport(summary="worker report", findings=["f"])

    class Lead:
        async def judge_convergence(self, task, reports, *, round_index, max_rounds):
            return ReviewVerdict(accepted=True, rationale="done")

        async def adjudicate(self, task, reports):
            return AnalystReport(summary="lead summary", findings=["final"])

    report = asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            [Worker()],
            Lead(),
            AnalysisTask(question="q"),
        )
    )

    assert report.summary == "lead summary"
    events = [event.event for event in monitor.events]
    assert "analyst.worker.initial_report" in events
    assert "analyst.lead.convergence" in events
    assert "analyst.lead.adjudication" in events


def test_run_research_returns_monitor_with_fake_dependencies(tmp_path: Path) -> None:
    class IO:
        async def compose_search_strategy(self, request, *, follow_up=None):
            return SearchRequest(query=request.question, sources=["local"])

        async def search(self, request):
            return []

        async def ingest_results(self, results):
            pass

    class Retrieval:
        async def text_search(self, query, *, limit=10):
            return []

        async def vector_search(self, query, *, limit=10):
            raise NotImplementedError

    class Reviewer:
        async def review(self, request, *, literature=(), report=None):
            return ReviewVerdict(
                accepted=report is not None,
                rationale="analysis done" if report is not None else "literature ok",
            )

    class Consensus:
        async def run(self, workers, lead, task):
            return AnalystReport(summary="fake analysis", findings=["ok"])

    run = asyncio.run(
        run_research(
            ResearchRequest(question="q"),
            reviewer=Reviewer(),
            io_agent=IO(),
            retrieval=Retrieval(),
            workers=[],
            lead=object(),
            consensus=Consensus(),
            max_iterations=1,
            chatter=10,
            monitor_jsonl=tmp_path / "monitor.jsonl",
            report_formats=[],
        )
    )

    assert run.verdict.accepted is True
    assert run.monitor.jsonl_path == tmp_path / "monitor.jsonl"
    events = [event.event for event in run.monitor.events]
    assert events[0] == "run.start"
    assert "librarian.search.strategy" in events
    assert "reviewer.analysis_review.verdict" in events
    assert events[-1] == "run.finish"
    assert "## Reviewer" in run.markdown()


def test_chatter_range_validation() -> None:
    with pytest.raises(ValueError, match="chatter"):
        RunMonitor(chatter=0)
    with pytest.raises(ValueError, match="chatter"):
        RunMonitor(chatter=11)
