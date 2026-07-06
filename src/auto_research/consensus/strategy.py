"""Debate-to-converge consensus strategy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from auto_research.monitor import RunMonitor
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    AuthoringDecision,
    DocumentRef,
    MergeFlowResult,
    ReviewVerdict,
    ToolResult,
    ToolSpec,
)
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

    async def decide_authoring(
        self,
        reports: Sequence[AnalystReport],
    ) -> AuthoringDecision:
        """Decide whether to author a tool for the workers' ``tool_request``s (§9.3)."""

    async def drive_authoring(
        self,
        tool_spec: ToolSpec,
        *,
        toolkit_root: Path,
        worktree_base: Path,
        direct_branch: str | None = None,
        colead: Any | None = None,
        human_handler: Any | None = None,
    ) -> MergeFlowResult:
        """Drive one tool through authoring via the lead's own ReAct loop (§9.3 B-topology):
        the lead steers the coder + submits to gate/review and diagnoses each verdict, owning the
        session lifecycle. Returns the terminal ``MergeFlowResult``."""

    async def finalize_authoring_outcome(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        tool_spec: ToolSpec,
        result: MergeFlowResult,
    ) -> AnalystReport:
        """Compose the terminal report after an authoring attempt did not commit (§9.6)."""


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

    async def run(
        self,
        workers: Sequence[WorkerLike],
        lead: LeadLike,
        task: AnalysisTask,
    ) -> AnalystReport:
        """Run worker debate until convergence or the hard round cap, authoring tools as needed.

        Workers gather evidence via their own tool loops inside ``analyze()``. When ≥1 report
        carries a ``tool_request`` and authoring is wired, the lead decides
        (``decide_authoring``) and — on a committed tool — the registry is reloaded, sandboxes
        restarted, the task augmented with provenance, and workers rerun (a *resume*, bounded
        by ``tool_author_cap``, not counting toward ``max_rounds``). A truncated shared summary
        of each worker's last tool results is injected into task instructions before each
        subsequent round.
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
            # --- authoring / resume phase (§9.10 step 7, A8) — one decision per reports set; the
            # lead's whole tool chain is authored, then workers rerun once and loop back here so a
            # gap the chain plan missed can re-drive a follow-up chain (bounded by budget). ---
            if author_budget > 0 and any(r.tool_request is not None for r in reports):
                resumed, final_report, reports, task, author_budget = (
                    await self._maybe_author_and_resume(
                        workers, lead, task, reports, author_budget
                    )
                )
                if final_report is not None:
                    # An authoring attempt did not commit: the lead composed the terminal report
                    # (§9.6). Return it — the reviewer adjudicates downstream (the reviewer is the
                    # sole I/O; no debate-spin, no re-authoring, no bypass).
                    return final_report
                if resumed:
                    continue  # fresh reports may surface the next dependency gap

            # --- debate phase ---
            debate_round += 1
            verdict = await lead.judge_convergence(
                task,
                reports,
                round_index=debate_round,
                max_rounds=self.max_rounds,
            )
            await _record(
                self.monitor,
                "analyst.lead.convergence",
                level=7,
                summary=f"Lead judged analyst debate round {debate_round}",
                actor="analyst_lead",
                data={"round_index": debate_round, "verdict": verdict},
            )
            if verdict.accepted or debate_round >= self.max_rounds:
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
                    data={"report": final, "round_index": debate_round},
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
                    data={"round_index": debate_round, "report": report},
                )
            reports = revised
            task = self._augment_with_peer_tools(workers, task)

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

    async def _maybe_author_and_resume(
        self,
        workers: Sequence[WorkerLike],
        lead: LeadLike,
        task: AnalysisTask,
        reports: list[AnalystReport],
        author_budget: int,
    ) -> tuple[bool, AnalystReport | None, list[AnalystReport], AnalysisTask, int]:
        """Author the lead's whole tool chain; resume on commit, finalize honestly on a non-commit (§9.6).

        The lead plans an **ordered dependency chain** (``decision.tool_specs``); this authors the
        tools **sequentially, one session at a time**, then reruns the workers **once** after the
        whole chain (A8) — not one tool then a full rerun per tool. Returns
        ``(resumed, final_report, reports, task, author_budget)``:
        - ``resumed=True`` — the chain committed (or budget was hit mid-chain) and the workers were
          rerun; loop back — a fresh gap the plan missed re-drives a follow-up chain.
        - ``final_report`` set — a chain tool did not commit; the lead composed the terminal honest
          report (``finalize_authoring_outcome`` on the failed tool) and ``run()`` returns it — the
          chain stops (downstream depends on it); the reviewer adjudicates downstream.
        - both falsy — the lead declined to author (a mis-called tool); fall through to debate.
        The LLM never issues git — this code calls ``merge_flow.propose_and_pr`` per tool (§9.9).
        """
        decision = await lead.decide_authoring(reports)
        await _record(
            self.monitor,
            "analyst.lead.authoring_decision",
            level=6,
            summary="Lead decided on tool authoring",
            actor="analyst_lead",
            data={"decision": decision},
        )
        if not decision.should_author or not decision.tool_specs:
            return False, None, reports, task, author_budget

        # A8: author the lead's whole ordered dependency chain sequentially, one session at a
        # time, BEFORE rerunning workers. Each tool commits atomically to the shared authoring
        # branch (direct-branch mode: worktree == toolkit_root), so the next tool's coder + its
        # Layer-B gate tests see the prior tool's committed code on disk — no registry reload or
        # sandbox restart is needed *between* chain tools (only the workers need discovery/REPL
        # refresh, once, before they rerun). B-topology (§9.3, A1): the lead drives the coder +
        # gate + review through its OWN ReAct loop per tool; the LLM never issues git —
        # build_authoring_session/approve_edit do (§9.9).
        committed = 0
        for spec in decision.tool_specs:
            if author_budget <= 0:
                break  # budget exhausted mid-chain — rerun workers on what did commit
            author_budget -= 1  # an authoring attempt is expensive — bound it regardless of outcome
            result = await lead.drive_authoring(
                spec,
                toolkit_root=self.toolkit_root,
                worktree_base=self.worktree_base or self.toolkit_root,
                direct_branch=self.direct_branch,
                colead=self.colead,
                human_handler=self.human_handler,
            )
            await _record(
                self.monitor,
                "analyst.tool.authored",
                level=5,
                summary=f"Tool authoring {result.status}: {spec.proposed_tool_name}",
                actor="analyst_lead",
                data={"result": result},
            )
            if result.status != "committed":
                # Honest failure (gate/review cap exhausted or human-block): downstream chain
                # tools depend on this one, so stop the chain and let the lead compose the
                # terminal report documenting the unavailable capability (§9.6) — no resume, no
                # re-authoring, no debate-spin. The reviewer adjudicates it downstream. Tools
                # that committed earlier in the chain stay on the branch for the human.
                final = await lead.finalize_authoring_outcome(task, reports, spec, result)
                return False, final, reports, task, author_budget
            # Provenance note per committed tool so reran workers know the whole chain landed.
            task = _augment_with_authored_tool(task, spec, result)
            committed += 1

        # Whole chain committed (or budget hit mid-chain): rerun workers ONCE. Refresh discovery
        # (orchestration env) + restart sandboxes (REPL re-imports the new code).
        if self.registry is not None and hasattr(self.registry, "reload"):
            self.registry.reload()
        for worker in workers:
            sandbox = getattr(worker, "sandbox", None)
            if sandbox is not None and hasattr(sandbox, "restart"):
                await sandbox.restart()
        reports = await self._initial_reports(workers, task)
        task = self._augment_with_peer_tools(workers, task)
        return True, None, reports, task, author_budget

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
