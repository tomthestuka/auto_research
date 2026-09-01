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

import asyncio
import csv
import os
import json
import re
from collections import Counter
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from benchmarks.science_bench import __main__ as cli
from benchmarks.science_bench import arms, environment, request, trial
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


# ── The per-trial environment ───────────────────────────────────────────────
#
# What a trial is configured with is asserted as data, without materialising anything, because the
# expensive parts (a toolkit clone, a data copy) are not what can silently be wrong. What can
# silently be wrong is a path that was not redirected, or an allow-list still naming the shared
# roots -- both of which produce a trial that runs and measures the wrong thing.

_SOURCE_ORCHESTRATION = {
    "runtime_dir": "/shared/runtime",
    "papers_dir": "/shared/papers",
    "toolkit_root": "/shared/toolkit",
    "max_iterations": 3,
    "tool_author_branch": "ai",
    "agent_os_jail": True,
    "agent_fs_allow": [
        "/shared/toolkit", "/shared/obsdb", "/shared/runtime",
        "/opt/conda/envs/astropy", "/opt/heasoft", "/man1/CALDB", "/tmp",
    ],
    "agent_fs_readonly": ["/opt/conda/envs/astropy", "/opt/heasoft", "/man1/CALDB"],
}

_SOURCE_MODELS = {
    "roles": {
        "reviewer": {"members": [{"name": "old", "provider": "openai", "model": "old-reviewer"}]},
        "analyst_lead": {"members": [{"name": "old", "provider": "openai", "model": "old-lead"}]},
        "analyst_worker": {"members": [{"name": "old", "provider": "openai", "model": "old-w"}]},
        "analyst_colead": {"members": [{"name": "old", "provider": "openai", "model": "old-cl"}]},
        "coder": {"members": [{"name": "old", "provider": "anthropic", "model": "old-coder"}]},
        "librarian_io": {"members": [{"name": "io", "provider": "google", "model": "keep-me"}]},
        "status_monitor": {"members": [{"name": "s", "provider": "openai", "model": "keep-me-2"}]},
    }
}


def _orchestration(**kwargs):
    from pathlib import Path as _P

    defaults = dict(
        toolkit_root=_P("/trial/toolkit"), runtime_dir=_P("/trial/runtime"),
        papers_dir=_P("/trial/papers"), obs_root=_P("/trial/obs"),
    )
    defaults.update(kwargs)
    return environment.orchestration_config(_SOURCE_ORCHESTRATION, **defaults)


def test_the_four_paths_a_trial_owns_are_redirected():
    config = _orchestration()
    assert config["toolkit_root"] == "/trial/toolkit"
    assert config["runtime_dir"] == "/trial/runtime"
    assert config["papers_dir"] == "/trial/papers"


def test_every_cap_and_flag_is_inherited_untouched():
    """The pipeline is what is being measured; only where it reads and writes may differ."""
    config = _orchestration()
    assert config["max_iterations"] == 3
    assert config["agent_os_jail"] is True
    assert config["tool_author_branch"] == "ai"


def test_the_allow_list_names_the_private_roots_and_drops_the_shared_ones(monkeypatch):
    """The failure this prevents: a trial pointed at private paths whose allow-list still names
    the shared ones has every one of its own reads denied, and under the kernel jail its data is
    not even mounted."""
    monkeypatch.setattr(environment, "_source_obs_roots", lambda config: ["/shared/obsdb"])
    allow = _orchestration()["agent_fs_allow"]
    assert allow[:4] == ["/trial/toolkit", "/trial/runtime", "/trial/papers", "/trial/obs"]
    assert not [entry for entry in allow if entry.startswith("/shared")]
    # The machine's own roots -- the science stack and its calibration data -- are untouched.
    assert "/opt/conda/envs/astropy" in allow and "/man1/CALDB" in allow


def test_the_read_only_roots_survive_and_gain_nothing_private():
    readonly = _orchestration()["agent_fs_readonly"]
    assert readonly == ["/opt/conda/envs/astropy", "/opt/heasoft", "/man1/CALDB"]


def test_the_source_configuration_is_not_mutated():
    before = json.dumps(_SOURCE_ORCHESTRATION, sort_keys=True)
    _orchestration()
    assert json.dumps(_SOURCE_ORCHESTRATION, sort_keys=True) == before


def test_the_five_arm_seats_are_written_and_the_rest_left_alone():
    config = environment.models_config(_anchor(), _SOURCE_MODELS)
    roles = config["roles"]
    assert roles["analyst_lead"]["members"][0]["model"] == "claude-opus-5"
    assert roles["reviewer"]["members"][0]["model"] == "claude-fable-5"
    assert [m["model"] for m in roles["analyst_worker"]["members"]] == [
        "claude-sonnet-5", "gpt-5.6-terra"
    ]
    assert roles["analyst_colead"]["members"][0]["model"] == COLEAD.model
    assert roles["coder"]["members"][0]["model"] == CODER.model
    # Not on any axis: varying them would add a factor nobody is measuring.
    assert roles["librarian_io"] == _SOURCE_MODELS["roles"]["librarian_io"]
    assert roles["status_monitor"] == _SOURCE_MODELS["roles"]["status_monitor"]


def test_the_worker_role_carries_both_members_not_one():
    """Two workers is what makes the debate a debate; writing one would change the pipeline."""
    config = environment.models_config(arm("qwen", "qwen", "w2"), _SOURCE_MODELS)
    assert len(config["roles"]["analyst_worker"]["members"]) == 2


def test_the_author_member_carries_its_harness_endpoint_and_key():
    coder = environment.models_config(_anchor(), _SOURCE_MODELS)["roles"]["coder"]["members"][0]
    assert coder["harness"] == arms.CODER_HARNESS
    assert coder["base_url"] == arms.CODER_BASE_URL
    assert coder["key_env"] == arms.CODER_KEY_ENV


def test_every_seat_carries_its_pinned_effort():
    roles = environment.models_config(_anchor(), _SOURCE_MODELS)["roles"]
    lead = roles["analyst_lead"]["members"][0]
    colead = roles["analyst_colead"]["members"][0]
    assert lead["parameters"] == {"thinking": {"type": "adaptive"}, "max_tokens": 21000}
    assert colead["parameters"]["reasoning"] == {"effort": arms.PINNED_EFFORT}


def test_capability_flags_reach_the_written_member():
    """A declaration that does not survive into the config is a declaration that does nothing."""
    roles = environment.models_config(arm("opus", "qwen", "w1"), _SOURCE_MODELS)["roles"]
    assert roles["reviewer"]["members"][0]["documents"] is False   # no native PDF
    assert roles["reviewer"]["members"][0]["vision"] is True


def test_the_observation_configuration_is_one_private_root():
    from pathlib import Path as _P

    assert environment.observations_config(_P("/trial/obs")) == {"obs_db_root": "/trial/obs"}


# ── The archive filter ──────────────────────────────────────────────────────
#
# The single most consequential thing in this module. The master's event directory for the May
# observation holds 1,023 files, six of which are the archive's; the rest are earlier runs'
# products, including the polarization cubes this question exists to compute.


def test_the_event_lists_kept_are_the_archive_s_and_the_preprocessing_script_s():
    """The line is 'is it a measurement', not 'did a human make it'.

    The `_rej` lists come from the group's own `filter_inst_bkg_IXPE.sh`, and the pinned toolkit's
    loader globs *only* them -- an archive-only clone made `load_data` fail outright, which cost
    block 0 its whole first authoring cycle and would have cost all 14 arms the same.
    """
    kept = [
        "ixpe01002901_det1_evt2_v01.fits",            # heasarc
        "ixpe01002901_det1_evt2_v01.fits.gz",         # heasarc
        "ixpe01002901_det3_evt2_v02.fits",            # heasarc
        "ixpe01002901_det1_evt2_v01_rej.fits",        # filter_inst_bkg_IXPE.sh
        "ixpe01002901_det2_evt2_v01_rej.fits",
    ]
    measurements = [
        "ixpe01002901_det1_evt2_v01_rej_01002901s000src_pcube.fits",  # the answer, precomputed
        "ixpe01002901_det1_evt2_v01_rej_01002901s000src.fits",        # a time-segment cut
        "ixpe01002901_det1_evt2_v01_rej_src_pha1.fits",
        "ixpe01002901_det1_evt2_v01_bkg.fits",
        "ixpe01002901_det1_evt2_v01_bkg_pcube.fits",
        "pcube_timeseg_01002901.csv",
    ]
    assert all(environment.RAW_EVENT_LIST.match(n) for n in kept), "a kept file was filtered out"
    assert not any(environment.RAW_EVENT_LIST.match(n) for n in measurements)


def test_the_loader_at_the_pinned_base_can_find_what_the_clone_keeps():
    """Pins the coupling that broke block 0: the toolkit globs `*evt2_v[0-9][0-9]_rej.fits`, so the
    clone must keep at least one file matching that or `load_data` raises `No files found`."""
    import re as _re

    toolkit_glob = _re.compile(r"^.*evt2_v[0-9][0-9]_rej\.fits$")
    kept = [n for n in ("ixpe01002901_det1_evt2_v01.fits",
                        "ixpe01002901_det1_evt2_v01_rej.fits")
            if environment.RAW_EVENT_LIST.match(n)]
    assert any(toolkit_glob.match(n) for n in kept)


def test_a_copied_observation_carries_the_archive_and_none_of_the_answers(tmp_path):
    source = tmp_path / "master" / "01002901"
    (source / "event_l2").mkdir(parents=True)
    (source / "auxil").mkdir()
    (source / "hk").mkdir()
    (source / "event_l1").mkdir()
    (source / "event_l2" / "ixpe01002901_det1_evt2_v01.fits").write_text("events")
    (source / "event_l2" / "ixpe01002901_det1_evt2_v01_rej.fits").write_text("filtered events")
    (source / "event_l2" / "ixpe01002901_det1_evt2_v01_rej_src_pcube.fits").write_text("ANSWER")
    (source / "event_l2" / "ixpe01002901_det1_evt2_v01_rej_src_pha1.fits").write_text("derived")
    (source / "auxil" / "ixpe01002901_det1_chgmap1_v02.fits.gz").write_text("aux")
    (source / "hk" / "ixpe01002901_all_adc_0100_v02.fits.gz").write_text("hk")
    (source / "event_l1" / "huge.fits").write_text("level one")
    (source / "README").write_text("readme")
    (source / "src.reg").write_text("a previous run's region file")

    counts = environment.copy_observation(source, tmp_path / "trial")
    landed = {p.name for p in (tmp_path / "trial").rglob("*") if p.is_file()}

    assert "ixpe01002901_det1_evt2_v01.fits" in landed
    assert "ixpe01002901_det1_evt2_v01_rej.fits" in landed   # the loader needs it
    assert {"ixpe01002901_det1_chgmap1_v02.fits.gz", "ixpe01002901_all_adc_0100_v02.fits.gz",
            "README"} <= landed
    assert not [name for name in landed if "pcube" in name or "pha1" in name]
    assert "src.reg" not in landed
    assert "huge.fits" not in landed          # Level 1, deliberately absent
    assert counts["skipped"] == 2


def test_the_copy_is_a_copy_and_cannot_reach_the_master(tmp_path):
    """Hardlinks would share inodes with a 1.2 TB archive whose own prescription tells the tree to
    edit FITS headers in place. A trial writing over its input must not reach the original."""
    source = tmp_path / "master" / "01002901"
    (source / "event_l2").mkdir(parents=True)
    original = source / "event_l2" / "ixpe01002901_det1_evt2_v01.fits"
    original.write_text("archive bytes")

    environment.copy_observation(source, tmp_path / "trial")
    copied = tmp_path / "trial" / "event_l2" / "ixpe01002901_det1_evt2_v01.fits"
    assert copied.stat().st_ino != original.stat().st_ino
    copied.write_text("a trial overwrote its input in place")
    assert original.read_text() == "archive bytes"


def test_an_archive_layout_that_no_longer_matches_fails_loudly(tmp_path):
    """Copying nothing must not read as a clean trial: it would run, and fail as science."""
    source = tmp_path / "master" / "IXPE" / "01002901"
    (source / "event_l2").mkdir(parents=True)
    (source / "event_l2" / "unrecognised_naming.fits").write_text("x")
    with pytest.raises(RuntimeError, match="copied nothing"):
        environment.build_observation_database(
            tmp_path / "trial", tmp_path / "master", ("01002901",), "IXPE", tmp_path
        )


def test_a_missing_observation_is_named(tmp_path):
    (tmp_path / "master" / "IXPE").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="01002901"):
        environment.build_observation_database(
            tmp_path / "trial", tmp_path / "master", ("01002901",), "IXPE", tmp_path
        )


# ── The launch environment ──────────────────────────────────────────────────


def test_a_trial_process_is_launched_with_the_seam_pointing_at_its_own_config(tmp_path):
    from auto_research.config_paths import CONFIG_DIR_ENV

    env = environment.TrialEnvironment(
        run_id="deadbeef", root=tmp_path, config_dir=tmp_path / "config",
        toolkit_root=tmp_path / "toolkit", runtime_dir=tmp_path / "runtime",
        papers_dir=tmp_path / "papers", obs_root=tmp_path / "obs", base_sha="abc123",
    )
    assert env.process_env({"PATH": "/usr/bin"})[CONFIG_DIR_ENV] == str(tmp_path / "config")
    # Addressed from the trial's own runtime directory, which is how the batch watches a run it
    # is not itself configured like.
    assert env.run_dir == tmp_path / "runtime" / "runs" / "deadbeef"


# ── The frozen question ─────────────────────────────────────────────────────

_ANSWER_KEY = Path(__file__).resolve().parents[3] / "papers" / "FANGZ" / (
    "data_benchmark_compare_krawczynski2022.csv"
)


def test_the_question_validates_as_the_pipeline_s_own_request_type():
    posed = request.load_request()
    assert posed.question.strip() and posed.context.strip()
    assert "Cyg X-1" in posed.question


def test_the_question_is_the_recovered_one_not_one_written_for_the_benchmark():
    provenance = request.request_provenance()
    assert provenance["prior_run_id"] == "bf1e39e079c79282b28e36af13492edb"
    assert "usage_example.ipynb" in provenance["recovered_from"]
    assert provenance["project_id"] == "cygx1_reproduce_krawczynski2022_polarization"


def test_the_hash_is_pinned():
    """It is stamped into every batch. An instrument revised in the light of its own results
    absorbs the variance it exists to detect, so a change here has to be visible."""
    assert request.request_sha256() == (
        "c2ffc48f4f786afef69eef63ad40124359b539c09711bbbf18b22818a4186fb6"
    )


def test_there_are_fourteen_quantities_and_they_are_the_answer_key_s_own():
    """Extraction is a keyed lookup, so the two lists must be identical rather than merely
    similar -- a label that drifts scores as a missing measurement."""
    rows = [
        line for line in _ANSWER_KEY.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    key = list(csv.DictReader(rows))
    assert len(key) == 14
    assert [q["label"] for q in request.quantities()] == [row["label"] for row in key]
    assert [q["unit"] for q in request.quantities()] == [row["unit"] for row in key]


def test_every_quantity_is_named_in_the_question_the_tree_reads():
    posed = request.load_request()
    for quantity in request.quantities():
        assert quantity["label"] in posed.question, quantity["label"]


def test_the_question_asks_for_the_artifact_each_number_came_from():
    """What separates a number this run measured from one transcribed out of the paper."""
    assert "artifact" in request.load_request().question.lower()


def test_the_question_makes_an_unmeasured_quantity_a_correct_answer():
    """The alternative to inventing one. It has to be said, not implied."""
    question = request.load_request().question
    assert "NOT MEASURED" in question
    assert "fabricated" in question.lower()


def test_the_question_forbids_tuning_toward_the_published_values():
    posed = request.load_request()
    assert "never substitute, adjust toward, or back-fill" in posed.question
    assert "NEVER a target to match" in posed.context


def test_the_added_step_is_the_only_difference_from_the_recovered_question():
    """Everything before it is the 2026-07-10 question byte for byte, which is what makes that
    run a reference point rather than a different experiment."""
    question = request.load_request().question
    head, marker, tail = question.partition("\n\nSTEP 10 - NAMED-QUANTITY REPORT")
    assert marker and tail
    assert head.rstrip().endswith(
        "Imaging X-ray Polarimetry Explorer: prelaunch "
        "(https://ui.adsabs.harvard.edu/abs/2022JATIS...8b6002W)"
    )


def test_the_answer_key_is_not_on_any_path_an_actor_may_read():
    """Tamper hygiene. The published values are public and the tree is told to read them; what
    must not happen is the scoring key sitting inside a root the trial is handed."""
    private = _orchestration()["agent_fs_allow"]
    assert not any(str(_ANSWER_KEY).startswith(root.rstrip("/") + "/") for root in private)


# ── The trial driver ────────────────────────────────────────────────────────


def _ledger(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def test_the_command_is_the_production_one_and_closes_the_human_channel():
    """Left unset, the channel resolver wires a terminal handler whenever it finds a tty, so a
    batch launched from a shell would silently acquire a human."""
    argv = trial.build_argv("deadbeef", chatter=4)
    assert argv[1:3] == ["-m", "auto_research"]
    assert argv[argv.index("--run-id") + 1] == "deadbeef"
    assert argv[argv.index("--human") + 1] == "none"
    assert "Cyg X-1" in argv[3]
    assert "--context" in argv


def test_the_question_reaches_the_command_whole():
    argv = trial.build_argv("deadbeef", chatter=4)
    assert argv[3] == request.load_request().question
    assert "STEP 10 - NAMED-QUANTITY REPORT" in argv[3]


def test_the_ledger_gives_the_rounds_and_the_spend(tmp_path):
    path = _ledger(tmp_path / "monitor.jsonl", [
        {"event": "gate.result", "data": {"passed": False, "failing_checks": ["no_fabrication:x"]}},
        {"event": "gate.result", "data": {"passed": True, "failing_checks": []}},
        {"event": "review.result", "data": {"approved": False, "issues": ["a bug"]}},
        {"event": "review.result", "data": {"approved": True}},
        # The real shape, from block 0's ledger. The invented one this test used to carry
        # (`analyst.tool.authored` with a top-level `tool`) is what let the reader defect ship: a
        # test written against a fabricated event shape validates the fabrication.
        {"event": "commit", "data": {"branch": "ai", "tool": "ixpe_pcube_repro",
                                     "commit_sha": "abc1234567def"}},
        {"event": "analyst.worker.revision", "data": {}},
        {"event": "cost.call", "data": {"role": "analyst_lead", "total_cost_usd": 1.5}},
        {"event": "cost.call", "data": {"role": "coder", "total_cost_usd": 0.25}},
    ])
    summary = trial.summarise_ledger(path)
    assert (summary["gate_runs"], summary["gate_passes"], summary["gate_rejections"]) == (2, 1, 1)
    assert (summary["review_approvals"], summary["review_rejections"]) == (1, 1)
    assert summary["tools_committed"] == ["ixpe_pcube_repro"]
    assert summary["no_fabrication_hits"] == ["no_fabrication:x"]
    assert summary["debate_rounds"] == 1
    assert summary["cost_by_role"] == {"analyst_lead": 1.5, "coder": 0.25}


def test_the_ledger_counts_how_often_the_tree_wanted_a_human(tmp_path):
    """With the channel closed this is the measurable form of a raise, and it is what bounds the
    unattended claim in the write-up."""
    path = _ledger(tmp_path / "monitor.jsonl", [
        {"event": "coder.ask_human", "data": {}},
        {"event": "coder.ask_human.unattended", "data": {}},
        {"event": "hitl.command", "data": {}},
        {"event": "cost.call", "data": {"role": "r", "total_cost_usd": 1.0}},
    ])
    assert trial.summarise_ledger(path)["wanted_a_human"] == 3


def test_a_half_written_ledger_still_reads(tmp_path):
    """A batch may read a ledger the run is still appending to."""
    path = tmp_path / "monitor.jsonl"
    path.write_text(
        json.dumps({"event": "gate.result", "data": {"passed": True}}) + "\n{\"event\": \"gat",
        encoding="utf-8",
    )
    assert trial.summarise_ledger(path)["gate_passes"] == 1


def test_an_absent_ledger_is_empty_not_an_error(tmp_path):
    assert trial.summarise_ledger(tmp_path / "nothing.jsonl") == {}


def test_infrastructure_and_measurement_are_separate(tmp_path):
    """The distinction the whole scoring rests on. A run that finished and refused is a *correct*
    outcome that happens to say no; a run that crashed is the harness's fault and re-runnable."""
    refused = trial.measured_outcome(
        {"status": "done", "verdict": {"accepted": False, "summary": "not verifiable"}},
        {"worker_statuses": ["needs_human"]},
    )
    assert refused["accepted"] is False
    assert refused["needs_human"] is True
    assert refused["verdict_summary"] == "not verifiable"


def test_a_run_that_wrote_no_marker_reads_as_died_not_as_a_verdict():
    assert trial.measured_outcome({}, {})["accepted"] is None


def test_the_stamps_name_the_code_the_base_the_gate_and_the_question(tmp_path):
    from auto_research.config_paths import orchestration_yaml

    if not orchestration_yaml().exists():
        pytest.skip("no live configuration on this machine")
    source = Path(yaml.safe_load(orchestration_yaml().read_text())["toolkit_root"])
    if not (source / ".git").exists():
        pytest.skip("no toolkit checkout on this machine")
    stamps = trial.collect_stamps(source)
    assert re.fullmatch(r"[0-9a-f]{40}", stamps["auto_research_sha"])
    assert re.fullmatch(r"[0-9a-f]{40}", stamps["toolkit_base_sha"])
    assert stamps["request_sha256"] == request.request_sha256()
    assert stamps["human_channel"].startswith("none")


def test_a_broken_trial_is_recorded_rather_than_raised(tmp_path, monkeypatch):
    """One failure must not end a batch of fourteen multi-hour runs."""
    def _explode(*args, **kwargs):
        raise RuntimeError("no disk")

    monkeypatch.setattr(trial.env_mod, "materialize", _explode)
    cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=tmp_path / "out")
    record = asyncio.run(trial.run_trial(BLOCK1[1], cfg))
    assert record["infrastructure"] == "error"
    assert "no disk" in record["error"]
    assert record["run_id"] == BLOCK1[1].run_id


def test_a_record_names_its_arm_for_the_human_and_its_run_by_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(trial.env_mod, "materialize",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")))
    cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=tmp_path / "out")
    record = asyncio.run(trial.run_trial(BLOCK1[7], cfg))
    assert record["lead"] == "qwen3.8-max" and record["reviewer"] == "qwen3.8-max"
    assert record["workers"] == ["qwen3.8-max", "glm-5.3-flash"]
    assert record["lead_correlated"] is True
    assert record["run_id"] == BLOCK1[7].run_id


def test_records_append_and_survive_being_read_mid_batch(tmp_path):
    trial.append_record(tmp_path, {"slot": 1, "infrastructure": "done"})
    trial.append_record(tmp_path, {"slot": 2, "infrastructure": "died"})
    (tmp_path / "records.jsonl").open("a").write('{"slot": 3, "infra')
    loaded = trial.load_records(tmp_path)
    assert [r["slot"] for r in loaded] == [1, 2]


def test_no_record_field_an_actor_could_reach_carries_the_arm(tmp_path, monkeypatch):
    """The record is the human's and is full of seat names; the *paths* in it must not be."""
    monkeypatch.setattr(trial.env_mod, "materialize",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=tmp_path / "out")
    record = asyncio.run(trial.run_trial(BLOCK1[7], cfg))
    for key in ("run_id", "trial_root", "run_dir"):
        value = str(record.get(key, ""))
        for token in _FORBIDDEN:
            assert token not in value, f"{token!r} leaks through record[{key!r}]"


# ── The CLI, the scheduler and the preflight ────────────────────────────────


def test_block0_is_selected_alone_and_is_the_anchor_slot():
    args = cli.parse_args(["--block0"])
    assert args.block0 is True
    assert BLOCK0.verbatim_recording is True


def test_slot_selection_accepts_ranges_lists_and_the_whole_block():
    assert [s.index for s in cli.parse_slots(None)] == list(range(1, 12))
    assert [s.index for s in cli.parse_slots("2-11")] == list(range(2, 12))
    assert [s.index for s in cli.parse_slots("1,4,7")] == [1, 4, 7]


def test_a_slot_that_does_not_exist_is_refused():
    with pytest.raises(SystemExit):
        cli.parse_slots("99")


def test_artifacts_are_kept_unless_asked_otherwise():
    """They are the entire input to the offline scoring pass."""
    assert cli.parse_args([]).sweep_artifacts is False


def test_the_preflight_refuses_a_shell_already_pointed_elsewhere(monkeypatch, tmp_path):
    """The batch reads the live configuration as its source; started already redirected it would
    clone the wrong toolkit and read the wrong archive."""
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(tmp_path))
    problems = cli.preflight([BLOCK0], workspace=tmp_path / "ws")
    assert any("AUTO_RESEARCH_CONFIG_DIR is set" in p for p in problems)


def test_the_preflight_refuses_a_workspace_an_actor_could_read(monkeypatch, tmp_path):
    """A trial's configuration is the one file naming its whole arm."""
    monkeypatch.delenv("AUTO_RESEARCH_CONFIG_DIR", raising=False)
    orchestration = tmp_path / "config" / "orchestration.yaml"
    orchestration.parent.mkdir(parents=True)
    orchestration.write_text(
        yaml.safe_dump({"toolkit_root": str(tmp_path / "tk"),
                        "agent_fs_allow": [str(tmp_path / "inside")]}),
        encoding="utf-8",
    )
    (tmp_path / "config" / "observations.yaml").write_text(
        f"obs_db_root: {tmp_path / 'obs'}\n", encoding="utf-8")
    (tmp_path / "config" / "code_gate.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        cli, "preflight",
        cli.preflight,  # keep the real one; the env check below is what we bypass
    )
    # Bypass only the "already pointed elsewhere" guard, which is what we are using to install a
    # synthetic configuration in the first place.
    real_environ_get = os.environ.get
    monkeypatch.setattr(
        os.environ, "get",
        lambda key, default=None: (None if key == "AUTO_RESEARCH_CONFIG_DIR"
                                   else real_environ_get(key, default)),
    )
    problems = cli.preflight([BLOCK0], workspace=tmp_path / "inside" / "ws")
    assert any("could read its own configuration" in p for p in problems)


def test_the_scheduler_never_exceeds_a_vendor_ceiling(monkeypatch, tmp_path):
    """Six of eleven slots carry the capped vendor, so a global concurrency number is not enough."""
    in_flight: list[int] = []
    peak = {"zai": 0}

    async def _fake_trial(slot, cfg):
        in_flight.append(slot.index)
        live = [s for s in BLOCK1 if s.index in in_flight]
        zai = sum(1 for s in live if "zai" in s.arm.vendors())
        peak["zai"] = max(peak["zai"], zai)
        await asyncio.sleep(0)
        in_flight.remove(slot.index)
        return {"slot": slot.index, "infrastructure": "done", "cost_usd": 0.0, "duration_s": 1}

    monkeypatch.setattr(cli.trial, "run_trial", _fake_trial)
    cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=tmp_path / "out")
    records = asyncio.run(cli.run_batch(list(BLOCK1), cfg, concurrency=8))
    assert sorted(r["slot"] for r in records) == list(range(1, 12))
    assert peak["zai"] <= arms.vendor_ceilings()["zai"]


def test_the_scheduler_honours_the_global_limit_too(monkeypatch, tmp_path):
    peak = {"n": 0}
    live: list[int] = []

    async def _fake_trial(slot, cfg):
        live.append(slot.index)
        peak["n"] = max(peak["n"], len(live))
        await asyncio.sleep(0)
        live.remove(slot.index)
        return {"slot": slot.index, "infrastructure": "done", "cost_usd": 0.0, "duration_s": 1}

    monkeypatch.setattr(cli.trial, "run_trial", _fake_trial)
    cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=tmp_path / "out")
    asyncio.run(cli.run_batch(list(BLOCK1), cfg, concurrency=2))
    assert peak["n"] <= 2


def test_every_record_is_on_disk_the_moment_its_trial_ends(monkeypatch, tmp_path):
    """A batch killed at hour thirty loses only what was in flight."""
    async def _fake_trial(slot, cfg):
        return {"slot": slot.index, "infrastructure": "done", "cost_usd": 1.0, "duration_s": 60}

    monkeypatch.setattr(cli.trial, "run_trial", _fake_trial)
    out = tmp_path / "out"
    cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=out)
    asyncio.run(cli.run_batch(list(BLOCK1[:3]), cfg, concurrency=3))
    assert len(trial.load_records(out)) == 3
    assert "3 trial(s) recorded" in cli.batch_status(out)


def test_status_of_a_directory_with_nothing_in_it_says_so(tmp_path):
    assert "no records yet" in cli.batch_status(tmp_path)


# ── The path guard, and the run that paid for it ────────────────────────────


def test_the_campaign_s_own_name_is_caught_in_a_path():
    """The leak that killed block 0's first launch, pinned as a regression.

    The workspace was called ``science_bench``, so every downloaded-paper path in the librarian's
    verbatim prompt read ``/data/.../science_bench/<hash>/papers/...``. It gives away no arm, and
    that is not the point: a model that knows it is inside a benchmark behaves differently in
    every seat.
    """
    leaks = arms.forbidden_in_path("/data/fzhou/ar_runtime/science_bench/abc123/papers/x.pdf")
    assert "science_bench" in leaks


def test_seat_identities_are_caught_as_whole_tokens_only():
    """A vendor name is three letters; a guard that fires on `zaire` is one somebody disables."""
    assert set(arms.forbidden_in_path("/tmp/lead_opus_rev_fable_w1/x")) >= {"opus", "fable", "w1"}
    assert arms.forbidden_in_path("/home/charmander/zairestudy/runs") == []


def test_an_ordinary_opaque_workspace_is_clean():
    assert arms.forbidden_in_path("/data/fzhou/ar_runtime/ws/deadbeef00/toolkit") == []


def test_materialize_refuses_a_leaking_workspace_before_it_builds_anything(tmp_path):
    """Structural, not editorial: no future workspace choice can leak past this.

    Exercised through ``materialize`` itself rather than through a stub, because the first version
    of this guard passed while the real thing leaked -- the test that should have caught it had
    monkeypatched away the very function that builds the paths.
    """
    leaking = tmp_path / "science_bench"
    with pytest.raises(ValueError, match="science_bench"):
        environment.materialize(BLOCK0.arm, BLOCK0.run_id,
                                workspace_root=leaking, base_ref="HEAD")
    assert not (leaking / BLOCK0.run_id).exists(), "it must refuse before building anything"


def test_the_default_workspace_says_nothing_about_the_experiment():
    assert arms.forbidden_in_path(cli.DEFAULT_WORKSPACE) == []


def test_every_path_a_trial_is_handed_is_clean(tmp_path):
    """Not just the root: the four directories under it are what actors actually read."""
    env = environment.TrialEnvironment(
        run_id="fbb93f2600d620b26baa9c71dc5e53d7", root=tmp_path / "ws" / "fbb93f26",
        config_dir=tmp_path / "ws" / "fbb93f26" / "config",
        toolkit_root=tmp_path / "ws" / "fbb93f26" / "toolkit",
        runtime_dir=tmp_path / "ws" / "fbb93f26" / "runtime",
        papers_dir=tmp_path / "ws" / "fbb93f26" / "papers",
        obs_root=tmp_path / "ws" / "fbb93f26" / "obs", base_sha="abc",
    )
    for path in (env.root, env.toolkit_root, env.runtime_dir, env.papers_dir, env.obs_root,
                 env.run_dir):
        assert arms.forbidden_in_path(path) == [], path


# ── The leak scanner ────────────────────────────────────────────────────────


def _dump(tmp_path, records):
    path = tmp_path / "slot-01" / "prompts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_the_scanner_finds_the_leak_that_killed_the_first_launch(tmp_path):
    dump = _dump(tmp_path, [{
        "role": "librarian_io", "model": "claude-sonnet-5", "provider": "anthropic",
        "messages": [{"content": "Downloaded: /data/x/science_bench/abc/papers/arxiv_1.pdf"}],
    }])
    findings, coverage = cli.scan_prompts(dump)
    assert any("science_bench" in f for f in findings)
    assert coverage == {"librarian_io/claude-sonnet-5": 1}


def test_the_scanner_does_not_cry_wolf_over_scientific_prose(tmp_path):
    """Its first version reported forty-nine leaks over five records, of which one was real: the
    rest were an abstract saying *probe*, a fit called an *anchor*, and the actor's own model
    name. A guard that cries wolf gets switched off over the run that mattered."""
    dump = _dump(tmp_path, [{
        "role": "librarian_io", "model": "claude-sonnet-5", "provider": "anthropic",
        "messages": [{"content":
            "X-ray polarimetry can probe the corona; the 2 keV point anchors the fit, and the "
            "control sample receives no treatment. Benchmarking against Krawczynski+2022. "
            "Stored at /data/fzhou/ar_runtime/ws/deadbeef/papers/arxiv_1.pdf"}],
    }])
    findings, _ = cli.scan_prompts(dump)
    assert findings == []


def test_a_seat_naming_its_own_model_is_not_a_leak(tmp_path):
    """It knows what it is. The arm is the combination, not any one seat."""
    dump = _dump(tmp_path, [{
        "role": "analyst_lead", "model": "claude-opus-5", "provider": "anthropic",
        "messages": [{"content": "/tmp/claude-opus-5/notes"}],
    }])
    assert cli.scan_prompts(dump)[0] == []


def test_another_seat_s_model_in_a_path_is_a_leak(tmp_path):
    dump = _dump(tmp_path, [{
        "role": "analyst_lead", "model": "claude-opus-5", "provider": "anthropic",
        "messages": [{"content": "/data/runs/reviewer_claude-fable-5/report.json"}],
    }])
    assert any("fable" in f for f in cli.scan_prompts(dump)[0])


def test_coverage_is_reported_so_silence_is_not_read_as_safety(tmp_path):
    """A clean scan over a dump holding only the librarian says nothing about the lead."""
    dump = _dump(tmp_path, [
        {"role": "librarian_io", "model": "m1", "provider": "p", "messages": []},
        {"role": "analyst_lead", "model": "m2", "provider": "p", "messages": []},
        {"role": "analyst_lead", "model": "m2", "provider": "p", "messages": []},
    ])
    _, coverage = cli.scan_prompts(dump)
    assert coverage == {"librarian_io/m1": 1, "analyst_lead/m2": 2}


def test_an_absent_dump_is_reported_rather_than_passing_silently(tmp_path):
    findings, coverage = cli.scan_prompts(tmp_path / "nothing.jsonl")
    assert findings and coverage == {}


def test_the_author_s_paths_are_read_from_the_ledger_it_does_reach(tmp_path):
    """The prompt chokepoint cannot see the author: it is a subprocess speaking its harness's own
    protocol, so its exposure is the paths in its commands."""
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text(
        json.dumps({"event": "coder.tool_use",
                    "data": {"command": "ls /data/x/science_bench/abc/toolkit"}}) + "\n"
        + json.dumps({"event": "analyst.analysis.report", "data": {"text": "we probe the corona"}})
        + "\n",
        encoding="utf-8",
    )
    findings = cli.scan_ledger_paths(ledger)
    assert any("science_bench" in f for f in findings)
    assert not any("probe" in f for f in findings)


def test_building_and_sweeping_a_world_stay_off_the_event_loop(monkeypatch, tmp_path):
    """A world is ~4 GB of copying and a repository clone. Inline, it would serialise concurrent
    materialisation and stall the pollers of runs already in flight, since every trial's coroutine
    shares one loop."""
    threads: list[str] = []
    import threading

    def _slow_materialize(*args, **kwargs):
        threads.append(threading.current_thread().name)
        raise RuntimeError("far enough")

    monkeypatch.setattr(trial.env_mod, "materialize", _slow_materialize)

    async def _drive():
        main_thread = threading.current_thread().name
        cfg = trial.TrialConfig(workspace_root=tmp_path / "ws", out_dir=tmp_path / "out")
        await trial.run_trial(BLOCK1[1], cfg)
        return main_thread

    main_thread = asyncio.run(_drive())
    assert threads and threads[0] != main_thread


def test_an_actor_reading_a_seat_naming_artifact_is_reported(tmp_path):
    """The run directory is a read root for the lead and the reviewer -- production makes it one
    on purpose -- and it holds a console log prefixed `role(model)` for every role and a ledger
    with a `role/member` on every cost line. Either one, read whole, hands an actor the arm.

    Measured rather than closed: closing it would change the pipeline being measured, on the
    strength of a behaviour no run has yet been seen to exhibit.
    """
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text("\n".join([
        json.dumps({"event": "analyst_lead.read_file.tool_call", "actor": "analyst_lead",
                    "data": {"args": {"path": "/data/x/ws/ab/runtime/runs/ab/backend.log"}}}),
        json.dumps({"event": "reviewer.read_file.tool_call", "actor": "reviewer",
                    "data": {"args": {"path": "/data/x/ws/ab/runtime/runs/ab/reviewer/report/report.jsonl"}}}),
    ]) + "\n", encoding="utf-8")
    findings = cli.scan_actor_reads(ledger)
    assert len(findings) == 1
    assert "analyst_lead read backend.log" in findings[0]


def test_the_ledger_naming_itself_is_not_reported_as_a_read(tmp_path):
    """Every line of a ledger contains its own run's paths; a scan that cannot tell a record from
    a read would report all of them."""
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text(
        json.dumps({"event": "run.start", "actor": "run",
                    "data": {"jsonl": "/data/x/runs/ab/monitor.jsonl"}}) + "\n",
        encoding="utf-8",
    )
    assert cli.scan_actor_reads(ledger) == []


def test_guard_rejections_and_schema_retries_are_counted_as_responses(tmp_path):
    """Both are taxes on the loop budget rather than facts about the science, and both are
    invisible in every other number: a model that exhausts `react_loop_cap` fighting a path checker
    looks exactly like a model that could not do the analysis. Observed live on three of four
    actors, one worker losing three consecutive iterations to it."""
    path = _ledger(tmp_path / "monitor.jsonl", [
        {"event": "analyst_worker.analyze.tool_result",
         "data": {"is_error": True, "text": "path not permitted: /event_l2/ixpe — outside the "
                                            "agent filesystem allow-list"}},
        {"event": "analyst_lead.read_file.tool_result",
         "data": {"is_error": True, "text": "path not permitted: /x/AGENTS.md"}},
        {"event": "analyst_worker.analyze.tool_result",
         "data": {"is_error": True, "text": "TimeoutError: the read operation timed out"}},
        {"event": "analyst_lead.decide_authoring.retry", "data": {}},
    ])
    summary = trial.summarise_ledger(path)
    assert summary["guard_rejections"] == 2   # the timeout is a different kind of failure
    assert summary["schema_retries"] == 1


# ── Reader defects block 0 caught, pinned against the real event shapes ─────
#
# Both were found by cross-checking a live run rather than by review, and both had the same
# signature: the reader returned a plausible number that was wrong. A reader pointed at the wrong
# field does not look like a defect, it looks like a boring result.


def test_a_committed_tool_is_read_from_the_event_that_carries_its_name(tmp_path):
    """Block 0 committed `load_data` and the record said zero tools committed.

    Both event shapes are verbatim from that run's ledger: `commit` carries the name and SHA at the
    top level, while `analyst.tool.authored` nests everything under `result`, so the obvious read
    of the latter returns None.
    """
    path = _ledger(tmp_path / "monitor.jsonl", [
        {"event": "commit", "data": {"branch": "ai", "tool": "load_data",
                                     "commit_sha": "2cc5dcfe9ddc4ca990241cf03b83b1db71799821",
                                     "message": "Fix IXPE Level-2 discovery"}},
        {"event": "analyst.tool.authored",
         "data": {"result": {"branch": "ai", "commit_sha": "2cc5dcfe9d", "gate_report": {}}}},
    ])
    summary = trial.summarise_ledger(path)
    assert summary["tools_committed"] == ["load_data"]
    assert summary["tools_committed_n"] == 1
    assert summary["commit_shas"] == ["2cc5dcfe9d"]


def test_a_role_label_on_a_cost_line_is_not_a_path(tmp_path):
    """186 findings in one run, every one of them `coder/deepseek_v4_flash` on a cost event.

    Two things were wrong: a single `/word` was being treated as a path, and cost accounting -- the
    harness's own bookkeeping, which reaches no actor -- was being scanned at all.
    """
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text(
        json.dumps({"event": "cost.call", "actor": "coder",
                    "data": {"role": "coder/deepseek_v4_flash", "total_cost_usd": 0.02}}) + "\n",
        encoding="utf-8",
    )
    assert cli.scan_ledger_paths(ledger) == []


def test_a_third_party_data_directory_is_not_a_campaign_word(tmp_path):
    """`ixpeobssim` creates `ixpeobssimdata/benchmarks` in the coder's own home. The science
    question is itself a reproduction benchmarked against a published paper, so those words are
    unusable as leak indicators here -- the distinctive string is matched exactly instead."""
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text(
        json.dumps({"event": "coder.tool_use", "actor": "coder",
                    "data": {"text": "About to create folder "
                                     "/tmp/auto_research-coder-home/codex-fb40/ixpeobssimdata/benchmarks"}}) + "\n",
        encoding="utf-8",
    )
    assert cli.scan_ledger_paths(ledger) == []
    assert "science_bench" in arms.CAMPAIGN_WORDS
    assert "benchmark" in arms.RETIRED_WORDS


def test_the_distinctive_string_still_catches_the_original_leak(tmp_path):
    """Retiring the ambiguous words must not retire the one that mattered."""
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text(
        json.dumps({"event": "coder.tool_use", "actor": "coder",
                    "data": {"command": "ls /data/x/science_bench/ab/toolkit"}}) + "\n",
        encoding="utf-8",
    )
    assert any("science_bench" in f for f in cli.scan_ledger_paths(ledger))
