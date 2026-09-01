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

**Built so far:** the measurement design (this module). The per-trial environment, the driver, the
scheduler and the scoring are not built.

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

Invariants a caller may rely on, each pinned by a test: block 1 is balanced as described; run ids
are unique across the whole campaign and carry no part of their condition; `BLOCK0 is BLOCK1[0]`;
every figure-bearing seat is vision-declared; every worker pair is provider-decorrelated; every
vendor on an axis has pinned parameters; the two pinned seats are identical in every arm and
decorrelated from each other.

## 4. Dependencies

Nothing. The module is frozen dataclasses and pure functions over the standard library — no
pipeline import, no configuration read, no filesystem, no network. That is deliberate: the design
has to be reviewable and testable before any of the machinery that spends money exists.

Its tests need only `pytest`. Nothing depends on this package.

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
