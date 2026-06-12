"""Debate-to-converge consensus strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from auto_research.monitor import RunMonitor
from auto_research.schemas import AnalysisTask, AnalystReport, DocumentRef, ReviewVerdict, ToolResult
from auto_research.schemas.figures import FigureRef
from auto_research.storage import MemoryRecord


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
    """Bounded analyst debate with lead convergence checks.

    Each worker's ``analyze()`` now runs its own inner ReAct tool loop and returns
    a report grounded in real tool results (with the loop's figures attached), so
    this strategy no longer executes tools itself. Between rounds it injects a
    truncated cross-worker summary built from each worker's ``last_tool_results``
    into the shared task instructions, so workers can reason from each other's
    results. ``registry``/``tool_retry_cap`` are accepted for call-site
    compatibility but unused (the worker owns its sandbox + react loop).
    """

    max_rounds: int = 3
    monitor: RunMonitor | None = None
    registry: Any | None = None  # ToolkitRegistry | None — accepted, unused
    tool_retry_cap: int = 3  # accepted, unused (the worker's react loop self-corrects)

    async def run(
        self,
        workers: Sequence[WorkerLike],
        lead: LeadLike,
        task: AnalysisTask,
    ) -> AnalystReport:
        """Run worker debate until convergence or the hard round cap.

        Workers gather evidence via their own tool loops inside ``analyze()``; a
        truncated shared summary of each worker's last tool results is injected
        into task instructions before each subsequent round.
        """

        if not workers:
            raise ValueError("at least one worker is required")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")

        await _record(
            self.monitor,
            "analyst.consensus.start",
            level=5,
            summary="Analyst debate started",
            data={"task": task, "worker_count": len(workers), "max_rounds": self.max_rounds},
        )

        # --- initial reports (each worker runs its own tool loop internally) ---
        reports: list[AnalystReport] = []
        for index, worker in enumerate(workers):
            report = await worker.analyze(task)
            reports.append(report)
            await _record(
                self.monitor,
                "analyst.worker.initial_report",
                level=7,
                summary=f"Analyst worker {index + 1} produced initial report",
                actor=f"worker-{index + 1}",
                data={"report": report},
            )

        task = self._augment_with_peer_tools(workers, task)

        for round_index in range(1, self.max_rounds + 1):
            verdict = await lead.judge_convergence(
                task,
                reports,
                round_index=round_index,
                max_rounds=self.max_rounds,
            )
            await _record(
                self.monitor,
                "analyst.lead.convergence",
                level=7,
                summary=f"Lead judged analyst debate round {round_index}",
                actor="analyst_lead",
                data={"round_index": round_index, "verdict": verdict},
            )
            if verdict.accepted or round_index == self.max_rounds:
                final = await lead.adjudicate(task, reports)
                # Carry all tool figures onto the final report so the reviewer
                # receives them at the multimodal boundary (ReviewerAgent.review
                # passes images=report.figures).
                all_figures = _deduplicated([f for r in reports for f in r.figures])
                final = final.model_copy(update={"figures": all_figures})
                await _record(
                    self.monitor,
                    "analyst.lead.adjudication",
                    level=5,
                    summary="Lead synthesized final analyst report",
                    actor="analyst_lead",
                    data={"report": final, "round_index": round_index},
                )
                return final

            # --- revision round ---
            revised: list[AnalystReport] = []
            for index, worker in enumerate(workers):
                report = await worker.analyze(task, peer_reports=_peers_for(reports, index))
                revised.append(report)
                await _record(
                    self.monitor,
                    "analyst.worker.revision",
                    level=7,
                    summary=f"Analyst worker {index + 1} revised report",
                    actor=f"worker-{index + 1}",
                    data={"round_index": round_index, "report": report},
                )
            reports = revised
            task = self._augment_with_peer_tools(workers, task)

        raise RuntimeError("unreachable consensus loop exit")

    def _augment_with_peer_tools(
        self,
        workers: Sequence[WorkerLike],
        task: AnalysisTask,
    ) -> AnalysisTask:
        """Inject a truncated cross-worker tool-result summary into the task.

        Reads each worker's ``last_tool_results`` (populated by its ReAct loop).
        Returns the task unchanged when no worker produced any results.
        """
        all_results: list[list[ToolResult]] = [
            list(getattr(worker, "last_tool_results", []) or []) for worker in workers
        ]
        summary = _peer_tool_summary(all_results)
        if not summary:
            return task
        new_instructions = "\n\n".join(filter(None, [task.instructions, summary]))
        return AnalysisTask(
            question=task.question,
            instructions=new_instructions,
            previous_report=task.previous_report,
        )


def _deduplicated(figures: list[FigureRef]) -> list[FigureRef]:
    """Deduplicate figures by path, preserving first-seen order."""
    seen: set[str] = set()
    out: list[FigureRef] = []
    for f in figures:
        key = str(f.path)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _peers_for(reports: Sequence[AnalystReport], index: int) -> list[AnalystReport]:
    return [report for report_index, report in enumerate(reports) if report_index != index]


_TOOL_STDOUT_LIMIT = 400  # chars per result for cross-worker sharing


def _peer_tool_summary(all_results: list[list[ToolResult]]) -> str:
    """Build a truncated shared summary of all workers' tool results."""
    if not any(all_results):
        return ""
    parts: list[str] = ["Peer tool execution summary (truncated):"]
    for worker_index, results in enumerate(all_results):
        if not results:
            continue
        parts.append(f"  Worker {worker_index + 1}:")
        for r in results:
            status = "OK" if r.exit_code == 0 else f"FAILED({r.exit_code})"
            stdout_snippet = r.stdout[:_TOOL_STDOUT_LIMIT] if r.stdout else ""
            line = f"    {r.tool_name} [{status}]"
            if stdout_snippet:
                line += f": {stdout_snippet}"
            if r.figures:
                line += f"  figures={[str(f.path) for f in r.figures]}"
            parts.append(line)
    return "\n".join(parts)


class IOAbstractionWorkerLike(Protocol):
    """Worker interface for IO paper abstraction debate."""

    async def abstract_document(
        self,
        document: DocumentRef,
        *,
        peer_abstracts: Sequence[MemoryRecord] = (),
    ) -> MemoryRecord:
        """Produce or revise one memory abstraction for a document."""


class IOAbstractionLeadLike(Protocol):
    """Lead interface for IO paper abstraction debate."""

    async def judge_abstract_convergence(
        self,
        document: DocumentRef,
        abstracts: Sequence[MemoryRecord],
        *,
        round_index: int,
        max_rounds: int,
    ) -> ReviewVerdict:
        """Judge whether worker abstractions have converged."""

    async def adjudicate_abstract(
        self,
        document: DocumentRef,
        abstracts: Sequence[MemoryRecord],
    ) -> MemoryRecord:
        """Synthesize the final memory record."""


class IOAbstractionStrategy(Protocol):
    """IO abstraction consensus policy interface."""

    async def run(
        self,
        workers: Sequence[IOAbstractionWorkerLike],
        lead: IOAbstractionLeadLike,
        document: DocumentRef,
    ) -> MemoryRecord:
        """Run IO abstraction consensus and return a final memory record."""


@dataclass(frozen=True)
class IODebateToConverge:
    """Bounded IO abstraction debate with lead convergence checks."""

    max_rounds: int = 3
    monitor: RunMonitor | None = None

    async def run(
        self,
        workers: Sequence[IOAbstractionWorkerLike],
        lead: IOAbstractionLeadLike,
        document: DocumentRef,
    ) -> MemoryRecord:
        """Run worker debate until convergence or the hard round cap."""

        if not workers:
            raise ValueError("at least one worker is required")
        if self.max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")

        await _record(
            self.monitor,
            "librarian.consensus.start",
            level=5,
            summary="Librarian IO abstraction debate started",
            data={"document": document, "worker_count": len(workers), "max_rounds": self.max_rounds},
        )
        abstracts: list[MemoryRecord] = []
        for index, worker in enumerate(workers):
            abstract = await worker.abstract_document(document)
            abstracts.append(abstract)
            await _record(
                self.monitor,
                "librarian.worker.initial_abstract",
                level=7,
                summary=f"Librarian IO worker {index + 1} produced initial abstraction",
                actor=f"io-worker-{index + 1}",
                data={"memory": abstract},
            )
        for round_index in range(1, self.max_rounds + 1):
            verdict = await lead.judge_abstract_convergence(
                document,
                abstracts,
                round_index=round_index,
                max_rounds=self.max_rounds,
            )
            await _record(
                self.monitor,
                "librarian.lead.convergence",
                level=7,
                summary=f"IO lead judged abstraction round {round_index}",
                actor="librarian_io_lead",
                data={"round_index": round_index, "verdict": verdict},
            )
            if verdict.accepted or round_index == self.max_rounds:
                final = await lead.adjudicate_abstract(document, abstracts)
                await _record(
                    self.monitor,
                    "librarian.lead.adjudication",
                    level=5,
                    summary="IO lead synthesized final memory record",
                    actor="librarian_io_lead",
                    data={"memory": final, "round_index": round_index},
                )
                return final
            revised: list[MemoryRecord] = []
            for index, worker in enumerate(workers):
                abstract = await worker.abstract_document(
                    document,
                    peer_abstracts=_peers_for_io(abstracts, index),
                )
                revised.append(abstract)
                await _record(
                    self.monitor,
                    "librarian.worker.revision",
                    level=7,
                    summary=f"Librarian IO worker {index + 1} revised abstraction",
                    actor=f"io-worker-{index + 1}",
                    data={"round_index": round_index, "memory": abstract},
                )
            abstracts = revised

        raise RuntimeError("unreachable IO consensus loop exit")


def _peers_for_io(abstracts: Sequence[MemoryRecord], index: int) -> list[MemoryRecord]:
    return [a for i, a in enumerate(abstracts) if i != index]


async def _record(
    monitor: RunMonitor | None,
    event: str,
    *,
    level: int,
    summary: str,
    actor: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    if monitor is not None:
        team = event.split(".", 1)[0]
        await monitor.record(team, event, level=level, summary=summary, actor=actor, data=data)
