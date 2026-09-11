# DOCS — `benchmarks/science_bench`

## 1. Purpose

Measure the **science half** of the pipeline. `benchmarks/author_bench` measures the coding half —
how reliably the system authors a tool — and `benchmarks/gate_bench` measures the gate that admits
one. Neither says anything about whether the assembled team, given a real research question,
arrives at the right number. This does, and the two are presented beside each other in
`papers/FANGZ` (item 7, reframed).

It is measurable at all because the question has a **published answer**: the independent
reproduction of the *IXPE* Cyg X-1 May-2022 hard-state polarization result of Krawczynski et al.
2022, from the same Level-2 data. Fourteen quantities are compared against the paper, so
performance is read directly rather than through a proxy.

The question also carries a trap planted by nature, and that is what makes it a real test rather
than a lookup. The correct answer is *not* "everything agrees": every polarization-degree quantity
sits 1.3–2.3 sigma below the published value while every polarization angle clusters at zero — a
coherent systematic offset, not a fluctuation. A tree that reports fourteen-quantity agreement has
failed. Detecting the pattern, declining to call it statistical, and attributing it is the
performance being measured, and it cannot be passed by luck.

Development code, not runtime code: it lives outside `src/auto_research/`, nothing in the pipeline
imports it, and it never runs during a research run.

**What is actually run is the demonstration** (ruling 2026-09-08): the incumbent seat assignment
three times and one contrast assignment three times, five of those six runs new. The eleven-slot
screening array is kept as the record of what was designed, selectable with `--block1`, and is not
run. The reason is in [What is varied](#what-is-varied-and-what-is-held-fixed): the claim this
measures is that a tree of weaker models still reaches a verifiable result or declines cleanly,
which is an existence claim, and an existence claim is demonstrated rather than estimated.

**Built:** the measurement design, the per-trial environment, the frozen question, the trial driver,
the CLI with its preflight, scheduler and leak scanner, and the whole scoring pass — the
ledger-derived tier, the frozen answer key, the deterministic fourteen-quantity extractor, the
scorer and the report with its two figures. Block 0 has run and is the anchor's first trial.

## 2. Architecture

### The structural difference from `author_bench`, and why this module exists

In `author_bench` a trial is **one lead call**, so its seats are objects the trial constructs and
injects. Here a trial is a **whole research run**, and the run resolves its own roles out of the
model configuration with no injection point — so a seat assignment is a *configuration*, not an
argument, and it has to reach the run through the environment the run is launched into rather than
through a parameter.

```
author_bench trial            science_bench trial
──────────────────────        ─────────────────────────────────────
lead   = injected object      lead     ─┐
colead = injected object      reviewer  ├─ the run resolves these itself
coder  = injected member      workers  ─┘  from its model configuration
toolkit clone = parameter     toolkit / runtime dir / filesystem
                              allow-list / observation database
                                       ─── likewise, from its own configuration
```

This module is therefore **pure design**: levels, arms, the run array, the run-identity rule and
the vendor ceilings, as frozen data and pure functions. Nothing here launches anything or touches a
disk. What carries an arm into a real run is built on top of it.

### What is varied and what is held fixed

Three seats vary — the analyst **lead**, the **reviewer**, and the analyst **worker pair** — over
3 × 2 × 3 levels. Two are pinned in every cell: the tool **author** and the **tool reviewer**. The
coding half is already measured, so holding it fixed is precisely what isolates the science half.
Both pins also make the campaign affordable and its costs real: the pinned author settled at an
order of magnitude below the incumbent and bills to a key rather than a subscription, so every
cost figure the sweep reports is money somebody paid.

Authoring is **on**. The toolkit is pinned to its state before this question's own tool chain
exists, so each tree must author its way to the capability, through the gate and the tool review,
before it can do the science.

### Levels are not tier-matched, deliberately

External benchmarks are not a design variable. Independent evaluators disagree with one another at
matched effort, vendors grade themselves on private suites, and the top of every composite sits
inside its own noise floor. **The factor level is the model, named.** Published scores appear in
the write-up as caveated context and nowhere in the code.

Reasoning effort is pinned high in every seat, because the spread *within* a model across effort
settings dwarfs every gap *between* models — a member left at its own role default would compare
configurations rather than models. It is pinned at **high rather than at the top of the scale**
(ruling 2026-09-01): the top level has never been sent by this codebase, every measured path here
and in both published batches used high, and an effort string a vendor does not accept is not a
degraded run but a rejected call in every seat of every trial. A verified level shared by all four
vendors is what parity across the axis actually requires, and it keeps this sweep comparable to the
two batches beside which it is reported.

### What is run: the demonstration, six runs

**The ruling (2026-09-08).** The screening array below estimates main effects from nine arms at
one trial each with two degrees of freedom of pure error, for order $800-2,000 over two to three
days. Two things were wrong with that as an instrument. The published floor for a stable per-cell
estimate on agentic tasks is **8 to 16 trials** (arXiv 2512.06710, ICC convergence), so nine cells
at n=1 could not carry a seat effect however it was analysed. And the claim the sweep exists to
support is not a per-seat effect: it is that **a tree of visibly weaker models still reaches a
verifiable result, or declines cleanly**, because the deterministic gate, the cross-provider tool
review and the honest-failure floor carry the load rather than the models' judgment. That is an
existence claim, and the way to establish one is to exhibit it.

So: two seat assignments, three trials each. Three separates "always" from "sometimes" and is
honest about not being an estimate; the report states the floor it does not reach and refuses a
ranking.

| Seat | Anchor | Contrast |
|---|---|---|
| reviewer | `claude-fable-5` | `qwen3.8-max` |
| analyst lead | `claude-opus-5` | `gpt-5.6-terra` |
| analyst workers | `claude-sonnet-5` + `gpt-5.6-terra` | `claude-sonnet-5` + `qwen3.7-plus` |
| tool co-lead, coder, librarian, effort | pinned | pinned |

**The contrast is chosen on independently measured capability, never on price**, and per seat,
because the seats do not use the same ability. A reviewer needs scientific critique and figure
reading and never sustains a long agentic loop; a lead plans, writes the brief the author executes
and adjudicates across the longest loop in the pipeline; the workers plan and call tools. So the
lead seat takes the strongest mid model on the sustained-tool-use axis (`gpt-5.6-terra`: Artificial
Analysis index 42 against the frontier band's 47-53, Terminal-Bench 4.0 21.5% against 37-58%), the
reviewer takes the model that is a real step down on critique while staying a first-rate figure
reader (`qwen3.8-max`: index 40, CritPt 20 against `claude-fable-5`'s 30, but third on arena
vision and first on tau3 banking), and the worker pair drops its OpenAI member from index 42 to
`qwen3.7-plus` at 26. Every number is read off an independent board, and none of them is a design
variable: the factor level is the model, named. Boards disagree at matched effort — the same
`qwen3.8-max` is first on one and thirty-seventh on another — which is why the write-up carries
them as caveated context only.

Two structural facts to state rather than remove. The anchor's reviewer shares its lead's vendor
and the contrast's does not, a coupling no measurement covers for the science seats (the lead x
co-lead grid measured it null for *tool* review). And `claude-sonnet-5` stays in both worker pairs
because, with `claude-haiku-4-5` excluded as an infrequently-updated model, it is already the
lowest current Anthropic vision model.

Attempt 6 **is** the anchor's first trial, by construction rather than by bookkeeping: a run id is
a hash of the seats and the replicate index, so `demonstration()[0].run_id` resolves to the id that
run already has and a completed trial cannot be double-counted or silently re-run.

### The superseded design: three blocks, fourteen runs

**Block 0 — leak probe, one run.** Before anything is batched, one run executes with verbatim
recording of every actor's prompt, and that recording is read for the arm. This is the mandatory
pre-flight of the no-leaks rule, discharged as a *run* rather than a static dump: a dump shows the
seed messages, while only a live run shows the paths, identifiers, branch names, tool descriptions
and test-runner banners that exist once the thing is executing — which is where both of the
2026-08-27 leaks lived. A clean block 0 **is** block 1's first slot, a usable measurement rather
than a throwaway, and the remaining ten then go to the batch.

The condition on counting it: block 0's only difference from a batched run is recording that never
enters an actor's context. `BLOCK0` is an *alias* of that slot rather than a copy, so the two
cannot drift, and a test enumerates the fields on which it differs from an ordinary batched run of
the same arm. A leak fix that changes anything an actor sees makes the probe a different treatment,
and it is discarded instead.

**Block 1 — screening, eleven runs.** A hand-balanced fractional array over the 3 × 2 × 3 factors:
nine distinct arms, each lead and each worker pair appearing three times, every lead × worker-pair
combination exactly once, the reviewer splitting five/four. The anchor is replicated at slots 1, 10
and 11, and those replicates are the design's only pure error — two degrees of freedom. With six
parameters in a main-effects model that leaves four to five residual, which is thin and is reported
rather than hidden.

The array is written **literally** while the axes are written as data, and the distinction is the
one `author_bench` settled: a *product* is generated, because twenty hand-written cells would hide
the design in repetition and make a missing cell invisible; a *hand-balanced selection* is literal,
because the choice is the content. What keeps a literal array honest is that its balance is
**asserted by test** rather than eyeballed, so a careless edit fails instead of quietly
unbalancing the design.

**Block 2 — confirm, three runs.** The best level per seat, assembled from block 1's estimates and
run three times. Its arm is unknowable until block 1 reports, so it is a function rather than a
literal. It is confirmed separately rather than claimed from the screening estimates because the
assembly assumes the seats do not interact, and because the assembled combination may be one no
block-1 cell ran.

### Run identity is opaque, and the replicate index is load-bearing

A run id is a hash of the **seat models plus the replicate index**. Hashing the combination alone
collides in exactly the rows that matter: the anchor's three slots are the *same* arm — being
identical is the whole point of a noise floor — so they would share one id, one run directory and
one checkpoint, and so would block 2's three confirms.

It is a hash rather than a readable label because a label lands in every path an actor touches.
Production has never had this problem, by accident: a production run id is a hex digest, so no
actor can learn anything about its run from a path it is handed. An arm's readable slug exists for
the report and the figure and is asserted to appear in no identifier.

### Vendor ceilings, and why a single concurrency number cannot express them

Each level declares how many *concurrently running trials* may use its vendor in any seat. This is
a vendor property, not a role property: a throttled lead, reviewer or worker stalls a run exactly
as a throttled author does. One vendor has a measured ceiling of two — above that it throttled the
large majority of turns.

Two facts make the constraint sharper here than in `author_bench`, where legs run sequentially and
one leg is one vendor per role. Here runs are **concurrent** and one arm touches up to four vendors
at once: the pinned author and the pinned tool reviewer are in **every** run, and the capped vendor
appears in six of block 1's eleven slots. "Launch ten" therefore saturates that vendor three times
over, which is why the roster carries the ceilings and a pure admissibility function beside them,
designed and tested here and merely enforced by whatever launches runs.

Load is counted **per run**: an arm seating one vendor in several roles counts once against it. The
graph dispatches its roles in phases rather than all at once, so three same-vendor seats in one run
are rarely three simultaneous conversations, and counting them as three would idle the batch for
contention that does not happen. The imprecision runs the other way only when a run really does
have two same-vendor roles in flight, which the ceilings absorb by sitting below the measured
limit. The approximation is stated rather than modelled away.

### Capability is declared, never assumed

Every seat carries its own vision and document flags. The reviewer, the lead and the worker are all
sent figures, so a vision-less member in one of those seats must fail a **test**, not a trial. Two
eligibility facts are carried as data rather than prose: one vendor's flagship is text-only, so its
figure-seat level is the smaller vision-capable model instead; and one reviewer level has no native
PDF, so it declares so, since the reviewer is handed paper PDFs and that arm needs extraction glue
with a model-visible note before it can review.

Prices are carried only for the models the maintained pricing map does not cover, and a model whose
rate has not been read off its vendor's own page is **named as unread** rather than guessed. A
guessed rate is indistinguishable from a read one once it is in a cost table, and the sweep's cost
claim is that its figures are money somebody paid.

### The human channel is closed for every run

The batch is unattended, and it must stay comparable to the two published batches, which were too.
A human answering one trial and not another would inject an uncontrolled variable straight into the
seat comparison. The consequence belongs in the write-up: the honest-failure rate this measures is
explicitly an **unattended** figure, and an attended lead can spend a question where an unattended
one must spend a rework round or stop.

### A trial's private world

A trial is a whole research run, and a run resolves its own seats, paths and caps from the
configuration directory it was launched against. So an arm reaches a run as a *configuration*, not
as an argument, and the harness writes one per trial: four YAML files, a toolkit checkout at the
pinned base, an observation database holding only what the question needs, and an empty runtime
directory. Nothing is shared but the machine.

The seats, the four paths and the filesystem allow-list are what differ; every cap, flag and
timeout is inherited, because those are the pipeline being measured. Rewriting the allow-list
alongside the paths is the step that is easy to forget and fatal to omit: it names the old roots
literally, so a trial pointed at private paths whose allow-list still named the shared ones would
have every one of its own reads denied, and under the kernel jail its data would not even be
mounted.

**The observation database is copied, not linked, and filtered per file.** The master archive is
1.2 TB of irreplaceable data; a hardlink clone shares inodes with it, so an in-place write in a
trial reaches the original with no undo — and this question's own prescription tells the tree to
edit FITS headers in place and to decompress files. The copy is confined to Level 2, which is what
the question analyses.

The per-file filter is the load-bearing part. The May observation's event directory holds 1,023
files, six of which are the archive's own event lists; the rest are earlier runs' products,
**including the polarization cubes this question exists to compute**. A trial cloned by directory
would find the answer already on disk. Only files matching the archive's own naming are taken, and
copying nothing is a hard failure rather than an empty trial, since a filter that has drifted from
the layout would otherwise produce a run that fails as science. The catalog is then reconciled by
the toolkit's own recovery path against what was actually copied, so the clone is consistent by the
same rules the launch health check applies — measured: 1,023 + 321 files down to 6 + 6, catalog
recomputed to level 2, in about a minute and 4 GB.

### The question, frozen

Recovered verbatim from the run that posed it on 2026-07-10, so a trial is the same task that run
attempted and that run is a reference point rather than a different experiment. One step is
appended, and it names the task rather than the answer: the fourteen quantities and their units,
each asked for as a value with its error **and the artifact it came from** — which is what
separates a number this run measured from one transcribed out of the paper. It says explicitly that
a quantity this run did not produce must be reported as not measured with the reason, because the
alternative to an honest gap is an invented number.

The bytes are hashed and the hash is stamped into every batch. The answer key lives in the
manuscript's data directory, which is on no root any actor may read — not a secret, since the paper
is published and the tree is told to read it, but the values a trial is scored against should not
sit on a path the trial can reach.

### One trial

The driver spawns the production entry point — the same command the detached backend spawns —
pointed at the private configuration, and then waits, collects, and writes one record. It does not
call the public detached entry, because that derives the run directory from *its own* configuration
and the point of a trial is that its configuration is not the batch's; everything below that one
line is reused. Liveness is process existence, never log freshness: this question spends most of
its time in a silent reduction.

**Two outcomes, and conflating them would make the scoring meaningless.** *Infrastructure* is
whether the run got to speak: finished, crashed with a marker, stopped by a human, or vanished
writing nothing — only the last two are the harness's fault and only those are re-runnable.
*Measurement* is what the run concluded, and under the governing ruling every value of it is a
correct outcome: the pipeline always reaches a verdict, and a verdict of "this is not verifiable",
plainly stated, is a success. Everything derived is read from the run's own ledger, since the
production flow already writes structured verdicts. Cost needs no special handling — each trial is
its own process, so its spend is its own by construction.

A record is appended the moment its trial ends, so a batch killed at hour thirty loses only what
was in flight. The runtime directory is kept: it holds the ledger, the figures and the report, and
the whole scoring pass is offline over exactly those. The toolkit checkout and the data copy are
swept, being reproducible from the pinned base and the archive.

### Scheduling, and why a single concurrency number will not do

Every run calls the pinned author and the pinned tool reviewer, and six of block 1's eleven slots
carry the one vendor with a measured ceiling, so "launch ten" saturates it three times over. The
scheduler starts a trial only when the roster's ceilings admit it, and the rule itself lives in
`arms` beside the ceilings it reads, so it is designed and tested as data and merely enforced here.

### The leak scan, and what block 0 caught

Block 0 is one run of the anchor arm with the pipeline's own verbatim prompt recording switched on
— the single chokepoint every actor's prompt passes through, which exists precisely because a
hand-reassembled payload contains only the channels its author remembered. It is additive: it
changes what is written down, never what is sent, which is what lets a clean block 0 count as
block 1's first slot.

**Its first launch leaked, five minutes in, and that is the whole argument for running it.** The
workspace directory was called `science_bench`, so every downloaded-paper path in the librarian's
prompt read `/data/.../science_bench/<hash>/papers/...`. It gives away no arm — and that is not the
point: a model that knows it is inside a benchmark behaves differently in every seat, which is how
a reviewer once quoted a benchmark's own framing back in its rationale. The run was killed for the
price of a few paper downloads.

The fix is structural rather than editorial. A word list lives with the roster, and building a
trial world **refuses** a root that carries any of it, so no future workspace choice can leak past
it; the default workspace name is empty of meaning; the preflight checks it before spending; and
the guard test now exercises the real path construction, since the version that missed this had
monkeypatched away the very function that builds the paths.

**The scanner reads paths, not prose, and that distinction is what keeps it usable.** Its first
version ran the word list over whole records and reported forty-nine leaks over five records, of
which one was real — the others were an abstract saying *probe*, a fit called an *anchor*, and the
actor's own model name. A guard that cries wolf is one somebody switches off over the run that
matters. So the list is applied to the path-shaped substrings a record contains, and separately, as
an exact match, to the few strings that cannot occur in scientific prose. Coverage is reported
beside the findings, because a clean scan over a dump holding only the librarian says nothing about
the lead. The author is absent from the dump entirely — it is a subprocess speaking its harness's
own protocol — so its paths are read from the run's ledger instead.

### The channel that is measured rather than closed

The run directory is a read root for the lead and the reviewer, and it holds a console log whose
lines are prefixed `role(model)` for every role, plus a ledger carrying a `role/member` on every
cost line. Either one, read whole, hands an actor the whole arm.

Production makes that root readable deliberately -- a role investigating its own run is a feature,
and it is how the status reporter works at all -- so closing it would make this benchmark's
pipeline differ from the one it claims to measure, on the strength of a behaviour no run has been
observed to exhibit. The handling is therefore to **detect**: the scan reports any actor tool call
whose arguments name one of those files. A trial where nothing touched them is clean; a trial where
something did is a contaminated trial that is known rather than one that is not. If prevention is
later preferred, the mechanism exists already -- the filesystem guard denies specific basenames
inside an allowed root -- and it is a per-trial addition to that list.

This was found by an audit of the first live ledger, not by design review: the paths a trial is
*handed* had been checked, and the files sitting inside them had not.

## 3. Interfaces / Contracts

| Symbol | Meaning |
|---|---|
| `Seat` | One model in one role: vendor, model, declared vision and document capability, vendor ceiling, and why the level is on its axis. |
| `Seat.price()` / `Seat.parameters()` | Its per-million-token rate when the maintained map lacks one, and its pinned maximum-effort settings. |
| `WorkerPair` | The two analyst workers of one arm; `providers()` is what the decorrelation test reads. |
| `Arm` | One seat combination — lead, reviewer, worker pair — plus the two pinned seats. |
| `Arm.slug` | Readable identity, for the report and figure only. Never a path, a directory, a branch or an id. |
| `Arm.seats()` / `Arm.vendors()` / `Arm.identity()` | Every seat in a stable order; the distinct vendors one run calls; the seat models that make the arm the treatment it is. |
| `Arm.lead_correlated` | Whether the lead and the reviewer share a vendor. **Derived**, so a cell cannot claim a correlation it does not have. |
| `arm(lead, reviewer, workers) -> Arm` | Build an arm from three level slugs; an unknown level raises. |
| `LEADS` / `REVIEWERS` / `WORKER_PAIRS` | The three varied axes, each level carrying its own rationale. |
| `CODER` / `COLEAD` | The two pinned seats, identical in all fourteen runs. |
| `CODER_HARNESS` / `CODER_KEY_ENV` / `CODER_BASE_URL` | What drives the pinned author and where it authenticates. |
| `PINNED_EFFORT` | The effort level every Responses-dialect seat is pinned to (`high`). One constant, so a probe that verifies a higher level changes one line. |
| `PRICES_UNREAD` | Models on an axis whose rate has not been read from the vendor's own page. |
| `run_id(arm, replicate) -> str` | The opaque 32-hex identifier for one (arm, repetition) pair. |
| `RunSlot` | One scheduled run: its campaign position, arm, repetition, block, and whether it records verbatim prompts. |
| `BLOCK1` | The eleven-slot screening array. |
| `BLOCK0` | The leak probe — an alias of `BLOCK1[0]`, not a copy. |
| `BLOCK2_UNASSIGNED` | Why block 2 is a function rather than a literal. |
| `confirm_block(lead, reviewer, workers, k=3)` | Block 2 once its arm is known; repetition numbering continues past any block-1 replicates of the same arm. |
| `campaign(block2=()) -> tuple[RunSlot, ...]` | Every slot in run order. Block 0 appears once, as block 1's first slot. |
| `vendor_ceilings() -> dict[str, int]` | The tightest declared ceiling per vendor, read off the roster so no stale copy can exist. |
| `vendor_load(arms) -> dict[str, int]` | How many of the given concurrent runs touch each vendor, counted per run. |
| `admissible(running, candidate) -> bool` | Whether a candidate may start while those arms are in flight. |
| `schedule(slots, ceilings=None) -> list[list[RunSlot]]` | A planning aid that packs slots into waves respecting every ceiling; raises rather than looping if a single run cannot meet one. |
| `without_recording(slot) -> RunSlot` | The same slot as an ordinary batched run, which is what makes "block 0 counts" checkable. |
| `BRAND_COLOURS` | Vendor identity for the figures, shared with `author_bench` so the two sweeps read as one campaign. |
| `CAMPAIGN_WORDS` / `forbidden_in_path(path)` | What may never appear in a path an actor is handed, and everything in one that does. Campaign words matched as substrings (the leak was a directory name inside a longer path); seat identities as whole tokens (three letters of a vendor appear inside ordinary words). |

### `environment` — a trial's private world

| Symbol | Meaning |
|---|---|
| `TrialEnvironment` | Where one trial's configuration, toolkit, data and runtime directory live. |
| `TrialEnvironment.run_dir` | The run's own directory, derived from *this* trial's runtime root — which is how a batch watches a run it is not configured like. |
| `TrialEnvironment.process_env(base?)` | The caller's environment plus the configuration-directory variable. |
| `materialize(arm, run_id, *, workspace_root, base_ref, …)` | Build the world and return where everything landed. Refuses a root whose path carries anything an actor must not read. |
| `models_config(arm, source)` | The trial's `models.yaml`: the arm's five seats written, every other role copied. Pure. |
| `orchestration_config(source, *, toolkit_root, runtime_dir, papers_dir, obs_root)` | The live one with its four paths redirected **and its allow-list rewritten**. Pure. |
| `observations_config(obs_root)` | One line, pointing at the private copy. |
| `clone_toolkit(dest, source, base_ref, branch)` | A private checkout at the pinned base on the branch the runtime authors onto; returns the base SHA. |
| `copy_observation(source, dest)` | Copy one observation's archive files and only those; returns what was taken and what was left. |
| `build_observation_database(dest, source, obsids, mission, toolkit_root)` | The private database, catalog reconciled by the toolkit's own recovery path. |
| `teardown(environment, *, keep_artifacts=True)` | Reclaim the toolkit checkout and the data copy; keep the runtime directory the scoring pass reads. |
| `QUESTION_OBSIDS` / `QUESTION_MISSION` / `RAW_DIRECTORIES` / `RAW_FILES` / `RAW_EVENT_LIST` | The observations, and the archive-file allow-list that keeps earlier runs' answers out. |

### `request` — the frozen question

| Symbol | Meaning |
|---|---|
| `load_request()` | The question, context and constraints, as the pipeline's own request type. |
| `quantities()` | The fourteen quantities and units, in the order the question lists them, keyed to the answer key's own labels. |
| `request_provenance()` | Where it came from, what was added, and the run it is comparable to. |
| `request_sha256()` | The hash stamped into every batch. |

### `trial` — one run

| Symbol | Meaning |
|---|---|
| `TrialConfig` | Workspace, records directory, base ref, stamps, chatter, retention, poll interval. |
| `run_trial(slot, cfg) -> dict` | One whole research run; returns its record. Never raises. |
| `build_argv(run_id, chatter)` | The production command, with the human channel explicitly closed. |
| `launch(environment, *, chatter, prompt_dump=None)` | Spawn it orphaned into its own configuration; `prompt_dump` is block 0's verbatim recording. |
| `wait_for(run_dir, *, poll_seconds)` | Block until terminal; liveness is process existence, never log freshness. |
| `summarise_ledger(path)` | Rounds, tools committed, scan hits, spend by role, and how often the tree wanted a human. |
| `measured_outcome(result, ledger)` | What the run concluded — every value of which is a correct outcome. |
| `commit_reachability(toolkit_root, commit_shas)` | `(checked, unreachable)` — which ledger-recorded commit SHAs are gone from the trial's checkout, and whether the checkout could be read at all. |
| `collect_stamps(toolkit_source, base_ref)` | Code, base, gate configuration and question, so two batches can be told apart. |
| `append_record` / `load_records` | Durable per-trial append; tolerant read for a live batch. |
| `BASE_REF` | `5accf33` — the toolkit state before this question's own tool chain existed. |

### `extract` — the fourteen quantities, read by code

| Symbol | Meaning |
|---|---|
| `ANSWER_KEY` / `load_answer_key(path=None)` | The frozen key beside the module, returned with its own sha256 so a scored record names the key that produced it. |
| `extract_report(path, key=None) -> Extraction` | Read one report file. Any text format; `report.tex` is what the pipeline writes. |
| `extract_text(text, labels) -> dict[str, Extracted]` | The same over a string, with `same artifact` back-references resolved. |
| `Extracted` | One quantity as a report stated it: value, error, unit text, dual error, claimed reference value, source artifact, the window it was read from, or `not_reported`. |
| `Extraction` | All fourteen for one report, plus `labels_found`, `labels_measured` and `compliant`. |
| `find_report(run_dir)` | The report a trial wrote, machine handle first. |
| `normalise(text)` | LaTeX as plain text, only the escapes that occur inside the labels and their numbers. |

**No model is in this path.** The dispatched prescription names the fourteen quantities and asks
for each under a verbatim label so the report is machine-readable, identically in every cell, which
turns extraction into a keyed lookup. Attempt 6 complied and all fourteen of its values come back
exactly, hand-checked. A report that does not comply is not silently patched: `compliant` is
`False`, the missing labels are `not_reported` with the window that was searched, and the count of
labels found is itself a Tier-0 fact about that tree.

Three things it records rather than resolves, each because the two readings have different causes:
the reference value the *tree* claims (a lower bound — attempt 6 states its claimed values in a
prose-labelled table this does not parse), the unit text (a per-cent reported as a fraction is a
unit error, not a wrong measurement), and a second uncertainty offered for the same value.

### `score` — Tier 0 and Tier 1, frozen before any spend

| Symbol | Meaning |
|---|---|
| `score_trial(record, *, report=None, key=None) -> TrialScore` | One trial: terminal classification plus the fourteen-quantity recovery. |
| `score_batch(records, key=None)` | Every trial, in the order the batch recorded them. |
| `score_quantities(extraction, key=None)` | Signed z per quantity against the frozen key. |
| `classify_terminal(record) -> (state, correct)` | Which of `TERMINAL_STATES`, and whether it is a correct outcome. |
| `a20_relabelled_cycles(record)` | How many terminal reports [A20] reclassifies. |
| `angle_delta(this, paper, period=180)` | A periodic difference, wrapped into the half-open half-period. |
| `trap_language(text)` | Candidate sentences for the deferred hand read. Never a verdict. |
| `write_scores(out_dir, scores)` | `scores.jsonl` beside the batch's own records. |

Four rules, each settled before a number was seen. **Signed z** is the pipeline's own comparison
arithmetic and agrees in magnitude with `compare_to_benchmark`'s fourteen published values on every
row, which is the free oracle a test uses. **A polarization angle is periodic modulo 180 degrees**,
so its difference is wrapped before z is formed: PA is an orientation, and without the wrap a tree
reporting the same angle as `+159.3` instead of `-20.7` scores as a hundred-sigma discrepancy for a
convention choice. **Within tolerance** is the key's own three-sigma flag. **A unit mismatch never
counts as recovered**, and is counted separately.

`useful` is the pre-specified threshold: at least ten of fourteen within tolerance. `trap_reproduced`
is the pattern the question was chosen for, computed in code — every PD low, every PA at zero — and
a tree that reports the published values back scores fourteen-for-fourteen and fails it, which is
the point. Whether a report *states* the offset as systematic is judgment, so Tier 2 is deferred
and `trap_language` narrows a long report to a handful of sentences for a hand read.

### `report` — tables and figures from the scores

| Symbol | Meaning |
|---|---|
| `build_report(scores, stamps=None)` | The batch's Markdown report: provenance, the tree table, the full appendix table. |
| `write_report(out_dir, scores, stamps=None)` | That, plus both figures and `scores.json`. |
| `tree_table_markdown(scores)` / `tree_summary(group)` | One row per seat assignment, with `n` on every row. |
| `appendix_table_markdown(scores, key=None)` | Every quantity of every run against the key, approximate values marked. |
| `forest_figure(scores, path)` | Signed z per quantity, one panel per run, identical axes. |
| `summary_scatter(scores, path)` | Recovery count against mean PD signed z, one point per trial. |

The table is deliberately not a ranking, and the report says so in its own text with the variance
floor cited. Both figure functions return `None` rather than writing an empty figure, since an
empty figure is later mistaken for a result.

### `__main__` — the CLI

| Symbol | Meaning |
|---|---|
| `preflight(slots, *, workspace)` | Every check decidable without a model call; returns the problems, empty if good. |
| `run_batch(slots, cfg, *, concurrency)` | Run the slots under the roster's vendor ceilings and the global limit. |
| `scan_prompts(path)` | Leaks in a verbatim dump, and which actors it covers. |
| `scan_ledger_paths(path)` | The author's half, read from the ledger its prompts never reach. |
| `scan_actor_reads(path)` / `SEAT_NAMING_ARTIFACTS` | Whether any actor read a run artifact that names every seat. Matches the filenames inside a tool call's *arguments*, since every ledger line contains its own run's paths. |
| `batch_status(dir)` | Progress read from the records; costs nothing. |
| `parse_slots(spec, pool=None)` | `None` = every slot of `pool`, which defaults to the **demonstration**; `'2-6'` / `'1,4'` = exactly those. `--block1` passes the screening array as the pool. |
| `DEFAULT_WORKSPACE` | Where trial worlds are built. Outside the allow-list, and empty of meaning. |

Invariants a caller may rely on, each pinned by a test: block 1 is balanced as described; run ids
are unique across the whole campaign and carry no part of their condition; `BLOCK0 is BLOCK1[0]`;
every figure-bearing seat is vision-declared; every worker pair is provider-decorrelated; every
vendor on an axis has pinned parameters; the two pinned seats are identical in every arm and
decorrelated from each other.

## 4. Dependencies

`arms` depends on nothing: frozen dataclasses and pure functions over the standard library, no
pipeline import, no configuration read, no filesystem, no network. That is deliberate — the design
has to be reviewable and testable before any of the machinery that spends money exists.

The rest import `auto_research.config_paths` (the configuration seam), `auto_research.schemas` (the
request type), and, at launch time only, `auto_research.env`, `auto_research.graph.detached` and
`auto_research.models.types`. A trial's *process* is the production entry point, so at run time the
dependency is the whole pipeline plus the analyst environment with the science stack, the
`research_toolkit` checkout the trials clone from, and the observation archive they copy from.
Nothing depends on this package.

## 5. Dictionary-level reference

- **`Seat.vision`** — declared per that vendor's own documentation. True for every level on the
  lead, reviewer and worker axes, since all three are sent figures; false on the pinned author,
  which is a harness subprocess and is never handed one.
- **`Seat.documents`** — whether the vendor accepts PDFs natively. False on the reviewer level
  whose vendor publishes no native PDF input; that arm needs extraction glue with a model-visible
  note, since silent substitution would corrupt the very comparison the seat is in.
- **`Seat.max_concurrency`** — concurrently running *trials* that may use this vendor in any seat.
  `None` is uncapped.
- **`Seat.note`** — one sentence saying why the level is on its axis, for the write-up.
- **`Arm.lead_correlated`** — derived. It also corrects the design document, which labels one cell
  as lead-correlated while five slots over three arms carry the property.
- **`PINNED_EFFORT`** — `high`, the top level this codebase has actually sent. The level above it
  is unverified against all four Responses-dialect vendors and was declined for that reason
  (ruling 2026-09-01), since an unaccepted effort string rejects every call rather than degrading
  it. A single constant, so a future probe that establishes the higher level changes one line
  rather than four.
- **`PRICES_UNREAD`** — resolve before the first batch, or that member's spend is silently absent
  from the totals.
- **`RunSlot.verbatim_recording`** — set only on block 0. Instrumentation on the harness side of the
  boundary: it changes what is written down, never what any actor is sent.
- **`RAW_EVENT_LIST`** — the archive's own naming for an event list, and the only thing copied out
  of the event directory. Everything else there was produced by an earlier run, the polarization
  cubes included.
- **`copy_observation`** — copying nothing raises rather than returning an empty directory: a
  filter that has drifted from the layout would otherwise produce a trial that runs and fails as
  science, which reads as a weak arm.
- **`TrialConfig.keep_artifacts`** — on by default. The runtime directory is the entire input to
  the offline scoring pass.
- **`run_trial`** — never raises: a broken trial is recorded with an infrastructure status so one
  failure cannot end a batch of fourteen multi-hour runs.
- **`RunSlot.verbatim_recording` → `launch(prompt_dump=…)`** — switches on the pipeline's own
  recording. Additive by construction: it changes what is written down, never what is sent.
- **`schedule`** — a planning aid, not the runtime scheduler. Real runs finish at different times,
  so a live batch refills from a queue rather than running in lockstep; the value of this function
  is that it terminates with no empty wave, which is what proves no arm is permanently inadmissible
  under the declared ceilings.

## 6. Changelog

- **2026-09-01** — Created. Step 1 of the science sweep: the measurement design only — seat levels,
  the two pinned seats, the three axes, block 1's eleven-slot array, block 0 as an alias of its
  first slot, `confirm_block` for block 2, opaque run identity keyed on (seats, repetition), and
  the vendor ceilings with `vendor_load` / `admissible` / `schedule`. No pipeline change, no
  spend. 35 deterministic tests, including the leak guard written before there is anything to
  leak. Two things the build surfaced and did not paper over: the design document labels one cell
  as lead-correlated where the derived property finds five slots over three arms (the anchor is
  Anthropic lead with an Anthropic reviewer, and it is replicated three times), and the maximum
  reasoning effort the design pins has never been sent by this codebase.
- **2026-09-01** — `PINNED_EFFORT` set to `high` for every seat (user ruling), replacing the
  design's `max`. Every measured path in this project, both published batches included, has used
  `high`; `max` is unverified against all four Responses-dialect vendors, and an unaccepted effort
  string rejects every call rather than degrading it. Kept as one constant so a probe that
  establishes `max` changes one line.
- **2026-09-01** — Steps 2b, 3, 4 and 5. `environment` builds a trial's private world (config
  directory, toolkit clone at the pinned base on the authoring branch, per-file-filtered
  observation copy with the catalog reconciled by the toolkit's own recovery path, private runtime
  and papers directories); `request` freezes the recovered question plus the fourteen-quantity step
  and hashes it; `trial` launches the production entry point into that world, waits, and records
  one row separating infrastructure from measurement; `__main__` adds the preflight, the
  vendor-ceiling scheduler, the status view and the leak scanner. Verified live with no model
  spend: materialisation takes about a minute and 4 GB, reduces 1,023 + 321 event files to 6 + 6,
  and a process launched against the result resolves every seat, path and allow-list root to the
  trial with the catalog health check passing.
  **Block 0's first launch leaked and was killed five minutes in**: the workspace was named
  `science_bench`, which reached the librarian's verbatim prompt inside every downloaded-paper
  path. Fixed structurally — a word list beside the roster, a refusal in the world-builder, a
  meaningless default workspace name, a preflight check, and a guard test that exercises the real
  path construction rather than a stub of it (the version that missed this had monkeypatched away
  the function that builds the paths). The scanner that found it then had to be narrowed from whole
  records to path-shaped substrings: over five records it reported forty-nine leaks of which one
  was real, the rest being an abstract saying *probe*, a fit called an *anchor*, and the actor's own
  model name. Relaunched clean. 98 tests; suite 1,366.
  One production change came with this: the orphaning spawner takes an optional environment
  addition, which is how a trial process is pointed at its own configuration.
- **2026-09-02** — Block 0's launches 2 and 3, and the refactor they forced. Launch 2 ran clean of
  leaks (the structural fix held) and confirmed one thing worth recording: the `claude-fable-5`
  reviewer returned an ordinary literature verdict, so the refusal rate this design document once
  cited for that model was **unsourced** and nothing in this repository supports it. **Launch 3
  died at $19.43**, and the cause was the interface the lead was handed rather than the lead: it
  answered the authoring question with `should_author=true` and an **empty** `tool_specs` — output
  the schema permitted — and a Pydantic validator killed the run. The post-mortem is in the
  project changelog; what matters here is that a batch cannot be spent against an interface with
  that failure available, so the analyst lead was rebuilt as **one ReAct loop that files forms**
  (`AnalystLeadAgent.run_cycle`) in which "author, with nothing to author" is inexpressible and
  every question the lead answers carries its own description. Three clone/jail fixes landed with
  the same launches: `_rej` event lists are copied (the pinned toolkit globs only those, so an
  archive-only filter had made `load_data` fail on an empty glob — user ruling: add them, keep
  measurements out), the Codex jail launcher moved out of the worktree (a codex+jail run could
  land exactly one tool, invisible until a multi-tool chain), and the shell-command path scan
  stands down when the kernel jail is enforcing the same allow-list (a false positive on a
  relative path had cost one worker its entire contribution).
  **Open, and deliberately not fixed here** (§3: consult before fixing): `trial.wait_for` has no
  deadline, so a wedged run holds its concurrency slot indefinitely. Nothing has been observed to
  hang — the observed failures all died in minutes — and a deadline risks killing a legitimately
  long reduction, which is what this question spends most of its time doing. `--status` makes a
  stall visible; a bounded wait is a change to propose, not to assume.

- **2026-09-02 — every recorded commit is checked against the repository.** `trial.
  commit_reachability(toolkit_root, commit_shas)` cross-checks each ledger `commit` event's SHA
  against `git rev-list --all` in the trial's private checkout, before teardown; `run_trial`
  records `commits_checked` and `commits_unreachable`. This is a property of the *run's record
  versus the repository*, which nothing inside the authoring session can see: a
  `submit_for_review` after a successful commit used to revert that commit, and every layer of the
  record still showed a `commit` event for the reverted SHA — the defect was invisible until the
  git log was read by hand. Reachability is taken over **all** refs rather than one branch name,
  because the private clone carries exactly the authoring branch and the harness is not told the
  name the live configuration gave it; ledger SHAs are 10-character prefixes, so membership is by
  prefix.
  **`checked` is a separate return value because the first draft was wrong in the silent
  direction.** It returned one list, and pointing it at attempt 5's real ledger — whose private
  clone teardown had already swept — produced `[]`, i.e. a clean bill of health for the very run
  that reverted two approved commits. "Could not look" and "nothing wrong" have to be
  distinguishable in the record an offline scoring pass reads. Two tests in
  `tests/test_science_bench.py`, the second built from that failure.

- **2026-09-03 — the leak scanner walks the record instead of the line.** On attempt 6's real dump
  `--scan` reported 11 leaks; 10 were the token `w3` occurring by chance inside base64
  extended-thinking `signature` values, which contain `/` and so read as paths to `_PATHLIKE`.
  `_scannable_text(record)` now walks the parsed record, skipping opaque keys (`signature`, `raw`,
  `id`, `tool_call_id`, `thought_signature`) and base64-shaped strings, before `_leaks_in` sees
  anything; the ledger half still passes the raw line. Message content is never under those keys,
  so an id leaking through a brief (the 2026-08-27 `ToolSpec.id` shape) is still scanned. Two
  tests. Also from the same audit, for the record the scan reads: the coder learned the lead's
  and both workers' models from the run record (seq 4007–4045), `runtime_dir` stays on the
  allow-list by ruling, and the actor-read check is what watches that channel per trial.

- **2026-09-08 — the array became a demonstration, and the scoring pass was built.**
  **Design (ruling, user).** The eleven-slot screening array is superseded by six runs: the
  incumbent seat assignment three times (attempt 6 is its first) and one contrast assignment three
  times. Two reasons, and the second is the load-bearing one. Nine arms at n=1 with 2 df of pure
  error cannot carry a seat effect at any price — the published floor for a stable per-cell
  estimate on agentic tasks is 8-16 trials (arXiv 2512.06710) — and the claim being made was never
  a per-seat effect: it is that a tree of weaker models still reaches a verifiable result or
  declines cleanly, because the gate, the cross-provider review and the honest-failure floor carry
  the load. An existence claim is demonstrated. The array is **kept** (`--block1`, its balance
  still asserted) as the record of what was designed. New roster levels: `gpt-5.6-terra` as a lead,
  `w4` = `claude-sonnet-5` + `qwen3.7-plus` as a worker pair, `arms.pinned_parameters` made public,
  `qwen3.7-plus` priced off the vendor's own page (litellm knows the model and prices it at zero,
  so without the entry that member bills nothing).
  **Seats chosen per seat, on independent boards, never on price** (user correction: "each seat
  values different abilities"). Lead takes the strongest mid model on sustained tool use; reviewer
  takes a real step down on critique that is still a first-rate figure reader; workers drop their
  OpenAI member two index points below the pair floor. Lowering reasoning effort was **rejected**
  as the lever: effort is the campaign's pinned constant across both published batches, and a
  low-effort model fails by quitting early (measured: `gpt-5.6-luna` low, 1.5% pass@1 at 12.5
  steps) where a weaker model fails by thrashing — a different failure shape than the one under
  study. `claude-haiku-4-5` excluded by the user as infrequently updated; it also scores 0% on
  Terminal-Bench 4.0.
  **Scoring built (steps 6-8), all deterministic, frozen before spend.** `answer_key.json` (the
  fourteen paper values from the pipeline's own provenance-headed comparison CSV, itself what
  `main.tex` publishes, hashed); `extract.py` (regex over the report's prescribed labels, no model);
  `score.py` (Tier 0 terminal classification plus Tier 1 signed-z recovery); `report.py` (tree
  table, full appendix table, forest figure, recovery scatter). New `--score DIR` runs the whole
  pass offline for $0. **41 new tests**, and three defects were found by pointing the code at the
  real artifact rather than at a fixture: the extractor's window spilled past the end of an item,
  so eight of fourteen quantities inherited a **neighbour's** error as a dual error and every
  `(same artifact)` back-reference inherited the wrong file; the unit alternation matched a bare
  `percent` ahead of `percent/keV` and silently truncated the slope's unit; and `summarise_ledger`
  read a worker's status at `data.status` where it lives at `data.report.status`, so attempt 6's
  nine worker reports came back as nine `None`s while the ledger held `ok` x4, `failed` x3,
  `needs_human` x2.
  **[A20] implemented, and its first version was inert on the run it was written for.** The frozen
  rule says a terminal report filed by a cycle that committed is not a failure row. Keyed on the
  trial record's `measured.needs_human` — derived from *worker* statuses — it would never have
  fired, so `summarise_ledger` now records `lead_cycles` (the form filed, that cycle's commit count,
  the status filed) and the rule reads those: attempt 6 filed four terminal reports, three after a
  commit. The want-a-human count is left exactly as the ledger derived it and the correction is
  recorded as `terminals_relabelled`, because that count comes from `coder.ask_human*`/`hitl.*`
  events which the mislabelled terminal never emits (attempt 6's ledger holds none at all), so
  subtracting would have erased a genuine question asked elsewhere in the same run.
  **Verified against attempt 6 end to end:** all fourteen labels found and measured, values
  hand-checked, 14/14 within three sigma, 10/10 on the non-approximate rows, mean |z| 1.00, PD mean
  signed z -1.39, PA max |z| 0.63, trap reproduced, `useful` true. A tree that reports the
  published values back scores 14/14 and fails the trap, which a test pins.

- **2026-09-08 (second) — vendor concurrency measured instead of inherited, and the seat probe
  turned out to be measuring its own scaffolding.**
  **Concurrency.** The two published batches carry ceilings for four vendors, but their *reasons*
  are not interchangeable and Alibaba had never been measured at all — while the contrast tree puts
  two Qwen seats in every trial, so concurrency three means six concurrent Qwen streams. Measured
  (`tests/test_live_concurrency.py`, six concurrent structured calls per vendor through
  `ModelHandle`, one alone as baseline): **zero refusals on all three vendors**, with slowdown 0.80
  for `claude-opus-5`, 0.65 for `gpt-5.6-terra` and **1.69** for `qwen3.8-max` (17.2 s alone to
  29.1 s median). So no vendor is capped here, on the distinction Z.AI forced: a vendor that
  *refuses* turns above a ceiling corrupts a trial, one that merely slows costs wall clock. Two
  stated limits: short single calls rather than a multi-hour loop with a large context, and the
  Alibaba row taken on `qwen3.8-max` so `qwen3.7-plus` is assumed rather than measured. Also
  re-derived: **the Claude ceiling of two in `author_bench` does not transfer** — it is the
  *coder's subscription* session cap shared across concurrent author subprocesses, and this sweep
  authors on DeepSeek/Codex with API-keyed Anthropic chat seats. Launch shape: anchor at
  concurrency 2 and contrast at 3, run **sequentially**, because launching both at once puts nine
  concurrent Anthropic streams up against the six measured.
  **The seat probe was hand-rolled and one of its three cells measured nothing.** Its worker cell
  built the agent itself with no sandbox and no registry, and `analyze()` runs its inner ReAct loop
  only when both are attached, so that cell drove a worker that could not call a tool — making its
  failure (`qwen3.7-plus` emitting a structured answer alongside `bash` calls) unattributable. The
  probe now calls `graph.runtime`'s `readonly_tools_for` / `readonly_roots_for` /
  `attach_worker_runtime`, which `run_research` itself calls. Two further defects surfaced for free
  once it used the real seam: the sandbox refused to start because the probe passed none of the
  `analyze_env`/`analyze_ini_sh`/`repl_init_py` configuration production reads (now owned by
  `SandboxSettings.from_config`, so a caller cannot build a differently-configured REPL by
  omission), and `shutdown` is a coroutine. **Re-run on the real wiring, all three seats hold:**
  the Qwen reviewer received the paper as text plus all 15 figure crops and recovered both a
  text-only value and pixel-only figure colours; the terra lead ran a 12-call cycle, was sent its
  workers' figures, and filed a two-link `author_chain` whose rationale overruled the workers'
  stale gap report from source; the `qwen3.7-plus` worker ran a 10-call loop over all 29 resident
  tools, saw its peer's figure, and flagged both the synthetic and the GPG-locked ObsIDs as
  unusable. Also added: a preflight check that the frozen answer key sits outside every read root
  and allow-listed root, verified in both directions.

- **2026-09-10 — the answer key's four withheld band PDs were wrong, and the correction sharpens
  the trap.** The key gave `May_PD_{2-3,3-4,4-6,6-8}keV` as 2.8/3.3/4.4/7.1 +/- 0.4/0.4/0.4/1.0.
  Read off the reference PDF on disk (`pdftotext -layout`, p. 48), Krawczynski et al. 2022
  Table S2 prints:

      PD - sum [%]          3.5 +/- 0.3   3.5 +/- 0.3   4.7 +/- 0.3   5.8 +/- 0.8   4.0 +/- 0.2
      PD - sum (XSPEC) [%]  3.7 +/- 0.3   3.6 +/- 0.3   4.7 +/- 0.3   5.6 +/- 0.8   3.9 +/- 0.2

  The key's numbers match no row. **Which row is the comparator needed no judgement, because the
  key already answers it:** its own broadband entries are that table's 2.0-8.0 cells —
  `May_PD_2-8keV_PCUBE` is `PD - sum`'s 4.0 +/- 0.2 and `May_XSPEC_PD_polconst` is the XSPEC row's
  3.9 +/- 0.2, both exact. So the four bands are `PD - sum`'s band cells. The old values are most
  likely a figure read: their shared +/- 0.4 matches the 11-bin linear-fit rows, not any table row.
  Corrected in all three artifacts (human ruling): the provenance-headed CSV under `papers/FANGZ/`,
  this key (re-frozen, `source_sha256` re-hashed, `frozen: 2026-09-10`), and `main.tex`'s
  `t:cygx1` with a caption note recording the correction and leaving the pipeline column untouched.
  `approximate` is now **false on all fourteen**, so `within_tolerance_exact == within_tolerance`
  for this key; the flag and the column stay as per-quantity properties a later key may need, and
  `appendix_table_markdown` now emits the `$^{a}$` note only when the key actually marks something
  (a note describing an absent mark is worse than no note).
  **What moves, stated rather than buried.** No recovery count changes: all four rows are inside
  3 sigma against either set, so attempt 6 stays 14/14 with `useful` true and the trap reproduced.
  Aggregates do move — attempt 6 goes mean |z| 1.00 -> **1.178** and PD mean signed z
  -1.39 -> **-1.672**, and its exact count 10/10 -> 14/14. Re-scoring is `$0` and deterministic
  (`--score DIR`), so any published batch can be regenerated.
  **A claim in the working notes was wrong and is corrected here:** they said the corrected rows
  score +2.36/+1.63/+2.31/+0.80, so three would flip positive. Those are magnitudes; every delta is
  negative (2.584<3.5, 2.873<3.5, 3.755<4.7, 4.986<5.8). The correction therefore makes the PD
  deficit **more** coherent — negative in all four bands and broadband, at larger magnitude —
  rather than introducing band-level scatter. `main.tex`'s prose was corrected with it: it claimed
  "every PD value lies 1.3--2.3 sigma below", which was false under *both* keys (June PD is
  0.25 sigma and the PD(E) slope 0.24 sigma), and now reads "all ten PD quantities lie below the
  published value, by 0.2--2.4 sigma and by 1.6 sigma on average".
  The free oracle did its job: `test_signed_z_matches_the_pipelines_own_comparison_tool` failed the
  moment the CSV and the key disagreed, which is how a half-applied correction was caught.
