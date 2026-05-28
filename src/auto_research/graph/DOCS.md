# `auto_research.graph` — LangGraph orchestration

## Purpose
Assembles the agents into the process loop described in
[CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop) and
exposes a single async entry point that takes a `ResearchRequest` and returns a
`ReviewVerdict`.

## Architecture
A LangGraph `StateGraph` with five nodes and two outer feedback edges (each
capped at `max_iterations`, default 3).  Tool execution is handled inside
`DebateToConverge` via persistent per-worker sandboxes — not via graph nodes.

```
START → literature → review_literature
                        │  needs_more_literature & lit < cap
                        ▼─────────────────────────────► literature   (loop ①)
                        │
                        ▼
                     analysis → review_analysis
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

**Tool execution inside `analysis_node`:** `analysis_node` calls
`consensus_strategy.run(workers, lead, task)`. `DebateToConverge.run()` now
includes a tool-execution phase after each round of `analyze()` calls.  Each
worker with an attached `PersistentSandbox` calls `execute_tools()`, which
calls `request_tools()`, executes via the sandbox, and appends results to the
worker's private `memory_path`.  A truncated shared summary is injected into
`task.instructions` for the next round.  Error-feedback retry is inside
`AnalystWorkerAgent.execute_tools()`, capped at `tool_retry_cap`.

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
  consensus=None, max_iterations=3, registry=None, monitor=None) -> CompiledStateGraph` —
  pure constructor.  `retrieval` defaults to `SqliteRetrievalBackend()`.
  `consensus` defaults to `DebateToConverge(max_rounds=max_iterations, monitor=monitor, registry=registry)`.
  `registry` defaults to `ToolkitRegistry(Path("/data/fzhou/research_toolkit"))` and is
  used for tool catalogue injection into task instructions.
  Raises `ValueError` if `max_iterations < 1`. Reads no environment at import.
  `analyze_env`, `analyze_ini_sh`, `tool_retry_cap`, and `toolkit_root` are no longer
  `build_graph()` parameters — they are consumed when constructing `PersistentSandbox`
  and `DebateToConverge` in `runtime.py`.
- `async run_request(request, *, reviewer=None, io_agent=None, retrieval=None,
  workers=None, lead=None, consensus=None, max_iterations=None, chatter=10,
  monitor=None, monitor_jsonl=None) -> ReviewVerdict` — compatibility entry.
  Runs `run_research()` and returns only the final reviewer verdict.
- `async run_research(request, *, ..., chatter=10, monitor=None,
  monitor_jsonl=None) -> ResearchRun` — high-level monitored entry. Missing
  dependencies are constructed from `config/models.yaml`. Reads
  `tool_retry_cap`, `toolkit_root`, `analyze_env`, and `analyze_ini_sh` from
  `config/orchestration.yaml`.  Creates per-run workspace
  (`$AUTO_RESEARCH_RUNTIME_DIR/runs/{run_id}/`), spawns one `PersistentSandbox`
  per `AnalystWorkerAgent`, assigns each worker's `sandbox` and `memory_path`,
  then tears all sandboxes down after the graph completes or raises.
  Returns the request, final verdict, final state, and `RunMonitor`.
- `ResearchState` — `TypedDict` with `request`, `literature: list[Path]`,
  `report`, `verdict`, `verdicts: Annotated[list[ReviewVerdict], operator.add]`,
  `lit_iterations`, `analysis_iterations`, `max_iterations`.
  `tool_queue` and `tool_results` removed — tool state is now private to each
  worker's `memory_path` file and embedded in the final `AnalystReport`.
- Monitor events are optional. When a monitor is supplied, graph nodes record
  node starts, search strategy/results, local retrieval paths, analyst reports,
  tool execution attempts and results (via consensus), and reviewer verdicts.
- `IOAgentLike` / `ReviewerLike` — minimal protocols for dependency injection.
  `ToolWorkerLike` protocol removed from `builder.py` (tool execution moved to
  consensus/roles layers).
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
- 2026-05-28 — Replaced one-shot dispatch/execution graph nodes with persistent
  per-worker sandbox architecture.  `tool_dispatch_node` and `tool_execution_node`
  removed from the graph.  `ResearchState` no longer carries `tool_queue` or
  `tool_results`; tool state lives in worker `memory_path` files.  `build_graph()`
  signature simplified: `analyze_env`, `analyze_ini_sh`, `tool_retry_cap`, and
  `toolkit_root` removed (now consumed by `PersistentSandbox` and `DebateToConverge`
  in `runtime.py`).  `run_research()` creates the per-run workspace, spawns one
  `PersistentSandbox` per worker, assigns `sandbox` and `memory_path` to each
  `AnalystWorkerAgent`, and tears sandboxes down in a `finally` block.
