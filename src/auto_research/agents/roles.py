"""Async role wrappers for the Phase 1 agent team."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from auto_research.agents.prompts import PromptRole, load_prompt
from auto_research.consensus import IODebateToConverge
from auto_research.monitor import RunMonitor
from auto_research.models import ModelGroupHandle, ModelHandle, ModelMessage, ModelResponse, resolve
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
        """Download, abstract, and store each search result.

        For PDF-bearing results: download → IODebateToConverge (2 worker passes +
        lead adjudication) → write mem_abbrev to memory table.
        For results without a PDF URL: store metadata only; use abstract as mem_abbrev.
        """

        consensus = IODebateToConverge(max_rounds=2, monitor=self.monitor)
        for result in results:
            stored = await download_and_ingest_result(
                self.storage, result, target_dir=papers_dir()
            )
            if stored is not None and stored.local_path is not None:
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
                memory = MemoryRecord(
                    paper_id=paper_id,
                    summary=result.title,
                    tags=result.keywords,
                    mem_abbrev=result.abstract,
                )
            await self.storage.replace_memory_for_paper(memory)

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
    """Analyst worker role: produce one analysis report."""

    role = "analyst_worker"

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

    async def analyze(self, task: AnalysisTask, *, peer_reports: Sequence[AnalystReport] = ()) -> AnalystReport:
        """Analyze a task, optionally with peer reports from a debate round."""

        payload = {
            "task": task.model_dump(mode="json"),
            "peer_reports": [report.model_dump(mode="json") for report in peer_reports],
            "available_toolkits": list(self.available_toolkits),
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
    except ValidationError as exc:
        try:
            return output.validate_python(_extract_json_value(text))
        except (json.JSONDecodeError, ValidationError) as inner_exc:
            raise ValueError("model response did not match the expected schema") from inner_exc


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
