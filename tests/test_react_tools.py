"""Deterministic tests for the reviewer knowledge-base tools and the _react
document-threading contract (Phase-2 reviewer-driven retrieval).

Covers: the KB tools (`text_search` + `open_paper`) are scoped together; `open_paper`
validates path/PDF and records opened paths; and `_react` accumulates a tool's documents
and re-attaches them to every subsequent model call.
"""

import asyncio

from auto_research.agents.react import ReadOnlyToolProvider, ToolOutcome
from auto_research.agents.roles import ReviewerAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.schemas import document_from_file


def _names(provider):
    return {d["name"] for d in provider.tool_definitions()}


def test_kb_tools_scoped_together(tmp_path):
    """text_search + open_paper are gated together: reviewer has both; lead/co-lead neither."""
    reviewer = ReadOnlyToolProvider(roots=[tmp_path])  # include_text_search default True
    assert {"text_search", "open_paper"} <= _names(reviewer)

    lead = ReadOnlyToolProvider(roots=[tmp_path], include_text_search=False)
    assert not ({"text_search", "open_paper"} & _names(lead))


def test_open_paper_attaches_pdf_and_records_path(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.5\nbody")
    provider = ReadOnlyToolProvider(roots=[tmp_path])

    out = asyncio.run(provider.execute("open_paper", {"path": str(pdf)}))

    assert not out.is_error
    assert len(out.documents) == 1
    assert out.documents[0].sha256
    assert provider.opened_paths == [pdf.resolve()]


def test_open_paper_rejects_out_of_root(tmp_path):
    provider = ReadOnlyToolProvider(roots=[tmp_path])
    out = asyncio.run(provider.execute("open_paper", {"path": "/etc/passwd"}))
    assert out.is_error
    assert provider.opened_paths == []


def test_open_paper_rejects_non_pdf(tmp_path):
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"not a pdf at all")
    provider = ReadOnlyToolProvider(roots=[tmp_path])
    out = asyncio.run(provider.execute("open_paper", {"path": str(fake)}))
    assert out.is_error
    assert provider.opened_paths == []


def test_react_threads_documents_into_subsequent_calls(tmp_path):
    """A document a tool attaches is re-sent on every later model call and returned."""
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.5\n")
    doc = document_from_file(pdf)

    class _FakeTools:
        def tool_definitions(self):
            return [
                {
                    "name": "open_paper",
                    "description": "d",
                    "input_schema": {"type": "object", "properties": {}, "required": []},
                }
            ]

        async def execute(self, name, arguments):
            return ToolOutcome(text="attached", documents=[doc])

    class _FakeModel:
        def __init__(self):
            self.documents_per_call: list[list] = []
            self._calls = 0

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.documents_per_call.append(list(documents))
            self._calls += 1
            if self._calls == 1:
                return ModelResponse(
                    tool_calls=[ToolCall(id="c1", name="open_paper", arguments={"path": str(pdf)})]
                )
            return ModelResponse(content="done")

    model = _FakeModel()
    agent = ReviewerAgent(model=model, prompt="p")

    messages, figures, documents = asyncio.run(
        agent._react(seed_messages=[], tools=_FakeTools(), action="review", cap=3)
    )

    # First call saw no documents; the second (after open_paper) saw the attached PDF.
    assert model.documents_per_call[0] == []
    assert [d.sha256 for d in model.documents_per_call[1]] == [doc.sha256]
    # The accumulated documents are returned for the terminal structured call.
    assert [d.sha256 for d in documents] == [doc.sha256]
