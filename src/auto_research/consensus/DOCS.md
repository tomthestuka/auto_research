# `auto_research.consensus` — analyst-worker debate

## Purpose
Drives analyst workers to agreement through bounded debate. Behind a swappable
`ConsensusStrategy` interface so the policy can be replaced without touching
agent or graph code ([CLAUDE.md §11](../../../CLAUDE.md#11-consensus-debate-to-converge)).

## Architecture
Phase 1 ships one strategy:
- Each worker produces an analysis.
- Each round, workers see peers' analyses (and figures, by `FigureRef`) and
  critique/revise.
- After each round, the **analyst lead** judges convergence and summarizes
  remaining disagreement.
- Stop on convergence **or** at a hard cap of **3 rounds**, after which the lead
  adjudicates/synthesizes a final report and flags residual uncertainty.

## Interfaces / Contracts
- `ConsensusStrategy` protocol:
  `async run(workers, lead, task) -> AnalystReport`.
- `DebateToConverge(max_rounds=3)` implements the Phase 1 policy. It requires at
  least one worker, asks all workers for initial reports, checks convergence
  through the lead after each round, requests revisions with peer reports when
  not converged, and returns a lead-adjudicated final report.
- Worker objects must provide
  `analyze(task, *, peer_reports=()) -> AnalystReport`.
- Lead objects must provide
  `judge_convergence(task, reports, *, round_index, max_rounds) ->
  ReviewVerdict` and `adjudicate(task, reports) -> AnalystReport`.
- The round cap is hard. If convergence is not accepted before the cap, the lead
  adjudicates the latest worker reports.
- Live consensus verification is opt-in:
  `AUTO_RESEARCH_LIVE_CONSENSUS_TESTS=1 pytest tests/test_live_consensus.py -s`.
  It writes JSONL plus Markdown logs under `logs/dev/step-9/`.

## Dependencies
- Internal: `auto_research.agents`, `auto_research.schemas`.

## Dictionary-level reference
- `WorkerLike` — protocol for analyst workers used by consensus. Method:
  `analyze()`.
- `LeadLike` — protocol for analyst lead used by consensus. Methods:
  `judge_convergence()` and `adjudicate()`.
- `ConsensusStrategy` — protocol for swappable consensus policies. Method:
  `run()`.
- `DebateToConverge(max_rounds: int = 3)` — bounded debate strategy. Method:
  `run(workers, lead, task)`.

## Changelog
- 2026-05-27 — Step 9 completed: added `ConsensusStrategy`,
  `DebateToConverge`, lead convergence checks, deterministic tests, and an
  opt-in live GX339-4 replication-planning debate test.
- 2026-05-26 — Folder seeded during step 0.
