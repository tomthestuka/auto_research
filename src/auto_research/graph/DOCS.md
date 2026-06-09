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
  monitor_jsonl=None) -> ResearchRun` — high-level monitored entry. Calls
  `check_required_env()` immediately (crashes if any required API key is absent).
  Missing dependencies are constructed from `config/models.yaml`. Reads
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
  `verdicts` is an accumulated list of reviewer decisions. Tool queues/results
  are not graph state; tool calls run inside analyst consensus and are recorded
  in per-worker `memory.md` files.
- `IOAgentLike` — protocol with `compose_search_strategy()`, `search()`, `ingest_results()`.
- `ReviewerLike` — protocol with `async review(request, *, literature, report)`.
- `build_graph(...)` — constructs and compiles the five-node state graph
  (`literature`, `review_literature`, `analysis`, `review_analysis`,
  `finalize_run`).
- `run_request(request, ...)` — compatibility entry point; delegates to
  `run_research()` and returns the final `ReviewVerdict`.
- `run_research(request, ...)` — monitored high-level entry point; returns
  `ResearchRun`. Reads `tool_retry_cap`, `toolkit_root`, `analyze_env`,
  `analyze_ini_sh`, and `mcp_servers` from `orchestration.yaml`, starts one
  persistent sandbox per analyst worker, and shuts down sandboxes/MCP clients in
  `finally`.
- `ResearchRun(request, verdict, final_state, monitor)` — result object with
  `.markdown()` for grouped monitor output.
- `_default_graph()` — synchronous; builds the compiled graph with all defaults from
  config. Called at import time by `serve.py` for LangGraph Studio.
- `load_orchestration_config(path?)` — loads `config/orchestration.yaml`; returns dict.
- `serve.py` — LangGraph Studio entry point; exposes `graph = _default_graph()` at
  module level so `langgraph dev` can import it. Referenced by `langgraph.json`.
- `DEFAULT_WORKER_COUNT` — default analyst worker count (currently 2).

## Changelog
- 2026-05-26/28 — Steps 10–12 built: `ResearchState`, `build_graph()` (five-node LangGraph with two capped feedback edges, `add_conditional_edges`), Studio wiring (`_default_graph()`, `serve.py`, `langgraph.json`). `run_research()` / `ResearchRun` / `run_request()`: per-run workspace creation, one `PersistentSandbox` per worker, `MCPToolClient` lifecycle, `RunMonitor` injection, teardown in `finally`. `literature_node` pipeline: compose strategy → online search → ingest → `text_search_paths()`. `_build_task()` injects reviewer follow-up, obs DB paths, and live tool catalogue into every analyst task. Tool state lives in worker `memory_path` files; `ResearchState` carries no tool queue/results.
- 2026-06-03 — `compose_report` node added to graph (runs after `finalize_run` on accepted verdicts, conditional on `report_formats`). `build_graph()` gains `run_id`, `storage`, `report_retry_cap`, `report_formats`. `run_research()` gains `report_formats` param; reads `report_retry_cap`/`report_formats` from `orchestration.yaml`; passes `run_id` and `storage` to graph. `ResearchRun` gains `research_report`, `report_jsonl`, `report_latex`, `report_pdf`. `ResearchState` gains the same four optional fields. `_normalize_report_formats()` validates and normalizes the harness handle.
- 2026-06-03 — `run_research()` and `_default_graph()` read `orchestration.yaml::disabled_tools` and pass it to `ToolkitRegistry`. `_builtin_server_config()` builds MCP server env from `_source_ini_env` (no `os.environ` fallback; `ValueError` if `analyze_ini_sh` missing); PYTHONPATH covers `toolkit_root` and `toolkit_root.parent`.
- 2026-06-09 — `run_research()` calls `check_required_env()` at entry so both CLI and library callers crash immediately on missing API keys.
