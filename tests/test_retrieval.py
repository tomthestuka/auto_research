"""Deterministic tests for step-6 retrieval."""

import asyncio

import pytest

from auto_research.retrieval import SqliteRetrievalBackend
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
