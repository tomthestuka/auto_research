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
*To be populated in step 9.* Planned:
- `class ConsensusStrategy(Protocol)` with `async run(workers, lead, task) -> AnalystReport`.
- `class DebateToConverge(ConsensusStrategy)` — the Phase 1 implementation.

## Dependencies
- Internal: `auto_research.agents`, `auto_research.schemas`.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.
