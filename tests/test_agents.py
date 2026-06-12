"""Deterministic tests for step-8 agent role wrappers."""

import asyncio
import json

import pytest

from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.models import ModelResponse
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    SearchRequest,
    SearchResult,
    document_from_file,
)
from auto_research.storage import MemoryRecord, Storage


class FakeModel:
    """Stand-in model returning a fixed structured object via native structured output."""

    def __init__(self, content):
        self.obj = content
        self.messages = None
        self.images = None
        self.schema = None

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto", images=(), documents=(), cache_prefix=False):
        self.messages = messages
        self.images = images
        self.documents = documents
        self.schema = schema
        return ModelResponse(parsed=self.obj)


class FakeSource:
    async def search(self, query: str, *, limit: int = 10):
        assert query == "Cyg X-1 polarization"
        assert limit == 1
        return [
            SearchResult(
                source="arxiv",
                title="Cyg X-1 paper",
                url="https://example.org/cyg",
                abstract="A relevant paper.",
            )
        ]


def _user_payload(fake_model: FakeModel):
    # The user message is now just the task-data JSON (the output schema is
    # enforced by the provider, not injected into the prompt).
    return json.loads(fake_model.messages[1].content)


def test_reviewer_agent_returns_verdict():
    fake = FakeModel({"accepted": True, "rationale": "Evidence is sufficient."})
    agent = ReviewerAgent(model=fake, prompt="reviewer prompt")

    verdict = asyncio.run(agent.review(ResearchRequest(question="Check this result.")))

    assert verdict.accepted is True
    assert verdict.rationale == "Evidence is sufficient."
    assert fake.messages[0].content == "reviewer prompt"


def test_call_fails_loudly_when_no_structured_output():
    """No salvage: a provider that returns no structured output raises, never fabricates."""

    class _NullModel:
        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            return ModelResponse(parsed=None)

    agent = ReviewerAgent(model=_NullModel(), prompt="p")
    with pytest.raises(ValueError):
        asyncio.run(agent.review(ResearchRequest(question="q")))


def test_call_fails_loudly_on_schema_mismatch():
    """No salvage: an off-schema structured result raises a validation error."""

    class _BadModel:
        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            return ModelResponse(parsed={"unexpected": "shape"})

    agent = ReviewerAgent(model=_BadModel(), prompt="p")
    with pytest.raises(Exception):
        asyncio.run(agent.review(ResearchRequest(question="q")))


def test_librarian_io_agent_searches_injected_source_and_returns_raw_results():
    # search() makes no LLM call — it returns raw source results directly.
    fake = FakeModel({})
    agent = LibrarianIOAgent(sources={"arxiv": FakeSource()}, model=fake, prompt="librarian prompt")

    results = asyncio.run(
        agent.search(SearchRequest(query="Cyg X-1 polarization", sources=["arxiv"], max_results=1))
    )

    assert results[0].source == "arxiv"
    assert results[0].title == "Cyg X-1 paper"
    assert results[0].abstract == "A relevant paper."
    assert fake.messages is None  # no LLM call was made


def test_librarian_io_agent_ingests_document_attachment(tmp_path):
    pdf = tmp_path / "GX339_4.pdf"
    pdf.write_bytes(b"%PDF-1.5\n")
    fake = FakeModel(
        {
            "paper_id": 3,
            "summary": "GX339-4 memory abstraction with spectural fit methods.",
            "tags": ["GX339-4", "spectural fit"],
        }
    )
    agent = LibrarianIOAgent(sources={}, model=fake, prompt="librarian prompt")
    document = document_from_file(pdf)

    memory = asyncio.run(agent.ingest_document(document, paper_id=3))

    assert memory.paper_id == 3
    assert "spectural fit" in memory.summary
    assert fake.documents == (document,) or list(fake.documents) == [document]
    payload = _user_payload(fake)
    assert payload["document"]["path"] == str(pdf)


def test_agent_extracts_json_from_wrapped_model_text():
    fake = FakeModel({"accepted": True, "rationale": "ok"})
    fake.content = 'Here is the result:\n{"accepted": true, "rationale": "ok"}\nDone.'
    agent = ReviewerAgent(model=fake, prompt="reviewer prompt")

    verdict = asyncio.run(agent.review(ResearchRequest(question="Check this result.")))

    assert verdict.accepted is True


def test_analyst_worker_agent_exposes_toolkit_as_available_context_only():
    fake = FakeModel({"summary": "Analysis complete.", "findings": ["One finding."]})
    agent = AnalystWorkerAgent(model=fake, prompt="worker prompt")

    report = asyncio.run(agent.analyze(AnalysisTask(question="Reproduce a spectral result.")))

    assert report.summary == "Analysis complete."
    payload = _user_payload(fake)
    assert payload["available_toolkits"] == ["research_toolkit.xspec_utils"]
    assert "xspec_utils" not in fake.messages[0].content


def test_analyst_lead_agent_adjudicates_reports():
    fake = FakeModel({"summary": "Final synthesis.", "findings": ["Workers agree."]})
    agent = AnalystLeadAgent(model=fake, prompt="lead prompt")
    report = AnalystReport(summary="Worker result.")

    final = asyncio.run(agent.adjudicate(AnalysisTask(question="Synthesize."), [report]))

    assert final.findings == ["Workers agree."]
    payload = _user_payload(fake)
    assert payload["worker_reports"][0]["summary"] == "Worker result."


def test_librarian_io_agent_compose_search_strategy():
    fake = FakeModel({"query": "Cyg X-1 X-ray polarization IXPE", "sources": ["arxiv"], "max_results": 5})
    agent = LibrarianIOAgent(sources={}, model=fake, prompt="librarian prompt")

    result = asyncio.run(
        agent.compose_search_strategy(
            ResearchRequest(question="What is the X-ray polarization of Cyg X-1?")
        )
    )

    assert result.query == "Cyg X-1 X-ray polarization IXPE"
    assert result.sources == ["arxiv"]
    payload = _user_payload(fake)
    assert "research_request" in payload
    assert payload["research_request"]["question"] == "What is the X-ray polarization of Cyg X-1?"


def test_librarian_io_agent_ingest_results_no_pdf(tmp_path):
    """ingest_results() with no PDF: stores metadata record with mem_abbrev=abstract."""

    stored_memories: list[MemoryRecord] = []

    class FakeStorage:
        async def upsert_paper(self, paper):
            return 42

        async def get_memory_for_paper(self, paper_id):
            return None

        async def replace_memory_for_paper(self, memory: MemoryRecord):
            stored_memories.append(memory)

    fake = FakeModel({})  # LLM should not be called for no-PDF path
    result = SearchResult(
        source="arxiv",
        title="Cyg X-1 polarization study",
        abstract="We measure hard X-ray polarization of Cyg X-1.",
        keywords=["X-ray", "polarization"],
    )
    agent = LibrarianIOAgent(sources={}, model=fake, prompt="librarian prompt")
    agent.storage = FakeStorage()

    asyncio.run(agent.ingest_results([result]))

    assert len(stored_memories) == 1
    mem = stored_memories[0]
    assert mem.paper_id == 42
    assert mem.mem_abbrev == "We measure hard X-ray polarization of Cyg X-1."
    assert "X-ray" in mem.tags
    assert fake.messages is None  # no LLM call made for no-PDF path
