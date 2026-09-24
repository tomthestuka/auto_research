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
Compacted 2026-09-01: entries through 2026-07-31 folded into themed ranges (detail in git history).

- 2026-05-26 -> 2026-06-26 — **Graph/runtime built, ReAct-wired, then made reviewer-driven.** Six-node
  LangGraph incl. `compose_report`, two capped feedback edges, Studio wiring, per-run workspace, one
  persistent sandbox per worker, MCP client lifecycle, read-only reviewer/lead tools, required-env
  checks, report formats. Phase 2 steps 0/5/7: off-repo MCP cwd + lead read-only `toolkit_root`;
  `run_research` resolves the raise-to-human channel, awaits the async preflight (channel ping) and
  threads the handler to roles; consensus authoring/resume wired. Then: the `literature` node only
  ingests and the reviewer pulls literature via its own `text_search`/`open_paper`; `bash_exec` PATH
  pinned to `{analyze_env}/bin`; hardcoded `toolkit_root` fallbacks removed. **Reviewer is the sole
  I/O (§9.6):** `analysis → review_analysis` is unconditional, `finalize_run` forwards the reviewer's
  verdict (recording `run.honest_failure`), and a report is always composed on finalize so an honest
  failure is documented rather than silent.
- 2026-07-06 -> 2026-07-16 — **Detached runs, and two ways they died.** `run_research_nohup` ([E5],
  partial) runs the pipeline in a detached child surviving kernel death, reconnecting by deterministic
  `run_id`, with `overwrite` guarded by process-based liveness and `RunDied` on a dead run. Two live
  failures followed: liveness now treats a **zombie** as dead (a backend that died at preflight stays
  unreaped in the notebook kernel's process table, and `overwrite=True` refused with a useless "kill
  <pid>" hint — a zombie ignores signals), and `_spawn_orphaned(argv, log_path, env=None)`
  **double-forks** so the backend is
  reparented to init at spawn — a real VSCode logout killed a live run because graceful teardown kills
  process *trees* by walking `/proc` parent→child links, which `start_new_session` does not sever (the
  original verification had SIGKILLed the kernel, so no cleanup ran and the gap never showed).
- 2026-07-22 -> 2026-07-31 — **Durable resume (Stages 1–3), the cost/summary ledger, and the interrupt
  path (Stage 4).** New `graph/checkpoint.py`: `run_research` always compiles with a per-run
  `AsyncSqliteSaver` at `run_dir/checkpoint.sqlite` (`thread_id=run_id`, `durability="sync"`), snapshotting
  `ResearchState` after every node; `resume=True` invokes with input `None` (the in-flight node re-runs;
  its ReAct history is not persisted) and records `run.resume`. **No silent non-durable fallback** — a
  `checkpointer_available` preflight fails the launch if `langgraph-checkpoint-sqlite` is absent.
  `run_research_nohup(resume=True)` relaunches a dead/`failed` backend (mutually exclusive with
  `overwrite`); CLI `--resume`. Stage 2: the resume calls `heal_toolkit_branch_if_dirty` **before**
  `run_preflight`, resetting a dirty `ai` branch to the persisted per-attempt base ref
  (`run.resume.heal`). Stage 3: `build_graph(run_control=)` + stop-aware review edges (**both deleted
  2026-07-31**, being unreachable once a stop cancels rather than winds down) and the `CommandContext` on
  a command-capable connector. **The full killed-and-resumed run was NOT LIVE TESTED then** (it passed
  four times later, 2026-08-03). **Cost/summary (2026-07-29):** `run_research` installs the run's
  `CostMeter` before any model client (so every monitor line, preflight included, carries accumulated
  spend), pins the monitor's `run_id` to the **run's** id (it had minted an unrelated `uuid4`), adds
  `cost_usd` to `run.finish`, and records a derived `run.summary` on the success *and* failure paths —
  rendered by `ResearchRun.summary()`, the CLI text output and `detached._partial` for a `RunDied`. `backend.log` is **appended**, not opened `"w"` (a
  `--resume` relaunch had erased the dead attempt's traceback, the thing `RunDied` points the human at);
  only `overwrite=True` is destructive, and the `result.json` unlink on relaunch stays so a stale `failed`
  marker cannot read as done (its reason survives as the ledger's `run.error`). **Interrupt path
  (2026-07-31):** `run_research` registers its task on `RunControl`, so a `/stop` — or SIGINT/SIGTERM
  under `install_signal_handlers=True`, which only the CLI and the detached backend pass — cancels it
  wherever it awaits. A `CancelledError` carrying `run_control.stop_requested` is ours: the task is
  `uncancel()`-ed, `_interrupt_cleanup` runs under `asyncio.shield` (teardown otherwise dies with the
  scope it is in, leaving sandbox children alive and the branch dirty), and `UserInterruptionError` is
  raised naming the run dir and ledger; a `CancelledError` *without* the flag is re-raised untouched,
  never relabelled as a human stop. `_interrupt_cleanup` is three ordered phases — **kill** every process
  that can write (each sandbox and its descendants), **repair** (the live `AuthoringSession.aclose()`,
  i.e. reset the branch to the attempt's base ref; the reverse order lets the coder re-dirty the branch a
  moment after the reset), then **close and record** (MCP, HITL connector, `run.interrupted`, the derived
  `run.summary`). Every step is bounded (`_INTERRUPT_STEP_TIMEOUT_S` 10 s, 30 s for the reset); a wedged
  step logs `run.interrupt.step_timeout` and yields, a branch left dirty by a timeout being caught by the
  next launch's clean-tree preflight and the resume-time heal. **No model is called on this path and no
  report is composed.** `reconstruct_run` raises `UserInterruptionError` (not `RunDied`) for an
  `interrupted` marker, so a detached notebook run sees the same exception as an in-process one. Tests:
  `tests/test_interrupt.py` (+15).

- 2026-09-08 — **Role wiring extracted into three factories the run path itself calls.** A caller
  that assembled a role by hand assembled a *different* role, and the worker was the sharp case:
  `analyze()` runs its inner ReAct tool loop only when a sandbox and a registry are attached and
  otherwise falls back to a plain structured call, so a hand-built worker looks like a worker,
  answers like a worker, and never calls a tool. Measured the same day on the science sweep's seat
  probe: its worker cell attached neither, so the failure it produced was **unattributable** — a
  model that will not drive a loop and a model that was never given one are indistinguishable from
  outside. Its lead cell was wrong more quietly, building a provider that differed from
  production's by five arguments. `run_research` now calls `readonly_tools_for`,
  `readonly_roots_for` and `attach_worker_runtime` rather than restating them, so there is one
  definition of what each role can do. Behaviour-preserving: the reviewer and lead branches
  reproduce the previous flag sets exactly, with `include_plan_and_debate` now derived from
  whether a human handler exists (previously passed `True` unconditionally for the lead, which was
  already conditional one layer down in the provider). New `tests/test_role_wiring_seams.py` (8)
  is a static guard: the two constructors appear in the run path only inside their factory,
  `run_research` actually calls both, the seat probe hand-rolls neither, and the lead's and
  reviewer's loops still differ the way this document says. **Known second definitions, left
  alone:** six live tests and `benchmarks/author_bench/trial.py` build a lead provider directly.
  The benchmark's copy was checked rather than assumed and resolves to the identical five tools,
  so the published Table 6 / Fig. 5 numbers are unaffected; no number in the paper is backed by a
  live test at all.

- 2026-09-24 — **Literature node = one librarian research pass** (95). `literature_node` calls
  `io_agent.research()` and puts the filed `LiteratureReport` and its receipts into state
  (`literature`, `literature_receipts`); `IOAgentLike` shrank to that method. The runtime creates
  the `librarian_io` working folder and sets `io_agent.report_dir`. The compose→search→ingest
  pipeline is gone.

- 2026-09-24 — **Workers and the lead get the run's receipt store (provenance P2, session 0f).**
  `attach_worker_runtime` sets `worker.receipts` and `attach_lead_runtime` sets `lead.receipts`
  plus `lead.tools.receipts` / `receipt_source="analyst_lead"`, all from
  `ReceiptStore.for_run(run_dir(run_id))` — the one process-wide store per run (the librarian's
  attach site uses the same accessor), so every role's handles share counters and survive a resume.
- 2026-09-24 (5c) — **The reviewer is a unit.** Reviewer nodes pass `stage`, the literature report
  and receipts, and `iterations_left`; `review_analysis_node` builds the `ReviewerRender` that makes
  rendering part of the reviewer's `turn_end`; `compose_report` publishes (receipts + JSONL) with no
  model. `ReviewVerdict` → `ReviewerReport` throughout; `cited_paths`/`_resolve_cited_paths` removed;
  `build_graph(storage=, report_retry_cap=)` → `receipts=`; new `attach_reviewer_runtime` seam
  (`test_role_wiring_seams` counts a factory called through it as the run path's);
  `readonly_tools_for("reviewer")` passes `include_open_paper=False`; `detached.py` reads the
  verdict as `ReviewerReport`. `config: report_retry_cap` removed.
