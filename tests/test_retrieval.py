"""Deterministic tests for step-6 retrieval."""

import asyncio

import pytest

from auto_research.retrieval import SqliteRetrievalBackend, text_search_paths, vector_search_paths
from auto_research.storage import MemoryRecord, PaperRecord, Storage


def test_text_search_finds_paper_with_normalized_query(tmp_path):
    storage = Storage(tmp_path / "test.sqlite3")
    backend = SqliteRetrievalBackend(storage)

    async def run():
        await storage.initialize()
        await storage.upsert_paper(
            PaperRecord(
                source="local_file",
                source_id="gx",
                title="GX339_4",
                local_path=tmp_path / "GX339_4.pdf",
                sha256="a" * 64,
            )
        )
        return await backend.text_search("GX339-4")

    hits = asyncio.run(run())

    assert hits
    assert hits[0].kind == "paper"
    assert hits[0].title == "GX339_4"


def test_text_search_finds_memory(tmp_path):
    storage = Storage(tmp_path / "test.sqlite3")
    backend = SqliteRetrievalBackend(storage)

    async def run():
        await storage.initialize()
        await storage.insert_memory(MemoryRecord(summary="GX339-4 has useful timing data."))
        return await backend.text_search("timing")

    hits = asyncio.run(run())

    assert hits[0].kind == "memory"
    assert hits[0].snippet == "GX339-4 has useful timing data."


def test_vector_search_stub_raises(tmp_path):
    backend = SqliteRetrievalBackend(Storage(tmp_path / "test.sqlite3"))

    with pytest.raises(NotImplementedError):
        asyncio.run(backend.vector_search("GX339-4"))


def test_text_search_paths_returns_only_paths_with_local_file(tmp_path):
    storage = Storage(tmp_path / "test.sqlite3")
    backend = SqliteRetrievalBackend(storage)
    pdf = tmp_path / "GX339_4.pdf"
    pdf.write_bytes(b"%PDF")

    async def run():
        await storage.initialize()
        await storage.upsert_paper(
            PaperRecord(
                source="local_file", source_id="gx", title="GX339_4",
                local_path=pdf, sha256="a" * 64,
            )
        )
        # A no-PDF result lives as an orphan memory note (no local file) — it matches the
        # query but must not appear in the PDF-paths result.
        await storage.insert_memory(MemoryRecord(summary="GX339-4 online review, no PDF."))
        return await text_search_paths("GX339-4", backend=backend)

    paths = asyncio.run(run())

    assert paths == [pdf]


def test_text_search_paths_returns_pdf_for_memory_backed_paper(tmp_path):
    """A paper matched only via its memory abstraction still yields its PDF."""
    storage = Storage(tmp_path / "test.sqlite3")
    backend = SqliteRetrievalBackend(storage)
    pdf = tmp_path / "cygx1.pdf"
    pdf.write_bytes(b"%PDF")

    async def run():
        await storage.initialize()
        # Paper whose own metadata does NOT contain the query term...
        paper_id = await storage.upsert_paper(
            PaperRecord(
                source="arxiv", source_id="cx", title="Cyg X-1 paper",
                local_path=pdf, sha256="b" * 64,
            )
        )
        # ...but its abstraction does.
        await storage.insert_memory(
            MemoryRecord(paper_id=paper_id, summary="Discusses intra-observation polarization.")
        )
        return await text_search_paths("polarization", backend=backend)

    paths = asyncio.run(run())

    assert paths == [pdf]


def test_text_search_paths_returns_each_paper_once(tmp_path):
    """A paper with a matching abstraction is one item, so its PDF appears once."""
    storage = Storage(tmp_path / "test.sqlite3")
    backend = SqliteRetrievalBackend(storage)
    pdf = tmp_path / "timing.pdf"
    pdf.write_bytes(b"%PDF")

    async def run():
        await storage.initialize()
        paper_id = await storage.upsert_paper(
            PaperRecord(
                source="arxiv", source_id="t1", title="timing study",
                local_path=pdf, sha256="b" * 64,
            )
        )
        await storage.insert_memory(
            MemoryRecord(paper_id=paper_id, summary="timing analysis of the source")
        )
        hits = await backend.text_search("timing", require_local_path=True)
        paths = await text_search_paths("timing", backend=backend)
        return hits, paths

    hits, paths = asyncio.run(run())

    assert [hit.id for hit in hits] == [1]  # one item keyed by the paper id
    assert paths == [pdf]


def test_vector_search_paths_stub_raises(tmp_path):
    with pytest.raises(NotImplementedError):
        asyncio.run(vector_search_paths("GX339-4"))
