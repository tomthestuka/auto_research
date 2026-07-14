# `auto_research.storage` — SQLite persistence

## Purpose
Persists everything durable: paper metadata + abstracts + keywords, memory items
(librarian abstractions), and a query log. Lives in the runtime data dir
([CLAUDE.md §10](../../../CLAUDE.md#10-tech-stack--conventions)), not in the repo.

## Architecture
A small set of tables, accessed through an async-signature wrapper around the
stdlib `sqlite3` driver. Phase 1 operations are tiny and execute synchronously
inside those async methods; callers still use async APIs so later I/O hardening
does not change boundaries.

Schema definition is centralized in `database.py` so Phase 2 vector retrieval can
slot in without migrations: embedding space is reserved both as nullable
`embedding` columns and an `embeddings` table
([CLAUDE.md §12](../../../CLAUDE.md#12-deferred-beyond-phase-2)). The intended
deferred backend is `sqlite-vec`, which co-exists in the same DB file.

Runtime DB path:
- `<runtime_dir>/auto_research.sqlite3` (`runtime_dir` from `config/orchestration.yaml`)
- default when unset: `~/.auto_research/auto_research.sqlite3`

Local development ingestion logs are written under `logs/dev/step-4/`.

## Interfaces / Contracts
- Tables:
  - `papers(id, source, source_id, title, authors_json, year, abstract,
    keywords_json, local_path, sha256, inserted_at, embedding)`.
  - `memory(id, paper_id, summary, tags_json, mem_abbrev, created_at, embedding)`.
    `mem_abbrev` is the IO-team consensus-produced memory abbreviation written by
    `LibrarianIOAgent.ingest_results()`. For papers with a PDF, it is a rich
    summary produced by `IODebateToConverge`; for a no-PDF search result, the
    result is recorded as an **orphan memory** (`paper_id = NULL`, no paper row)
    whose `mem_abbrev` is the original `SearchResult.abstract`. Added via additive
    migration in `Storage._initialize()`.
  - `query_log(id, role, query, source, n_results, latency_ms, created_at)`.
  - `embeddings(id, owner_table, owner_id, embedding, created_at)` reserved for
    Phase 2.
- **A paper is identified by its PDF content hash:** `papers.sha256` is `NOT NULL`
  with a `UNIQUE(sha256)` constraint, so a paper row always has a downloaded PDF and
  the same PDF reached via two sources (e.g. arXiv + ADS) collapses to one row.
  Upserts dedup `ON CONFLICT(sha256)`: metadata + `local_path` are refreshed but the
  first ingester's `source`/`source_id` are preserved, and the original row id is kept.
- Local PDFs are ingested as `source="local_file"`, `source_id=<sha256>`, title
  from filename stem, `local_path`, and `sha256`.
- Downloaded source PDFs are ingested with source metadata when available. For
  example, arXiv downloads use `source="arxiv"` and `source_id=<arxiv_id>`.
- A search result with **no downloadable PDF never becomes a paper row** — it is
  kept only as an orphan `memory` note (see `LibrarianIOAgent.ingest_results`).
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
  [], mem_abbrev: str | None = None)` — memory input model. `mem_abbrev` is
  the IO-team consensus abbreviation written to the new `memory.mem_abbrev`
  column.
- `QueryLogRecord(role: str, query: str, source: str, n_results: int,
  latency_ms: float)` — query log input model.
- `runtime_data_dir() -> Path` — re-exported from `auto_research.env`; reads
  `config/orchestration.yaml::runtime_dir` (defaults to `~/.auto_research`).
- `database_path(data_dir: str | Path | None = None) -> Path` — returns the
  SQLite DB path for a runtime data dir.
- `check_database_health() -> None` — launch-time preflight check: ensures the runtime
  DB exists with its full schema, creating the file and tables if absent (idempotent
  `CREATE TABLE IF NOT EXISTS` via `Storage._initialize`), then verifies every expected
  table (`papers`, `memory`, `query_log`, `embeddings`) is present and raises otherwise.
  Registered in `auto_research.preflight` as `database_health`, so a fresh run no longer
  fails its first `text_search` on `no such table: papers`.
- `EXPECTED_TABLES: frozenset[str]` — the table names `check_database_health` requires.
- `sha256_file(path: str | Path) -> str` — returns a file digest.
- `paper_from_local_file(path: str | Path) -> PaperRecord` — builds minimal
  metadata for a local PDF or other file.
- `paper_from_search_result(result: SearchResult, path: str | Path) ->
  PaperRecord` — builds storage metadata from a source search result plus a
  downloaded PDF path; sets `sha256` from the file. (A no-PDF result has no
  paper factory — it becomes an orphan memory, not a paper.)
- `Storage(path: str | Path | None = None)` — SQLite wrapper. Methods:
  `initialize()`, `upsert_paper(paper)`, `get_paper(paper_id)`,
  `list_papers()`, `insert_memory(memory)`,
  `replace_memory_for_paper(memory)`, `log_query(query)`.
- `ingest_local_papers(storage: Storage, papers_dir: str | Path) ->
  list[StoredPaper]` — upserts all `*.pdf` files in a directory.
- `librarian_io_enrich_stub(storage: Storage, paper_id: int) -> None` —
  no-op stub; superseded by `LibrarianIOAgent.ingest_results()` which now
  handles PDF download, IO debate abstraction, and memory writes.
- `auto_research.storage.ingest_local.ingest_and_log(papers_dir: Path) -> dict`
  — developer utility that ingests PDFs and writes a `logs/dev/step-4/` JSON
  log.
- `python -m auto_research.storage.ingest_local <papers_dir>` — CLI form of the
  local paper ingestion utility.

## Changelog
- 2026-05-26 -> 2026-06-12 — SQLite storage built: papers, memory with `mem_abbrev`, query log,
  reserved embedding columns/table, paper ingestion/upserts, no-PDF metadata records, memory
  replacement, local PDF ingestion, config-driven runtime DB path.
- 2026-06-22 — Paper identity became the PDF content hash: `papers.sha256` is `NOT NULL` with
  `UNIQUE(sha256)` (was `UNIQUE(source, source_id)`) and `_upsert_paper` dedups
  `ON CONFLICT(sha256)`, so the same PDF via two sources collapses to one row; a no-PDF result is
  now recorded as an orphan `memory` note rather than a paper row (`paper_record_from_result`
  removed). Requires a fresh DB.
- 2026-06-23 — Added `check_database_health()` (+ `EXPECTED_TABLES`), registered in the launch
  preflight as `database_health`: creates the schema if absent and verifies tables, so a fresh
  run's first `text_search` no longer dies on `no such table: papers`.
