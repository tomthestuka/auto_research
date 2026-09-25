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

## Changelog
Compacted 2026-08-05 and again 2026-09-24 (every entry); detail in git history.

- 2026-05-26 -> 2026-06-16 — `DebateToConverge` (cap-3 debate, lead adjudication, cross-worker
  tool summaries, figure propagation) and `IODebateToConverge` built; step 7 added lead authoring/
  resume: a commit reloads the registry, restarts sandboxes, reruns workers (`tool_author_cap`).
- 2026-06-26 -> 2026-07-14 — A non-commit returns an honest terminal to the reviewer (§9.6); A1: the
  lead drives authoring itself (`drive_authoring`); [A8] the whole `tool_specs` chain is authored,
  then one rerun; V-4: the lead cites figures from a per-worker catalog (`_deduplicated` removed).
- 2026-07-22 — [A11] reload/restart/rerun whenever ≥1 tool committed; a non-commit loops back
  (`_augment_with_failed_tool`); §9.6 terminal only at credit exhaustion; `analyst.tool.chain_resume`.
- 2026-07-31 — Pre-authoring stop poll removed (`_stop_requested` deleted: `/stop` cancels the task);
  `run_control` still carried so the lead can publish its authoring session.
- 2026-09-02 — `LeadLike` shrank to `run_cycle`; `_maybe_author_and_resume` replaced by
  `_resume_after_authoring` + `_revision_round`; authoring ends the cycle; new `_augment_with_probe`;
  a cycle with no form and no report raises ([F6]).
- 2026-09-02 — The rerun follows `authored_chain or committed >= 1`, so a terminal filed mid-chain
  still reloads, restarts and reruns; bounded by `tool_author_cap`.
- 2026-09-09 — N11 (ruling): author credits are per analysis, never refilled by a send-back, held in
  a mutable `_AuthorCredits` on the frozen strategy.
- 2026-09-09 — F9 (ruling): the chain continues past a failed link, telling the lead why;
  `AnalystReport.status` is agent-declared `succeeded|failed`, default `failed`, `needs_human`
  retired. N4: unclean-branch error names paths; N8: `_worker_safe_reason` strips remediation.
- 2026-09-12 — A8: `run` branches on the report's `rework`, not a filed form; `analyst.lead.cycle`
  records `rework`/`status`. Ruling 18: commit and rework stay separate; a probe rides along with a
  commit, charged to an author credit.
- 2026-09-24 — `IODebateToConverge` and its `IOAbstraction*` protocols deleted (the librarian is
  one loop).
