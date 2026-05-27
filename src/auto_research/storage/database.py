"""SQLite persistence for papers, memory, and query logs."""

import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


DB_FILENAME = "auto_research.sqlite3"


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL DEFAULT '[]',
    year INTEGER,
    abstract TEXT,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    local_path TEXT,
    sha256 TEXT,
    inserted_at TEXT NOT NULL,
    embedding BLOB,
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY,
    paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    query TEXT NOT NULL,
    source TEXT NOT NULL,
    n_results INTEGER NOT NULL,
    latency_ms REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    owner_table TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    embedding BLOB,
    created_at TEXT NOT NULL
);
"""


class PaperRecord(BaseModel):
    """Paper metadata accepted by storage."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    local_path: Path | None = None
    sha256: str | None = None


class StoredPaper(PaperRecord):
    """Paper metadata read back from storage."""

    id: int
    inserted_at: datetime


class MemoryRecord(BaseModel):
    """Memory item accepted by storage."""

    model_config = ConfigDict(extra="forbid")

    paper_id: int | None = None
    summary: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class QueryLogRecord(BaseModel):
    """Query log item accepted by storage."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1)
    query: str = Field(min_length=1)
    source: str = Field(min_length=1)
    n_results: int = Field(ge=0)
    latency_ms: float = Field(ge=0)


def runtime_data_dir() -> Path:
    """Return the runtime data directory."""

    return Path(os.getenv("AUTO_RESEARCH_RUNTIME_DIR", "~/.auto_research")).expanduser()


def database_path(data_dir: str | Path | None = None) -> Path:
    """Return the SQLite database path for a runtime data directory."""

    root = Path(data_dir).expanduser() if data_dir is not None else runtime_data_dir()
    return root / DB_FILENAME


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 digest for a local file."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def paper_from_local_file(path: str | Path) -> PaperRecord:
    """Create a minimal local-paper record from a file path."""

    file_path = Path(path)
    digest = sha256_file(file_path)
    return PaperRecord(
        source="local_file",
        source_id=digest,
        title=file_path.stem,
        local_path=file_path,
        sha256=digest,
    )


def paper_from_search_result(result, path: str | Path) -> PaperRecord:
    """Create a paper record from source metadata plus a downloaded file."""

    file_path = Path(path)
    digest = sha256_file(file_path)
    source_id = (
        result.identifiers.get("arxiv_id")
        or result.identifiers.get("bibcode")
        or result.identifiers.get("doi")
        or digest
    )
    return PaperRecord(
        source=result.source,
        source_id=source_id,
        title=result.title,
        authors=result.authors,
        year=result.published.year if result.published else None,
        abstract=result.abstract,
        keywords=result.keywords,
        local_path=file_path,
        sha256=digest,
    )


def safe_filename(name: str, *, suffix: str = ".pdf") -> str:
    """Return a filesystem-safe filename."""

    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return f"{cleaned[:180] or 'paper'}{suffix}"


class Storage:
    """Small async wrapper around a SQLite database file."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path is not None else database_path()

    async def initialize(self) -> None:
        """Create tables if needed."""

        self._initialize()

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(SCHEMA_SQL)

    async def upsert_paper(self, paper: PaperRecord) -> int:
        """Insert or update a paper and return its row id."""

        return self._upsert_paper(paper)

    def _upsert_paper(self, paper: PaperRecord) -> int:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """
                INSERT INTO papers (
                    source, source_id, title, authors_json, year, abstract,
                    keywords_json, local_path, sha256, inserted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                    title = excluded.title,
                    authors_json = excluded.authors_json,
                    year = excluded.year,
                    abstract = excluded.abstract,
                    keywords_json = excluded.keywords_json,
                    local_path = excluded.local_path,
                    sha256 = excluded.sha256
                """,
                (
                    paper.source,
                    paper.source_id,
                    paper.title,
                    json.dumps(paper.authors),
                    paper.year,
                    paper.abstract,
                    json.dumps(paper.keywords),
                    str(paper.local_path) if paper.local_path else None,
                    paper.sha256,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM papers WHERE source = ? AND source_id = ?",
                (paper.source, paper.source_id),
            ).fetchone()
        return int(row[0])

    async def get_paper(self, paper_id: int) -> StoredPaper | None:
        """Return one paper by row id."""

        return self._get_paper(paper_id)

    def _get_paper(self, paper_id: int) -> StoredPaper | None:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return _paper_from_row(row) if row else None

    async def list_papers(self) -> list[StoredPaper]:
        """Return all stored papers ordered by id."""

        return self._list_papers()

    def _list_papers(self) -> list[StoredPaper]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM papers ORDER BY id").fetchall()
        return [_paper_from_row(row) for row in rows]

    async def insert_memory(self, memory: MemoryRecord) -> int:
        """Insert a memory item and return its row id."""

        return self._insert_memory(memory)

    def _insert_memory(self, memory: MemoryRecord) -> int:
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO memory (paper_id, summary, tags_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    memory.paper_id,
                    memory.summary,
                    json.dumps(memory.tags),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    async def replace_memory_for_paper(self, memory: MemoryRecord) -> int:
        """Replace existing memory rows for a paper, then insert one row."""

        return self._replace_memory_for_paper(memory)

    def _replace_memory_for_paper(self, memory: MemoryRecord) -> int:
        if memory.paper_id is None:
            return self._insert_memory(memory)
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM memory WHERE paper_id = ?", (memory.paper_id,))
        return self._insert_memory(memory)

    async def log_query(self, query: QueryLogRecord) -> int:
        """Insert a query log row and return its row id."""

        return self._log_query(query)

    def _log_query(self, query: QueryLogRecord) -> int:
        with sqlite3.connect(self.path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO query_log (role, query, source, n_results, latency_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    query.role,
                    query.query,
                    query.source,
                    query.n_results,
                    query.latency_ms,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return int(cursor.lastrowid)


async def ingest_local_papers(storage: Storage, papers_dir: str | Path) -> list[StoredPaper]:
    """Ingest all PDFs in a directory as minimal local-file paper records."""

    root = Path(papers_dir)
    inserted: list[StoredPaper] = []
    for path in sorted(root.glob("*.pdf")):
        paper_id = await storage.upsert_paper(paper_from_local_file(path))
        paper = await storage.get_paper(paper_id)
        if paper is not None:
            inserted.append(paper)
    return inserted


async def librarian_io_enrich_stub(storage: Storage, paper_id: int) -> None:
    """Placeholder for librarian I/O PDF reading and abstraction.

    Phase 1 only records metadata and local files. The librarian I/O agent will
    later read downloaded PDFs, produce summaries/keywords, and write memory
    records through the storage API.
    """

    _ = (storage, paper_id)


def _paper_from_row(row: sqlite3.Row) -> StoredPaper:
    data: dict[str, Any] = dict(row)
    return StoredPaper(
        id=data["id"],
        source=data["source"],
        source_id=data["source_id"],
        title=data["title"],
        authors=json.loads(data["authors_json"]),
        year=data["year"],
        abstract=data["abstract"],
        keywords=json.loads(data["keywords_json"]),
        local_path=Path(data["local_path"]) if data["local_path"] else None,
        sha256=data["sha256"],
        inserted_at=datetime.fromisoformat(data["inserted_at"]),
    )
