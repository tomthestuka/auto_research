"""Deterministic tests for step-4 SQLite storage."""

import asyncio

from auto_research.storage import (
    MemoryRecord,
    PaperRecord,
    QueryLogRecord,
    Storage,
    database_path,
    ingest_local_papers,
    librarian_io_enrich_stub,
    paper_from_local_file,
    paper_from_search_result,
)


def test_database_path_uses_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))

    assert database_path() == tmp_path / "auto_research.sqlite3"


def test_write_then_read_back_paper(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")

    async def run():
        await db.initialize()
        paper_id = await db.upsert_paper(
            PaperRecord(
                source="arxiv",
                source_id="1234.5678",
                title="A Paper",
                authors=["A. Scientist"],
                year=2026,
                abstract="Short abstract.",
                keywords=["black holes"],
            )
        )
        return await db.get_paper(paper_id)

    paper = asyncio.run(run())

    assert paper is not None
    assert paper.title == "A Paper"
    assert paper.authors == ["A. Scientist"]
    assert paper.keywords == ["black holes"]


def test_upsert_paper_updates_existing_row(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")

    async def run():
        await db.initialize()
        first = await db.upsert_paper(PaperRecord(source="web", source_id="x", title="Old"))
        second = await db.upsert_paper(PaperRecord(source="web", source_id="x", title="New"))
        papers = await db.list_papers()
        return first, second, papers

    first, second, papers = asyncio.run(run())

    assert first == second
    assert len(papers) == 1
    assert papers[0].title == "New"


def test_memory_and_query_log_insert(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")

    async def run():
        await db.initialize()
        memory_id = await db.insert_memory(MemoryRecord(summary="Useful memory", tags=["xray"]))
        query_id = await db.log_query(
            QueryLogRecord(
                role="librarian_io",
                query="ixpe black holes",
                source="arxiv",
                n_results=3,
                latency_ms=12.5,
            )
        )
        return memory_id, query_id

    memory_id, query_id = asyncio.run(run())

    assert memory_id == 1
    assert query_id == 1


def test_paper_from_local_file_hashes_file(tmp_path):
    paper_path = tmp_path / "Example Paper.pdf"
    paper_path.write_bytes(b"pdf-ish")

    paper = paper_from_local_file(paper_path)

    assert paper.source == "local_file"
    assert paper.title == "Example Paper"
    assert paper.local_path == paper_path
    assert len(paper.source_id) == 64
    assert paper.source_id == paper.sha256


def test_ingest_local_papers_reads_pdf_directory(tmp_path):
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    (papers_dir / "One.pdf").write_bytes(b"one")
    (papers_dir / "Two.txt").write_text("not a pdf", encoding="utf-8")
    (papers_dir / "Three.pdf").write_bytes(b"three")
    db = Storage(tmp_path / "test.sqlite3")

    async def run():
        await db.initialize()
        return await ingest_local_papers(db, papers_dir)

    papers = asyncio.run(run())

    assert [paper.title for paper in papers] == ["One", "Three"]


def test_paper_from_search_result_preserves_source_metadata(tmp_path):
    from auto_research.schemas import SearchResult

    path = tmp_path / "paper.pdf"
    path.write_bytes(b"pdf")
    result = SearchResult(
        source="arxiv",
        title="Metadata Title",
        authors=["A. Author"],
        abstract="Abstract",
        identifiers={"arxiv_id": "1234.5678"},
        keywords=["astro-ph.HE"],
    )

    paper = paper_from_search_result(result, path)

    assert paper.source == "arxiv"
    assert paper.source_id == "1234.5678"
    assert paper.title == "Metadata Title"
    assert paper.authors == ["A. Author"]
    assert paper.sha256 is not None


def test_librarian_io_enrich_stub_is_callable(tmp_path):
    db = Storage(tmp_path / "test.sqlite3")

    async def run():
        await db.initialize()
        await librarian_io_enrich_stub(db, 1)

    asyncio.run(run())
