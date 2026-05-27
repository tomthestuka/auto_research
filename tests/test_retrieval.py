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
            PaperRecord(source="local_file", source_id="gx", title="GX339_4", local_path=pdf)
        )
        # Paper without local_path — should not appear in paths result.
        await storage.upsert_paper(
            PaperRecord(source="arxiv", source_id="ax1", title="GX339_4 online")
        )
        return await text_search_paths("GX339-4", backend=backend)

    paths = asyncio.run(run())

    assert paths == [pdf]


def test_vector_search_paths_stub_raises(tmp_path):
    with pytest.raises(NotImplementedError):
        asyncio.run(vector_search_paths("GX339-4"))
