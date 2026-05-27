"""Async role wrappers for the Phase 1 agent team."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from auto_research.agents.prompts import PromptRole, load_prompt
from auto_research.models import ModelGroupHandle, ModelHandle, ModelMessage, ModelResponse, resolve
from auto_research.retrieval import MemoryHit, RetrievalBackend, SqliteRetrievalBackend
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
)
from auto_research.sources import ArxivSource, NasaAdsSource, QuerySource, WebSource
from auto_research.storage import MemoryRecord


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
    ):
        self.prompt = prompt or load_prompt(self.role)
        self.model = model or _select_model(model_group or resolve(self.role), model_name)
        self.last_response: ModelResponse | None = None

    async def _call(
        self,
        payload: Mapping[str, Any],
        output: TypeAdapter[T],
        *,
        images=(),
        documents=(),
    ) -> T:
        response = await self.model.call(
            [
                ModelMessage(role="system", content=self.prompt),
                ModelMessage(role="user", content=_payload_text(payload, output)),
            ],
            images=images,
            documents=documents,
        )
        self.last_response = response
        return _parse_json_response(response.content, output)


class ReviewerAgent(AgentRole):
    """Reviewer role: compare request, literature, and analysis into a verdict."""

    role = "reviewer"

    async def review(
        self,
        request: ResearchRequest,
        *,
        literature: Sequence[SearchResult] = (),
        report: AnalystReport | None = None,
    ) -> ReviewVerdict:
        """Return a review verdict for the supplied research state."""

        payload = {
            "request": request.model_dump(mode="json"),
            "literature": [item.model_dump(mode="json") for item in literature],
            "analysis_report": report.model_dump(mode="json") if report else None,
        }
        return await self._call(payload, TypeAdapter(ReviewVerdict), images=report.figures if report else ())


class LibrarianIOAgent(AgentRole):
    """Librarian I/O role: query injected external sources and rank results."""

    role = "librarian_io"

    def __init__(
        self,
        *,
        sources: Mapping[str, QuerySource] | None = None,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
    ):
        super().__init__(model=model, model_group=model_group, model_name=model_name, prompt=prompt)
        self.sources = dict(sources) if sources is not None else _default_sources()

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Search requested external sources and return model-ranked results."""

        selected = request.sources or ["arxiv", "nasa_ads", "web"]
        results: list[SearchResult] = []
        for source_name in selected:
            source = self.sources.get(source_name)
            if source is None:
                continue
            results.extend(await source.search(request.query, limit=request.max_results))
        payload = {
            "request": request.model_dump(mode="json"),
            "source_results": [item.model_dump(mode="json") for item in results],
        }
        return await self._call(payload, TypeAdapter(list[SearchResult]))

    async def ingest_document(
        self,
        document: DocumentRef,
        *,
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
        }
        return await self._call(payload, TypeAdapter(MemoryRecord), documents=[document])


class LibrarianSearcherAgent(AgentRole):
    """Librarian searcher role: query local memory and summarize local hits."""

    role = "librarian_searcher"

    def __init__(
        self,
        *,
        retrieval: RetrievalBackend | None = None,
        model: CallableModel | None = None,
        model_group: ModelGroupHandle | None = None,
        model_name: str | None = None,
        prompt: str | None = None,
    ):
        super().__init__(model=model, model_group=model_group, model_name=model_name, prompt=prompt)
        self.retrieval = retrieval or SqliteRetrievalBackend()

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        """Return compact local-memory search results."""

        hits = await self.retrieval.text_search(request.query, limit=request.max_results)
        payload = {
            "request": request.model_dump(mode="json"),
            "local_hits": [hit.model_dump(mode="json") for hit in hits],
        }
        return await self._call(payload, TypeAdapter(list[SearchResult]))


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
    ):
        super().__init__(model=model, model_group=model_group, model_name=model_name, prompt=prompt)
        self.available_toolkits = tuple(available_toolkits)

    async def analyze(self, task: AnalysisTask, *, peer_reports: Sequence[AnalystReport] = ()) -> AnalystReport:
        """Analyze a task, optionally with peer reports from a debate round."""

        payload = {
            "task": task.model_dump(mode="json"),
            "peer_reports": [report.model_dump(mode="json") for report in peer_reports],
            "available_toolkits": list(self.available_toolkits),
        }
        figures = [figure for report in peer_reports for figure in report.figures]
        return await self._call(payload, TypeAdapter(AnalystReport), images=figures)

    async def request_tools(self, task: AnalysisTask) -> list[ToolCallRequest]:
        """Return structured tool requests without executing them."""

        payload = {
            "task": task.model_dump(mode="json"),
            "available_toolkits": list(self.available_toolkits),
            "execution_policy": "Return requested tool operations only; do not claim they have run.",
        }
        return await self._call(payload, TypeAdapter(list[ToolCallRequest]))


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
    ):
        super().__init__(model=model, model_group=model_group, model_name=model_name, prompt=prompt)
        self.available_toolkits = tuple(available_toolkits)

    async def adjudicate(self, task: AnalysisTask, reports: Sequence[AnalystReport]) -> AnalystReport:
        """Synthesize a final analysis report from worker reports."""

        payload = {
            "task": task.model_dump(mode="json"),
            "worker_reports": [report.model_dump(mode="json") for report in reports],
            "available_toolkits": list(self.available_toolkits),
        }
        figures = [figure for report in reports for figure in report.figures]
        return await self._call(payload, TypeAdapter(AnalystReport), images=figures)

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
        return await self._call(payload, TypeAdapter(ReviewVerdict), images=figures)

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
        return await self._call(payload, TypeAdapter(list[ToolCallReview]))


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


def _default_sources() -> dict[str, QuerySource]:
    return {
        "arxiv": ArxivSource(),
        "nasa_ads": NasaAdsSource(),
        "web": WebSource(),
    }
