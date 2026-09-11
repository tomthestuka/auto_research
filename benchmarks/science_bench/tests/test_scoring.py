"""The scoring pass gets its own tests, because a reader pointed at the wrong field looks fine.

Three paid runs were lost on 2026-09-01 to bugs in code that *read* results rather than code under
test, and building this pass repeated the lesson twice: the extractor's window spilled into the
next quantity (so eight of fourteen inherited a neighbour's error and every ``same artifact``
entry inherited the wrong file), and the [A20] rule was first keyed on a field that is ``None`` for
the very run it was written for. Both are pinned below.

Two oracles make most of this free:

* **The z arithmetic** is checked against the toolkit's own ``compare_to_benchmark`` output, which
  published fourteen ``|z|`` values for this exact question. Our signed z must agree in magnitude
  on every row.
* **The extractor** is checked against attempt 6's real report, frozen as a fixture, where all
  fourteen values are known by hand.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from benchmarks.science_bench import arms, extract, report as report_mod, score as score_mod

FIXTURES = Path(__file__).parent / "fixtures"
ATTEMPT6 = FIXTURES / "attempt6_named_quantities.tex"
CSV = Path(__file__).resolve().parents[3] / "papers/FANGZ/data_benchmark_compare_krawczynski2022.csv"


@pytest.fixture(scope="module")
def key():
    return extract.load_answer_key()


# ── the frozen key ──────────────────────────────────────────────────────────────────────


def test_the_key_holds_exactly_the_fourteen_prescribed_labels(key):
    assert len(key["quantities"]) == 14
    assert key["useful_threshold_k"] == 10
    assert key["sigma_flag"] == 3.0
    # Corrected 2026-09-10: every value is a verbatim Table S2 read, so nothing is marked
    # approximate. The four band PDs had carried a figure read (2.8/3.3/4.4/7.1 +/- 0.4) matching
    # no row of that table; `PD - sum` is the comparator the key's own broadband entries already
    # come from (its 2.0-8.0 cell is 4.0 +/- 0.2, which is `May_PD_2-8keV_PCUBE`).
    assert [n for n, q in key["quantities"].items() if q["approximate"]] == []
    for label, expected in (
        ("May_PD_2-3keV", (3.5, 0.3)),
        ("May_PD_3-4keV", (3.5, 0.3)),
        ("May_PD_4-6keV", (4.7, 0.3)),
        ("May_PD_6-8keV", (5.8, 0.8)),
    ):
        quantity = key["quantities"][label]
        assert (quantity["paper_value"], quantity["paper_error"]) == expected, label
        assert "PD - sum" in quantity["paper_source"], label


def test_the_key_matches_the_request_the_batch_dispatches(key):
    """The scored labels and the labels the trees are asked for are the same set, or the sweep is
    scoring something it never requested."""
    from benchmarks.science_bench import request as request_mod

    text = request_mod.load_request().model_dump_json()
    for label in key["quantities"]:
        assert label in text, f"{label} is scored but never dispatched"


def test_the_key_carries_its_own_hash_and_it_is_stable(key):
    again = extract.load_answer_key()
    assert key["answer_key_sha256"] == again["answer_key_sha256"]
    assert len(key["answer_key_sha256"]) == 64


# ── the z arithmetic, against the toolkit's own published output ─────────────────────────


def test_signed_z_matches_the_pipelines_own_comparison_tool(key):
    """Our z must agree in magnitude with ``compare_to_benchmark`` on all fourteen rows."""
    rows = list(csv.DictReader(CSV.read_text(encoding="utf-8").splitlines()[1:]))
    assert len(rows) == 14
    for row in rows:
        expected = key["quantities"][row["label"]]
        value, error = float(row["value_this"]), float(row["err_this"])
        period = expected.get("periodic_deg")
        delta = (
            score_mod.angle_delta(value, expected["paper_value"], float(period))
            if period
            else value - expected["paper_value"]
        )
        z = delta / math.hypot(error, expected["paper_error"])
        assert abs(z) == pytest.approx(float(row["z"]), rel=1e-9), row["label"]


def test_a_polarization_angle_is_periodic_modulo_180():
    """PA is an orientation, so the same angle written the other way round is not a discrepancy."""
    assert score_mod.angle_delta(159.3, -20.7) == pytest.approx(0.0, abs=1e-9)
    assert score_mod.angle_delta(-20.7, 159.3) == pytest.approx(0.0, abs=1e-9)
    assert score_mod.angle_delta(-15.0, -20.7) == pytest.approx(5.7, abs=1e-9)
    # Wrapped into the half-open half-period, never outside it.
    for this in (-400.0, -91.0, 0.0, 89.0, 271.0):
        assert -90.0 <= score_mod.angle_delta(this, 0.0) < 90.0


def test_the_periodic_wrap_is_what_saves_a_correctly_reported_angle(key):
    """Without the wrap, a PA reported in the other convention scores as a huge discrepancy."""
    text = "May_PA_2-8keV_PCUBE = 159.330 +/- 1.605 deg"
    extraction = extract.Extraction(
        report_path="x", report_sha256="0", labels_found=1, labels_measured=1,
        quantities=extract.extract_text(text, list(key["quantities"])),
    )
    scored = score_mod.score_quantities(extraction, key)["May_PA_2-8keV_PCUBE"]
    assert scored.reported
    assert abs(scored.z) < 0.1, "the same angle in the other convention must not be a discrepancy"
    assert scored.within


# ── the extractor, against attempt 6's real report ──────────────────────────────────────


@pytest.fixture(scope="module")
def attempt6(key):
    return extract.extract_text(ATTEMPT6.read_text(encoding="utf-8"), list(key["quantities"]))


def test_all_fourteen_are_found_and_measured(attempt6):
    assert len(attempt6) == 14
    assert all(q.measured for q in attempt6.values()), [
        n for n, q in attempt6.items() if not q.measured
    ]


@pytest.mark.parametrize(
    "label,value,error,unit",
    [
        ("May_PD_2-8keV_PCUBE", 3.4534, 0.1935, "percent"),
        ("May_PA_2-8keV_PCUBE", -20.670, 1.605, "deg"),
        ("Combined_PD_2-8keV_PCUBE", 3.5240, 0.1637, "percent"),
        ("May_XSPEC_PD_polconst", 3.26, 0.14, "percent"),
        ("May_LinearFit_PD_B_slope", 0.5342, 0.1125, "percent/kev"),
        ("May_PD_6-8keV", 5.0537, 0.6314, "percent"),
    ],
)
def test_hand_checked_values_come_back_exactly(attempt6, label, value, error, unit):
    found = attempt6[label]
    assert found.value == pytest.approx(value)
    assert found.error == pytest.approx(error)
    assert found.unit_text == unit


def test_the_window_stops_at_the_next_quantity(attempt6):
    """The defect this pins: an unbounded window read the *next* item's error as a dual error.

    Measured on this very report before the fix — eight of fourteen quantities carried a
    neighbour's number, and the two genuine dual errors were missed.
    """
    assert attempt6["May_PA_2-8keV_PCUBE"].dual_error is None
    assert attempt6["June_PD_2-8keV_PCUBE"].dual_error is None
    assert attempt6["May_PD_2-3keV"].dual_error is None
    # The two real ones: the XSPEC fit's raw and chi^2-inflated uncertainties.
    assert attempt6["May_XSPEC_PD_polconst"].dual_error == pytest.approx(0.56)
    assert attempt6["May_XSPEC_PA_polconst"].dual_error == pytest.approx(4.96)


def test_a_back_reference_inherits_the_artifact_it_refers_to(attempt6):
    """``(same artifact)`` resolves, instead of reading as a tree that cited nothing."""
    assert attempt6["May_PD_2-8keV_PCUBE"].source_artifact == "pcube_spectrum_01002901.csv"
    assert attempt6["May_PA_2-8keV_PCUBE"].source_artifact == "pcube_spectrum_01002901.csv"
    assert attempt6["May_XSPEC_PA_polconst"].source_artifact == "xspec_fit_polconst_may.json"


def test_the_unit_alternation_is_longest_first(attempt6):
    """A bare ``percent`` matched ahead of ``percent/keV`` truncates the slope's unit silently."""
    assert attempt6["May_LinearFit_PD_B_slope"].unit_text == "percent/kev"


# ── absences, unit errors, non-compliance ───────────────────────────────────────────────


def test_a_stated_absence_is_recorded_as_one_not_as_a_parse_failure(key):
    text = (
        "\\item May_PD_2-8keV_PCUBE = NOT MEASURED (loader limit).\n"
        "\\item May_PA_2-8keV_PCUBE $= -20.670 \\pm 1.605$ deg.\n"
    )
    found = extract.extract_text(text, list(key["quantities"]))
    assert found["May_PD_2-8keV_PCUBE"].not_reported
    assert found["May_PD_2-8keV_PCUBE"].value is None
    assert found["May_PA_2-8keV_PCUBE"].measured


def test_a_label_that_never_appears_is_not_reported(key):
    found = extract.extract_text("nothing relevant here", list(key["quantities"]))
    assert all(q.not_reported for q in found.values())
    assert all(q.occurrences == 0 for q in found.values())


def test_a_unit_mismatch_is_scored_apart_from_a_wrong_value(key):
    """A degree reported in radians is a unit error, and must not read as a wrong measurement."""
    text = "May_PA_2-8keV_PCUBE $= -0.361 \\pm 0.028$ radians"
    extraction = extract.Extraction(
        report_path="x", report_sha256="0", labels_found=1, labels_measured=1,
        quantities=extract.extract_text(text, list(key["quantities"])),
    )
    scored = score_mod.score_quantities(extraction, key)["May_PA_2-8keV_PCUBE"]
    assert scored.unit_mismatch
    assert not scored.within, "a unit error never counts as recovered"


def test_non_compliance_with_the_verbatim_label_instruction_is_visible(tmp_path, key):
    partial = tmp_path / "report.tex"
    partial.write_text("May_PD_2-8keV_PCUBE $= 3.4534 \\pm 0.1935$ percent", encoding="utf-8")
    extraction = extract.extract_report(partial, key)
    assert not extraction.compliant
    assert extraction.labels_found == 1
    assert extraction.labels_measured == 1


def test_latex_escapes_in_a_label_do_not_hide_it(key):
    text = r"\item \textbf{May\_PD\_2-8keV\_PCUBE} $= 3.4534 \pm 0.1935$ percent"
    found = extract.extract_text(text, list(key["quantities"]))
    assert found["May_PD_2-8keV_PCUBE"].value == pytest.approx(3.4534)


# ── the whole trial score ───────────────────────────────────────────────────────────────


def _record(**overrides):
    record = {
        "run_id": "r1",
        "tree": "opus/fable/w1",
        "infrastructure": "done",
        "tools_committed_n": 3,
        "wanted_a_human": 0,
        "worker_statuses": ["ok", "failed", "needs_human"],
        "lead_cycles": [],
        "measured": {"accepted": True},
    }
    record.update(overrides)
    return record


def test_attempt_sixes_report_scores_as_a_full_recovery_with_the_trap(tmp_path, key):
    report = tmp_path / "report.tex"
    report.write_text(ATTEMPT6.read_text(encoding="utf-8"), encoding="utf-8")
    scored = score_mod.score_trial(_record(), report=report, key=key)

    assert scored.terminal == "accepted"
    assert scored.quantities_reported == 14
    assert scored.within_tolerance == 14
    # All fourteen key values are verbatim table reads since 2026-09-10, so the exact subset is
    # all of them. It was 10 while the four band PDs carried a figure read.
    assert scored.within_tolerance_exact == 14
    assert scored.useful
    # The trap: every PD low, every PA at zero. This is the pattern, not a scalar.
    assert scored.pd_mean_signed_z is not None and scored.pd_mean_signed_z < -1.0
    assert scored.pa_max_abs_z is not None and scored.pa_max_abs_z < 1.0
    assert scored.trap_reproduced is True
    assert scored.unit_mismatches == 0
    assert scored.answer_key_sha256 and scored.scorer_sha256


def test_a_tree_reporting_agreement_everywhere_fails_the_trap(tmp_path, key):
    """Reporting the published values back is the failure mode the question was chosen for."""
    lines = [
        f"\\item {label} $= {q['paper_value']} \\pm {q['paper_error']}$ {q['unit']}"
        for label, q in key["quantities"].items()
    ]
    report = tmp_path / "report.tex"
    report.write_text("\n".join(lines), encoding="utf-8")
    scored = score_mod.score_trial(_record(), report=report, key=key)

    assert scored.within_tolerance == 14, "the values agree by construction"
    assert scored.pd_mean_signed_z == pytest.approx(0.0, abs=1e-6)
    assert scored.trap_reproduced is False, "perfect agreement is the wrong answer here"


# ── [A20], the frozen terminal rule ─────────────────────────────────────────────────────

_A20_CYCLES = [
    {"filed": "submit_terminal_report", "committed": 1, "status": "needs_human"},
    {"filed": "submit_terminal_report", "committed": 1, "status": "needs_human"},
    {"filed": "submit_terminal_report", "committed": 1, "status": "needs_human"},
    {"filed": "submit_terminal_report", "committed": 0, "status": "needs_human"},
]


def test_a_terminal_filed_after_a_commit_is_not_a_failure_row():
    record = _record(measured={"accepted": False}, lead_cycles=_A20_CYCLES)
    terminal, correct = score_mod.classify_terminal(record)
    assert terminal == "accepted_with_limits"
    assert correct
    assert score_mod.a20_relabelled_cycles(record) == 3


def test_a_terminal_filed_with_nothing_committed_is_a_genuine_decline():
    record = _record(
        measured={"accepted": False},
        tools_committed_n=0,
        lead_cycles=[{"filed": "submit_terminal_report", "committed": 0, "status": "needs_human"}],
    )
    assert score_mod.classify_terminal(record) == ("declined", True)
    assert score_mod.a20_relabelled_cycles(record) == 0


def test_the_a20_rule_reads_the_cycle_ledger_not_the_worker_statuses():
    """The defect this pins: keyed on ``measured.needs_human``, the rule was inert on attempt 6.

    That field is derived from *worker* statuses. Attempt 6's workers did report ``needs_human``,
    but a trial whose workers did not, and whose lead filed the mislabelled terminal anyway, is
    exactly the case the rule exists for — and the old key would have missed it.
    """
    record = _record(
        measured={"accepted": False, "needs_human": False},
        worker_statuses=["ok", "ok"],
        lead_cycles=_A20_CYCLES,
    )
    assert score_mod.classify_terminal(record)[0] == "accepted_with_limits"


def test_a20_never_erases_a_genuine_question_to_a_human(tmp_path, key):
    """The correction is recorded, not subtracted: a real ``ask_human`` elsewhere must survive."""
    report = tmp_path / "report.tex"
    report.write_text(ATTEMPT6.read_text(encoding="utf-8"), encoding="utf-8")
    scored = score_mod.score_trial(
        _record(measured={"accepted": False}, lead_cycles=_A20_CYCLES, wanted_a_human=4),
        report=report,
        key=key,
    )
    assert scored.terminal == "accepted_with_limits"
    assert scored.wanted_a_human == 4
    assert scored.terminals_relabelled == 3


def test_a_harness_fault_is_not_the_trees_outcome():
    for infrastructure, expected in (
        ("error", "harness_error"),
        ("died", "harness_error"),
        ("failed", "infrastructure_failure"),
    ):
        terminal, correct = score_mod.classify_terminal(_record(infrastructure=infrastructure))
        assert terminal == expected
        assert not correct, "a batch fault must never count as a declined result"


# ── the report generator ────────────────────────────────────────────────────────────────


def _scored(tree: str, **overrides) -> score_mod.TrialScore:
    base = dict(
        run_id="r", tree=tree, terminal="accepted", committed=3, quantities_reported=14,
        within_tolerance=13, within_tolerance_exact=10, mean_abs_z=1.0,
        pd_mean_signed_z=-1.4, pa_max_abs_z=0.6, trap_reproduced=True, useful=True, compliant=True,
    )
    base.update(overrides)
    return score_mod.TrialScore(**base)


def test_the_tree_table_has_one_row_per_tree_and_states_n():
    scores = [_scored("a"), _scored("a"), _scored("b")]
    table = report_mod.tree_table_markdown(scores)
    assert table.count("\n") == 3, table  # header, rule, then one row per tree
    assert "| a | 2 |" in table
    assert "| b | 1 |" in table


def test_the_appendix_table_marks_only_what_the_key_marks(tmp_path, key):
    """The mark and its note appear together, or neither appears.

    A note describing a mark the table does not carry is worse than no note: it tells a reader
    four values are approximate when the key says none are.
    """
    import json as _json

    report = tmp_path / "report.tex"
    report.write_text(ATTEMPT6.read_text(encoding="utf-8"), encoding="utf-8")
    scored = score_mod.score_trial(_record(), report=report, key=key)

    table = report_mod.appendix_table_markdown([scored], key)
    for label in key["quantities"]:
        assert label in table
    # The shipped key marks nothing (2026-09-10 correction).
    assert "$^{a}$" not in table

    marked = _json.loads(_json.dumps(key))
    marked["quantities"]["May_PD_6-8keV"]["approximate"] = True
    table = report_mod.appendix_table_markdown([scored], marked)
    assert "May_PD_6-8keV$^{a}$" in table
    assert "$^{a}$ Key value is approximate" in table


def test_the_report_refuses_to_imply_an_estimate():
    text = report_mod.build_report([_scored("a"), _scored("b")], {})
    assert "demonstration, not an estimation" in text
    assert "2512.06710" in text, "the variance floor it does not reach must be cited"


def test_the_figures_return_none_rather_than_writing_an_empty_one(tmp_path):
    unmeasured = [_scored("a", quantities_reported=0, pd_mean_signed_z=None)]
    assert report_mod.forest_figure(unmeasured, tmp_path / "f.png") is None
    assert report_mod.summary_scatter(unmeasured, tmp_path / "s.png") is None
    assert not (tmp_path / "f.png").exists()


def test_the_figures_are_written_for_a_real_score(tmp_path, key):
    report = tmp_path / "report.tex"
    report.write_text(ATTEMPT6.read_text(encoding="utf-8"), encoding="utf-8")
    scored = score_mod.score_trial(_record(), report=report, key=key)
    out = tmp_path / "out"
    written = report_mod.write_report(out, [scored], {"toolkit": "5accf33"})
    assert written.is_file()
    assert (out / "fig_seat_forest.png").is_file()
    assert (out / "fig_seat_recovery.png").is_file()
    assert (out / "scores.json").is_file()


# ── the demonstration slots ─────────────────────────────────────────────────────────────


def test_the_demonstration_is_two_trees_at_three_trials():
    assert len(arms.DEMO) == 6
    trees = {arms.demo_tree_slug(slot) for slot in arms.DEMO}
    assert trees == {"opus/fable/w1", "terra/qwen/w4"}
    for tree in trees:
        assert sum(1 for s in arms.DEMO if arms.demo_tree_slug(s) == tree) == 3


def test_the_anchors_first_replicate_is_the_run_that_already_happened():
    """Attempt 6 is slot 1 by construction: the id is a hash of the seats and the replicate."""
    assert arms.DEMO[0].run_id == "fbb93f2600d620b26baa9c71dc5e53d7"
    assert arms.DEMO[0].run_id == arms.BLOCK0.run_id


def test_every_demonstration_slot_has_a_distinct_run_id():
    ids = [slot.run_id for slot in arms.DEMO]
    assert len(set(ids)) == len(ids)


def test_the_contrast_tree_is_a_tier_down_in_all_three_varied_seats():
    anchor = arms.arm("opus", "fable", "w1")
    contrast = arms.arm("terra", "qwen", "w4")
    assert anchor.lead.model != contrast.lead.model
    assert anchor.reviewer.model != contrast.reviewer.model
    assert anchor.workers.slug != contrast.workers.slug
    # The pinned seats do not move, which is what isolates the science half.
    assert anchor.coder == contrast.coder
    assert anchor.colead == contrast.colead


def test_the_contrast_trees_workers_stay_provider_decorrelated():
    pair = arms.BY_WORKER_PAIR["w4"]
    assert len(set(pair.providers())) == 2


def test_every_seat_on_a_figure_bearing_axis_is_vision_capable():
    for seat in (*arms.LEADS, *arms.REVIEWERS):
        assert seat.vision, seat.model
    for pair in arms.WORKER_PAIRS:
        for member in pair.members:
            assert member.vision, member.model


def test_every_demonstration_member_has_a_price_or_is_declared_unread():
    """An unpriced member bills nothing and quietly understates its arm's cost."""
    import litellm

    for slot in arms.DEMO:
        for seat in (*slot.arm.seats(), *slot.arm.workers.members):
            if seat.price():
                continue
            known = seat.model in litellm.model_cost or any(
                seat.model in candidate for candidate in litellm.model_cost
            )
            assert known or seat.model in arms.PRICES_UNREAD, (
                f"{seat.model} has no rate here, none in litellm, and is not declared unread"
            )


def test_the_superseded_screening_array_is_kept_intact():
    """It is the record of what was designed; the test that it is balanced still guards it."""
    assert len(arms.BLOCK1) == 11
    assert len({arms.demo_tree_slug(s) for s in arms.BLOCK1}) == 9


# ── the answer key must stay out of every actor-readable root ───────────────────────────


def test_the_frozen_key_is_outside_every_root_an_actor_can_read():
    """Tamper hygiene, not secrecy: this question's answer is published and the tree is *supposed*
    to read the paper. What must not happen is a scored artifact sitting where an actor could
    reach it, because a measurement over an artifact the subject could touch is not evidence about
    the subject. The orchestration repository is on no allow-list, which is what makes the key's
    home beside this module safe."""
    import yaml

    from auto_research.config_paths import orchestration_yaml

    source = yaml.safe_load(Path(orchestration_yaml()).read_text(encoding="utf-8")) or {}
    key_path = Path(extract.ANSWER_KEY).resolve()
    roots = [source.get(name) for name in ("runtime_dir", "papers_dir", "toolkit_root")]
    roots += list(source.get("agent_fs_allow") or [])
    for root in [r for r in roots if r]:
        resolved = Path(str(root)).expanduser().resolve()
        assert not key_path.is_relative_to(resolved), f"the key is readable from {resolved}"


def test_the_preflight_sees_a_key_that_is_readable():
    """The other half: a check that cannot fail is not a check.

    Driven against a real allow-listed root on this host rather than a fabricated one, so it
    exercises the same comparison the launch does.
    """
    import json as _json
    from unittest.mock import patch

    import yaml

    from auto_research.config_paths import orchestration_yaml
    from benchmarks.science_bench import __main__ as cli

    source = yaml.safe_load(Path(orchestration_yaml()).read_text(encoding="utf-8")) or {}
    allowed = [r for r in (source.get("agent_fs_allow") or []) if Path(str(r)).expanduser().is_dir()]
    if not allowed:
        pytest.skip("no allow-listed root exists on this host")

    planted = Path(str(allowed[0])).expanduser() / "probe_answer_key.json"
    try:
        planted.write_text(_json.dumps({"quantities": {}}), encoding="utf-8")
    except OSError:
        pytest.skip(f"cannot write into {planted.parent}")
    try:
        with patch.object(extract, "ANSWER_KEY", planted):
            problems = cli.preflight(
                list(arms.DEMO), workspace=Path(cli.DEFAULT_WORKSPACE)
            )
        assert any("answer key" in problem for problem in problems), (
            "the preflight did not notice a key sitting inside an allow-listed root"
        )
    finally:
        planted.unlink(missing_ok=True)
