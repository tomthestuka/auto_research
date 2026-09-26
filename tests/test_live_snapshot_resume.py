"""Env-gated LIVE test of the submission-snapshot checkpoint (§9.9, built 2026-08-31).

Activate with:
    AUTO_RESEARCH_LIVE_SNAPSHOT_RESUME=1 pytest tests/test_live_snapshot_resume.py -q -s

The scenario the checkpoint exists for, end to end, through PRODUCTION functions only —
`build_authoring_session`, the session primitives the lead's authoring tools call
(`drive`/`run_gate_step`/`run_review_step`/`commit`), `heal_toolkit_branch_if_dirty` — with a
REAL coder (the configured `coder` member), the REAL deterministic gate, and the REAL
provider-decorrelated co-lead review:

  phase 1  a ToolSpec harvested from a real run (the Opus lead's own brief from run 46ed6671,
           frozen by `benchmarks/author_bench`) is authored on a throwaway clone at the same
           base the real authoring started from; the coder submits, the gate runs, and the
           process is "killed" in the middle of the LLM review — the in-flight review task is
           cancelled and NO cleanup runs (`aclose` is skipped), exactly a hard kill's shape:
           dirty tree, no reset, no verdict.

  phase 2  the production resume sequence: `heal_toolkit_branch_if_dirty` (resets the dirty
           branch to the recorded base ref), then `build_authoring_session` for the same tool —
           which restores the last submission onto the tree — and a fresh coder session that
           must (a) find the prior work already in the tree and continue on it, and (b) receive
           the previous session's memory (the restored-work seed section carrying the last
           gate/review state). The gate then re-runs on the restored tree and the co-lead
           review completes; a commit is recorded when earned.

Asserted mechanically (the probe-asserts-actor-capability rule — LLM behaviour is recorded for
hand-reading, never asserted): the snapshot exists with the gate outcome; the heal resets; the
restore puts the phase-1 files back BEFORE the new coder's first turn; the seed message carries
the restored-work section; the phase-2 submission's snapshot index advances (cumulative work,
not a wipe); the phase-2 gate delivers a verdict over that tree. The coder's own words and the
final outcome land in `logs/dev/live-snapshot-resume/`.

Costs one real authoring session (subscription) + one co-lead review (~$1-3) + the killed
review fragment. Requires the analyst env (the gate's Layer-B pytest), like other live tests.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from auto_research.graph.runtime import load_orchestration_config
from auto_research.sandbox.merge_flow import (
    _gate_feedback_message,
    _review_feedback_message,
    build_authoring_session,
    heal_toolkit_branch_if_dirty,
)

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_SNAPSHOT_RESUME") != "1",
    reason="set AUTO_RESEARCH_LIVE_SNAPSHOT_RESUME=1 to run the live snapshot-resume test",
)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "live-snapshot-resume"
GATE_REWORK_CAP = 3   # per phase, mirroring the production default
REVIEW_ROUNDS_CAP = 2  # phase 2 only; bounds spend
KILL_AFTER_S = 15      # the co-lead's fastest observed verdict is ~36 s — mid-flight


class _JsonlMonitor:
    """Production-shaped monitor: records every event to a JSONL, so the run is auditable."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    async def record(self, team, event, *, level, summary, actor=None, data=None):
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "t": time.time(), "team": team, "event": event, "level": level,
                "summary": summary, "actor": actor, "data": data,
            }, default=str) + "\n")


class _ScriptedLead:
    """ask_lead answers with the standing instruction; every escalation is logged."""

    def __init__(self, log):
        self._log = log
        self.agent_key = "analyst_lead"
        self.channel = None
        from auto_research.run_control import RunControl

        self._control = RunControl()

    def ensure_run_control(self):
        """The coder's `message(to="lead")` posts to this inbox (E12); nothing drains it here."""
        return self._control

    async def clarify_for_author(self, message, *, kind="clarify_code"):
        self._log("ask_lead", message)
        return (
            "Proceed exactly as the ToolSpec specifies. Resolve the synthetic fixture ObsIDs "
            "by writing controlled CSVs into the test's own tmp_path, as the committed tests "
            "of neighbouring tools do."
        )


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


def _clone_at_brief_base() -> Path:
    """A throwaway full clone at the frozen brief's base SHA, on `ai` — the same starting
    state the real run 46ed6671 authored from (tool absent, everything it builds on present).

    NEVER under pytest's basetemp: the first live run nested the clone in this test's own
    ``tmp_path`` (`/tmp/pytest-of-<user>/pytest-NNN/...`), and the coder's legitimate pytest
    runs inside the clone rotated that basetemp — pytest keeps the 3 newest ``pytest-NNN``
    session dirs and deleted the outer session, clone included, 18 minutes into authoring
    (the [F7] /tmp hazard family, new instance). A private mkdtemp prefix is outside every
    rotation scheme; the test removes it on success and leaves it for forensics on failure.
    """
    import tempfile

    from benchmarks.author_bench.brief import BASE_SHA

    root = Path(load_orchestration_config().get("toolkit_root", "")).expanduser()
    if not (root / ".git").exists():
        pytest.skip(f"toolkit_root {root} is not a git checkout")
    clone = Path(tempfile.mkdtemp(prefix="ar_livesnap_")) / "toolkit_clone"
    subprocess.run(
        ["git", "clone", "--quiet", str(root), str(clone)],
        check=True, capture_output=True, text=True,
    )
    _git(clone, "config", "user.email", "live@test")
    _git(clone, "config", "user.name", "live")
    try:
        _git(clone, "checkout", "-q", "-B", "ai", BASE_SHA)
    except subprocess.CalledProcessError:
        pytest.skip(f"brief base {BASE_SHA} is not reachable in this toolkit checkout")
    return clone


async def _drive_to_gate_pass(session, first_message, log) -> "object":
    """The mechanical half of the lead's loop: drive, submit, feed gate rejections back."""
    reply = await session.drive(first_message)
    log("coder_reply", reply)
    for _ in range(GATE_REWORK_CAP + 1):
        gate = await session.run_gate_step()
        if gate.passed:
            return gate
        reply = await session.drive(_gate_feedback_message(gate))
        log("coder_reply", reply)
    pytest.fail(f"gate never passed within {GATE_REWORK_CAP} reworks — see the run log")


def _scripted_human(log):
    """A handler so `ask_human` answers instead of raising headless — its reply is the same
    standing instruction the scripted lead gives, and every query is logged."""
    from auto_research.schemas import HumanResponse

    async def handler(query):
        log("ask_human", query.message)
        return HumanResponse(
            query_id=query.id,
            answer=(
                "Proceed exactly as the ToolSpec specifies; resolve synthetic fixtures by "
                "writing controlled CSVs into the test's own tmp_path."
            ),
        )

    return handler


def test_live_kill_mid_review_then_resume_restores_and_continues():
    from dotenv import load_dotenv

    load_dotenv()  # the co-lead review needs the API keys (live-test convention)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = LOG_DIR / stamp
    monitor = _JsonlMonitor(run_dir / "events.jsonl")
    transcript = run_dir / "transcript.md"
    transcript.parent.mkdir(parents=True, exist_ok=True)

    def log(kind, text):
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {kind} @ {time.strftime('%H:%M:%S')}\n\n{text}\n")

    from benchmarks.author_bench.brief import load_brief

    spec = load_brief()  # the Opus lead's real ToolSpec, verbatim from run 46ed6671
    clone = _clone_at_brief_base()
    worktree_base = run_dir / "authoring"
    tool = spec.proposed_tool_name
    snap_dir = worktree_base / "submissions" / tool
    human_handler = _scripted_human(log)

    async def phase1() -> str:
        session = build_authoring_session(
            spec, lead=_ScriptedLead(log), toolkit_root=clone, human_handler=human_handler,
            worktree_base=worktree_base, direct_branch="ai", monitor=monitor,
        )
        assert session.restored_submission is None  # nothing to restore yet
        await session.connect()
        try:
            gate = await _drive_to_gate_pass(session, session.initial_message, log)
            # ── the kill: mid-LLM-review, no cleanup — a hard kill's exact shape ──
            review_task = asyncio.ensure_future(session.run_review_step(gate))
            await asyncio.sleep(KILL_AFTER_S)
            assert not review_task.done(), (
                "the review finished before the kill fired — lower KILL_AFTER_S"
            )
            review_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await review_task
            log("kill", f"review cancelled after {KILL_AFTER_S}s; aclose deliberately skipped")
        finally:
            # A dead process takes its coder child with it; nothing else may run —
            # in particular NOT aclose(), whose tree reset a hard kill never reaches.
            await session._backend.disconnect()
        return session._base_ref

    base_ref = asyncio.run(phase1())

    # ── the killed attempt's evidence ──
    assert _git(clone, "status", "--porcelain").strip(), "tree should be dirty after the kill"
    state = json.loads((snap_dir / "state.json").read_text(encoding="utf-8"))
    phase1_submits = len(state["submits"])
    assert state["submits"][-1]["gate_passed"] is True
    assert state["submits"][-1]["review_approved"] is None  # killed mid-review
    lib_files = [p for p in (clone / "xspec_utils" / "lib").glob("*.py")
                 if tool.replace("ixpe_", "") in p.name or tool in p.name]
    log("phase1", f"submits={phase1_submits} base_ref={base_ref} lib_files={lib_files}")

    # ── the production resume sequence ──
    heal = heal_toolkit_branch_if_dirty(run_dir, root=clone, branch="ai")
    assert heal.healed and heal.base_ref == base_ref, heal
    assert not _git(clone, "status", "--porcelain").strip()  # branch healed to base

    async def phase2() -> dict:
        session = build_authoring_session(
            spec, lead=_ScriptedLead(log), toolkit_root=clone, human_handler=human_handler,
            worktree_base=worktree_base, direct_branch="ai", monitor=monitor,
        )
        # (a) the dirty working tree is picked up: the restore put the work back BEFORE
        #     the new coder's first turn
        assert session.restored_submission is not None
        assert session.restored_submission["index"] == phase1_submits
        assert _git(clone, "status", "--porcelain").strip(), "restore must dirty the tree"
        # (b) previous memory from the same session rides the seed
        assert "Restored prior work" in session.initial_message
        assert "passed the deterministic gate" in session.initial_message
        await session.connect()
        try:
            gate = await _drive_to_gate_pass(session, session.initial_message, log)
            review = await session.run_review_step(gate)
            log("review", json.dumps(review.model_dump(), indent=1, default=str))
            for _ in range(REVIEW_ROUNDS_CAP):
                if review.approved:
                    break
                reply = await session.drive(_review_feedback_message(review))
                log("coder_reply", reply)
                gate = await session.run_gate_step()
                if not gate.passed:
                    break
                review = await session.run_review_step(gate)
                log("review", json.dumps(review.model_dump(), indent=1, default=str))
            sha = None
            if review.approved and gate.passed:
                sha = await session.commit(review.commit_summary or f"Add {tool}")
            return {"approved": review.approved, "commit_sha": sha,
                    "restored_index": session.restored_submission["index"]}
        finally:
            await session.aclose()  # normal end-of-attempt path this time

    outcome = asyncio.run(phase2())

    # phase 2 continued the phase-1 work: its snapshots extend the same record
    state2 = json.loads((snap_dir / "state.json").read_text(encoding="utf-8")) \
        if (snap_dir / "state.json").exists() else None
    if state2 is not None:  # commit discards the record; absence + a sha is the success shape
        assert len(state2["submits"]) > phase1_submits
    else:
        assert outcome["commit_sha"], "snapshots gone without a commit"
    log("outcome", json.dumps(outcome, indent=1))
    print(f"\nlive snapshot-resume outcome: {outcome}  (log: {run_dir})")
    import shutil

    shutil.rmtree(clone.parent, ignore_errors=True)  # success: the throwaway clone is done
