"""LangGraph assembly for the Phase 1 research loop."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph

from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.state import ResearchState
from auto_research.monitor import RunMonitor
from auto_research.observations import ObservationsConfig, load_observations_config
from auto_research.provenance import ReceiptStore
from auto_research.report import publish_report
from auto_research.retrieval import RetrievalBackend
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.workspace import report_dir as _report_dir
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewerReport,
)
from auto_research.schemas.literature import LiteratureReport
from auto_research.schemas.artifacts import Receipt


class IOAgentLike(Protocol):
    """Minimal interface required of the librarian."""

    last_receipts: dict[str, Receipt]

    async def research(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> LiteratureReport:
        """Research the literature for ``request`` and return the report it filed."""


class ReviewerLike(Protocol):
    """Minimal interface used by the review nodes."""

    async def review(
        self,
        request: ResearchRequest,
        *,
        stage: str,
        literature: LiteratureReport | None = None,
        literature_receipts: Mapping[str, Receipt] | None = None,
        report: AnalystReport | None = None,
        iterations_left: int = 0,
        render: Any | None = None,
    ) -> ReviewerReport:
        """Run one review and return the report the reviewer filed through ``turn_end``."""


def _build_task(
    state: ResearchState,
    *,
    extra_instructions: Sequence[str] = (),
    obs_config: ObservationsConfig | None = None,
    tool_catalogue: str | None = None,
) -> AnalysisTask:
    """Reconstruct the current AnalysisTask from graph state."""
    request = state["request"]
    instructions_parts: list[str] = []
    if request.context:
        instructions_parts.append(request.context)
    prior = _latest_verdict(state)
    if prior and prior.follow_up:
        label = "Reviewer feedback for revision" if prior.needs_analysis_rework else "Reviewer dispatch"
        instructions_parts.append(f"{label}: {prior.follow_up}")
    if obs_config and obs_config.missions:
        obs_lines = ["Local observation databases (check these before querying remote archives):"]
        for mission, cfg in obs_config.missions.items():
            obs_lines.append(f"  {mission}: {cfg.obs_db_path}")
        instructions_parts.append("\n".join(obs_lines))
    if tool_catalogue:
        instructions_parts.append(f"Available sandbox tools:\n{tool_catalogue}")
    instructions_parts.extend(extra_instructions)
    return AnalysisTask(
        question=request.question,
        instructions="\n\n".join(instructions_parts) or None,
        previous_report=state.get("report"),
    )


def build_graph(
    *,
    reviewer: ReviewerLike,
    io_agent: IOAgentLike,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike],
    lead: LeadLike,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int = 3,
    registry: ToolkitRegistry | None = None,
    monitor: RunMonitor | None = None,
    run_id: str | None = None,
    receipts: ReceiptStore | None = None,
    report_formats: list[str] | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Compile the Phase 1 research-loop graph.

    Two outer feedback edges, each hard-capped at ``max_iterations``:
    - paper-search review: ``review_literature -> literature``
    - analysis review: ``review_analysis -> analysis``

    Tool execution is handled inside ``DebateToConverge`` via persistent
    per-worker sandboxes.  Pass ``registry`` (with the already-loaded
    ``ToolkitRegistry``) so the consensus strategy can execute tools and
    share truncated summaries across workers between debate rounds.

    The literature node runs the librarian's research loop, which searches, reads papers and
    pages in full, and files a ``LiteratureReport`` through ``turn_end``. The report and the
    documents it read (by handle) go into state for the reviewer.
    """

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")

    if registry is not None:
        _registry = registry
    else:
        # Standalone/Studio path (real runs always inject a registry). Read the toolkit root
        # from config rather than a hardcoded literal so it stays portable; lazy import keeps
        # builder ⇍ runtime (runtime imports build_graph) acyclic.
        from auto_research.graph.runtime import load_orchestration_config

        _registry = ToolkitRegistry(Path(load_orchestration_config()["toolkit_root"]))
    consensus_strategy = consensus or DebateToConverge(
        max_rounds=max_iterations, monitor=monitor, registry=_registry
    )
    try:
        _obs_config: ObservationsConfig | None = load_observations_config()
    except Exception:
        _obs_config = None

    async def literature_node(state: ResearchState) -> dict[str, Any]:
        await _record(
            monitor,
            "librarian.literature.start",
            level=3,
            summary="Literature node started",
            data={
                "lit_iterations": state.get("lit_iterations", 0),
                "question": state["request"].question,
            },
        )
        prior = _latest_verdict(state)
        follow_up = prior.follow_up if prior and prior.needs_more_literature else None

        report = await io_agent.research(state["request"], follow_up=follow_up)
        await _record(
            monitor,
            "librarian.literature.report",
            level=3,
            summary=(
                f"Librarian filed {len(report.findings)} finding(s), status {report.status}"
            ),
            actor="librarian_io",
            data={"report": report, "documents": sorted(io_agent.last_receipts)},
        )
        return {
            "literature": report,
            "literature_receipts": dict(io_agent.last_receipts),
            "lit_iterations": state.get("lit_iterations", 0) + 1,
        }

    async def review_literature_node(state: ResearchState) -> dict[str, Any]:
        await _record(
            monitor,
            "reviewer.literature_review.start",
            level=3,
            summary="Reviewer literature review started",
            actor="reviewer",
        )
        verdict = await reviewer.review(
            state["request"],
            stage="literature",
            literature=state.get("literature"),
            literature_receipts=state.get("literature_receipts") or {},
            iterations_left=max_iterations - state.get("lit_iterations", 0),
        )
        await _record(
            monitor,
            "reviewer.literature_review.verdict",
            level=2,
            summary="Reviewer filed its literature review",
            actor="reviewer",
            data={"verdict": verdict},
        )
        return {"verdicts": [verdict]}

    def after_review_literature(state: ResearchState) -> str:
        verdict = _latest_verdict(state)
        if (
            verdict is not None
            and verdict.needs_more_literature
            and state.get("lit_iterations", 0) < max_iterations
        ):
            return "literature"
        return "analysis"

    async def analysis_node(state: ResearchState) -> dict[str, Any]:
        task = _build_task(
            state,
            obs_config=_obs_config,
            tool_catalogue=_registry.tool_catalogue(),
        )
        await _record(
            monitor,
            "analyst.analysis.start",
            level=3,
            summary="Analyst consensus node started",
            data={
                "analysis_iterations": state.get("analysis_iterations", 0),
                "task": task,
            },
        )
        report = await consensus_strategy.run(workers, lead, task)
        await _record(
            monitor,
            "analyst.analysis.report",
            level=5,
            summary="Analyst consensus returned report",
            data={"report": report},
        )
        return {
            "report": report,
            "analysis_iterations": state.get("analysis_iterations", 0) + 1,
        }

    async def review_analysis_node(state: ResearchState) -> dict[str, Any]:
        """The analysis review, whose final report is rendered inside its own ``turn_end``.

        The ``.tex`` and PDF are therefore finished when the reviewer's loop returns; this node
        only records where they are, and any compile failure that was the renderer's rather than
        the model's, loudly (it never holds the reviewer in a loop it cannot leave).
        """
        from auto_research.agents.reviewer_tools import ReviewerRender

        await _record(
            monitor,
            "reviewer.analysis_review.start",
            level=3,
            summary="Reviewer analysis review started",
            actor="reviewer",
            data={"report": state.get("report")},
        )
        render = ReviewerRender(
            report_dir=_report_dir(_run_id), run_id=_run_id, formats=tuple(_report_formats),
        )
        verdict = await reviewer.review(
            state["request"],
            stage="analysis",
            literature=state.get("literature"),
            literature_receipts=state.get("literature_receipts") or {},
            report=state.get("report"),
            iterations_left=max_iterations - state.get("analysis_iterations", 0),
            render=render,
        )
        await _record(
            monitor,
            "reviewer.analysis_review.verdict",
            level=2,
            summary="Reviewer filed its analysis review",
            actor="reviewer",
            data={"verdict": verdict},
        )
        update: dict[str, Any] = {"verdicts": [verdict]}
        if not verdict.needs_analysis_rework and "tex" in _report_formats:
            tex = render.render.tex_path if render.render is not None else None
            pdf = render.compile.pdf_path if render.compile is not None else None
            update["report_latex"] = tex
            update["report_pdf"] = pdf
            if render.renderer_errors or pdf is None:
                await _record(
                    monitor,
                    "reviewer.compose_report.renderer_error",
                    level=1,
                    summary="The report was accepted but no PDF was produced — a renderer defect",
                    actor="reviewer",
                    data={"errors": render.renderer_errors, "tex": str(tex) if tex else None},
                )
        return update

    def after_review_analysis(state: ResearchState) -> str:
        verdict = _latest_verdict(state)
        if (
            verdict is not None
            and verdict.needs_analysis_rework
            and state.get("analysis_iterations", 0) < max_iterations
        ):
            return "analysis"
        return "finalize_run"

    async def finalize_run_node(state: ResearchState) -> dict[str, Any]:
        """Pass the reviewer's verdict through to report composition (§9.6).

        Every analysis — including an honest ``failed``/``needs_human`` outcome — is now
        adjudicated by the reviewer (the reviewer is the sole I/O; the analyst lead gets no
        bypass). This node only records the terminal and forwards the reviewer's verdict; the
        report is always composed (``route_after_finalize`` honours ``report_formats``), so an
        honest failure is *documented*, never silent. The no-fabrication guarantee lives in the
        reviewer's prompt and in its ``turn_end`` check, not in suppressing the report.
        """
        report = state.get("report")
        # An absent report is not a success (F9): the harness never infers a terminal state, and
        # `AnalystReport.status` now defaults to `failed` for the same reason.
        status = getattr(report, "status", "failed") if report is not None else "failed"
        verdict = _latest_verdict(state)
        if status != "succeeded":
            await _record(
                monitor,
                "run.honest_failure",
                level=1,
                summary=f"Analysis ended with honest status '{status}' — reviewer-adjudicated, documented",
                data={"status": status, "report": report, "verdict": verdict},
            )
        return {"verdict": verdict}

    _run_id = run_id or uuid.uuid4().hex
    _report_formats: list[str] = report_formats if report_formats is not None else ["jsonl", "tex"]

    async def compose_report_node(state: ResearchState) -> dict[str, Any]:
        """Publish the accepted report: receipts for its files, and the JSONL when configured.

        No model runs here. The reviewer wrote the report inside its loop and its ``turn_end``
        rendered and compiled the ``.tex``; what remains is making the report an artifact
        (``rev:dN`` receipts whose parents are the receipts it cites) and the machine handle.
        """
        verdict = state["verdict"]
        store = receipts if receipts is not None else getattr(reviewer, "receipts", None)
        result: dict[str, Any] = {}
        if store is None:
            await _record(
                monitor,
                "reviewer.compose_report.no_store",
                level=1,
                summary="No receipt store in this run, so the report was not published",
                actor="reviewer",
            )
            return result
        published = publish_report(
            verdict,
            store=store,
            run_id=_run_id,
            request_question=state["request"].question,
            report_dir=_report_dir(_run_id),
            formats=_report_formats,
            tex_path=state.get("report_latex"),
            pdf_path=state.get("report_pdf"),
        )
        if published.research_report is not None:
            result["research_report"] = published.research_report
            result["report_jsonl"] = published.jsonl_path
        await _record(
            monitor,
            "run.report",
            level=1,
            summary="Research report artifacts published",
            data={
                "artifacts": published.artifacts,
                "jsonl": str(published.jsonl_path) if published.jsonl_path else None,
                "tex": str(state.get("report_latex")) if state.get("report_latex") else None,
                "pdf": str(state.get("report_pdf")) if state.get("report_pdf") else None,
            },
        )
        return result

    def route_after_finalize(state: ResearchState) -> str:
        # Always document the outcome (success or an honest failure) — the reviewer has already
        # decided finalize-vs-rework. report_formats=[] means no report files are written.
        return "compose_report" if _report_formats else END

    builder = StateGraph(ResearchState)
    builder.add_node("literature", literature_node)
    builder.add_node("review_literature", review_literature_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("review_analysis", review_analysis_node)
    builder.add_node("finalize_run", finalize_run_node)
    builder.add_node("compose_report", compose_report_node)

    builder.add_edge(START, "literature")
    builder.add_edge("literature", "review_literature")
    # The reviewer is the sole I/O: every analysis result — success or honest failure —
    # is adjudicated by review_analysis (no analyst-lead bypass to finalize).
    builder.add_edge("analysis", "review_analysis")
    builder.add_conditional_edges(
        "review_literature",
        after_review_literature,
        {"literature": "literature", "analysis": "analysis"},
    )
    builder.add_conditional_edges(
        "review_analysis",
        after_review_analysis,
        {"analysis": "analysis", "finalize_run": "finalize_run"},
    )
    builder.add_conditional_edges(
        "finalize_run",
        route_after_finalize,
        {"compose_report": "compose_report", END: END},
    )
    builder.add_edge("compose_report", END)

    # A checkpointer (when supplied) makes LangGraph snapshot ResearchState after every node,
    # keyed by the invocation's thread_id — the basis for durable mid-run resume (§12). None
    # keeps the Studio/test path (no persistence).
    return builder.compile(checkpointer=checkpointer)


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


def _latest_verdict(state: ResearchState) -> ReviewerReport | None:
    verdicts = state.get("verdicts") or []
    if verdicts:
        return verdicts[-1]
    return state.get("verdict")
