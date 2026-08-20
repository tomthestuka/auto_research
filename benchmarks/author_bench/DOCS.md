# DOCS — `benchmarks/author_bench`

## 1. Purpose

Measure how reliably the system authors a tool. Section 3 of `papers/FANGZ` documents one
authoring lifecycle in depth; one lifecycle is an anecdote. This benchmark re-runs a single fixed
brief K times through the **production** authoring loop and reports the distribution: what
fraction commits, how many gate and review rounds it takes, what it costs, and how the attempts
that do not commit end.

It fills `papers/FANGZ` §5.3 Table 6 (paper item 4), and it is the driver the pressure ablation
(item 8) and the cross-model-tier sweep (item 7) reuse, since coder tier and reviewer provider are
already configuration rather than code.

Development code, not runtime code: it lives outside `src/auto_research/`, nothing in the
pipeline imports it, and it never runs during a research run.

## 2. Architecture

```
trial i  →  private clone of research_toolkit @ pinned base
                ↓
            AnalystLeadAgent (Opus) + read-only tools rooted at the clone
                ↓
            lead.drive_authoring(FIXED BRIEF, direct_branch=author-bench-tNN)
                ↓
            drive_coder (Sonnet, Claude Agent SDK)  ⇄  ask_lead
                ↓
            submit_for_review → gate.run_gate → co-lead review (GPT) → approve_edit
                ↓
            MergeFlowResult + the trial's own ledger  →  one JSONL record
```

Four design commitments, each load-bearing:

**The production loop, never the legacy one.** The trial calls `AnalystLeadAgent.drive_authoring`
exactly as `consensus.strategy` calls it. The older `merge_flow.propose_and_pr` auto-loop is not
used: its rework caps are marked unused on the lead-driven path, so a distribution measured
through it would describe a path the runtime has left. What is measured is therefore the lead as
plan owner, its submit and debate caps, the real gate, and the real provider-decorrelated review.

**A real brief, recovered rather than written.** The instrument is the `ToolSpec` the Opus lead
produced in run `46ed6671` (analysis #2), frozen verbatim in
`brief_ixpe_pa_pd_stability.json`. Writing a brief for the benchmark would measure a brief tuned
to the benchmark. Two consequences are deliberate: the paper's §3 walkthrough becomes trial 0 of
the same distribution, and the brief keeps its real ambiguities. Its proposed checks name two
synthetic fixture ObsIDs, and the original authoring resolved that by writing controlled CSVs into
the test's own `tmp_path` instead. How each trial resolves it is part of the measurement, so the
brief is never edited to smooth it.

**The base is where the original authoring began.** Trials clone at `fa99ef8`, the parent of the
commit that landed this tool, so the capability is absent and everything it builds on is present.
Each trial gets its own clone on its own branch, which is what makes trials concurrent, keeps
them from seeing each other's commits, and leaves the live checkout untouched.

**No human handler, and this bounds the result.** A blocked coder or lead raises, and the trial
records `needs_human` as its terminal state rather than being answered on its behalf. The cost is
that the lead loses two tools an attended production run gives it: `ask_human` and the budget-free
`plan_and_debate` design channel, both gated on a handler being wired. Everything else is
identical, which `tests/test_author_bench.py` pins by diffing the two surfaces and asserting the
difference is exactly those two names.

The consequence is behavioural, not cosmetic. Facing a co-lead rejection, an attended lead can
spend a question; an unattended one can only spend another rework round, so it grinds toward the
submit cap. Observed 2026-08-18: three of six trials reached `submit_for_review` = 5 with zero
approvals, a pattern the project's attended runs do not show. **The honest-failure rate these
trials produce is therefore an unattended figure and an upper bound on the attended pipeline's**,
and the report says so in its own "Measurement surface" section rather than leaving it to the
write-up.

### Isolation of the measurement

The cost meter is a `ContextVar`, so each trial installs its own **inside its own task**: every
provider call, and the coder subprocess's own session spend, is booked against that trial alone.
Without this, K concurrent trials would each report the batch's running total and the cost
distribution would be meaningless. `tests/test_author_bench.py` pins the property.

Each trial also writes its own ledger, so per-round counts are read from the structured verdicts
the production flow emitted (`gate.result.passed`, `review.result.approved`) rather than by
wrapping the hooks or parsing a summary string.

### Terminal states against rounds

A **terminal state** is what a trial ended as, so those rows partition K exactly: committed,
honest failure, raised to human, harness error. A **round** is an event inside a trial, and one
trial can carry several. The paper's draft table currently mixes the two, listing "gate rejection,
recovered" beside "committed", which double-counts a trial that recovered and then committed. The
report therefore renders them as two tables, and the paper's table needs the same correction.

**Fabrication is reported as a signal, not a verdict.** A no-fabrication scan hit or a co-lead
rejection naming invented data is evidence to be read, not a count to be published. This mirrors
the gate benchmark's attribution roster: crediting a layer, or charging a coder, without reading
what was actually said inflates the number.

## 3. Interfaces

| Symbol | Meaning |
|---|---|
| `brief.load_brief() -> ToolSpec` | The frozen brief, validated. |
| `brief.brief_provenance() -> dict` | The run, ledger record and lead model it was recovered from. |
| `brief.BASE_SHA` | Toolkit state each trial authors from (`fa99ef8`). |
| `brief.VALIDATION_OBSIDS` | The brief's six real validation observations, checked in preflight. |
| `trial.TrialConfig` | Toolkit root, output directory, stamps, clone retention, chatter. |
| `trial.collect_stamps(root, ref) -> dict` | Orchestration SHA and dirtiness, toolkit base, `code_gate.yaml` hash, brief provenance. |
| `trial.prepare_clone(dest, root, sha, branch) -> Path` | A throwaway clone at the pinned base on the trial's own branch. |
| `trial.summarise_ledger(path) -> dict` | Per-round counts and per-role cost, read from one trial's ledger. |
| `trial.run_trial(index, cfg) -> dict` | One authoring lifecycle; returns its JSONL record. Never raises. |
| `score.summarise(records) -> dict` | Terminal-state partition, totals. |
| `score.build_report(records, stamps) -> str` | The Markdown report and the source of Table 6. |
| `__main__.preflight(root, ref) -> list[str]` | Every check decidable without a model call. |

### CLI

```
python -m benchmarks.author_bench --k 15 --concurrency 4   # a batch
python -m benchmarks.author_bench --check                  # preflight only, no spend
python -m benchmarks.author_bench --rescore DIR            # re-render a finished batch
```

Outputs land in `logs/dev/author-bench/<UTC>/`: `records.jsonl` (one line per trial),
`report.md`, `brief.json` (the exact brief used), and `trial-NN/monitor.jsonl` (each trial's full
ledger, the audit trail behind every count).

### Preflight

Every check that can be decided without a model call runs before the first trial spends anything:
the project environment, the frozen brief, the base ref, the six validation products on disk, and
the Claude Agent SDK's own CLI resolution. The gate benchmark lost 26 runs to a missing `.env`
discovered one failed call at a time; a batch of authoring trials is far more expensive to
discover that way. The CLI is resolved by asking the SDK, not by probing `PATH`, because recent
versions ship a bundled binary that `which` cannot see.

## 4. Dependencies

Imports `auto_research.agents` (the lead and its read-only tool provider),
`auto_research.sandbox.merge_flow` and `gate` and `review` indirectly through `drive_authoring`,
`auto_research.monitor`, `auto_research.observability.costs`, and `auto_research.schemas`. Needs
the configured `research_toolkit` checkout, the analyst conda environment with the science stack
(the coder and the gate run the tests they write), and the brief's validation products under the
observation database. Nothing depends on this package.

## 5. Dictionary-level reference

- **`BASE_SHA`** — `fa99ef8`, the parent of `a6e427c` ("Add IXPE PA/PD stability analysis tool to
  xspec_utils"). A trial begins where the original authoring began.
- **`VALIDATION_OBSIDS`** — the six IXPE observations the brief names, each carrying a
  `pcube_timeseg_<obsid>.csv` product; verified present in preflight.
- **`TrialConfig.keep_clones`** — leave a trial's clone on disk for inspection; off by default,
  since a batch of 15 clones of the toolkit is large.
- **`TrialConfig.chatter`** — per-trial monitor chatter. The ledger is always written at full
  fidelity; chatter governs only what is held in memory.
- **`run_trial`** — never raises: a broken trial is recorded with status `error` so one failure
  cannot end a batch. Records status, commit SHA and committed files, reason, cost, duration, and
  the ledger-derived round counts.
- **`summarise_ledger`** — tolerant of a truncated final line, so a batch that is still running
  can be read live.
- **`stress` (in `tmp_scripts/author_bench_heartbeat.py`)** — cap proximity plus rejection
  density, reported at every heartbeat while a batch runs, because pressure is the leading
  indicator of fabrication.

## 6. Changelog

- 2026-08-18 — Package created for paper item 4. `drive_authoring` chosen as the trial unit over
  the legacy `propose_and_pr`; the #2 brief recovered verbatim from its run ledger and frozen;
  base pinned to `fa99ef8`; per-trial clone, ledger and `ContextVar` cost meter for concurrency;
  no human handler, so a raise records as `needs_human`; preflight covering environment, brief,
  base, validation products and the SDK CLI. Report separates terminal states from rounds and
  reports fabrication as a signal for hand-reading. Deterministic suite `tests/test_author_bench.py`
  (14 tests, no spend), including the proof that concurrent trials do not pool their spend.
- 2026-08-18 — First batch launched (K=15, concurrency 4) and **aborted at 48 minutes with 0 of
  15 trials finished**: the OpenAI account hit a zero credit balance, every co-lead call returned
  `429 insufficient_quota`, and the transient-retry layer backed off through 42 s to 341 s waits
  rather than failing, so the two gate-passing trials were never reviewed. OpenAI has no
  key-failover path (rotation covers Anthropic only). Kept as
  `logs/dev/author-bench/20260818T200942Z/` under an `ABORTED.md` that forbids quoting it.
  It did measure the cost of authoring up to the gate, all four trials having reached it:
  **$23.73 / $38.42 / $38.56 / $26.70**, of which the coder is 88 per cent. A complete trial is
  therefore **$27 to $45**, against $13 to $30 projected from the Q21 authoring windows, which
  priced tools authored onto a branch that already carried their chain-mates. K set to 10 for the
  relaunch.

- 2026-08-18 — **Targeted re-runs, merged reports, and the surface labelled.** `--only 5,6,7`
  re-runs named trials keeping their numbers, and `--rescore DIR...` merges batches with a later
  directory's trial replacing an earlier one of the same number, so a contaminated trial is
  repaired rather than reported beside its replacement. Records now stamp
  `human_channel: none (unattended)` and the report carries a "Measurement surface" section, after
  a surface diff against production found the trial lead missing exactly `ask_human` and
  `plan_and_debate` — the likely cause of trials grinding to the submit cap in a way attended runs
  do not. Prompts verified unmodified (lead `prompts/analyst_lead.md`, coder `prompts/coder.md`).
  Tests 14 → 20.
