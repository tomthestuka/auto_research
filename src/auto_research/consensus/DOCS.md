# `auto_research.consensus` — analyst-worker debate

## Purpose
Drives analyst workers to agreement through bounded debate. Behind a swappable
`ConsensusStrategy` interface so the policy can be replaced without touching
agent or graph code ([CLAUDE.md §8](../../../CLAUDE.md#8-what-phase-1-built-the-system-as-it-stands)).

## Architecture
Phase 1 ships two strategies — one for analyst debate, one for IO abstraction:

**Analyst debate** (`DebateToConverge`):
- Each worker produces an analysis.
- Each worker's `analyze()` owns its own ReAct tool loop; consensus reads the
  resulting report, figures, and `last_tool_results` rather than executing tools itself.
- Each round, workers see peers' analyses (and figures, by `FigureRef`) and
  critique/revise.
- After each round, the **analyst lead** judges convergence and summarizes
  remaining disagreement.
- Stop on convergence **or** at a hard cap of **3 rounds**, after which the lead
  adjudicates/synthesizes a final report and flags residual uncertainty.

**IO abstraction debate** (`IODebateToConverge`):
- Mirrors `DebateToConverge` but operates over `MemoryRecord` instead of
  `AnalystReport`. Workers call `abstract_document()` with peer abstracts;
  the lead calls `judge_abstract_convergence()` and `adjudicate_abstract()`.
- Used by `LibrarianIOAgent.ingest_results()` with `max_rounds=2`.

## Interfaces / Contracts
- `ConsensusStrategy` protocol:
  `async run(workers, lead, task) -> AnalystReport`.
- `DebateToConverge(max_rounds=3)` implements the Phase 1 analyst policy.
  It requires at least one worker, asks all workers for initial reports, checks
  convergence through the lead after each round, requests revisions with peer
  reports when not converged, injects a truncated cross-worker tool-result
  summary from `last_tool_results`, and returns a lead-adjudicated final report.
  Optional `monitor` records worker reports, convergence verdicts, revisions,
  and lead adjudication. `registry` and `tool_retry_cap` are accepted for
  call-site compatibility but unused by the strategy.
- `IOAbstractionStrategy` protocol:
  `async run(workers, lead, document) -> MemoryRecord`.
- `IODebateToConverge(max_rounds=3)` implements the IO abstraction policy.
  Same bounded debate structure as `DebateToConverge` but produces a
  `MemoryRecord` rather than `AnalystReport`. Optional `monitor` records the
  IO abstraction debate.
- Worker objects for analyst debate must provide
  `analyze(task, *, peer_reports=()) -> AnalystReport`.
- Lead objects for analyst debate must provide
  `judge_convergence(task, reports, *, round_index, max_rounds) ->
  ReviewVerdict`, `adjudicate(task, reports) -> AnalystReport`, and (for the
  authoring path) `decide_authoring(reports) -> AuthoringDecision`,
  `drive_authoring(tool_spec, *, toolkit_root, worktree_base, direct_branch, colead, human_handler)
  -> MergeFlowResult`, and `finalize_authoring_outcome(task, reports, tool_spec, result) ->
  AnalystReport`.
- Authoring/resume (Phase 2 step 7; A1 B-topology, §9.3): when ≥1 worker report carries a
  `tool_request` and authoring is wired (`toolkit_root` set, with `direct_branch`), the strategy
  asks the lead to `decide_authoring` (plan the whole ordered dependency chain, `tool_specs`); on
  `should_author` it authors **each tool in the chain sequentially** via `lead.drive_authoring(spec,
  …)` — the lead drives the coder + gate + GPT review through its **own** ReAct loop
  (`drive_coder`/`submit_for_review`), diagnosing each verdict, and owns each `AuthoringSession`
  lifecycle. The strategy never calls `propose_and_pr` (that auto-loop is kept for tests / non-lead
  callers); the LLM never issues git — `build_authoring_session`/`approve_edit` do (§9.9). **A8:**
  after the **whole chain** commits — each tool sees the prior tool's committed code (direct-branch
  mode: `worktree == toolkit_root` on the `ai` branch) — the strategy triggers `registry.reload()` +
  per-worker `sandbox.restart()` + a per-tool provenance-augmented task + **one** worker rerun (a
  *resume*), not a reload/restart/rerun per tool. `tool_author_cap` bounds **total tools** across the
  chain (not counted against `max_rounds`); if the budget is hit mid-chain the workers still rerun on
  what committed. `should_author=false` (empty `tool_specs`) falls through to ordinary debate (a
  mis-called tool, not a failure). A **non-committed** chain tool (gate/review cap exhausted, or a
  human block) is an honest failure (§9.6): the chain **stops at that tool** (downstream depends on
  it), the lead composes a terminal report via `finalize_authoring_outcome` and `run()` **returns it**
  immediately — no re-authoring, no debate-spin. That report flows to the reviewer, which is the sole
  I/O and adjudicates it (finalize vs. bounded rework); never fabricated.
- Worker objects for IO debate (`IOAbstractionWorkerLike`) must provide
  `abstract_document(document, *, peer_abstracts=()) -> MemoryRecord`.
- Lead objects for IO debate (`IOAbstractionLeadLike`) must provide
  `judge_abstract_convergence(document, abstracts, *, round_index, max_rounds)
  -> ReviewVerdict` and `adjudicate_abstract(document, abstracts) -> MemoryRecord`.
- The round cap is hard. If convergence is not accepted before the cap, the lead
  adjudicates the latest reports.
- Live consensus verification is opt-in:
  `AUTO_RESEARCH_LIVE_CONSENSUS_TESTS=1 pytest tests/test_live_consensus.py -s`.
  It writes JSONL plus Markdown logs under `logs/dev/step-9/`.

## Dependencies
- Internal: `auto_research.agents`, `auto_research.schemas`, `auto_research.storage`.

## Dictionary-level reference
- `WorkerLike` — protocol for analyst workers used by `DebateToConverge`. Method:
  `analyze()`.
- `LeadLike` — protocol for analyst lead used by `DebateToConverge`. Methods:
  `judge_convergence()`, `adjudicate()`, `decide_authoring()`, and
  `finalize_authoring_outcome()` (the terminal honest report after a non-committed authoring
  attempt, §9.6).
- `ConsensusStrategy` — protocol for swappable analyst consensus policies. Method:
  `run()`.
- `DebateToConverge(max_rounds=3, monitor=None, registry=None, tool_retry_cap=3,
  tool_author_cap=3, toolkit_root=None, worktree_base=None, direct_branch=None,
  human_handler=None, colead=None)` — bounded analyst debate strategy. Each worker gathers
  evidence inside `AnalystWorkerAgent.analyze()`; the strategy injects a truncated shared
  summary from each worker's `last_tool_results` before later rounds and deduplicates figures
  onto the lead's final report so they flow to the reviewer. When `toolkit_root` is set it also
  runs the authoring/resume path (above). Method: `run(workers, lead, task)` → `AnalystReport`.
  Internals: `_initial_reports(workers, task)`; `_maybe_author_and_resume(...)` →
  `(resumed, final_report, reports, task, author_budget)` (`final_report` set ⇔ a non-committed
  authoring attempt — `run()` returns it as the §9.6 terminal); `_augment_with_peer_tools(workers,
  task)`; module-level `_augment_with_authored_tool(task, tool_spec, result)`;
  `_deduplicated(figures)`.
- `IOAbstractionWorkerLike` — protocol for IO abstraction workers. Method:
  `abstract_document()`.
- `IOAbstractionLeadLike` — protocol for IO abstraction lead. Methods:
  `judge_abstract_convergence()` and `adjudicate_abstract()`.
- `IOAbstractionStrategy` — protocol for swappable IO abstraction policies.
  Method: `run()`.
- `IODebateToConverge(max_rounds: int = 3, monitor: RunMonitor | None = None)`
  — bounded IO abstraction debate strategy. Method:
  `run(workers, lead, document)`.

## Changelog
- 2026-05-26 -> 2026-06-12 — Consensus strategies built and ReAct-aligned:
  `DebateToConverge` cap-3 analyst debate with lead adjudication, cross-worker
  `last_tool_results` summaries, final figure propagation, and `IODebateToConverge` for
  PDF memory abstraction. `registry`/`tool_retry_cap` remain accepted but unused.
- 2026-06-12 — Compacted changelog and removed stale consensus-owned tool-execution text.
- 2026-06-16 — Phase 2 step 7: `DebateToConverge` drives lead-driven tool authoring/resume.
  New optional fields (`tool_author_cap`, `toolkit_root`, `worktree_base`, `direct_branch`,
  `human_handler`, `colead`); `LeadLike` gained `decide_authoring`; `run()` restructured with a
  separate `debate_round`/`author_budget` and a `_maybe_author_and_resume` helper. A committed
  tool reloads the registry, restarts sandboxes, augments the task with provenance, and reruns
  workers — bounded by `tool_author_cap`, not counted against `max_rounds`. Phase-1 behaviour is
  unchanged when `toolkit_root` is unset. Coverage: `tests/test_consensus_authoring.py`.
- 2026-06-26 — **Honest finalize on a non-committed authoring attempt (handoff #4).** A
  non-committed `propose_and_pr` result no longer falls through to debate (where workers
  re-requested the tool and the lead re-authored — the cap-exhausted *spin* of the 2026-06-26
  run). `_maybe_author_and_resume` now returns `(resumed, final_report, …)`; on a non-committed
  result it calls the new `lead.finalize_authoring_outcome(task, reports, tool_spec, result)` and
  `run()` returns that terminal report immediately. The report flows to the reviewer (the sole
  I/O, no bypass — graph change), which adjudicates finalize vs. bounded rework. `should_author=false`
  still falls through to debate. `LeadLike` gained `finalize_authoring_outcome`. Coverage:
  `tests/test_consensus_authoring.py`.
- 2026-06-29 — **A1 slice 3: the lead drives authoring (B-topology, §9.3).**
  `_maybe_author_and_resume` no longer calls `merge_flow.propose_and_pr`; it calls
  `lead.drive_authoring(tool_spec, toolkit_root=…, worktree_base=…, direct_branch=…, colead=…,
  human_handler=…)`, which builds + connects an `AuthoringSession`, runs the lead's ReAct loop over
  `drive_coder`/`submit_for_review` (the lead diagnoses each gate/review verdict and re-steers), and
  returns the terminal `MergeFlowResult` (committed → resume; failed → `finalize_authoring_outcome`).
  `LeadLike` gained `drive_authoring`. Budget: `tool_author_submit_cap` (orchestration.yaml) bounds
  `submit_for_review`; the loop ceiling is `react_loop_absolute_cap`. The committed/failed routing,
  reload/restart/rerun, and `tool_author_cap` bound are unchanged — `test_consensus_authoring.py`
  stubs `drive_authoring`'s outcome (no SDK) instead of monkeypatching `propose_and_pr`. Suite 373.
- 2026-07-06 — **[A8] author the whole tool chain before re-running workers.** `_maybe_author_and_resume`
  now iterates the lead's ordered `AuthoringDecision.tool_specs` (was a single `tool_spec`), authoring
  each tool sequentially via `drive_authoring` and moving `registry.reload()` + `sandbox.restart()` +
  `_initial_reports` rerun to **once, after the whole chain** (was per tool). Each tool sees the prior
  tool's committed code (direct-branch mode). A mid-chain non-commit stops the chain → honest
  `finalize_authoring_outcome` terminal (§9.6); `tool_author_cap` now bounds total tools across the
  chain; the outer `run()` loop stays the safety net for a gap the plan missed. New tests in
  `test_consensus_authoring.py`: `test_chain_authored_sequentially_reruns_once`,
  `test_chain_midchain_failure_stops_and_finalizes`, `test_chain_budget_caps_total_tools`.
