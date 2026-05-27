"""Deterministic tests for step-9 consensus."""

import asyncio

import pytest

from auto_research.consensus import DebateToConverge
from auto_research.schemas import AnalysisTask, AnalystReport, ReviewVerdict


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
