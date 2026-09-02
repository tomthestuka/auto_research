"""Debate-to-converge consensus strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from auto_research.monitor import RunMonitor
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    DocumentRef,
    ReviewVerdict,
    ToolResult,
)
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
    """Lead interface needed by consensus — one cycle, one call.

    The five separate actions this used to name (judge convergence, adjudicate, decide authoring,
    drive it, finalize an honest failure) are branches the lead now takes *inside* one ReAct loop
    by filing a form, so consensus asks for a cycle and reads which form came back. What consensus
    keeps is the counting: the round cap, the authoring budget, and the reload/restart/rerun.
    """

    async def run_cycle(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        *,
        sources: Sequence[str] | None = None,
        rounds_left: int = 0,
        author_budget: int = 0,
        toolkit_root: Path | None = None,
        worktree_base: Path | None = None,
        direct_branch: str | None = None,
        colead: Any | None = None,
        human_handler: Any | None = None,
    ) -> Any:
        """Run one cycle and return its :class:`~auto_research.agents.react.LeadCycleOutcome`.

        ``sources`` names each report's worker (V-4), so the lead keys its per-worker figure
        catalog and can cite any worker's figure from any round. ``rounds_left`` and
        ``author_budget`` are the caps consensus owns; the rest is the authoring wiring the lead
        needs to build a session per chain link (§9.9: that code, never the model, issues git).
        """


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
    registry: Any | None = None  # ToolkitRegistry | None — used for reload() after authoring
    tool_retry_cap: int = 3  # accepted, unused (the worker's react loop self-corrects)
    # --- Phase-2 step-7 authoring/resume (§9.10). All optional: when toolkit_root is unset
    # the strategy behaves exactly as Phase-1 debate (no authoring path). ---
    tool_author_cap: int = 3  # max authoring attempts per analysis (the 3-gap chain bound)
    toolkit_root: Path | None = None
    worktree_base: Path | None = None
    direct_branch: str | None = None  # runtime authoring branch (e.g. "ai"); None ⇒ worktree mode
    human_handler: Any | None = None
    colead: Any | None = None  # GPT co-lead; None ⇒ propose_and_pr builds one from config
    # Carried so the lead can publish its live authoring session for interrupt cleanup; a /stop is
    # never *polled* here — it cancels the run task (``RunControl.interrupt``).
    run_control: Any | None = None

    async def run(
        self,
        workers: Sequence[WorkerLike],
        lead: LeadLike,
        task: AnalysisTask,
    ) -> AnalystReport:
        """Run worker debate until the lead files a report, authoring tools as needed.

        Workers gather evidence via their own tool loops inside ``analyze()``. The lead then takes
        one **cycle** — a single ReAct loop over a single conversation (``run_cycle``) in which it
        investigates and ends by filing a form: ``author_chain``/``hold_authoring`` while a tool
        request is open, then ``request_another_round`` or a report terminal. This method acts on
        whichever form arrived and owns every count: the debate round cap, the authoring budget,
        the registry reload, the sandbox restart and the worker rerun. The loop chooses *what*;
        this chooses *how many times*.

        A cycle that authored a chain ends before judging, because a committed tool is evidence
        the current reports predate: the workers rerun **once** on whatever committed (or on the
        failure note) and the lead judges the fresh reports in the next cycle (A8/A11). Bounded by
        ``tool_author_cap``, which does not consume debate rounds. The rerun is driven by the
        **commit count**, not by which form arrived: an honest terminal filed from inside the
        authoring stage (§9.6) ends the chain, and its committed prefix still has to become
        callable before any report is final.
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
        reports = await self._initial_reports(workers, task)
        task = self._augment_with_peer_tools(workers, task)

        author_budget = self.tool_author_cap if self.toolkit_root is not None else 0
        debate_round = 0
        while True:
            outcome = await lead.run_cycle(
                task,
                reports,
                sources=_source_keys(workers),
                # Rounds the cap still allows AFTER this cycle — what request_another_round is
                # measured against, so the handler can refuse it with the cap named.
                rounds_left=max(0, self.max_rounds - debate_round - 1),
                author_budget=author_budget,
                toolkit_root=self.toolkit_root,
                worktree_base=self.worktree_base or self.toolkit_root,
                direct_branch=self.direct_branch,
                colead=self.colead,
                human_handler=self.human_handler,
            )
            author_budget = outcome.author_budget
            await _record(
                self.monitor,
                "analyst.lead.cycle",
                level=6,
                summary=f"Lead filed {outcome.filed or 'nothing'}",
                actor="analyst_lead",
                data={
                    "filed": outcome.filed,
                    "round_index": debate_round + 1,
                    "committed": outcome.committed,
                    "author_budget": author_budget,
                    "decision": outcome.decision,
                    "verdict": outcome.verdict,
                },
            )

            if outcome.authored_chain or outcome.committed >= 1:
                # Acting on the commit count, NOT only on which form was filed: an honest
                # terminal (§9.6) is reachable from inside the authoring stage, and filing it
                # there ends the CHAIN, never the analysis. Whatever committed still has to
                # become callable before any report is final — [A11] is the only thing that
                # makes a freshly authored tool usable in the same run, and gating the resume
                # on `authored_chain` alone let a mid-chain terminal discard it (observed live:
                # two gated, approved, committed tools returned `exit 127 Tool not registered`).
                # Terminates: each pass here needs ≥1 commit and every commit spends an author
                # credit, so `tool_author_cap` bounds how often this branch can be taken.
                reports, task = await self._resume_after_authoring(
                    workers, task, outcome, reports
                )
                continue

            if outcome.wants_another_round and debate_round + 1 < self.max_rounds:
                debate_round += 1
                await _record(
                    self.monitor,
                    "analyst.lead.convergence",
                    level=7,
                    summary=f"Lead sent the analysts back for round {debate_round + 1}",
                    actor="analyst_lead",
                    data={"round_index": debate_round, "verdict": outcome.verdict},
                )
                task = _augment_with_probe(task, outcome.verdict)
                reports = await self._revision_round(workers, task, reports, debate_round)
                task = self._augment_with_peer_tools(workers, task)
                continue

            if outcome.report is None:
                # No form filed and no report written: there is genuinely nothing the lead
                # produced. Composing one here would put a runtime-authored report into the
                # record, which sits closer to fabrication than the failure it hides ([F6]).
                raise RuntimeError(
                    "the analyst lead ended its cycle without filing a report "
                    f"(filed={outcome.filed!r}); no report was written to stand in for it"
                )
            await _record(
                self.monitor,
                "analyst.lead.adjudication",
                level=5,
                summary="Lead filed the final analyst report",
                actor="analyst_lead",
                data={
                    "report": outcome.report,
                    "round_index": debate_round + 1,
                    "filed": outcome.filed,
                },
            )
            return outcome.report

    async def _resume_after_authoring(
        self,
        workers: Sequence[WorkerLike],
        task: AnalysisTask,
        outcome: Any,
        reports_unchanged: Sequence[AnalystReport],
    ) -> tuple[list[AnalystReport], AnalysisTask]:
        """Rerun the workers once on whatever the chain committed (A8/A11).

        Each authored link leaves a provenance note on the task so a reran worker uses the new
        tool rather than re-reporting the gap, and a non-commit leaves a failure note so the
        lead's next cycle adapts instead of repeating the plan. Discovery is reloaded and the
        sandboxes restarted only when ≥1 tool committed — there is new code to import only then;
        a chain that committed nothing still reruns so the workers see the failure note.
        """
        specs = list(outcome.decision.tool_specs) if outcome.decision is not None else []
        for spec, result in zip(specs, outcome.authored):
            if result.status == "committed":
                task = _augment_with_authored_tool(task, spec, result)
            else:
                task = _augment_with_failed_tool(task, spec, result)
        if outcome.committed >= 1:
            if self.registry is not None and hasattr(self.registry, "reload"):
                self.registry.reload()
            for worker in workers:
                sandbox = getattr(worker, "sandbox", None)
                if sandbox is not None and hasattr(sandbox, "restart"):
                    await sandbox.restart()
        exhausted = outcome.committed == 0 and outcome.author_budget <= 0
        await _record(
            self.monitor,
            "analyst.tool.chain_resume",
            level=6,
            summary=(
                f"Resuming after authoring: committed={outcome.committed}, "
                f"budget_left={outcome.author_budget}"
            ),
            actor="analyst_lead",
            data={
                "committed": outcome.committed,
                "author_budget": outcome.author_budget,
                "rerunning_workers": not exhausted,
            },
        )
        if exhausted:
            # Credits spent with nothing ever committed: there is no capability to rerun on, and
            # rerunning would only re-surface the same gap at the cost of a full worker round. The
            # existing reports carry the failure notes, so the lead judges them and files its
            # honest terminal in the next cycle (§9.6).
            return list(reports_unchanged), task
        reports = await self._initial_reports(workers, task)
        return reports, self._augment_with_peer_tools(workers, task)

    async def _revision_round(
        self,
        workers: Sequence[WorkerLike],
        task: AnalysisTask,
        reports: list[AnalystReport],
        debate_round: int,
    ) -> list[AnalystReport]:
        """One debate round: every worker revises with its peers' reports in view."""
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
                data={"round_index": debate_round, "report": report},
            )
        return revised

    async def _initial_reports(
        self, workers: Sequence[WorkerLike], task: AnalysisTask
    ) -> list[AnalystReport]:
        """Each worker produces an initial report via its own tool loop."""
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
        return reports

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


def _augment_with_authored_tool(task: AnalysisTask, tool_spec: Any, result: Any) -> AnalysisTask:
    """Append authored-tool provenance to the task so reran workers know to use it (§9.10)."""
    sha = (getattr(result, "commit_sha", "") or "")[:12]
    note = (
        "A new toolkit capability was just authored and committed and is now available — "
        f"use it instead of reporting the gap again:\n"
        f"  tool: {tool_spec.proposed_tool_name} (domain: {tool_spec.target_domain}, "
        f"disposition: {tool_spec.disposition})\n"
        f"  provenance: branch {getattr(result, 'branch', '')}, commit {sha}\n"
        f"  intent: {tool_spec.expected_behavior}"
    )
    new_instructions = "\n\n".join(filter(None, [task.instructions, note]))
    return AnalysisTask(
        question=task.question,
        instructions=new_instructions,
        previous_report=task.previous_report,
    )


def _augment_with_failed_tool(task: AnalysisTask, tool_spec: Any, result: Any) -> AnalysisTask:
    """Append a note that an authoring attempt did NOT commit, so reran workers refine their
    request and the lead's next ``decide_authoring`` adapts (a different approach / revised brief)
    instead of repeating the failed plan (A11 recovery)."""
    reason = (getattr(result, "reason", "") or "").strip()
    note = (
        "An attempt to author a new tool did NOT commit — the capability is still missing. "
        "Consider a different approach or a revised brief before requesting it again:\n"
        f"  tool: {tool_spec.proposed_tool_name} (domain: {tool_spec.target_domain})\n"
        f"  outcome: {getattr(result, 'status', 'failed')}\n"
        f"  reason: {reason}"
    )
    new_instructions = "\n\n".join(filter(None, [task.instructions, note]))
    return AnalysisTask(
        question=task.question,
        instructions=new_instructions,
        previous_report=task.previous_report,
    )


def _augment_with_probe(task: AnalysisTask, verdict: Any) -> AnalysisTask:
    """Hand the lead's ``request_another_round`` dispatch text to every worker, verbatim.

    The form's description promises exactly that ("your dispatch text reaches every worker
    verbatim as their next instruction"), so the promise is kept here — a constraint stated in a
    tool description and unimplemented in code is worse than one never stated. Returns the task
    unchanged when the verdict carries no dispatch text.
    """
    probe = (getattr(verdict, "follow_up", None) or "").strip()
    if not probe:
        return task
    rationale = (getattr(verdict, "rationale", None) or "").strip()
    note = "\n".join(
        filter(
            None,
            [
                "The analyst lead reviewed this round and asks for one more, on this specifically:",
                f"  what to probe: {probe}",
                f"  why: {rationale}" if rationale else "",
            ],
        )
    )
    new_instructions = "\n\n".join(filter(None, [task.instructions, note]))
    return AnalysisTask(
        question=task.question,
        instructions=new_instructions,
        previous_report=task.previous_report,
    )


def _source_keys(workers: Sequence[Any]) -> list[str]:
    """Stable per-worker catalog source keys (V-4), aligned to the reports list by index.

    Uses each worker's ``agent_key`` (its working-folder name, set by runtime), falling back to a
    positional ``analyst_worker_<i>`` so a caller-supplied fake still keys cleanly.
    """
    return [getattr(w, "agent_key", None) or f"analyst_worker_{i}" for i, w in enumerate(workers)]


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
