"""Deterministic tests for step-8 agent role wrappers."""

import asyncio
import json

from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    LibrarianSearcherAgent,
    ReviewerAgent,
)
from auto_research.models import ModelResponse
from auto_research.retrieval import MemoryHit
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    SearchRequest,
    SearchResult,
    document_from_file,
)


class FakeModel:
    def __init__(self, content):
        self.content = json.dumps(content)
        self.messages = None
        self.images = None

    async def call(self, messages, *, schema=None, images=(), documents=()):
        self.messages = messages
        self.images = images
        self.documents = documents
        assert schema is None
        return ModelResponse(content=self.content)


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


class FakeRetrieval:
    async def text_search(self, query: str, *, limit: int = 10):
        assert query == "Cyg X-1"
        assert limit == 1
        return [MemoryHit(kind="paper", id=7, score=3, title="Local Cyg X-1", snippet="Stored note.")]

    async def vector_search(self, query: str, *, limit: int = 10):
        raise NotImplementedError


def _user_payload(fake_model: FakeModel):
    user_message = fake_model.messages[1]
    marker = "Task data:\n"
    return json.loads(user_message.content.split(marker, 1)[1])


def test_reviewer_agent_returns_verdict():
    fake = FakeModel({"accepted": True, "rationale": "Evidence is sufficient."})
    agent = ReviewerAgent(model=fake, prompt="reviewer prompt")

    verdict = asyncio.run(agent.review(ResearchRequest(question="Check this result.")))

    assert verdict.accepted is True
    assert verdict.rationale == "Evidence is sufficient."
    assert fake.messages[0].content == "reviewer prompt"


def test_librarian_io_agent_searches_injected_source_and_validates_results():
    fake = FakeModel(
        [
            {
                "source": "arxiv",
                "title": "Cyg X-1 paper",
                "url": "https://example.org/cyg",
                "abstract": "Ranked summary.",
            }
        ]
    )
    agent = LibrarianIOAgent(sources={"arxiv": FakeSource()}, model=fake, prompt="librarian prompt")

    results = asyncio.run(
        agent.search(SearchRequest(query="Cyg X-1 polarization", sources=["arxiv"], max_results=1))
    )

    assert results[0].source == "arxiv"
    assert results[0].title == "Cyg X-1 paper"
    payload = _user_payload(fake)
    assert payload["source_results"][0]["abstract"] == "A relevant paper."


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


def test_librarian_searcher_agent_uses_injected_retrieval():
    fake = FakeModel([{"source": "local", "title": "Local Cyg X-1", "abstract": "Stored note."}])
    agent = LibrarianSearcherAgent(retrieval=FakeRetrieval(), model=fake, prompt="searcher prompt")

    results = asyncio.run(agent.search(SearchRequest(query="Cyg X-1", sources=["local"], max_results=1)))

    assert results[0].source == "local"
    payload = _user_payload(fake)
    assert payload["local_hits"][0]["id"] == 7


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
