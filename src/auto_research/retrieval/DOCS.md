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
  Each result item is **one paper, keyed by paper id**: its memory abstraction(s)
  (joined via `memory.paper_id`) and its downloaded PDF (`local_path`) are
  attributes of that single item, never separate competing hits. A memory row
  with no `paper_id` (an orphan note) is its own standalone hit. Text search uses
  lightweight normalized token matching across the paper's
  title/abstract/keywords/path/source id **and its joined memory
  summary/tags/mem_abbrev**, so a paper matched only via its abstraction still
  scores and still surfaces its PDF. One-character numeric tokens are ignored to
  avoid broad accidental matches. `require_local_path` drops items without a PDF
  *before* the `limit` is applied.

`text_search_paths()` is now reached through the **reviewer's `text_search` tool**
(`ReadOnlyToolProvider`), not the graph node: the reviewer searches the knowledge base
and opens chosen PDFs (`open_paper`) inside its ReAct loop. The `literature_node` only
ingests.

Live retrieval verification is opt-in:
`AUTO_RESEARCH_LIVE_RETRIEVAL_TESTS=1 pytest tests/test_live_retrieval.py -s`.

## Interfaces / Contracts
- `text_search_paths(query, *, limit=10, backend=None) -> list[Path]`
  — pure Python, no LLM. Returns up to `limit` local PDF paths for
  keyword-matching papers, top-scored first. Each paper is one item, so a PDF is
  never returned twice; only papers with a non-null `local_path` are included.
- `vector_search_paths(query, *, limit=10, backend=None) -> list[Path]`
  — Phase 2 stub; raises `NotImplementedError`.
- `RetrievalBackend` protocol:
  - `async text_search(query: str, *, limit: int = 10, require_local_path: bool = False) -> list[MemoryHit]`
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
- 2026-06-22 — Unified retrieval items by paper: a paper and its memory
  abstraction are now **one hit keyed by paper id**, with the PDF and the joined
  memory text as attributes (previously a paper surfaced twice — once as a
  `paper` hit, once as a `memory` hit with `local_path=None` — so high-scoring
  abstractions crowded the paper rows past the `limit` and `text_search_paths`
  returned 0). Memory text is now folded into the paper's search haystack, and
  `text_search(..., require_local_path=True)` filters to PDF-backed papers before
  the `limit`. Orphan memories (no `paper_id`) remain standalone hits. Added two
  regression tests.
- 2026-06-22 — Storage now keys papers by `UNIQUE(sha256)` (see `storage/DOCS.md`), so the
  same PDF reached via two sources is a single paper row — `text_search_paths` can no longer
  return the same paper as two different file paths. No retrieval code change; the per-paper
  hit model already returns one item per row.
