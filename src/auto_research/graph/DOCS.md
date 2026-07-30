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
accumulated verdict into `state["verdict"]`, then routes to `compose_report`
whenever any report format is configured (else `END`).

**The reviewer is the sole I/O (§9.6).** Every analysis result — success *or* an honest
`failed`/`needs_human` outcome — is adjudicated by `review_analysis`: `analysis → review_analysis`
is now an **unconditional** edge (the old `after_analysis` bypass that sent a failed report
straight to `finalize_run` is gone; the analyst lead gets no bypass). The reviewer decides
**finalize vs. bounded rework** (`needs_analysis_rework`, capped by `max_iterations`), and on
finalize a report is **always** composed (`route_after_finalize → compose_report` when
`report_formats` is non-empty), so an honest failure is **documented, not silent**.
`finalize_run` only records the terminal (`run.honest_failure` for a `failed`/`needs_human`
status) and forwards the reviewer's verdict. The no-fabrication guarantee now lives in the
reviewer + compose prompts (document the limitation; never invent a result), not in suppressing
the report.

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
  `.markdown()` for grouped monitor output and `.summary()` for the end-of-run digest block
  (renders the recorded `run.summary` event, so a reconnected/replayed run reports exactly what
  the run computed).
- `_default_graph()` — synchronous; builds the compiled graph with all defaults from
  config. Called at import time by `serve.py` for LangGraph Studio.
- `load_orchestration_config(path?)` — loads `config/orchestration.yaml`; returns dict.
- `serve.py` — LangGraph Studio entry point; exposes `graph = _default_graph()` at
  module level so `langgraph dev` can import it. Referenced by `langgraph.json`.
- `DEFAULT_WORKER_COUNT` — default analyst worker count (currently 2).

## Changelog
- 2026-05-26 -> 2026-06-12 — Graph/runtime built and ReAct-wired: six-node LangGraph incl. `compose_report`, two capped feedback edges, Studio wiring, per-run workspace, one persistent sandbox per worker, MCP client lifecycle, read-only reviewer/lead tools, required-env checks, report formats.
- 2026-06-12 -> 2026-06-18 — Phase 2 steps 0/5/7: off-repo MCP cwd + lead read-only `toolkit_root`; `run_research` resolves the raise-to-human channel, awaits the async preflight (channel ping), threads the handler to roles; consensus authoring/resume wired (`DebateToConverge` gets `tool_author_cap`/`toolkit_root`/`worktree_base`/`direct_branch`/`human_handler`).
- 2026-06-22 -> 2026-06-24 — Reviewer-driven retrieval + env/path fixes: the `literature` node only ingests, the reviewer pulls literature via its own `text_search`/`open_paper` (`_resolve_cited_paths`); `bash_exec` PATH pinned to `{analyze_env}/bin`; hardcoded `toolkit_root` fallbacks removed; lead built with `include_plan_and_debate=True`.
- 2026-06-26 — Reviewer is the sole I/O (§9.6): `analysis → review_analysis` unconditional (the `after_analysis` bypass removed); `finalize_run` forwards the reviewer's verdict (records `run.honest_failure`); on finalize a report is always composed so an honest failure is documented, not silent.
- 2026-07-06 — Detached runs `run_research_nohup` (E5, partial): pipeline runs in a detached `python -m auto_research` child (own session) surviving kernel death, reconnect by deterministic `run_id`, `overwrite` guarded by process-based liveness, `RunDied` on a dead run; streams live `monitor.jsonl` chatter to `stream`. Additive `run_id` + CLI `--run-id`/`--max-iterations`/`result.json`. Not true mid-run resume (§12 deferred).
- 2026-07-16 — Liveness treats a **zombie** as dead (`_proc_create_time` returns None on state `Z`): a backend that died at preflight stays unreaped in the notebook kernel's process table, and `overwrite=True` refused with a useless "kill <pid>" hint (a zombie ignores signals; only reaping clears it). Observed live on the #21 relaunch after a Telegram-ping preflight failure. Test: `test_detached.py::test_liveness_zombie_is_dead`.
- 2026-07-16 — **Double-fork spawn (`_spawn_orphaned`): detached backends survive logout.** A real VSCode logout killed a live detached run seconds before reconnect: `start_new_session` shields against signals and kernel death, but the graceful teardown kills process TREES by walking parent→child `/proc` links, which setsid does not sever (the [E5] verification had SIGKILLed the kernel — no cleanup ran — so the gap never showed). `_default_launcher` now spawns via a short-lived intermediary that starts the backend (own session, log wired) and exits, reparenting it to init immediately — verified adopted by pid 1 on the cluster host. Test: `test_detached.py::test_spawn_orphaned_breaks_ancestry`.
- 2026-07-22 — **Durable mid-run resume — Stage 1 of the §12 bundle ([E5] backend-death recovery).** New `graph/checkpoint.py` (`open_checkpointer`/`checkpoint_path`/`check_checkpointer_available`). `build_graph(checkpointer=)` → `compile(checkpointer=)`. `run_research` always compiles with a per-run `AsyncSqliteSaver` at `run_dir/checkpoint.sqlite` (`thread_id=run_id`, `durability="sync"`) — `ResearchState` is snapshotted after every node; `resume=True` invokes with input `None` to continue from the latest snapshot (the in-flight node re-runs; its ReAct history is not persisted), else fresh; a `run.resume` monitor event records which. **No silent non-durable fallback** — a new `checkpointer_available` preflight fails the launch if `langgraph-checkpoint-sqlite` (pip; §environment.yml) is absent. `run_research_nohup(resume=True)` re-launches a dead/`failed` backend under `--resume` (mutually exclusive with `overwrite`); CLI `--resume`. Deterministic tests `tests/test_durable_resume.py` (checkpointer mechanism via `MemorySaver` + real `AsyncSqliteSaver` cross-instance/process-boundary resume + the nohup relaunch plumbing). **Full killed-and-resumed `run_research` is NOT LIVE TESTED yet** (needs a real run). Stages 2–4 (dirty-branch self-heal, HITL slash-commands, graceful cleanup) follow.
- 2026-07-22 — **Durable resume Stage 2: authoring-branch self-heal.** `run_research(resume=True)` calls `merge_flow.heal_toolkit_branch_if_dirty(run_dir(run_id))` **before** `run_preflight` — resetting a dirty `ai` branch (left by a hard-kill mid-authoring) to the per-attempt base-ref persisted under `run_dir/authoring/current_base_ref`, so the launch clean-tree check then passes instead of looping forever; records a `run.resume.heal` event. `run_id`/request-normalization moved ahead of the preflight to enable this. See `sandbox/DOCS.md` for the heal + `ToolkitBranchUnclean` degrade. Tests `tests/test_resume_heal.py`; suite green (527).
- 2026-07-22 — **HITL /stop honored in the graph (§12 Stage 3).** `build_graph(run_control=)`; the
  `after_review_literature`/`after_review_analysis` edges route toward `finalize_run` (no more
  literature / no rework) once `run_control.stop_requested`, so a human `/stop` binds the whole run
  to an honest §9.6 terminal (workers/lead already break in `_react`; the consensus authoring loop
  skips new authoring). `run_research` builds the `RunControl`, threads it to every role + the
  consensus strategy + `build_graph`, and attaches a `hitl.commands.CommandContext` (roster, monitor
  JSONL, `status_monitor` model) to a command-capable connector (Telegram/Slack).

- 2026-07-29 — **Cost meter + end-of-run summary wired into the entry point.** `run_research`
  installs the run's `CostMeter` before any model client (so every monitor line carries accumulated
  spend, preflight included), pins the monitor's `run_id` to the **run's** id (it used to mint an
  unrelated `uuid4`, leaving log lines untraceable to their run dir), adds `cost_usd` to
  `run.finish`, and records a derived `run.summary` event on both the success and failure paths.
  `ResearchRun.summary()` renders that digest; the CLI text output prints it above the verdict
  fields; `detached._partial` prepends it to a `RunDied`'s partial so a killed run is legible too.
- 2026-07-29 (follow-up) — **`backend.log` is appended, not truncated.** The orphan spawner opened it
  with `"w"`, so a `--resume` relaunch erased the dead attempt's output — the traceback a `RunDied`
  points the human at, and the only record of why the run needed resuming. Only `overwrite=True`
  (which removes the run dir) is destructive now. The `result.json` unlink on relaunch stays: a stale
  `failed` marker must not make the relaunched run read as done, and the reason survives in the
  ledger's `run.error` event.
