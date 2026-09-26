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
from auto_research.provenance.store import INPUT_SOURCE
from auto_research.report import publish_report
from auto_research.retrieval import RetrievalBackend
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.workspace import ledger_dir as _ledger_dir
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
    """Minimal interface used by the review node."""

    async def review(
        self,
        request: ResearchRequest,
        *,
        arrived: str = "request",
        literature: Sequence[LiteratureReport] = (),
        literature_receipts: Mapping[str, Receipt] | None = None,
        report: AnalystReport | None = None,
        previous: Sequence[ReviewerReport] = (),
        human_nudges: Sequence[str] = (),
        human_files: Sequence[str] = (),
        budgets: Mapping[str, int] | None = None,
        human_attended: bool = False,
        render: Any | None = None,
    ) -> ReviewerReport:
        """Run one reviewer turn and return the report it filed through ``turn_end``."""


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
        label = "Reviewer feedback for revision" if state.get("report") else "Reviewer dispatch"
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
    human_handler: Any | None = None,
) -> Any:
    """Compile the research graph: the reviewer is the sole I/O (human 2026-09-25).

    Every turn starts at the reviewer and routes on its report's ``assign_to``:
    - ``librarian`` — a literature pass on the reviewer's questions, then back to the reviewer;
    - ``analysts`` — an analysis pass (``DebateToConverge``), then back to the reviewer;
    - ``human`` — the report is published and sent to the human (``human_handler``); a reply
      returns to the reviewer as a nudge (its files as ``input:`` receipts), and an exact
      ``done`` (or no handler) ends the run.
    The librarian and analysis edges are each capped at ``max_iterations`` passes per run; the
    reviewer's ``turn_end`` refuses an assignment past its cap. The human is an edge endpoint, not
    a unit: the human node runs no model, so resuming re-sends the report and waits again.
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

    _run_id = run_id or uuid.uuid4().hex
    _report_formats: list[str] = report_formats if report_formats is not None else ["jsonl", "tex"]

    async def review_node(state: ResearchState) -> dict[str, Any]:
        """One reviewer turn; a report to the human is rendered inside its own ``turn_end``.

        The ``.tex`` and PDF are therefore finished when the reviewer's loop returns; this node
        only records where they are, and any compile failure that was the renderer's rather than
        the model's, loudly (it never holds the reviewer in a loop it cannot leave).
        """
        from auto_research.agents.reviewer_tools import ReviewerRender

        arrived = state.get("arrived", "request")
        budgets = {
            "librarian": max_iterations - state.get("lit_iterations", 0),
            "analysts": max_iterations - state.get("analysis_iterations", 0),
        }
        await _record(
            monitor,
            "reviewer.review.start",
            level=3,
            summary=f"Reviewer turn started ({arrived} arrived)",
            actor="reviewer",
            data={"arrived": arrived, "budgets": budgets},
        )
        render = ReviewerRender(
            report_dir=_version_dir(_run_id, state.get("human_rounds", 0) + 1),
            run_id=_run_id, formats=tuple(_report_formats),
        )
        verdict = await reviewer.review(
            state["request"],
            arrived=arrived,
            literature=state.get("literature") or [],
            literature_receipts=state.get("literature_receipts") or {},
            report=state.get("report"),
            previous=state.get("verdicts") or [],
            human_nudges=state.get("human_nudges") or [],
            human_files=state.get("human_thread") or [],
            budgets=budgets,
            human_attended=human_handler is not None,
            render=render,
        )
        await _record(
            monitor,
            "reviewer.review.verdict",
            level=2,
            summary=f"Reviewer assigned its report to the {verdict.assign_to}",
            actor="reviewer",
            data={"verdict": verdict},
        )
        update: dict[str, Any] = {"verdicts": [verdict]}
        if verdict.assign_to == "human" and "tex" in _report_formats:
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

    def after_review(state: ResearchState) -> str:
        verdict = _latest_verdict(state)
        target = verdict.assign_to if verdict is not None else None
        # `turn_end` refuses a report with no assignee or one past its cap, so reaching either
        # here is a harness defect, raised rather than routed somewhere the reviewer did not name.
        if target == "librarian" and state.get("lit_iterations", 0) < max_iterations:
            return "literature"
        if target == "analysts" and state.get("analysis_iterations", 0) < max_iterations:
            return "analysis"
        if target == "human":
            return "finalize_run"
        raise RuntimeError(f"the reviewer's filed report cannot be routed (assign_to={target!r})")

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
        report = await io_agent.research(state["request"], follow_up=prior.follow_up if prior else None)
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
            "literature": [report],
            "literature_receipts": dict(io_agent.last_receipts),
            "lit_iterations": state.get("lit_iterations", 0) + 1,
            "arrived": "librarian",
        }

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
            "arrived": "analysts",
        }

    async def finalize_run_node(state: ResearchState) -> dict[str, Any]:
        """Pass the reviewer's report to the human through to publishing (§9.6).

        Every analysis — including an honest ``failed``/``needs_human`` outcome — is adjudicated
        by the reviewer (the sole I/O; the analyst lead gets no bypass). This node only records
        the outcome and forwards the report; it is always published, so an honest failure is
        *documented*, never silent. The no-fabrication guarantee lives in the reviewer's prompt
        and in its ``turn_end`` check, not in suppressing the report.
        """
        report = state.get("report")
        verdict = _latest_verdict(state)
        if report is None:
            await _record(
                monitor,
                "run.report_without_analysis",
                level=2,
                summary="The reviewer reported to the human before any analysis ran",
                data={"verdict": verdict},
            )
        elif getattr(report, "status", "failed") != "succeeded":
            # An absent status is not a success (F9): the harness never infers a terminal state.
            status = getattr(report, "status", "failed")
            await _record(
                monitor,
                "run.honest_failure",
                level=1,
                summary=f"Analysis ended with honest status '{status}' — reviewer-adjudicated, documented",
                data={"status": status, "report": report, "verdict": verdict},
            )
        return {"verdict": verdict}

    async def compose_report_node(state: ResearchState) -> dict[str, Any]:
        """Publish the report to the human: receipts for its files, and the JSONL when configured.

        No model runs here. The reviewer wrote the report inside its loop and its ``turn_end``
        rendered and compiled the ``.tex``; what remains is making the report an artifact
        (``rev:dN`` receipts whose parents are the receipts it cites) and the machine handle. Each
        report to the human has its own folder (``v<k>``), so an earlier ``rev:dN`` keeps its bytes.
        """
        verdict = state["verdict"]
        store = receipts if receipts is not None else getattr(reviewer, "receipts", None)
        result: dict[str, Any] = {"report_artifacts": {}}
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
            report_dir=_version_dir(_run_id, state.get("human_rounds", 0) + 1),
            formats=_report_formats,
            tex_path=state.get("report_latex"),
            pdf_path=state.get("report_pdf"),
        )
        result["report_artifacts"] = dict(published.artifacts)
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
        # report_formats=[] writes no report files; the human still receives the report's text.
        return "compose_report" if _report_formats else "human"

    async def human_node(state: ResearchState) -> dict[str, Any]:
        """Send the published report to the human and bring the reply back to the reviewer.

        The human is an edge endpoint, not a unit (human 2026-09-25): no model runs here. The
        reply's text is a nudge (human 2026-09-25): it reaches the reviewer as guidance and is
        never a receipt, so a number in it grounds nothing. Files the human sends are data, minted
        as ``input:`` receipts that ground only where cited. The reply ``done`` ends the run; so
        does a run no human attends, once the report is published.
        """
        rounds = state.get("human_rounds", 0) + 1
        if human_handler is None:
            await _record(
                monitor, "run.human.unattended", level=2,
                summary="No human attends this run: the report is published and the run ends",
            )
            return {"human_rounds": rounds, "arrived": "end"}
        from auto_research.hitl import resolve_human
        from auto_research.schemas.hitl import HumanQuery

        verdict = state["verdict"]
        store = receipts if receipts is not None else getattr(reviewer, "receipts", None)
        pdf = (state.get("report_artifacts") or {}).get("pdf")
        extra: dict[str, Any] = {}
        if pdf is not None and store is not None:
            from auto_research.hitl.attachments import resolve_attachments
            from auto_research.provenance import ScopedLookup

            attachments, problems = resolve_attachments(
                ScopedLookup(store, own_source="rev", admitted=[pdf]), [pdf]
            )
            if problems:
                # The runtime minted this PDF itself; a handle it cannot resolve is a defect.
                raise RuntimeError(f"the published report cannot be attached: {problems}")
            extra["attachments"] = attachments
        reply_dir = _ledger_dir(_run_id) / "human_replies" / f"v{rounds}"
        reply_dir.mkdir(parents=True, exist_ok=True)
        query = HumanQuery(
            kind="report",
            message=_report_message(verdict, attached=bool(extra)),
            asked_by="reviewer",
            model=_model_label(reviewer),
            # Outside every role's allow-list, beside the ledger: a file the human sends is read
            # through its receipt, never re-read from disk and re-minted as the reviewer's own.
            reply_dir=reply_dir,
            **extra,
        )
        await _record(
            monitor, "run.human.sent", level=1,
            summary=f"Report {rounds} sent to the human", actor="reviewer",
            data={"query": query, "pdf": pdf},
        )
        try:
            response = await resolve_human(human_handler, query)
        except TimeoutError:
            await _record(
                monitor, "run.human.timeout", level=1,
                summary="The human did not reply; the published report stands and the run ends",
            )
            return {"human_rounds": rounds, "arrived": "end"}
        if response.answer.strip().lower() == END_REPLY and not response.attachments:
            await _record(monitor, "run.human.done", level=1, summary="The human ended the run")
            return {"human_rounds": rounds, "arrived": "end"}
        files: list[str] = []
        if response.attachments:
            if store is None:
                raise RuntimeError("the human's files need the run's receipt store to be minted")
            from auto_research.hitl.attachments import mint_reply_attachments

            files = [
                r.handle for r in mint_reply_attachments(
                    store, source=INPUT_SOURCE, response=response, parent=None,
                )
            ]
        nudge = response.answer
        if response.notes:
            nudge += "\n\n[channel notes] " + " ".join(response.notes)
        await _record(
            monitor, "run.human.reply", level=1,
            summary="The human replied; the reviewer takes it up", actor="human",
            data={"answer": response.answer, "files": files, "notes": response.notes},
        )
        return {
            "human_rounds": rounds, "human_nudges": [nudge], "human_thread": files,
            "arrived": "human",
        }

    def route_after_human(state: ResearchState) -> str:
        return "review" if state.get("arrived") == "human" else END

    builder = StateGraph(ResearchState)
    builder.add_node("review", review_node)
    builder.add_node("literature", literature_node)
    builder.add_node("analysis", analysis_node)
    builder.add_node("finalize_run", finalize_run_node)
    builder.add_node("compose_report", compose_report_node)
    builder.add_node("human", human_node)

    # The reviewer is the sole I/O: the run starts at it, and every team's work returns to it.
    builder.add_edge(START, "review")
    builder.add_conditional_edges(
        "review",
        after_review,
        {"literature": "literature", "analysis": "analysis", "finalize_run": "finalize_run"},
    )
    builder.add_edge("literature", "review")
    builder.add_edge("analysis", "review")
    builder.add_conditional_edges(
        "finalize_run",
        route_after_finalize,
        {"compose_report": "compose_report", "human": "human"},
    )
    builder.add_edge("compose_report", "human")
    builder.add_conditional_edges("human", route_after_human, {"review": "review", END: END})

    # A checkpointer (when supplied) makes LangGraph snapshot ResearchState after every node,
    # keyed by the invocation's thread_id — the basis for durable mid-run resume (§12). None
    # keeps the Studio/test path (no persistence).
    return builder.compile(checkpointer=checkpointer)


#: The whole reply that ends the run. Plain text: a chat connector reads `/…` as a command.
END_REPLY = "done"


def _version_dir(run_id: str, version: int) -> Path:
    """The folder of the ``version``-th report to the human, with its figures folder."""
    d = _report_dir(run_id) / f"v{version}"
    (d / "figures").mkdir(parents=True, exist_ok=True)
    return d


def _report_message(report: ReviewerReport, *, attached: bool) -> str:
    """The message the human receives with the report: its title, abstract and how to answer."""
    where = "The report is attached as a PDF." if attached else "The report's text follows."
    body = [f"**{report.title}**", "", report.abstract, "", where]
    if not attached:
        for section in report.sections:
            body += ["", f"**{section.heading}**", "", section.body]
    body += [
        "",
        f"Reply to discuss it with the reviewer, or reply `{END_REPLY}` to end the run.",
    ]
    return "\n".join(body)


def _model_label(role: Any) -> str:
    """The model behind a role, for the human's message header (blank when unknown)."""
    config = getattr(getattr(role, "model", None), "config", None)
    label = getattr(config, "model", None)
    return label if isinstance(label, str) else ""


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
