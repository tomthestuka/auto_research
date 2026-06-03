"""Deterministic tests for step-9 consensus."""

import asyncio
from pathlib import Path

import pytest

from auto_research.consensus import DebateToConverge
from auto_research.schemas import AnalysisTask, AnalystReport, ReviewVerdict
from auto_research.schemas.figures import FigureRef


class FakeWorker:
    def __init__(self, name: str):
        self.name = name
        self.calls = 0
        self.peer_counts: list[int] = []

    async def analyze(self, task: AnalysisTask, *, peer_reports=()):
        self.calls += 1
        self.peer_counts.append(len(peer_reports))
        return AnalystReport(
            summary=f"{self.name} round {self.calls}: {task.question}",
            findings=[f"{self.name} finding {self.calls}"],
        )


class FakeLead:
    def __init__(self, converge_on: int):
        self.converge_on = converge_on
        self.judged_rounds: list[int] = []
        self.final_reports: list[AnalystReport] = []

    async def judge_convergence(self, task, reports, *, round_index, max_rounds):
        self.judged_rounds.append(round_index)
        return ReviewVerdict(
            accepted=round_index >= self.converge_on,
            rationale=f"round {round_index}",
            needs_analysis_rework=round_index < self.converge_on,
        )

    async def adjudicate(self, task, reports):
        report = AnalystReport(
            summary=f"final from {len(reports)} reports",
            findings=[report.findings[0] for report in reports],
        )
        self.final_reports.append(report)
        return report


def test_debate_to_converge_stops_when_lead_accepts():
    workers = [FakeWorker("w1"), FakeWorker("w2")]
    lead = FakeLead(converge_on=1)

    final = asyncio.run(
        DebateToConverge(max_rounds=3).run(workers, lead, AnalysisTask(question="Plan replication."))
    )

    assert final.summary == "final from 2 reports"
    assert lead.judged_rounds == [1]
    assert [worker.calls for worker in workers] == [1, 1]


def test_debate_to_converge_revises_with_peer_reports():
    workers = [FakeWorker("w1"), FakeWorker("w2")]
    lead = FakeLead(converge_on=2)

    final = asyncio.run(
        DebateToConverge(max_rounds=3).run(workers, lead, AnalysisTask(question="Plan replication."))
    )

    assert final.findings == ["w1 finding 2", "w2 finding 2"]
    assert lead.judged_rounds == [1, 2]
    assert [worker.peer_counts for worker in workers] == [[0, 1], [0, 1]]


def test_debate_to_converge_hard_cap_adjudicates():
    workers = [FakeWorker("w1"), FakeWorker("w2")]
    lead = FakeLead(converge_on=99)

    final = asyncio.run(
        DebateToConverge(max_rounds=3).run(workers, lead, AnalysisTask(question="Plan replication."))
    )

    assert final.findings == ["w1 finding 3", "w2 finding 3"]
    assert lead.judged_rounds == [1, 2, 3]
    assert [worker.calls for worker in workers] == [3, 3]


def test_debate_to_converge_requires_worker():
    with pytest.raises(ValueError, match="at least one worker"):
        asyncio.run(
            DebateToConverge(max_rounds=3).run([], FakeLead(converge_on=1), AnalysisTask(question="x"))
        )


def _make_fig(name: str) -> FigureRef:
    from datetime import UTC, datetime
    return FigureRef(
        path=Path(f"/tmp/{name}.png"),
        caption=name,
        mime_type="image/png",
        produced_by="test",
        created_at=datetime.now(UTC),
        sha256="a" * 64,
    )


class FakeWorkerWithTools(FakeWorker):
    """Worker that also exposes execute_tools, returning a fixed figure per call."""

    def __init__(self, name: str, figures: list[FigureRef]):
        super().__init__(name)
        self._figures = figures
        self.is_alive = True

    async def execute_tools(self, task, registry, *, tool_retry_cap=3):
        from auto_research.schemas import ToolResult
        return [
            ToolResult(tool_name="fake_plot", stdout="", stderr="", exit_code=0, elapsed_s=0.0, figures=self._figures)
        ]


class FakeRegistry:
    pass


def test_tool_figures_propagate_to_final_report():
    """Tool FigureRefs must survive to the final adjudicated report."""
    fig_w1 = _make_fig("w1_plot")
    fig_w2 = _make_fig("w2_plot")
    workers = [FakeWorkerWithTools("w1", [fig_w1]), FakeWorkerWithTools("w2", [fig_w2])]
    lead = FakeLead(converge_on=1)

    final = asyncio.run(
        DebateToConverge(max_rounds=3, registry=FakeRegistry()).run(
            workers, lead, AnalysisTask(question="Propagate figures.")
        )
    )

    paths = {str(f.path) for f in final.figures}
    assert str(fig_w1.path) in paths
    assert str(fig_w2.path) in paths


def test_tool_figures_deduplicated_across_rounds():
    """Figures produced in multiple rounds are deduplicated by path."""
    fig = _make_fig("shared_plot")
    workers = [FakeWorkerWithTools("w1", [fig])]
    lead = FakeLead(converge_on=2)  # forces one revision round → two tool phases

    final = asyncio.run(
        DebateToConverge(max_rounds=3, registry=FakeRegistry()).run(
            workers, lead, AnalysisTask(question="Dedup figures.")
        )
    )

    assert len([f for f in final.figures if str(f.path) == str(fig.path)]) == 1
