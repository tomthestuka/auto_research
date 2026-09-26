"""One authoring trial: clone, wire the production roles, drive the brief, record the outcome.

Nothing here reimplements the authoring flow. The trial calls ``AnalystLeadAgent.drive_authoring``
exactly as ``consensus.strategy`` does, so what is measured is the lead-driven loop the runtime
actually ships: the Opus lead steering a Sonnet coder through ``drive_coder``, submitting to the
deterministic gate and the provider-decorrelated GPT co-lead, and committing only on gate-pass and
review-pass. The legacy ``propose_and_pr`` auto-loop is deliberately not used; its rework caps are
marked unused on the lead-driven path, so measuring it would measure a path production has left.

Three isolation properties make K trials concurrent and independent:

**A private clone per trial.** The live checkout is never touched, and each trial authors onto its
own branch at the same pinned base, so trials cannot see each other's commits.

**A private ledger and cost meter per trial.** The meter is a ``ContextVar``, so installing it
inside the trial's own task keeps every provider call, and the coder subprocess's own session
spend, booked against that trial alone.

**No human handler.** A blocked coder or lead raises ``HumanInputRequired``, which the trial
records as the ``needs_human`` terminal state. That is a first-class honest outcome (§9.6) and a
row of the paper's own table, so answering on the flow's behalf would erase the measurement.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.author_bench.brief import load_brief
from benchmarks.author_bench.legs import Leg

REPO_ROOT = Path(__file__).resolve().parents[2]

# The settings a pinned lead or reviewer is given, harmonised with the configured OpenAI co-lead's
# (``reasoning.effort: high``, ``max_output_tokens: 16384``) rather than left at each vendor's
# default. The reviewer swap established why: a member left at its own role setting compares
# configurations instead of models.
#
# Every vendor on a grid axis needs an entry, or that axis silently compares a harmonised member
# against a defaulted one -- the exact error this table exists to prevent. Anthropic's analogue of
# a high reasoning effort is adaptive thinking; the three Responses-dialect vendors take
# ``reasoning.effort`` and ``max_output_tokens`` under the names their own adapters send.
#
# Qwen is the one to watch rather than to tune away: Alibaba defaults a Responses call to medium
# effort and high is slower still (measured 2026-08-25, 517 s for an eleven-call schema probe
# against Sonnet's 78 s). It is set to high anyway, because parity across the axis is the property
# the measurement depends on and latency is a cost rather than a confound.
_PINNED_COLEAD_PARAMETERS: dict[str, dict[str, object]] = {
    "anthropic": {"thinking": {"type": "adaptive"}, "max_tokens": 16384},
    "openai": {"max_output_tokens": 16384, "reasoning": {"effort": "high"}},
    "zai": {"max_output_tokens": 16384, "reasoning": {"effort": "high"}},
    "dashscope": {"max_output_tokens": 16384, "reasoning": {"effort": "high"}},
    "deepseek": {"max_output_tokens": 16384, "reasoning": {"effort": "high"}},
}


def build_pinned_lead(leg: Leg, monitor: Any | None) -> Any:
    """The analyst lead a leg pins, built outside the configured role.

    Same construction as the co-lead below and for the same reason: the measurement lives in the
    roster rather than in machine-local config, so a batch is reproducible. Parameters are the
    per-provider pinned set, so a lead swap compares models and not configurations.
    """
    from auto_research.agents import AnalystLeadAgent
    from auto_research.models.providers import provider_for
    from auto_research.models.types import ModelHandle, RoleModelConfig

    member = RoleModelConfig(
        name=f"sweep_lead_{leg.slug}",
        provider=leg.lead_provider,
        model=leg.lead_model,
        # Declared by the roster, not assumed here. The lead IS sent figures, so a vision-less
        # lead is a configuration error the roster tests catch rather than a mid-trial failure.
        vision=leg.lead_vision,
        documents=True,
        parameters=dict(_PINNED_COLEAD_PARAMETERS.get(leg.lead_provider, {})),
    )
    handle = ModelHandle(
        role="analyst_lead", config=member, provider=provider_for(member.provider)
    )
    return AnalystLeadAgent(model=handle, monitor=monitor)


def build_pinned_colead(leg: Leg, monitor: Any | None) -> Any:
    """The co-lead a decorrelation leg pins, built outside the configured role.

    Needed because ``analyst_colead`` carries only the production OpenAI member, so the
    same-provider arm of an Anthropic author has no member to route to. Building it here keeps the
    measurement out of machine-local config, which is what makes the batch reproducible.
    """
    from auto_research.agents import AnalystColeadAgent
    from auto_research.models.providers import provider_for
    from auto_research.models.types import ModelHandle, RoleModelConfig

    member = RoleModelConfig(
        name=f"sweep_colead_{leg.slug}",
        provider=leg.colead_provider,
        model=leg.colead_model,
        # A text-only reviewer is legitimate -- the co-lead is never sent an image -- so this is
        # declared by the roster rather than assumed true. Declaring vision on a member whose
        # vendor will not serve it would be a false statement of capability.
        vision=leg.colead_vision,
        documents=leg.colead_vision,
        parameters=dict(_PINNED_COLEAD_PARAMETERS.get(leg.colead_provider, {})),
    )
    handle = ModelHandle(
        role="analyst_colead", config=member, provider=provider_for(member.provider)
    )
    return AnalystColeadAgent(model=handle, monitor=monitor)


@dataclass
class TrialConfig:
    """Everything a batch of trials needs that is not the brief itself."""

    toolkit_root: Path
    out_dir: Path
    base_sha: str
    stamps: dict[str, str] = field(default_factory=dict)
    keep_clones: bool = False
    chatter: int = 4
    leg: Leg | None = None
    """The sweep row this batch is running; ``None`` means the configured author, i.e. the
    single-leg reliability batch the benchmark started as."""


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return out.stdout.strip()


def resolve_base_sha(toolkit_root: Path, ref: str) -> str:
    """Resolve the pinned base to a concrete SHA, failing with the fix if it is absent."""
    try:
        return _git(Path(toolkit_root), "rev-parse", "--verify", f"{ref}^{{commit}}")
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"base ref {ref!r} does not resolve in {toolkit_root}.\n"
            f"Trials author from the toolkit state the original authoring started from.\n"
            f"Pass --base-ref <branch|sha> to measure against a different base."
        ) from None


def collect_stamps(toolkit_root: Path, base_ref: str) -> dict[str, str]:
    """Orchestration commit, toolkit base, scan configuration, and the brief's provenance.

    A distribution is only reproducible against the exact flow that produced it, so the stamps
    name the code, the base, and the gate configuration rather than only the date.
    """
    import hashlib

    from benchmarks.author_bench.brief import brief_provenance

    scan_config = REPO_ROOT / "config" / "code_gate.yaml"
    provenance = brief_provenance()
    return {
        "auto_research_sha": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "auto_research_dirty": str(bool(_git(REPO_ROOT, "status", "--porcelain"))),
        "toolkit_base_ref": base_ref,
        "toolkit_base_sha": resolve_base_sha(toolkit_root, base_ref),
        "code_gate_sha256": hashlib.sha256(scan_config.read_bytes()).hexdigest(),
        "brief_run_id": provenance["run_id"],
        "brief_lead_model": provenance["lead_model"],
        # Recorded, not merely described: the lead's human channel is absent here, which is the
        # one place the trial surface departs from an attended production run.
        "human_channel": "none (unattended)",
    }


def prepare_clone(dest: Path, toolkit_root: Path, base_sha: str, branch: str) -> Path:
    """A throwaway clone of the toolkit at the pinned base, on a branch of the trial's own."""
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(toolkit_root), str(dest)],
        check=True, capture_output=True, text=True,
    )
    _git(dest, "checkout", "--quiet", "-b", branch, base_sha)
    return dest


def save_committed_patch(clone: Path, commit_sha: str, trial_dir: Path) -> Path:
    """Persist the committed tool as a patch beside the trial's ledger.

    The clone is deleted when the trial ends, so without this the only surviving record of what
    was committed is the ledger, whose coder tool inputs are capped mid-file. A one-commit
    ``format-patch`` against the pinned base is complete and applies with ``git am`` onto a fresh
    clone at that base, which is what lets a committed changeset be re-gated or re-reviewed later
    without re-authoring it. Failing to write it leaves the trial's own outcome untouched.
    """
    out = trial_dir / "committed.patch"
    try:
        out.write_text(
            _git(clone, "format-patch", "-1", "--stdout", commit_sha), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 — a missing patch never fails a measured trial
        out.write_text(f"<patch unavailable: {type(exc).__name__}: {exc}>\n", encoding="utf-8")
    return out


# ----------------------------------------------------------------------------
# What the ledger says happened
# ----------------------------------------------------------------------------

def summarise_ledger(path: Path) -> dict:
    """Derive a trial's per-round counts from its own ledger.

    The ledger, not the harness, is the record: the gate and review emit structured verdicts
    (``passed`` / ``approved``), so the rounds distribution is read from the events the
    production flow wrote rather than counted by wrapping the hooks.
    """
    if not path.exists():
        return {}
    events: Counter[str] = Counter()
    gate_pass = gate_fail = review_pass = review_fail = 0
    cmd_ok = cmd_failed = 0
    turns_failed = turns_rate_limited = 0
    cost_by_role: Counter[str] = Counter()
    gate_failing_checks: list[str] = []
    review_issues: list[str] = []
    scan_findings: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = row.get("event", "")
        data = row.get("data") or {}
        events[event] += 1
        if event == "gate.result":
            if data.get("passed"):
                gate_pass += 1
            else:
                gate_fail += 1
                gate_failing_checks.extend(data.get("failing_checks") or [])
                scan_findings.extend(
                    c for c in (data.get("failing_checks") or []) if "no_fabrication" in c
                )
        elif event == "review.result":
            if data.get("approved"):
                review_pass += 1
            else:
                review_fail += 1
                review_issues.extend(data.get("issues") or [])
        elif event == "coder.tool_use" and data.get("exit_code") is not None:
            # Whether the harness actually served the model. A coder that cannot execute anything
            # still produces text, still produces tool calls, and still reaches the gate, because
            # the gate runs the tests in its own process — so every other number in this record
            # looks normal while the author is disabled. That is not hypothetical: on 2026-08-20 a
            # probe ran 58 commands with 0 successes, nothing in the summary showed it, and the
            # batch that followed cost US$155 and measured nothing.
            if data.get("exit_code") == 0:
                cmd_ok += 1
            else:
                cmd_failed += 1
        elif event == "coder.turn_end":
            # A *failed* author turn produces no tool calls and no text, so it reaches the lead
            # indistinguishable from an author that chose to do nothing — which is how 670
            # rate-limited GLM turns read as a lazy model for an hour (2026-08-21). Counted here so
            # the record says which it was.
            if "failed" in str(data.get("status") or "").lower():
                turns_failed += 1
            if "ate limit" in str(data.get("error") or ""):
                turns_rate_limited += 1
        elif event == "coder.tool_result":
            # The other harness shape. The Claude SDK reports `is_error` on a separate tool_result
            # rather than an exit code on the tool_use, so counting only the Codex shape recorded a
            # Claude author's 12 blocked calls as "0 ok / 0 failed" — the exact blindness this
            # counter exists to remove, in the harness it was not written for (2026-08-21).
            if data.get("is_error"):
                cmd_failed += 1
            else:
                cmd_ok += 1
        elif event == "cost.call":
            cost_by_role[data.get("role") or "?"] += float(data.get("total_cost_usd") or 0.0)
    return {
        "gate_runs": gate_pass + gate_fail,
        "gate_passes": gate_pass,
        "gate_rejections": gate_fail,
        "review_runs": review_pass + review_fail,
        "review_approvals": review_pass,
        "review_rejections": review_fail,
        "coder_sessions": events.get("coder.session_start", 0),
        "coder_asks_lead": events.get("coder.ask_lead", 0),
        "coder_asks_human": events.get("coder.ask_human", 0),
        "coder_turns_failed": turns_failed,
        "coder_turns_rate_limited": turns_rate_limited,
        "coder_commands_ok": cmd_ok,
        "coder_commands_failed": cmd_failed,
        "coder_committed_itself": events.get("coder.committed", 0),
        "gate_failing_checks": gate_failing_checks,
        "no_fabrication_hits": scan_findings,
        "review_issues": review_issues,
        "cost_by_role": {k: round(v, 4) for k, v in cost_by_role.most_common()},
    }


# ----------------------------------------------------------------------------
# One trial
# ----------------------------------------------------------------------------

async def run_trial(index: int, cfg: TrialConfig) -> dict:
    """Author the fixed brief once, end to end, and return the trial's JSONL record."""
    from auto_research.agents import AnalystLeadAgent
    from auto_research.agents.react import ReadOnlyToolProvider
    from auto_research.hitl import HumanInputRequired
    from auto_research.monitor import RunMonitor
    from auto_research.observability.costs import install_cost_meter
    from auto_research.sources import WebSource
    from auto_research.sources.downloads import papers_dir

    started = time.time()
    # The label names the leg once there is one: with legs running concurrently, two of them both
    # have a trial 1, and a bare "t01" would give two live ledgers the same run id.
    label = f"t{index:02d}" if cfg.leg is None else f"{cfg.leg.slug}-t{index:02d}"
    trial_dir = cfg.out_dir / f"trial-{index:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)

    # ── The actors must not be able to read which experiment they are in ──────────────────
    # `label` names the campaign and the leg (`grid_lead_qwen_colead_deepseek-t01`), so it may
    # reach a RECORD or the monitor but never a path an actor can see. It used to be the tempdir
    # prefix and the branch name, which put it in front of all three: the lead typed such a path in
    # 2,641 tool calls and the co-lead in 1,923 (measured on the 2026-08-26 grid), because
    # `ReadOnlyToolProvider` renders its roots into the `read_file`/`list_dir` descriptions the
    # model reads. A co-lead in the correlated arm could therefore read
    # `lead_anthropic_colead_anthropic` — exactly the assignment it must be blind to.
    #
    # `opaque` is what production has always had by accident: the lead's roots there are
    # `run_dir(run_id)`, and a run id is a hex string, so no production actor can learn anything
    # about its run from a path. This makes the bench match that rather than diverge from it.
    opaque = hashlib.sha256(f"{cfg.stamps.get('toolkit_base_sha','')}/{label}".encode()).hexdigest()[:16]
    workdir = Path(tempfile.mkdtemp(prefix=f"ar_run_{opaque}_"))
    record: dict = {"trial": index, "workdir_token": opaque, **cfg.stamps}

    monitor = RunMonitor(
        chatter=cfg.chatter, run_id=f"author-bench-{label}",
        jsonl_path=trial_dir / "monitor.jsonl",
    )
    # Installed inside this trial's own task: the meter is a ContextVar, so a task's copy of the
    # context is what keeps K concurrent trials from pooling their spend into one number.
    meter = install_cost_meter(run_id=f"author-bench-{label}", monitor=monitor)
    monitor.cost_meter = meter

    try:
        # Branch name is opaque for the same reason: it is visible to `git`-adjacent output and
        # to anything that prints the checkout state.
        clone = prepare_clone(
            workdir / "repo", cfg.toolkit_root, cfg.stamps["toolkit_base_sha"], f"authoring-{opaque}"
        )
        spec = load_brief()

        leg_for_lead = cfg.leg
        if leg_for_lead is not None and leg_for_lead.lead_model:
            lead = build_pinned_lead(leg_for_lead, monitor)
        else:
            lead = AnalystLeadAgent(monitor=monitor)
        # The lead's investigation surface, wired as runtime.py wires it: the toolkit it is
        # authoring into, the papers dir, and its own run directory. No human is reachable here,
        # which is the unattended production shape.
        #
        # `workdir` rather than `trial_dir`: the latter is `…/<leg.slug>/trial-NN`, organised by
        # experimental arm for the human reading results, and putting it here published the arm to
        # the lead. The authoring worktrees live under `workdir` now, so the lead still reads
        # everything it needs to and the leg-named tree holds only records no actor sees.
        lead.tools = ReadOnlyToolProvider(
            roots=[clone, papers_dir(), workdir],
            web_source=WebSource(),
            include_data_catalog=True,
            include_text_search=False,
        )
        # The lead steers its coder with `message(to="coder")` (E12), so it is put on a quick
        # channel through production's seam: a one-seat channel, no human (unattended), keyed by
        # the opaque token so no path names the arm.
        from auto_research.graph.runtime import attach_channel

        lead.agent_key = "analyst_lead"
        attach_channel([lead], run_id=f"ar_{opaque}", run_control=lead.ensure_run_control(),
                       human_handler=None, monitor=monitor)

        leg = cfg.leg
        # A leg supplies the author member, and for a pinned-reviewer leg the co-lead too.
        # Everything else about the call is identical across legs, which is the whole point: what
        # varies between rows is the author or the reviewer, never the loop.
        colead = build_pinned_colead(leg, monitor) if leg is not None and leg.colead_model else None
        result = await lead.drive_authoring(
            spec,
            toolkit_root=clone,
            # Under `workdir` (opaque), not `trial_dir` (leg-named): the co-lead's own roots are
            # derived from this worktree by `make_review_hook`, so a leg-named base leaked the arm
            # to the reviewer as well as to the lead.
            worktree_base=workdir / "authoring",
            direct_branch=f"authoring-{opaque}",
            # colead None => built from config, provider-decorrelated from the author
            colead=colead,
            coder=None if leg is None else leg.member(),
            allow_correlated_review=bool(leg is not None and leg.correlated),
            human_handler=None,  # a raise becomes the needs_human terminal, never an answer
        )
        record["status"] = result.status
        record["commit_sha"] = result.commit_sha
        record["reason"] = result.reason
        record["branch"] = result.branch
        if result.status == "committed" and result.commit_sha:
            record["committed_files"] = _git(
                clone, "show", "--name-only", "--pretty=format:", result.commit_sha
            ).split()
            record["patch"] = str(save_committed_patch(clone, result.commit_sha, trial_dir))
    except HumanInputRequired as exc:
        # §9.8 fail-safe: the flow needed a human and none was wired. An honest terminal, and the
        # one the paper's table names, so it is recorded rather than answered around.
        record["status"] = "needs_human"
        record["reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 — one broken trial must not end the batch
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # The authoring worktrees moved under `workdir` so no actor could read the leg from their
        # path — which also put them in line to be swept. §9.9 writes a discarded attempt's diff
        # there as `tool_author_failed_*.patch`, and that is the only record of what a
        # non-committing attempt actually wrote, so rescue it into the record tree first. Records
        # are organised by leg on purpose; the human reads them and no actor ever sees them.
        try:
            saved = []
            for patch in sorted((workdir / "authoring").rglob("tool_author_failed*.patch")):
                dest = trial_dir / patch.name
                shutil.copy2(patch, dest)
                saved.append(str(dest))
            if saved:
                record["failed_attempt_patches"] = saved
        except Exception as exc:  # noqa: BLE001 — a diagnostic must never fail a trial
            record["failed_attempt_patches_error"] = f"{type(exc).__name__}: {exc}"
        if cfg.keep_clones:
            record["clone"] = str(workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    # The row names its own author and reviewer. Without this a merged multi-leg report cannot
    # group its records, and a rescored batch cannot be told apart from another leg's.
    if cfg.leg is not None:
        record["leg"] = cfg.leg.slug
        record["coder"] = cfg.leg.model
        record["coder_provider"] = cfg.leg.provider
        record["coder_harness"] = cfg.leg.harness
        record["lead"] = cfg.leg.lead_model or "(configured)"
        record["lead_correlated_with_colead"] = cfg.leg.lead_correlated
        record["colead"] = cfg.leg.colead_model or "(configured)"
        record["correlated_review"] = cfg.leg.correlated
    record["duration_s"] = round(time.time() - started, 2)
    record["cost_usd"] = round(getattr(meter, "total_usd", 0.0), 4)
    record.update(summarise_ledger(trial_dir / "monitor.jsonl"))
    return record
