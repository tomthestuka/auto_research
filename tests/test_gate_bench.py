"""Deterministic tests for the seeded-defect gate benchmark (``benchmarks/gate_bench``).

No science stack, no LLM, no toolkit checkout: the builders are exercised against throwaway
git repos, the gate integration injects a fake Layer-B runner, and the scorer runs over
hand-built records. The real-base builders need the configured toolkit, so their test skips
when it is absent (this suite must pass on a fresh clone).

Run from the repository root, so that ``benchmarks`` is importable:
``conda run -n auto_research python -m pytest tests/test_gate_bench.py``.
"""

import asyncio
import subprocess

import pytest
import yaml

from auto_research.sandbox.gate import collect_targets, run_gate
from auto_research.schemas import CheckResult

from benchmarks.gate_bench import basepkg, harness, score
from benchmarks.gate_bench.mutants import (
    DETERMINISTIC_LAYERS,
    FAM_CONTROL,
    FAMILY_ORDER,
    LAYERS,
    by_id,
    corpus,
)

VALID_EXPECTATIONS = {*LAYERS, "miss", "pass"}


# ── corpus shape ────────────────────────────────────────────────────────────


def test_corpus_ids_are_unique():
    ids = [m.id for m in corpus()]
    assert len(ids) == len(set(ids))


def test_every_mutant_declares_a_known_family_and_expectation():
    for mutant in corpus():
        assert mutant.family in FAMILY_ORDER, mutant.id
        assert mutant.expected in VALID_EXPECTATIONS, mutant.id
        assert mutant.base in ("synthetic", "real"), mutant.id
        assert (mutant.expected == "pass") == (mutant.family == FAM_CONTROL), mutant.id


def test_every_paper_family_is_populated():
    families = {m.family for m in corpus()}
    assert set(FAMILY_ORDER) <= families


def test_both_bases_are_exercised():
    bases = {m.base for m in corpus()}
    assert bases == {"synthetic", "real"}


# ── builders produce complete changesets ────────────────────────────────────


@pytest.fixture
def repo(tmp_path):
    return basepkg.make_synthetic_repo(tmp_path / "repo")


def test_base_package_is_a_changeset_the_gate_can_see(repo):
    basepkg.write_base_package(repo)
    rels = {str(p.relative_to(repo)) for p in collect_targets(repo)}
    assert "demo_utils/lib/demo_help.py" in rels
    assert "demo_utils/skills/__init__.py" in rels
    assert "demo_utils/tests/test_demo_summary.py" in rels


def test_byte_compiled_output_is_not_part_of_a_changeset(repo):
    """The synthetic base mirrors the toolkit's ignore rules, so a test run's ``.pyc`` output
    cannot masquerade as authored content (it would otherwise inflate the [G3] sweep)."""
    pkg = basepkg.write_base_package(repo)
    cache = pkg / "lib" / "__pycache__"
    cache.mkdir()
    (cache / "demo_help.cpython-313.pyc").write_bytes(b"\x00compiled")
    rels = {str(p.relative_to(repo)) for p in collect_targets(repo)}
    assert not [r for r in rels if r.endswith(".pyc")]


@pytest.mark.parametrize("mutant", [m for m in corpus() if m.base == "synthetic"], ids=lambda m: m.id)
def test_synthetic_builders_write_a_changeset(mutant, tmp_path):
    repo = basepkg.make_synthetic_repo(tmp_path / mutant.id)
    mutant.build(repo)
    assert collect_targets(repo), f"{mutant.id} planted no changeset"


def test_patch_refuses_a_missed_anchor(tmp_path):
    """A real-base anchor that no longer matches must fail loudly, never plant nothing."""
    path = tmp_path / "x.py"
    path.write_text("a = 1\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        basepkg.patch(path, "b = 2\n", "b = 3\n")


def test_real_builders_apply_against_the_pinned_toolkit(tmp_path):
    """Every real-base builder's anchors still match the pinned ref (skipped off-host)."""
    config = harness.REPO_ROOT / "config" / "orchestration.yaml"
    if not config.is_file():
        pytest.skip("no machine-local orchestration config")
    toolkit_root = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("toolkit_root")
    if not toolkit_root or not (harness.Path(toolkit_root) / ".git").exists():
        pytest.skip("toolkit checkout unavailable")
    ref = harness.DEFAULT_TOOLKIT_REF
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=str(toolkit_root), capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip(f"toolkit ref {ref!r} is not present in this checkout")
    cfg = harness.RunConfig(
        toolkit_root=harness.Path(toolkit_root), toolkit_ref=ref,
        stamps={"toolkit_sha": probe.stdout.strip()},
    )
    for mutant in [m for m in corpus() if m.base == "real"]:
        worktree = harness.prepare_worktree(mutant, tmp_path / mutant.id, cfg)
        mutant.build(worktree)  # raises if an anchor drifted
        assert collect_targets(worktree), mutant.id


# ── the gate integration (fake Layer-B runner: no science stack) ────────────


async def _pass_runner(worktree, test_paths):
    return {
        "results": [CheckResult(name="layer_b.pytest", layer="B", passed=True)],
        "deterministic": True,
        "determinism_detail": "",
    }


def _gate(mutant_id, repo):
    mutant = by_id()[mutant_id]
    mutant.build(repo)
    spec = harness.tool_spec_for(mutant)
    return asyncio.run(run_gate(repo, spec, test_runner=_pass_runner))


def test_clean_control_passes_the_gate(repo):
    report = _gate("control_synthetic_clean", repo)
    assert report.passed, report.reason


def test_disclosure_control_passes_with_a_recorded_finding(repo):
    """The §9.5 disclosure language is a finding, never a rejection ([C1])."""
    report = _gate("control_synthetic_disclosure", repo)
    assert report.passed, report.reason


def test_fabrication_mutant_is_rejected_by_the_scan(repo):
    report = _gate("fab_vocab_docstring", repo)
    assert not report.passed
    assert any(c.name.startswith("no_fabrication.") and not c.passed for c in report.layer_a)


def test_vocabulary_gap_mutant_reaches_a_commit(repo):
    """The scan is blind to these words today; the benchmark records that rather than fixing it."""
    report = _gate("fabgap_simulated", repo)
    assert report.passed, report.reason


def test_structural_mutant_is_rejected_before_layer_b(repo):
    report = _gate("struct_missing_test", repo)
    assert not report.passed
    assert not report.layer_b


# ── scoring and attribution ─────────────────────────────────────────────────


def _record(**kw):
    base = dict(
        id="m", family="Fabrication, in-vocabulary", base="synthetic", expected="scan",
        description="", known_hole=None, notes="",
        gate={"passed": False, "reason": "", "layer_a": [], "layer_b": [], "findings": []},
        review=None, commit_sweep=None,
    )
    base.update(kw)
    return base


def _check(name, passed=False):
    return {"name": name, "passed": passed, "detail": ""}


def test_scan_failure_attributes_to_the_scan():
    record = score.score_record(_record(gate={
        "passed": False, "layer_a": [_check("no_fabrication.vocabulary")], "layer_b": [],
        "findings": [], "reason": "",
    }))
    assert record["caught_by"] == ["scan"]
    assert record["outcome"] == "caught"
    assert record["expectation"] == "met"


def test_provenance_failure_attributes_to_the_scan():
    record = score.score_record(_record(gate={
        "passed": False, "layer_a": [_check("provenance.figure_ref")], "layer_b": [],
        "findings": [], "reason": "",
    }))
    assert record["caught_by"] == ["scan"]


def test_skipped_determinism_is_not_credited_to_determinism():
    """The gate reports ``determinism`` as failed when structure failed and it never ran;
    crediting that would attribute a catch to a layer that did not execute."""
    record = score.score_record(_record(expected="structural", gate={
        "passed": False, "layer_b": [], "findings": [], "reason": "",
        "layer_a": [_check("structural.test_present"), _check("determinism")],
    }))
    assert record["caught_by"] == ["structural"]


def test_layer_b_failure_attributes_to_the_tool_tests():
    record = score.score_record(_record(expected="layer_b", gate={
        "passed": False, "layer_a": [], "layer_b": [_check("layer_b.pytest")],
        "findings": [], "reason": "",
    }))
    assert record["caught_by"] == ["layer_b"]


def test_findings_are_never_a_catch():
    record = score.score_record(_record(expected="review", gate={
        "passed": True, "layer_a": [], "layer_b": [],
        "findings": [_check("no_fabrication.code_pattern_finding", passed=True)], "reason": "",
    }))
    assert record["caught_by"] == []
    assert record["outcome"] == "reached_commit"


def test_review_rejection_attributes_to_the_review():
    record = score.score_record(_record(
        expected="review",
        gate={"passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""},
        review={"ran": True, "approved": False, "checks_adequate": False, "issues": [], "rationale": "r"},
    ))
    assert record["caught_by"] == ["review"]
    assert record["expectation"] == "met"


def test_review_expectation_is_not_evaluated_when_the_leg_did_not_run():
    record = score.score_record(_record(
        expected="review",
        gate={"passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""},
    ))
    assert record["expectation"] == "not_evaluated"


def test_expected_miss_is_met_only_when_nothing_caught_it():
    passing = score.score_record(_record(expected="miss", gate={
        "passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""}))
    assert passing["expectation"] == "met"
    caught = score.score_record(_record(expected="miss", gate={
        "passed": False, "layer_a": [_check("no_fabrication.vocabulary")], "layer_b": [],
        "findings": [], "reason": ""}))
    assert caught["expectation"] == "unmet"


def test_commit_sweep_catch_is_attributed_and_still_counts_as_reaching_a_commit():
    record = score.score_record(_record(
        expected="commit_sweep",
        gate={"passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""},
        commit_sweep={"ran": True, "caught": True, "commit_sha": "abc", "n_committed": 2,
                      "unscanned_paths": ["demo_utils/lib/x.dat"]},
    ))
    assert record["caught_by"] == ["commit_sweep"]
    assert record["expectation"] == "met"


def test_a_rejected_control_is_a_false_rejection():
    record = score.score_record(_record(family=FAM_CONTROL, expected="pass", gate={
        "passed": False, "layer_a": [_check("no_fabrication.vocabulary")], "layer_b": [],
        "findings": [], "reason": ""}))
    assert record["outcome"] == "false_reject"
    assert record["expectation"] == "unmet"


def test_a_control_the_review_rejected_is_a_false_rejection():
    record = score.score_record(_record(
        family=FAM_CONTROL, expected="pass",
        gate={"passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""},
        review={"ran": True, "approved": False, "checks_adequate": True, "issues": [], "rationale": "r"},
    ))
    assert record["outcome"] == "false_reject"


def test_an_errored_mutant_is_reported_not_scored_as_a_miss():
    record = score.score_record(_record(error="RuntimeError: boom"))
    assert record["outcome"] == "error" and record["expectation"] == "error"


def test_deterministic_layers_exclude_the_review():
    assert "review" not in DETERMINISTIC_LAYERS


# ── attribution roster (a co-lead catch counts only when its reason is the plant) ──


def _review_catch(**kw):
    return _record(
        expected="review",
        gate={"passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""},
        review={"ran": True, "approved": False, "checks_adequate": False,
                "issues": ["the position angle is mirrored"], "rationale": "sign error"},
        **kw,
    )


def test_a_co_lead_catch_is_undecided_until_a_human_reads_it():
    record = score.score_record(_review_catch())
    assert record["caught_by"] == ["review"]
    assert record["review_attribution"] == "undecided"


def test_a_roster_decision_marks_the_catch_on_target():
    record = _review_catch()
    roster = {"m": {"attribution": "on_target",
                    "verdict_fingerprint": score.review_fingerprint(record)}}
    assert score.score_record(record, roster)["review_attribution"] == "on_target"


def test_an_off_target_decision_leaves_the_layer_attribution_untouched():
    """The human judgement sits beside the layer attribution, never inside it."""
    record = _review_catch()
    roster = {"m": {"attribution": "off_target",
                    "verdict_fingerprint": score.review_fingerprint(record)}}
    scored = score.score_record(record, roster)
    assert scored["review_attribution"] == "off_target"
    assert scored["caught_by"] == ["review"] and scored["outcome"] == "caught"


def test_a_changed_verdict_goes_stale_rather_than_inheriting_the_old_read():
    roster = {"m": {"attribution": "on_target", "verdict_fingerprint": "0" * 16}}
    record = score.score_record(_review_catch(), roster)
    assert record["review_attribution"] == "stale"


def test_attribution_is_absent_when_the_co_lead_did_not_reject():
    record = score.score_record(_record(expected="scan", gate={
        "passed": False, "layer_a": [_check("no_fabrication.vocabulary")], "layer_b": [],
        "findings": [], "reason": ""}))
    assert record["review_attribution"] is None


def test_a_missing_roster_file_reads_as_no_decisions(tmp_path):
    assert score.load_roster(tmp_path / score.ROSTER_NAME) == {}


def test_matrix_reports_attribution_of_co_lead_catches():
    record = _review_catch()
    roster = {"m": {"attribution": "on_target",
                    "verdict_fingerprint": score.review_fingerprint(record)}}
    text = score.build_matrix([score.score_record(record, roster)])
    assert "Attribution of co-lead catches" in text
    assert "1/1 co-lead rejections state the planted defect" in text


# ── matrix rendering ────────────────────────────────────────────────────────


def test_matrix_reports_every_populated_family_and_the_controls():
    records = [
        score.score_record(_record(id="a", expected="scan", gate={
            "passed": False, "layer_a": [_check("no_fabrication.vocabulary")], "layer_b": [],
            "findings": [], "reason": ""})),
        score.score_record(_record(id="b", family=FAM_CONTROL, expected="pass", gate={
            "passed": True, "layer_a": [], "layer_b": [], "findings": [], "reason": ""})),
    ]
    text = score.build_matrix(records, {"toolkit_sha": "deadbeef"})
    assert "Fabrication, in-vocabulary" in text
    assert "1/1 clean changesets passed" in text
    assert "deadbeef" in text
    assert "Every mutant behaved as the corpus predicted." in text


# ── provenance stamps ───────────────────────────────────────────────────────


def test_missing_toolkit_ref_fails_with_the_fix(tmp_path):
    repo = basepkg.make_synthetic_repo(tmp_path / "repo")
    with pytest.raises(SystemExit) as excinfo:
        harness.resolve_toolkit_ref(repo, "no-such-branch")
    assert "does not resolve" in str(excinfo.value)
    assert "git -C" in str(excinfo.value)


def test_stamps_carry_the_scan_configuration_hash(tmp_path):
    stamps = harness.collect_stamps(tmp_path, "unused", resolve_toolkit=False)
    assert len(stamps["code_gate_sha256"]) == 64
    assert stamps["toolkit_sha"] == "(no real mutant selected)"
