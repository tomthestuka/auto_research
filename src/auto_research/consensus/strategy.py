"""Debate-to-converge consensus strategy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from auto_research.schemas import AnalysisTask, AnalystReport, ReviewVerdict


class WorkerLike(Protocol):
    """Worker interface needed by consensus."""

    async def analyze(
        self,
        task: AnalysisTask,
        *,
        peer_reports: Sequence[AnalystReport] = (),
    ) -> AnalystReport:
        """Produce or revise one analysis report."""


class LeadLike(Protocol):
    """Lead interface needed by consensus."""

    async def judge_convergence(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        *,
        round_index: int,
        max_rounds: int,
    ) -> ReviewVerdict:
        """Judge whether worker reports have converged."""

    async def adjudicate(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
    ) -> AnalystReport:
        """Synthesize the final report."""


class ConsensusStrategy(Protocol):
    """Consensus policy interface."""

    async def run(
        self,
        workers: Sequence[WorkerLike],
        lead: LeadLike,
        task: AnalysisTask,
    ) -> AnalystReport:
        """Run consensus and return a final report."""


@dataclass(frozen=True)
class DebateToConverge:
    """Bounded analyst debate with lead convergence checks."""

    max_rounds: int = 3

    async def run(
        self,
        workers: Sequence[WorkerLike],
        lead: LeadLike,
        task: AnalysisTask,
    ) -> AnalystReport:
        """Run worker debate until convergence or the hard round cap."""

        if not workers:
            raise ValueError("at least one worker is required")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")

        reports = [await worker.analyze(task) for worker in workers]
        for round_index in range(1, self.max_rounds + 1):
            verdict = await lead.judge_convergence(
                task,
                reports,
                round_index=round_index,
                max_rounds=self.max_rounds,
            )
            if verdict.accepted or round_index == self.max_rounds:
                return await lead.adjudicate(task, reports)
            reports = [
                await worker.analyze(task, peer_reports=_peers_for(reports, index))
                for index, worker in enumerate(workers)
            ]

        raise RuntimeError("unreachable consensus loop exit")


def _peers_for(reports: Sequence[AnalystReport], index: int) -> list[AnalystReport]:
    return [report for report_index, report in enumerate(reports) if report_index != index]
