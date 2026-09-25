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

Reviewer nodes return state updates containing the newest `ReviewerReport` — the one report the
reviewer filed through `turn_end` for that review (`ReviewerAgent.review(stage=…,
iterations_left=…)`, each stage told what its loop edge has left).
`add_conditional_edges()` then routes from `review_literature` back to
`literature` or onward to `analysis`, and from `review_analysis` back to
`analysis` or onward to `finalize_run`. `finalize_run` copies the latest
accumulated verdict into `state["verdict"]`, then routes to `compose_report`
whenever any report format is configured (else `END`).

**The reviewer is the sole I/O (§9.6).** Every analysis result — success *or* an honest
`failed`/`needs_human` outcome — is adjudicated by `review_analysis`: `analysis → review_analysis`
is now an **unconditional** edge (the old `after_analysis` bypass that sent a failed report
straight to `finalize_run` is gone; the analyst lead gets no bypass). The reviewer decides
**finalize vs. bounded rework** (`needs_analysis_rework`, capped by `max_iterations`), and on
finalize the report is **always** written (the final review's report is the run's report), so an
honest failure is **documented, not silent**. With `tex` configured the `.tex`/PDF are rendered and
compiled **inside the reviewer's `turn_end`** (a `ReviewerRender` built by `review_analysis_node`),
and `review_analysis_node` records `report_latex`/`report_pdf` plus a level-1
`reviewer.compose_report.renderer_error` when the report was accepted but no PDF came out. The
`compose_report` node runs **no model**: `report.publish_report` mints `rev:dN` receipts for the
published files and writes the JSONL when `jsonl` is configured.
`finalize_run` only records the terminal (`run.honest_failure` for a `failed`/`needs_human`
status) and forwards the reviewer's verdict. The no-fabrication guarantee lives in the reviewer's
prompt and its `turn_end` check (every number grounded in a receipt), not in suppressing the report.

**Tool execution inside `analysis_node`:** `analysis_node` calls
`consensus_strategy.run(workers, lead, task)`. Each worker's
`AnalystWorkerAgent.analyze()` runs the inner ReAct loop over `SandboxToolProvider`
when sandbox + registry are attached, writes real `ToolResult`s to its private
`memory_path`, and exposes `last_tool_results`. `DebateToConverge` injects a
truncated cross-worker summary into `task.instructions` for the next round.

The literature node runs **one librarian research pass**: `io_agent.research(request,
follow_up=…)` searches, reads papers and pages in full, and files a `LiteratureReport` through
`turn_end` (see `agents/DOCS.md`). The node writes `state["literature"]` (that report) and
`state["literature_receipts"]` (handle → `Receipt` for every document it opened); each pass
replaces both. Both reviews receive them; the reviewer also keeps `text_search` and its own
receipted `read_paper`.

The analysis node builds an `AnalysisTask` from the question and optional
instructions (carrying the reviewer's `follow_up` on rework). References are built by the
renderer from the paper/web receipts the final report cites.

`ResearchState` carries: the request, the literature report and its receipts, the latest analyst
report, the final reviewer report (`verdict`), the accumulated reviewer reports (`verdicts`), two
iteration counters, the cap, and the published artifact paths.

## Interfaces / Contracts
- `build_graph(*, reviewer, io_agent, retrieval=None, workers, lead,
  consensus=None, max_iterations=3, registry=None, monitor=None, run_id=None, receipts=None,
  report_formats=None, checkpointer=None) -> CompiledStateGraph` —
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
  report_formats=None, human_handler=None, human_channel=None, run_id=None, resume=False) -> ResearchRun` — high-level
  monitored entry. Resolves the raise-to-human channel (`hitl.resolve_handler` — explicit
  `human_handler` → `human_channel` → auto-detect TTY/library), builds the `RunMonitor`, then
  `await run_preflight(human_handler=…, monitor=…)` (the launch-time checklist: `.env` +
  required keys, the author↔reviewer decorrelation, and — when a channel is resolved — a
  **channel ping** that must reply), so a misconfigured/unreachable-human run crashes before
  any LLM is constructed. The monitor is built **before** the preflight (it was after) so each
  check lands in the run ledger as `preflight.<name>` — otherwise the launch state existed only
  as terminal scrollback that a detached or notebook run never sees, and a preflight failure
  produced no run artifact at all ([J1]a).
  It also installs the run's **cost meter** (`observability.install_cost_meter`) before any model
  client exists and pins the monitor's `run_id` to the run's, so every ledger line carries the
  accumulated spend and names the run dir it belongs to; and it records a final `run.summary`
  event (derived from the ledger by `monitor.summary`) after `run.finish` **and** after
  `run.error`, so a crashed run ends on the same digest as a finished one.
  The resolved handler is threaded to the analyst workers and the reviewer/lead tool providers
  so any role can `ask_human`.
  Missing dependencies are constructed from `config/models.yaml`. Reads
  `tool_retry_cap`, `toolkit_root`, `analyze_env`, and `analyze_ini_sh` from
  `config/orchestration.yaml`.  Creates per-run workspace
  (`<runtime_dir>/runs/{run_id}/`; `runtime_dir` from `config/orchestration.yaml`), spawns one `PersistentSandbox`
  per `AnalystWorkerAgent`, assigns each worker's `sandbox` and `memory_path`,
  then tears all sandboxes down after the graph completes or raises.
  Returns the request, final verdict, final state, and `RunMonitor`. Optional `run_id` pins
  the workspace/report/JSONL dir (defaults to an auto-generated uuid) — used by the detached
  entry below; otherwise behavior is unchanged.
  **Durable resume (§12):** the graph is always compiled with a per-run `AsyncSqliteSaver`
  (`run_dir/checkpoint.sqlite`, `thread_id=run_id`, `durability="sync"`) so `ResearchState` is
  snapshotted after every node. `resume=True` re-enters from the latest snapshot — continuing at
  the last *completed* node (the in-flight node's transient ReAct history is not persisted, so it
  re-runs); with no checkpoint it starts fresh. A missing `langgraph-checkpoint-sqlite` package
  fails at the `checkpointer_available` preflight (no silent non-durable run). Resume needs a
  stable `run_id`. **On `resume=True` it first heals a dirty authoring branch** (Stage 2 —
  `merge_flow.heal_toolkit_branch_if_dirty`, run *before* the preflight so the clean-tree check
  passes) that a hard-kill during tool authoring may have left behind, recording a `run.resume.heal`
  monitor event; without this the preflight would refuse the resume and loop forever.
- `async run_research_nohup(request, *, chatter=10, report_formats=None, human_channel=None,
  max_iterations=None, run_id='auto', overwrite=False, resume=False, poll_s=5.0, stream=<cell stdout>) -> ResearchRun`
  (`graph/detached.py`) — runs the pipeline in a **detached** child (`python -m auto_research`
  in its own session via `start_new_session`), so a killed kernel (logout) does not kill it;
  re-running the same call **reconnects** by a deterministic `run_id` (`'auto'` = `sha256`
  of the request; a named string forces a distinct run, validated path-safe). `overwrite=True`
  erases an existing run and starts fresh but **refuses if it is still alive**. Liveness is
  process existence (`backend.pid` = pid + `/proc` start-time, PID-reuse-safe; a **zombie**
  reads as dead — a backend that died at preflight lingers unreaped in its spawner's process
  table and must not block overwrite), never log freshness. **Live progress streams to `stream`** (default: the current cell's `sys.stdout`,
  resolved at call time so it follows ipykernel's per-cell rebinding; pass `None` to silence)
  by following the run's `monitor.jsonl` chatter-filtered — identical to a non-detached live
  run — so detaching does not cost the live view. Output is reconstructed from that `monitor.jsonl`
  + terminal `result.json` (written by the CLI backend); `human_channel='cli'` is rejected (a
  detached child has no stdin). Raises `RunDied` (carrying the run_id, reason, log path, and
  reconstructed partial markdown) when the process died without writing a result — honest, no
  silent relaunch. **`resume=True`** turns that death into recovery instead: a dead backend (or
  one that crashed with a `failed` marker) is **re-launched under `--resume`** to continue from
  its checkpoint; `resume` and `overwrite` are mutually exclusive (overwrite wipes the checkpoint).
- `RunDied(run_id, reason, *, log_path=None, partial='')` — a detached run ended without a
  usable result (process died mid-run, or the pipeline raised).
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
  text is fed back into the next `research(follow_up=…)` call and/or analysis
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
- `ResearchState` — TypedDict for the graph state; `verdicts` is an accumulated list of
  `ReviewerReport`s, `verdict` the final one. Tool queues/results are not graph state; tool calls run inside analyst
  workers and are recorded in per-worker `memory.md` files.
- `IOAgentLike` — protocol with `async research(request, *, follow_up) -> LiteratureReport` and
  `last_receipts: dict[str, Receipt]`.
- `ResearchState` literature keys: `literature: LiteratureReport | None`,
  `literature_receipts: dict[str, Receipt]`.
- `ReviewerLike` — protocol with `async review(request, *, stage, literature=None,
  literature_receipts=None, report=None, iterations_left=0, render=None) -> ReviewerReport`.
- `attach_reviewer_runtime(reviewer, *, run_id, toolkit_root, run_monitor=None, retrieval=None,
  web_source=None, human_handler=None)` — the reviewer's production wiring (tools via
  `readonly_tools_for("reviewer")`, `report_dir`, the run's `ReceiptStore`), called by
  `run_research` and by live cells so they drive the same reviewer.
- `readonly_tools_for(role, *, roots, retrieval=None, web_source=None, human_handler=None,
  model="") -> ReadOnlyToolProvider` — the read-only inspection loop production gives the
  `reviewer` or the `analyst_lead`. The two differ on purpose: the lead gets the data catalog and,
  when a human is reachable, the budget-free design channel, and is denied the paper knowledge
  base, because it investigates toolkit source and the archive rather than literature. Any other
  role raises rather than receiving a default loop.
- `readonly_roots_for(run_id, toolkit_root) -> list[Path]` — the three roots those two roles may
  read: the run directory, the papers directory, the toolkit.
- `attach_worker_runtime(workers, *, run_id, registry, toolkit_root, analyze_env=None,
  analyze_ini_sh=None, repl_init_py=None, mcp_client=None, run_control=None)
  -> list[PersistentSandbox]` — gives each `AnalystWorkerAgent` its stable key, memory path,
  report directory, one persistent sandbox and the registry, and registers the sandbox's aborter
  so a human stop cancels an in-flight tool call. Returns the sandboxes for the caller to shut
  down. **Without it a worker has no tool loop**: `analyze()` runs its inner ReAct loop only when
  a sandbox and a registry are both attached, and otherwise falls back to a plain structured call.
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
  `.markdown()` for grouped monitor output and `.summary()` for the end-of-run digest block
  (renders the recorded `run.summary` event, so a reconnected/replayed run reports exactly what
  the run computed).
- `_default_graph()` — synchronous; builds the compiled graph with all defaults from
  config. Called at import time by `serve.py` for LangGraph Studio.
- `load_orchestration_config(path=None)` — loads the configured `orchestration.yaml`; returns a
  dict. `None` asks `auto_research.config_paths`, resolved per call rather than captured as a
  default argument (which would be evaluated at import and pin the answer before a caller could
  point the process elsewhere).
- `serve.py` — LangGraph Studio entry point; exposes `graph = _default_graph()` at
  module level so `langgraph dev` can import it. Referenced by `langgraph.json`.
- `DEFAULT_WORKER_COUNT` — default analyst worker count (currently 2).

## Changelog
Compacted 2026-09-01 and again 2026-09-24 (every entry); detail in git history.

- 2026-05-26 -> 2026-06-26 — Six-node graph (`compose_report`, two capped feedback edges, Studio,
  per-run workspace, per-worker sandbox, MCP lifecycle); HITL channel, async preflight and consensus
  authoring wired. Reviewer is the sole I/O (§9.6): a report is always composed on finalize.
- 2026-07-06 -> 2026-07-16 — [E5] `run_research_nohup` detached runs (reconnect by `run_id`,
  `RunDied`); liveness treats a zombie as dead; `_spawn_orphaned` double-forks to survive IDE logout.
- 2026-07-22 -> 2026-07-29 — Durable resume: `graph/checkpoint.py` (`AsyncSqliteSaver`, `resume=True`,
  `checkpointer_available` preflight), `heal_toolkit_branch_if_dirty` before preflight, Stage 3
  `CommandContext`; killed-and-resumed run LIVE-VERIFIED 2026-08-03.
- 2026-07-29 — Cost meter installed before any model client; monitor `run_id` pinned to the run;
  `cost_usd` on `run.finish`; `run.summary` on success and failure; `backend.log` appended.
- 2026-07-31 — Interrupt: `/stop` or signals cancel the registered task; shielded `_interrupt_cleanup`
  (kill → repair branch → close/record, each step bounded) raises `UserInterruptionError` with no
  model call; `build_graph(run_control=)` and stop-aware edges deleted.
- 2026-09-08 — Role wiring extracted into `readonly_tools_for`, `readonly_roots_for` and
  `attach_worker_runtime`, called by `run_research` (behaviour-preserving); static guard
  `tests/test_role_wiring_seams.py`.
- 2026-09-24 — Literature node runs one librarian `research()` pass into `literature` /
  `literature_receipts` (`IOAgentLike` shrank to it); workers and the lead get
  `ReceiptStore.for_run` via `attach_worker_runtime` / `attach_lead_runtime`.
- 2026-09-24 (5c) — Reviewer is a unit: `ReviewVerdict` → `ReviewerReport`, rendering inside its
  `turn_end` (`ReviewerRender`), `compose_report` publishes with no model, new
  `attach_reviewer_runtime`; `build_graph(storage=, report_retry_cap=)` → `receipts=`.
