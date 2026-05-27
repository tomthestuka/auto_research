# `auto_research.graph` — LangGraph orchestration

## Purpose
Assembles the agents into the process loop described in
[CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop) and
exposes a single async entry point that takes a `ResearchRequest` and returns a
`ReviewVerdict`.

## Architecture
A LangGraph `StateGraph` with four nodes and two real feedback edges, **each
capped at `max_iterations` (default 3)**:

```
START → literature → review_literature
                        │  needs_more_literature & lit < cap
                        ▼─────────────────────────────► literature   (loop ①)
                        │
                        ▼
                     analysis → review_analysis
                                  │  needs_analysis_rework & analysis < cap
                                  ▼──────────────────────► analysis  (loop ②)
                                  │
                                  ▼
                                 END
```

The literature node follows a four-step pipeline:
1. **IO agent composes search strategy** — `io_agent.compose_search_strategy()`
   translates the research intent (and optional reviewer follow-up) into a
   focused `SearchRequest`.
2. **IO agent queries online sources** — `io_agent.search()` hits external
   APIs (arXiv, NASA ADS, web). No LLM ranking; raw results only.
3. **IO agent downloads and abstracts** — `io_agent.ingest_results()` downloads
   PDFs, runs `IODebateToConverge`, and writes `mem_abbrev` to SQLite.
4. **`text_search_paths()` queries local SQLite** — pure Python, no LLM.
   Returns `list[Path]` for papers with downloaded PDFs. The reviewer receives
   these paths and loads each as a full PDF `DocumentRef` attachment.

The analysis node builds an `AnalysisTask` from the question and optional
instructions (carrying the reviewer's `follow_up` on rework).

`ResearchState` carries: the request, a `list[Path]` literature set, the latest
analyst report, the latest reviewer verdict, two iteration counters, and the cap.

## Interfaces / Contracts
- `build_graph(*, reviewer, io_agent, retrieval=None, workers, lead,
  consensus=None, max_iterations=3) -> CompiledStateGraph` — pure constructor.
  `retrieval` accepts any `RetrievalBackend`; defaults to
  `SqliteRetrievalBackend()`. Defaults `consensus` to
  `DebateToConverge(max_rounds=max_iterations)`. Raises `ValueError` if
  `max_iterations < 1`. Reads no environment at import.
- `async run_request(request, *, reviewer=None, io_agent=None, retrieval=None,
  workers=None, lead=None, consensus=None, max_iterations=None) -> ReviewVerdict`
  — high-level entry. Missing dependencies are constructed from
  `config/models.yaml`: one `ReviewerAgent`, one `LibrarianIOAgent(storage=storage)`,
  one `SqliteRetrievalBackend(storage)`, `DEFAULT_WORKER_COUNT`
  `AnalystWorkerAgent`s, one `AnalystLeadAgent`. Returns the final reviewer verdict.
- `ResearchState` — `TypedDict` with `request`, `literature: list[Path]`,
  `report`, `verdict`, `lit_iterations`, `analysis_iterations`,
  `max_iterations`. Nodes return partial-state dicts; LangGraph replaces values
  on update.
- `IOAgentLike` / `ReviewerLike` — minimal protocols for dependency injection.
  `IOAgentLike` requires `compose_search_strategy()`, `search()`, and
  `ingest_results()`. `ReviewerLike` requires `review(request, *, literature:
  Sequence[Path], report)`.
- Both feedback edges are gated by the reviewer's structured verdict flags
  (`needs_more_literature`, `needs_analysis_rework`). The reviewer's
  `follow_up` text is fed back into the next `compose_search_strategy()` call
  and/or analysis instructions.
- Live graph verification is opt-in:
  `AUTO_RESEARCH_LIVE_GRAPH_TESTS=1 pytest tests/test_live_graph.py -s`. It
  drives the compiled graph with `LoggingIOAgent` wrapping `LibrarianIOAgent`
  and a `SqliteRetrievalBackend`, and writes JSONL plus Markdown logs under
  `logs/dev/step-10/`.

## Dependencies
- External: `langgraph`.
- Internal: `auto_research.agents`, `auto_research.consensus`,
  `auto_research.models`, `auto_research.retrieval`, `auto_research.schemas`.

## Dictionary-level reference
- `ResearchState` — TypedDict for the graph state; `literature` is now
  `list[Path]` (local PDF paths from `text_search_paths()`).
- `IOAgentLike` — protocol with `compose_search_strategy()`, `search()`, and
  `ingest_results()`.
- `ReviewerLike` — protocol with `async review(request, *, literature:
  Sequence[Path], report) -> ReviewVerdict`.
- `build_graph(...)` — constructs and compiles the state graph.
- `run_request(request, ...)` — high-level entry point. Returns the final
  `ReviewVerdict` after the graph runs.
- `DEFAULT_WORKER_COUNT` — default number of analyst workers when
  `run_request` constructs them from the registry (currently 2).

## Changelog
- 2026-05-27 — Replaced `SearcherLike` / `LibrarianSearcherAgent` with
  `text_search_paths()` (pure Python, no LLM). `literature_node` step 4 now
  calls `text_search_paths(query, backend=retrieval)` → `list[Path]`.
  `ResearchState.literature` changed from `list[SearchResult]` to `list[Path]`.
  `ReviewerLike.review()` `literature` parameter is now `Sequence[Path]`.
  `build_graph()` takes `retrieval: RetrievalBackend` instead of
  `searcher: SearcherLike`. `run_request()` constructs `SqliteRetrievalBackend`
  as the default. `SearcherLike` protocol removed.
- 2026-05-27 — Architectural correction: `build_graph()` takes `io_agent`
  and `searcher` instead of `librarian`. `literature_node` follows a four-step
  pipeline: compose strategy → online search → ingest (download + abstract +
  store) → local SQLite query.
- 2026-05-27 — Step 10 completed: added `ResearchState`, `build_graph`,
  `run_request`, and the LangGraph state machine wiring the two capped
  feedback edges. Deterministic tests cover single-pass, literature-loop cap,
  analysis-loop cap, custom cap, and constructor validation.
- 2026-05-26 — Folder seeded during step 0.
