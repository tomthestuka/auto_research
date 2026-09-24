"""Deterministic tests for the human interrupt: `/stop` and Ctrl-C are the same thing (§12).

A stop is not a request the pipeline honours at its next safe point — it cancels the run task,
runs a deterministic teardown, and raises ``UserInterruptionError``. What these pin:

- the ordering that makes the teardown correct (**kill before repair** — resetting the toolkit
  while the coder can still write to it just lets it dirty the branch again);
- that **no model is called** on the way out (the old wind-down made ~14 further calls);
- that a wedged step yields instead of hanging the interrupt, which is the failure the whole
  design exists to remove;
- that a cancel which is *not* ours is re-raised rather than mislabelled as a human stop.

No LLM, no git, no subprocesses: the pieces are fakes that record what was called and when.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from auto_research.graph import runtime
from auto_research.graph.runtime import _interrupt_cleanup
from auto_research.monitor import RunMonitor
from auto_research.run_control import RunControl, UserInterruptionError


class _Recorder:
    """Shared call log — the order across fakes is what most of these tests assert."""

    def __init__(self) -> None:
        self.events: list[str] = []


class _FakeSandbox:
    def __init__(self, log: _Recorder, name: str) -> None:
        self._log, self._name = log, name

    async def kill_now(self) -> None:
        self._log.events.append(f"kill:{self._name}")


class _FakeSession:
    def __init__(self, log: _Recorder) -> None:
        self._log = log
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1
        self._log.events.append("authoring_reset")


class _FakeMCP:
    def __init__(self, log: _Recorder) -> None:
        self._log = log

    async def shutdown(self) -> None:
        self._log.events.append("mcp")


class _FakeHandler:
    def __init__(self, log: _Recorder) -> None:
        self._log = log

    async def shutdown(self) -> None:
        self._log.events.append("hitl")


def _cleanup(log: _Recorder, *, sandboxes=None, session=None, monitor=None, handler=True):
    rc = RunControl()
    rc.request_stop("human /stop")
    rc.authoring_session = session
    return rc, _interrupt_cleanup(
        rc,
        sandboxes=sandboxes if sandboxes is not None else [_FakeSandbox(log, "w0")],
        mcp_client=_FakeMCP(log),
        human_handler=_FakeHandler(log) if handler else None,
        monitor=monitor or RunMonitor(chatter=10),
    )


# ── ordering: kill everything that can write, THEN repair the toolkit ──────────────────────────


def test_cleanup_kills_before_it_repairs_the_toolkit():
    """The load-bearing order. A git reset that runs while the coder subprocess is still alive is
    undone by the coder a moment later, so the branch is dirty anyway."""
    log = _Recorder()
    session = _FakeSession(log)
    _, coro = _cleanup(
        log,
        sandboxes=[_FakeSandbox(log, "w0"), _FakeSandbox(log, "w1")],
        session=session,
    )
    asyncio.run(coro)

    assert log.events == ["kill:w0", "kill:w1", "authoring_reset", "mcp", "hitl"]
    assert session.closed == 1


def test_cleanup_clears_the_session_so_the_reset_never_runs_twice():
    """`drive_authoring`'s own `finally` may still run after the interrupt closed the session;
    the handle is cleared here and `aclose` is idempotent, so the patch artifact is written once."""
    log = _Recorder()
    session = _FakeSession(log)
    rc, coro = _cleanup(log, session=session)
    asyncio.run(coro)

    assert rc.authoring_session is None
    assert log.events.count("authoring_reset") == 1


def test_cleanup_runs_without_an_authoring_session():
    log = _Recorder()
    _, coro = _cleanup(log, session=None)
    asyncio.run(coro)
    assert log.events == ["kill:w0", "mcp", "hitl"]


# ── a wedged step must not hang the interrupt ──────────────────────────────────────────────────


def test_a_wedged_step_times_out_and_the_rest_still_run(monkeypatch):
    """A git lock or a D-state child on NFS can block forever. An interrupt that hangs in teardown
    is the failure this design removes, so a step that overruns is recorded and skipped."""

    class _Wedged:
        async def kill_now(self) -> None:
            await asyncio.sleep(3600)

    log = _Recorder()
    monitor = RunMonitor(chatter=10)
    rc = RunControl()
    rc.request_stop("human /stop")
    # Shrink the per-step bound rather than waiting it out; the real value is the 10 s constant.
    monkeypatch.setattr(runtime, "_INTERRUPT_STEP_TIMEOUT_S", 0.01)

    asyncio.run(
        _interrupt_cleanup(
            rc,
            sandboxes=[_Wedged(), _FakeSandbox(log, "w1")],
            mcp_client=_FakeMCP(log),
            human_handler=None,
            monitor=monitor,
        )
    )

    assert log.events == ["kill:w1", "mcp"]  # the wedged step yielded; the rest ran
    names = [e.event for e in monitor.events]
    assert "run.interrupt.step_timeout" in names
    assert "run.interrupted" in names


def test_a_failing_step_is_recorded_and_does_not_stop_the_others():
    class _Broken:
        async def kill_now(self) -> None:
            raise RuntimeError("no such process")

    log = _Recorder()
    monitor = RunMonitor(chatter=10)
    rc = RunControl()
    rc.request_stop("human /stop")
    asyncio.run(
        _interrupt_cleanup(
            rc,
            sandboxes=[_Broken(), _FakeSandbox(log, "w1")],
            mcp_client=_FakeMCP(log),
            human_handler=None,
            monitor=monitor,
        )
    )

    assert log.events == ["kill:w1", "mcp"]
    assert "run.interrupt.step_error" in [e.event for e in monitor.events]


# ── the ledger closes the run, without a model ─────────────────────────────────────────────────


def test_cleanup_records_the_interruption_and_a_summary():
    monitor = RunMonitor(chatter=10)
    log = _Recorder()
    rc = RunControl()
    rc.request_stop("human /stop", by="human")
    asyncio.run(
        _interrupt_cleanup(
            rc, sandboxes=[_FakeSandbox(log, "w0")], mcp_client=_FakeMCP(log),
            human_handler=None, monitor=monitor,
        )
    )

    names = [e.event for e in monitor.events]
    assert names[-2:] == ["run.interrupted", "run.summary"]
    interrupted = [e for e in monitor.events if e.event == "run.interrupted"][0]
    assert interrupted.level == 1
    assert interrupted.data["reason"] == "human /stop"


def test_cleanup_calls_no_model():
    """The point of the redesign: a stop spends nothing. Any model reached from here would be a
    call the human already declined to pay for."""

    class _Exploding:
        async def call(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("a model was called on the interrupt path")

    monitor = RunMonitor(chatter=10)
    log = _Recorder()
    rc = RunControl()
    rc.request_stop("human /stop")
    rc.authoring_session = _FakeSession(log)
    asyncio.run(
        _interrupt_cleanup(
            rc, sandboxes=[_FakeSandbox(log, "w0")], mcp_client=_FakeMCP(log),
            human_handler=_Exploding(), monitor=monitor,  # no shutdown/close attr → skipped
        )
    )
    assert "authoring_reset" in log.events


# ── RunControl.interrupt itself ────────────────────────────────────────────────────────────────


def test_interrupt_flags_then_cancels_the_registered_task():
    async def scenario():
        rc = RunControl()
        task = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        rc.register_run_task(task)

        cancelled_immediately = rc.interrupt("human /stop") and not task.cancelled()
        with pytest.raises(asyncio.CancelledError):
            await task
        return rc, cancelled_immediately

    rc, deferred = asyncio.run(scenario())
    assert rc.stop_requested and rc.stop_event.is_set()
    assert deferred  # scheduled via call_soon, so a command handler's reply gets out first


def test_interrupt_without_a_task_still_flags_the_stop():
    async def scenario():
        rc = RunControl()
        return rc, rc.interrupt("human /stop")

    rc, cancelled = asyncio.run(scenario())
    assert rc.stop_requested and not cancelled


def test_interrupt_is_idempotent_on_a_finished_task():
    async def scenario():
        rc = RunControl()
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        rc.register_run_task(task)
        return rc.interrupt("second stop")

    assert asyncio.run(scenario()) is False


# ── the exception the caller sees ──────────────────────────────────────────────────────────────


def test_user_interruption_error_names_the_artifacts_and_the_way_back():
    exc = UserInterruptionError(
        "human /stop", by="human", where="analysis",
        artifacts={"run_dir": "/runs/abc", "ledger": "/runs/abc/monitor.jsonl"},
    )
    text = str(exc)
    assert "interrupted by human" in text and "while analysis" in text
    assert "No report was composed" in text and "resume=True" in text
    assert "/runs/abc/monitor.jsonl" in text
    assert exc.reason == "human /stop" and exc.by == "human"


# ── the sandbox's descendant walk ──────────────────────────────────────────────────────────────


def test_descendant_pids_walks_the_process_tree(tmp_path, monkeypatch):
    """Killing the REPL alone leaves an XSPEC fit or a download still writing to the obs-db, so
    the interrupt kills descendants too — read from /proc, not via killpg (the REPL shares the
    backend's process group, so a group kill would take the run down with it)."""
    from auto_research.sandbox import persistent

    # A fake /proc: 100 → 200, 201; 200 → 300.
    for pid, kids in {100: "200 201", 200: "300", 201: "", 300: ""}.items():
        task_dir = tmp_path / str(pid) / "task" / str(pid)
        task_dir.mkdir(parents=True)
        (task_dir / "children").write_text(kids)

    monkeypatch.setattr(
        persistent, "Path", lambda p: tmp_path / str(p).removeprefix("/proc/")
    )
    assert persistent._descendant_pids(100) == [200, 201, 300]


def test_descendant_pids_is_empty_when_proc_is_unreadable(monkeypatch):
    from auto_research.sandbox import persistent

    assert persistent._descendant_pids(2_147_483_646) == []


# ── the two entry points that surface an interruption ──────────────────────────────────────────


def test_signal_handlers_route_sigint_to_the_interrupt_and_restore_cleanly():
    """The CLI and the detached backend own their process, so Ctrl-C is the same interrupt as
    `/stop`. Installed through the loop's signal API, since a signal handler cannot await and the
    cancellation has to be scheduled on the loop."""
    import os
    import signal

    async def scenario():
        rc = RunControl()
        restore = runtime._install_interrupt_signals(rc)
        os.kill(os.getpid(), signal.SIGINT)
        # Delivery goes through the loop's self-pipe, so it needs a real iteration, not a yield.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if rc.stop_requested:
                break
        restore()
        return rc

    rc = asyncio.run(scenario())
    assert rc.stop_requested and rc.stopped_by == "signal"
    assert "SIGINT" in rc.stop_reason


def test_detached_reconstruct_raises_interruption_not_run_died(tmp_path):
    """A human stop is not a death: the detached backend cleaned up and exited on purpose, so the
    notebook sees the same exception an in-process run raises."""
    import json

    from auto_research.graph.detached import reconstruct_run

    (tmp_path / "result.json").write_text(
        json.dumps({"status": "interrupted", "error": "run interrupted by human"}),
        encoding="utf-8",
    )

    with pytest.raises(UserInterruptionError) as caught:
        reconstruct_run("q", tmp_path, "run-a")
    assert "monitor.jsonl" in str(caught.value)


# ══════════════════════════════════════ B8: /stop while the coder is mid-drive
#
# B made the coder run BESIDE the lead, which put a new writer on the toolkit that the interrupt
# path did not previously have to think about. The cleanup's own docstring states the invariant it
# turns on — *"nothing may be able to write to the toolkit when the toolkit is repaired"* — and a
# background drive is exactly a thing that can write while the reset runs.
#
# `RunControl.interrupt` cancels only the REGISTERED RUN TASK, and a task created inside the
# session is not in that set, so nothing else would stop it. These drive the **production**
# `_interrupt_cleanup` over a **real** `AuthoringSession` whose backend is a stub, with a drive
# genuinely in flight. What stays live-only is the real-process half: an actual coder subprocess
# and an actual git worktree.


class _WritingBackend:
    """A backend whose drive keeps 'writing to the toolkit' until something cancels it."""

    def __init__(self, log: _Recorder) -> None:
        self._log = log
        self.writes = 0

    async def connect(self) -> None:
        self._log.events.append("connect")

    async def drive(self, message: str) -> str:
        self._log.events.append("drive:start")
        try:
            while True:                       # the coder, working
                await asyncio.sleep(0.01)
                self.writes += 1
        except asyncio.CancelledError:
            self._log.events.append("drive:cancelled")
            raise

    async def disconnect(self) -> None:
        self._log.events.append("disconnect")


def _live_session(log: _Recorder):
    """A real AuthoringSession — real `start_drive`, real `aclose` — over a stub backend."""
    from auto_research.sandbox.merge_flow import AuthoringSession

    session = object.__new__(AuthoringSession)
    session._backend = _WritingBackend(log)
    session._closed = False
    session._drive_task = None
    session._drive_status = "idle"
    session._drive_result = None
    session._drive_error = None
    session._direct_branch = "ai"
    session._monitor = None
    session._pending_questions = []
    session._init_channel()
    session._toolkit_root = None
    session._worktree_base = None
    session._base_ref = "abc1234"
    session.tool_spec = SimpleNamespace(proposed_tool_name="ixpe_pcube_bands")

    # The reset is the "repair" step; record it rather than touching a real repository.
    def _pending() -> bool:
        return True

    session.pending_changes = _pending
    return session


def test_stop_mid_drive_kills_the_coder_before_the_toolkit_is_repaired(monkeypatch):
    """B8, the invariant the whole three-phase order exists for.

    A drive is in flight when the human stops the run. The coder task must be cancelled before the
    branch reset runs — a reset that races a live writer is undone by that writer a moment later,
    which is the dirty-tree state the next launch's preflight then blocks on.

    Verified to discriminate against two separate mutations before being kept: disconnecting
    before cancelling, and cancelling **without awaiting**. The second is the subtle one, and this
    catches it because `drive:cancelled` is logged by the drive itself — a cancel that is issued
    but never awaited never reaches that line before the reset runs. A third cell that counted the
    coder's writes during the repair was written and then deleted: it failed neither mutation, and
    its timing depended on when the loop happened to schedule the cancelled task.
    """
    log = _Recorder()

    async def _fake_reset(*args, **kwargs):
        log.events.append("tree_reset")

    monkeypatch.setattr("auto_research.sandbox.merge_flow._reset_direct_tree", _fake_reset)

    async def scenario():
        session = _live_session(log)
        session.start_drive("write the wrapper")
        await asyncio.sleep(0.05)                      # the coder is genuinely working
        assert session.drive_status == "working"
        _, coro = _cleanup(log, session=session)
        await coro

    asyncio.run(scenario())

    assert "drive:start" in log.events, "the drive never started; the test proves nothing"
    for earlier, later in (("drive:cancelled", "disconnect"), ("drive:cancelled", "tree_reset")):
        assert log.events.index(earlier) < log.events.index(later), (
            f"{earlier} did not precede {later}: {log.events}"
        )
    # And the kill phase still comes first, as it did before B.
    assert log.events.index("kill:w0") < log.events.index("tree_reset")


def test_stop_with_no_drive_in_flight_is_unchanged():
    """The guard is narrow: an interrupt with an idle coder behaves exactly as it did before B."""
    log = _Recorder()

    async def scenario():
        session = _live_session(log)
        _, coro = _cleanup(log, session=session)
        await coro

    with pytest.MonkeyPatch.context() as mp:
        async def _fake_reset(*args, **kwargs):
            log.events.append("tree_reset")

        mp.setattr("auto_research.sandbox.merge_flow._reset_direct_tree", _fake_reset)
        asyncio.run(scenario())

    assert "drive:cancelled" not in log.events
    assert log.events.index("disconnect") < log.events.index("tree_reset")
