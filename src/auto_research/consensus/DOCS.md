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
  authoring path) `decide_authoring(reports) -> AuthoringDecision`.
- Authoring/resume (Phase 2 step 7): when ≥1 worker report carries a `tool_request` and
  authoring is wired (`toolkit_root` set, with `direct_branch`), the strategy asks the lead
  to `decide_authoring`; on `should_author` it calls `merge_flow.propose_and_pr(direct_branch=…)`
  itself (the LLM never issues git — §9.9). A committed tool triggers `registry.reload()` +
  per-worker `sandbox.restart()` + a provenance-augmented task + a worker rerun (a *resume*,
  bounded by `tool_author_cap`, not counted against `max_rounds`); `should_author=false` and
  non-committed results fall through to ordinary debate (honest failure, no fabrication).
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
  `judge_convergence()`, `adjudicate()`, and `decide_authoring()`.
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
  `(resumed, reports, task, author_budget)`; `_augment_with_peer_tools(workers, task)`;
  module-level `_augment_with_authored_tool(task, tool_spec, result)`; `_deduplicated(figures)`.
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
