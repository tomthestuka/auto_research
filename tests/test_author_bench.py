"""Deterministic tests for the authoring-reliability benchmark (``benchmarks/author_bench``).

No LLM, no coder subprocess, no spend: the trial is exercised with a fake lead so that the
plumbing a live batch depends on — the private clone, the per-trial ledger and cost meter, the
ledger-derived round counts, and the terminal-state classification — is proven before K real
trials are launched. The toolkit-dependent test skips when the checkout is absent, so this suite
passes on a fresh clone.

Run from the repository root: ``conda run -n auto_research python -m pytest tests/test_author_bench.py``.
"""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from auto_research.schemas import MergeFlowResult

from benchmarks.author_bench import brief, trial
from benchmarks.author_bench.score import build_report, summarise

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── the frozen brief ────────────────────────────────────────────────────────


def test_brief_validates_as_a_toolspec():
    spec = brief.load_brief()
    assert spec.proposed_tool_name == "ixpe_pa_pd_stability"
    assert spec.disposition == "extend_existing"
    assert spec.expected_behavior.strip()


def test_brief_is_plan_grade_and_names_its_validation_data():
    """The instrument is a real lead's brief: algorithm-level behaviour plus named inputs."""
    spec = brief.load_brief()
    assert len(spec.expected_behavior) > 1500
    assert spec.validation_data
    assert spec.proposed_checks


def test_brief_provenance_names_the_run_it_came_from():
    provenance = brief.brief_provenance()
    assert provenance["run_id"]
    assert provenance["recovered_from"].endswith(".jsonl")


# ── ledger summarisation ────────────────────────────────────────────────────


def _ledger(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_summarise_ledger_reads_verdicts_from_the_events_not_the_summaries(tmp_path):
    """Gate and review verdicts are structured fields; a summary string is never parsed."""
    path = _ledger(tmp_path / "monitor.jsonl", [
        {"event": "coder.session_start", "data": {}},
        {"event": "gate.result", "summary": "gate passed",
         "data": {"passed": False, "failing_checks": ["[A] no_fabrication.vocabulary"]}},
        {"event": "gate.result", "summary": "gate rejected", "data": {"passed": True}},
        {"event": "review.result", "summary": "co-lead approved", "data": {"approved": False, "issues": ["x"]}},
        {"event": "review.result", "data": {"approved": True}},
        {"event": "coder.ask_lead", "data": {}},
        {"event": "cost.call", "data": {"role": "coder", "total_cost_usd": 1.5}},
        {"event": "cost.call", "data": {"role": "coder", "total_cost_usd": 0.5}},
        {"event": "cost.call", "data": {"role": "analyst_lead", "total_cost_usd": 0.25}},
    ])
    got = trial.summarise_ledger(path)
    assert (got["gate_runs"], got["gate_passes"], got["gate_rejections"]) == (2, 1, 1)
    assert (got["review_runs"], got["review_approvals"], got["review_rejections"]) == (2, 1, 1)
    assert got["coder_sessions"] == 1 and got["coder_asks_lead"] == 1
    assert got["no_fabrication_hits"] == ["[A] no_fabrication.vocabulary"]
    assert got["cost_by_role"] == {"coder": 2.0, "analyst_lead": 0.25}


def test_summarise_ledger_survives_a_truncated_line(tmp_path):
    path = tmp_path / "monitor.jsonl"
    path.write_text('{"event": "gate.result", "data": {"passed": true}}\n{"event": "gat',
                    encoding="utf-8")
    assert trial.summarise_ledger(path)["gate_passes"] == 1


def test_summarise_ledger_of_a_missing_file_is_empty(tmp_path):
    assert trial.summarise_ledger(tmp_path / "absent.jsonl") == {}


# ── one trial, with a fake lead ─────────────────────────────────────────────


class _FakeLead:
    """Stands in for ``AnalystLeadAgent``: records what it was handed, returns a terminal."""

    def __init__(self, result=None, raises=None, **_kwargs):
        self.tools = None
        self.model = None
        self._result = result
        self._raises = raises
        self.seen: dict = {}

    async def drive_authoring(self, spec, **kwargs):
        self.seen = {"spec": spec, **kwargs}
        if self._raises is not None:
            raise self._raises
        return self._result


def _toolkit_root() -> Path:
    return Path(yaml.safe_load(
        (REPO_ROOT / "config" / "orchestration.yaml").read_text(encoding="utf-8")
    )["toolkit_root"])


def _skip_without_toolkit() -> Path:
    root = _toolkit_root()
    if not (root / ".git").exists():
        pytest.skip(f"toolkit checkout absent at {root}")
    return root


def _install_fake_lead(monkeypatch, lead):
    import auto_research.agents as agents

    monkeypatch.setattr(agents, "AnalystLeadAgent", lambda **kwargs: lead)


def _config(tmp_path, root) -> trial.TrialConfig:
    sha = trial.resolve_base_sha(root, brief.BASE_SHA)
    return trial.TrialConfig(
        toolkit_root=root, out_dir=tmp_path / "out", base_sha=brief.BASE_SHA,
        stamps={"toolkit_base_sha": sha}, keep_clones=False, chatter=1,
    )


def test_trial_clones_at_the_pinned_base_and_records_a_commit(tmp_path, monkeypatch):
    """The trial authors from the state the original authoring started from, on its own branch."""
    root = _skip_without_toolkit()
    lead = _FakeLead(result=MergeFlowResult(
        tool_spec_id="ixpe_pa_pd_stability", status="committed", branch="author-bench-t01",
        commit_sha="", reason="gate-pass and co-lead review-pass",
    ))
    _install_fake_lead(monkeypatch, lead)
    cfg = _config(tmp_path, root)

    record = asyncio.run(trial.run_trial(1, cfg))

    assert record["status"] == "committed"
    assert record["trial"] == 1
    # The lead was handed a clone, never the live checkout, and the production authoring shape.
    assert lead.seen["toolkit_root"] != root
    assert lead.seen["direct_branch"] == "author-bench-t01"
    assert lead.seen["human_handler"] is None
    assert lead.seen["spec"].proposed_tool_name == "ixpe_pa_pd_stability"


def test_save_committed_patch_writes_a_patch_that_reapplies_onto_the_base(tmp_path):
    """The patch is the surviving record of a committed tool, so it has to re-create the tree.

    The clone a trial authors in is deleted when the trial ends and the ledger caps coder tool
    inputs mid-file, so a patch that does not apply leaves a committed changeset unrecoverable.
    """
    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "--quiet")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "keep.txt").write_text("base\n", encoding="utf-8")
    git(origin, "add", "keep.txt")
    git(origin, "commit", "--quiet", "-m", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(origin),
                          capture_output=True, text=True, check=True).stdout.strip()

    clone = tmp_path / "clone"
    trial.prepare_clone(clone, origin, base, "author-bench-t01")
    (clone / "tool.py").write_text("def tool():\n    return 1\n", encoding="utf-8")
    git(clone, "add", "tool.py")
    git(clone, "commit", "--quiet", "-m", "add the tool")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(clone),
                         capture_output=True, text=True, check=True).stdout.strip()

    trial_dir = tmp_path / "trial-01"
    trial_dir.mkdir()
    patch = trial.save_committed_patch(clone, sha, trial_dir)

    assert patch == trial_dir / "committed.patch"
    assert "add the tool" in patch.read_text(encoding="utf-8")

    # The point of keeping it: a fresh clone at the pinned base plus the patch is the tree again.
    fresh = tmp_path / "fresh"
    trial.prepare_clone(fresh, origin, base, "replay")
    git(fresh, "config", "user.email", "t@t")
    git(fresh, "config", "user.name", "t")
    git(fresh, "am", str(patch))
    assert (fresh / "tool.py").read_text(encoding="utf-8") == "def tool():\n    return 1\n"


def test_a_committed_trial_records_the_patch_beside_its_ledger(tmp_path, monkeypatch):
    """A committed record names a patch on disk, so the changeset outlives the deleted clone."""
    root = _skip_without_toolkit()

    class _CommittingLead(_FakeLead):
        """Commits into the clone it was handed, the way the authoring flow does."""

        async def drive_authoring(self, spec, **kwargs):
            clone = kwargs["toolkit_root"]
            (clone / "xspec_utils" / "lib" / "bench_probe.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
            for args in (
                ("add", "xspec_utils/lib/bench_probe.py"),
                ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--quiet", "-m", "probe"),
            ):
                subprocess.run(["git", *args], cwd=str(clone), check=True,
                               capture_output=True, text=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(clone),
                                 capture_output=True, text=True, check=True).stdout.strip()
            self.seen = {"spec": spec, **kwargs}
            return MergeFlowResult(
                tool_spec_id="ixpe_pa_pd_stability", status="committed",
                branch="author-bench-t01", commit_sha=sha, reason="gate-pass and review-pass",
            )

    lead = _CommittingLead()
    _install_fake_lead(monkeypatch, lead)

    record = asyncio.run(trial.run_trial(1, _config(tmp_path, root)))

    assert record["status"] == "committed"
    assert record["committed_files"] == ["xspec_utils/lib/bench_probe.py"]
    patch = Path(record["patch"])
    assert patch.exists() and "bench_probe" in patch.read_text(encoding="utf-8")
    # The clone is gone; the patch is what is left.
    assert not (tmp_path / "out" / "trial-01" / "repo").exists()


def test_trial_records_a_human_raise_as_its_own_terminal_state(tmp_path, monkeypatch):
    """An unattended raise is an honest terminal, never an error and never answered for."""
    from auto_research.hitl import HumanInputRequired

    root = _skip_without_toolkit()
    _install_fake_lead(monkeypatch, _FakeLead(raises=HumanInputRequired("no handler wired")))

    record = asyncio.run(trial.run_trial(2, _config(tmp_path, root)))

    assert record["status"] == "needs_human"
    assert "no handler wired" in record["reason"]


def test_trial_records_an_unexpected_failure_without_ending_the_batch(tmp_path, monkeypatch):
    root = _skip_without_toolkit()
    _install_fake_lead(monkeypatch, _FakeLead(raises=RuntimeError("git exploded")))

    record = asyncio.run(trial.run_trial(3, _config(tmp_path, root)))

    assert record["status"] == "error"
    assert "git exploded" in record["error"]


def test_concurrent_trials_do_not_pool_their_spend(tmp_path, monkeypatch):
    """The cost meter is a ContextVar, which is what keeps K concurrent trials separable.

    If a trial installed its meter outside its own task, every trial would report the batch's
    running total instead of its own, and the cost distribution would be meaningless.
    """
    from auto_research.observability.costs import current_cost_meter, install_cost_meter

    async def scenario():
        async def leg(amount: float) -> float:
            meter = install_cost_meter(run_id=f"leg-{amount}")
            await asyncio.sleep(0)
            await meter.record_session_total(
                role="coder", member="m", provider="anthropic", model="claude-sonnet-5",
                session_id="session", cumulative_usd=amount,
            )
            await asyncio.sleep(0)
            assert current_cost_meter() is meter
            return meter.total_usd

        return await asyncio.gather(
            *(asyncio.create_task(leg(a)) for a in (1.0, 2.0, 3.0))
        )

    assert asyncio.run(scenario()) == [1.0, 2.0, 3.0]


# ── the report ──────────────────────────────────────────────────────────────


def _record(trial_index: int, status: str, **kwargs) -> dict:
    base = {"trial": trial_index, "status": status, "cost_usd": 10.0, "duration_s": 600.0,
            "gate_runs": 1, "gate_passes": 1, "gate_rejections": 0, "review_runs": 1,
            "review_approvals": 1, "review_rejections": 0, "coder_sessions": 1,
            "coder_asks_lead": 0, "no_fabrication_hits": []}
    base.update(kwargs)
    return base


def test_terminal_states_partition_the_trials():
    """Every trial lands in exactly one terminal row; rounds are reported separately."""
    records = [_record(1, "committed"), _record(2, "failed"), _record(3, "needs_human"),
               _record(4, "error")]
    summary = summarise(records)
    assert sum(len(rows) for rows in summary["by_state"].values()) == len(records)
    assert summary["other"] == []


def test_report_separates_rounds_from_terminal_states():
    """A recovered gate rejection is a round inside a committed trial, not a fifth outcome."""
    records = [_record(1, "committed", gate_runs=3, gate_rejections=2, review_runs=2,
                       review_rejections=1)]
    report = build_report(records, {"toolkit_base_sha": "deadbeef"})
    assert "## Terminal states" in report and "## Rounds inside a trial" in report
    committed_row = [l for l in report.splitlines() if l.startswith("| Committed |")][0]
    assert committed_row.split("|")[2].strip() == "1"


def test_report_lists_fabrication_signals_as_signals(tmp_path):
    records = [_record(1, "failed", no_fabrication_hits=["[A] no_fabrication.vocabulary"])]
    report = build_report(records, {})
    assert "hand-read" in report
    assert "no_fabrication.vocabulary" in report


def test_report_states_why_each_non_committing_trial_ended():
    records = [_record(1, "failed", reason="submit cap exhausted without an approved review")]
    assert "submit cap exhausted" in build_report(records, {})


# ── targeted re-runs ────────────────────────────────────────────────────────


def _args(**kwargs):
    import argparse

    return argparse.Namespace(**{"k": 10, "only": None, **kwargs})


def test_trial_indices_default_to_the_whole_batch():
    from benchmarks.author_bench.__main__ import trial_indices

    assert trial_indices(_args(k=3)) == [1, 2, 3]


def test_only_reruns_the_named_trials_keeping_their_numbers():
    """A trial's number is its identity across batches, so a re-run does not renumber."""
    from benchmarks.author_bench.__main__ import trial_indices

    assert trial_indices(_args(only="5,6,9")) == [5, 6, 9]


def test_a_rerun_replaces_the_trial_it_repairs_rather_than_joining_it(tmp_path):
    """Merging batches must not double-count: the later record wins for the same number."""
    from benchmarks.author_bench.__main__ import rescore

    first, second = tmp_path / "a", tmp_path / "b"
    for d in (first, second):
        d.mkdir()
    (first / "records.jsonl").write_text(
        "".join(json.dumps(_r) + "\n" for _r in [
            {"trial": 1, "status": "committed", "cost_usd": 10.0, "duration_s": 60.0},
            {"trial": 2, "status": "failed", "cost_usd": 2.0, "duration_s": 10.0},
        ]), encoding="utf-8")
    (second / "records.jsonl").write_text(
        json.dumps({"trial": 2, "status": "committed", "cost_usd": 20.0, "duration_s": 90.0}) + "\n",
        encoding="utf-8")

    assert rescore([first, second]) == 0
    report = (second / "report.md").read_text(encoding="utf-8")
    assert "| Committed | 2 |" in report          # both trials, the repaired one counted once
    assert "| Honest failure (no commit) | 0 |" in report


# ── the trial surface matches production ────────────────────────────────────


def _surface(*, human_handler, tmp_path):
    """The lead's tool names, built exactly as runtime.py and the authoring loop build them."""
    from auto_research.agents.react import ReadOnlyToolProvider

    class _Session:  # only its presence matters to the provider
        worktree = tmp_path

    provider = ReadOnlyToolProvider(
        roots=[tmp_path], retrieval=None, web_source=object(),
        include_data_catalog=True,
        include_plan_and_debate=human_handler is not None,
        include_text_search=False, authoring_session=_Session(),
        human_handler=human_handler, asked_by="analyst_lead", model="m",
    )
    return [d["name"] for d in provider.tool_definitions()]


def test_the_trial_surface_differs_from_attended_production_only_by_the_human_channel(tmp_path):
    """The measurement is only about authoring if the surface is production's.

    Trials run unattended, so the lead loses `ask_human` and the budget-free `plan_and_debate`
    design channel. Everything else it can do must be identical, and this pins that: a tool added
    to (or removed from) the production loop fails here until the benchmark's label is revisited.
    A lead that cannot spend a question can only spend another rework round, which is why the
    honest-failure rate these trials produce is an unattended figure.
    """

    attended = _surface(human_handler=object(), tmp_path=tmp_path)
    unattended = _surface(human_handler=None, tmp_path=tmp_path)

    assert set(attended) - set(unattended) == {"ask_human", "plan_and_debate"}
    assert not set(unattended) - set(attended)
    for name in ("drive_coder", "submit_for_review", "respond_to_review", "read_file", "list_dir"):
        assert name in unattended


def test_the_roles_use_the_shipped_prompts_unmodified(tmp_path):
    """A trial measures the shipped pipeline, so no prompt may be substituted for the benchmark."""
    from auto_research.agents import AnalystLeadAgent
    from auto_research.agents.prompts import load_prompt

    assert AnalystLeadAgent().prompt == load_prompt("analyst_lead")
    assert load_prompt("coder").strip()  # the coder's prompt is loaded by build_authoring_session


def test_records_state_that_trials_ran_without_a_human_channel():
    """The label lives in the record, not only in the write-up."""
    from benchmarks.author_bench.trial import collect_stamps

    root = _toolkit_root()
    if not (root / ".git").exists():
        pytest.skip(f"toolkit checkout absent at {root}")
    assert collect_stamps(root, brief.BASE_SHA)["human_channel"] == "none (unattended)"


# ── the sweep roster ────────────────────────────────────────────────────────


def test_no_tier_row_shares_the_configured_coleads_provider():
    """The tier axis is only interpretable if the reviewer is the same in every tier row.

    The configured co-lead is OpenAI, so an OpenAI author would force the reviewer to change and
    the row would vary two things at once. Such an author belongs in a decorrelation row, labelled
    as correlated, which is exactly where the roster puts it.
    """
    from auto_research.models import resolve

    from benchmarks.author_bench.legs import ROSTER

    colead_provider = resolve("analyst_colead").handles[0].config.provider
    tier = [leg for leg in ROSTER if leg.purpose == "tier"]
    assert tier, "the roster has no tier rows"
    for leg in tier:
        assert leg.provider != colead_provider, (
            f"tier row {leg.slug} shares the co-lead's provider {colead_provider!r}, so its "
            "reviewer cannot be held fixed"
        )
        assert leg.colead_provider is None, f"tier row {leg.slug} pins a reviewer"
        assert not leg.correlated


def test_every_decorrelation_row_is_correlated_and_says_so():
    """A decorrelation row exists to measure the same-provider arrangement, so it must actually
    be one; the flag that permits it is what the run passes to the review hook."""
    from benchmarks.author_bench.legs import ROSTER

    rows = [leg for leg in ROSTER if leg.purpose == "decorrelation"]
    assert rows, "the roster has no decorrelation rows"
    for leg in rows:
        assert leg.correlated, f"{leg.slug} is a decorrelation row but is not correlated"


def test_every_leg_is_a_pairing_the_harness_table_supports():
    """A leg naming an impossible author pairing would fail inside the author subprocess."""
    from auto_research.sandbox.coder.harness import HARNESS_ENDPOINTS

    from benchmarks.author_bench.legs import ROSTER

    for leg in ROSTER:
        key = (leg.provider, leg.harness)
        assert key in HARNESS_ENDPOINTS, f"{leg.slug}: {leg.provider}+{leg.harness} does not exist"
        documented = HARNESS_ENDPOINTS[key]
        if documented is None:
            continue
        assert leg.base_url == documented, (
            f"{leg.slug} points at {leg.base_url!r}; the vendor documents {documented!r}"
        )


def test_the_claude_legs_cap_their_own_concurrency():
    """The subscription session cap is a measured hazard, not a remembered one: it contaminated
    six of ten trials in the first batch. It belongs in the roster, where a batch cannot forget
    it, rather than in whoever types the command."""
    from benchmarks.author_bench.legs import ROSTER

    for leg in ROSTER:
        if leg.harness == "claude_sdk":
            assert leg.max_concurrency is not None and leg.max_concurrency <= 2, leg.slug
        else:
            assert leg.max_concurrency is None, leg.slug


def test_a_leg_supplies_the_author_member_and_its_correlation_setting(tmp_path, monkeypatch):
    """What a leg actually changes about the call: the author member and, for a correlated row,
    the switch that lets a same-provider reviewer run. Everything else stays identical."""
    from benchmarks.author_bench.legs import BY_SLUG

    root = _skip_without_toolkit()
    lead = _FakeLead(result=MergeFlowResult(
        tool_spec_id="ixpe_pa_pd_stability", status="committed", branch="b", commit_sha="",
        reason="ok",
    ))
    _install_fake_lead(monkeypatch, lead)

    leg = BY_SLUG["deepseek_v4_flash"]
    cfg = _config(tmp_path, root)
    cfg = trial.TrialConfig(
        toolkit_root=cfg.toolkit_root, out_dir=cfg.out_dir, base_sha=cfg.base_sha,
        stamps=cfg.stamps, keep_clones=False, chatter=1, leg=leg,
    )
    record = asyncio.run(trial.run_trial(1, cfg))

    member = lead.seen["coder"]
    assert member.model == "deepseek-v4-flash"
    assert member.provider == "deepseek"
    assert member.harness == "codex"
    assert lead.seen["allow_correlated_review"] is False
    assert lead.seen["colead"] is None, "a tier row takes the configured decorrelated routing"
    # The record names its own author, or a merged multi-leg report cannot group it.
    assert record["leg"] == "deepseek_v4_flash"
    assert record["coder"] == "deepseek-v4-flash"
    assert record["correlated_review"] is False


def test_a_correlated_leg_pins_its_reviewer_and_opens_the_switch(tmp_path, monkeypatch):
    root = _skip_without_toolkit()
    lead = _FakeLead(result=MergeFlowResult(
        tool_spec_id="ixpe_pa_pd_stability", status="committed", branch="b", commit_sha="",
        reason="ok",
    ))
    _install_fake_lead(monkeypatch, lead)

    from benchmarks.author_bench.legs import BY_SLUG

    leg = BY_SLUG["claude_sonnet_5_correlated"]
    base = _config(tmp_path, root)
    cfg = trial.TrialConfig(
        toolkit_root=base.toolkit_root, out_dir=base.out_dir, base_sha=base.base_sha,
        stamps=base.stamps, keep_clones=False, chatter=1, leg=leg,
    )
    record = asyncio.run(trial.run_trial(1, cfg))

    assert lead.seen["allow_correlated_review"] is True
    colead = lead.seen["colead"]
    assert colead is not None, "the same-provider arm has no configured member to route to"
    assert colead.model.config.model == "claude-opus-5"
    assert colead.model.config.provider == "anthropic"
    assert record["correlated_review"] is True


def test_a_pinned_reviewer_is_harmonised_with_the_configured_one():
    """A member left at its own default would compare configurations rather than models, which is
    the correction the reviewer swap already had to make."""
    from benchmarks.author_bench.legs import BY_SLUG
    from benchmarks.author_bench.trial import build_pinned_colead

    colead = build_pinned_colead(BY_SLUG["claude_sonnet_5_correlated"], None)
    assert colead.model.config.parameters["max_tokens"] == 16384


# ── the multi-leg report ────────────────────────────────────────────────────


def _leg_record(leg, trial_no, status, cost, colead="(configured)", correlated=False):
    return {
        "trial": trial_no, "status": status, "cost_usd": cost, "duration_s": 60.0,
        "leg": leg, "coder": f"{leg}-model", "colead": colead,
        "correlated_review": correlated, "review_runs": 2,
    }


def test_a_multi_leg_report_partitions_by_leg_not_by_pooling():
    """Pooling terminal states across legs would average away the very axis being measured, so a
    multi-leg report grows a per-leg table and the per-trial rows name their leg."""
    records = [
        _leg_record("alpha", 1, "committed", 10.0),
        _leg_record("alpha", 2, "failed", 20.0),
        _leg_record("beta", 1, "committed", 30.0, colead="pinned", correlated=True),
    ]
    report = build_report(records, {})

    assert "## By leg" in report
    assert "| alpha |" in report and "| beta |" in report
    assert "pinned [correlated]" in report
    # Trial numbers repeat across legs, so a row without its leg is ambiguous.
    assert "| Leg | Trial |" in report


def test_a_single_leg_report_gains_no_leg_column():
    """The reliability batch's own report must not change shape because the sweep exists: its
    numbers are already in the paper."""
    records = [_leg_record("alpha", 1, "committed", 10.0)]
    report = build_report(records, {})
    assert "## By leg" not in report
    assert "| Leg | Trial |" not in report


def test_records_from_different_legs_never_replace_each_other_on_rescore(tmp_path):
    """Every leg numbers its trials from one, so keying a merge on the trial number alone would
    let one leg's trial silently overwrite another's."""
    from benchmarks.author_bench.__main__ import rescore

    first = tmp_path / "a"
    second = tmp_path / "b"
    for path, leg in ((first, "alpha"), (second, "beta")):
        path.mkdir()
        (path / "records.jsonl").write_text(
            json.dumps(_leg_record(leg, 1, "committed", 5.0)) + "\n", encoding="utf-8"
        )

    rescore([first, second])
    report = (second / "report.md").read_text(encoding="utf-8")
    assert "| alpha |" in report and "| beta |" in report


# ── the per-leg vendor probe ────────────────────────────────────────────────


class _ProbeError(Exception):
    """A stand-in SDK error carrying the two fields the key classifiers read."""

    def __init__(self, message, *, status_code=None, type_=None, code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if type_ is not None:
            self.type = type_
        if code is not None:
            self.code = code


def _spent_balance():
    # OpenAI reports a spent balance as a 429; only `code` separates it from a refilling one.
    return _ProbeError("You have no credits remaining.", status_code=429,
                       type_="insufficient_quota", code="credit_balance_exhausted")


def _install_fake_openai(monkeypatch, outcomes: dict[str, object]):
    """A client whose behaviour depends on the key it was handed."""
    import openai

    seen: list[str] = []

    class _Responses:
        def __init__(self, key):
            self._key = key

        def create(self, **_kw):
            seen.append(self._key)
            outcome = outcomes.get(self._key)
            if isinstance(outcome, Exception):
                raise outcome
            return object()

    class _Client:
        def __init__(self, api_key=None, base_url=None):
            self.responses = _Responses(api_key)

    monkeypatch.setattr(openai, "OpenAI", _Client, raising=True)
    return seen


def test_the_probe_fails_over_and_leaves_the_pool_on_the_key_that_answered(monkeypatch):
    """The pool advances only when a call fails, so at launch it still holds a spent primary.

    The probe is the run's first real call to that vendor, so it fails over: the spent key is
    retired for the process and the batch then launches its authors on the key that answered,
    instead of discovering the dead one inside an author subprocess one trial at a time.
    """
    from auto_research.models import keys as keymod

    from benchmarks.author_bench.__main__ import probe_leg_vendor
    from benchmarks.author_bench.legs import BY_SLUG

    monkeypatch.setenv("OPENAI_API_KEY", "dead")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "live")
    keymod.reset_pools()
    seen = _install_fake_openai(monkeypatch, {"dead": _spent_balance(), "live": None})

    problem = probe_leg_vendor(BY_SLUG["gpt_5_6_terra_correlated"])

    assert problem is None
    assert seen == ["dead", "live"], "the probe must try the live key, not just report the dead one"
    assert keymod.key_pool("openai").active() == "live", (
        "the pool must be left on the key that answered, or the author still launches on the dead one"
    )
    keymod.reset_pools()


def test_the_probe_reports_honestly_when_every_key_is_dead(monkeypatch):
    """Rotation offers each key once and then stops. A batch must not launch on the belief that
    some key works when none does."""
    from auto_research.models import keys as keymod

    from benchmarks.author_bench.__main__ import probe_leg_vendor
    from benchmarks.author_bench.legs import BY_SLUG

    monkeypatch.setenv("OPENAI_API_KEY", "dead1")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "dead2")
    keymod.reset_pools()
    _install_fake_openai(
        monkeypatch, {"dead1": _spent_balance(), "dead2": _spent_balance()}
    )

    problem = probe_leg_vendor(BY_SLUG["gpt_5_6_terra_correlated"])

    assert problem is not None
    assert "every configured key" in problem
    keymod.reset_pools()


def test_the_probe_does_not_rotate_a_vendor_with_no_cited_taxonomy(monkeypatch):
    """A harness-only vendor is deliberately outside the rotating set: no cited error taxonomy, so
    nothing may be inferred from its refusal. Its failure is reported, not routed around."""
    from benchmarks.author_bench.__main__ import probe_leg_vendor
    from benchmarks.author_bench.legs import BY_SLUG

    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    seen = _install_fake_openai(monkeypatch, {"k": _spent_balance()})

    problem = probe_leg_vendor(BY_SLUG["deepseek_v4_flash"])

    assert problem is not None and "refused" in problem
    assert seen == ["k"], "one attempt only: there is nothing to rotate to"


def test_a_claude_leg_has_nothing_to_probe():
    """It authenticates from stored subscription credentials, not a key."""
    from benchmarks.author_bench.__main__ import probe_leg_vendor
    from benchmarks.author_bench.legs import BY_SLUG

    assert probe_leg_vendor(BY_SLUG["claude_sonnet_5_correlated"]) is None


def test_a_leg_cannot_hold_a_global_slot_while_queued_behind_its_own_cap():
    """Lock order decides whether legs interleave or starve each other.

    Taking the batch-wide slot before the per-leg one lets a trial occupy the batch-wide budget
    while it is still waiting for its own leg's cap, so the legs created first hold every slot and
    the later legs never start. Asserted on the source order, because the failure is an ordering
    property that a passing run hides: the batch still completes, just one pair of legs at a time.
    """
    import inspect

    from benchmarks.author_bench import __main__ as cli

    source = inspect.getsource(cli._run)
    assert "async with own, everything:" in source, (
        "the per-leg semaphore must be acquired before the batch-wide one"
    )
    assert "async with everything, own:" not in source


def test_the_record_counts_whether_the_coder_could_execute_anything(tmp_path):
    """Exit codes, not call counts: the one number that separates a weak model from a dead harness.

    An author that cannot run a command still emits text, still calls tools, and can still reach a
    gate pass, because the gate runs the tests in its own process. So every other field in the
    record looks normal while the author is disabled.
    """
    ledger = tmp_path / "monitor.jsonl"
    rows = [
        {"event": "coder.tool_use", "data": {"exit_code": None, "input": "pytest"}},
        {"event": "coder.tool_use", "data": {"exit_code": 0, "input": "pytest"}},
        {"event": "coder.tool_use", "data": {"exit_code": 1, "input": "pytest -x"}},
        {"event": "coder.tool_use", "data": {"exit_code": 2, "input": "python -c ..."}},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    got = trial.summarise_ledger(ledger)

    assert got["coder_commands_ok"] == 1
    assert got["coder_commands_failed"] == 2, "a pending call (exit_code None) counts as neither"


def test_a_report_shouts_when_a_coder_could_not_execute_anything():
    """It has to be visible where the numbers are read. Buried in a ledger it was missed for an
    entire batch, at a cost of US$155 and no measurement."""
    dead = _leg_record("alpha", 1, "failed", 3.0)
    dead.update({"coder_commands_ok": 0, "coder_commands_failed": 58})
    fine = _leg_record("beta", 1, "committed", 9.0)
    fine.update({"coder_commands_ok": 81, "coder_commands_failed": 4})

    report = build_report([dead, fine], {})

    assert "could not execute anything" in report
    assert "alpha trial 01: 0 succeeded, 58 failed" in report
    assert "beta trial 01" not in report.split("## Fabrication")[0].split("WARNING")[-1]


def test_no_warning_when_every_coder_could_execute():
    fine = _leg_record("beta", 1, "committed", 9.0)
    fine.update({"coder_commands_ok": 81, "coder_commands_failed": 4})
    assert "could not execute anything" not in build_report([fine], {})


def test_the_command_counter_reads_both_harness_shapes(tmp_path):
    """Codex reports `exit_code` on the tool_use; the Claude SDK reports `is_error` on a separate
    tool_result. Counting one shape only made a Claude author's blocked calls read as no calls."""
    ledger = tmp_path / "monitor.jsonl"
    rows = [
        {"event": "coder.tool_use", "data": {"exit_code": 0}},
        {"event": "coder.tool_use", "data": {"exit_code": 1}},
        {"event": "coder.tool_result", "data": {"is_error": True, "text": "denied"}},
        {"event": "coder.tool_result", "data": {"is_error": False, "text": "ok"}},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    got = trial.summarise_ledger(ledger)

    assert got["coder_commands_ok"] == 2
    assert got["coder_commands_failed"] == 2
