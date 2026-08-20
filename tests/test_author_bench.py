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
