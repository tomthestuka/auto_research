"""Deterministic tests for step-9 consensus."""

import asyncio
from pathlib import Path

import pytest

from auto_research.agents.react import (
    TURN_END_TOOL,
    LeadCycleOutcome,
)
from auto_research.consensus import DebateToConverge
from auto_research.schemas import AnalysisTask, AnalystReport
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
    """A lead that files one form per cycle, the way ``run_cycle`` does.

    It mirrors the production handler in the one place that matters here: another round is only
    filed while ``rounds_left`` allows one, because the real ``request_another_round`` handler
    refuses it once the cap is spent (the cap is Python's, not the model's).
    """

    def __init__(self, converge_on: int):
        self.converge_on = converge_on
        self.cycles = 0
        self.judged_rounds: list[int] = []
        self.rounds_left_seen: list[int] = []
        self.final_reports: list[AnalystReport] = []
        self.adjudicate_sources: list[str] | None = None
        self.adjudicate_reports: list[AnalystReport] = []

    async def run_cycle(
        self, task, reports, *, sources=None, rounds_left=0, author_budget=0, **kwargs
    ):
        self.cycles += 1
        self.judged_rounds.append(self.cycles)
        self.rounds_left_seen.append(rounds_left)
        if self.cycles < self.converge_on and rounds_left > 0:
            # A3: the report IS the decision — `rework` asks for another round, and the
            # probe reaches every worker verbatim.
            return LeadCycleOutcome(
                filed=TURN_END_TOOL,
                report=AnalystReport(
                    summary=f"round {self.cycles}: not settled",
                    rework=True,
                    rationale=f"round {self.cycles}",
                    what_to_probe="refit the segment",
                ),
                author_budget=author_budget,
            )
        # V-4: the consensus hands the lead the worker source keys; the lead owns figures.
        self.adjudicate_sources = list(sources) if sources is not None else None
        self.adjudicate_reports = list(reports)
        report = AnalystReport(
            summary=f"final from {len(reports)} reports",
            findings=[report.findings[0] for report in reports],
        )
        self.final_reports.append(report)
        return LeadCycleOutcome(
            filed=TURN_END_TOOL, report=report, author_budget=author_budget
        )


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
        produced_by="research_toolkit.t.test",
        created_at=datetime.now(UTC),
        sha256="a" * 64,
    )


class FakeWorkerWithTools(FakeWorker):
    """Worker whose analyze() returns a report carrying tool figures.

    Mirrors the ReAct shape: the worker runs its own tool loop inside analyze(),
    attaches the loop's figures to its report, and exposes last_tool_results for
    the consensus peer summary.
    """

    def __init__(self, name: str, figures: list[FigureRef]):
        super().__init__(name)
        self._figures = figures
        self.is_alive = True
        self.last_tool_results: list = []

    async def analyze(self, task: AnalysisTask, *, peer_reports=()):
        from auto_research.schemas import ToolResult

        self.calls += 1
        self.peer_counts.append(len(peer_reports))
        self.last_tool_results = [
            ToolResult(tool_name="fake_plot", stdout="", stderr="", exit_code=0,
                       elapsed_s=0.0, figures=self._figures)
        ]
        return AnalystReport(
            summary=f"{self.name} round {self.calls}: {task.question}",
            findings=[f"{self.name} finding {self.calls}"],
            figures=list(self._figures),
        )


class FakeRegistry:
    pass


def test_consensus_feeds_worker_figures_and_sources_to_lead():
    """V-4: the consensus hands the lead each worker's report + its source key, and returns the
    lead's report VERBATIM (no last-round figure force-carry — the lead now owns citation)."""
    fig_w1 = _make_fig("w1_plot")
    fig_w2 = _make_fig("w2_plot")
    w1 = FakeWorkerWithTools("w1", [fig_w1])
    w2 = FakeWorkerWithTools("w2", [fig_w2])
    w1.agent_key, w2.agent_key = "analyst_worker_0", "analyst_worker_1"
    lead = FakeLead(converge_on=1)

    final = asyncio.run(
        DebateToConverge(max_rounds=3, registry=FakeRegistry()).run(
            [w1, w2], lead, AnalysisTask(question="Propagate figures.")
        )
    )

    # source keys are aligned to the reports by worker index
    assert lead.adjudicate_sources == ["analyst_worker_0", "analyst_worker_1"]
    # the reports handed to the lead carried each worker's figures (the lead accumulates + cites)
    seen = {str(f.path) for r in lead.adjudicate_reports for f in r.figures}
    assert str(fig_w1.path) in seen and str(fig_w2.path) in seen
    # the strategy returns the lead's report unmodified — no figure overwrite
    assert final is lead.final_reports[-1]


def test_consensus_source_keys_fall_back_to_positional():
    """Workers without an agent_key key positionally (analyst_worker_<i>)."""
    workers = [FakeWorker("w1"), FakeWorker("w2")]
    lead = FakeLead(converge_on=1)
    asyncio.run(
        DebateToConverge(max_rounds=3).run(workers, lead, AnalysisTask(question="x"))
    )
    assert lead.adjudicate_sources == ["analyst_worker_0", "analyst_worker_1"]
