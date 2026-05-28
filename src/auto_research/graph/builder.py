"""LangGraph assembly for the Phase 1 research loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph

from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.state import ResearchState
from auto_research.monitor import RunMonitor
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend, text_search_paths
from auto_research.sandbox.merge_flow import propose_and_pr
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.runner import run_tool
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
    ToolCallRequest,
    ToolResult,
)


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


class ToolWorkerLike(WorkerLike, Protocol):
    """Worker that also supports structured tool requests."""

    async def request_tools(self, task: AnalysisTask) -> list[ToolCallRequest]:
        """Return a list of tool call requests needed for the task."""


def _format_tool_results(results: list[ToolResult]) -> str:
    """Format tool results for injection into task instructions."""
    if not results:
        return ""
    parts = ["Tool execution results:"]
    for r in results:
        status = "OK" if r.exit_code == 0 else f"FAILED (exit {r.exit_code})"
        parts.append(f"  [{status}] {r.tool_name} ({r.elapsed_s:.1f}s)")
        if r.stdout:
            parts.append(f"    stdout: {r.stdout[:500]}")
        if r.stderr and r.exit_code != 0:
            parts.append(f"    stderr: {r.stderr[:300]}")
        if r.figures:
            parts.append(f"    figures: {[str(f.path) for f in r.figures]}")
    return "\n".join(parts)


def _build_task(
    state: ResearchState,
    *,
    extra_instructions: Sequence[str] = (),
) -> AnalysisTask:
    """Reconstruct the current AnalysisTask from graph state."""
    request = state["request"]
    instructions_parts: list[str] = []
    if request.context:
        instructions_parts.append(request.context)
    prior = _latest_verdict(state)
    if prior and prior.needs_analysis_rework and prior.follow_up:
        instructions_parts.append(f"Reviewer feedback for revision: {prior.follow_up}")
    tool_results = state.get("tool_results") or []
    if tool_results:
        instructions_parts.append(_format_tool_results(tool_results))
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
    tool_retry_cap: int = 3,
    toolkit_root: Path | None = None,
    monitor: RunMonitor | None = None,
) -> Any:
    """Compile the Phase 1 research-loop graph.

    Two outer feedback edges, each hard-capped at ``max_iterations``:
    - paper-search review: ``review_literature -> literature``
    - analysis review: ``review_analysis -> analysis``

    Between ``analysis`` and ``review_analysis``, two tool nodes handle
    structured tool requests from workers:
    - ``tool_dispatch``: workers request tools; known → queue, unknown → stub
    - ``tool_execution``: subprocess sandbox with error-feedback retry loop

    The literature node follows a four-step pipeline:
    IO agent composes search strategy → IO agent queries online sources →
    IO agent downloads and abstracts results → ``text_search_paths()`` queries
    local SQLite (pure Python, no LLM). The reviewer receives local PDF paths
    and loads them as full-document attachments.
    """

    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")
    consensus_strategy = consensus or DebateToConverge(max_rounds=max_iterations, monitor=monitor)
    _retrieval = retrieval or SqliteRetrievalBackend()
    _registry = registry or ToolkitRegistry(toolkit_root or Path("/data/fzhou/research_toolkit"))

    # Resolve runtime data dir for tool execution workdir.
    import os
    _workdir = Path(os.environ.get("AUTO_RESEARCH_RUNTIME_DIR", Path.home() / ".auto_research")) / "tool_workdir"

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
        task = _build_task(state)
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

    async def tool_dispatch_node(state: ResearchState) -> dict[str, Any]:
        """Ask workers for tool requests; route known vs unknown."""
        # Only the first worker makes tool requests (one structured LLM call).
        dispatcher = workers[0] if workers else None
        if dispatcher is None or not hasattr(dispatcher, "request_tools"):
            await _record(
                monitor,
                "tooling.dispatch.skipped",
                level=4,
                summary="No tool-capable worker was available",
            )
            return {"tool_queue": []}

        catalogue = _registry.tool_catalogue()
        task = _build_task(
            state,
            extra_instructions=[f"Available sandbox tools:\n{catalogue}"],
        )
        await _record(
            monitor,
            "tooling.dispatch.start",
            level=3,
            summary="Tool dispatch started",
            data={"tool_catalogue": catalogue},
        )

        requests: list[ToolCallRequest] = await dispatcher.request_tools(task)  # type: ignore[attr-defined]
        await _record(
            monitor,
            "tooling.dispatch.requests",
            level=7,
            summary=f"Worker requested {len(requests)} tools",
            actor="analyst_worker",
            data={"requests": requests},
        )

        queue: list[ToolCallRequest] = []
        unavailable_results: list[ToolResult] = []

        for req in requests:
            if _registry.is_known(req.kind):
                queue.append(req)
                await _record(
                    monitor,
                    "tooling.dispatch.queued",
                    level=7,
                    summary=f"Queued known tool {req.kind}",
                    data={"request": req},
                )
            else:
                # Phase 2 stub — catches NotImplementedError and records as unavailable.
                try:
                    await propose_and_pr(req, lead=lead)
                except NotImplementedError as exc:
                    result = ToolResult(
                        tool_name=req.kind,
                        stdout="",
                        stderr=str(exc),
                        exit_code=127,  # "command not found" convention
                        elapsed_s=0.0,
                    )
                    unavailable_results.append(result)
                    await _record(
                        monitor,
                        "tooling.dispatch.unavailable",
                        level=7,
                        summary=f"Tool {req.kind} is unavailable in Phase 1",
                        data={"request": req, "result": result},
                    )

        return {"tool_queue": queue, "tool_results": unavailable_results}

    async def tool_execution_node(state: ResearchState) -> dict[str, Any]:
        """Execute queued tool requests with an error-feedback retry loop."""
        queue: list[ToolCallRequest] = state.get("tool_queue") or []
        if not queue:
            await _record(
                monitor,
                "tooling.execution.skipped",
                level=4,
                summary="No tool requests were queued",
            )
            return {"tool_queue": [], "tool_results": []}

        task = _build_task(state)
        dispatcher = workers[0] if workers else None
        new_results: list[ToolResult] = []

        for req in queue:
            spec = _registry.spec(req.kind)
            current_req = req
            for attempt in range(tool_retry_cap):
                await _record(
                    monitor,
                    "tooling.execution.attempt",
                    level=7,
                    summary=f"Executing {req.kind} attempt {attempt + 1}",
                    data={
                        "request": current_req,
                        "attempt": attempt + 1,
                        "tool_retry_cap": tool_retry_cap,
                        "spec": spec,
                    },
                )
                result = await run_tool(
                    spec["module"],
                    spec["fn"],
                    current_req.arguments,
                    timeout_s=current_req.timeout_s,
                    workdir=_workdir,
                    toolkit_root=_registry._root,
                )
                new_results.append(result)
                await _record(
                    monitor,
                    "tooling.execution.result",
                    level=7,
                    summary=f"Tool {req.kind} exited with code {result.exit_code}",
                    data={"result": result, "attempt": attempt + 1},
                )
                if result.exit_code == 0:
                    break
                # LLM self-correction: ask worker to revise args given the error.
                if attempt < tool_retry_cap - 1 and dispatcher and hasattr(dispatcher, "request_tools"):
                    error_instructions = (
                        f"Tool {req.kind} failed (attempt {attempt + 1}/{tool_retry_cap}).\n"
                        f"Error: {result.stderr}\n"
                        "Please revise your tool call with corrected arguments."
                    )
                    retry_task = AnalysisTask(
                        question=task.question,
                        instructions=error_instructions,
                    )
                    revised: list[ToolCallRequest] = await dispatcher.request_tools(retry_task)  # type: ignore[attr-defined]
                    if revised:
                        current_req = revised[0]
                        await _record(
                            monitor,
                            "tooling.execution.retry_request",
                            level=7,
                            summary=f"Worker revised {req.kind} arguments after failure",
                            actor="analyst_worker",
                            data={"request": current_req},
                        )

        return {"tool_queue": [], "tool_results": new_results}

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

    builder = StateGraph(ResearchState)
    builder.add_node("literature", literature_node)
    builder.add_node("review_literature", review_literature_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("tool_dispatch", tool_dispatch_node)
    builder.add_node("tool_execution", tool_execution_node)
    builder.add_node("review_analysis", review_analysis_node)
    builder.add_node("finalize_run", finalize_run_node)

    builder.add_edge(START, "literature")
    builder.add_edge("literature", "review_literature")
    builder.add_edge("analysis", "tool_dispatch")
    builder.add_edge("tool_dispatch", "tool_execution")
    builder.add_edge("tool_execution", "review_analysis")
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
    builder.add_edge("finalize_run", END)

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
