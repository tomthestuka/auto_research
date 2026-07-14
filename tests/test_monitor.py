"""Deterministic tests for step-12 run monitoring."""

from __future__ import annotations

import asyncio
from io import StringIO
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

    # In-memory events are filtered by chatter (level<=5 kept), but the JSONL log
    # is always full fidelity: BOTH events are written regardless of chatter.
    assert [event.event for event in monitor.events] == ["analyst.visible"]
    assert (tmp_path / "run.jsonl").read_text(encoding="utf-8").count("\n") == 2
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


def test_monitor_streams_visible_event_summaries(tmp_path: Path) -> None:
    stream = StringIO()
    monitor = RunMonitor(chatter=7, jsonl_path=tmp_path / "stream.jsonl", stream=stream)

    asyncio.run(
        monitor.record(
            "analyst",
            "analyst.analyze.tools",
            level=7,
            summary="Analyst worker ran tools",
            actor="analyst_worker",
            data={
                "model": "claude-sonnet-4-6",
                "calls": [
                    {"name": "load_data", "is_error": False},
                    {"name": "run_fit", "is_error": True},
                ],
            },
        )
    )
    asyncio.run(
        monitor.record(
            "analyst",
            "analyst.analyze.output",
            level=10,
            summary="Hidden full model output",
        )
    )
    asyncio.run(
        monitor.record(
            "analyst",
            "analyst.analyze.tool_result",
            level=7,
            summary="analyst_worker tool run_fit returned",
            actor="analyst_worker",
            data={
                "model": "claude-sonnet-4-6",
                "tool": "run_fit",
                "is_error": False,
                "chars": 4321,
                "figure_count": 1,
            },
        )
    )

    text = stream.getvalue()
    assert "#0001 analyst(claude-sonnet-4-6) L7" in text
    assert "analyst.analyze.tools" in text
    assert "tools=load_data,run_fit!" in text
    assert "tool=run_fit status=ok chars=4321 figures=1" in text
    assert "Hidden full model output" not in text
    assert (tmp_path / "stream.jsonl").read_text(encoding="utf-8").count("\n") == 3


def test_consensus_records_worker_and_lead_events() -> None:
    monitor = RunMonitor(chatter=10)

    class Worker:
        async def analyze(self, task, *, peer_reports=()):
            return AnalystReport(summary="worker report", findings=["f"])

    class Lead:
        async def judge_convergence(self, task, reports, *, round_index, max_rounds):
            return ReviewVerdict(accepted=True, rationale="done")

        async def adjudicate(self, task, reports, *, sources=None):
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


def test_run_research_returns_monitor_with_fake_dependencies(tmp_path: Path, monkeypatch) -> None:
    # This test exercises run_research wiring, not launch preconditions — the deterministic
    # preflight (incl. the toolkit branch/clean check, which depends on the live
    # research_toolkit's current branch) is covered by tests/test_preflight.py and
    # tests/test_merge_flow.py. Stub it so this test stays independent of repo state.
    async def _noop_preflight(*, human_handler=None):
        return None

    monkeypatch.setattr("auto_research.graph.runtime.run_preflight", _noop_preflight)

    # Hermetic orchestration config so the wiring runs on any machine (no real analyze_env /
    # toolkit): analyze_env=None ⇒ the builtin MCP server uses sys.executable; an empty tmp
    # toolkit and a no-op env script keep it independent of the live config + repo state.
    _ini = tmp_path / "env.sh"
    _ini.write_text("#!/bin/bash\n", encoding="utf-8")
    _tk = tmp_path / "toolkit"
    _tk.mkdir()
    _hermetic_orch = {
        "toolkit_root": str(_tk), "analyze_env": None, "analyze_ini_sh": str(_ini),
        "disabled_tools": [], "mcp_servers": [],
    }
    monkeypatch.setattr(
        "auto_research.graph.runtime.load_orchestration_config", lambda *a, **k: _hermetic_orch
    )

    class IO:
        async def compose_search_strategy(self, request, *, follow_up=None):
            return SearchRequest(query=request.question, sources=["local"])

        async def search(self, request):
            return []

        async def ingest_results(self, results):
            pass

    class Retrieval:
        async def text_search(self, query, *, limit=10, require_local_path=False):
            return []

        async def vector_search(self, query, *, limit=10):
            raise NotImplementedError

    class Reviewer:
        async def review(self, request, *, report=None):
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
