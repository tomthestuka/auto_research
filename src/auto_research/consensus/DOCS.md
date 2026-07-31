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
  ReviewVerdict`, `adjudicate(task, reports, *, sources=None) -> AnalystReport` (V-4: `sources` names
  each report's worker so the lead keys its per-worker figure catalog and owns figure citation), and
  (for the authoring path) `decide_authoring(reports) -> AuthoringDecision`,
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
  tools are authored **sequentially, stopping at the first non-commit** — each committed tool sees
  the prior tool's committed code (direct-branch mode: `worktree == toolkit_root` on the `ai`
  branch) — then the workers rerun **once** on what committed (a *resume*), not a
  reload/restart/rerun per tool. `tool_author_cap` bounds **total tools** across the chain (not
  counted against `max_rounds`). `should_author=false` (empty `tool_specs`) falls through to
  ordinary debate (a mis-called tool, not a failure). **A11 — recover, never choke on one failed
  session:** because authoring stops at the first failure, everything committed is a complete,
  dependency-satisfied prefix, so `registry.reload()` + per-worker `sandbox.restart()` fire whenever
  **≥1 tool committed** (was: only after the *whole* chain committed — the bug that left committed
  tools out of the reran workers' manifest, #21). A **non-committed** chain tool (gate/review cap
  exhausted, or a human block) is an honest failure (§9.6) that **stops the chain at that tool** and
  records a task note (so the reran workers refine their request and the lead's next
  `decide_authoring` adapts), then: with ≥1 committed the workers rerun on the committed prefix; with
  **nothing** committed but credits remaining the workers still rerun on the failure note so the lead
  re-attempts (burning a `tool_author_cap` credit) — the run recovers instead of failing on the first
  miss. The honest terminal report (`finalize_authoring_outcome`, returned by `run()`) fires **only
  when credits are exhausted with nothing ever committed**; it flows to the reviewer (sole I/O) which
  adjudicates finalize vs. bounded rework; never fabricated.
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
  `(resumed, final_report, reports, task, author_budget)` (`resumed=True` ⇔ progress worth another
  loop — ≥1 tool committed OR nothing committed but credits remain; `final_report` set ⇔ credits
  exhausted with nothing ever committed — `run()` returns it as the §9.6 terminal; A11);
  `_augment_with_peer_tools(workers, task)`; module-level
  `_augment_with_authored_tool(task, tool_spec, result)` (committed-tool provenance note) and
  `_augment_with_failed_tool(task, tool_spec, result)` (failed-attempt note so a retry adapts).
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
- 2026-05-26 -> 2026-06-12 — Consensus strategies built: `DebateToConverge` (cap-3 analyst debate, lead adjudication, cross-worker `last_tool_results` summaries, final figure propagation) and `IODebateToConverge` (PDF memory abstraction). `registry`/`tool_retry_cap` accepted but unused.
- 2026-06-16 — Phase 2 step 7: `DebateToConverge` drives lead tool authoring/resume — new `tool_author_cap`/`toolkit_root`/`worktree_base`/`direct_branch`/`human_handler`/`colead`, `LeadLike.decide_authoring`; a committed tool reloads the registry, restarts sandboxes, and reruns workers, bounded by `tool_author_cap` (not `max_rounds`); Phase-1 behaviour unchanged when `toolkit_root` unset.
- 2026-06-26 — Honest finalize on a non-committed authoring attempt: `_maybe_author_and_resume` returns a terminal report via the new `lead.finalize_authoring_outcome` instead of spinning in debate; the report flows to the reviewer (sole I/O), which adjudicates finalize vs. rework.
- 2026-06-29 — A1: the lead drives authoring (B-topology, §9.3) via `lead.drive_authoring` (builds/connects an `AuthoringSession`, runs its own ReAct loop over `drive_coder`/`submit_for_review`, diagnosing each verdict) instead of `merge_flow.propose_and_pr`; bounded by `tool_author_submit_cap`.
- 2026-07-06 — [A8] author the whole ordered `AuthoringDecision.tool_specs` chain sequentially (each tool sees the prior's committed code), then reload/restart/rerun workers once (was per tool); a mid-chain non-commit stops the chain → honest terminal; `tool_author_cap` bounds total tools.
- 2026-07-14 — V-4: the lead owns figures; `_deduplicated` removed. `run()` no longer force-carries the last round's figures; it calls `adjudicate(task, reports, sources=_source_keys(workers))` and the lead accumulates a per-worker figure catalog and cites the subset.
- 2026-07-22 — [A11] recover, never choke on one failed authoring session. `_maybe_author_and_resume` now stops the chain at the first non-commit and fires `registry.reload()`/`sandbox.restart()`/worker-rerun whenever **≥1 tool committed** (was: only after the *whole* chain committed — the bug that hid committed tools from the reran workers, blocking #21's census). A non-commit records a task note (`_augment_with_failed_tool`) and loops back: ≥1 committed → rerun on the committed prefix; nothing committed but credits remain → rerun on the failure note so the lead re-attempts (burns a `tool_author_cap` credit). The honest §9.6 terminal (`finalize_authoring_outcome`) now fires **only** when credits are exhausted with nothing ever committed. New event `analyst.tool.chain_resume`.


- 2026-07-31 — **The pre-authoring stop check is gone.** `DebateToConverge` no longer polls
  `run_control.stop_requested` before deciding to author or between chain tools: a `/stop` cancels
  the run task, so there is no wind-down window in which the flag is set and the strategy still
  runs. `run_control` is still carried — the lead uses it to publish its live authoring session so
  an interrupt can reset the toolkit branch — and `_stop_requested` is removed. Tests:
  `tests/test_consensus_authoring.py` (the skip-authoring test rewritten to pin the absence).
