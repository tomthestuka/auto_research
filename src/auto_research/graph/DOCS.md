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

The dispatch step is deterministic in Phase 1: the literature node forms a
`SearchRequest` from the request's question (and appends the reviewer's prior
`follow_up` on retries), and the analysis node builds an `AnalysisTask` from
the question and current literature (carrying the reviewer's `follow_up` into
the task instructions on rework). This keeps step 10 focused on orchestration;
a more elaborate reviewer-driven dispatch is a future extension.

`ResearchState` carries: the request, the literature set, the latest analyst
report, the latest reviewer verdict, two iteration counters, and the cap.

## Interfaces / Contracts
- `build_graph(*, reviewer, librarian, workers, lead, consensus=None,
  max_iterations=3) -> CompiledStateGraph` — pure constructor. Defaults
  `consensus` to `DebateToConverge(max_rounds=max_iterations)`. Raises
  `ValueError` if `max_iterations < 1`. Reads no environment at import.
- `async run_request(request, *, reviewer=None, librarian=None,
  workers=None, lead=None, consensus=None, max_iterations=3) -> ReviewVerdict`
  — high-level entry. Missing dependencies are constructed from
  `config/models.yaml`: one `ReviewerAgent`, one `LibrarianIOAgent`,
  `DEFAULT_WORKER_COUNT` `AnalystWorkerAgent`s, one `AnalystLeadAgent`. Returns
  the final reviewer verdict.
- `ResearchState` — `TypedDict` with `request`, `literature`, `report`,
  `verdict`, `lit_iterations`, `analysis_iterations`, `max_iterations`. Nodes
  return partial-state dicts; LangGraph replaces values on update.
- `LibrarianLike` / `ReviewerLike` — minimal protocols used by the graph so
  callers can inject fakes in tests and alternative librarians at runtime.
- Both feedback edges are gated by the reviewer's structured verdict flags
  (`needs_more_literature`, `needs_analysis_rework`). The reviewer's
  `follow_up` text is fed back into the next literature query / analysis
  instructions, so loops carry information rather than just retry.
- Live graph verification is opt-in:
  `AUTO_RESEARCH_LIVE_GRAPH_TESTS=1 pytest tests/test_live_graph.py -s`. It
  drives the compiled graph against a thin arxiv-source librarian and real
  reviewer / worker / lead model calls, and writes JSONL plus Markdown logs
  under `logs/dev/step-10/`. The thin librarian sidesteps
  `LibrarianIOAgent`'s LLM-ranking pass, whose end-to-end robustness is a
  step-5 / step-8 concern rather than a step-10 deliverable.

## Dependencies
- External: `langgraph`.
- Internal: `auto_research.agents`, `auto_research.consensus`,
  `auto_research.models`, `auto_research.schemas`.

## Dictionary-level reference
- `ResearchState` — TypedDict for the graph state.
- `LibrarianLike` — protocol with `async search(SearchRequest) ->
  list[SearchResult]`.
- `ReviewerLike` — protocol with `async review(request, *, literature, report)
  -> ReviewVerdict`.
- `build_graph(...)` — constructs and compiles the state graph.
- `run_request(request, ...)` — high-level entry point. Returns the final
  `ReviewVerdict` after the graph runs.
- `DEFAULT_WORKER_COUNT` — default number of analyst workers when
  `run_request` constructs them from the registry (currently 2).

## Changelog
- 2026-05-27 — Step 10 completed: added `ResearchState`, `build_graph`,
  `run_request`, and the LangGraph state machine wiring the two capped
  feedback edges. Deterministic tests cover single-pass, literature-loop cap,
  analysis-loop cap, custom cap, and constructor validation. Opt-in live test
  drives the graph with real reviewer / worker / lead model calls against a
  thin arxiv-source librarian.
- 2026-05-26 — Folder seeded during step 0.
