"""Deterministic tests for the detached (nohup-style) run entry (E5).

Covers the pure logic — run_id resolution, process-based liveness (with a REAL pid + a
PID-reuse guard), run status, reconstruction from disk — and `run_research_nohup`'s
attach/launch/overwrite behavior against a fake launcher that owns a real pid + temp files.
The real pipeline is never spawned here; the actual detached `Popen` is an opt-in live check.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from auto_research.graph import detached as d
from auto_research.graph.detached import (
    RunDied,
    _proc_create_time,
    is_run_alive,
    reconstruct_run,
    resolve_run_id,
    run_research_nohup,
    run_status,
)
from auto_research.schemas.review import ReviewVerdict


@pytest.fixture
def rundir(tmp_path, monkeypatch):
    """Point `run_dir()` at a tmp tree so tests never touch the real runtime dir."""
    monkeypatch.setattr(d, "run_dir", lambda rid: tmp_path / "runs" / rid)
    return lambda rid: tmp_path / "runs" / rid


@pytest.fixture
def sleeper():
    """Spawn real, short-lived processes so liveness runs against genuine pids."""
    procs: list[subprocess.Popen] = []

    def _spawn() -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        procs.append(p)
        return p

    yield _spawn
    for p in procs:
        try:
            p.kill()
            p.wait()
        except Exception:  # noqa: BLE001 — already gone
            pass


def _write_pid(rd: Path, pid: int, create_time) -> None:
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "backend.pid").write_text(json.dumps({"pid": pid, "create_time": create_time}))


def _done_result(rationale: str, accepted: bool = True) -> str:
    return json.dumps({
        "status": "done",
        "verdict": ReviewVerdict(accepted=accepted, rationale=rationale).model_dump(mode="json"),
    })


# --- run_id resolution ---

def test_resolve_run_id_auto_deterministic():
    a = resolve_run_id("cyg x-1 polarization")
    assert a == resolve_run_id("cyg x-1 polarization")   # same question → same id (reconnect)
    assert len(a) == 32 and a != resolve_run_id("a different question")


def test_resolve_run_id_named_and_validated():
    assert resolve_run_id("q", "my-run_1") == "my-run_1"
    with pytest.raises(ValueError):
        resolve_run_id("q", "bad id!")   # not path-safe


# --- liveness: process existence, not log freshness ---

def test_liveness_real_pid(tmp_path, sleeper):
    rd = tmp_path / "r"
    p = sleeper()
    _write_pid(rd, p.pid, _proc_create_time(p.pid))
    assert is_run_alive(rd) and run_status(rd) == "alive"
    p.kill(); p.wait()
    assert not is_run_alive(rd) and run_status(rd) == "dead"


def test_liveness_pid_reuse_guarded(tmp_path, sleeper):
    p = sleeper()
    rd = tmp_path / "r"
    _write_pid(rd, p.pid, (_proc_create_time(p.pid) or 0) + 999)  # pid alive, start-time mismatched
    assert not is_run_alive(rd)   # a recycled pid must not read as the original run


def test_status_absent_and_done(tmp_path):
    rd = tmp_path / "r"
    assert run_status(rd) == "absent"
    rd.mkdir(parents=True)
    (rd / "result.json").write_text("{}")
    assert run_status(rd) == "done"   # result.json wins even with no live process


# --- reconstruction from disk ---

def test_reconstruct_done(tmp_path):
    rd = tmp_path / "r"; rd.mkdir(parents=True)
    (rd / "result.json").write_text(json.dumps({
        "status": "done",
        "verdict": ReviewVerdict(accepted=True, rationale="looks good").model_dump(mode="json"),
        "final_state": {"analysis_iterations": 2},
    }))
    (rd / "monitor.jsonl").write_text("")
    run = reconstruct_run("cyg x-1", rd, "rid")
    assert run.verdict.accepted and run.verdict.rationale == "looks good"
    assert run.final_state["analysis_iterations"] == 2
    assert run.request.question == "cyg x-1"


def test_reconstruct_failed_raises(tmp_path):
    rd = tmp_path / "r"; rd.mkdir(parents=True)
    (rd / "result.json").write_text(json.dumps({"status": "failed", "error": "boom"}))
    with pytest.raises(RunDied) as exc:
        reconstruct_run("q", rd, "rid")
    assert "boom" in str(exc.value)


# --- overwrite guardrail (alive → refuse, dead → erase) ---

def test_overwrite_refuses_when_alive(rundir, sleeper):
    rid = resolve_run_id("q1"); rd = rundir(rid)
    p = sleeper()
    _write_pid(rd, p.pid, _proc_create_time(p.pid))
    (rd / "keep.txt").write_text("x")
    launched: list = []
    with pytest.raises(RuntimeError, match="still alive"):
        asyncio.run(run_research_nohup("q1", overwrite=True, poll_s=0.01,
                                       _launcher=lambda *a, **k: launched.append(1)))
    assert (rd / "keep.txt").exists()   # a live run is never wiped
    assert not launched                 # and never relaunched


def test_overwrite_erases_when_dead(rundir, sleeper):
    rid = resolve_run_id("q2"); rd = rundir(rid)
    p = sleeper(); pid, ct = p.pid, _proc_create_time(p.pid); p.kill(); p.wait()
    _write_pid(rd, pid, ct)             # dead pid
    (rd / "stale.txt").write_text("old")

    def _fake_launch(request, run_id, run_directory, **kw):
        run_directory.mkdir(parents=True, exist_ok=True)
        (run_directory / "result.json").write_text(_done_result("fresh", accepted=False))
        return 4242

    run = asyncio.run(run_research_nohup("q2", overwrite=True, poll_s=0.01, _launcher=_fake_launch))
    assert not (rd / "stale.txt").exists()   # dead run erased
    assert run.verdict.rationale == "fresh"


# --- attach vs launch ---

def test_attach_alive_does_not_relaunch(rundir, sleeper):
    """A live run re-invoked → attaches (never launches), returns when it writes result.json."""
    rid = resolve_run_id("q3"); rd = rundir(rid)
    p = sleeper()
    _write_pid(rd, p.pid, _proc_create_time(p.pid))
    (rd / "monitor.jsonl").write_text("")
    launched: list = []

    async def _drive():
        task = asyncio.create_task(run_research_nohup(
            "q3", poll_s=0.02, _launcher=lambda *a, **k: launched.append(1)))
        await asyncio.sleep(0.1)   # let it observe "alive" and start tailing
        (rd / "result.json").write_text(_done_result("done"))
        return await task

    run = asyncio.run(_drive())
    assert run.verdict.accepted
    assert not launched            # attached to the live run, never relaunched


def test_absent_launches_then_tails(rundir):
    def _fake_launch(request, run_id, run_directory, **kw):
        run_directory.mkdir(parents=True, exist_ok=True)
        (run_directory / "result.json").write_text(_done_result("ok"))
        return 1

    run = asyncio.run(run_research_nohup("q4", poll_s=0.01, _launcher=_fake_launch))
    assert run.verdict.accepted


def test_dead_at_entry_raises_rundied(rundir, sleeper):
    rid = resolve_run_id("q5"); rd = rundir(rid)
    p = sleeper(); pid, ct = p.pid, _proc_create_time(p.pid); p.kill(); p.wait()
    _write_pid(rd, pid, ct)
    (rd / "monitor.jsonl").write_text("")
    with pytest.raises(RunDied):
        asyncio.run(run_research_nohup("q5", poll_s=0.01, _launcher=lambda *a, **k: None))


# --- detached ⇒ no stdin, so cli HITL is rejected up front ---

def test_cli_channel_rejected():
    with pytest.raises(ValueError, match="cli"):
        asyncio.run(run_research_nohup("q", human_channel="cli"))


# --- live streaming: follow the backend's monitor.jsonl, chatter-filtered ---

def _emit_events(jsonl: Path):
    """Write real monitor events (the exact on-disk shape the backend streams) — L3 and L8.
    Synchronous (no event loop) so it is safe to call from inside a running `run_research_nohup`."""
    from auto_research.monitor import MonitorEvent
    events = [
        MonitorEvent(run_id="r", sequence=1, team="analysts", event="lvl3_event", level=3,
                     summary="low-level detail"),
        MonitorEvent(run_id="r", sequence=2, team="analysts", event="lvl8_event", level=8,
                     summary="high-level detail"),
    ]
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e.model_dump(mode="json"), sort_keys=True) + "\n")


def test_drain_jsonl_chatter_filters(tmp_path):
    import io

    from auto_research.graph.detached import _drain_jsonl
    jsonl = tmp_path / "monitor.jsonl"
    _emit_events(jsonl)

    lo = io.StringIO()
    _drain_jsonl(jsonl, 0, chatter=2, stream=lo)
    assert lo.getvalue() == ""                       # both events above chatter=2

    mid = io.StringIO()
    off = _drain_jsonl(jsonl, 0, chatter=5, stream=mid)
    assert "lvl3_event" in mid.getvalue() and "lvl8_event" not in mid.getvalue()
    assert off == jsonl.stat().st_size               # all complete lines consumed

    hi = io.StringIO()
    _drain_jsonl(jsonl, 0, chatter=10, stream=hi)
    assert "lvl3_event" in hi.getvalue() and "lvl8_event" in hi.getvalue()


def test_drain_jsonl_leaves_partial_line(tmp_path):
    import io

    from auto_research.graph.detached import _drain_jsonl
    jsonl = tmp_path / "monitor.jsonl"
    _emit_events(jsonl)
    full = jsonl.read_bytes()
    jsonl.write_bytes(full + b'{"partial": ')       # a not-yet-complete trailing line
    off = _drain_jsonl(jsonl, 0, chatter=10, stream=io.StringIO())
    assert off == len(full)                          # stops at the last newline; partial left


def test_stream_relays_events_during_run(rundir):
    import io

    buf = io.StringIO()

    def _fake_launch(request, run_id, run_directory, **kw):
        run_directory.mkdir(parents=True, exist_ok=True)
        _emit_events(run_directory / "monitor.jsonl")
        (run_directory / "result.json").write_text(_done_result("ok"))
        return 1

    run = asyncio.run(run_research_nohup(
        "q_stream", chatter=5, poll_s=0.01, stream=buf, _launcher=_fake_launch))
    assert run.verdict.accepted
    out = buf.getvalue()
    assert "lvl3_event" in out and "lvl8_event" not in out   # streamed live, chatter-filtered
