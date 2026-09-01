"""Deterministic tests for the science sweep's measurement design (``benchmarks/science_bench``).

No LLM, no network, no spend. What is pinned here is the design itself -- the balance of the
array, the opacity of a run id, the capability declarations, and the vendor ceilings -- because
every one of those is a property that reads as fine and fails silently. An unbalanced array still
runs; a readable run id still runs; a vision-less member in a figure seat still runs, right up
until the trial that pays for it.

The leak guard is written before there is anything to leak, on purpose. Two leaks shipped
undetected on 2026-08-27 and cost roughly $445 of invalidated measurement, and both would have been
caught on day one by a test asserting that no field an actor can reach varies with the condition.

Run from the repository root:
``conda run -n auto_research python -m pytest benchmarks/science_bench/tests``.
"""

import re
from collections import Counter
from dataclasses import fields

import pytest

from benchmarks.science_bench import arms
from benchmarks.science_bench.arms import (
    BLOCK0,
    BLOCK1,
    CODER,
    COLEAD,
    LEADS,
    REVIEWERS,
    WORKER_PAIRS,
    Arm,
    admissible,
    arm,
    campaign,
    confirm_block,
    run_id,
    schedule,
    vendor_ceilings,
    vendor_load,
)


def _anchor() -> Arm:
    return arm("opus", "fable", "w1")


# ── Block 1: the array is balanced as the design claims ─────────────────────
#
# Written literally because it is a hand-balanced fractional array rather than a product, which is
# exactly why the balance has to be asserted instead of eyeballed.


def test_block1_has_eleven_slots_over_nine_distinct_arms():
    assert len(BLOCK1) == 11
    assert len({slot.arm for slot in BLOCK1}) == 9


def test_each_lead_and_each_worker_pair_appears_three_times():
    leads = Counter(slot.arm.lead.slug for slot in BLOCK1)
    pairs = Counter(slot.arm.workers.slug for slot in BLOCK1)
    assert set(leads) == {seat.slug for seat in LEADS}
    assert set(pairs) == {pair.slug for pair in WORKER_PAIRS}
    # The anchor's two replicates land on lead `opus` and pair `w1`, so those carry 5 rather than 3.
    distinct = {slot.arm for slot in BLOCK1}
    assert Counter(a.lead.slug for a in distinct) == {"opus": 3, "sol": 3, "qwen": 3}
    assert Counter(a.workers.slug for a in distinct) == {"w1": 3, "w2": 3, "w3": 3}


def test_every_lead_by_worker_pair_combination_appears_exactly_once():
    combos = Counter((a.lead.slug, a.workers.slug) for a in {slot.arm for slot in BLOCK1})
    assert len(combos) == len(LEADS) * len(WORKER_PAIRS)
    assert set(combos.values()) == {1}


def test_the_reviewer_splits_five_four_across_the_slots():
    """Uneven by design: a computed array would recover the small efficiency loss, and the plan
    accepts it rather than hiding it."""
    reviewers = Counter(slot.arm.reviewer.slug for slot in BLOCK1)
    assert sorted(reviewers.values()) == [4, 7]  # 4 qwen, 7 fable (5 distinct + 2 replicates)
    assert Counter(a.reviewer.slug for a in {slot.arm for slot in BLOCK1}) == {"fable": 5, "qwen": 4}


def test_the_noise_floor_is_three_runs_of_one_arm():
    """Slots 1, 10 and 11 are the same treatment; being identical is the whole point."""
    anchor_slots = [slot for slot in BLOCK1 if slot.arm == _anchor()]
    assert [slot.index for slot in anchor_slots] == [1, 10, 11]
    assert [slot.replicate for slot in anchor_slots] == [1, 2, 3]


def test_lead_reviewer_correlation_is_derived_and_wider_than_the_plan_labelled():
    """Five of eleven slots share a vendor between the lead and the reviewer, not one.

    The plan's array labels only the Qwen/Qwen cell as lead-correlated, but the anchor is
    Anthropic lead with an Anthropic reviewer and is replicated three times, so the correlated
    set is slots 1, 3, 8, 10 and 11 over three distinct arms. Deriving the flag rather than
    annotating it is what surfaced that; the write-up reports the derived count.
    """
    assert [slot.index for slot in BLOCK1 if slot.arm.lead_correlated] == [1, 3, 8, 10, 11]
    assert len({slot.arm for slot in BLOCK1 if slot.arm.lead_correlated}) == 3
    assert not arm("sol", "fable", "w1").lead_correlated


# ── Run identity ────────────────────────────────────────────────────────────


def test_run_ids_are_unique_across_the_whole_campaign():
    """Hashing the combination alone would collide on the anchor's three slots -- the rows whose
    identity is that they are identical -- and again on block 2's three confirms."""
    block2 = confirm_block("sol", "fable", "w2", k=3)
    ids = [slot.run_id for slot in campaign(block2)]
    assert len(ids) == len(set(ids)) == 14


def test_a_run_id_is_stable_and_production_shaped():
    first, second = run_id(_anchor(), 1), run_id(_anchor(), 1)
    assert first == second
    assert re.fullmatch(r"[0-9a-f]{32}", first)


def test_replicate_index_is_one_based():
    with pytest.raises(ValueError):
        run_id(_anchor(), 0)


def test_two_arms_differing_in_one_seat_get_different_ids():
    assert run_id(arm("opus", "fable", "w1"), 1) != run_id(arm("opus", "fable", "w3"), 1)


# ── The leak guard ──────────────────────────────────────────────────────────
#
# A benchmark's entire value is that the actor cannot read its condition. An actor that can read
# its arm measures compliance, not capability.

_FORBIDDEN = tuple(
    {seat.slug for seat in (*LEADS, *REVIEWERS, CODER, COLEAD)}
    | {seat.model for seat in (*LEADS, *REVIEWERS, CODER, COLEAD)}
    | {seat.provider for seat in (*LEADS, *REVIEWERS, CODER, COLEAD)}
    | {pair.slug for pair in WORKER_PAIRS}
    | {member.model for pair in WORKER_PAIRS for member in pair.members}
    | {member.slug for pair in WORKER_PAIRS for member in pair.members}
    | {"science_bench", "bench", "block0", "block1", "block2", "anchor", "control"}
)


def test_no_run_id_carries_any_part_of_its_condition():
    block2 = confirm_block("qwen", "qwen", "w2", k=3)
    for slot in campaign(block2):
        for token in _FORBIDDEN:
            assert token not in slot.run_id, f"{token!r} leaks through run id of slot {slot.index}"


def test_a_run_id_is_the_only_identity_a_path_may_be_built_from():
    """The readable slug exists for the report, and this is the line that keeps it there.

    The 2026-08-27 leak was a temporary directory named after the leg: the read-only tool provider
    renders its roots into the ``read_file``/``list_dir`` descriptions the model reads, so a
    human-friendly directory name is prompt content. Production has never had this problem by
    accident -- its run ids are hex digests -- so the guard is that ours are too.
    """
    slug = BLOCK1[7].arm.slug
    assert "qwen" in slug  # the slug really is readable, which is why it must stay off disk
    assert slug not in BLOCK1[7].run_id


def test_every_slot_of_one_arm_shares_the_arm_and_differs_only_in_repetition():
    anchor_slots = [slot for slot in BLOCK1 if slot.arm == _anchor()]
    assert len({slot.arm for slot in anchor_slots}) == 1
    assert len({slot.run_id for slot in anchor_slots}) == 3


# ── Block 0 ─────────────────────────────────────────────────────────────────


def test_block0_is_block1_slot_one_rather_than_a_copy_of_it():
    """An alias, so the probe and the measurement it becomes cannot drift apart."""
    assert BLOCK0 is BLOCK1[0]
    assert BLOCK0.run_id == run_id(_anchor(), 1)


def test_block0_is_the_only_recorded_slot():
    assert [slot.index for slot in BLOCK1 if slot.verbatim_recording] == [1]


def test_block0_differs_from_a_batched_run_of_the_same_arm_only_in_bookkeeping():
    """Why a clean block 0 counts as a real measurement instead of a discarded probe.

    Compared against slot 10, which is an ordinary batched run of the identical arm: everything an
    actor is sent is a function of the arm, and the fields that differ are the harness's own
    bookkeeping plus the recording flag, which writes things down without sending anything.
    """
    batched = BLOCK1[9]
    assert batched.arm == BLOCK0.arm and batched.block == BLOCK0.block
    differing = {
        field.name
        for field in fields(BLOCK0)
        if getattr(BLOCK0, field.name) != getattr(batched, field.name)
    }
    assert differing == {"index", "replicate", "note", "verbatim_recording"}


# ── Capability declarations ─────────────────────────────────────────────────


def test_every_figure_bearing_seat_is_vision_declared():
    """The reviewer, the lead and the worker are all sent figures; the co-lead and coder are not."""
    figure_seats = [*LEADS, *REVIEWERS, *(m for p in WORKER_PAIRS for m in p.members)]
    assert all(seat.vision for seat in figure_seats)


def test_the_text_only_zai_flagship_is_in_no_figure_seat():
    figure_models = {
        seat.model for seat in (*LEADS, *REVIEWERS, *(m for p in WORKER_PAIRS for m in p.members))
    }
    assert "glm-5.3" not in figure_models
    assert "glm-5.3-flash" in figure_models


def test_the_reviewer_without_native_pdf_declares_it():
    """Declared as data so the harness can act on it, rather than living in a caveat."""
    qwen = next(seat for seat in REVIEWERS if seat.provider == "dashscope")
    assert qwen.documents is False
    fable = next(seat for seat in REVIEWERS if seat.provider == "anthropic")
    assert fable.documents is True


def test_each_worker_pair_is_provider_decorrelated():
    for pair in WORKER_PAIRS:
        assert len(set(pair.providers())) == 2, pair.slug


def test_every_axis_vendor_has_pinned_parameters():
    """A vendor without an entry silently compares a pinned member against a defaulted one."""
    vendors = {seat.provider for seat in (*LEADS, *REVIEWERS, CODER, COLEAD)}
    vendors |= {m.provider for p in WORKER_PAIRS for m in p.members}
    for vendor in vendors:
        assert arms._PINNED_PARAMETERS.get(vendor), vendor


def test_prices_are_read_or_declared_unread_never_invented():
    """Every non-litellm model on an axis is either priced from its vendor's page or named as
    unread. A guessed rate is indistinguishable from a read one once it is in a cost table."""
    litellm_priced = {"anthropic", "openai", "deepseek"}
    for seat in (*LEADS, *REVIEWERS, CODER, COLEAD, *(m for p in WORKER_PAIRS for m in p.members)):
        if seat.provider in litellm_priced:
            continue
        assert seat.price() or seat.model in arms.PRICES_UNREAD, seat.model


# ── The pinned seats ────────────────────────────────────────────────────────


def test_the_author_and_tool_reviewer_are_the_same_in_every_arm():
    for slot in BLOCK1:
        assert slot.arm.coder is CODER
        assert slot.arm.colead is COLEAD


def test_the_tool_reviewer_is_decorrelated_from_the_author():
    assert COLEAD.provider != CODER.provider


def test_the_author_is_a_codex_harness_member_with_its_own_endpoint():
    assert arms.CODER_HARNESS == "codex"
    assert arms.CODER_KEY_ENV == "DEEPSEEK_API_KEY"
    assert arms.CODER_BASE_URL


# ── Vendor concurrency ──────────────────────────────────────────────────────


def test_a_multi_seat_same_vendor_arm_counts_once():
    """Slot 8 seats Qwen as lead, as reviewer and as a worker; the graph dispatches its roles in
    phases, so counting it three times would idle the batch for contention that does not happen."""
    correlated = BLOCK1[7].arm
    assert sum(1 for seat in correlated.seats() if seat.provider == "dashscope") == 3
    assert vendor_load([correlated])["dashscope"] == 1


def test_every_run_loads_the_pinned_vendors():
    load = vendor_load([slot.arm for slot in BLOCK1])
    assert load["deepseek"] == len(BLOCK1)   # the author
    assert load["openai"] >= len(BLOCK1)     # the tool reviewer, in every run


def test_the_measured_zai_ceiling_is_declared_and_enforced():
    assert vendor_ceilings()["zai"] == 2
    zai_arms = [slot.arm for slot in BLOCK1 if "zai" in slot.arm.vendors()]
    assert len(zai_arms) == 6, "six of eleven slots carry a Z.AI worker"
    assert admissible(zai_arms[:1], zai_arms[1])
    assert not admissible(zai_arms[:2], zai_arms[2])


def test_an_uncapped_vendor_never_blocks():
    uncapped = [slot.arm for slot in BLOCK1 if "zai" not in slot.arm.vendors()]
    assert admissible(uncapped, uncapped[0])


def test_the_whole_block_schedules_under_the_declared_ceilings():
    """Terminates, and no wave is empty -- which is what proves no arm is permanently
    inadmissible. A global concurrency number cannot express this: every run calls the author and
    the tool reviewer, and six of eleven carry the one vendor with a measured ceiling."""
    waves = schedule(BLOCK1)
    assert sum(len(wave) for wave in waves) == len(BLOCK1)
    assert all(wave for wave in waves)
    for wave in waves:
        load = vendor_load([slot.arm for slot in wave])
        for vendor, ceiling in vendor_ceilings().items():
            assert load.get(vendor, 0) <= ceiling


def test_a_ceiling_no_single_run_can_meet_is_reported_rather_than_looping():
    with pytest.raises(RuntimeError):
        schedule(BLOCK1, ceilings={"deepseek": 0})


# ── Block 2 ─────────────────────────────────────────────────────────────────


def test_confirm_block_continues_the_replicate_numbering_of_its_arm():
    """Re-confirming the incumbent adds runs rather than colliding with the three it has."""
    confirm = confirm_block("opus", "fable", "w1", k=3)
    assert [slot.replicate for slot in confirm] == [4, 5, 6]
    assert not {s.run_id for s in confirm} & {s.run_id for s in BLOCK1}


def test_confirm_block_may_assemble_a_combination_no_block1_cell_ran():
    confirm = confirm_block("sol", "fable", "w1", k=3)
    assert confirm[0].arm not in {slot.arm for slot in BLOCK1}
    assert [slot.replicate for slot in confirm] == [1, 2, 3]


def test_an_unknown_level_fails_loudly():
    with pytest.raises(KeyError):
        arm("opus", "fable", "w9")


def test_block2_records_why_it_is_unassigned():
    assert "confirm_block" in arms.BLOCK2_UNASSIGNED
