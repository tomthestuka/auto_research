"""Deterministic tests for step-12 run monitoring."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from auto_research.consensus import DebateToConverge
from auto_research.graph.runtime import run_research
from auto_research.monitor import MonitorEvent, RunMonitor, build_run_summary, render_run_summary
from auto_research.agents.react import (
    ANOTHER_ROUND_TOOL,
    SUBMIT_ADJUDICATION_TOOL,
    LeadCycleOutcome,
)
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


def test_stream_line_carries_accumulated_cost_column(tmp_path: Path) -> None:
    """With a meter attached, every line shows the run's accumulated spend after `#seq`."""

    class Meter:
        total_usd = 12.3456

    stream = StringIO()
    monitor = RunMonitor(
        chatter=7, jsonl_path=tmp_path / "cost.jsonl", stream=stream, cost_meter=Meter()
    )

    asyncio.run(monitor.record("analyst", "analyst.step", level=5, summary="did a thing"))

    assert "#0001 $12.35 analyst L5 analyst.step: did a thing" in stream.getvalue()
    assert monitor.events[0].cost_usd_total == 12.3456
    # Markdown carries the same figure per event plus a header total.
    report = monitor.render_markdown()
    assert "- Accumulated cost: $12.3456" in report
    assert "- Cost so far: $12.3456" in report


def test_stream_line_unchanged_without_a_meter(tmp_path: Path) -> None:
    """No meter (unit tests, replayed old logs) → the pre-cost line format, byte for byte."""
    stream = StringIO()
    monitor = RunMonitor(chatter=7, jsonl_path=tmp_path / "plain.jsonl", stream=stream)

    asyncio.run(monitor.record("analyst", "analyst.step", level=5, summary="did a thing"))

    assert "#0001 analyst L5 analyst.step: did a thing" in stream.getvalue()
    assert monitor.events[0].cost_usd_total is None
    assert "Cost so far" not in monitor.render_markdown()


def test_legacy_monitor_line_validates_without_the_cost_field() -> None:
    """An event written before cost stamping still loads (the field defaults to None)."""
    line = (
        '{"run_id":"r","sequence":1,"created_at":"2026-07-01T00:00:00Z","team":"run",'
        '"event":"run.start","level":1,"summary":"started","actor":null,"data":{}}'
    )
    event = MonitorEvent.model_validate_json(line)
    assert event.cost_usd_total is None


def test_second_monitor_over_the_same_ledger_appends_and_continues_numbering(tmp_path: Path) -> None:
    """A resumed run must NEVER erase its ledger — only `overwrite` (whole-dir) is destructive.

    Truncating on construction threw away everything recorded before an interruption: the
    durable-resume evidence a reader looks for *after* the restart, and (since cost joined the
    ledger) all spend booked before it.
    """
    path = tmp_path / "monitor.jsonl"
    first = RunMonitor(chatter=10, jsonl_path=path, run_id="r1")
    asyncio.run(first.record("run", "run.start", level=1, summary="started"))
    asyncio.run(first.record("analyst", "analyst.step", level=5, summary="working"))

    resumed = RunMonitor(chatter=10, jsonl_path=path, run_id="r1")  # what --resume constructs
    asyncio.run(resumed.record("run", "run.resume", level=1, summary="resumed"))

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [r["event"] for r in rows] == ["run.start", "analyst.step", "run.resume"]
    assert [r["sequence"] for r in rows] == [1, 2, 3]  # monotonic: readers key on (run_id, sequence)


def test_append_survives_a_torn_final_line(tmp_path: Path) -> None:
    """A hard kill can stop mid-write; the fragment stays its own line, the next event is intact.

    Numbering continues from the last *parseable* event (a torn record cannot be read, and no
    reader counts it), and the new event is never glued onto the fragment.
    """
    path = tmp_path / "monitor.jsonl"
    good = json.dumps({
        "run_id": "r", "sequence": 7, "created_at": "2026-07-01T00:00:00+00:00", "team": "run",
        "event": "analyst.step", "level": 5, "summary": "working", "data": {},
    })
    path.write_text(good + '\n{"run_id":"r","sequence":8,"team":"run","ev', encoding="utf-8")

    monitor = RunMonitor(chatter=10, jsonl_path=path, run_id="r")
    asyncio.run(monitor.record("run", "run.resume", level=1, summary="resumed"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3  # good line · the fragment, still its own line · the new event
    assert json.loads(lines[2])["sequence"] == 8  # continued from the last readable event


def test_write_jsonl_refuses_to_overwrite_the_live_ledger(tmp_path: Path) -> None:
    """The export path must not replace the full-fidelity log with chatter-filtered events."""
    path = tmp_path / "monitor.jsonl"
    monitor = RunMonitor(chatter=4, jsonl_path=path)
    asyncio.run(monitor.record("run", "cost.call", level=10, summary="hidden from memory"))

    with pytest.raises(ValueError, match="append-only"):
        monitor.write_jsonl(path)

    copy = monitor.write_jsonl(tmp_path / "copy.jsonl")
    assert copy.exists()
    assert path.read_text(encoding="utf-8").count("cost.call") == 1  # live ledger untouched


def test_run_summary_derives_cost_tools_and_reports_from_the_event_stream() -> None:
    """The digest is computed from the ledger — nothing is passed in at the call site."""
    base = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

    def event(seq, name, *, data=None, minutes=0, cost=None, level=5):
        return {
            "run_id": "run-x", "sequence": seq, "team": "run", "event": name, "level": level,
            "summary": name, "data": data or {}, "cost_usd_total": cost,
            "created_at": (base + timedelta(minutes=minutes)).isoformat(),
        }

    events = [
        event(1, "run.start", cost=0.0, minutes=0,
              data={"run_dir": "/runs/x", "monitor_jsonl": "/runs/x/monitor.jsonl"}),
        event(2, "cost.call", data={"role": "analyst_worker", "model": "m1",
                                    "total_cost_usd": 2.0}, cost=2.0, minutes=1, level=10),
        event(3, "cost.call", data={"role": "coder", "model": "sonnet", "total_cost_usd": 3.0,
                                    "price_source": "claude_sdk"}, cost=5.0, minutes=2, level=10),
        event(4, "coder.session_start", minutes=3),
        event(5, "gate.result", data={"passed": False}, minutes=4),
        event(6, "gate.result", data={"passed": True}, minutes=5),
        event(7, "review.result", data={"approved": True}, minutes=6),
        event(8, "commit", data={"tool": "ixpe_qu_dcf", "commit_sha": "abcdef1234567"}, minutes=7),
        event(9, "analyst.tool_result", data={"tool": "run_fit", "is_error": True}, minutes=8),
        event(10, "analyst.tool_call", data={"tool": "ask_human"}, minutes=9),
        event(11, "reviewer.analysis_review.verdict",
              data={"verdict": {"accepted": True, "rationale": "sound"}}, minutes=10),
        event(12, "run.report", data={"report_pdf": "/runs/x/report.pdf"}, minutes=11),
        event(13, "run.finish", data={"lit_iterations": 1, "analysis_iterations": 2},
              cost=5.0, minutes=12),
    ]

    summary = build_run_summary(events)

    assert summary["status"] == "accepted"
    assert summary["cost_usd"] == 5.0 and summary["llm_calls"] == 2
    assert summary["coder_usd"] == 3.0
    assert summary["cost_by_role"] == {"analyst_worker": 2.0, "coder": 3.0}
    assert summary["tools_authored"] == [{"tool": "ixpe_qu_dcf", "commit_sha": "abcdef1234"}]
    assert summary["authoring"] == {"sessions": 1, "gate_passed": 1, "gate_rejected": 1,
                                    "review_approved": 1, "review_rejected": 0}
    assert (summary["tool_calls"], summary["tool_errors"], summary["hitl_raises"]) == (1, 1, 1)
    assert summary["analysis_iterations"] == 2
    assert summary["wall_clock_s"] == 12 * 60

    text = render_run_summary(summary)
    assert "RUN SUMMARY  run-x  —  accepted" in text
    assert "$5.0000 over 2 LLM calls  (coder SDK $3.00)" in text
    assert "ixpe_qu_dcf → abcdef1234" in text
    assert "12m 00s" in text
    # Findable artifacts: one labelled line per report path, plus where the run itself lives.
    assert "report pdf   /runs/x/report.pdf" in text
    assert "run dir      /runs/x" in text
    assert "ledger       /runs/x/monitor.jsonl" in text


def test_run_summary_of_a_crashed_run_reports_the_error_and_the_spend() -> None:
    events = [
        {"run_id": "r", "sequence": 1, "created_at": "2026-07-01T12:00:00+00:00", "team": "run",
         "event": "run.start", "level": 1, "summary": "s", "data": {}, "cost_usd_total": 0.0},
        {"run_id": "r", "sequence": 2, "created_at": "2026-07-01T12:30:00+00:00", "team": "run",
         "event": "run.error", "level": 1, "summary": "failed",
         "data": {"error": "BadRequestError(...)"}, "cost_usd_total": 7.5},
    ]
    summary = build_run_summary(events)
    assert summary["status"] == "error" and summary["cost_usd"] == 7.5
    text = render_run_summary(summary)
    assert "error" in text and "BadRequestError" in text


def test_stream_renders_the_summary_event_as_a_block(tmp_path: Path) -> None:
    """`run.summary` prints as the digest block, so a live/detached/notebook tail ends readable."""
    stream = StringIO()
    monitor = RunMonitor(chatter=10, jsonl_path=tmp_path / "s.jsonl", stream=stream)

    asyncio.run(
        monitor.record(
            "run", "run.summary", level=1, summary="Run summary: accepted, $1.0000",
            data={"run_id": "r", "status": "accepted", "cost_usd": 1.0, "llm_calls": 3},
        )
    )

    text = stream.getvalue()
    assert "RUN SUMMARY  r  —  accepted" in text
    assert "$1.0000 over 3 LLM calls" in text


def test_consensus_records_worker_and_lead_events() -> None:
    monitor = RunMonitor(chatter=10)

    class Worker:
        async def analyze(self, task, *, peer_reports=()):
            return AnalystReport(summary="worker report", findings=["f"])

    class Lead:
        """Files one more round, then adjudicates — so both lead events fire."""

        def __init__(self):
            self.cycles = 0

        async def run_cycle(self, task, reports, *, sources=None, rounds_left=0, **kwargs):
            self.cycles += 1
            if self.cycles == 1 and rounds_left > 0:
                return LeadCycleOutcome(
                    filed=ANOTHER_ROUND_TOOL,
                    verdict=ReviewVerdict(
                        accepted=False, rationale="not yet", needs_analysis_rework=True,
                        follow_up="probe further",
                    ),
                )
            return LeadCycleOutcome(
                filed=SUBMIT_ADJUDICATION_TOOL,
                report=AnalystReport(summary="lead summary", findings=["final"]),
            )

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
    assert "analyst.lead.cycle" in events        # one event per filed form
    assert "analyst.lead.convergence" in events  # the round it sent back
    assert "analyst.lead.adjudication" in events


def test_run_research_returns_monitor_with_fake_dependencies(tmp_path: Path, monkeypatch) -> None:
    # This test exercises run_research wiring, not launch preconditions — the deterministic
    # preflight (incl. the toolkit branch/clean check, which depends on the live
    # research_toolkit's current branch) is covered by tests/test_preflight.py and
    # tests/test_merge_flow.py. Stub it so this test stays independent of repo state.
    async def _noop_preflight(*, human_handler=None, monitor=None):
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
    # The run closes on the derived digest, one event after the finish record.
    assert events[-2:] == ["run.finish", "run.summary"]
    assert "## Reviewer" in run.markdown()
    # A summary is available for the caller (and printed to the stream) — derived, never hand-built.
    summary = run.summary()
    assert "RUN SUMMARY" in summary and "accepted" in summary
    assert "cost" in summary and "wall clock" in summary


def test_chatter_range_validation() -> None:
    with pytest.raises(ValueError, match="chatter"):
        RunMonitor(chatter=0)
    with pytest.raises(ValueError, match="chatter"):
        RunMonitor(chatter=11)


def test_summary_total_sums_priced_calls_across_resume_segments() -> None:
    """The headline must not be the highest stamped total: a resumed segment restarts its stamps."""
    def event(seq, name, *, data=None, cost=None, level=5):
        return {
            "run_id": "r", "sequence": seq, "team": "run", "event": name, "level": level,
            "summary": name, "data": data or {}, "cost_usd_total": cost,
            "created_at": datetime(2026, 7, 29, 12, 0, seq, tzinfo=UTC).isoformat(),
        }

    events = [
        event(1, "cost.call", data={"role": "reviewer", "model": "m", "total_cost_usd": 8.80},
              cost=8.80, level=10),
        event(2, "run.resume", cost=0.0, level=1),                       # new process, meter at 0
        event(3, "cost.call", data={"role": "analyst_worker", "model": "m",
                                   "total_cost_usd": 0.18}, cost=0.18, level=10),
    ]

    summary = build_run_summary(events)

    assert summary["cost_usd"] == 8.98          # summed, not max(8.80, 0.18)
    assert summary["llm_calls"] == 2
    assert "$8.9800 over 2 LLM calls" in render_run_summary(summary)
