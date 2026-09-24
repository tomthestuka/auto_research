"""The shared paper reader: native PDF, one receipt per paper for the whole run, any role."""

from __future__ import annotations

import asyncio
from pathlib import Path

from auto_research.agents.paper_tools import PaperReader, paper_reader_tool
from auto_research.provenance import ReceiptStore, ScopedLookup


def _text_pdf(path: Path, text: str) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure()
    fig.text(0.1, 0.5, text)
    fig.savefig(path)
    plt.close(fig)
    return path


def _run(coro):
    return asyncio.run(coro)


def test_the_same_pdf_opened_by_two_sources_is_one_receipt(tmp_path):
    """One paper, one handle: the reviewer re-opening the librarian's PDF cites lib:p1."""
    pdf = _text_pdf(tmp_path / "lag.pdf", "The lag is 12.5 ± 3.1 days.")
    store = ReceiptStore(tmp_path / "receipts")
    librarian = PaperReader(store, store, source="lib")
    first, receipt = _run(librarian.open_pdf(pdf, title="Lag", ids={"arxiv": "2101.00001"}))
    assert receipt.handle == "lib:p1" and first.documents

    scope = ScopedLookup(store, own_source="rev")
    assert not scope.visible("lib:p1")
    reviewer = PaperReader(store, scope, source="rev")
    outcome = _run(reviewer.execute({"path": str(pdf)}))
    assert outcome.text.startswith("[lib:p1]") and outcome.documents
    assert scope.visible("lib:p1")  # admitted, so the reviewer's quotes against it resolve
    assert [r.handle for r in store.receipts(kind="paper")] == ["lib:p1"]


def test_open_by_handle_attaches_the_receipts_pdf(tmp_path):
    pdf = _text_pdf(tmp_path / "lag.pdf", "The lag is 12.5 ± 3.1 days.")
    store = ReceiptStore(tmp_path / "receipts")
    _run(PaperReader(store, store, source="lib").open_pdf(pdf, title="Lag"))
    scope = ScopedLookup(store, own_source="rev", admitted=["lib:p1"])
    outcome = _run(PaperReader(store, scope, source="rev").execute({"handle": "lib:p1"}))
    (document,) = outcome.documents
    assert document.path == pdf.resolve()
    unseen = _run(PaperReader(store, ScopedLookup(store, own_source="rev"), source="rev")
                  .execute({"handle": "lib:p1"}))
    assert unseen.is_error  # a paper outside the role's scope cannot be opened by handle


def test_a_new_pdf_is_minted_under_the_readers_source_with_kb_metadata(tmp_path):
    pdf = _text_pdf(tmp_path / "x.pdf", "Some measured value 7.2.")

    class Storage:
        async def list_papers(self):
            from types import SimpleNamespace

            return [SimpleNamespace(local_path=str(pdf), sha256=None, source="nasa_ads",
                                    title="A Real Title", source_id="2025ApJ...1..1A")]

    store = ReceiptStore(tmp_path / "receipts")
    _, receipt = _run(PaperReader(store, store, source="rev", storage=Storage()).open_pdf(pdf))
    assert receipt.handle == "rev:p1" and receipt.title == "A Real Title"
    assert receipt.ids == {"bibcode": "2025ApJ...1..1A"}


def test_the_lookup_may_be_a_callable_for_per_round_scopes(tmp_path):
    pdf = _text_pdf(tmp_path / "lag.pdf", "The lag is 12.5 ± 3.1 days.")
    store = ReceiptStore(tmp_path / "receipts")
    _run(PaperReader(store, store, source="lib").open_pdf(pdf, title="Lag"))
    scopes = [ScopedLookup(store, own_source="w0")]
    reader = PaperReader(store, lambda: scopes[-1], source="w0")
    _run(reader.open_pdf(pdf))
    scopes.append(ScopedLookup(store, own_source="w0"))  # the next round's fresh scope
    reader._attached.clear()
    _run(reader.open_pdf(pdf))
    assert scopes[-1].visible("lib:p1")


def test_paper_reader_tool_is_an_extra_tool_pair(tmp_path):
    definition, executor = paper_reader_tool(ReceiptStore(tmp_path), None, source="rev")
    assert definition["name"] == "read_paper" and callable(executor)
    assert set(definition["input_schema"]["properties"]) == {"path", "handle"}


def test_a_pdf_outside_the_knowledge_base_takes_its_id_from_the_downloaders_name(tmp_path):
    from auto_research.agents.paper_tools import format_ids, ids_from_stem

    assert ids_from_stem("nasa_ads_2022ApJ...934....4K") == {"bibcode": "2022ApJ...934....4K"}
    assert ids_from_stem("arxiv_2206.09972v2") == {"arxiv": "2206.09972v2"}
    assert ids_from_stem("GX339_4") == {}
    pdf = _text_pdf(tmp_path / "arxiv_2206.09972v2.pdf", "A value of 4.0 here.")
    store = ReceiptStore(tmp_path / "receipts")
    outcome, receipt = _run(PaperReader(store, store, source="rev").open_pdf(pdf))
    assert receipt.ids == {"arxiv": "2206.09972v2"} and "arXiv:2206.09972v2" in outcome.text
    assert format_ids({"doi": "10.1/x", "bibcode": "b"}) == "doi:10.1/x, bibcode b"

