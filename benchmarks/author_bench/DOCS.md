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
Compacted 2026-09-01: entries through 2026-08-27 folded into themed ranges (detail in git history).

- 2026-08-18 — **Instrument built (paper item 4), first batch aborted, re-run/merge machinery added.**
  `drive_authoring` chosen as the trial unit over the legacy `propose_and_pr`; the #2 brief recovered
  verbatim from its run ledger and frozen; base pinned to `fa99ef8`; per-trial clone, ledger and
  `ContextVar` cost meter for concurrency; no human handler, so a raise records as `needs_human`;
  preflight over environment, brief, base, validation products and the SDK CLI. The report separates
  terminal states from rounds and reports fabrication as a signal for hand-reading. Deterministic
  suite `tests/test_author_bench.py` (14 tests, no spend), including the proof that concurrent trials
  do not pool their spend.
  **First batch (K=15, concurrency 4) aborted at 48 minutes with 0 of 15 trials finished:** the OpenAI
  account hit a zero credit balance, every co-lead call returned `429 insufficient_quota`, and the
  transient-retry layer backed off through 42 s to 341 s waits rather than failing, so the two
  gate-passing trials were never reviewed. OpenAI had no key-failover path (rotation covered Anthropic
  only). Kept as `logs/dev/author-bench/20260818T200942Z/` under an `ABORTED.md` that forbids quoting
  it. It did measure authoring cost up to the gate, all four trials having reached it: **$23.73 /
  $38.42 / $38.56 / $26.70**, of which the coder is 88 per cent — so a complete trial is **$27 to
  $45**, against $13 to $30 projected from the Q21 authoring windows, which priced tools authored onto
  a branch already carrying their chain-mates. **K set to 10** for the relaunch.
  **Re-run and merge machinery, and the surface labelled.** `--only 5,6,7` re-runs named trials
  keeping their numbers; `--rescore DIR...` merges batches with a later directory's trial replacing an
  earlier one of the same number, so a contaminated trial is repaired rather than reported beside its
  replacement. Records stamp `human_channel: none (unattended)` and the report carries a "Measurement
  surface" section, after a surface diff against production found the trial lead missing exactly
  `ask_human` and `plan_and_debate` — the likely cause of trials grinding to the submit cap in a way
  attended runs do not. Prompts verified unmodified (`prompts/analyst_lead.md`, `prompts/coder.md`).
  Tests 14 → 20.

- 2026-08-20 -> 2026-08-23 — **The benchmark became the model sweep (paper item 7), committed trials
  kept their changesets, and the tests moved beside the instrument.** New `legs.py` roster: a leg is
  one (author member, reviewer arrangement) pair with its own K, concurrency ceiling and brand colour;
  `--legs` selects them, legs run sequentially with trials concurrent inside, each leg's records land
  under its own directory, and every record names its author and reviewer. The per-leg table appears
  only when more than one leg is present, and `--rescore` keys on `(leg, trial)` — every leg numbers
  from one, so keying on the number alone would let one leg's trial silently overwrite another's.
  **The single-leg report is byte-identical**, verified against the batch already in the paper, so its
  published numbers are untouched. Three findings from building it: **the DOCS was wrong** that tier
  and provider were already configuration — the author harness read the `coder` role directly with no
  injection point, so `build_authoring_session(coder=...)` had to be built; **a tier row cannot be an
  OpenAI author** with the reviewer held fixed at the OpenAI co-lead, which is why that author appears
  only as a correlated decorrelation row; and the live vendor probe **found the OpenAI primary key
  spent while its fallback answered**, exposing a real defect in the Codex author's key resolution
  (fixed in `sandbox/DOCS.md`, same date) and is why the probe now fails over rather than reporting
  the first refusal. Tests 20 → 34.
  **2026-08-22 — a committed trial now keeps its changeset.** `save_committed_patch` writes
  `committed.patch` into each committed trial's directory and the record names it. Found by asking
  whether the correlated arm's **five committed trees** could be re-reviewed: they could not. The
  clones were deleted (`keep_clones` off by default), the toolkit repository never received the
  commits, all five SHAs were absent from every surviving `/tmp` clone (each sits at the pinned base),
  and the ledger carries no diff — the gate records failing checks and a report hash, the review input
  a message count and a model. Reconstruction from the coder's own edits fails too: its tool inputs
  are capped at 4,000 characters and 8 of one trial's 41 write/edit payloads are stored truncated; one
  trial committed a file the ledger records no write to, and another wrote through six shell
  redirects. Tests 39 → 41 (the patch reapplies onto a fresh clone at the base; a committed trial's
  record names a patch that outlives its clone).
  **2026-08-23 — tests moved to `benchmarks/author_bench/tests/`**, the repository's `tests/` tree
  being the pipeline's own suite. The move exposed a repo-root of `parents[1]`, which had been
  resolving to the benchmark package itself and looking for `config/orchestration.yaml` inside it; it
  is now `parents[3]` with the depth named.

- 2026-08-24 -> 2026-08-25 — **The lead became a roster axis, the lead x co-lead grid was generated,
  and the batch learned to run unattended.** `Leg` gained `lead_provider`/`lead_model` and a
  `lead_correlated` property; `trial.py` gained `build_pinned_lead`, mirroring the co-lead builder so
  the measurement lives in the roster rather than machine-local config; each record carries `lead` and
  `lead_correlated_with_colead`. Five panel-(b) legs added, repeating every panel-(a) arm under a
  `gpt-5.6-sol` lead, plus the cell panel (a) cannot reach (author correlated with the reviewer, lead
  not). Tests 41 → 46. **Why:** panel (a) held the lead at `claude-opus-4-8` throughout, so its single
  zero-rejection arm is equally explained by the author sharing the reviewer's provider or by the lead
  doing so, and the author axis tracks nothing in that data while the lead axis tracks the whole
  effect. **Two roster gaps closed by the new tests:** the panel-(a) GLM rows carried the throttling
  finding in prose and set no ceiling, though the batch had voided every GLM trial above two in flight
  — both rows now cap at two; and the concurrency test asserted that only the Claude legs cap
  themselves, which the vendor ceilings make false, so it now states that a ceiling is a vendor's
  limit wherever one exists. **Not yet run:** 25 trials, roughly $700-900 at list prices with about
  $150 of that billed, since the Claude author legs draw on the subscription.
  **2026-08-25 — the lead x co-lead grid, generated rather than written out.** Twenty cells
  (`legs.GRID`), one per (lead, co-lead) pair, at a pinned `deepseek-v4-flash` author, **K=5**. The
  roster splits into `CURATED` (the hand-chosen author sweep, unchanged) and `GRID` (a product of two
  axis tables), because a factorial's cells have coordinates rather than rationales and twenty
  literals would have hidden a missing cell; `--legs grid` selects it through the new `GROUPS`.
  **Three things the grid needed, none generatable.** The pinned-parameter table carried Anthropic and
  OpenAI only, so a Z.AI or Qwen lead would have run at its vendor default against harmonised peers —
  the "compares configurations rather than models" error that table exists to prevent; all five
  vendors now have entries, **Qwen at high effort despite the measured latency**, because parity is
  the property the measurement rests on and latency is a cost rather than a confound.
  `build_pinned_lead`/`build_pinned_colead` hardcoded `vision=True`, which cannot express the
  text-only reviewer the grid depends on, so vision is declared per axis member and carried onto the
  built member. And preflight probed the *author's* vendor alone; a cell can pin two vendors the
  author never touches, so `probe_chat_member` now probes each distinct pinned member through
  `ModelHandle`, once per (provider, model) rather than per leg. **Two test corrections, both widening
  rather than weakening:** the vendor-ceiling test read `leg.provider`, which names the author seat
  only, so a cell pinning Z.AI as *lead* escaped its measured ceiling entirely — it now checks the
  chat seats too; and the lead-pinning invariant keyed on `purpose == "lead"`, which a grid leg is
  not, so it now names the set of purposes allowed to move the lead. Suite: 54 in this package, 1400
  green overall. Unrelated and pre-existing, recorded so it is not read as fallout: 11 `gate_bench`
  failures from planted mutants gone inert against a `research_toolkit` checkout that has advanced
  past the corpus's pinned base. **Not yet run:** 100 trials, order $600 at list prices from the
  measured $6.50 median, wall-clock the binding constraint at about 82 trial-hours.
  **Detach, status and resume (second entry, same day), built before the two-day batch launched.**
  Durability already held: `records.jsonl` is appended per trial under a lock, so a kill loses only
  what was in flight. `--detach` spawns the batch orphaned through `graph.detached._spawn_orphaned`
  (reused, not reinvented — the double fork survives the `/proc` link a VSCode logout walks), after
  preflight rather than before it. `--status DIR` reports per-leg progress, terminal-state counts,
  spend and pid liveness from the records rather than the report, which does not exist until the end.
  `--resume DIR` recomputes gaps **per leg** — the pre-existing `--only` is global across legs and
  would have re-run succeeded trials in a twenty-leg batch — and re-runs an `error` record while never
  re-running a measurement. Both read a new `batch.json` the batch writes about itself, so a resume
  cannot be handed the wrong flags. The end-of-run report renders from every record on disk, or a
  resumed batch would have reported only its second sitting. Ten tests; package suite 54 → 63.

- 2026-08-27 — **Leak: the trial's paths told every actor which experiment it was in; they are opaque
  now.** `mkdtemp(prefix=f"author_bench_{label}_")` and `direct_branch=f"author-bench-{label}"` put
  the campaign name and the leg slug into paths all three actors handle, and the leg slug names the
  lead's vendor *and* the co-lead's. Measured on the 2026-08-26 grid: **the lead typed such a path in
  2,641 tool calls and the co-lead in 1,923**, because `ReadOnlyToolProvider` renders its `roots` into
  the `read_file`/`list_dir` descriptions the model reads. A co-lead in the correlated arm could
  therefore read `lead_anthropic_colead_anthropic` — the assignment it exists to be blind to. Unlike
  the sibling leak found the same day in `gate_bench`, nothing here reveals an *answer* (nothing is
  planted), so authoring outcomes stand; what it contaminates is the arm comparison, which is the
  grid's whole purpose. **The 100-trial grid is therefore re-run rather than reported.**
  **The fix is to match production, not to compromise with it.** Production's lead roots are
  `[run_dir(run_id), papers_dir(), toolkit_root]` and a run id is a hex string, so no production actor
  has ever been able to learn anything about its run from a path. The trial now mints an opaque
  per-trial token, holds the clone *and the authoring worktrees* under it, names the branch
  `authoring-<token>`, and hands the lead that directory instead of the leg-named results tree.
  Because `make_review_hook` derives the co-lead's roots from the authoring worktree, one change
  blinds all three actors; records stay organised by leg, and no actor ever sees them. **One artifact
  needed rescuing:** §9.9 writes a discarded attempt's diff as `tool_author_failed_*.patch` under the
  authoring dir, which moved into the swept temp tree; those are now copied into the trial's record
  directory before cleanup, guarded so a diagnostic can never fail a trial. Three tests: the branch is
  opaque, no path an actor sees names the experiment, and the leg-named tree never reaches a prompt.
  Package suite 63 → 65.

- 2026-09-21 — **`plot_coder_sweep_cost.py` promoted out of `tmp_scripts/` into the package** as
  `plot_sweep_cost.py`, with the two `parents[1]` repo-root offsets corrected to `parents[2]` and
  the usage line changed to `python -m benchmarks.author_bench.plot_sweep_cost`. Behaviour is
  unchanged: regenerating writes the same 1541x984 panel with the same per-leg totals
  (deepseek $37.98, glm $80.98, claude-sonnet-5 $400.26, the same-provider Claude arm $132.27,
  gpt-5.6-terra $56.02). The figure backs a manuscript claim, so its generator belongs beside the
  benchmark that produced its ledgers rather than in a gitignored scratch directory;
  `papers/FANGZ/DOCS.md` §4 is updated to the new path.

- 2026-09-22 — **Role vocabulary in the two generated paper artifacts follows the manuscript.**
  `table_grid.py` now emits "LLM reviewer" where it emitted "co-lead" (caption and column group)
  and "coder" where it emitted "author", and `plot_sweep_cost.py` draws the Claude arm at its full
  $K=10$ rather than the first five trials, so its row agrees with the manuscript prose (7 of 10
  committed) instead of reporting 3 of 5 for the same configuration. The same-provider Claude arm
  (`claude_sonnet_5_correlated`) is no longer drawn: its reviewer differs from every other arm's,
  so it varied the reviewer as well as the coder, and reviewer coupling is measured on the
  lead x reviewer grid instead. Numbers are untouched in both: regenerating the grid changes only
  the four renamed strings, and the sweep figure's Claude total is $809.40, the US$809 the
  manuscript already quoted.

