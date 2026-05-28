# `auto_research.graph` — LangGraph orchestration

## Purpose
Assembles the agents into the process loop described in
[CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop) and
exposes a single async entry point that takes a `ResearchRequest` and returns a
`ReviewVerdict`.

## Architecture
A LangGraph `StateGraph` with seven nodes, two outer feedback edges (each
capped at `max_iterations`, default 3), and an inner tool error-feedback loop
(capped at `tool_retry_cap`, default 3):

```
START → literature → review_literature
                        │  needs_more_literature & lit < cap
                        ▼─────────────────────────────► literature   (loop ①)
                        │
                        ▼
                     analysis → tool_dispatch → tool_execution → review_analysis
                                                                  │  needs_rework & analysis < cap
                                                                  ▼─────────────► analysis  (loop ②)
                                                                  │
                                                                  ▼
                                                             finalize_run → END
```

Reviewer nodes return state updates containing the newest `ReviewVerdict`.
`add_conditional_edges()` then routes from `review_literature` back to
`literature` or onward to `analysis`, and from `review_analysis` back to
`analysis` or onward to `finalize_run`. `finalize_run` copies the latest
accumulated verdict into `state["verdict"]`, then exits via a static edge to
`END`.

**`tool_dispatch_node`:** after `analysis_node`, calls
`worker.request_tools(task)` on the first worker. For each `ToolCallRequest`:
- Known in `ToolkitRegistry` → adds to `state["tool_queue"]`
- Unknown → calls `propose_and_pr()` (Phase 2 stub), catches
  `NotImplementedError`, adds a `ToolResult(exit_code=127)` to
  `state["tool_results"]`

**`tool_execution_node`:** for each request in `tool_queue`, runs
`runner.run_tool()` in an isolated subprocess. On failure, calls
`worker.request_tools(error_task)` to get revised arguments (LLM
self-correction), up to `tool_retry_cap` attempts. Appends all `ToolResult`
objects to `state["tool_results"]`. Clears `tool_queue` on completion.

On subsequent analysis iterations (rework), `analysis_node` includes
accumulated `tool_results` in the task instructions so workers can reason
from actual tool output.

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
analyst report, the final reviewer verdict, accumulated reviewer verdicts, two
iteration counters, and the cap.

## Interfaces / Contracts
- `build_graph(*, reviewer, io_agent, retrieval=None, workers, lead,
  consensus=None, max_iterations=3, registry=None, tool_retry_cap=3,
  toolkit_root=None, monitor=None, analyze_env=None) -> CompiledStateGraph` — pure constructor. `retrieval`
  defaults to `SqliteRetrievalBackend()`. `consensus` defaults to
  `DebateToConverge(max_rounds=max_iterations, monitor=monitor)`. `registry` defaults to
  `ToolkitRegistry(toolkit_root or Path("/data/fzhou/research_toolkit"))`.
  `analyze_env` (Path or None) is forwarded to `run_tool()` to select the
  analysis Python interpreter (e.g. the conda env with XSPEC/HEASoft).
  Raises `ValueError` if `max_iterations < 1`. Reads no environment at import.
- `async run_request(request, *, reviewer=None, io_agent=None, retrieval=None,
  workers=None, lead=None, consensus=None, max_iterations=None, chatter=10,
  monitor=None, monitor_jsonl=None) -> ReviewVerdict` — compatibility entry.
  Runs `run_research()` and returns only the final reviewer verdict.
- `async run_research(request, *, ..., chatter=10, monitor=None,
  monitor_jsonl=None) -> ResearchRun` — high-level monitored entry. Missing
  dependencies are constructed from `config/models.yaml`. Reads
  `tool_retry_cap` and `toolkit_root` from `config/orchestration.yaml`. Returns
  the request, final verdict, final state, and `RunMonitor`.
- `ResearchState` — `TypedDict` with `request`, `literature: list[Path]`,
  `report`, `verdict`, `verdicts: Annotated[list[ReviewVerdict], operator.add]`,
  `lit_iterations`, `analysis_iterations`, `max_iterations`, `tool_results:
  Annotated[list[ToolResult], operator.add]` (accumulated), and `tool_queue:
  list[ToolCallRequest]` (transient, set by dispatch, cleared by execution).
- Monitor events are optional. When a monitor is supplied, graph nodes record
  node starts, search strategy/results, local retrieval paths, analyst reports,
  tool dispatch/execution attempts and results, and reviewer verdicts.
- `IOAgentLike` / `ReviewerLike` / `ToolWorkerLike` — minimal protocols for
  dependency injection. `ToolWorkerLike` extends `WorkerLike` with
  `async request_tools(task) -> list[ToolCallRequest]`.
- Both outer feedback edges are gated by the reviewer's structured verdict flags
  (`needs_more_literature`, `needs_analysis_rework`). The reviewer's `follow_up`
  text is fed back into the next `compose_search_strategy()` call and/or analysis
  instructions. The routing is implemented with LangGraph conditional edges
  after each reviewer node has appended its verdict to `state["verdicts"]`.
- Live graph verification is opt-in:
  `AUTO_RESEARCH_LIVE_GRAPH_TESTS=1 pytest tests/test_live_graph.py -s`. Writes
  JSONL plus Markdown logs under `logs/dev/step-10/`.
- Live sandbox verification is opt-in:
  `AUTO_RESEARCH_LIVE_SANDBOX_TESTS=1 pytest tests/test_live_sandbox.py -s`.
  Writes logs under `logs/dev/step-11/`.
- LangGraph Studio: run `langgraph dev` from the repo root (requires
  `langgraph-cli[inmem]`). Server binds to `localhost:2024` on the remote
  machine. Over SSH, forward the port locally:
  `ssh -L 2024:localhost:2024 your_server`, then connect Studio from your
  laptop at `https://smith.langchain.com/studio/?baseUrl=http://localhost:2024`.
  Alternatively run `langgraph dev --host 0.0.0.0` and use the server's IP.
  Entry point: `src/auto_research/graph/serve.py:graph`.

## Dependencies
- External: `langgraph`.
- Internal: `auto_research.agents`, `auto_research.consensus`,
  `auto_research.models`, `auto_research.retrieval`, `auto_research.schemas`.

## Dictionary-level reference
- `ResearchState` — TypedDict for the graph state; `literature` is `list[Path]`;
  `verdicts` and `tool_results` are accumulated lists; `tool_queue` is
  `list[ToolCallRequest]` (transient).
- `IOAgentLike` — protocol with `compose_search_strategy()`, `search()`, `ingest_results()`.
- `ReviewerLike` — protocol with `async review(request, *, literature, report)`.
- `ToolWorkerLike` — protocol extending `WorkerLike` with `async request_tools(task)`.
- `build_graph(...)` — constructs and compiles the state graph (seven nodes).
- `run_request(request, ...)` — high-level entry point; reads `tool_retry_cap` and
  `toolkit_root` from `orchestration.yaml`. Returns the final `ReviewVerdict`.
- `run_research(request, ...)` — monitored high-level entry point; returns
  `ResearchRun`.
- `ResearchRun(request, verdict, final_state, monitor)` — result object with
  `.markdown()` for grouped monitor output.
- `_default_graph()` — synchronous; builds the compiled graph with all defaults from
  config. Called at import time by `serve.py` for LangGraph Studio.
- `load_orchestration_config(path?)` — loads `config/orchestration.yaml`; returns dict.
- `serve.py` — LangGraph Studio entry point; exposes `graph = _default_graph()` at
  module level so `langgraph dev` can import it. Referenced by `langgraph.json`.
- `DEFAULT_WORKER_COUNT` — default analyst worker count (currently 2).

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
- 2026-05-28 — Step 11: added `tool_dispatch_node` and `tool_execution_node`.
  `ResearchState` gains `tool_results` (accumulated via `operator.add`) and
  `tool_queue` (transient). `build_graph()` gains `registry`, `tool_retry_cap`,
  `toolkit_root` parameters. `run_request()` reads these from `orchestration.yaml`.
  `ToolWorkerLike` protocol added. `_build_task()` helper formats tool results into
  task instructions for rework iterations.
- 2026-05-28 — LangGraph Studio wiring: added `_default_graph()` to `runtime.py`
  (synchronous default-config graph constructor) and `serve.py` (module-level `graph`
  variable for `langgraph dev`). Added `langgraph.json` at repo root and
  `langgraph-cli[inmem]` to `environment.yml` pip block.
- 2026-05-28 — Step 12: added monitored `run_research()` / `ResearchRun`,
  optional `RunMonitor` instrumentation, accumulated `verdicts`, and
  reviewer-node `Command(goto=...)` routing to avoid the installed LangGraph
  async conditional-edge hang while preserving the two capped feedback edges.
- 2026-05-28 — Restored reviewer routing to ordinary `add_conditional_edges()`
  after terminal verification showed the async conditional-edge issue was not
  reproducible in the user's environment. `review_literature` and
  `review_analysis` now append verdicts only; branch functions perform the
  capped loop routing.
- 2026-05-28 — Fixed analyst task construction: `_build_task` now always injects
  `prior.follow_up` as instructions (labelled "Reviewer dispatch" on first pass,
  "Reviewer feedback for revision" on rework), loads `observations.yaml` once at
  `build_graph` time and injects obs DB paths into every analyst task, and passes
  the live tool catalogue to both `analysis_node` and `tool_dispatch_node` so
  workers see available tools during debate and at dispatch.
- 2026-05-28 — Added `analyze_env` and `analyze_ini_sh` parameters to
  `build_graph()`. Both are forwarded to `run_tool()` in `tool_execution_node`
  so sandbox tools run in the configured analysis Python environment with the
  full HEASoft/CALDB environment sourced from the init script. Read from
  `config/orchestration.yaml` in both `_default_graph()` and `run_research()`.
