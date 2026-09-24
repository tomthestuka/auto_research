"""Deterministic tests for the librarian as one research loop that exits only through turn_end.

Covers the tool surface (search → read in full → capture → report → turn_end), the capture layer
(every document hashed at capture, the reading copy is the whole text), the turn_end validator
(each rejection names its reason), and the agent loop end to end over a scripted model.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import auto_research.agents.librarian_tools as lt
from auto_research.agents.librarian_tools import LibrarianToolProvider
from auto_research.agents.roles import LibrarianIOAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.schemas import ResearchRequest, SearchResult
from auto_research.provenance import ReceiptStore, sha256_file
from auto_research.sources.receipts import existing_receipt, mint_paper_receipt, mint_web_receipt


# ── fakes ────────────────────────────────────────────────────────────────────────


class FakeSource:
    def __init__(self, results=None, error: Exception | None = None):
        self.results = results or []
        self.error = error

    async def search(self, query, *, limit=10):
        if self.error is not None:
            raise self.error
        return list(self.results)[:limit]


class FakeWeb:
    def __init__(self, page="# Page\nThe measured lag is 12 . 5 days.\n"):
        self.page = page

    async def fetch(self, url):
        return self.page


class FakeStorage:
    def __init__(self):
        self.papers = []
        self.memories = []

    async def upsert_paper(self, paper):
        self.papers.append(paper)
        return len(self.papers)

    async def insert_memory(self, memory):
        self.memories.append(memory)
        return len(self.memories)


def _hit(title="Lag paper", pdf=True):
    return SearchResult(
        source="arxiv",
        title=title,
        url="https://arxiv.org/abs/2101.00001",
        pdf_url="https://arxiv.org/pdf/2101.00001" if pdf else None,
        abstract="We measure a lag.",
        identifiers={"arxiv_id": "2101.00001"},
    )


def _fake_minter(text: str):
    """The real minter: under option A a paper receipt is the PDF itself, so there is nothing to fake.

    ``text`` is kept so call sites read as before; it no longer reaches the receipt.
    """
    return mint_paper_receipt


def _provider(tmp_path, *, sources=None, text="The lag is 12 . 5 ± 3 . 1 days.\n", events=None):
    def downloader(url, destination):
        destination.write_bytes(b"%PDF-1.5 fake " + url.encode())
        return destination

    async def record(event, **kwargs):
        if events is not None:
            events.append((event, kwargs))

    return LibrarianToolProvider(
        sources if sources is not None else {"arxiv": FakeSource([_hit()])},
        storage=FakeStorage(),
        store=ReceiptStore(tmp_path / "receipts"),
        report_path=tmp_path / "report" / "literature_pass1.jsonl",
        papers_dir=tmp_path / "papers",
        web_source=FakeWeb(),
        record=record,
        downloader=downloader,
        paper_minter=_fake_minter(text),
    )


def _run(coro):
    return asyncio.run(coro)


# ── search ───────────────────────────────────────────────────────────────────────


def test_search_numbers_hits_across_calls(tmp_path):
    p = _provider(tmp_path, sources={"arxiv": FakeSource([_hit("A"), _hit("B")])})
    first = _run(p.execute("search_literature", {"source": "arxiv", "query": "q"}))
    second = _run(p.execute("search_literature", {"source": "arxiv", "query": "q2"}))
    assert "[hit 1] A" in first.text and "[hit 2] B" in first.text
    assert "[hit 3] A" in second.text
    assert "arXiv:2101.00001" in first.text and "PDF available" in first.text


def test_failing_source_is_an_error_outcome_and_a_recorded_event(tmp_path):
    """[I9]: a spent key or quota shows up in the run record rather than silently zeroing a source."""
    events = []
    p = _provider(tmp_path, sources={"nasa_ads": FakeSource(error=RuntimeError("401 bad token"))},
                  events=events)
    outcome = _run(p.execute("search_literature", {"source": "nasa_ads", "query": "q"}))
    assert outcome.is_error and "401 bad token" in outcome.text
    assert [e for e, _ in events] == ["librarian.source.error"]


# ── reading ──────────────────────────────────────────────────────────────────────


def test_read_paper_captures_hashes_and_stores_once(tmp_path):
    p = _provider(tmp_path)
    _run(p.execute("search_literature", {"source": "arxiv", "query": "q"}))
    outcome = _run(p.execute("read_paper", {"hit": 1}))
    assert not outcome.is_error
    assert outcome.text.startswith("[lib:p1] Lag paper") and "PDF is attached" in outcome.text
    # The PDF itself is what the model reads: attached natively, never the checked text layer.
    (document,) = outcome.documents
    assert document.path == tmp_path / "papers" / "arxiv_2101.00001.pdf"
    assert document.title.startswith("[lib:p1]") and "12 . 5" not in outcome.text
    receipt = p.captures["lib:p1"]
    pdf = tmp_path / "papers" / "arxiv_2101.00001.pdf"
    assert receipt.kind == "paper" and receipt.source_sha256 == sha256_file(pdf)
    assert receipt.ids == {"arxiv": "2101.00001", "url": "https://arxiv.org/abs/2101.00001"}
    assert receipt.source_path == pdf.resolve()
    assert p.store.verify("lib:p1") is None
    # Reopening the same document returns the same handle and stores no second paper row.
    again = _run(p.execute("read_paper", {"hit": 1}))
    assert "already attached" in again.text and not again.documents and len(p.captures) == 1
    assert len(p.storage.papers) == 1 and p.paper_ids == {"lib:p1": 1}


def test_read_paper_without_a_pdf_points_at_web_fetch_and_the_gaps(tmp_path):
    p = _provider(tmp_path, sources={"arxiv": FakeSource([_hit(pdf=False)])})
    _run(p.execute("search_literature", {"source": "arxiv", "query": "q"}))
    outcome = _run(p.execute("read_paper", {"hit": 1}))
    assert outcome.is_error and "web_fetch" in outcome.text and "gaps" in outcome.text


def test_read_paper_rejects_an_unknown_hit(tmp_path):
    p = _provider(tmp_path)
    assert _run(p.execute("read_paper", {"hit": 4})).is_error


def test_web_fetch_captures_the_page_as_a_web_handle(tmp_path):
    p = _provider(tmp_path)
    outcome = _run(p.execute("web_fetch", {"url": "https://docs.example.org/lag"}))
    assert outcome.text.startswith("[lib:w1]") and "12 . 5 days" in outcome.text
    receipt = p.captures["lib:w1"]
    assert receipt.kind == "web" and receipt.ids == {"url": "https://docs.example.org/lag"}
    assert p.store.verify("lib:w1") is None


# ── report + turn_end ────────────────────────────────────────────────────────────


def _read_one(tmp_path):
    p = _provider(tmp_path)
    _run(p.execute("search_literature", {"source": "arxiv", "query": "q"}))
    _run(p.execute("read_paper", {"hit": 1}))
    return p


def _report(support=("lib:p1",), status="succeeded"):
    return {
        "summary": "One paper measures the lag.",
        "findings": [{"claim": 'A lag of {{lib:p1 "12.5 ± 3.1 days" Table 1}}.', "support": list(support)}],
        "gaps": [],
        "status": status,
    }


def test_turn_end_without_a_report_is_rejected_with_the_reason(tmp_path):
    p = _read_one(tmp_path)
    outcome = _run(p.execute("turn_end", {"summary": "done"}))
    assert outcome.is_error and "No report has been written" in outcome.text


def test_edit_report_persists_jsonl_and_flags_an_unopened_handle(tmp_path):
    p = _read_one(tmp_path)
    outcome = _run(p.execute("edit_report", _report(support=("lib:p1", "lib:p7"))))
    assert not outcome.is_error and "lib:p7" in outcome.text
    saved = json.loads(p.report_path.read_text())
    assert saved["findings"][0]["support"] == ["lib:p1", "lib:p7"]


@pytest.mark.parametrize(
    ("report", "reason"),
    [
        (_report(support=()), "cites no document"),
        (_report(support=("lib:p7",)), "not a document you opened"),
        ({"summary": "x", "findings": [], "status": "succeeded"}, "lists no findings"),
        ({"summary": " ", "findings": [], "status": "failed"}, "summary is empty"),
    ],
)
def test_turn_end_names_each_problem(tmp_path, report, reason):
    p = _read_one(tmp_path)
    _run(p.execute("edit_report", report))
    outcome = _run(p.execute("turn_end", {"summary": "done"}))
    assert outcome.is_error and reason in outcome.text


def test_turn_end_rejects_a_document_changed_since_it_was_read(tmp_path):
    p = _read_one(tmp_path)
    _run(p.execute("edit_report", _report()))
    Path(p.captures["lib:p1"].path).write_text("tampered", encoding="utf-8")
    outcome = _run(p.execute("turn_end", {"summary": "done"}))
    assert outcome.is_error and "no longer match" in outcome.text


def test_turn_end_accepts_an_honest_failure_with_no_findings(tmp_path):
    p = _provider(tmp_path)
    _run(p.execute("edit_report", {"summary": "No source reachable.", "gaps": ["ADS 401"],
                                   "status": "failed"}))
    assert not _run(p.execute("turn_end", {"summary": "failed"})).is_error


def test_edit_report_schema_carries_the_model_descriptions_and_no_refs(tmp_path):
    spec = next(d for d in _provider(tmp_path).tool_definitions() if d["name"] == "edit_report")
    schema = json.dumps(spec["input_schema"])
    assert "$ref" not in schema and "$defs" not in schema
    item = spec["input_schema"]["properties"]["findings"]["items"]["properties"]
    assert item["support"]["description"].startswith("Handles of the documents")


# ── captures ─────────────────────────────────────────────────────────────────────


def _text_pdf(path: Path, text: str) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure()
    fig.text(0.1, 0.5, text)
    fig.savefig(path)
    plt.close(fig)
    return path


def test_a_paper_receipt_is_the_pdf_itself_and_is_cited_not_quoted(tmp_path):
    """Option A (human ruling 2026-09-24): a paper is cited by handle and never text-matched."""
    from auto_research.provenance import check_bindings

    pdf = _text_pdf(tmp_path / "lag.pdf", "The lag is 12.5 ± 3.1 days.")
    store = ReceiptStore(tmp_path / "receipts")
    receipt = mint_paper_receipt(store, pdf, source="lib", title="Lag", ids={"arxiv": "2101.00001"})
    assert receipt.handle == "lib:p1" and receipt.parser is None
    assert Path(receipt.path) == pdf.resolve() == receipt.source_path
    assert receipt.sha256 == receipt.source_sha256 == sha256_file(pdf)
    assert store.text("lib:p1") is None  # no text is ever extracted from a paper
    assert mint_paper_receipt(store, pdf, source="lib", title="Lag").handle == "lib:p1"
    placeless = check_bindings('{{lib:p1 "12.5 ± 3.1 days"}}', store, own_source="lib")
    assert placeless.problems and "without saying where" in placeless.problems[0]
    attributed = check_bindings('{{lib:p1 "12.5 ± 3.1 days" Sec. 2}}', store, own_source="lib")
    assert not attributed.problems  # attributed, not text-matched


def test_a_paper_citation_never_exempts_the_numbers_beside_it(tmp_path):
    """Human ruling 2026-09-24: each paper value is attributed; every other number is checked."""
    p = _read_one(tmp_path)
    _run(p.execute("web_fetch", {"url": "https://docs.example.org/lag"}))  # lib:w1: "12 . 5 days"
    report = {
        "summary": "Two sources.",
        "findings": [
            # An unattributed paper number: refused, with the attribution hint.
            {"claim": "The paper reports a lag of 99.9 days [lib:p1].", "support": ["lib:p1"]},
            # Attributed to the paper: accepted, collected for a person to check.
            {"claim": 'The paper reports {{lib:p1 "a lag of 12.5 ± 3.1 days" Table 1}}.', "support": ["lib:p1"]},
            # A web-only number no page carries: refused.
            {"claim": "The page reports a lag of 77.7 days.", "support": ["lib:w1"]},
        ],
        "status": "succeeded",
    }
    _run(p.execute("edit_report", report))
    outcome = _run(p.execute("turn_end", {"summary": "done"}))
    assert outcome.is_error and "99.9" in outcome.text and "77.7" in outcome.text
    assert "attribute it" in outcome.text
    assert "Finding 2" not in outcome.text  # the attributed finding raised nothing
    values = p.attributed_values(p.written_report)
    assert [(v.handle, v.value, v.locator) for v in values] == [("lib:p1", "a lag of 12.5 ± 3.1 days", "Table 1")]
    assert p.scope.get(values[0].handle).title == "Lag paper"  # title resolves from the receipt


def test_an_attribution_must_say_where_in_the_paper(tmp_path):
    p = _read_one(tmp_path)
    report = _report()
    report["findings"][0]["claim"] = 'A lag of {{lib:p1 "12.5 ± 3.1 days"}}.'
    _run(p.execute("edit_report", report))
    outcome = _run(p.execute("turn_end", {"summary": "done"}))
    assert outcome.is_error and "without saying where" in outcome.text


def test_a_cited_pdf_that_changed_is_refused(tmp_path):
    pdf = _text_pdf(tmp_path / "lag.pdf", "The lag is 12.5 ± 3.1 days.")
    store = ReceiptStore(tmp_path / "receipts")
    mint_paper_receipt(store, pdf, source="lib", title="Lag")
    pdf.write_bytes(pdf.read_bytes() + b"%tampered")
    assert store.verify("lib:p1") is not None


def test_a_file_that_is_not_a_pdf_mints_nothing(tmp_path):
    html = tmp_path / "landing.pdf"
    html.write_text("<html>paywall</html>")
    store = ReceiptStore(tmp_path / "receipts")
    assert mint_paper_receipt(store, html, source="lib", title="Landing") is None


def test_web_receipt_is_reused_for_the_same_extract_and_new_for_a_changed_one(tmp_path):
    store = ReceiptStore(tmp_path)
    a = mint_web_receipt(store, "https://x.org", "same text", source="lib")
    b = mint_web_receipt(store, "https://x.org", "same text", source="lib")
    c = mint_web_receipt(store, "https://x.org", "changed text", source="lib")
    assert a.handle == b.handle == "lib:w1" and c.handle == "lib:w2"
    assert a.parser == "tavily-extract" and store.verify(a.handle) is None


def test_receipts_survive_a_new_store_on_the_same_directory(tmp_path):
    """Resume: a reopened document keeps its handle across a process boundary."""
    mint_web_receipt(ReceiptStore(tmp_path), "https://x.org", "page", source="lib")
    again = mint_web_receipt(ReceiptStore(tmp_path), "https://x.org", "page", source="lib")
    assert again.handle == "lib:w1"


# ── the agent loop ───────────────────────────────────────────────────────────────


class _ScriptedModel:
    """Emits a fixed sequence of tool-call turns; records the tool results it was shown."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        self.calls += 1
        name, args = self.turns.pop(0)
        return ModelResponse(tool_calls=[ToolCall(id=f"c{self.calls}", name=name, arguments=args)])


def _agent(tmp_path, monkeypatch, turns):
    monkeypatch.setattr(lt, "mint_paper_receipt", _fake_minter("The lag is 12 . 5 ± 3 . 1 days.\n"))
    monkeypatch.setattr("auto_research.agents.roles.papers_dir", lambda: tmp_path / "papers")

    def downloader(url, destination):
        destination.write_bytes(b"%PDF-1.5 " + url.encode())
        return destination

    monkeypatch.setattr("auto_research.sources.downloads.download_pdf", downloader)
    storage = FakeStorage()
    agent = LibrarianIOAgent(
        sources={"arxiv": FakeSource([_hit()])}, storage=storage,
        model=_ScriptedModel(turns), prompt="librarian prompt",
    )
    agent.report_dir = tmp_path / "librarian_io"
    return agent, storage


def test_research_loop_files_a_report_and_lays_down_memory(tmp_path, monkeypatch):
    agent, storage = _agent(tmp_path, monkeypatch, [
        ("search_literature", {"source": "arxiv", "query": "lag"}),
        ("read_paper", {"hit": 1}),
        ("edit_report", _report()),
        ("turn_end", {"summary": "done"}),
    ])
    report = _run(agent.research(ResearchRequest(question="What lag is measured?")))
    assert report.status == "succeeded" and report.findings[0].support == ["lib:p1"]
    assert list(agent.last_receipts) == ["lib:p1"]
    # The store lives beside the ledger, outside every role's allow-list (0f, laundering fix).
    assert ReceiptStore.for_run(tmp_path / "librarian_io").ledger.is_file()
    assert (tmp_path / "librarian_io" / "literature_pass1.jsonl").is_file()
    assert [m.paper_id for m in storage.memories] == [1]
    assert "12.5 ± 3.1" in storage.memories[0].mem_abbrev
    assert [(v.handle, v.locator) for v in report.attributed_values] == [("lib:p1", "Table 1")]
    saved = json.loads((tmp_path / "librarian_io" / "literature_pass1.jsonl").read_text())
    assert saved["attributed_values"][0]["value"] == "12.5 ± 3.1 days"


def test_research_loop_stays_in_the_loop_after_a_rejected_turn_end(tmp_path, monkeypatch):
    agent, _ = _agent(tmp_path, monkeypatch, [
        ("search_literature", {"source": "arxiv", "query": "lag"}),
        ("read_paper", {"hit": 1}),
        ("edit_report", _report(support=("lib:p9",))),
        ("turn_end", {"summary": "done"}),  # rejected: lib:p9 was never opened
        ("edit_report", _report()),
        ("turn_end", {"summary": "done"}),
    ])
    report = _run(agent.research(ResearchRequest(question="q")))
    assert report.findings[0].support == ["lib:p1"]
    assert agent.model.calls == 6


# ── arXiv matching ───────────────────────────────────────────────────────────────


class _RecordingArxivClient:
    """Stands in for ``arxiv.Client``: records each query and answers from a table."""

    def __init__(self, answers):
        self.answers = answers
        self.queries = []

    def results(self, search):
        self.queries.append(search.query)
        return iter(self.answers.get(search.query, []))


def _arxiv_item(title):
    from types import SimpleNamespace

    return SimpleNamespace(
        title=title, entry_id="http://arxiv.org/abs/2206.09972v2", summary="s", authors=[],
        published=None, get_short_id=lambda: "2206.09972v2", categories=[],
        pdf_url="http://arxiv.org/pdf/2206.09972v2",
    )


def test_arxiv_prefers_papers_matching_every_term():
    from auto_research.sources.arxiv_source import ArxivSource

    client = _RecordingArxivClient({"all:disk AND all:jet": [_arxiv_item("Disk-jet geometry")]})
    results, match = _run(ArxivSource(client=client).search_with_match("disk jet"))
    assert match == "all" and client.queries == ["all:disk AND all:jet"]
    assert results[0].title == "Disk-jet geometry"


def test_arxiv_falls_back_to_a_loose_match_and_the_tool_says_so(tmp_path):
    from auto_research.sources.arxiv_source import ArxivSource

    client = _RecordingArxivClient({"disk jet rare": [_arxiv_item("Loose hit")]})
    source = ArxivSource(client=client)
    _, match = _run(source.search_with_match("disk jet rare"))
    assert match == "any" and client.queries == ["all:disk AND all:jet AND all:rare", "disk jet rare"]
    p = _provider(tmp_path, sources={"arxiv": source})
    outcome = _run(p.execute("search_literature", {"source": "arxiv", "query": "disk jet rare"}))
    assert outcome.text.startswith("(No paper matched all of your terms")


def test_arxiv_sends_field_syntax_as_written():
    from auto_research.sources.arxiv_source import ArxivSource

    client = _RecordingArxivClient({})
    _, match = _run(ArxivSource(client=client).search_with_match('au:Smith AND abs:"lag"'))
    assert match == "as_written" and client.queries == ['au:Smith AND abs:"lag"']



def test_a_librarian_cannot_cite_another_roles_receipt(tmp_path):
    """Its scope is what it opened; a worker's receipt in the same store grounds nothing."""
    p = _read_one(tmp_path)
    other = p.store.mint(source="analyst_worker_0", kind="result", produced_by="fit",
                         data="The fitted lag is 7.7 days.")
    report = _report()
    report["findings"].append({"claim": f"A fit gives 7.7 days [{other.handle}].", "support": ["lib:p1"]})
    _run(p.execute("edit_report", report))
    outcome = _run(p.execute("turn_end", {"summary": "done"}))
    assert outcome.is_error and "not a receipt you can cite" in outcome.text
