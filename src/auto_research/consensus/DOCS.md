# `auto_research.consensus` — analyst-worker debate

## Purpose
Drives analyst workers to agreement through bounded debate. Behind a swappable
`ConsensusStrategy` interface so the policy can be replaced without touching
agent or graph code ([CLAUDE.md §11](../../../CLAUDE.md#11-consensus-debate-to-converge)).

## Architecture
Phase 1 ships two strategies — one for analyst debate, one for IO abstraction:

**Analyst debate** (`DebateToConverge`):
- Each worker produces an analysis.
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
  reports when not converged, and returns a lead-adjudicated final report.
  Optional `monitor` records worker reports, convergence verdicts, revisions,
  and lead adjudication.
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
  ReviewVerdict` and `adjudicate(task, reports) -> AnalystReport`.
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
  `judge_convergence()` and `adjudicate()`.
- `ConsensusStrategy` — protocol for swappable analyst consensus policies. Method:
  `run()`.
- `DebateToConverge(max_rounds=3, monitor=None, registry=None, tool_retry_cap=3)` —
  bounded analyst debate strategy.  When `registry` is supplied, runs tool
  execution after each round of `analyze()` calls via
  `AnalystWorkerAgent.execute_tools()`.  Per-worker results are appended to
  `memory_path`; a truncated shared summary is injected into task instructions;
  and `FigureRef`s from each worker's tool results are merged (deduped by path)
  onto the matching `AnalystReport.figures` so they flow through
  `lead.adjudicate()` → `lead.judge_convergence()` → final report → reviewer.
  Workers with an attached restartable sandbox are allowed through to
  `execute_tools()` even if the previous subprocess died; the worker/sandbox
  layer starts a clean replacement.
  Method: `run(workers, lead, task)` → `AnalystReport`.
  Internals: `_run_tools(workers, task)` → `(AnalysisTask, list[list[FigureRef]])`;
  `_merge_figures(reports, per_worker_figures)` → updated reports;
  `_deduplicated(figures)` → deduped by path.
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
- 2026-05-26/28 — Steps 9–12 built: `ConsensusStrategy`, `DebateToConverge` (cap-3 debate, lead adjudication, `RunMonitor` recording, `registry`/`tool_retry_cap` for in-loop tool execution via `_run_tools()`), `IODebateToConverge` (IO-team abstraction debate over `MemoryRecord`). `_run_tools()` calls `execute_tools()` per worker after each round, appends results to private `memory_path`, injects a shared summary into the next round. Workers with dead sandboxes are restarted before tool calls.
- 2026-06-03 — Figure propagation fix. `_run_tools()` now returns `(AnalysisTask, list[list[FigureRef]])`. New `_merge_figures()` merges per-worker tool figures onto `AnalystReport.figures` (deduped by path via `_deduplicated()`). `run()` applies this after each tool phase and copies all accumulated figures onto the final adjudicated report, so `ReviewerAgent.review(images=report.figures)` receives real `FigureRef`s.
- 2026-06-11 — **Tool execution moved into the worker's ReAct loop.** `DebateToConverge` no longer executes tools: each `worker.analyze()` now runs its own inner tool loop and returns a report already grounded in real results (with the loop's figures attached). `run()` calls `analyze()` only; `_run_tools`/`_merge_figures` removed and replaced by `_augment_with_peer_tools(workers, task)`, which builds the truncated cross-worker summary from each worker's `last_tool_results` and injects it into task instructions between rounds. The `registry`/`tool_retry_cap` constructor fields are accepted for call-site compatibility but unused (the worker owns its sandbox + loop). Outer caps (`debate_max_rounds`) and final figure dedup at adjudication are unchanged.
