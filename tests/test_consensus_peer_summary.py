"""B8: the debate's peer-tool summary is built per worker per round — it no longer stacks in the
shared task, never carries a worker's own calls back to it, and never reaches the lead.
Driven through ``DebateToConverge.run``; the workers and the lead are fakes of the debate's
protocol (their own tests drive the real ones)."""

from __future__ import annotations

import asyncio

from auto_research.consensus import DebateToConverge
from auto_research.schemas import AnalysisTask, AnalystReport, ToolResult
from tests.test_consensus import FakeLead


class _Worker:
    def __init__(self, key: str):
        self.agent_key = key
        self.tasks: list[AnalysisTask] = []
        self.last_tool_results: list[ToolResult] = []

    async def analyze(self, task, *, peer_reports=()):
        self.tasks.append(task)
        n = len(self.tasks)
        self.last_tool_results = [ToolResult(
            tool_name=f"{self.agent_key}_tool", stdout=f"{self.agent_key} output round {n}",
            stderr="", exit_code=0, elapsed_s=0.1,
        )]
        return AnalystReport(summary=f"{self.agent_key} {n}", findings=[f"{self.agent_key} f{n}"],
                             status="succeeded")


class _Lead(FakeLead):
    def __init__(self, converge_on):
        super().__init__(converge_on)
        self.tasks: list[AnalysisTask] = []

    async def run_cycle(self, task, reports, **kwargs):
        self.tasks.append(task)
        return await super().run_cycle(task, reports, **kwargs)


def test_each_worker_sees_its_peers_last_round_once_and_never_its_own():
    workers = [_Worker("analyst_worker_0"), _Worker("analyst_worker_1")]
    lead = _Lead(converge_on=3)
    asyncio.run(DebateToConverge(max_rounds=3).run(workers, lead, AnalysisTask(question="q")))
    w0, w1 = workers
    assert len(w0.tasks) == 3
    assert "Peer tool execution summary" not in (w0.tasks[0].instructions or "")
    for round_ in (1, 2):
        text = w0.tasks[round_].instructions or ""
        assert text.count("Peer tool execution summary") == 1, "one summary, not stacked"
        assert "analyst_worker_1_tool" in text and "analyst_worker_0_tool" not in text
        assert f"analyst_worker_1 output round {round_}" in text, "the peers' latest round"
    assert "analyst_worker_0 output" in (w1.tasks[1].instructions or "")
    assert all("Peer tool execution summary" not in (t.instructions or "") for t in lead.tasks)


def test_every_worker_sees_its_peers_previous_round_whatever_its_position():
    """The summary is a snapshot of the round before: a worker that runs after its peers in this
    round still sees their previous round, never the one they just ran."""
    workers = [_Worker(f"analyst_worker_{i}") for i in range(3)]
    asyncio.run(DebateToConverge(max_rounds=3).run(workers, _Lead(converge_on=3),
                                                   AnalysisTask(question="q")))
    for me, worker in enumerate(workers):
        for round_ in (1, 2):
            text = worker.tasks[round_].instructions or ""
            for peer in range(3):
                if peer == me:
                    assert f"analyst_worker_{me}_tool" not in text
                    continue
                assert f"analyst_worker_{peer} output round {round_}" in text
                assert f"analyst_worker_{peer} output round {round_ + 1}" not in text


def test_the_task_keeps_its_other_fields():
    """The per-worker task is a copy of the shared one with the summary added, nothing lost."""
    workers = [_Worker("analyst_worker_0"), _Worker("analyst_worker_1")]
    task = AnalysisTask(question="q", instructions="base instructions")
    asyncio.run(DebateToConverge(max_rounds=2).run(workers, _Lead(converge_on=2), task))
    revised = workers[0].tasks[1]
    assert revised.question == "q" and revised.instructions.startswith("base instructions")
