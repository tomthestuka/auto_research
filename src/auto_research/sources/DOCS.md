# `auto_research.sources` — external literature sources

## Purpose
Provides the librarian I/O sub-team with read access to external literature and
the web, behind a single shared interface so new sources drop in cleanly
([CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop)).

## Architecture
- `base.py` — the `QuerySource` Protocol/ABC.
- `arxiv_source.py` — backed by the `arxiv` Python library.
- `nasa_ads_source.py` — backed by the `ads` client; token from `ADS_DEV_KEY`.
- `web_source.py` — generic web search via Tavily; also reused by analysts for
  reading authoritative documentation.

Each module is independent; no source imports another.

## Interfaces / Contracts
*To be populated in step 5.* Planned:
- `class QuerySource(Protocol)` with `async search(query, *, limit) -> list[SearchResult]`
  (`SearchResult` defined in `auto_research.schemas`).
- One concrete class per source above, each constructible without arguments
  (config + env are read internally).

## Dependencies
- External: `arxiv`, `ads`, `tavily-python`.
- Internal: `auto_research.schemas`, `auto_research.storage` (for the query log).

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.