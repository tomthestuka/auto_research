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
- Text search uses lightweight normalized token matching across paper
  title/abstract/keywords/path/source id and memory summary/tags. One-character
  numeric tokens are ignored to avoid broad accidental matches.
- Live retrieval verification is opt-in:
  `AUTO_RESEARCH_LIVE_RETRIEVAL_TESTS=1 pytest tests/test_live_retrieval.py -s`.
  It queries the runtime SQLite DB for `GX339-4` and writes JSONL plus Markdown
  logs under `logs/dev/step-6/`.

## Interfaces / Contracts
- `RetrievalBackend` protocol:
  - `async text_search(query: str, *, limit: int = 10) -> list[MemoryHit]`
  - `async vector_search(query: str, *, limit: int = 10) -> list[MemoryHit]`
- `SqliteRetrievalBackend` implements `text_search()` and keeps
  `vector_search()` as a Phase 2 stub that raises `NotImplementedError`.
- Hits are ranked by descending integer score, then kind/id for deterministic
  ordering.

## Dependencies
- Internal: `auto_research.storage`, `auto_research.schemas`.

## Dictionary-level reference
- `HitKind` — literal hit kind: `"paper"` or `"memory"`.
- `MemoryHit(kind: HitKind, id: int, score: int, title: str, snippet: str | None
  = None, source: str | None = None, source_id: str | None = None,
  local_path: Path | None = None)` — one retrieval result.
- `RetrievalBackend` — protocol for local retrieval backends.
- `SqliteRetrievalBackend(storage: Storage | None = None)` — Phase 1 backend
  over the runtime SQLite DB. Methods: `text_search()` and `vector_search()`.

## Changelog
- 2026-05-27 — Implemented step 6: SQLite text retrieval over papers/memory,
  `vector_search()` Phase 2 stub, deterministic tests, and live `GX339-4`
  retrieval logging. Runtime query returned `GX339_4` from the local paper DB.
- 2026-05-26 — Folder seeded during step 0.
