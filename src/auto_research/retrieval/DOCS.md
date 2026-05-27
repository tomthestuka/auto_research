# `auto_research.retrieval` — local-memory retrieval

## Purpose
Lets the librarian searcher sub-team query the local SQLite memory. Phase 1
provides exact/keyword text retrieval; vector retrieval is a documented stub for
Phase 2.

## Architecture
- `RetrievalBackend` interface with two methods. Phase 1 implements
  `text_search()` over the SQLite `papers`/`memory` tables;
  `vector_search()` raises `NotImplementedError` ([CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop)
  and [§13](../../../CLAUDE.md#13-phase-2-readiness)).

## Interfaces / Contracts
*To be populated in step 6.* Planned:
- `class RetrievalBackend(Protocol)`:
  - `async text_search(query, *, limit) -> list[MemoryHit]`
  - `async vector_search(query, *, limit) -> list[MemoryHit]` (stub)
- Concrete `SqliteRetrievalBackend` for Phase 1.

## Dependencies
- Internal: `auto_research.storage`, `auto_research.schemas`.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.
