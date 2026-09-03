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

**Built so far:** the measurement design, the per-trial environment, the frozen question, the trial
driver, and the CLI with its preflight, scheduler and leak scanner. Block 0 has run. The scoring —
the ledger-derived tier, the fourteen-quantity extractor and the figures — is not built.

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

### The campaign is three blocks, fourteen runs

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

### `__main__` — the CLI

| Symbol | Meaning |
|---|---|
| `preflight(slots, *, workspace)` | Every check decidable without a model call; returns the problems, empty if good. |
| `run_batch(slots, cfg, *, concurrency)` | Run the slots under the roster's vendor ceilings and the global limit. |
| `scan_prompts(path)` | Leaks in a verbatim dump, and which actors it covers. |
| `scan_ledger_paths(path)` | The author's half, read from the ledger its prompts never reach. |
| `scan_actor_reads(path)` / `SEAT_NAMING_ARTIFACTS` | Whether any actor read a run artifact that names every seat. Matches the filenames inside a tool call's *arguments*, since every ledger line contains its own run's paths. |
| `batch_status(dir)` | Progress read from the records; costs nothing. |
| `parse_slots(spec)` | `None` = all of block 1; `'2-11'` / `'1,4,7'` = exactly those. |
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
