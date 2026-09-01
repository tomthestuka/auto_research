"""One trial: launch a whole research run into its own world, watch it, record what happened.

Nothing here reimplements the pipeline. A trial spawns the production entry point -- the same
command the detached backend spawns -- pointed at the private configuration built for its arm, and
then does only three things: wait, collect, and write one record.

**Why it does not call the public detached entry.** Almost all of that machinery takes an explicit
directory and is reused here: liveness by pid-and-start-time, the terminal-marker check, the
orphaning double-fork. The one thing that cannot be reused is the entry point itself, because it
derives the run directory from *its own* configuration, and the whole point of a trial is that its
configuration is not the batch's. The batch already knows where the trial's runtime directory is --
it built it -- so it addresses that directory and keeps every piece below that line. A narrower
dependency on production, not a fork of it.

**Two outcomes, and conflating them would make the scoring meaningless.**

*Infrastructure* is whether the run got to speak at all: it finished, it crashed leaving a marker,
a human stopped it, or its process vanished writing nothing. Only the last two are the harness's
fault, and only those are re-runnable.

*Measurement* is what the run concluded -- the verdict, whether the reviewer accepted, whether any
role ended needing a human. Under the governing ruling these are all **correct outcomes**: the
pipeline always reaches a verdict, and a verdict of "this is not verifiable", plainly stated, is a
success rather than a failure. Folding them into a failure rate would measure the opposite of what
this sweep claims.

Everything derived is read from the **run's own ledger**, because the production flow already
writes structured verdicts and reading those keeps this a measurement of production rather than of
a wrapper around it. Cost needs no special handling: each trial is its own process, so its spend is
its own by construction and lands in its own ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.science_bench import environment as env_mod
from benchmarks.science_bench.arms import RunSlot
from benchmarks.science_bench.request import load_request, request_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The pinned toolkit state. Every tree authors from the commit before this question's own tool
#: chain existed, so the capability it needs is genuinely absent and must be built through the gate.
BASE_REF = "5accf33"

#: How often a trial's terminal marker is checked. A research run is hours long, so this is about
#: not busy-waiting rather than about latency.
POLL_SECONDS = 15.0


@dataclass
class TrialConfig:
    """Everything a batch of trials needs that is not the arm or the question."""

    workspace_root: Path
    """Where private trial worlds are built. Must sit outside the filesystem allow-list a trial is
    given, so the configuration -- the one file naming the whole arm -- is unreadable by any
    actor."""

    out_dir: Path
    """Where records land. Organised for the human; no actor ever sees it."""

    base_ref: str = BASE_REF
    stamps: dict[str, str] = field(default_factory=dict)
    chatter: int = 4
    keep_artifacts: bool = True
    """Keep each trial's runtime directory: it holds the ledger, the figures and the report, and
    the whole scoring pass is offline over exactly those. The toolkit checkout and the data copy
    are swept either way, being reproducible from the pinned base and the archive."""

    poll_seconds: float = POLL_SECONDS


# ── Stamps ──────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def collect_stamps(toolkit_source: Path, base_ref: str = BASE_REF) -> dict[str, str]:
    """What this batch was measured against: the code, the base, the gate, the question.

    A distribution is reproducible only against the exact flow that produced it, so the stamps name
    the orchestration commit and whether it was dirty, the toolkit base, the hash of the gate's
    scan configuration, and the hash of the question -- the four things that would silently make
    two batches incomparable.
    """
    from auto_research.config_paths import code_gate_yaml

    return {
        "auto_research_sha": _git(REPO_ROOT, "rev-parse", "HEAD"),
        "auto_research_dirty": str(bool(_git(REPO_ROOT, "status", "--porcelain"))),
        "toolkit_base_ref": base_ref,
        "toolkit_base_sha": _git(Path(toolkit_source), "rev-parse", "--verify", f"{base_ref}^{{commit}}"),
        "code_gate_sha256": hashlib.sha256(code_gate_yaml().read_bytes()).hexdigest(),
        "request_sha256": request_sha256(),
        # Recorded rather than merely described: the channel is closed, so every honest-failure
        # figure this batch produces is an unattended one and an upper bound on the attended
        # pipeline's.
        "human_channel": "none (unattended)",
    }


# ── Launching ───────────────────────────────────────────────────────────────


def build_argv(run_id: str, chatter: int) -> list[str]:
    """The production command, with the question passed exactly as the detached backend passes it.

    ``--human none`` is explicit rather than omitted: left unset, the channel resolver auto-detects
    and wires a terminal handler whenever it finds a tty, so a batch launched from a shell would
    silently acquire a human -- and a human answering one trial and not another injects an
    uncontrolled variable straight into the seat comparison.
    """
    posed = load_request()
    argv = [
        sys.executable, "-m", "auto_research", posed.question,
        "--run-id", run_id,
        "--chatter", str(chatter),
        "--human", "none",
    ]
    if posed.context:
        argv += ["--context", posed.context]
    for constraint in (posed.constraints or []):
        argv += ["--constraint", constraint]
    return argv


def launch(
    environment: env_mod.TrialEnvironment, *, chatter: int, prompt_dump: Path | None = None
) -> int:
    """Spawn the run orphaned, into its own configuration, and record its pid where a watcher
    looks for it. Returns the pid.

    ``prompt_dump`` switches on the pipeline's own verbatim recording -- the single chokepoint
    every actor's prompt passes through, added after the 2026-08-27 leaks precisely because a
    hand-reassembled payload contains only the channels its author remembered. Block 0 sets it and
    nothing else does. It is additive: it changes what is written down, never what is sent, which
    is what lets a clean block 0 count as a real measurement.
    """
    from auto_research.config_paths import CONFIG_DIR_ENV
    from auto_research.graph.detached import _proc_create_time, _spawn_orphaned
    from auto_research.models.types import PROMPT_DUMP_ENV

    run_directory = environment.run_dir
    run_directory.mkdir(parents=True, exist_ok=True)
    child_env = {CONFIG_DIR_ENV: str(environment.config_dir)}
    if prompt_dump is not None:
        prompt_dump.parent.mkdir(parents=True, exist_ok=True)
        child_env[PROMPT_DUMP_ENV] = str(prompt_dump)
    pid = _spawn_orphaned(
        build_argv(environment.run_id, chatter),
        run_directory / "backend.log",
        env=child_env,
    )
    (run_directory / "backend.pid").write_text(
        json.dumps({"pid": pid, "create_time": _proc_create_time(pid)}), encoding="utf-8"
    )
    return pid


async def wait_for(run_directory: Path, *, poll_seconds: float) -> str:
    """Block until the run reaches a terminal state; return ``'done'`` or ``'dead'``.

    Liveness is process existence, never log freshness: a long silent reduction is exactly what
    this question spends most of its time doing, and reading that as death would kill every trial.
    """
    from auto_research.graph.detached import run_status

    while True:
        status = run_status(run_directory)
        if status in ("done", "dead"):
            return status
        await asyncio.sleep(poll_seconds)


# ── What the ledger says happened ───────────────────────────────────────────


def summarise_ledger(path: Path) -> dict[str, Any]:
    """Derive a trial's counts from its own ledger.

    The ledger, not the harness, is the record: the gate, the review and the reviewer all emit
    structured verdicts, so these are read from what the production flow wrote rather than counted
    by wrapping its hooks.
    """
    if not path.exists():
        return {}
    events: Counter[str] = Counter()
    cost_by_role: Counter[str] = Counter()
    gate_pass = gate_fail = review_pass = review_fail = 0
    tools_committed: list[str] = []
    commit_shas: list[str] = []
    scan_findings: list[str] = []
    worker_statuses: list[str] = []
    reviewer_verdicts: list[bool] = []
    wanted_a_human = 0
    guard_rejections = 0
    schema_retries = 0

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a batch may read a ledger that is still being written
        event = row.get("event", "")
        data = row.get("data") or {}
        events[event] += 1

        if event == "gate.result":
            if data.get("passed"):
                gate_pass += 1
            else:
                gate_fail += 1
                scan_findings.extend(
                    check for check in (data.get("failing_checks") or [])
                    if "no_fabrication" in check
                )
        elif event == "review.result":
            review_pass += bool(data.get("approved"))
            review_fail += not data.get("approved")
        elif event == "commit":
            # The `commit` event carries the tool name and SHA at the top level; the
            # `analyst.tool.authored` event that follows nests everything under `result`.
            # Reading the latter's `data.tool` returns None, so a run that committed a tool
            # recorded ZERO committed tools -- caught here only because the branch was checked
            # against git by hand. A reader pointed at the wrong field does not look like a
            # defect; it looks like a boring result, which is the whole hazard.
            name = data.get("tool")
            if name:
                tools_committed.append(str(name))
            sha = data.get("commit_sha")
            if sha:
                commit_shas.append(str(sha)[:10])
        elif event in ("analyst.analysis.report", "analyst.worker.initial_report",
                       "analyst.worker.revision"):
            status = data.get("status")
            if status:
                worker_statuses.append(str(status))
        elif event == "reviewer.analysis_review.verdict":
            if "accepted" in data:
                reviewer_verdicts.append(bool(data["accepted"]))
        elif event == "cost.call":
            cost_by_role[data.get("role") or "?"] += float(data.get("total_cost_usd") or 0.0)
        # A tool call the filesystem guard killed. Counted as a first-class response because it is
        # a *tax on the loop budget*, not a fact about the science: the guard reads absolute-looking
        # fragments out of a shell command, so a python glob or a path relative to a `cwd` reads to
        # it as a path outside the allow-list. Observed on three of four actors in the first live
        # trial, one worker losing three consecutive iterations to it. It falls on every arm
        # equally, so it does not bias the comparison -- but it consumes `react_loop_cap`, and a
        # model that exhausts its budget fighting a path checker looks exactly like a model that
        # could not do the science. Counting it is what keeps those two apart.
        if data.get("is_error") and "path not permitted" in str(data.get("text") or ""):
            guard_rejections += 1
        if event.endswith(".retry"):
            schema_retries += 1
        # "How many times did this tree want a human it did not have." With the channel closed
        # this is the measurable form of a raise, and it is the number the write-up needs to bound
        # its unattended claim.
        if event.startswith("coder.ask_human") or event.startswith("hitl."):
            wanted_a_human += 1

    return {
        "gate_runs": gate_pass + gate_fail,
        "gate_passes": gate_pass,
        "gate_rejections": gate_fail,
        "review_runs": review_pass + review_fail,
        "review_approvals": review_pass,
        "review_rejections": review_fail,
        "tools_committed": tools_committed,
        "commit_shas": commit_shas,
        "tools_committed_n": len(tools_committed),
        "no_fabrication_hits": scan_findings,
        "coder_sessions": events.get("coder.session_start", 0),
        "coder_asks_lead": events.get("coder.ask_lead", 0),
        "debate_rounds": events.get("analyst.worker.revision", 0),
        "rework_iterations": events.get("reviewer.analysis_review.verdict", 0),
        "worker_statuses": worker_statuses,
        "reviewer_verdicts": reviewer_verdicts,
        "wanted_a_human": wanted_a_human,
        "guard_rejections": guard_rejections,
        "schema_retries": schema_retries,
        "events_total": sum(events.values()),
        "cost_by_role": {role: round(total, 4) for role, total in cost_by_role.most_common()},
    }


def read_result(run_directory: Path) -> dict[str, Any]:
    """The run's own terminal marker, or an empty dict if it never wrote one."""
    try:
        return json.loads((run_directory / "result.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def measured_outcome(result: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    """What the run concluded -- every value of which is a correct outcome.

    The pipeline always reaches a verdict; what this sweep measures is what the verdict *says*. A
    refusal, an honest failure and a run that declares its result unverifiable all belong here, not
    in a failure rate.
    """
    verdict = result.get("verdict") or {}
    return {
        "accepted": verdict.get("accepted"),
        "verdict_summary": verdict.get("summary") or verdict.get("rationale"),
        "report_jsonl": result.get("report_jsonl"),
        "report_latex": result.get("report_latex"),
        "report_pdf": result.get("report_pdf"),
        "worker_statuses": ledger.get("worker_statuses", []),
        "needs_human": "needs_human" in ledger.get("worker_statuses", []),
    }


# ── One trial ───────────────────────────────────────────────────────────────


async def run_trial(slot: RunSlot, cfg: TrialConfig) -> dict[str, Any]:
    """Run one whole research run for one slot and return its record. Never raises."""
    started = time.time()
    trial_dir = cfg.out_dir / f"slot-{slot.index:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "slot": slot.index,
        "block": slot.block,
        "replicate": slot.replicate,
        "run_id": slot.run_id,
        "verbatim_recording": slot.verbatim_recording,
        # The arm, spelled out. This file is the human's; no actor is ever handed a path to it.
        "lead": slot.arm.lead.model,
        "reviewer": slot.arm.reviewer.model,
        "workers": [member.model for member in slot.arm.workers.members],
        "worker_pair": slot.arm.workers.slug,
        "coder": slot.arm.coder.model,
        "colead": slot.arm.colead.model,
        "lead_correlated": slot.arm.lead_correlated,
        **cfg.stamps,
    }

    environment: env_mod.TrialEnvironment | None = None
    try:
        # Off the event loop. Building a world copies about 4 GB and clones a repository, which
        # takes a minute of pure blocking I/O -- and this coroutine shares its loop with every
        # other trial's poller, so doing it inline would serialise concurrent materialisation and
        # stall the watchers of runs already in flight.
        environment = await asyncio.to_thread(
            env_mod.materialize,
            slot.arm, slot.run_id, workspace_root=cfg.workspace_root, base_ref=cfg.base_ref,
        )
        record["trial_root"] = str(environment.root)
        record["run_dir"] = str(environment.run_dir)

        prompt_dump = trial_dir / "prompts.jsonl" if slot.verbatim_recording else None
        record["pid"] = launch(environment, chatter=cfg.chatter, prompt_dump=prompt_dump)
        if prompt_dump is not None:
            record["prompt_dump"] = str(prompt_dump)
        status = await wait_for(environment.run_dir, poll_seconds=cfg.poll_seconds)

        result = read_result(environment.run_dir)
        ledger = summarise_ledger(environment.run_dir / "monitor.jsonl")
        # `done` is the marker's presence, not its contents: the run writes one for a crash and for
        # a human stop too, and those say so in their own status.
        record["infrastructure"] = result.get("status", "died") if status == "done" else "died"
        if record["infrastructure"] == "failed":
            record["error"] = result.get("error")
        record["measured"] = measured_outcome(result, ledger)
        record.update(ledger)
    except Exception as exc:  # noqa: BLE001 — one broken trial must never end a batch
        record["infrastructure"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if environment is not None:
            try:
                # Also off the loop: removing the data copy is several gigabytes of unlink.
                await asyncio.to_thread(
                    env_mod.teardown, environment, keep_artifacts=cfg.keep_artifacts
                )
            except Exception as exc:  # noqa: BLE001 — housekeeping never fails a measured trial
                record["teardown_error"] = f"{type(exc).__name__}: {exc}"

    record["duration_s"] = round(time.time() - started, 2)
    record["cost_usd"] = round(sum(record.get("cost_by_role", {}).values()), 4)
    return record


def append_record(out_dir: Path, record: dict[str, Any]) -> Path:
    """Append one record the moment its trial ends, so a killed batch loses only what was in
    flight and everything already measured is on disk."""
    path = out_dir / "records.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")
    return path


def load_records(out_dir: Path) -> list[dict[str, Any]]:
    """Every record on disk, tolerant of a truncated final line so a live batch can be read."""
    path = Path(out_dir) / "records.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
