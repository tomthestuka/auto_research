# `auto_research.storage` — SQLite persistence

## Purpose
Persists everything durable: paper metadata + abstracts + keywords, memory items
(librarian abstractions), and a query log. Lives in the runtime data dir
([CLAUDE.md §9](../../../CLAUDE.md#9-tech-stack--conventions)), not in the repo.

## Architecture
A small set of tables, accessed through an async-signature wrapper around the
stdlib `sqlite3` driver. Phase 1 operations are tiny and execute synchronously
inside those async methods; callers still use async APIs so later I/O hardening
does not change boundaries.

Schema definition is centralized in `database.py` so Phase 2 vector retrieval can
slot in without migrations: embedding space is reserved both as nullable
`embedding` columns and an `embeddings` table
([CLAUDE.md §13](../../../CLAUDE.md#13-phase-2-readiness)). The intended Phase 2
backend is `sqlite-vec`, which co-exists in the same DB file.

Runtime DB path:
- `AUTO_RESEARCH_RUNTIME_DIR/auto_research.sqlite3`
- default when unset: `~/.auto_research/auto_research.sqlite3`

Local development ingestion logs are written under `logs/dev/step-4/`.

## Interfaces / Contracts
- Tables:
  - `papers(id, source, source_id, title, authors_json, year, abstract,
    keywords_json, local_path, sha256, inserted_at, embedding)`.
  - `memory(id, paper_id, summary, tags_json, created_at, embedding)`.
  - `query_log(id, role, query, source, n_results, latency_ms, created_at)`.
  - `embeddings(id, owner_table, owner_id, embedding, created_at)` reserved for
    Phase 2.
- `papers` has a unique `(source, source_id)` constraint. Upserts update
  metadata but preserve the original row id.
- Local PDFs are ingested as `source="local_file"`, `source_id=<sha256>`, title
  from filename stem, `local_path`, and `sha256`.
- Downloaded source PDFs are ingested with source metadata when available. For
  example, arXiv downloads use `source="arxiv"` and `source_id=<arxiv_id>`.
- `librarian_io_enrich_stub()` is the documented handoff point for the librarian
  I/O team to read downloaded PDFs, extract abstractions/keywords, and write
  `memory` rows. It intentionally does no work until agent roles land.
- Tests use temp dirs and must not touch the real runtime DB.

## Dependencies
- External: stdlib `sqlite3`, `pydantic` (Phase 2 will add `sqlite-vec`).
- Imported by: `auto_research.retrieval`, librarian agents via
  `auto_research.sources`.

## Dictionary-level reference
- `DB_FILENAME: str` — `"auto_research.sqlite3"`.
- `SCHEMA_SQL: str` — SQLite schema for papers, memory, query logs, and reserved
  embedding space.
- `PaperRecord(source: str, source_id: str, title: str, authors: list[str] = [],
  year: int | None = None, abstract: str | None = None, keywords: list[str] =
  [], local_path: Path | None = None, sha256: str | None = None)` — paper input
  model.
- `StoredPaper(PaperRecord)` — read model with `id` and `inserted_at`.
- `MemoryRecord(paper_id: int | None = None, summary: str, tags: list[str] =
  [])` — memory input model.
- `QueryLogRecord(role: str, query: str, source: str, n_results: int,
  latency_ms: float)` — query log input model.
- `runtime_data_dir() -> Path` — returns `AUTO_RESEARCH_RUNTIME_DIR` or
  `~/.auto_research`.
- `database_path(data_dir: str | Path | None = None) -> Path` — returns the
  SQLite DB path for a runtime data dir.
- `sha256_file(path: str | Path) -> str` — returns a file digest.
- `paper_from_local_file(path: str | Path) -> PaperRecord` — builds minimal
  metadata for a local PDF or other file.
- `paper_from_search_result(result: SearchResult, path: str | Path) ->
  PaperRecord` — builds storage metadata from a source search result plus a
  downloaded PDF path.
- `Storage(path: str | Path | None = None)` — SQLite wrapper. Methods:
  `initialize()`, `upsert_paper(paper)`, `get_paper(paper_id)`,
  `list_papers()`, `insert_memory(memory)`, `log_query(query)`.
- `ingest_local_papers(storage: Storage, papers_dir: str | Path) ->
  list[StoredPaper]` — upserts all `*.pdf` files in a directory.
- `librarian_io_enrich_stub(storage: Storage, paper_id: int) -> None` —
  no-op Phase 1 stub where the librarian I/O team will later read PDFs and write
  memory abstractions.
- `auto_research.storage.ingest_local.ingest_and_log(papers_dir: Path) -> dict`
  — developer utility that ingests PDFs and writes a `logs/dev/step-4/` JSON
  log.
- `python -m auto_research.storage.ingest_local <papers_dir>` — CLI form of the
  local paper ingestion utility.

## Changelog
- 2026-05-27 — Implemented step 4 SQLite storage: schema creation, paper
  upsert/read/list, memory insert, query logging, local PDF ingestion, and
  deterministic storage tests. Ingested 10 local PDFs from
  `/data/fzhou/ar_runtime/papers` into
  `/data/fzhou/ar_runtime/test_runtime/auto_research.sqlite3`.
- 2026-05-27 — Added source-result ingestion and the librarian I/O enrichment
  stub for future PDF reading/abstraction.
- 2026-05-26 — Folder seeded during step 0.
