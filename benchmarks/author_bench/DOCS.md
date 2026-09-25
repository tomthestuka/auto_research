# DOCS — `benchmarks/author_bench`

## 1. Purpose

Measure how reliably the system authors a tool. Section 3 of `papers/FANGZ` documents one
authoring lifecycle in depth; one lifecycle is an anecdote. This benchmark re-runs a single fixed
brief K times through the **production** authoring loop and reports the distribution: what
fraction commits, how many gate and review rounds it takes, what it costs, and how the attempts
that do not commit end.

It fills `papers/FANGZ` §5.3 Table 6 (paper item 4). Since 2026-08-20 it also **is** the
cross-model-tier sweep (item 7): a **leg** is one (author member, reviewer arrangement) pair, and a
batch runs the roster. The earlier note here that tier and provider were "already configuration
rather than code" was wrong — the author harness resolved its member by reading the `coder` role
directly, with no injection point, which is the seam that had to be built
(`merge_flow.build_authoring_session(coder=...)`). The pressure ablation (item 8) still reuses this
driver.

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

### What survives a trial

The clone is deleted when a trial ends, so a committed changeset would otherwise exist only as a
commit SHA in a repository that no longer exists. Two facts make the ledger an inadequate fallback:
coder tool inputs are capped at 4,000 characters before they are recorded, so a file written in one
call is stored truncated, and the review payload records only a message count and a model name. A
committed trial therefore writes `committed.patch` beside its ledger, a one-commit `format-patch`
against the pinned base, which applies onto a fresh clone at that base with `git am`. That is what
lets a committed changeset be re-gated or re-reviewed later without re-authoring it, which a
reviewer-arrangement control needs and a rubber-stamp test is.

`--keep-clones` stores the same information as a whole toolkit copy per trial, so it stays an
inspection aid rather than the record.

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

### The sweep: what a leg varies, and what it may not

Two comparisons live in one roster and they cannot share rows.

**Tier rows** vary the author and hold the reviewer fixed. The reviewer must therefore be the same
member in every tier row, which is why **no tier row may be an OpenAI author**: the configured
co-lead is OpenAI, so such a row would force the reviewer to change and vary two things at once.

**Decorrelation rows** hold the author fixed and vary only the reviewer, which is the comparison
`papers/FANGZ` §6.2 asks for. The cross-provider arm is the production shape and is already measured
by the `claude_sonnet_5` row, so that row is declared `reuse` and reported without being re-run; the
new arm gives the same author a same-provider reviewer. `claude-opus-5` is the pinned reviewer
because the reviewer swap found it the strongest, so a cost to sharing the author's provider even
there is a strong result and no cost is an honest negative one. A pinned reviewer's reasoning effort
and output budget are **harmonised** with the configured co-lead's, since a member left at its own
default would compare configurations rather than models.

A leg changes exactly two things about the call: the author member, and (for a correlated row) the
switch that lets a same-provider reviewer run. The loop, the brief, the base, the caps, the gate and
the prompts are identical across legs, which is the only reason the rows are comparable.

**Legs run sequentially, trials inside a leg concurrently.** Sequential legs keep two vendors' rate
limits and two accounts' balances from interacting inside one measurement, and they make a mid-batch
stop leave whole legs finished rather than all of them partial. The **concurrency ceiling lives on the
roster row**, not in whoever types the command: the Claude legs cap at 2 because that author
authenticates from a subscription whose session cap is shared across concurrent coders, and four at
once exhausted it after about two hours and contaminated six of ten trials in the first batch.

**Every leg's vendor is probed live before any lead spend**, and the probe participates in key
failover. The pool only advances when a call fails, so at launch it still holds a spent primary; the
probe is the run's first real call, so a spent balance retires that key for the process and a rate
limit rotates past it, leaving the pool on the key that answered. Without this a dead key is
discovered inside an author subprocess, one trial at a time, after each trial's lead spend is gone —
and a harness-only vendor's key never passes through chat-provider rotation at all. The gate
benchmark learned the general lesson the expensive way, losing 26 runs to a missing key found one
failed call at a time.

**Qwen has no *author* row, and the reason changed.** `legs.QWEN_ABSENT` records it: through
2026-08-19 the account was not entitled (`AccessDenied.Unpurchased` on every model and host, a
service-activation gate rather than our wiring), which was cleared on 2026-08-24. Qwen is live and
carries a lead and a co-lead axis in the grid below; what still keeps it off the author axis is
that `dashscope`+`codex` sits in `UNVERIFIED_PAIRINGS`, never having run. A reader has to be able
to tell a skipped vendor from an unconsidered one.

### 2.1 The lead x co-lead grid

A second experiment beside the curated roster, and **generated rather than written out**. The
curated rows are chosen one at a time and each carries prose saying why. The grid is a *product* --
every eligible lead crossed with every eligible co-lead at one pinned author -- so a cell's
identity is its coordinates and there is no per-cell rationale to write. Twenty hand-authored
literals would hide the design in repetition and make a missing cell invisible, which is what
`legs._grid_legs()` and `test_the_grid_is_the_full_product_of_its_two_axes` exist to prevent.

**What it measures.** CLAUDE.md §9.3 binds `reviewer.provider != author.provider` and says nothing
about the lead. `papers/FANGZ/DOCS.md` §8.5 found the coder sweep cannot separate those, because
the lead was Anthropic in all five arms, so the reviewer's relation to the author and to the lead
moved together. Holding the author fixed and crossing the other two separates them, and the result
decides whether §9.3 binds the right actor.

**Axes.** Leads are `claude-opus-5`, `gpt-5.6-sol`, `glm-5.3`, `qwen3.8-max`; co-leads are those
four plus `deepseek-v4-pro`. Author pinned at `deepseek-v4-flash` on the Codex harness, K=5, twenty
cells, 100 trials. The axes are unequal because vision binds the roles that are sent figures: the
lead is handed the accumulated per-worker catalog and the co-lead is handed none, so a text-only
vendor may review but not lead. Google is out of both, its second tool call in any loop being a
hard 400 ([I8]).

**Why Pro reviews rather than Flash.** It is the stronger reviewer on the axis a reviewer uses
(independent testing 77 against 60, with Flash's win confined to agentic tool use), and it keeps
the author-correlated column same-provider-different-model, matching the one author-correlated arm
already measured. A Flash co-lead would make that column same-*model* and incomparable to it.
Live-probed 2026-08-25: nine of nine real schemas with tools declared, so Pro honours every
DeepSeek descriptor flag previously measured on Flash alone.

**The shape's virtue and its cost.** Because the author is DeepSeek and DeepSeek cannot lead, the
two correlations are fully crossed and never coincide -- four lead-correlated cells, four
author-correlated, twelve clean, none both -- so each is estimable free of the other. The cost is
that the doubly-correlated cell is unreachable, so no interaction term is estimable, which the
manuscript states rather than leaves to a reader.

**Selection.** The grid stays out of a bare batch (`in_default=False`) and is named as a group:
`--legs grid`. Groups exist so a factorial is selected by what it is rather than by pasting twenty
slugs, which is the other way a cell goes missing.

### 2.2 Running unattended for two days

The grid is 100 trials at roughly 82 trial-hours, so it outlives any shell it is launched from and
the operator is not watching when it breaks. Three properties make that survivable, and only the
first predates the grid.

**Durability was already there.** `records.jsonl` is appended per trial, under a lock, the moment
that trial finishes. A kill at hour 40 loses only the trials actually in flight; everything
measured is on disk. Each trial also keeps its own `monitor.jsonl`, which is where an agent's own
words are read when a leg goes wrong.

**`--detach` launches it orphaned.** A double fork through `graph.detached._spawn_orphaned`, not a
local `nohup`, because `start_new_session` alone leaves the parent-to-child `/proc` link that a
graceful VSCode or Jupyter teardown walks when it kills process trees -- which killed a live
detached run once already (2026-07-16). Detaching happens *after* preflight, so a batch that would
have failed its checks says so on the terminal that launched it instead of burying the reason in a
log nobody reads for two days.

**`--status DIR` is the visibility, `--resume DIR` the recovery.** Both read `batch.json`, which
the batch writes about itself at launch, so neither needs the original flags retyped -- a resume
handed the wrong `--legs` at 3am is how a grid loses a day. Status reads the *records* rather than
the report, because the report is written once at the end and would be absent for the entire run;
it reports per-leg progress, terminal-state counts, spend, and whether the recorded pid is still
alive, which is the failure that otherwise hides (a batch dead for hours whose directory still
looks busy). Resume recomputes the gaps **per leg**: a twenty-leg batch dies with different gaps in
each, and the pre-existing global `--only` cannot express that, since it would re-run trial 3 of
every leg named including those whose trial 3 succeeded. A trial recorded `error` is re-run,
because an error is an infrastructure failure and `score.py` partitions trials by terminal state,
where `error` is not one; a committed or honestly-failed trial is a measurement and is never
re-run, which is what stops a resume quietly changing the batch it repairs. The final report is
rendered from every record on disk rather than from the resuming process's own list, so a resumed
batch reports both sittings instead of losing the first.

## 3. Interfaces

| Symbol | Meaning |
|---|---|
| `brief.load_brief() -> ToolSpec` | The frozen brief, validated. |
| `brief.brief_provenance() -> dict` | The run, ledger record and lead model it was recovered from. |
| `brief.BASE_SHA` | Toolkit state each trial authors from (`fa99ef8`). |
| `brief.VALIDATION_OBSIDS` | The brief's six real validation observations, checked in preflight. |
| `trial.TrialConfig` | Toolkit root, output directory, stamps, clone retention, chatter, and the `leg` being run (`None` = the configured author). |
| `trial.build_pinned_lead(leg, monitor)` | The analyst lead a leg pins, built outside the configured role and harmonised with it. |
| `trial.build_pinned_colead(leg, monitor)` | The co-lead a leg pins, likewise. Vision is declared by the roster, so a text-only reviewer is expressible. |
| `legs.Leg` | One roster row: author (provider, harness, model, key var, endpoint), reviewer arrangement, purpose, brand colour, per-leg K and concurrency ceiling. |
| `legs.ROSTER` / `legs.BY_SLUG` | The full roster (`CURATED + GRID`) and its index. |
| `legs.CURATED` / `legs.GRID` | The hand-chosen author sweep, and the generated lead x co-lead product. |
| `legs.GridMember` | One coordinate on a grid axis: vendor, model, declared vision, vendor concurrency ceiling. |
| `legs.GRID_LEADS` / `legs.GRID_COLEADS` / `legs.GRID_AUTHOR` | The two axes and the author held fixed across all twenty cells. |
| `legs.GROUPS` | Named selections `--legs` accepts beside individual slugs (`grid`, `curated`). |
| `plot_sweep_cost.collect()` / `.plot(corpus, out)` | Cumulative cost against elapsed time for the reported sweep trials, one line per trial. `LEGS` names the arms drawn and their line style; `K` is the trials-per-leg taken by number. |
| `legs.resolve(slugs) -> list[Leg]` | Every runnable leg, or exactly the ones named; a name may be a leg slug or a group; `reuse` rows are skipped unless named. |
| `legs.BRAND_COLOURS` / `legs.QWEN_ABSENT` | Figure colours by vendor; the recorded reason the Qwen row is absent rather than substituted. |
| `score.leg_of(record)` / `score.by_leg(records)` | Which row a record came from, and the grouping the multi-leg report renders. |
| `trial.collect_stamps(root, ref) -> dict` | Orchestration SHA and dirtiness, toolkit base, `code_gate.yaml` hash, brief provenance. |
| `trial.prepare_clone(dest, root, sha, branch) -> Path` | A throwaway clone at the pinned base on the trial's own branch. |
| `trial.summarise_ledger(path) -> dict` | Per-round counts and per-role cost, read from one trial's ledger. |
| `trial.run_trial(index, cfg) -> dict` | One authoring lifecycle; returns its JSONL record. Never raises. |
| `score.summarise(records) -> dict` | Terminal-state partition, totals. |
| `score.build_report(records, stamps) -> str` | The Markdown report and the source of Table 6. |
| `__main__.preflight(root, ref, legs) -> list[str]` | Every check decidable without a model call, plus one live vendor probe per leg and one per distinct pinned chat member. |
| `__main__.probe_chat_member(provider, model)` | Liveness of a vendor a leg pins as lead or co-lead, through `ModelHandle` so key rotation applies. |
| `__main__.batch_status(dir) -> str` | A batch's progress and liveness, read from its records. Costs nothing, never touches the run. |
| `__main__.missing_indices(dir, plan) -> dict` | The (leg, trial) pairs a resume must run: never recorded, or recorded as an error. |
| `__main__._detach(args, out_dir)` | Relaunch the batch orphaned via `graph.detached._spawn_orphaned`, recording `batch.pid`. |
| `BATCH_PLAN` / `BATCH_PID` / `BATCH_LOG` | `batch.json`, `batch.pid`, `batch.log` — what a batch writes about itself so `--status`/`--resume` need only the directory. |
| `__main__.probe_leg_vendor(leg) -> str \| None` | One trivial call to a leg's vendor; participates in key failover. |

### CLI

```
python -m benchmarks.author_bench                                  # every runnable leg, each at its own K
python -m benchmarks.author_bench --legs deepseek_v4_flash --k 3   # one leg, three trials
python -m benchmarks.author_bench --check                          # preflight + vendor probes, no lead spend
python -m benchmarks.author_bench --rescore DIR...                 # re-render/merge finished batches
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
- **`save_committed_patch`** — writes a committed trial's `committed.patch` (one-commit
  `format-patch` against the pinned base) into the trial directory and returns its path; a failure
  to produce it is written into the file rather than raised, so it can never fail a measured trial.
  The record carries the path as `patch`.
- **`summarise_ledger`** — tolerant of a truncated final line, so a batch that is still running
  can be read live.
- **`plot_sweep_cost`** — the generator for the manuscript's sweep-cost figure
  (`papers/FANGZ/figures/fig_coder_sweep_cost.png`), promoted here from `tmp_scripts/` on
  2026-09-21 so a published figure is regenerable from the package that produced its data. Reads
  each reported trial's `monitor.jsonl` through `timeline.load_corpus` and colours by vendor via
  `legs.BRAND_COLOURS`; colour carries the vendor and shape carries the event, so neither
  encoding depends on the other. Run as `python -m benchmarks.author_bench.plot_sweep_cost
  [out.png]`, defaulting to the manuscript's figure path.

- **`stress` (in `tmp_scripts/author_bench_heartbeat.py`)** — cap proximity plus rejection
  density, reported at every heartbeat while a batch runs, because pressure is the leading
  indicator of fabrication.

## 5b. Provider coupling has two axes, and the rule binds the one that shows no effect

The sweep was built to vary the author against a fixed reviewer, on the understanding that §9.3's
`reviewer.provider != author.provider` names the coupling that matters. Panel (a)'s own data says
otherwise, and the reason is that the **analyst lead was never varied**: it is `claude-opus-4-8`
in all five arms, so a change in the reviewer's provider moved the reviewer's relation to the
author and to the lead at the same time.

```
coder              co-lead         coder~rev   lead~rev   co-lead rejections
deepseek-v4-flash  gpt-5.6-sol        no          no             8
glm-5.3            gpt-5.6-sol        no          no            14
claude-sonnet-5    gpt-5.6-sol        no          no            11
gpt-5.6-terra      gpt-5.6-sol       YES          no            14
claude-sonnet-5    claude-opus-5     YES         YES             0
```

Read down the two correlation columns. Sharing the **author's** provider with the reviewer tracks
nothing: a GPT author under the GPT co-lead is author-correlated and drew the joint-highest count.
Every arm with an Anthropic lead and an OpenAI reviewer drew 8 to 14 whoever wrote the code. The
single zero-rejection arm is the single arm whose reviewer shares the **lead's** vendor.

**Why the lead is the more plausible locus.** The coder writes text; the lead writes the
instructions the coder executes, reads the result, decides when to submit, and argues the verdict.
Submission is the lead's act, so the lead determines what a reviewer is asked to judge and when.
In the zero-rejection arm a `claude-opus-5` reviewer is passing on a submission decision made by
`claude-opus-4-8`.

**What this does not establish.** The arms confound the two axes perfectly, so the table above is
suggestive and not decisive: no arm exists in which the author shares the reviewer's provider while
the lead does not. Two other readings survive it. The arms' code may genuinely have differed, which
is partly circular because an approved first submission ends a trial cheap and fast, and that arm's
median cost and wall clock are a third of its twin's. And the seeded-defect corpus rules out the
simplest explanations for either reviewer: over the 54 authored-style changesets of
`benchmarks/gate_bench`, `claude-opus-5` caught 26 of 26 planted defects with 0 of 28 false
rejections while raising **3.15 blocking issues per rejecting review** against `gpt-5.6-sol`'s
1.27. On that evidence the same-provider reviewer is the stricter of the two per call, not the more
permissive, so "Opus rubber-stamps" and "sol is too harsh" both fail as intrinsic accounts.

**The decision (2026-08-24): panel (b) varies the lead.** Every panel-(a) arm is repeated with the
lead pinned to `gpt-5.6-sol` and nothing else changed, which crosses the two axes at a fixed
author and makes them separable. The prediction is a mirror image: under a GPT lead the
GPT-reviewed arms fall toward zero and the `claude-opus-5`-reviewed arm rises into the 8 to 14
band. The arm that makes this work is `claude_sonnet_5_gptlead_anthropic_review`, the cell panel
(a) cannot reach, where the author shares the reviewer's vendor and the lead does not.

Rejected alternative: holding the author at a non-Anthropic vendor and varying only the reviewer.
It isolates the lead axis but tests it indirectly, and it does not produce a panel that reads as a
sibling of the one already published.

**Consequence for §9.3, recorded because it outlives this measurement.** The rule binds the author
and is silent on the lead. A configuration with an Anthropic co-lead correlates the reviewer with
the author *and* with the lead while the decorrelation preflight passes, because the check reads
the author's vendor alone. `Leg.lead_correlated` now reports the second axis alongside
`Leg.correlated` so a row cannot hide it. Whether the rule should bind the lead is a question for
the measurement, not for this DOCS.

## 6. Changelog
Compacted 2026-09-01 and again 2026-09-24 (detail in git history).

- 2026-08-18 — Instrument built (paper item 4): `drive_authoring` trial unit, the frozen #2 brief,
  base `fa99ef8`, per-trial clone/ledger/cost meter, unattended (`human_channel: none`, a raise
  records `needs_human`); `--only` re-runs and `--rescore DIR...` merges batches.
- 2026-08-18 — First batch (K=15) aborted at 48 min on OpenAI `429 insufficient_quota`; kept as
  `logs/dev/author-bench/20260818T200942Z/` under `ABORTED.md`, not quotable. K set to 10.
- 2026-08-20 -> 2026-08-23 — Model sweep (paper item 7): `legs.py` roster, `--legs`, per-leg records,
  `--rescore` keyed on `(leg, trial)`; the single-leg report stays byte-identical to the published
  batch. Tests moved to `benchmarks/author_bench/tests/` (08-23).
- 2026-08-22 — `save_committed_patch` writes `committed.patch` per committed trial; the correlated
  arm's five earlier committed trees are unrecoverable.
- 2026-08-24 -> 2026-08-25 — Lead became a roster axis (`lead_provider`/`lead_model`,
  `build_pinned_lead`); five panel-(b) legs under a `gpt-5.6-sol` lead; GLM rows capped at 2 in flight.
- 2026-08-25 — Lead x co-lead grid `legs.GRID` (20 cells, pinned `deepseek-v4-flash` author, K=5),
  `CURATED`/`GROUPS`; pinned parameters for all five vendors (Qwen at high effort); per-axis vision;
  per-member preflight probe. `--detach`, `--status DIR`, `--resume DIR` (per-leg, `batch.json`).
- 2026-08-27 — Leak fixed: trial paths and branch named the campaign and the leg's vendors to all
  actors; now an opaque per-trial token. The 2026-08-26 100-trial grid is re-run, not reported.
- 2026-09-21 — `tmp_scripts/plot_coder_sweep_cost.py` promoted to `plot_sweep_cost.py` (backs a
  manuscript figure); output unchanged.
- 2026-09-22 — `table_grid.py`/`plot_sweep_cost.py` use manuscript role names ("LLM reviewer",
  "coder"); Claude arm drawn at full K=10 ($809.40); same-provider Claude arm no longer drawn.

