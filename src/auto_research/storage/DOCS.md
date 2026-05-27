# `auto_research.storage` — SQLite persistence

## Purpose
Persists everything durable: paper metadata + abstracts + keywords, memory items
(librarian abstractions), and a query log. Lives in the runtime data dir
([CLAUDE.md §9](../../../CLAUDE.md#9-tech-stack--conventions)), not in the repo.

## Architecture
A small set of tables, accessed through a thin async wrapper around the stdlib
`sqlite3` driver. Schema definition is centralized so Phase 2 vector retrieval can
slot in without migrations: **reserve an embedding column/table now**
([CLAUDE.md §13](../../../CLAUDE.md#13-phase-2-readiness)) — intended backend is
`sqlite-vec`, which co-exists in the same DB file.

## Interfaces / Contracts
*To be populated in step 4.* Planned tables:
- `papers(id, source, source_id, title, authors, year, abstract, keywords_json,
  inserted_at, embedding BLOB NULL)` — `embedding` reserved, unused in Phase 1.
- `memory(id, paper_id?, summary, tags_json, created_at, embedding BLOB NULL)`.
- `query_log(id, role, query, source, n_results, latency_ms, created_at)`.

Public API: insert/upsert helpers, plus the read primitives consumed by
`auto_research.retrieval`.

## Dependencies
- External: stdlib `sqlite3` (Phase 2 will add `sqlite-vec`).
- Imported by: `auto_research.retrieval`, librarian agents via
  `auto_research.sources`.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.