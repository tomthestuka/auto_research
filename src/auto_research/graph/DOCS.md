# `auto_research.graph` — LangGraph orchestration

## Purpose
Assembles the agents into the process loop described in the Phase 1 system tour
([CLAUDE.md §8](../../../CLAUDE.md#8-what-phase-1-built-the-system-as-it-stands)) and
exposes async entry points for both monitored runs (`ResearchRun`) and verdict-only
compatibility (`ReviewVerdict`).

## Architecture
A LangGraph `StateGraph` with six nodes and two outer feedback edges (each
capped at `max_iterations`, default 3). Tool execution is owned by analyst
workers inside their `analyze()` ReAct loop via persistent per-worker sandboxes —
not by graph nodes.

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
                             finalize_run
                                  │ accepted + report formats
                                  ▼
                             compose_report → END
```

Reviewer nodes return state updates containing the newest `ReviewVerdict`.
`add_conditional_edges()` then routes from `review_literature` back to
`literature` or onward to `analysis`, and from `review_analysis` back to
`analysis` or onward to `finalize_run`. `finalize_run` copies the latest
accumulated verdict into `state["verdict"]`, then either routes to `compose_report`
on an accepted verdict with requested report formats or exits to `END`.

**Honest-failure routing (§9.6).** `after_analysis` inspects the adjudicated
`AnalystReport.status`: an `ok` report goes to `review_analysis`; a `failed`/`needs_human`
report routes **straight to `finalize_run`**, bypassing reviewer rework (an honest failure is
a *correct* outcome with no penalty). `finalize_run` then emits a non-accepted `ReviewVerdict`
for those, so `route_after_finalize` exits to `END` — **no report is composed**, never a
fabricated one.

**Tool execution inside `analysis_node`:** `analysis_node` calls
`consensus_strategy.run(workers, lead, task)`. Each worker's
`AnalystWorkerAgent.analyze()` runs the inner ReAct loop over `SandboxToolProvider`
when sandbox + registry are attached, writes real `ToolResult`s to its private
`memory_path`, and exposes `last_tool_results`. `DebateToConverge` injects a
truncated cross-worker summary into `task.instructions` for the next round.

The literature node is **ingest-only** (three steps); retrieval-for-review is
reviewer-driven (see below):
1. **IO agent composes search strategy** — `io_agent.compose_search_strategy()`
   translates the research intent (and optional reviewer follow-up) into a
   focused `SearchRequest`.
2. **IO agent queries online sources** — `io_agent.search()` hits external
   APIs (arXiv, NASA ADS, web). No LLM ranking; raw results only.
3. **IO agent downloads and abstracts** — `io_agent.ingest_results()` downloads
   PDFs, runs `IODebateToConverge`, and writes `mem_abbrev` to SQLite (no-PDF
   results land as orphan memory notes). The node no longer pre-loads papers for
   the reviewer — **the reviewer pulls literature itself** via its `text_search`
   (find candidate paths) and `open_paper` (read a chosen PDF) ReAct tools.

The analysis node builds an `AnalysisTask` from the question and optional
instructions (carrying the reviewer's `follow_up` on rework). After the final
analysis review, `review_analysis_node` records `cited_paths` =
`verdict.cited_paths ∩ reviewer.tools.opened_paths` (via `_resolve_cited_paths`),
so a citation can only name a paper the reviewer actually opened; `compose_report`
builds References from those paths.

`ResearchState` carries: the request, the `cited_paths: list[Path]` the reviewer
cited, the latest analyst report, the final reviewer verdict, accumulated reviewer
verdicts, two iteration counters, and the cap.

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
- `async run_research(request, *, ..., chatter=10, monitor=None, monitor_jsonl=None,
  report_formats=None, human_handler=None, human_channel=None) -> ResearchRun` — high-level
  monitored entry. Resolves the raise-to-human channel (`hitl.resolve_handler` — explicit
  `human_handler` → `human_channel` → auto-detect TTY/library) then `await run_preflight(
  human_handler=…)` immediately (the launch-time checklist: `.env` + required keys, the
  author↔reviewer decorrelation, and — when a channel is resolved — a **channel ping** that
  must reply), so a misconfigured/unreachable-human run crashes before any LLM is constructed.
  The resolved handler is threaded to the analyst workers and the reviewer/lead tool providers
  so any role can `ask_human`.
  Missing dependencies are constructed from `config/models.yaml`. Reads
  `tool_retry_cap`, `toolkit_root`, `analyze_env`, and `analyze_ini_sh` from
  `config/orchestration.yaml`.  Creates per-run workspace
  (`<runtime_dir>/runs/{run_id}/`; `runtime_dir` from `config/orchestration.yaml`), spawns one `PersistentSandbox`
  per `AnalystWorkerAgent`, assigns each worker's `sandbox` and `memory_path`,
  then tears all sandboxes down after the graph completes or raises.
  Returns the request, final verdict, final state, and `RunMonitor`.
- `ResearchState` — `TypedDict` with `request`, `cited_paths: list[Path]`,
  `report`, `verdict`, `verdicts: Annotated[list[ReviewVerdict], operator.add]`,
  `lit_iterations`, `analysis_iterations`, `max_iterations`.
  `tool_queue` and `tool_results` removed — tool state is now private to each
  worker's `memory_path` file and embedded in the final `AnalystReport`.
- Monitor events are optional. When a monitor is supplied, graph nodes record
  node starts, search strategy/results, local retrieval paths, analyst reports,
  worker tool-result summaries (via consensus/agents), reviewer verdicts, and report
  composition events.
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
- `ResearchState` — TypedDict for the graph state; `cited_paths` is `list[Path]`
  (the papers the reviewer cited); `verdicts` is an accumulated list of reviewer
  decisions. Tool queues/results are not graph state; tool calls run inside analyst
  workers and are recorded in per-worker `memory.md` files.
- `IOAgentLike` — protocol with `compose_search_strategy()`, `search()`, `ingest_results()`.
- `ReviewerLike` — protocol with `async review(request, *, report)`.
- `_resolve_cited_paths(verdict, reviewer) -> list[Path]` — resolves a verdict's
  `cited_paths` to `Path`s, keeping only papers the reviewer actually opened this run
  (no enforcement when the reviewer has no KB-tool provider, e.g. a test double).
- `build_graph(...)` — constructs and compiles the six-node state graph
  (`literature`, `review_literature`, `analysis`, `review_analysis`,
  `finalize_run`, `compose_report`).
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
- 2026-05-26 -> 2026-06-12 — Graph/runtime built and ReAct-wired: six-node LangGraph
  including `compose_report`, two capped feedback edges, Studio wiring, per-run workspace,
  one persistent sandbox per worker, MCP client lifecycle, read-only reviewer/lead tools,
  required-env checks, role-specific ReAct caps, report formats, and ReAct literature
  gathering.
- 2026-06-12 — Phase 2 step 0 added off-repo MCP server cwd pinning and lead read-only
  `toolkit_root` access; compacted changelog, refreshed the graph diagram/interfaces, and
  reflected worker-owned tool execution.
- 2026-06-15 -> 2026-06-18 — Phase 2 steps 5 and 7. `run_research` resolves a raise-to-human
  channel (`human_handler`/`human_channel`), awaits the now-async preflight (incl. the channel
  ping), and threads the handler to the role tool providers; `after_analysis` routes an honest
  `failed`/`needs_human` report around the reviewer to `finalize_run` (no fabricated report).
  `runtime.py` wires the consensus authoring/resume path — `DebateToConverge` receives
  `tool_author_cap`, `toolkit_root`, `worktree_base` (`<run_dir>/authoring`), `direct_branch`
  (`orchestration.yaml: tool_author_branch`, default `ai`), and `human_handler`, so the lead
  authors missing capability into `research_toolkit` and workers rerun on it. The reviewer's
  read-only `roots` now also include `toolkit_root` (shared `readonly_roots = [run_dir,
  papers_dir, toolkit_root]`), so it can read tool source/`TOOL_MANIFEST` and verify analyst
  results used correct call parameters.
- 2026-06-22 — **Retrieval-for-review is reviewer-driven.** The `literature` node no longer calls
  `text_search_paths()` or pre-loads papers into state — it only ingests; the reviewer pulls
  literature itself via its `text_search`/`open_paper` tools. `ResearchState.literature` was
  replaced by `cited_paths`; `review_analysis_node` sets it to `verdict.cited_paths ∩
  reviewer.tools.opened_paths` (`_resolve_cited_paths`), and `compose_report` builds References
  from those. `reviewer.review(...)` dropped its `literature=` parameter; the analyst lead's
  `ReadOnlyToolProvider` now passes `include_text_search=False` (knowledge-base tools are the
  reviewer's alone).
- 2026-06-23 — **`_builtin_server_config` now pins `{analyze_env}/bin` to the front of the
  builtin MCP server's `PATH`.** `bash_exec` runs a bare `python` through the shell, which
  resolves against `PATH` — not the server's own interpreter — so inline inspection
  (`python -c "import astropy"`, `import xspec_utils`) previously used whatever conda env
  leaked onto the launching shell's `PATH` (the configured `analyze_env` was never on it).
  That made it nondeterministic and broke whenever the orchestrator env lacked the science
  stack (observed: `ModuleNotFoundError: No module named 'astropy'`). A bare `python`/`pip`
  inside `bash_exec` is now the same interpreter the toolkit tools invoke directly. Guarded
  by `tests/test_sandbox.py::TestBashExecInterpreterConsistency`.
- 2026-06-24 — **Removed the hardcoded `toolkit_root` fallbacks (fix-list #2).** `runtime.py`
  (both graph builders) now read `orch["toolkit_root"]` directly — the literal
  `"/data/fzhou/research_toolkit"` default is gone; the launch preflight's new
  `check_toolkit_root_configured` is the clean gate, so a missing key fails there with a precise
  message rather than silently routing agents to a stale path. `builder.py`'s no-registry fallback
  (Studio/standalone) reads the root from `load_orchestration_config()` via a lazy import (keeps
  builder ⇍ runtime acyclic) instead of the hardcoded literal.
- 2026-06-24 — **§5.5 lead design channel wiring.** `runtime.py` now builds the analyst lead's
  `ReadOnlyToolProvider` with `include_plan_and_debate=True`, so the lead (only) gets the
  budget-free `plan_and_debate` channel to settle a toolkit design with the human before
  authoring. Reviewer/co-lead providers unchanged. (Loop/tool mechanics in `agents/DOCS.md`.)
