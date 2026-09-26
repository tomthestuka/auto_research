"""Opt-in LIVE tests of the lead<->coder channel: the blocking `wait_coder` and queued `drive_coder`.

    AUTO_RESEARCH_LIVE_CODER_WAIT=1 pytest tests/test_live_coder_wait.py -s
    AUTO_RESEARCH_LIVE_CODER_WAIT=1 pytest tests/test_live_coder_wait.py -s -k mechanism   # cheap
    # the lead cells, once per seat (one draw each):
    AUTO_RESEARCH_LIVE_CODER_WAIT=1 pytest tests/test_live_coder_wait.py -s -k lead
    AUTO_RESEARCH_LIVE_CODER_WAIT=1 AUTO_RESEARCH_SEATS='analyst_lead=openai:gpt-5.6-terra' \\
        pytest tests/test_live_coder_wait.py -s -k lead

**Why.** Slot 6 (2026-09-15, `gpt-5.6-terra` lead, $20.43 — the run's most expensive role) made
134 `check_coder` calls (114 refused by a 900 s rate limit), 47 mid-turn steers, 31 refused
submits and 9 refused `turn_end` calls at a 3-10 s cadence: a loop with no way to let time pass
emits a tool call to do it, and a refusal is a paid turn like any other. The replacement is a
blocking wait with early wake (Codex `wait_agent`'s shape). The deterministic suite pins it over a
stub backend; what it cannot reach is a real coder *process* whose turns, questions and tool calls
arrive on their own clock, and a real lead choosing what to do with the time.

**Cells.** Every behaviour in the design, each asserted on our side of the boundary:

* mechanism cells — a REAL `AuthoringSession` (configured coder member, real harness process,
  throwaway toolkit clone) driven through the PRODUCTION executors (`execute_drive_coder`,
  `execute_wait_coder`, `execute_submit`) with the test standing in for the lead:
  turn end wakes the wait · a coder question wakes it and the answer goes mid-turn · a short
  timeout is raised to the floor and times out while the coder works · a plain message is queued
  and becomes the next turn while `mid_turn` steers the running one · a submit waits for the coder
  to stop before the gate runs;
* lead cells — a REAL analyst lead (`attach_lead_runtime` + `run_cycle`, model from the config
  pointer) authoring a real link: it waits instead of polling, and a human `/steer` wakes its wait;
  and a `/stop` landing while the lead is parked in the wait cancels it and leaves a clean tree.

**Discipline** (the live-test rules): production seams only, models from config (`--seats` /
`AUTO_RESEARCH_SEATS`), the cost meter installed inside the task, a ledger and a git bundle of the
clone under `logs/dev/coder-wait/<stamp>/` written BEFORE any assertion. What the coder and the
lead *say* is recorded beside the result and never asserted; what the harness *does* is.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_CODER_WAIT") != "1",
    reason="set AUTO_RESEARCH_LIVE_CODER_WAIT=1 to spend provider tokens on the coder-wait cells",
)

load_dotenv()

STAMP = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "coder-wait" / STAMP
CYCLE_TIMEOUT_S = float(os.getenv("AUTO_RESEARCH_CYCLE_TIMEOUT_S", "5400"))
# A wake is "prompt" when the wait returns within this many seconds of the event that caused it.
WAKE_SLACK_S = float(os.getenv("AUTO_RESEARCH_WAKE_SLACK_S", "5"))
# How long the lead must have been parked in wait_coder before the steer / stop is sent.
PARKED_FOR_S = float(os.getenv("AUTO_RESEARCH_PARKED_FOR_S", "10"))


# ─────────────────────────────────────────────────────────────── shared plumbing


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _clone() -> tuple[Path, str]:
    """A throwaway clone at science_bench's pinned base, outside pytest's rotating basetemp.

    The base is the commit before that question's tool chain existed, so a lead briefed with the
    request below genuinely has something to author (the stop drill's measured lesson).
    """
    from auto_research.env import toolkit_root as configured_toolkit_root
    from benchmarks.science_bench.environment import clone_toolkit
    from benchmarks.science_bench.trial import BASE_REF

    clone = Path(tempfile.mkdtemp(prefix="ar_livewait_")) / "toolkit"
    base = clone_toolkit(clone, configured_toolkit_root(), BASE_REF, "ai")
    return clone, base


def _monitor(cell: str):
    from auto_research.monitor import RunMonitor

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return RunMonitor(chatter=10, jsonl_path=LOG_DIR / f"{cell}_ledger.jsonl")


def _events(monitor, name: str | None = None, prefix: str | None = None) -> list:
    out = []
    for event in monitor.events:
        ev = getattr(event, "event", "")
        if (name is None or ev == name) and (prefix is None or ev.startswith(prefix)):
            out.append(event)
    return out


def _now() -> datetime:
    return datetime.now(UTC)


_CODER_MARKERS = ("codex", "claude-code", "claude_sdk", "bwrap")


def _own_coder_pids() -> set[int]:
    """Author-harness processes descended from THIS test process — never a peer session's.

    Matching by name alone (the stop drill's `_coder_pids`) sees every Claude Code or Codex
    process on the host, including other sessions' coders, which a kill must never touch.
    """
    found: set[int] = set()
    frontier = [os.getpid()]
    while frontier:
        pid = frontier.pop()
        for children in Path(f"/proc/{pid}/task").glob("*/children"):
            try:
                kids = [int(k) for k in children.read_text().split()]
            except (OSError, ValueError):
                continue
            frontier.extend(kids)
            for kid in kids:
                try:
                    cmd = Path(f"/proc/{kid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
                except OSError:
                    continue
                if any(marker in cmd for marker in _CODER_MARKERS):
                    found.add(kid)
    return found


def _preserve(cell: str, clone: Path, observed: dict, monitor) -> Path:
    """Bundle the clone and write the record — BEFORE any assertion runs."""
    bundle = LOG_DIR / f"{cell}_toolkit.bundle"
    try:
        _git(clone, "bundle", "create", str(bundle), "--all")
    except subprocess.CalledProcessError as exc:  # recorded, never fatal to the record
        observed["bundle_error"] = exc.stderr
    from tests.test_live_stop_mid_drive import _coder_digest

    record = LOG_DIR / f"{cell}.json"
    record.write_text(
        json.dumps({**observed, "coder": _coder_digest(monitor)}, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"\n[{cell}] record → {record}")
    return record


class _LeadInbox:
    """The lead's side of the channel, and nothing else: the coder's ``message(to="lead")`` posts
    here (E12) and wakes a pending wait. The cells read the question back from this inbox."""

    def __init__(self) -> None:
        from auto_research.run_control import RunControl

        self.agent_key = "analyst_lead"
        self.channel = None
        self.control = RunControl()

    def ensure_run_control(self):
        return self.control

    def questions(self) -> list[str]:
        return [e.text for e in self.control.drain_inbox(self.agent_key) if e.sender == "coder"]


async def execute_drive_coder(session, arguments):
    """The lead's ``message(to="coder")`` as these cells drive it: the production executor."""
    from auto_research.agents.react import execute_coder_message

    return await execute_coder_message(
        session, str(arguments.get("instructions") or ""), mid_turn=bool(arguments.get("mid_turn"))
    )


def _build_session(clone: Path, monitor):
    """The production session builder, configured coder member, the frozen real brief."""
    from auto_research.sandbox.merge_flow import build_authoring_session
    from benchmarks.author_bench.brief import load_brief

    lead = _LeadInbox()
    session = build_authoring_session(
        load_brief(),
        lead=lead,
        toolkit_root=clone,
        worktree_base=clone.parent / "authoring",
        direct_branch="ai",
        monitor=monitor,
    )
    session.watch_steers(lead.control.steer_event(lead.agent_key))
    session.test_lead = lead  # the cells read the coder's questions from its inbox
    return session


def _mechanism(cell: str, scenario) -> dict:
    """Run one mechanism cell: real session, cost meter inside the task, record then return."""
    from auto_research.observability.costs import install_cost_meter

    clone, base = _clone()
    monitor = _monitor(cell)
    observed: dict = {"cell": cell, "base_ref": base}

    async def run() -> None:
        # Attached to the monitor as `run_research` does (runtime.py), so every ledger line
        # carries the accumulated spend.
        monitor.cost_meter = install_cost_meter(run_id=monitor.run_id, monitor=monitor)
        session = _build_session(clone, monitor)
        await session.connect()
        try:
            await scenario(session, monitor, observed)
        finally:
            # Bounded: a cell that kills the coder must still get its tree reset, and a hang
            # here is itself a finding, recorded rather than wedging the suite.
            try:
                await asyncio.wait_for(session.aclose(), timeout=120)
            except TimeoutError:
                observed["aclose_timed_out"] = True

    try:
        asyncio.run(run())
    finally:
        observed["status_after"] = _git(clone, "status", "--porcelain")
        observed["leaked_coder_pids"] = sorted(_own_coder_pids())
        observed["cost_usd_total"] = next(
            (e.cost_usd_total for e in reversed(monitor.events) if e.cost_usd_total is not None),
            None,
        )
        _preserve(cell, clone, observed, monitor)
    shutil.rmtree(clone.parent, ignore_errors=True)
    return observed


def _seconds_after(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


# ─────────────────────────────────────────────────────────────── mechanism cells


def test_mechanism_turn_end_wakes_the_wait():
    """The coder's turn ends → the wait returns at once with that turn's narration."""
    from auto_research.agents.react import execute_wait_coder

    async def scenario(session, monitor, observed):
        sent = await execute_drive_coder(
            session, {"instructions": "Reply with the single word READY and end your turn."}
        )
        observed["drive_reply"] = sent.text
        out = await execute_wait_coder(session, {"timeout_s": 600})
        observed["wait_returned_at"] = _now()
        observed["wait_text"] = out.text

    obs = _mechanism("turn_end", scenario)
    ends = [e for e in _read_ledger("turn_end") if e["event"] == "coder.turn_end"]
    assert ends, "the coder never finished a turn, so the wake was not exercised"
    latency = _seconds_after(_parse(obs["wait_returned_at"]), _parse(ends[0]["created_at"]))
    assert "The coder's turn ended" in obs["wait_text"], obs["wait_text"]
    assert latency < WAKE_SLACK_S, f"the wait returned {latency:.1f}s after the turn ended"


def test_mechanism_question_wakes_the_wait_and_the_answer_goes_mid_turn():
    """`message(to="lead")` → the wait returns on the question; a plain reply is delivered mid-turn."""
    from auto_research.agents.react import execute_wait_coder

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Call your message tool with to='lead' and exactly this text: 'WAIT-PROBE: should the "
            "band be PI or ENERGY?'. After it returns, run the shell command `sleep 45`, then end "
            "your turn by writing ANSWER=<the lead's answer>, or ANSWER=NONE if none reached you."
        )})
        first = await execute_wait_coder(session, {"timeout_s": 600})
        observed["first_wait_returned_at"] = _now()
        observed["first_wait_text"] = first.text
        observed["inbox_questions"] = session.test_lead.questions()
        answer = await execute_drive_coder(session, {"instructions": "PI."})
        observed["answer_reply"] = answer.text
        final = await execute_wait_coder(session, {"timeout_s": 600})
        observed["final_wait_text"] = final.text

    obs = _mechanism("question", scenario)
    ledger = _read_ledger("question")
    asks = [e for e in ledger if e["event"] == "coder.ask_lead"]
    assert asks, "the coder never messaged the lead, so the question wake was not exercised"
    assert "A message for you has arrived" in obs["first_wait_text"], obs["first_wait_text"]
    assert any("WAIT-PROBE" in q for q in obs["inbox_questions"]), obs["inbox_questions"]
    latency = _seconds_after(_parse(obs["first_wait_returned_at"]), _parse(asks[0]["created_at"]))
    assert latency < WAKE_SLACK_S, f"the wait returned {latency:.1f}s after the question"
    assert "as the answer to its question" in obs["answer_reply"], obs["answer_reply"]
    assert any(e["event"] == "coder.steer" for e in ledger), "the answer never reached the turn"
    # The coder's own words: the answer must have reached its model, not only its harness.
    assert "ANSWER=PI" in obs["final_wait_text"].replace("*", "").replace(" ", ""), obs["final_wait_text"]


def test_mechanism_short_timeout_is_raised_to_the_floor_and_times_out():
    """`wait_coder(5)` on a busy coder holds for the floor and returns 'still working'."""
    from auto_research.agents.react import execute_wait_coder
    from auto_research.env import coder_wait_limits_s

    floor, _, _ = coder_wait_limits_s()

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            f"Run the shell command `sleep {floor + 40}`, then run it once more, then reply DONE."
        )})
        started = time.monotonic()
        out = await execute_wait_coder(session, {"timeout_s": 5})
        observed["waited_s"] = time.monotonic() - started
        observed["wait_text"] = out.text
        observed["status_after"] = session.drive_status

    obs = _mechanism("floor", scenario)
    if "The coder's turn ended" in obs["wait_text"]:
        pytest.skip(f"the coder finished before the {floor}s floor, so no timeout could occur")
    assert f"raised to the {floor}s minimum" in obs["wait_text"], obs["wait_text"]
    assert obs["waited_s"] >= floor - 1, f"returned after {obs['waited_s']:.1f}s, under the floor"
    assert "still working" in obs["wait_text"] and obs["status_after"] == "working"


def test_mechanism_plain_message_is_queued_and_mid_turn_steers():
    """A plain `drive_coder` becomes the next turn; `mid_turn=true` reaches the running one."""
    from auto_research.agents.react import execute_wait_coder

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Run the shell command `sleep 40`, then run `sleep 40` again, then reply FIRST-DONE."
        )})
        await asyncio.sleep(10)                    # the turn is genuinely running
        queued = await execute_drive_coder(
            session, {"instructions": "Reply with the single word QUEUED-OK."}
        )
        observed["queued_reply"] = queued.text
        observed["steers_after_queue"] = len(_events(monitor, "coder.steer"))
        steered = await execute_drive_coder(session, {
            "instructions": "WAIT-PROBE mid-turn: skip any remaining sleep and reply STEERED-OK.",
            "mid_turn": True,
        })
        observed["steered_reply"] = steered.text
        waits = []
        for _ in range(3):                           # first turn, then the queued turn
            out = await execute_wait_coder(session, {"timeout_s": 600})
            waits.append(out.text)
            if session.drive_status != "working":
                break
        observed["wait_texts"] = waits

    obs = _mechanism("queue", scenario)
    ledger = _read_ledger("queue")
    assert obs["queued_reply"].startswith("Queued"), obs["queued_reply"]
    assert obs["steers_after_queue"] == 0, "a plain drive_coder steered the running turn"
    assert "inside the turn the coder is running" in obs["steered_reply"], obs["steered_reply"]
    assert any(e["event"] == "coder.steer" for e in ledger), "mid_turn delivered nothing"
    assert any(e["event"] == "coder.queued.delivered" for e in ledger), (
        "the queued message never became the coder's next turn"
    )
    assert sum(e["event"] == "coder.turn_end" for e in ledger) >= 2


def test_mechanism_submit_waits_for_the_coder_before_the_gate():
    """`submit_for_review` on a working coder: the gate starts only after the turn ends."""
    from auto_research.agents.react import execute_submit

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Create the file xspec_utils/lib/wait_probe_note.py containing the single line "
            "`NOTE = 1`, then run the shell command `sleep 40`, then reply DONE."
        )})
        await asyncio.sleep(5)
        observed["status_at_submit"] = session.drive_status
        out = await execute_submit(session, None)
        observed["submit_text"] = out.text

    obs = _mechanism("submit", scenario)
    ledger = _read_ledger("submit")
    if obs["status_at_submit"] != "working":
        pytest.skip("the coder had already stopped when the submit was sent")
    ends = [e for e in ledger if e["event"] == "coder.turn_end"]
    gates = [e for e in ledger if e["event"] == "gate.start"]
    assert ends and gates, "no turn end or no gate run was recorded"
    assert _parse(gates[0]["created_at"]) >= _parse(ends[0]["created_at"]), (
        "the gate started before the coder's turn ended — it judged a half-written tree"
    )
    assert obs["submit_text"].startswith("[waited "), obs["submit_text"][:200]


def _kill_new_coder_processes(observed: dict) -> list[int]:
    """SIGKILL the author processes this cell's session started — a real dead coder."""
    import signal

    victims = sorted(_own_coder_pids())
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    observed["killed_pids"] = victims
    return victims


def _assert_cell_left_nothing_behind(obs: dict) -> None:
    assert not obs.get("aclose_timed_out"), "closing the session hung"
    assert obs["status_after"] == "", f"the worktree is dirty afterwards:\n{obs['status_after']}"
    assert not obs["leaked_coder_pids"], f"coder process(es) survived: {obs['leaked_coder_pids']}"


def test_mechanism_waiting_on_an_idle_coder_returns_at_once():
    """Nothing was sent, so there is nothing to wait for — the answer is immediate, not a hold."""
    from auto_research.agents.react import execute_wait_coder

    async def scenario(session, monitor, observed):
        started = time.monotonic()
        out = await execute_wait_coder(session, {"timeout_s": 600})
        observed["waited_s"] = time.monotonic() - started
        observed["wait_text"] = out.text

    obs = _mechanism("idle", scenario)
    assert "nothing to wait for" in obs["wait_text"], obs["wait_text"]
    assert obs["waited_s"] < 2, f"an idle wait held for {obs['waited_s']:.1f}s"
    _assert_cell_left_nothing_behind(obs)


def test_mechanism_a_dead_coder_wakes_the_wait_as_a_failure():
    """The author process is SIGKILLed mid-turn: the wait must return FAILED, not hang or read
    as narration (the 2026-09-09 corpse-steering defect, now on the waiting path)."""
    from auto_research.agents.react import execute_wait_coder

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Run the shell command `sleep 120`, then reply DONE."
        )})
        await asyncio.sleep(15)
        _kill_new_coder_processes(observed)
        started = time.monotonic()
        out = await execute_wait_coder(session, {"timeout_s": 300})
        observed["waited_s"] = time.monotonic() - started
        observed["wait_text"] = out.text
        observed["wait_is_error"] = out.is_error

    obs = _mechanism("dead_coder", scenario)
    assert obs["killed_pids"], "no coder process was found to kill; the cell measured nothing"
    assert obs["wait_is_error"] and "FAILED" in obs["wait_text"], obs["wait_text"][:400]
    assert obs["waited_s"] < 60, f"the wait took {obs['waited_s']:.1f}s to notice a dead coder"
    _assert_cell_left_nothing_behind(obs)


def test_mechanism_submit_hands_back_a_question_instead_of_gating():
    """A coder that asks while a submit waits would never stop; the submit returns the question
    and the gate never runs."""
    from auto_research.agents.react import execute_submit

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Call your message tool with to='lead' and exactly this text: 'SUBMIT-PROBE: which "
            "band?'. After it returns, run the shell command `sleep 60`, then reply DONE."
        )})
        out = await execute_submit(session, None)
        observed["submit_text"] = out.text
        observed["inbox_questions"] = session.test_lead.questions()
        observed["submits_used"] = session.submits_used

    obs = _mechanism("submit_question", scenario)
    ledger = _read_ledger("submit_question")
    if not any(e["event"] == "coder.ask_lead" for e in ledger):
        pytest.skip("the coder never messaged the lead (it reported the tool missing — see the "
                    "record), so the hand-back path was not exercised")
    assert "Not submitted" in obs["submit_text"] and "message" in obs["submit_text"]
    assert any("SUBMIT-PROBE" in q for q in obs["inbox_questions"]), obs["inbox_questions"]
    assert not any(e["event"] == "gate.start" for e in ledger), "the gate ran anyway"
    assert obs["submits_used"] == 0, "a handed-back submit spent a submission"
    _assert_cell_left_nothing_behind(obs)


def test_mechanism_submit_with_a_coder_that_dies_while_it_waits():
    """The coder is killed while a submit is waiting on it: the submit reports the failure and
    never gates a tree nobody finished."""
    from auto_research.agents.react import execute_submit

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Run the shell command `sleep 120`, then reply DONE."
        )})
        await asyncio.sleep(10)
        submit = asyncio.create_task(execute_submit(session, None))
        await asyncio.sleep(5)
        _kill_new_coder_processes(observed)
        started = time.monotonic()
        out = await asyncio.wait_for(submit, timeout=300)
        observed["returned_after_kill_s"] = time.monotonic() - started
        observed["submit_text"] = out.text
        observed["submit_is_error"] = out.is_error

    obs = _mechanism("submit_dead_coder", scenario)
    ledger = _read_ledger("submit_dead_coder")
    assert obs["killed_pids"], "no coder process was found to kill; the cell measured nothing"
    assert obs["submit_is_error"] and "FAILED" in obs["submit_text"], obs["submit_text"][:400]
    assert not any(e["event"] == "gate.start" for e in ledger), "the gate ran on a dead coder"
    assert obs["returned_after_kill_s"] < 60
    _assert_cell_left_nothing_behind(obs)


def test_mechanism_a_stop_during_a_submit_wait_cancels_it():
    """`/stop` (RunControl.interrupt on the run task) while a submit is waiting on the coder:
    the task cancels promptly, the gate never runs, and closing leaves a clean tree."""
    from auto_research.agents.react import execute_submit
    from auto_research.run_control import RunControl

    async def scenario(session, monitor, observed):
        await execute_drive_coder(session, {"instructions": (
            "Run the shell command `sleep 120`, then reply DONE."
        )})
        await asyncio.sleep(15)
        observed["status_at_submit"] = session.drive_status
        control = RunControl()
        submit = asyncio.create_task(execute_submit(session, None))
        control.register_run_task(submit)
        await asyncio.sleep(3)
        observed["interrupted"] = control.interrupt("live stop-during-submit cell", by="test")
        started = time.monotonic()
        try:
            await submit
            observed["outcome"] = "completed"
        except asyncio.CancelledError:
            observed["outcome"] = "cancelled"
        observed["cancel_latency_s"] = time.monotonic() - started

    obs = _mechanism("stop_submit", scenario)
    ledger = _read_ledger("stop_submit")
    if obs["status_at_submit"] != "working":
        pytest.skip("the coder had already stopped, so the submit had nothing to wait on")
    assert obs["interrupted"] and obs["outcome"] == "cancelled", obs.get("outcome")
    assert obs["cancel_latency_s"] < 5, f"the cancel took {obs['cancel_latency_s']:.1f}s"
    assert not any(e["event"] == "gate.start" for e in ledger), "the gate ran after the stop"
    _assert_cell_left_nothing_behind(obs)


# ─────────────────────────────────────────────────────────────── lead cells


def _lead_request_reports():
    """Two worker reports, one carrying a real `tool_request` — what opens the authoring branch."""
    from auto_research.schemas import AnalystReport, AnalystToolRequest

    request = AnalystToolRequest(
        science_context=(
            "We need per-DU event counts for an IXPE observation to sanity-check exposure before "
            "any polarization fit; the toolkit exposes no such summary."
        ),
        expected_behavior=(
            "ixpe_du_event_counts(ixpe_path, obsid) -> {'du1': int, 'du2': int, 'du3': int} read "
            "from the Level-2 event files, with the per-DU live time alongside."
        ),
        proposed_tool_name="ixpe_du_event_counts",
        validation_data=[{"mission": "IXPE", "obsid": "01002901"}],
    )
    return [
        AnalystReport(
            summary=(
                "I could not verify per-DU exposure: there is no toolkit call that returns event "
                "counts per detector unit, so the cross-check is missing."
            ),
            findings=["exposure cross-check unavailable"],
            status="failed",
            tool_request=request,
        ),
        AnalystReport(
            summary="Same gap here — I worked around it by hand, which is not reproducible.",
            findings=["hand-computed counts, not reproducible"],
            status="failed",
        ),
    ]


def _pending_wait(monitor) -> tuple[str, datetime] | None:
    """The lead's `wait_coder` call that has no result yet, and when it was made."""
    calls = {}
    for event in _events(monitor, prefix="analyst_lead.run_cycle.tool_"):
        data = getattr(event, "data", {}) or {}
        if data.get("tool") != "wait_for_reply":
            continue
        if event.event.endswith("tool_call"):
            calls[data.get("tool_call_id")] = event.created_at
        elif event.event.endswith("tool_result"):
            calls.pop(data.get("tool_call_id"), None)
    if not calls:
        return None
    call_id, at = max(calls.items(), key=lambda kv: kv[1])
    return call_id, at


def _lead_run(cell: str, on_parked):
    """A real lead cycle; ``on_parked(control, lead, observed)`` fires once the lead has been
    parked in wait_coder for PARKED_FOR_S with its coder working. Returns (observed, clone, base)."""
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.graph.runtime import _interrupt_cleanup, attach_lead_runtime
    from auto_research.observability.costs import install_cost_meter
    from auto_research.run_control import RunControl
    from auto_research.schemas import AnalysisTask
    clone, base = _clone()
    monitor = _monitor(cell)
    control = RunControl()
    lead = AnalystLeadAgent(monitor=monitor)
    attach_lead_runtime(
        lead, run_id=monitor.run_id, toolkit_root=clone, run_monitor=monitor, run_control=control,
    )
    assert lead.tools is not None, "the seam gave the lead no tools; it would run blind"
    observed: dict = {"cell": cell, "base_ref": base, "lead_model": getattr(lead, "model", None)}

    class _NoMCP:
        async def shutdown(self):
            return None

    async def scenario():
        # Attached to the monitor as `run_research` does (runtime.py), so every ledger line
        # carries the accumulated spend.
        monitor.cost_meter = install_cost_meter(run_id=monitor.run_id, monitor=monitor)
        task = asyncio.create_task(lead.run_cycle(
            AnalysisTask(
                question="Check the per-DU exposure for the IXPE observation of Cyg X-1.",
                instructions="A worker reports a missing capability. Settle it.",
            ),
            _lead_request_reports(),
            sources=["analyst_worker_0", "analyst_worker_1"],
            rounds_left=1,
            author_budget=1,
            toolkit_root=clone,
            worktree_base=clone.parent / "authoring",
            direct_branch="ai",
        ))
        control.register_run_task(task)
        fired = False
        deadline = time.monotonic() + CYCLE_TIMEOUT_S
        while not task.done() and time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            pending = _pending_wait(monitor)
            session = control.authoring_session
            if fired or pending is None or getattr(session, "drive_status", None) != "working":
                continue
            if _seconds_after(_now(), pending[1]) >= PARKED_FOR_S:
                fired = True
                observed["parked_call_id"] = pending[0]
                observed["fired_at"] = _now()
                await on_parked(control, lead, observed)
        observed["fired"] = fired
        if not task.done():
            observed["timed_out"] = not observed.get("interrupted")
            if not observed.get("interrupted"):
                control.interrupt("live coder-wait cell timed out", by="test")
        try:
            await task
            observed["outcome"] = "cycle_completed"
        except asyncio.CancelledError:
            observed["outcome"] = "cancelled"
            observed["cancelled_at"] = _now()
        except Exception as exc:  # noqa: BLE001 — recorded; the tree and ledger are asserted
            observed["outcome"] = f"error: {exc!r}"
        if observed["outcome"] != "cycle_completed":
            await _interrupt_cleanup(
                control, sandboxes=[], mcp_client=_NoMCP(), human_handler=None, monitor=monitor,
            )

    try:
        asyncio.run(scenario())
    finally:
        observed["cost_usd_total"] = next(
            (e.cost_usd_total for e in reversed(monitor.events) if e.cost_usd_total is not None),
            None,
        )
        observed["head_after"] = _git(clone, "rev-parse", "HEAD")
        observed["status_after"] = _git(clone, "status", "--porcelain")
        observed["leaked_coder_pids"] = sorted(_own_coder_pids())
        observed["pacing"] = _lead_pacing(_read_ledger(cell))
        _preserve(cell, clone, observed, monitor)
    return observed, clone, base


def _lead_pacing(ledger: list[dict]) -> dict:
    """What the lead did while its coder worked, read off the ledger (the headline metric)."""
    from auto_research.env import coder_wait_limits_s

    floor, _, _ = coder_wait_limits_s()
    starts = [e for e in ledger if e["event"] == "coder.session_start"]
    ends = [e for e in ledger if e["event"] == "coder.turn_end"]
    if not starts or not ends:
        return {"authoring_window_s": 0}
    t0, t1 = _parse(starts[0]["created_at"]), _parse(ends[-1]["created_at"])
    in_window = [e for e in ledger if t0 <= _parse(e["created_at"]) <= t1]
    turns = [e for e in in_window if e["event"] == "analyst_lead.run_cycle.tools"]
    calls = [e for e in in_window if e["event"] == "analyst_lead.run_cycle.tool_call"]
    results = [e for e in ledger if e["event"] == "analyst_lead.run_cycle.tool_result"]
    waits = [r for r in results if (r.get("data") or {}).get("tool") == "wait_for_reply"]
    session_waits = [e.get("data") or {} for e in ledger if e["event"] == "coder.wait"]
    coder_events = sum(e["event"] in ("coder.turn_end", "coder.ask_lead") for e in in_window)
    by_tool: dict[str, int] = {}
    for c in calls:
        tool = (c.get("data") or {}).get("tool", "?")
        by_tool[tool] = by_tool.get(tool, 0) + 1
    window = _seconds_after(t1, t0)
    return {
        "authoring_window_s": round(window, 1),
        "lead_turns_in_window": len(turns),
        "lead_calls_by_tool": by_tool,
        "coder_events": coder_events,
        "wait_texts": [(w.get("data") or {}).get("text", "")[:300] for w in waits],
        "session_waits": session_waits,
        "unknown_tool_results": sum(
            "unknown tool" in (r.get("data") or {}).get("text", "") for r in results
        ),
        "floor_s": floor,
        "slot6_baseline": "~400 lead calls over ~75 min of coder work (2026-09-15)",
    }


def _assert_lead_messaged_its_coder(ledger: list[dict]) -> None:
    """E12: the lead reaches its coder with `message(to="coder")`, in its own tool call."""
    to_coder = [
        e for e in ledger if e["event"] == "channel.message"
        and (e.get("data") or {}).get("from") == "analyst_lead"
        and (e.get("data") or {}).get("to") == "coder"
    ]
    assert to_coder, "the lead never messaged its coder, so the coder address was not exercised"
    assert not any((e.get("data") or {}).get("is_error") for e in to_coder), to_coder


def _assert_lead_waited(obs: dict) -> None:
    pacing = obs["pacing"]
    assert pacing.get("authoring_window_s"), "no coder turn was recorded, so nothing was measured"
    assert pacing["lead_calls_by_tool"].get("wait_for_reply"), "the lead never used wait_for_reply"
    assert pacing["unknown_tool_results"] == 0
    for wait in pacing["session_waits"]:
        # Mechanical: a wait with no news can only return at or after the floor.
        if wait.get("reason") == "timeout":
            assert wait["waited_s"] >= pacing["floor_s"] - 1, f"a no-news wait returned early: {wait}"
    ceiling = 2 * pacing["coder_events"] + pacing["authoring_window_s"] / 60 + 10
    assert pacing["lead_turns_in_window"] <= ceiling, (
        f"{pacing['lead_turns_in_window']} lead turns in {pacing['authoring_window_s']:.0f}s of "
        f"coder work with {pacing['coder_events']} coder events — above the {ceiling:.0f} ceiling; "
        "the lead is still busy-looping. Read the record."
    )


def test_lead_waits_instead_of_polling_and_a_steer_wakes_it():
    """Cells 1 + 4: a real lead authors one link; a human steer lands while it is parked."""
    steer_text = "Operator timing probe: no change to your plan. Carry on as you were."

    async def on_parked(control, lead, observed):
        control.add_steer(lead.agent_key, steer_text)
        observed["steer_sent_at"] = _now()

    obs, clone, _ = _lead_run("lead_cycle", on_parked)
    ledger = _read_ledger("lead_cycle")
    _assert_lead_waited(obs)
    _assert_lead_messaged_its_coder(ledger)
    if not obs.get("fired"):
        pytest.skip("the lead was never parked in wait_for_reply while its coder worked; "
                    "the steer wake was not exercised (pacing above was still asserted)")
    call_id = obs["parked_call_id"]
    result = next(
        e for e in ledger
        if e["event"] == "analyst_lead.run_cycle.tool_result"
        and (e.get("data") or {}).get("tool_call_id") == call_id
    )
    latency = _seconds_after(_parse(result["created_at"]), _parse(obs["steer_sent_at"]))
    assert latency < WAKE_SLACK_S, f"the parked wait returned {latency:.1f}s after the steer"
    assert "a message for you has arrived" in result["data"]["text"].lower()
    assert any(e["event"] == "analyst_lead.run_cycle.steer" for e in ledger), (
        "the wait woke but the steer never reached the lead's conversation"
    )
    shutil.rmtree(clone.parent, ignore_errors=True)


def test_lead_stop_while_parked_in_the_wait_cancels_cleanly():
    """Cell 5: `/stop` lands while the lead is parked in wait_coder with its coder working."""

    async def on_parked(control, lead, observed):
        observed["interrupted"] = control.interrupt("live coder-wait stop cell", by="test")

    obs, clone, base = _lead_run("lead_stop", on_parked)
    if not obs.get("fired"):
        pytest.skip("the lead was never parked in wait_for_reply while its coder worked")
    assert obs.get("interrupted"), "the stop landed but cancelled nothing"
    assert obs["outcome"] == "cancelled", obs["outcome"]
    latency = _seconds_after(_parse(obs["cancelled_at"]), _parse(obs["fired_at"]))
    assert latency < 30, f"the run took {latency:.1f}s to cancel from inside wait_coder"
    assert obs["head_after"] == base, "a non-committed attempt left HEAD off its base ref"
    assert obs["status_after"] == "", f"the worktree is dirty after the stop:\n{obs['status_after']}"
    assert not obs["leaked_coder_pids"], f"coder process(es) survived: {obs['leaked_coder_pids']}"
    shutil.rmtree(clone.parent, ignore_errors=True)


# ─────────────────────────────────────────────────────────────── ledger reading


def _read_ledger(cell: str) -> list[dict]:
    path = LOG_DIR / f"{cell}_ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

