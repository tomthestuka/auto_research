# `auto_research.graph` — LangGraph orchestration

## Purpose
Assembles the agents into the process loop described in the Phase 1 system tour
([CLAUDE.md §8](../../../CLAUDE.md#8-what-phase-1-built-the-system-as-it-stands)) and
exposes async entry points for both monitored runs (`ResearchRun`) and verdict-only
compatibility (`ReviewVerdict`).

## Architecture
A LangGraph `StateGraph` with six nodes. **The reviewer is the sole I/O** (human 2026-09-25):
the run starts at it, every team's work returns to it, and it routes the graph with its report's
`assign_to`. Tool execution is owned by analyst workers inside their `analyze()` ReAct loop via
persistent per-worker sandboxes — not by graph nodes.

```
START ─► review ─assign_to─┬─ librarian ─► literature ─► review   (≤ max_iterations passes/run)
           ▲               ├─ analysts  ─► analysis   ─► review   (≤ max_iterations passes/run)
           │               └─ human ─► finalize_run ─► compose_report ─► human ─┬─ reply ─► review
           │                                                                    └─ done / unattended / timeout ─► END
           └────────────────────────────────────────────────────────────────────┘
```

- **`review`** runs one reviewer turn (`ReviewerAgent.review`): told what `arrived` since its last
  turn (`request`, `librarian`, `analysts`, `human`), every literature report so far, the latest
  analysis report, its own earlier reports, the human's nudges and files, and the passes left per
  team. Its `turn_end` refuses an assignment past its cap, so `after_review` never reroutes: a
  report it cannot route raises. A report to the human is rendered and compiled inside that
  `turn_end` (a `ReviewerRender` over the round's `report/v<k>/`), and the node records
  `report_latex`/`report_pdf` (with `tex`) and `report_html` (with `html`), each with a level-1
  `reviewer.compose_report.renderer_error` when its file did not come out. The render reads the
  run ledger (`monitor.jsonl_path`) to rebuild each call's arguments.
- **`literature`** runs one librarian pass on the reviewer's `follow_up` and returns to review.
  Literature reports and their receipts **accumulate** across passes (a second search builds on
  the first).
- **`analysis`** runs `DebateToConverge` on an `AnalysisTask` carrying the reviewer's `follow_up`,
  and returns to review — an honest `failed`/`needs_human` outcome included (§9.6, no bypass).
- **`finalize_run`** records the outcome (`run.honest_failure` for a failed analysis,
  `run.report_without_analysis` when none ran) and forwards the report; **`compose_report`** runs
  no model: `report.publish_report` mints `rev:dN` receipts for the files and writes the JSONL
  when `jsonl` is configured. Each report to the human has its own `v<k>/` folder, so an earlier
  `rev:dN` keeps its bytes.
- **`human`** is an edge endpoint, not a unit: no model runs. It sends the report (title,
  abstract, the PDF through `hitl.attachments.resolve_attachments`, `kind="report"`) through the
  blocking `human_handler`. The reply's **text is a nudge** (human 2026-09-25): delivered to the
  reviewer as guidance, never minted, so it grounds nothing. Files the human sends are minted by
  `mint_reply_attachments` as `input:` receipts (cite-only), saved under the ledger (outside every
  allow-list). The exact reply `done` (trimmed, any case) ends the run; so does an unattended run
  once the report is published, and a handler `TimeoutError` (the report stands). Any other
  handler error propagates. A resumed run re-enters this node, which re-sends and waits again.
- The reviewer's report folder and every role's report JSONL live **beside the ledger**, outside
  every allow-list (`sandbox/workspace.report_dir`, `report_record_path`), so no role can read a
  report back and mint its own typed numbers as a receipt.

**Tool execution inside `analysis_node`:** `analysis_node` calls
`consensus_strategy.run(workers, lead, task)`. Each worker's
`AnalystWorkerAgent.analyze()` runs the inner ReAct loop over `SandboxToolProvider`
when sandbox + registry are attached, continues its session from the run ledger
(`agents/history.py`), and exposes `last_tool_results`. `DebateToConverge` injects a
truncated cross-worker summary into `task.instructions` for the next round.

`ResearchState` carries: the request, the literature reports and their receipts, the latest analyst
report, the reviewer's reports (`verdicts`, the latest in `verdict`), `arrived`, the human's nudges
(`human_nudges`) and file handles (`human_thread`), `human_rounds`, two pass counters, the cap, and
the published artifact paths and handles (`report_artifacts`).

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
  The resolved handler reaches the seats through the quick channel: `attach_channel(seats, *,
  run_id, run_control, human_handler, monitor)` builds one `hitl.router.SeatRouter` over the
  reviewer, librarian, lead and workers (never the co-lead or status monitor), so any seat can
  `message` any other or the human. **A stop tears down leaf to root, in the run task (ruling
  D8):** `_interrupt_cleanup` runs in the task that opened the MCP client (shielding it into
  another task failed the anyio close, left the shell server running and turned the stop into a
  bare `CancelledError`). Order: agents (`run_control.cancel_resumed()`, resumed seats and awaited
  human replies) → each sandbox (`kill_now`) → the authoring branch (`session.aclose()`, patch
  saved) → the MCP client → `run.interrupted` + `run.summary` → the landing checklist through the
  connector's `notify` (`_stop_checklist`: every step in order — ✓, ✗ with the reason it failed, or
  "– skipped (reason)" — then each command still running that the close cancels, read from the
  connector's `in_flight_commands()` as "/status answer – cancelled by /stop", ending "cleanup
  complete" or "cleanup finished with failures"; a reset with nothing to reset says so) → the connector's `aclose`, last. Every step is bounded and recorded
  (`run.interrupt.step` / `step_error` / `step_timeout` / `step_skipped` with its reason), and the
  caller gets `UserInterruptionError` with its artifacts. A reset `aclose` reports as failed (an
  event, not a raise) is shown ✗ (read back from the ledger, `_ledger_mark`/`_ledger_since`). An
  outer cancel during the cleanup (a caller's timeout), or one that landed together with the stop's
  own (`_spend_cancels`, `outer_pending=`), is held until every step ran, then re-raised.
  The lead's own unwinding leaves an open authoring link to this cleanup (no summary model call, no
  reset before the sandboxes are killed). Once the run ends — by an error, a stop or a finish — the
  run task is unregistered, so a late `/stop` cannot cancel the teardown, and the `finally` does
  not repeat a cleanup that ran. The run's normal `finally` cancels
  resumed seats too, on a finish or a crash (D5). A
  `run_control.fatal` (a resumed seat's `ReactBudgetExhausted`, ruling D6) cancels the run task;
  `run_research` then runs `_interrupt_cleanup(..., fatal=)` in the run task itself (kill, the
  authoring-branch repair, close; recorded as `run.error`, not `run.interrupted`) and raises it.
  Once `graph.ainvoke` returns, the run task is unregistered (nothing may cancel the teardown) and
  `fatal` is checked once more. A human stop that came first wins over a later failure.
  Missing dependencies are constructed from `config/models.yaml`. Reads
  `tool_retry_cap`, `toolkit_root`, `analyze_env`, and `analyze_ini_sh` from
  `config/orchestration.yaml`.  Creates per-run workspace
  (`<runtime_dir>/runs/{run_id}/`; `runtime_dir` from `config/orchestration.yaml`), spawns one `PersistentSandbox`
  per `AnalystWorkerAgent`, assigns each worker's `sandbox`,
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
- `ResearchState` — `TypedDict`; see the Architecture paragraph. `literature` and `verdicts`
  accumulate (`operator.add`), `literature_receipts` merges by handle, `human_nudges` and
  `human_thread` accumulate. A checkpoint without `arrived` came from the graph before 2026-09-25
  and `run_research(resume=True)` refuses it (`_refuse_pre_sole_io_checkpoint`).
- Monitor events are optional. When a monitor is supplied, graph nodes record
  node starts, search strategy/results, local retrieval paths, analyst reports,
  worker tool-result summaries (via consensus/agents), reviewer verdicts, and report
  composition events.
- `IOAgentLike` / `ReviewerLike` — minimal protocols for dependency injection.
  `ToolWorkerLike` protocol removed from `builder.py` (tool execution moved to
  consensus/roles layers).
- The graph routes on the reviewer's `assign_to` alone. Its `follow_up` becomes the librarian's
  `research(follow_up=…)` or the analysts' instructions; a report to the human is published and
  sent. `max_iterations` caps librarian and analysis passes per run; a human reply changes no
  budget (the human nudges, the reviewer assigns).
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
  workers and are recorded in the run ledger, which is every seat's session history.
- `IOAgentLike` — protocol with `async research(request, *, follow_up) -> LiteratureReport` and
  `last_receipts: dict[str, Receipt]`.
- `ResearchState` literature keys: `literature: list[LiteratureReport]` (accumulated),
  `literature_receipts: dict[str, Receipt]` (merged).
- `ReviewerLike` — protocol with `async review(request, *, arrived="request", literature=(),
  literature_receipts=None, report=None, previous=(), human_nudges=(), human_files=(),
  budgets=None, human_attended=False, render=None) -> ReviewerReport`.
- `END_REPLY = "done"` — the whole human reply that ends the run.
- `_version_dir(run_id, k)` — the k-th report-to-the-human folder `report/v<k>/` (with `figures/`).
- `_report_message(report, *, attached)` — the human's message: title, abstract, how to reply;
  the sections too when no PDF is attached.
- `_refuse_pre_sole_io_checkpoint(existing)` (runtime) — raises on a pre-2026-09-25 checkpoint.
- `attach_reviewer_runtime(reviewer, *, run_id, toolkit_root, run_monitor=None, retrieval=None,
  web_source=None, human_handler=None)` — the reviewer's production wiring (tools via
  `readonly_tools_for("reviewer")`, `report_dir`, the run's `ReceiptStore`), called by
  `run_research` and by live cells so they drive the same reviewer.
- `readonly_tools_for(role, *, roots, retrieval=None, web_source=None) -> ReadOnlyToolProvider` — the read-only inspection loop production gives the
  `reviewer` or the `analyst_lead`. The two differ on purpose: the lead gets the data catalog and,
  when a human is reachable, the budget-free design channel, and is denied the paper knowledge
  base, because it investigates toolkit source and the archive rather than literature. Any other
  role raises rather than receiving a default loop.
- `readonly_roots_for(run_id, toolkit_root) -> list[Path]` — the three roots those two roles may
  read: the run directory, the papers directory, the toolkit. Report files are not in it: they
  live beside the ledger.
- `attach_worker_runtime(workers, *, run_id, registry, toolkit_root, analyze_env=None,
  analyze_ini_sh=None, repl_init_py=None, mcp_client=None, run_control=None)
  -> list[PersistentSandbox]` — gives each `AnalystWorkerAgent` its stable key,
  report directory, one persistent sandbox and the registry, and registers the sandbox's aborter
  so a human stop cancels an in-flight tool call. Returns the sandboxes for the caller to shut
  down. **Without it a worker has no tool loop**: `analyze()` runs its inner ReAct loop only when
  a sandbox and a registry are both attached, and otherwise falls back to a plain structured call.
- `build_graph(..., human_handler=None)` — constructs and compiles the six-node state graph
  (`review`, `literature`, `analysis`, `finalize_run`, `compose_report`, `human`).
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
Compacted 2026-09-01 and again 2026-09-24 (every entry), and 2026-10-07 (the 2026-10-06/07
entries); detail in git history.

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
- 2026-09-25 — [R1] The reviewer is the sole I/O (human ruling): START → `review`; one router on
  `ReviewerReport.assign_to ∈ {librarian, analysts, human}` replaces the two stage-bound reviews
  and flags; librarian passes accumulate; new `human` edge endpoint (blocking handler, PDF attached
  via E11's `resolve_attachments`, reply text a nudge never a receipt, files `input:` receipts,
  `done`/unattended/timeout end the run); report folders versioned `v<k>/` and moved beside the
  ledger with every role's report JSONL; `build_graph(human_handler=)`; pre-change checkpoints
  refused on resume. Deterministic only; live adversarial cells pending.
- 2026-09-25 — [E12] (wording from auto-research-3b) The human handler reaches the seats through
  `attach_channel` / `hitl.router.SeatRouter`; `readonly_tools_for` dropped `human_handler`/`model`;
  `_interrupt_cleanup` cancels resumed seats first.
- 2026-09-25 — [R1] LIVE-TESTED: `tests/test_live_reviewer_paths.py` graph cell (real reviewer + librarian, stub analysis, attacking human): START → review, librarian pass, three reports to the human in `v1`–`v3` with earlier PDFs re-verified, nudges minted nothing, exact `done` ended the run.
- 2026-10-06 — **[F20] [F22] [F23] Shell server and run records.** `_builtin_server_config` launches
  the shell server inside the OS jail (`jail.analysis_policy`: toolkit read-only, private `/tmp`,
  cwd writable; `PYTHONNOUSERSITE=1`; private `PFILES` and home) and records `"jailed"` on the
  returned config (read by the shell guard). Detached-run records (`backend.pid`, `result.json`,
  the monitor) live beside the ledger; `overwrite=True` erases the ledger directory too. Workers'
  REPL workdir is `agent_dir`.
- 2026-10-06 — **Environments (env plan B2b, B3c-5).** `SandboxSettings.catalogue`
  (`partial(env.env_catalogue, config)`) reaches each worker's sandbox via `attach_worker_runtime`;
  the shell's jail binds `<runtime>/conda/envs` and `activation/` read-only. `run_research`
  reconciles the pipeline's environments after preflight (`_reconcile_environments` →
  `merge_flow.reconcile_environments`): missing or stale builds made, a drifted prefix kept aside
  and rebuilt, changed externals reported, each a ledger event; a problem never stops the launch; a
  stop kills the builds.
- 2026-10-17 — `report_html` in the state, `ResearchRun`, the detached result and the CLI; `publish_report(html_path=)` mints it as a `rev:` receipt; `run.report` names it.
- 2026-10-17 — The human node attaches the published PDF, else the HTML page (`report_formats` without `tex`); the message says "The report is attached.".
- 2026-10-17 — `_normalize_report_formats` accepts `html` and validates the configured default too; `compose_report` republishes only this run's formats (a resumed checkpoint's earlier PDF is not sent); `run.human.sent` records `attachment`.
- 2026-10-18 — [B2] `attach_worker_runtime` no longer assigns a memory path (memory.md retired; the run ledger is every seat's session history).
- 2026-10-18 — [D5 review] `run_research`'s `finally` cancels the run's channel tasks
  (`run_control.cancel_resumed()`) before the sandboxes shut down, on a normal finish or a crash
  too, so a seat woken as the run ended never outlives it (restarting a sandbox, spending after
  `run.finish`).
- 2026-10-18 — [D6 ruling] `run_research` raises `run_control.fatal` — set when a resumed seat
  exhausts its caps in a channel task — whether it arrives as the run task's cancellation or after
  the graph returned; recorded as `run.error` with `from: channel`.
- 2026-10-18 — [D6 review] The fatal path runs the interrupt's kill-and-repair in the run task
  (`_interrupt_cleanup(fatal=)`); the run task is unregistered when the graph returns; a stop that
  came first wins.
- 2026-10-18 — [D8 ruling] `/stop` cleans leaf to root in the run task (no shield): every step
  recorded (`run.interrupt.step*`, skips with their reason), the landing checklist sent before the
  connector closes, the connector closed last (`aclose` now found). Tests:
  `tests/test_stop_teardown.py` (real `run_research`, real MCP server process checked gone).
- 2026-10-18 — [D8 review] Outer cancels held during the stop cleanup; step 1 bounded
  (`cancel_resumed(timeout=)`); a quietly failed reset shown ✗; the run task unregistered on every
  way out; the `finally` skips a cleanup that ran.
- 2026-10-18 — [D8 follow-ups] Checklist: a justified skip reads "– skipped (reason)", cancelled
  in-flight commands are listed, a reset with nothing to reset says so, patch paths in backticks,
  a step's own timeout message kept; an outer cancel landing with the stop is honoured after the
  cleanup. Live: `tests/test_live_stop_telegram.py` (2 cells).
