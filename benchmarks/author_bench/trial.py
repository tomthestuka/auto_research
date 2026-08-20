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

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class TrialConfig:
    """Everything a batch of trials needs that is not the brief itself."""

    toolkit_root: Path
    out_dir: Path
    base_sha: str
    stamps: dict[str, str] = field(default_factory=dict)
    keep_clones: bool = False
    chatter: int = 4


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
    label = f"t{index:02d}"
    trial_dir = cfg.out_dir / f"trial-{index:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"author_bench_{label}_"))
    record: dict = {"trial": index, **cfg.stamps}

    monitor = RunMonitor(
        chatter=cfg.chatter, run_id=f"author-bench-{label}",
        jsonl_path=trial_dir / "monitor.jsonl",
    )
    # Installed inside this trial's own task: the meter is a ContextVar, so a task's copy of the
    # context is what keeps K concurrent trials from pooling their spend into one number.
    meter = install_cost_meter(run_id=f"author-bench-{label}", monitor=monitor)
    monitor.cost_meter = meter

    try:
        clone = prepare_clone(
            workdir / "repo", cfg.toolkit_root, cfg.stamps["toolkit_base_sha"], f"author-bench-{label}"
        )
        spec = load_brief()

        lead = AnalystLeadAgent(monitor=monitor)
        # The lead's investigation surface, wired as runtime.py wires it: the toolkit it is
        # authoring into, the papers dir, and its own trial directory. plan_and_debate needs a
        # human and is off here, which is the unattended production shape.
        lead.tools = ReadOnlyToolProvider(
            roots=[clone, papers_dir(), trial_dir],
            web_source=WebSource(),
            include_data_catalog=True,
            include_plan_and_debate=False,
            include_text_search=False,
            asked_by="analyst_lead",
            model=getattr(getattr(lead, "model", None), "name", "") or "",
        )

        result = await lead.drive_authoring(
            spec,
            toolkit_root=clone,
            worktree_base=trial_dir / "authoring",
            direct_branch=f"author-bench-{label}",
            colead=None,        # built from config: provider-decorrelated from the Claude author
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
    except HumanInputRequired as exc:
        # §9.8 fail-safe: the flow needed a human and none was wired. An honest terminal, and the
        # one the paper's table names, so it is recorded rather than answered around.
        record["status"] = "needs_human"
        record["reason"] = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 — one broken trial must not end the batch
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if cfg.keep_clones:
            record["clone"] = str(workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    record["duration_s"] = round(time.time() - started, 2)
    record["cost_usd"] = round(getattr(meter, "total_usd", 0.0), 4)
    record.update(summarise_ledger(trial_dir / "monitor.jsonl"))
    return record
