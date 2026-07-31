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
