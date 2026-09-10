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
- Lead objects for analyst debate must provide **one** method:
  `run_cycle(task, reports, *, sources=None, rounds_left=0, author_budget=0, toolkit_root=None,
  worktree_base=None, direct_branch=None, colead=None, human_handler=None) -> LeadCycleOutcome`.
  The five actions this used to name (judge convergence, adjudicate, decide authoring, drive it,
  finalize an honest failure) are branches the lead takes *inside* one ReAct loop by filing a form,
  so consensus asks for a cycle and reads which form came back. `sources` still names each report's
  worker so the lead keys its per-worker figure catalog and owns figure citation (V-4);
  `rounds_left` and `author_budget` are the caps consensus owns.
- Authoring/resume (Phase 2 step 7; A1 B-topology, §9.3): when ≥1 worker report carries a
  `tool_request` and authoring is wired (`toolkit_root` set, with `direct_branch`), the strategy
  passes `author_budget` into the cycle and the **lead's own loop** opens at its `decide` stage: it
  reads the relevant `lib/`, then files `author_chain` (the whole ordered dependency chain) or
  `hold_authoring`. Filing the chain authors **each link sequentially inside that same
  conversation** — the lead drives the coder + gate + GPT review through `drive_coder` /
  `submit_for_review`, diagnosing each verdict, and each link's `AuthoringSession` lifecycle is
  owned by `run_cycle`. The strategy never calls `propose_and_pr` (that auto-loop is kept for tests
  / non-lead callers); the LLM never issues git — `build_authoring_session`/`approve_edit` do
  (§9.9). **A cycle that authored ends before judging** (`outcome.authored_chain`): a committed tool
  is evidence the current reports predate, so `_resume_after_authoring` reruns the workers and the
  lead judges the fresh set in the **next** cycle. **A8:**
  tools are authored **sequentially, stopping at the first non-commit** — each committed tool sees
  the prior tool's committed code (direct-branch mode: `worktree == toolkit_root` on the `ai`
  branch) — then the workers rerun **once** on what committed (a *resume*), not a
  reload/restart/rerun per tool. `tool_author_cap` bounds **total tools** across the chain (not
  counted against `max_rounds`). `hold_authoring` advances the same cycle to its judging stage (a
  mis-called tool, not a failure). **A11 — recover, never choke on one failed
  session:** because authoring stops at the first failure, everything committed is a complete,
  dependency-satisfied prefix, so `registry.reload()` + per-worker `sandbox.restart()` fire whenever
  **≥1 tool committed** (was: only after the *whole* chain committed — the bug that left committed
  tools out of the reran workers' manifest, #21). A **non-committed** chain link (gate/review cap
  exhausted, or a human block) is an honest failure (§9.6) that **stops the chain at that link** and
  records a task note (so the reran workers refine their request and the lead's next cycle adapts),
  then: with ≥1 committed the workers rerun on the committed prefix; with **nothing** committed but
  credits remaining the workers still rerun on the failure note so the lead re-attempts (burning a
  `tool_author_cap` credit) — the run recovers instead of failing on the first miss. **Credits spent
  with nothing ever committed skips the rerun** (`exhausted`): there is no capability to rerun on
  and rerunning would only re-surface the same gap at the cost of a full worker round, so the
  existing reports — which carry the failure notes — are judged in the next cycle, where the lead
  files `submit_terminal_report` (§9.6). That report flows to the reviewer (sole I/O) which
  adjudicates finalize vs. bounded rework; never fabricated. A cycle that files **no** form and
  wrote **no** report raises rather than composing one ([F6]).
- Worker objects for IO debate (`IOAbstractionWorkerLike`) must provide
  `abstract_document(document, *, peer_abstracts=()) -> MemoryRecord`.
- Lead objects for IO debate (`IOAbstractionLeadLike`) must provide
  `judge_abstract_convergence(document, abstracts, *, round_index, max_rounds)
  -> ReviewVerdict` and `adjudicate_abstract(document, abstracts) -> MemoryRecord`.
- The round cap is hard, and it is enforced in **two** places that agree: consensus passes
  `rounds_left` into the cycle, and the `request_another_round` handler refuses the form once that
  reaches zero, naming the cap and pointing at `submit_adjudication`. So a lead at the cap files a
  report rather than asking for a round it cannot have.
- Live consensus verification is opt-in:
  `AUTO_RESEARCH_LIVE_CONSENSUS_TESTS=1 pytest tests/test_live_consensus.py -s`.
  It writes JSONL plus Markdown logs under `logs/dev/step-9/`.

## Dependencies
- Internal: `auto_research.agents`, `auto_research.schemas`, `auto_research.storage`.

## Dictionary-level reference
- `WorkerLike` — protocol for analyst workers used by `DebateToConverge`. Method:
  `analyze()`.
- `LeadLike` — protocol for analyst lead used by `DebateToConverge`. One method: `run_cycle()`,
  returning a `LeadCycleOutcome` naming the form the lead filed. What consensus keeps is the
  counting (round cap, authoring budget, reload/restart/rerun); what the lead owns is the choice.
- `ConsensusStrategy` — protocol for swappable analyst consensus policies. Method:
  `run()`.
- `DebateToConverge(max_rounds=3, monitor=None, registry=None, tool_retry_cap=3,
  tool_author_cap=3, toolkit_root=None, worktree_base=None, direct_branch=None,
  human_handler=None, colead=None)` — bounded analyst debate strategy. Each worker gathers
  evidence inside `AnalystWorkerAgent.analyze()`; the strategy injects a truncated shared
  summary from each worker's `last_tool_results` before later rounds and deduplicates figures
  onto the lead's final report so they flow to the reviewer. When `toolkit_root` is set it also
  runs the authoring/resume path (above). Method: `run(workers, lead, task)` → `AnalystReport`.
  Internals: `_initial_reports(workers, task)`; `_revision_round(workers, task, reports, round)`
  (one debate round, peers in view); `_resume_after_authoring(workers, task, outcome,
  reports_unchanged)` → `(reports, task)` — per-link provenance/failure notes, then
  `registry.reload()` + per-worker `sandbox.restart()` when ≥1 committed, then rerun the workers
  once; it **skips the rerun** when credits are spent with nothing committed (A11);
  `_augment_with_peer_tools(workers, task)`; module-level
  `_augment_with_authored_tool(task, tool_spec, result)` (committed-tool provenance note),
  `_augment_with_failed_tool(task, tool_spec, result)` (failed-attempt note so a retry adapts), and
  `_augment_with_probe(task, verdict)` — hands the lead's `request_another_round` dispatch text to
  every worker verbatim, which is what that form's description promises.
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
Compacted 2026-08-05: entries folded into themed ranges (detail in git history).

- 2026-05-26 -> 2026-06-16 — Consensus strategies built: `DebateToConverge` (cap-3 analyst debate,
  lead adjudication, cross-worker `last_tool_results` summaries, final figure propagation) and
  `IODebateToConverge` (PDF memory abstraction). Phase 2 step 7 then had `DebateToConverge` drive lead
  tool authoring/resume (`tool_author_cap`/`toolkit_root`/`worktree_base`/`direct_branch`/
  `human_handler`/`colead`, `LeadLike.decide_authoring`): a committed tool reloads the registry,
  restarts sandboxes and reruns workers, bounded by `tool_author_cap` (not `max_rounds`), with Phase-1
  behaviour unchanged when `toolkit_root` is unset.
- 2026-06-26 -> 2026-07-14 — **Honest finalize, lead-driven authoring, whole-chain planning, V-4.**
  A non-committed attempt returns a terminal report via `lead.finalize_authoring_outcome` instead of
  spinning in debate, and it flows to the reviewer (sole I/O) to adjudicate finalize vs. rework (§9.6).
  A1: the lead drives authoring itself (B-topology, §9.3) through `lead.drive_authoring` — its own
  ReAct loop over `drive_coder`/`submit_for_review`, diagnosing each verdict — bounded by
  `tool_author_submit_cap`. **[A8]:** the whole ordered `AuthoringDecision.tool_specs` chain is
  authored sequentially (each tool sees the prior's committed code), then reload/restart/rerun happens
  once. **V-4:** the lead owns figures (`_deduplicated` removed); `run()` calls
  `adjudicate(task, reports, sources=…)` and the lead accumulates a per-worker catalog and cites a
  subset, instead of force-carrying the last round's figures.
- 2026-07-22 — **[A11] recover, never choke on one failed authoring session.**
  `_maybe_author_and_resume` stops the chain at the first non-commit and fires
  `registry.reload()`/`sandbox.restart()`/worker-rerun whenever **≥1 tool committed** — previously only
  a whole committed chain triggered it, which hid already-committed tools from the reran workers and
  blocked #21's census. A non-commit records a task note (`_augment_with_failed_tool`) and loops back:
  ≥1 committed → rerun on the committed prefix; nothing committed but credits remain → re-attempt
  (burning a `tool_author_cap` credit). The honest §9.6 terminal fires **only** at credit exhaustion
  with nothing ever committed. New event `analyst.tool.chain_resume`.
- 2026-07-31 — **The pre-authoring stop check is gone.** `DebateToConverge` no longer polls
  `run_control.stop_requested` before deciding to author or between chain tools: a `/stop` cancels
  the run task, so there is no wind-down window in which the flag is set and the strategy still
  runs. `run_control` is still carried — the lead uses it to publish its live authoring session so
  an interrupt can reset the toolkit branch — and `_stop_requested` is removed. Tests:
  `tests/test_consensus_authoring.py` (the skip-authoring test rewritten to pin the absence).
- 2026-09-02 — **`LeadLike` shrank to one method: `run_cycle`.** The five lead actions consensus
  used to sequence are branches the lead now takes inside one ReAct loop by filing a form, so
  `run()` asks for a cycle and acts on whichever form came back. `_maybe_author_and_resume` is
  replaced by `_resume_after_authoring` (per-link notes, reload/restart when ≥1 committed, rerun
  once) and `_revision_round`; the authoring branch now **ends the cycle** — a committed tool is
  evidence the current reports predate, so the workers rerun and the lead judges a fresh set in the
  next cycle (A8), and authoring still does not consume debate rounds. Credits spent with nothing
  committed **skips** the rerun (nothing to load, and re-running only re-surfaces the same gap), so
  the honest §9.6 terminal is now the lead filing `submit_terminal_report` in the following cycle
  rather than a separate `finalize_authoring_outcome` conversation. The round cap is enforced twice
  and the two agree: consensus passes `rounds_left`, and the `request_another_round` handler refuses
  the form at zero with the cap named. New `_augment_with_probe` hands that form's dispatch text to
  every worker verbatim, which is what its description promises — a constraint stated in a tool
  description and unimplemented in code is worse than one never stated. A cycle that files no form
  and wrote no report raises rather than composing one ([F6]). The eleven behavioural tests in
  `tests/test_consensus_authoring.py` pass **unchanged in intent** against the new topology.

- **2026-09-02 (second) — the rerun follows the commit count, never the filed form.** A cycle
  whose lead filed `submit_terminal_report` from **inside** the authoring stage returned with
  `authored_chain` False, so `_resume_after_authoring` never ran: no `registry.reload()`, no
  `sandbox.restart()`, no worker rerun. Observed live (block 0 attempt 5): links 1-2 committed
  `ixpe_pcube_polarimetry` and `ixpe_pol_energy_fit`, link 3 exhausted its submissions, the lead
  stopped honestly, and the reviewer's send-back then had a worker call the freshly committed tool
  and receive `[exit 127] Tool not registered`. Two gated, co-lead-approved, committed tools that
  the analysis could not use. **§9.6 and [A11] met at a stage §9.6 was opened into, and §9.6 won by
  control flow rather than by decision.** `strategy.run` now branches on
  `outcome.authored_chain or outcome.committed >= 1`: an honest terminal filed from the authoring
  stage ends the **chain**, never the analysis, and its committed prefix still has to become
  callable before any report is final. The §9.6 terminal keeps its meaning where it is a real
  terminal — with nothing committed there is no new evidence to rerun on, and the honest report
  stands. **Termination:** each pass through the branch needs at least one commit and every commit
  spends an author credit, so `tool_author_cap` bounds it. `tests/test_consensus_authoring.py`
  gains `test_terminal_filed_mid_chain_still_reloads_restarts_and_reruns` (verified to fail
  against the old condition) and `test_terminal_with_nothing_committed_still_ends_the_analysis`.

- 2026-09-09 — **N11: `tool_author_cap` is per analysis, and used not to be.** The budget was a
  local initialised inside `run()`, and `run()` is re-entered on every reviewer send-back, so a
  send-back silently refilled it: the real ceiling was `max_iterations x tool_author_cap` (36
  against a documented 12), visible in slot 2's ledger as `author_budget: 11 -> 10 -> 10` across
  cycles that spent 1, 1 and 2 credits. Human ruling 2026-09-09: per-analysis, never refilled.
  Spent credits now live on the instance (the strategy is built once per run in `graph/builder.py`
  / `graph/runtime.py` and the node closes over it) in a small mutable `_AuthorCredits` holder,
  because `DebateToConverge` is a **frozen** dataclass and a plain int field raised
  `FrozenInstanceError` at the first spend. Never binding in the observed runs (4 attempts used),
  but the documented cap was wrong by 3x. Tests:
  `test_consensus_authoring.py::test_n11_author_credits_are_per_analysis_and_never_refilled_by_a_send_back`,
  `::test_n11_the_credit_holder_is_mutable_under_a_frozen_strategy`.

- 2026-09-09 — **F9: the chain outlives one failed link, and terminal status is agent-declared.**
  Two shapes of one defect. *(1)* `_author_chain` ended on the first non-committed link
  (`if result.status != "committed": break`), on the sound reasoning that a chain is ordered so
  later links depend on the committed prefix — and that made a single failure end the whole chain
  with the credits that would have paid for a retry still unspent. Slot 3 (2026-09-08) spent
  2h43m and ~$18 on six cycles refused before a coder session even opened; slot 4 queued ~8 more.
  Human ruling: the **lead** owns the stop decision, and spending the authoring budget
  discovering a capability cannot be built is an honest answer — burning all 12 credits is
  intended. The loop now continues and appends a user turn naming the failed link, its reason,
  and how to stop the chain (`hold_authoring` or a terminal), because "the lead decides" is only
  real over a reason the lead can read. *(2)* `AnalystReport.status` is
  `Literal["succeeded", "failed"]`, **declared by the agent**, defaulting to **`failed`** — a
  report nobody filled in has not succeeded at anything, and the harness must never infer success
  from silence (it previously defaulted to `ok`). `needs_human` is retired: headless, the
  condition every benchmark batch runs under, `ask_human` is not in a graph role's tool list, so
  the state was unreachable by the route it names, while [A20] shows the label being filed for
  something that was not a human block at all. **In scope by ruling, because a permanent cause now
  costs the whole budget and the lead can only avoid that over a legible reason:** N4 — the
  unclean-branch error names the offending paths (with `-uall`, since plain `--porcelain`
  collapses a new directory to its own name and slot 5's dirtying file is *permanently
  undetermined* for want of exactly this); and N8 — `_worker_safe_reason` strips operator
  remediation from the note reaching the analysts, which previously carried a §9.9 citation, an
  internal repo path and two destructive git commands into slot 4's worker prompt three times, in
  a role whose own prompt forbids it from touching git.
  **Not changed, and flagged:** the lead's `submit_terminal_report` still takes
  `failed`/`needs_human`. That is a different field (a cycle decision, not a worker's outcome) and
  the ruling named `AnalystReport`; retiring it there would move the frozen [A20] Tier-0 scoring
  rule, which is an instrument decision.
