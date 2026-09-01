"""The science sweep's measurement design: which model sits in which seat, in which run.

``author_bench`` measures the *coding* half of the pipeline -- one lead driving one author through
gate and review. This measures the *science* half: a whole ``run_research`` against a question with
a published answer, so what varies is the reviewer, the analyst lead and the analyst worker pair,
and what is held fixed is everything the coding sweep already measured.

**The structural difference from `author_bench`, and why this module exists at all.** There a trial
is one lead call, so its seats are objects the trial constructs and injects. Here a trial is a whole
run, and ``run_research`` resolves its own roles out of ``config/models.yaml`` with no injection
point -- so a seat assignment is a *configuration*, not an argument. This module is therefore pure
design: the levels, the arms, the run array, the run-identity rule and the vendor ceilings, as data
and pure functions. Nothing here launches anything; the per-trial config that carries an arm into a
real run is built on top of it.

Two conventions are inherited from ``author_bench.legs`` deliberately:

**A product is generated; a curated selection is literal.** The lead x co-lead grid there is a
product, so writing its cells out by hand would hide the design in repetition and make a missing
cell invisible. Block 1 here is the opposite case -- a hand-balanced fractional array, chosen so
each lead and each worker pair appear three times and every lead x worker-pair combination appears
once -- so it is written literally and its balance is *asserted by test* rather than trusted.

**Capability is declared, never assumed.** Every seat carries its own ``vision`` and ``documents``
flags, so a vision-less member in a figure-bearing seat fails a test instead of a trial. Two
eligibility facts are carried as data rather than prose: ``glm-5.3`` is text-only, so Z.AI's
figure-seat level is ``glm-5.3-flash``; and ``qwen3.8-max`` has no native PDF, so its *reviewer*
level declares ``documents=False`` until the extraction glue for the no-native-PDF reviewers lands.

**The campaign is three blocks, 14 runs.**

``block 0`` -- one run, the anchor arm, with verbatim recording of every actor's prompt, read for
the arm before anything is batched. A static prompt dump shows the seed messages; only a real run
shows the paths, ids, branch names, tool descriptions and test-runner banners that exist once the
thing is executing, and those are where both 2026-08-27 leaks lived. A clean block 0 **is** block 1's
first slot -- its only difference from a batched run is recording that never enters an actor's
context, which :data:`BLOCK0` encodes by being that slot rather than a copy of it. A leak fix that
changes anything an actor sees makes the probe a different treatment, so it is discarded instead.

``block 1`` -- 11 slots over 9 distinct arms, the anchor replicated at slots 1, 10 and 11 to give
the design its only pure error (2 df).

``block 2`` -- assembled from block 1's estimates and confirmed at K=3, so it cannot be written
here; :func:`confirm_block` builds it when block 1 reports.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

# Vendor brand colours, shared with ``author_bench`` so the two sweeps read as one campaign.
# Colour never carries meaning alone in the figures (each vendor also gets its own marker and a
# direct label), so these are identity, not scale.
BRAND_COLOURS: dict[str, str] = {
    "anthropic": "#d97757",   # clay orange
    "openai": "#0b0b0b",      # near-black
    "deepseek": "#4d6bfe",    # indigo
    "zai": "#12a594",         # teal
    "dashscope": "#7c5cd6",   # purple
}

# Published per-million-token rates for the models litellm's map does not carry. Anthropic, OpenAI
# and DeepSeek are deliberately absent: a hand-copied rate that shadows a maintained one is a
# liability.
_PRICES: dict[str, dict[str, float]] = {
    "qwen3.8-max": {"input": 2.0, "output": 6.0},
}

#: Models on an axis whose rate has not been read off the vendor's own page yet. Named rather than
#: guessed: a made-up rate is indistinguishable from a read one once it is in a cost table, and the
#: sweep's whole cost claim is that its figures are money somebody paid. Resolve before the first
#: batch, or that member's spend is silently absent from the totals.
PRICES_UNREAD: tuple[str, ...] = ("glm-5.3-flash",)

# Reasoning effort is pinned at each vendor's maximum in **every** seat, and that is a measurement
# decision rather than a tuning one: the intra-model spread across effort settings dwarfs every
# between-model gap, so a member left at its own role default would compare configurations instead
# of models. Anthropic's analogue of a maximum reasoning effort is adaptive thinking; the three
# Responses-dialect vendors take ``reasoning.effort`` under the names their own adapters send.
#
# Every vendor that appears on an axis needs an entry here, or that axis silently compares a pinned
# member against a defaulted one -- the exact error this table exists to prevent.
#: The effort level every Responses-dialect seat is pinned to.
#:
#: ``high``, not ``max`` (ruling 2026-09-01). The design's reason for wanting the top of the scale
#: stands -- the spread within one model across effort settings dwarfs every gap between models, so
#: the level has to be pinned and pinned high -- but ``max`` has **never been sent by this
#: codebase**: every measured path in the project, both published batches included, used ``high``,
#: and whether each of the four Responses-dialect vendors accepts that exact string is unverified.
#: An unaccepted value is not a degraded run, it is a rejected call in every seat of every trial.
#:
#: So the trade is taken deliberately: a verified level shared by all four vendors, which is what
#: parity across the axis actually requires, over an unverified one that might be a fraction better
#: on the vendors that do take it. It also keeps this sweep comparable to the coder sweep and the
#: lead x co-lead grid, which pinned ``high`` for the same reason. One constant, so a future probe
#: that establishes ``max`` changes one line rather than four.
PINNED_EFFORT = "high"

_PINNED_PARAMETERS: dict[str, dict[str, object]] = {
    "anthropic": {"thinking": {"type": "adaptive"}, "max_tokens": 21000},
    "openai": {"max_output_tokens": 21000, "reasoning": {"effort": PINNED_EFFORT}},
    "zai": {"max_output_tokens": 21000, "reasoning": {"effort": PINNED_EFFORT}},
    "dashscope": {"max_output_tokens": 21000, "reasoning": {"effort": PINNED_EFFORT}},
    "deepseek": {"max_output_tokens": 21000, "reasoning": {"effort": PINNED_EFFORT}},
}


@dataclass(frozen=True)
class Seat:
    """One model in one role: what it is, what it can be sent, and what its vendor tolerates."""

    slug: str
    """Short vendor-and-tier tag. Report and figure only -- it never reaches a path, an id, or
    anything an actor can read."""

    provider: str
    model: str

    vision: bool = True
    """Whether this member is declared vision-capable, per that vendor's own documentation.

    The reviewer, the analyst lead and the analyst worker are all sent figures, so every level on
    those three axes must be true. It exists as a declaration so a vision-less member in a
    figure-bearing seat fails a test rather than a trial: ``glm-5.3`` is text-only by its vendor's
    own description, which is why Z.AI's level here is ``glm-5.3-flash``."""

    documents: bool = True
    """Whether this member accepts PDFs natively.

    False for ``qwen3.8-max`` in the reviewer seat: the reviewer is handed paper PDFs and Alibaba
    publishes no native PDF input, so that arm needs extraction glue with a model-visible note
    before it can review. Declared here so the gap is data the harness can act on rather than a
    caveat in prose."""

    max_concurrency: int | None = None
    """How many concurrently running trials may use this vendor in any seat; ``None`` is uncapped.

    A vendor ceiling, not a role ceiling: a throttled lead, reviewer or worker stalls a run exactly
    as a throttled author does. Z.AI is the measured case -- above two in flight it throttled 87 to
    97 per cent of turns."""

    note: str = ""
    """Why this level is on the axis. One sentence, for the write-up."""

    def price(self) -> dict[str, float]:
        """Per-million-token rates for the vendors litellm does not price; empty otherwise."""
        return dict(_PRICES.get(self.model, {}))

    def parameters(self) -> dict[str, object]:
        """The pinned maximum-effort settings for this seat's vendor."""
        return dict(_PINNED_PARAMETERS.get(self.provider, {}))


@dataclass(frozen=True)
class WorkerPair:
    """The two analyst workers of one arm, mutually provider-decorrelated.

    Decorrelation inside the pair is what makes the debate two independent readings rather than one
    vendor agreeing with itself, so it is a property of the level and is asserted, not hoped for.
    """

    slug: str
    members: tuple[Seat, Seat]
    note: str = ""

    def providers(self) -> tuple[str, ...]:
        return tuple(member.provider for member in self.members)


# ── The seats held fixed in every cell ──────────────────────────────────────
#
# The coding half is already measured (the coder sweep and the lead x co-lead grid), so holding it
# fixed is precisely what isolates the science half. Both pins are also what make the campaign
# affordable and its costs real money: the DeepSeek author settled at $6-10 per authoring trial
# against Claude Sonnet's $100+, and it bills to an API key rather than a subscription, so every
# cost figure this sweep reports is a price somebody paid.

#: The author, identical in all 14 runs. Authoring is ON -- the toolkit is pinned before #12's own
#: tool chain exists, so every tree must author its way to the capability before it can do the
#: science.
CODER = Seat(
    slug="deepseek",
    provider="deepseek",
    model="deepseek-v4-flash",
    vision=False,
    documents=False,
    note=(
        "cheapest capable author measured, and it bills to a key rather than a subscription, so "
        "the sweep's cost figures are real money"
    ),
)

#: The Codex app-server drives the author; DeepSeek publishes the Responses endpoint it needs.
CODER_HARNESS = "codex"
CODER_KEY_ENV = "DEEPSEEK_API_KEY"
CODER_BASE_URL = "https://api.deepseek.com/"

#: The tool reviewer, identical in all 14 runs. Decorrelated from a DeepSeek author by
#: construction, which is what CLAUDE.md 9.3 requires. Never sent an image, so its vision
#: declaration is about capability rather than need.
COLEAD = Seat(
    slug="openai",
    provider="openai",
    model="gpt-5.6-sol",
    note="provider-decorrelated from the DeepSeek author, per the merge rule",
)


# ── The three varied axes ───────────────────────────────────────────────────

#: The analyst lead. Sent the accumulated per-worker figure catalog, so every level is
#: vision-capable.
LEADS: tuple[Seat, ...] = (
    Seat(
        slug="opus",
        provider="anthropic",
        model="claude-opus-5",
        note="the incumbent lead, and the top composite generalist",
    ),
    Seat(
        slug="sol",
        provider="openai",
        model="gpt-5.6-sol",
        note="first on research-level physics, the eval closest to what a lead actually does",
    ),
    Seat(
        slug="qwen",
        provider="dashscope",
        model="qwen3.8-max",
        note="tied-first figure reader and first on agentic tool use; its own effort setting is undocumented",
    ),
)

#: The reviewer. Also sent figures. Cut from three levels to two on budget, keeping the strongest
#: available reviewer and one from a different ecosystem.
REVIEWERS: tuple[Seat, ...] = (
    Seat(
        slug="fable",
        provider="anthropic",
        model="claude-fable-5",
        note=(
            "first on text and vision arena, closed-book knowledge and scientific coding; carries "
            "a measured refusal rate, so its fallback route is wired before it reviews anything"
        ),
    ),
    Seat(
        slug="qwen",
        provider="dashscope",
        model="qwen3.8-max",
        documents=False,
        note=(
            "tied-first figure reader at a fifth of the price; no native PDF, so this arm needs "
            "extraction glue with a model-visible note before it sees a paper"
        ),
    ),
)

#: The analyst worker pair. Both members are sent figures -- their peers' and their own tools' --
#: so both are vision-capable, and the two must come from different vendors.
WORKER_PAIRS: tuple[WorkerPair, ...] = (
    WorkerPair(
        slug="w1",
        members=(
            Seat(slug="sonnet", provider="anthropic", model="claude-sonnet-5"),
            Seat(slug="terra", provider="openai", model="gpt-5.6-terra"),
        ),
        note="the incumbent pair: what every published run of this pipeline has used",
    ),
    WorkerPair(
        slug="w2",
        members=(
            Seat(slug="qwen", provider="dashscope", model="qwen3.8-max"),
            Seat(slug="glmflash", provider="zai", model="glm-5.3-flash", max_concurrency=2),
        ),
        note="neither incumbent vendor, so it tests whether the pipeline depends on its usual pair",
    ),
    WorkerPair(
        slug="w3",
        members=(
            Seat(slug="sonnet", provider="anthropic", model="claude-sonnet-5"),
            Seat(slug="glmflash", provider="zai", model="glm-5.3-flash", max_concurrency=2),
        ),
        note=(
            "the informative pairing on cost-performance: the two members sit two orders of "
            "magnitude apart in price at comparable agentic scores"
        ),
    ),
)

BY_LEAD: dict[str, Seat] = {seat.slug: seat for seat in LEADS}
BY_REVIEWER: dict[str, Seat] = {seat.slug: seat for seat in REVIEWERS}
BY_WORKER_PAIR: dict[str, WorkerPair] = {pair.slug: pair for pair in WORKER_PAIRS}


@dataclass(frozen=True)
class Arm:
    """One seat combination: the three varied seats plus the two pinned ones."""

    lead: Seat
    reviewer: Seat
    workers: WorkerPair

    @property
    def coder(self) -> Seat:
        return CODER

    @property
    def colead(self) -> Seat:
        return COLEAD

    @property
    def slug(self) -> str:
        """Human-readable identity, for the report and the figure only.

        Never a path, a directory name, a branch or a run id. A readable label put in front of an
        actor is how a benchmark leaks its condition: a leg-named temporary directory reached the
        lead in 2,641 tool calls and the co-lead in 1,923 before it was caught, because the
        read-only tool provider renders its roots into the tool descriptions the model reads.
        """
        return f"lead_{self.lead.slug}_rev_{self.reviewer.slug}_{self.workers.slug}"

    def seats(self) -> tuple[Seat, ...]:
        """Every seat in the arm, varied and pinned, in a stable order."""
        return (self.lead, self.reviewer, *self.workers.members, self.coder, self.colead)

    @property
    def lead_correlated(self) -> bool:
        """Whether the analyst lead and the reviewer share a vendor.

        **Derived, never annotated.** ``author_bench`` learned the rule the same way: a cell that
        states its own correlation can state one it does not have. Deriving it also corrects the
        plan's array, which labels only the Qwen/Qwen cell as lead-correlated while the anchor
        (Anthropic lead, Anthropic reviewer) is equally correlated and is replicated three times.

        Distinct from the merge rule, which binds the *tool* reviewer against the *author* -- both
        pinned here and decorrelated by construction. Nothing in the project binds this pair; the
        property exists so the write-up can say how many cells carry it rather than guess.
        """
        return self.lead.provider == self.reviewer.provider

    def vendors(self) -> frozenset[str]:
        """The distinct vendors one run of this arm calls."""
        return frozenset(seat.provider for seat in self.seats())

    def identity(self) -> list[str]:
        """The seat models, in a stable order: what makes this arm the treatment it is.

        Two arms with the same models in the same seats are the same treatment, which is what
        makes this the right thing to hash a run id from.
        """
        return [f"{seat.provider}:{seat.model}" for seat in self.seats()]


def arm(lead: str, reviewer: str, workers: str) -> Arm:
    """Build an arm from three level slugs, failing loudly on an unknown level."""
    for name, slug, index in (
        ("lead", lead, BY_LEAD),
        ("reviewer", reviewer, BY_REVIEWER),
        ("worker pair", workers, BY_WORKER_PAIR),
    ):
        if slug not in index:
            raise KeyError(f"unknown {name} level {slug!r}; known: {', '.join(index)}")
    return Arm(lead=BY_LEAD[lead], reviewer=BY_REVIEWER[reviewer], workers=BY_WORKER_PAIR[workers])


# ── Run identity ────────────────────────────────────────────────────────────

#: Fixed salt, so a run id cannot collide with a production run id derived from a request, and so
#: the campaign's ids are stable across re-runs. It is inside the hash and never in a path.
_ID_SALT = "science_bench/cygx1_reproduce_krawczynski2022_polarization"


def run_id(arm: Arm, replicate: int) -> str:
    """The opaque run id for one (arm, replicate) pair.

    **The replicate index is load-bearing.** Hashing the combination alone collides in exactly the
    rows that matter: block 1's slots 1, 10 and 11 are the *same* arm -- being identical is the
    whole point of a noise floor -- so they would share one id, one run directory and one
    checkpoint, and so would block 2's three confirms.

    A hash rather than a readable label, matching what production has by accident: a production run
    id is a hex digest, so no actor can learn anything about its run from a path it is handed. A
    label like ``lead_qwen_rev_fable_w2`` would be in every tool description the lead and reviewer
    read.
    """
    if replicate < 1:
        raise ValueError(f"replicate index is 1-based; got {replicate}")
    payload = json.dumps(
        {"salt": _ID_SALT, "seats": arm.identity(), "replicate": replicate},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class RunSlot:
    """One scheduled run: an arm, which repetition of it this is, and how it is recorded."""

    index: int
    """Position in the campaign, 1-based. The plan's run number, for the human."""

    arm: Arm
    replicate: int
    """Which repetition of *this arm* the slot is, 1-based. Distinguishes the anchor's three
    slots, which are otherwise identical by design."""

    block: str
    """``block0`` / ``block1`` / ``block2``."""

    verbatim_recording: bool = False
    """Record every actor's complete prompt for the leak read.

    Set only on block 0. It is instrumentation on the harness side of the boundary -- it changes
    what is written down, never what any actor is sent -- which is the whole reason a clean block 0
    counts as a real measurement instead of a discarded probe.
    """

    note: str = ""

    @property
    def run_id(self) -> str:
        return run_id(self.arm, self.replicate)


def _slots() -> tuple[RunSlot, ...]:
    """Block 1's array, written out from the design rather than generated.

    A product would be generated; this is a hand-balanced fractional array over 3 x 2 x 3 factors,
    chosen so each lead appears three times, each worker pair three times, and every lead x
    worker-pair combination exactly once. The reviewer splits 5/4 rather than evenly, which is a
    small efficiency loss the design accepts. Writing it literally keeps the choice visible; the
    balance itself is asserted by test, so a careless edit fails rather than quietly unbalancing
    the design.
    """
    rows: tuple[tuple[str, str, str, int, str], ...] = (
        ("opus", "fable", "w1", 1, "anchor / incumbent"),
        ("opus", "qwen", "w2", 1, ""),
        ("opus", "fable", "w3", 1, ""),
        ("sol", "qwen", "w1", 1, ""),
        ("sol", "fable", "w2", 1, ""),
        ("sol", "qwen", "w3", 1, ""),
        ("qwen", "fable", "w1", 1, ""),
        ("qwen", "qwen", "w2", 1, ""),
        ("qwen", "fable", "w3", 1, ""),
        ("opus", "fable", "w1", 2, "noise floor: a replicate of slot 1"),
        ("opus", "fable", "w1", 3, "noise floor: a replicate of slot 1"),
    )
    return tuple(
        RunSlot(
            index=position,
            arm=arm(lead, reviewer, workers),
            replicate=replicate,
            block="block1",
            verbatim_recording=(position == 1),
            note=note,
        )
        for position, (lead, reviewer, workers, replicate, note) in enumerate(rows, start=1)
    )


#: Block 1: 11 slots, 9 distinct arms, the anchor replicated three times for 2 df of pure error.
BLOCK1: tuple[RunSlot, ...] = _slots()

#: Block 0: the leak probe. It **is** block 1's first slot -- an alias, not a copy, so the probe
#: and the measurement it becomes cannot drift apart. Run it alone, read its verbatim prompts for
#: the arm, and only then batch the remaining ten.
BLOCK0: RunSlot = BLOCK1[0]

#: What block 2 needs before it can exist. Recorded so a reader can tell an unassigned block from
#: a forgotten one.
BLOCK2_UNASSIGNED = (
    "Block 2 assembles the best level per seat from block 1's estimates, so its arm is not known "
    "until block 1 reports. Build it with confirm_block(). The assembly assumes the seats do not "
    "interact, which is why it is confirmed at K=3 rather than claimed from the screening "
    "estimates -- and why the assembled combination may be one no block-1 cell ran."
)


def confirm_block(lead: str, reviewer: str, workers: str, k: int = 3) -> tuple[RunSlot, ...]:
    """Block 2: the assembled best-per-seat combination, confirmed K times.

    Replicate numbering continues past any block-1 replicates of the same arm, so re-confirming the
    incumbent adds runs rather than colliding with the three it already has.
    """
    if k < 1:
        raise ValueError(f"block 2 needs at least one run; got k={k}")
    chosen = arm(lead, reviewer, workers)
    used = max((slot.replicate for slot in BLOCK1 if slot.arm == chosen), default=0)
    return tuple(
        RunSlot(
            index=len(BLOCK1) + offset,
            arm=chosen,
            replicate=used + offset,
            block="block2",
            note="confirm: the assembled best level per seat",
        )
        for offset in range(1, k + 1)
    )


def campaign(block2: Sequence[RunSlot] = ()) -> tuple[RunSlot, ...]:
    """Every slot in run order: block 1, then block 2 once it is assembled.

    Block 0 is not a separate entry -- it is block 1's first slot, and appears once.
    """
    return BLOCK1 + tuple(block2)


# ── Vendor concurrency ──────────────────────────────────────────────────────


def vendor_load(arms: Iterable[Arm]) -> dict[str, int]:
    """How many of the given concurrent runs touch each vendor.

    Counted **per run**, so an arm using one vendor in several seats counts once against it. That
    is deliberate and conservative in the useful direction: the graph dispatches its roles in
    phases rather than all at once, so three Qwen seats in one run are rarely three simultaneous
    Qwen conversations, and counting them as three would idle the batch for a contention that does
    not happen. The imprecision is in the other direction only when a run really does have two
    same-vendor roles in flight, which the ceilings absorb by being set below the measured limit.
    """
    load: Counter[str] = Counter()
    for item in arms:
        for vendor in item.vendors():
            load[vendor] += 1
    return dict(load)


def vendor_ceilings() -> dict[str, int]:
    """The tightest declared ceiling per vendor, over every seat on every axis.

    Read off the roster rather than written down separately, so raising a ceiling on a level cannot
    leave a stale copy behind.
    """
    ceilings: dict[str, int] = {}
    pairs_members = [member for pair in WORKER_PAIRS for member in pair.members]
    for seat in (*LEADS, *REVIEWERS, *pairs_members, CODER, COLEAD):
        if seat.max_concurrency is None:
            continue
        current = ceilings.get(seat.provider)
        ceilings[seat.provider] = (
            seat.max_concurrency if current is None else min(current, seat.max_concurrency)
        )
    return ceilings


def admissible(running: Iterable[Arm], candidate: Arm) -> bool:
    """Whether ``candidate`` may start while ``running`` arms are in flight.

    The scheduling rule lives here, beside the ceilings it reads, so it is designed and tested with
    the roster and merely enforced by whatever launches runs. A global concurrency number cannot
    express it: every run calls the DeepSeek author and the OpenAI co-lead, and six of block 1's
    eleven slots carry a Z.AI worker, so "launch ten" saturates the one vendor with a measured
    ceiling three times over.
    """
    ceilings = vendor_ceilings()
    load = vendor_load(running)
    return all(load.get(vendor, 0) + 1 <= ceilings[vendor]
               for vendor in candidate.vendors() if vendor in ceilings)


def schedule(slots: Sequence[RunSlot], ceilings: dict[str, int] | None = None) -> list[list[RunSlot]]:
    """Greedily pack slots into waves that respect every vendor ceiling.

    A planning aid rather than the runtime scheduler -- real runs finish at different times, so a
    live batch refills from the queue instead of running in lockstep. Its value here is that it
    terminates: a wave is always non-empty, which is what proves no arm is permanently
    inadmissible under the declared ceilings.
    """
    limits = vendor_ceilings() if ceilings is None else dict(ceilings)
    pending = list(slots)
    waves: list[list[RunSlot]] = []
    while pending:
        wave: list[RunSlot] = []
        deferred: list[RunSlot] = []
        for slot in pending:
            load = vendor_load([entry.arm for entry in wave])
            fits = all(load.get(vendor, 0) + 1 <= limits[vendor]
                       for vendor in slot.arm.vendors() if vendor in limits)
            (wave if fits else deferred).append(slot)
        if not wave:
            raise RuntimeError(
                f"no slot is admissible on an empty wave under ceilings {limits}: "
                f"a single run of {deferred[0].arm.slug} already exceeds one of them"
            )
        waves.append(wave)
        pending = deferred
    return waves


def without_recording(slot: RunSlot) -> RunSlot:
    """The same slot as a batched run: identical but for the recording flag.

    Exists so the claim that block 0 is a usable measurement is checkable rather than asserted in
    prose -- if any other field ever differed, this would no longer equal the batched slot.
    """
    return replace(slot, verbatim_recording=False)
