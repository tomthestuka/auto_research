"""SQLite-backed retrieval for local papers and memory."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from auto_research.storage import Storage, database_path


HitKind = Literal["paper", "memory"]


class MemoryHit(BaseModel):
    """One local retrieval hit."""

    model_config = ConfigDict(extra="forbid")

    kind: HitKind
    id: int
    score: int = Field(ge=0)
    title: str
    snippet: str | None = None
    source: str | None = None
    source_id: str | None = None
    local_path: Path | None = None


class RetrievalBackend(Protocol):
    """Local retrieval interface."""

    async def text_search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        """Search local text metadata."""

    async def vector_search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        """Search vector embeddings."""


class SqliteRetrievalBackend:
    """Text retrieval over SQLite papers and memory rows."""

    def __init__(self, storage: Storage | None = None):
        self.storage = storage or Storage(database_path())

    async def text_search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        """Search papers and memory using simple normalized token matching."""

        return self._text_search(query, limit)

    def _text_search(self, query: str, limit: int) -> list[MemoryHit]:
        terms = _terms(query)
        if not terms:
            return []
        with sqlite3.connect(self.storage.path) as conn:
            conn.row_factory = sqlite3.Row
            paper_rows = conn.execute("SELECT * FROM papers").fetchall()
            memory_rows = conn.execute("SELECT * FROM memory").fetchall()

        hits: list[MemoryHit] = []
        for row in paper_rows:
            haystack = " ".join(
                str(part or "")
                for part in (
                    row["title"],
                    row["abstract"],
                    row["keywords_json"],
                    row["local_path"],
                    row["source_id"],
                )
            )
            score = _score(terms, haystack)
            if score:
                hits.append(
                    MemoryHit(
                        kind="paper",
                        id=row["id"],
                        score=score,
                        title=row["title"],
                        snippet=row["abstract"],
                        source=row["source"],
                        source_id=row["source_id"],
                        local_path=Path(row["local_path"]) if row["local_path"] else None,
                    )
                )

        for row in memory_rows:
            haystack = " ".join(str(part or "") for part in (row["summary"], row["tags_json"]))
            score = _score(terms, haystack)
            if score:
                hits.append(
                    MemoryHit(
                        kind="memory",
                        id=row["id"],
                        score=score,
                        title=f"memory:{row['id']}",
                        snippet=row["summary"],
                    )
                )

        hits.sort(key=lambda hit: (-hit.score, hit.kind, hit.id))
        return hits[:limit]

    async def vector_search(self, query: str, *, limit: int = 10) -> list[MemoryHit]:
        """Phase 2 stub for sqlite-vec retrieval."""

        _ = (query, limit)
        raise NotImplementedError("vector_search is deferred to Phase 2")


def _terms(query: str) -> list[str]:
    return [
        term
        for term in re.split(r"[^a-z0-9]+", query.lower())
        if term and not (len(term) == 1 and term.isdigit())
    ]


def _score(terms: list[str], text: str) -> int:
    normalized = " ".join(_terms(text))
    collapsed = normalized.replace(" ", "")
    score = 0
    for term in terms:
        if term in normalized:
            score += 2
        elif term in collapsed:
            score += 1
    query_collapsed = "".join(terms)
    if query_collapsed and query_collapsed in collapsed:
        score += len(terms)
    return score
