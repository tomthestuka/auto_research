"""Async role wrappers for the Phase 1 agent team."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from auto_research.agents.prompts import PromptRole, load_prompt
from auto_research.consensus import IODebateToConverge
from auto_research.observations import load_observations_config
from auto_research.monitor import RunMonitor
from auto_research.models import ModelGroupHandle, ModelHandle, ModelMessage, ModelResponse, resolve
from auto_research.sandbox.workspace import append_memory, read_memory
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    DocumentRef,
    FigureRef,
    ResearchRequest,
    ReviewVerdict,
    SearchRequest,
    SearchResult,
    ToolCallRequest,
    ToolCallReview,
    ToolResult,
    document_from_file,
)
from auto_research.sources import ArxivSource, NasaAdsSource, QuerySource, WebSource
from auto_research.sources.downloads import download_and_ingest_result, papers_dir
from auto_research.storage import MemoryRecord, Storage, paper_record_from_result


T = TypeVar("T")


class CallableModel(Protocol):
    """Minimal model interface used by role wrappers and tests."""

    async def call(
        self,
        messages: Sequence[ModelMessage | Mapping[str, str]],
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
    ) -> ModelResponse:
        """Call the model and return text content."""


class AgentRole:
    """Base class shared by thin role wrappers."""

    role: PromptRole

    def __init__(
        self,
        *,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        self.prompt = prompt or load_prompt(self.role)
        self.model = model or _select_model(model_group or resolve(self.role), model_name)
        self.last_response: ModelResponse | None = None
        self.monitor = monitor

    async def _call(
        self,
        payload: Mapping[str, Any],
        output: TypeAdapter[T],
        *,
        images=(),
        documents=(),
        action: str = "call",
    ) -> T:
        await self._record(
            f"{self.role}.{action}.input",
            level=10,
            summary=f"{self.role} received {action} input",
            data={
                "payload": payload,
                "image_count": len(images),
                "document_count": len(documents),
                "documents": [doc.model_dump(mode="json") for doc in documents],
            },
        )
        try:
            response = await self.model.call(
                [
                    ModelMessage(role="system", content=self.prompt),
                    ModelMessage(role="user", content=_payload_text(payload, output)),
                ],
                images=images,
                documents=documents,
            )
            self.last_response = response
            parsed = _parse_json_response(response.content, output)
        except Exception as exc:
            await self._record(
                f"{self.role}.{action}.error",
                level=2,
                summary=f"{self.role} failed during {action}",
                data={"error": repr(exc)},
            )
            raise
        await self._record(
            f"{self.role}.{action}.output",
            level=10,
            summary=f"{self.role} returned {action} output",
            data={
                "output": parsed,
                "usage": response.usage,
                "raw_content": response.content,
            },
        )
        return parsed

    async def _record(
        self,
        event: str,
        *,
        level: int,
        summary: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        if self.monitor is not None:
            await self.monitor.record(
                _team_for_role(self.role),
                event,
                level=level,
                summary=summary,
                actor=self.role,
                data=data,
            )


class ReviewerAgent(AgentRole):
    """Reviewer role: compare request, literature, and analysis into a verdict."""

    role = "reviewer"

    async def review(
        self,
        request: ResearchRequest,
        *,
        literature: Sequence[Path] = (),
        report: AnalystReport | None = None,
    ) -> ReviewVerdict:
        """Return a review verdict for the supplied research state.

        ``literature`` is a list of local PDF paths returned by
        ``text_search_paths()``. Each existing path is loaded as a
        ``DocumentRef`` attachment so the model can read the full paper.
        """

        payload = {
            "request": request.model_dump(mode="json"),
            "literature_paths": [str(p) for p in literature],
            "analysis_report": report.model_dump(mode="json") if report else None,
        }
        docs = [document_from_file(p) for p in literature if p.exists()]
        return await self._call(
            payload,
            TypeAdapter(ReviewVerdict),
            images=report.figures if report else (),
            documents=docs,
            action="review",
        )

    async def compose_machine_report(
        self,
        request: ResearchRequest,
        report: AnalystReport,
        references: "list",
        verdict: ReviewVerdict,
        run_id: str,
    ) -> "ResearchReport":
        """Compose a structured machine-readable research report (JSONL handle).

        After the LLM returns the parsed report, figures/references/run_id/created_at
        are overwritten deterministically so machine refs are correct by construction.
        Only the prose fields (title, abstract, sections, key_findings) are LLM-authored.
        """
        from datetime import UTC, datetime

        from auto_research.schemas.report import ResearchReport

        payload = {
            "output_format": "jsonl",
            "output_format_instructions": (
                "You are composing a structured machine-readable research report in JSON. "
                "Populate title, abstract, sections (list of {heading, body}), and key_findings. "
                "The runtime will overwrite figures, references, run_id, and created_at — "
                "you may leave them as empty lists / empty string / now."
            ),
            "request": request.model_dump(mode="json"),
            "analysis_report": report.model_dump(mode="json"),
            "verdict_rationale": verdict.rationale,
            "references": [r.model_dump(mode="json") for r in references],
            "run_id": run_id,
        }
        result: ResearchReport = await self._call(
            payload,
            TypeAdapter(ResearchReport),
            images=report.figures,
            action="compose_machine_report",
        )
        return result.model_copy(
            update={
                "run_id": run_id,
                "request_question": request.question,
                "figures": report.figures,
                "references": references,
                "verdict_rationale": verdict.rationale,
                "created_at": datetime.now(UTC),
            }
        )

    async def compose_latex_report(
        self,
        request: ResearchRequest,
        report: AnalystReport,
        staged_figures: "list[tuple]",
        references: "list",
        verdict: ReviewVerdict,
        *,
        feedback: list[str] | None = None,
    ) -> str:
        """Compose a LaTeX research report (human handle).

        Returns the raw LaTeX string. ``staged_figures`` is a list of
        (FigureRef, relpath) pairs; relpaths are relative to the report directory
        and must be used verbatim in \\includegraphics{}.
        ``feedback`` carries compile/ref errors from the prior attempt for
        self-correction.
        """
        from auto_research.schemas.report import LatexReport

        fig_descriptions = [
            {"relpath": relpath, "caption": figref.caption, "sha256": figref.sha256}
            for figref, relpath in staged_figures
        ]
        ref_descriptions = [
            r.model_dump(mode="json") for r in references
        ]
        payload = {
            "output_format": "tex",
            "output_format_instructions": (
                "You are composing a LaTeX research report. "
                "Return a JSON object with a single key 'latex' whose value is the complete "
                "LaTeX document as a string. In JSON strings, every backslash must be escaped "
                "as \\\\ (e.g. \\\\section{}, \\\\includegraphics{}). "
                "Use only the exact figure relpaths and cite keys supplied below. "
                "Use \\\\includegraphics{<relpath>} verbatim for each figure. "
                "Define a self-contained \\\\begin{thebibliography} block; do not use an "
                "external .bib file. Cite only the provided keys with \\\\cite{<key>}."
            ),
            "request": request.model_dump(mode="json"),
            "analysis_report": report.model_dump(mode="json"),
            "verdict_rationale": verdict.rationale,
            "figures": fig_descriptions,
            "references": ref_descriptions,
        }
        if feedback:
            payload["feedback"] = (
                "The previous attempt had these errors — fix them exactly:\n"
                + "\n".join(f"  - {e}" for e in feedback)
            )
        result: LatexReport = await self._call(
            payload,
            TypeAdapter(LatexReport),
            images=report.figures,
            action="compose_latex_report",
        )
        return result.latex


class LibrarianIOAgent(AgentRole):
    """Librarian I/O role: compose search strategies, query external sources, and abstract papers."""

    role = "librarian_io"

    def __init__(
        self,
        *,
        sources: Mapping[str, QuerySource] | None = None,
        storage: Storage | None = None,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(
            model=model,
            model_group=model_group,
            model_name=model_name,
            prompt=prompt,
            monitor=monitor,
        )
        self.sources = dict(sources) if sources is not None else _default_sources()
        self.storage = storage or Storage()

    async def compose_search_strategy(
        self,
        request: ResearchRequest,
        *,
        follow_up: str | None = None,
    ) -> SearchRequest:
        """Compose a focused search strategy from a research request."""

        payload = {
            "research_request": request.model_dump(mode="json"),
            "follow_up": follow_up,
        }
        return await self._call(payload, TypeAdapter(SearchRequest), action="compose_search_strategy")

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Query external sources and return raw results.

        No LLM call: the I/O team does not filter or rank results. Per-paper
        abstraction is a separate step via ingest_results().
        A failing source is skipped so the others can still contribute results.
        """

        selected = request.sources or ["arxiv", "nasa_ads", "web"]
        results: list[SearchResult] = []
        for source_name in selected:
            source = self.sources.get(source_name)
            if source is None:
                continue
            try:
                results.extend(await source.search(request.query, limit=request.max_results))
            except Exception:
                pass  # one source failing does not abort the others
        return results

    async def ingest_document(
        self,
        document: DocumentRef,
        *,
        peer_abstracts: Sequence[MemoryRecord] = (),
        paper_id: int | None = None,
        instructions: str | None = None,
    ) -> MemoryRecord:
        """Read a document attachment and return one memory abstraction."""

        payload = {
            "document": document.model_dump(mode="json"),
            "paper_id": paper_id,
            "instructions": instructions
            or (
                "Compose a durable memory abstraction: abstract, key points, key methods, "
                "data, and analysis results useful for future text search."
            ),
            "peer_abstracts": [a.model_dump(mode="json") for a in peer_abstracts],
        }
        return await self._call(
            payload,
            TypeAdapter(MemoryRecord),
            documents=[document],
            action="ingest_document",
        )

    async def abstract_document(
        self,
        document: DocumentRef,
        *,
        peer_abstracts: Sequence[MemoryRecord] = (),
    ) -> MemoryRecord:
        """Worker shim: produce one memory abstraction for IO debate."""

        return await self.ingest_document(document, peer_abstracts=peer_abstracts)

    async def judge_abstract_convergence(
        self,
        document: DocumentRef,
        abstracts: Sequence[MemoryRecord],
        *,
        round_index: int,
        max_rounds: int,
    ) -> ReviewVerdict:
        """Lead role: judge whether IO worker abstractions have converged."""

        payload = {
            "document": document.model_dump(mode="json"),
            "abstracts": [a.model_dump(mode="json") for a in abstracts],
            "round_index": round_index,
            "max_rounds": max_rounds,
            "decision_semantics": (
                "accepted=true means workers have converged to a consistent memory abstraction; "
                "accepted=false means another round would improve coverage or consistency."
            ),
        }
        return await self._call(
            payload,
            TypeAdapter(ReviewVerdict),
            documents=[document],
            action="judge_abstract_convergence",
        )

    async def adjudicate_abstract(
        self,
        document: DocumentRef,
        abstracts: Sequence[MemoryRecord],
    ) -> MemoryRecord:
        """Lead role: synthesize the final memory record from worker abstractions."""

        payload = {
            "document": document.model_dump(mode="json"),
            "worker_abstracts": [a.model_dump(mode="json") for a in abstracts],
            "instructions": (
                "Synthesize the worker abstractions into one final memory record. "
                "Produce a mem_abbrev that is a comprehensive, durable summary of the paper's "
                "key findings, methods, data analysis, and results."
            ),
        }
        return await self._call(
            payload,
            TypeAdapter(MemoryRecord),
            documents=[document],
            action="adjudicate_abstract",
        )

    async def ingest_results(self, results: list[SearchResult]) -> None:
        """Download, abstract, and store each search result in parallel.

        Papers already abstracted (mem_abbrev populated in memory table) are
        skipped — PDFs are re-downloaded only if the file is absent.
        Concurrency is capped at 4 simultaneous ingests to respect API rate limits.
        """

        consensus = IODebateToConverge(max_rounds=2, monitor=self.monitor)
        sem = asyncio.Semaphore(4)

        async def _ingest_one(result: SearchResult) -> None:
            async with sem:
                stored = await download_and_ingest_result(
                    self.storage, result, target_dir=papers_dir()
                )
                if stored is not None and stored.local_path is not None:
                    existing = await self.storage.get_memory_for_paper(stored.id)
                    if existing is not None and existing.mem_abbrev:
                        return
                    doc = document_from_file(stored.local_path)
                    memory = await consensus.run([self, self], self, doc)
                    memory = MemoryRecord(
                        paper_id=stored.id,
                        summary=memory.summary,
                        tags=memory.tags,
                        mem_abbrev=memory.mem_abbrev,
                    )
                else:
                    paper_id = await self.storage.upsert_paper(paper_record_from_result(result))
                    existing = await self.storage.get_memory_for_paper(paper_id)
                    if existing is not None and existing.mem_abbrev:
                        return
                    memory = MemoryRecord(
                        paper_id=paper_id,
                        summary=result.title,
                        tags=result.keywords,
                        mem_abbrev=result.abstract,
                    )
                await self.storage.replace_memory_for_paper(memory)

        await asyncio.gather(*(_ingest_one(r) for r in results))

    async def evaluate_search_results(
        self,
        request: ResearchRequest,
        results: list[SearchResult],
    ) -> ReviewVerdict:
        """Phase 1 stub — IO search iteration is deferred. See CLAUDE.md §13."""

        _ = (request, results)
        return ReviewVerdict(
            accepted=True,
            rationale="IO search evaluation deferred — see CLAUDE.md §13",
        )


class AnalystWorkerAgent(AgentRole):
    """Analyst worker role: produce one analysis report.

    When a ``PersistentSandbox`` is attached (via ``sandbox``) and a
    ``memory_path`` is given, this worker:
    - Reads ``memory_path`` at the start of every ``analyze()`` call and injects
      the contents into the payload (private, full history).
    - Exposes ``execute_tools()`` which calls ``request_tools()``, runs each
      request through the sandbox, appends results to ``memory_path``, and
      returns the list of ``ToolResult`` objects.
    - Exposes ``is_alive`` which is ``False`` if the sandbox subprocess has died;
      once dead the worker stops calling tools for the session.
    """

    role = "analyst_worker"

    def __init__(
        self,
        *,
        available_toolkits: Sequence[str] = ("research_toolkit.xspec_utils",),
        sandbox: Any | None = None,  # PersistentSandbox | None — avoid circular import
        memory_path: Path | None = None,
        worker_index: int = 0,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(
            model=model,
            model_group=model_group,
            model_name=model_name,
            prompt=prompt,
            monitor=monitor,
        )
        self.available_toolkits = tuple(available_toolkits)
        self.sandbox = sandbox
        self.memory_path = memory_path
        self.worker_index = worker_index

    @property
    def is_alive(self) -> bool:
        """True while the attached sandbox is running (or when no sandbox is used)."""
        if self.sandbox is None:
            return True
        return bool(self.sandbox.is_alive)

    async def analyze(self, task: AnalysisTask, *, peer_reports: Sequence[AnalystReport] = ()) -> AnalystReport:
        """Analyze a task, optionally with peer reports from a debate round.

        Injects private ``memory`` from ``memory_path`` into the payload so the
        worker can reason from its own tool-call history.
        """
        memory = read_memory(self.memory_path) if self.memory_path else ""
        payload = {
            "task": task.model_dump(mode="json"),
            "peer_reports": [report.model_dump(mode="json") for report in peer_reports],
            "available_toolkits": list(self.available_toolkits),
            "memory": memory or None,
        }
        figures = [figure for report in peer_reports for figure in report.figures]
        return await self._call(
            payload,
            TypeAdapter(AnalystReport),
            images=figures,
            action="analyze",
        )

    async def request_tools(self, task: AnalysisTask) -> list[ToolCallRequest]:
        """Return structured tool requests without executing them."""

        payload = {
            "task": task.model_dump(mode="json"),
            "available_toolkits": list(self.available_toolkits),
            "execution_policy": "Return requested tool operations only; do not claim they have run.",
        }
        return await self._call(payload, TypeAdapter(list[ToolCallRequest]), action="request_tools")

    async def execute_tools(
        self,
        task: AnalysisTask,
        registry: Any,  # ToolkitRegistry — avoid circular import
        *,
        tool_retry_cap: int = 3,
    ) -> list[ToolResult]:
        """Request tools from self then execute them through the persistent sandbox.

        Results are appended to ``memory_path`` as a bare terminal recording.
        Returns an empty list when the sandbox is dead or not configured.
        """
        if self.sandbox is None:
            return []
        if not self.sandbox.is_alive:
            await self.sandbox.start()

        requests: list[ToolCallRequest] = await self.request_tools(task)
        if not requests:
            return []

        await self._record(
            "analyst_worker.tools.request",
            level=7,
            summary=f"Worker {self.worker_index + 1} requested {len(requests)} tool(s)",
            data={"requests": [r.model_dump(mode="json") for r in requests]},
        )

        from auto_research.schemas.figures import FigureRef  # local to avoid circularity

        results: list[ToolResult] = []
        for req in requests:
            # Routing and argument resolution are recomputed every attempt from
            # `current_req`, because the self-correction retry may return a request
            # with a different kind that must route differently.
            current_req = req
            for attempt in range(tool_retry_cap):
                kind = current_req.kind
                # Merge the dedicated `command` field into the args so shell tools
                # (e.g. mcp.builtin.bash_exec) receive it; the model puts shell
                # commands there rather than in `arguments`.
                args = dict(current_req.arguments)
                if current_req.command and "command" not in args:
                    args["command"] = current_req.command
                # Auto-inject the analyst's working directory into bash_exec so
                # files written by `python -c "..."` land in the worker's folder
                # rather than the MCP server's launch cwd.
                if (
                    kind == "mcp.builtin.bash_exec"
                    and "cwd" not in args
                    and self.memory_path is not None
                ):
                    args["cwd"] = str(self.memory_path.parent)

                t0 = time.monotonic()
                if not registry.is_known(kind):
                    raw = {
                        "exit_code": 127,
                        "stderr": f"Tool not registered in ToolkitRegistry: {kind}",
                        "stdout": "",
                    }
                elif registry.is_mcp(kind):
                    raw = await self.sandbox.call_mcp_tool(
                        kind, args, timeout_s=current_req.timeout_s
                    )
                else:
                    spec = registry.spec(kind)
                    # Auto-fill instrument data paths (ixpe_path/nicer_path/…) from
                    # observations.yaml when the tool declares them and the model
                    # omitted them, so a path is always supplied from config.
                    schema_props = (spec.get("args_schema") or {}).get("properties", {})
                    for arg_name, path in _observation_path_args().items():
                        if arg_name in schema_props and not args.get(arg_name):
                            args[arg_name] = path
                    raw = await self.sandbox.call_tool(
                        spec["module"], spec["fn"], args, timeout_s=current_req.timeout_s
                    )
                elapsed = time.monotonic() - t0

                raw = _normalize_tool_raw(raw)
                figures = [FigureRef.model_validate(f) for f in raw.get("figures", [])]
                r = ToolResult(
                    tool_name=kind,
                    stdout=raw.get("stdout", ""),
                    stderr=raw.get("stderr", ""),
                    exit_code=raw.get("exit_code", 1),
                    figures=figures,
                    elapsed_s=elapsed,
                )
                results.append(r)

                await self._record(
                    "analyst_worker.tools.result",
                    level=7,
                    summary=f"Worker {self.worker_index + 1}: {kind} exit={r.exit_code} ({elapsed:.1f}s)",
                    data={"result": r.model_dump(mode="json"), "attempt": attempt + 1},
                )

                if r.exit_code == 0:
                    break

                # LLM self-correction retry
                if attempt < tool_retry_cap - 1 and self.is_alive:
                    error_detail = r.stderr or r.stdout or "(no error detail)"
                    error_task = AnalysisTask(
                        question=task.question,
                        instructions=(
                            f"Tool {kind} failed (attempt {attempt + 1}/{tool_retry_cap}).\n"
                            f"Error: {error_detail}\n"
                            "Revise your tool call. Use the EXACT kind string from the catalogue — "
                            "do not shorten or guess tool names. For shell tools, put the command "
                            "in the `command` field.\n\n"
                            f"{registry.tool_catalogue()}"
                        ),
                    )
                    revised = await self.request_tools(error_task)
                    if revised:
                        current_req = revised[0]

        # Append bare terminal recording to memory
        if self.memory_path and results:
            _append_tool_results_to_memory(self.memory_path, results)

        return results


class AnalystLeadAgent(AgentRole):
    """Analyst lead role: adjudicate worker reports into one report."""

    role = "analyst_lead"

    def __init__(
        self,
        *,
        available_toolkits: Sequence[str] = ("research_toolkit.xspec_utils",),
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(
            model=model,
            model_group=model_group,
            model_name=model_name,
            prompt=prompt,
            monitor=monitor,
        )
        self.available_toolkits = tuple(available_toolkits)

    async def adjudicate(self, task: AnalysisTask, reports: Sequence[AnalystReport]) -> AnalystReport:
        """Synthesize a final analysis report from worker reports."""

        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "available_toolkits": list(self.available_toolkits),
        }
        figures = [figure for report in reports for figure in report.figures]
        return await self._call(
            payload,
            TypeAdapter(AnalystReport),
            images=figures,
            action="adjudicate",
        )

    async def judge_convergence(
        self,
        task: AnalysisTask,
        reports: Sequence[AnalystReport],
        *,
        round_index: int,
        max_rounds: int,
    ) -> ReviewVerdict:
        """Judge whether worker reports have converged."""

        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "round_index": round_index,
            "max_rounds": max_rounds,
            "decision_semantics": (
                "accepted=true means workers have converged enough for final synthesis; "
                "accepted=false means another debate round is useful if the cap allows it."
            ),
        }
        figures = [figure for report in reports for figure in report.figures]
        return await self._call(
            payload,
            TypeAdapter(ReviewVerdict),
            images=figures,
            action="judge_convergence",
        )

    async def review_tool_requests(
        self,
        requests: Sequence[ToolCallRequest],
    ) -> list[ToolCallReview]:
        """Review proposed tool requests without executing them."""

        payload = {
            "tool_requests": [request.model_dump(mode="json") for request in requests],
            "available_toolkits": list(self.available_toolkits),
            "execution_policy": "Review only; no tool or shell command has been executed.",
        }
        return await self._call(
            payload,
            TypeAdapter(list[ToolCallReview]),
            action="review_tool_requests",
        )


def _select_model(group: ModelGroupHandle, model_name: str | None) -> ModelHandle:
    if model_name is not None:
        return group.member(model_name)
    return group.handles[0]


@lru_cache(maxsize=1)
def _observation_path_args() -> dict[str, str]:
    """Map tool path-args to configured observation roots, e.g. ``ixpe_path`` →
    the IXPE ``obs_db_path`` from ``config/observations.yaml``.

    Convention: mission ``<m>`` in observations.yaml fills a tool argument named
    ``<m>_path``.  Used to auto-supply instrument data paths so the model need not.
    """
    try:
        cfg = load_observations_config()
    except Exception:  # noqa: BLE001
        return {}
    return {f"{mission}_path": str(c.obs_db_path) for mission, c in cfg.missions.items()}


def _payload_text(payload: Mapping[str, Any], output: TypeAdapter[Any]) -> str:
    schema = output.json_schema()
    return (
        "Return only JSON that validates against this JSON Schema.\n\n"
        f"JSON Schema:\n{json.dumps(schema, sort_keys=True)}\n\n"
        f"Task data:\n{json.dumps(payload, sort_keys=True)}"
    )


def _parse_json_response(content: str, output: TypeAdapter[T]) -> T:
    text = _strip_json_fence(content)
    try:
        return output.validate_json(text)
    except ValidationError:
        pass
    try:
        value = _extract_json_value(text)
        return output.validate_python(value)
    except (json.JSONDecodeError, ValidationError):
        pass
    # Last resort: if the model returned a single dict but a list was expected,
    # wrap it (happens when a JSON array is truncated mid-write).
    try:
        value = _extract_json_value(text)
        if isinstance(value, dict):
            return output.validate_python([value])
    except (json.JSONDecodeError, ValidationError):
        pass
    raise ValueError("model response did not match the expected schema")


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_json_value(text: str) -> Any:
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(text) if char in "[{"]
    for index in starts:
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise json.JSONDecodeError("no JSON object or array found", text, 0)


def _team_for_role(role: PromptRole) -> str:
    if role == "reviewer":
        return "reviewer"
    if role == "librarian_io":
        return "librarian"
    return "analyst"


def _default_sources() -> dict[str, QuerySource]:
    return {
        "arxiv": ArxivSource(),
        "nasa_ads": NasaAdsSource(),
        "web": WebSource(),
    }


# Keys that belong to the transport protocol — not serialized as tool payload.
_PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"stdout", "stderr", "exit_code", "figures", "console_tail"}
)

# Per-field character caps written to memory.md (crash-prevention).
_STDOUT_MEM_CAP = 8_000
_STDERR_MEM_CAP = 2_000


def _normalize_tool_raw(raw: dict) -> dict:
    """Ensure every tool result has a non-empty ``stdout`` for the model to read.

    Tools that return structured payloads without a ``stdout`` key (e.g.
    ``list_dir`` → ``{path, entries}``, ``load_data`` → nested indices dict)
    are invisible to the model otherwise.  When ``stdout`` is absent or empty,
    serialize the non-protocol payload keys as compact JSON.

    Also folds any captured ``console_tail`` (XSPEC/C-level prints) into
    ``stdout`` so the model sees count-rate and fit-convergence logs.
    """
    stdout = raw.get("stdout") or ""
    if not stdout:
        extra = {k: v for k, v in raw.items() if k not in _PROTOCOL_KEYS}
        if extra:
            stdout = json.dumps(extra, indent=2, default=str)

    console = raw.get("console_tail") or ""
    if console:
        sep = "\n--- console ---\n"
        stdout = (stdout + sep + console) if stdout else (sep.lstrip() + console)

    return {**raw, "stdout": stdout}


def _append_tool_results_to_memory(path: Path, results: Sequence[ToolResult]) -> None:
    """Append bare terminal recordings of tool calls to the worker's memory file.

    stdout and stderr are capped at write time so memory.md stays bounded.
    """
    import datetime
    lines: list[str] = [f"\n## Tool calls — {datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds')}"]
    for r in results:
        status = "OK" if r.exit_code == 0 else f"FAILED (exit {r.exit_code})"
        lines.append(f"\n### {r.tool_name}  [{status}]  ({r.elapsed_s:.1f}s)")
        if r.stdout:
            text = r.stdout
            if len(text) > _STDOUT_MEM_CAP:
                text = text[:_STDOUT_MEM_CAP] + f"\n[stdout truncated — {len(r.stdout):,} chars total]"
            lines.append(f"stdout:\n{text}")
        if r.stderr:
            text = r.stderr
            if len(text) > _STDERR_MEM_CAP:
                text = text[:_STDERR_MEM_CAP] + f"\n[stderr truncated — {len(r.stderr):,} chars total]"
            lines.append(f"stderr:\n{text}")
        if r.figures:
            lines.append(f"figures: {[str(f.path) for f in r.figures]}")
    append_memory(path, "\n".join(lines) + "\n")
