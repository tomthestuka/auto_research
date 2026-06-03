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
from auto_research.report import (
    build_references,
    compile_latex,
    stage_figures,
    validate_latex_refs,
    write_report_jsonl,
    write_report_latex,
)
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend, text_search_paths
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.workspace import report_dir as _report_dir
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
)
from auto_research.storage import Storage


class IOAgentLike(Protocol):
    """Minimal interface required of the librarian I/O agent."""

    async def compose_search_strategy(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> SearchRequest:
        """Compose a focused search strategy from a research request."""

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Query external sources and return raw results."""

    async def ingest_results(self, results: list[SearchResult]) -> None:
        """Download, abstract, and store each search result."""


class ReviewerLike(Protocol):
    """Minimal interface used by the review nodes."""

    async def review(
        self,
        request: ResearchRequest,
        *,
        literature: Sequence[Path] = (),
        report: AnalystReport | None = None,
    ) -> ReviewVerdict:
        """Return a review verdict for the supplied state."""


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
    storage: Storage | None = None,
    report_retry_cap: int = 2,
    report_formats: list[str] | None = None,
) -> Any:
    """Compile the Phase 1 research-loop graph.

    Two outer feedback edges, each hard-capped at ``max_iterations``:
    - paper-search review: ``review_literature -> literature``
    - analysis review: ``review_analysis -> analysis``

    Tool execution is handled inside ``DebateToConverge`` via persistent
    per-worker sandboxes.  Pass ``registry`` (with the already-loaded
    ``ToolkitRegistry``) so the consensus strategy can execute tools and
    share truncated summaries across workers between debate rounds.

    The literature node follows a four-step pipeline:
    IO agent composes search strategy → IO agent queries online sources →
    IO agent downloads and abstracts results → ``text_search_paths()`` queries
    local SQLite (pure Python, no LLM). The reviewer receives local PDF paths
    and loads them as full-document attachments.
    """

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")

    _registry = registry or ToolkitRegistry(Path("/data/fzhou/research_toolkit"))
    consensus_strategy = consensus or DebateToConverge(
        max_rounds=max_iterations, monitor=monitor, registry=_registry
    )
    _retrieval = retrieval or SqliteRetrievalBackend()
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
        search_request = await io_agent.compose_search_strategy(
            state["request"], follow_up=follow_up
        )
        await _record(
            monitor,
            "librarian.search.strategy",
            level=5,
            summary="Librarian IO composed search strategy",
            actor="librarian_io",
            data={"search_request": search_request, "follow_up": follow_up},
        )
        raw = await io_agent.search(search_request)
        await _record(
            monitor,
            "librarian.search.results",
            level=5,
            summary=f"Librarian IO returned {len(raw)} raw search results",
            actor="librarian_io",
            data={"results": raw},
        )
        await io_agent.ingest_results(raw)
        paths = await text_search_paths(
            search_request.query,
            limit=search_request.max_results,
            backend=_retrieval,
        )
        await _record(
            monitor,
            "librarian.local_retrieval.results",
            level=5,
            summary=f"Local retrieval returned {len(paths)} literature paths",
            data={"paths": paths},
        )
        return {
            "literature": paths,
            "lit_iterations": state.get("lit_iterations", 0) + 1,
        }

    async def review_literature_node(state: ResearchState) -> dict[str, Any]:
        await _record(
            monitor,
            "reviewer.literature_review.start",
            level=3,
            summary="Reviewer literature review started",
            actor="reviewer",
            data={"literature": state.get("literature", [])},
        )
        verdict = await reviewer.review(
            state["request"],
            literature=state.get("literature", []),
            report=None,
        )
        await _record(
            monitor,
            "reviewer.literature_review.verdict",
            level=2,
            summary="Reviewer returned literature verdict",
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
        await _record(
            monitor,
            "reviewer.analysis_review.start",
            level=3,
            summary="Reviewer analysis review started",
            actor="reviewer",
            data={"report": state.get("report")},
        )
        verdict = await reviewer.review(
            state["request"],
            literature=state.get("literature", []),
            report=state.get("report"),
        )
        await _record(
            monitor,
            "reviewer.analysis_review.verdict",
            level=2,
            summary="Reviewer returned final/rework verdict",
            actor="reviewer",
            data={"verdict": verdict},
        )
        return {"verdicts": [verdict]}

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
        return {"verdict": _latest_verdict(state)}

    _run_id = run_id or uuid.uuid4().hex
    _storage = storage or Storage()
    _report_formats: list[str] = report_formats if report_formats is not None else ["jsonl", "tex"]

    async def compose_report_node(state: ResearchState) -> dict[str, Any]:
        verdict = state["verdict"]
        analyst_report = state.get("report")
        literature = state.get("literature") or []

        rdir = _report_dir(_run_id)
        references = await build_references(_storage, literature)

        result: dict[str, Any] = {}

        if "jsonl" in _report_formats:
            machine = await reviewer.compose_machine_report(  # type: ignore[attr-defined]
                state["request"], analyst_report, references, verdict, _run_id
            )
            jsonl_path = write_report_jsonl(machine, rdir / "report.jsonl")
            result["research_report"] = machine
            result["report_jsonl"] = jsonl_path
            await _record(
                monitor,
                "reviewer.compose_report.jsonl",
                level=3,
                summary="Reviewer wrote machine report (JSONL)",
                actor="reviewer",
                data={"path": str(jsonl_path)},
            )

        if "tex" in _report_formats:
            staged = stage_figures(analyst_report.figures if analyst_report else [], rdir)
            feedback: list[str] | None = None
            latex_path: Path | None = None
            pdf_path: Path | None = None

            for attempt in range(max(1, report_retry_cap)):
                latex = await reviewer.compose_latex_report(  # type: ignore[attr-defined]
                    state["request"], analyst_report, staged, references, verdict,
                    feedback=feedback,
                )
                latex_path = write_report_latex(latex, rdir / "report.tex")
                errs = validate_latex_refs(latex, staged, references)
                ok, cerrs = compile_latex(rdir)
                errs = errs + cerrs
                await _record(
                    monitor,
                    "reviewer.compose_report.compile",
                    level=4,
                    summary=f"LaTeX compile attempt {attempt + 1}: {'OK' if not errs else f'{len(errs)} error(s)'}",
                    actor="reviewer",
                    data={"attempt": attempt + 1, "errors": errs},
                )
                if not errs:
                    pdf_candidates = list(rdir.glob("*.pdf"))
                    pdf_path = pdf_candidates[0] if pdf_candidates else None
                    break
                feedback = errs

            if errs:
                await _record(
                    monitor,
                    "reviewer.compose_report.compile_warning",
                    level=2,
                    summary=f"LaTeX compile finished with {len(errs)} residual error(s) after {report_retry_cap} attempt(s)",
                    actor="reviewer",
                    data={"errors": errs},
                )
            result["report_latex"] = latex_path
            if pdf_path:
                result["report_pdf"] = pdf_path

        await _record(
            monitor,
            "run.report",
            level=1,
            summary="Research report artifacts written",
            data={k: str(v) for k, v in result.items() if isinstance(v, Path)},
        )
        return result

    def route_after_finalize(state: ResearchState) -> str:
        verdict = state.get("verdict")
        if verdict and verdict.accepted and _report_formats:
            return "compose_report"
        return END

    builder = StateGraph(ResearchState)
    builder.add_node("literature", literature_node)
    builder.add_node("review_literature", review_literature_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("review_analysis", review_analysis_node)
    builder.add_node("finalize_run", finalize_run_node)
    builder.add_node("compose_report", compose_report_node)

    builder.add_edge(START, "literature")
    builder.add_edge("literature", "review_literature")
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

    return builder.compile()


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


def _latest_verdict(state: ResearchState) -> ReviewVerdict | None:
    verdicts = state.get("verdicts") or []
    if verdicts:
        return verdicts[-1]
    return state.get("verdict")
