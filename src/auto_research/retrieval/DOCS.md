# `auto_research.retrieval` — local-memory retrieval

## Purpose
Pure-Python tools for querying the local SQLite DB. No LLM is involved.
Phase 1 provides text/keyword retrieval; vector retrieval is a Phase 2 stub.
These tools are callable from the graph's `literature_node`, the reviewer, and
(Phase 2) the lead-scientist group.

## Architecture
Two public tool functions (one module each) plus an internal backend:

- `retrieval/text_search.py` — `text_search_paths()`: SQLite keyword match,
  returns `list[Path]` for papers with a downloaded PDF.
- `retrieval/vector_search.py` — `vector_search_paths()`: Phase 2 stub that
  raises `NotImplementedError`.
- `retrieval/backend.py` — `SqliteRetrievalBackend`: internal engine for
  `text_search()` over `papers`/`memory` tables; used by `text_search_paths()`.
  Text search uses lightweight normalized token matching across paper
  title/abstract/keywords/path/source id and memory summary/tags/mem_abbrev.
  One-character numeric tokens are ignored to avoid broad accidental matches.

The graph's `literature_node` calls `text_search_paths()` after ingest;
the reviewer receives the resulting `list[Path]` and loads them as full PDF
attachments.

Live retrieval verification is opt-in:
`AUTO_RESEARCH_LIVE_RETRIEVAL_TESTS=1 pytest tests/test_live_retrieval.py -s`.

## Interfaces / Contracts
- `text_search_paths(query, *, limit=10, backend=None) -> list[Path]`
  — pure Python, no LLM. Returns local PDF paths for keyword-matching papers.
  Only papers with a non-null `local_path` in the DB are included.
- `vector_search_paths(query, *, limit=10, backend=None) -> list[Path]`
  — Phase 2 stub; raises `NotImplementedError`.
- `RetrievalBackend` protocol:
  - `async text_search(query: str, *, limit: int = 10) -> list[MemoryHit]`
  - `async vector_search(query: str, *, limit: int = 10) -> list[MemoryHit]`
- `SqliteRetrievalBackend` implements `text_search()` and keeps
  `vector_search()` as a Phase 2 stub that raises `NotImplementedError`.
- Hits are ranked by descending integer score, then kind/id for deterministic
  ordering.

## Dependencies
- Internal: `auto_research.storage`.

## Dictionary-level reference
- `HitKind` — literal hit kind: `"paper"` or `"memory"`.
- `MemoryHit(kind: HitKind, id: int, score: int, title: str, snippet: str | None
  = None, source: str | None = None, source_id: str | None = None,
  local_path: Path | None = None)` — one internal retrieval result (not exposed
  to graph or agents directly; the public API returns `list[Path]`).
- `RetrievalBackend` — protocol for local retrieval backends.
- `SqliteRetrievalBackend(storage: Storage | None = None)` — Phase 1 backend.
  Methods: `text_search()` and `vector_search()`.
- `text_search_paths(query, *, limit=10, backend=None) -> list[Path]` — public
  tool. SQLite text retrieval; returns PDF paths only.
- `vector_search_paths(query, *, limit=10, backend=None) -> list[Path]` — public
  tool; Phase 2 stub.

## Changelog
- 2026-05-26 -> 2026-06-12 — Pure-Python local retrieval built: backend protocol,
  SQLite text search over papers/memory/`mem_abbrev`, deterministic ordering/tests, opt-in
  live test, and `vector_search()` retained as a deferred stub.
- 2026-06-12 — Compacted changelog; no retrieval behavior changes.
