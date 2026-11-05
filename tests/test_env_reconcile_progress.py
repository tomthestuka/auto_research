"""D1 — the launch reconcile says what it builds and keeps saying it while the build runs.

Run polevo-20261104 was silent for 34 minutes while `ar-xspec` rebuilt. The launch step
(`graph.runtime._reconcile_environments`) runs the real `merge_flow.reconcile_environments` over a
real committed definition; only conda (the runner's subprocess) is faked, and made slow.
"""

from __future__ import annotations

import asyncio
import time

from tests.test_env_tools import _reconcile_setup


class _Ledger:
    """The run monitor's interface, keeping each event whole (summary included)."""

    def __init__(self):
        self.events: list[dict] = []

    async def record(self, team, event, *, level, summary, actor=None, data=None):
        self.events.append({"event": event, "level": level, "summary": summary, "data": data or {}})

    def of(self, name):
        return [e for e in self.events if e["event"] == name]


def test_a_long_build_records_its_start_and_a_heartbeat_until_it_ends(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.graph import runtime
    from auto_research.sandbox import env_build

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    fake = _fake_conda([])

    class _SlowConda(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            time.sleep(0.15)  # each conda step of the build, in the reconcile's thread
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _SlowConda)
    monkeypatch.setattr("auto_research.sandbox.merge_flow.BUILD_HEARTBEAT_S", 0.05)
    mon = _Ledger()

    asyncio.run(runtime._reconcile_environments(toolkit, mon))

    names = [e["event"] for e in mon.events]
    [start] = mon.of("env.build.start")
    assert start["data"] == {"env": "built", "cause": "missing"}
    assert start["summary"] == "Building ar-built (missing)" and start["level"] == 2
    beats = mon.of("env.build.progress")
    assert len(beats) >= 2, "the ledger hears from a running build at every interval"
    assert all(b["data"]["env"] == "built" for b in beats)
    assert "ar-built" in beats[-1]["summary"]
    # In order: the start, the heartbeats while it ran, then the build's own result.
    assert names.index("env.build.start") < names.index("env.build.progress") < names.index("env.build")
    assert not [n for n in names[names.index("env.build"):] if n == "env.build.progress"]


def test_a_quick_reconcile_records_no_heartbeat(tmp_path, monkeypatch):
    """Nothing to build (the second launch): a reused prefix is not announced, and no heartbeat
    falls inside one interval."""
    from tests.test_env_build import _fake_conda

    from auto_research.graph import runtime
    from auto_research.sandbox import env_build
    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    fake = _fake_conda([])
    reconcile_environments(toolkit, runner=fake)  # built once already

    class _Conda(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _Conda)
    mon = _Ledger()
    asyncio.run(runtime._reconcile_environments(toolkit, mon))
    assert not mon.of("env.build.progress") and not mon.of("env.build.start")


def test_a_stop_during_a_build_stops_the_runner_and_ends_the_launch_step(tmp_path, monkeypatch):
    """Cancellation (a /stop) reaches the builds: the runner is stopped and the step re-raises."""
    import pytest

    from tests.test_env_build import _fake_conda

    from auto_research.graph import runtime
    from auto_research.sandbox import env_build

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    fake = _fake_conda([])
    runners: list = []

    class _SlowConda(env_build.StoppableRunner):
        def __init__(self):
            super().__init__()
            runners.append(self)

        def __call__(self, argv, **kwargs):
            for _ in range(40):  # a long conda step that a stop ends
                if self.stopped:
                    raise RuntimeError("stopped")
                time.sleep(0.01)
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _SlowConda)
    monkeypatch.setattr("auto_research.sandbox.merge_flow.BUILD_HEARTBEAT_S", 0.05)

    async def scenario():
        step = asyncio.create_task(runtime._reconcile_environments(toolkit, _Ledger()))
        await asyncio.sleep(0.12)
        step.cancel()
        with pytest.raises(asyncio.CancelledError):
            await step

    asyncio.run(scenario())
    assert runners and runners[0].stopped


def test_the_commit_time_build_records_its_start_and_a_heartbeat(tmp_path, monkeypatch):
    """D1b: the build after `approve_edit` (a direct-branch commit's environments) gets the same
    events as the launch reconcile. The real `AuthoringSession._build_environments` runs on a
    session holding only what it reads; conda is faked and slow."""
    from types import SimpleNamespace

    from tests.test_env_build import _fake_conda

    from auto_research.sandbox import env_build
    from auto_research.sandbox.merge_flow import AuthoringSession

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    fake = _fake_conda([])

    class _SlowConda(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            time.sleep(0.15)
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _SlowConda)
    monkeypatch.setattr("auto_research.sandbox.merge_flow.BUILD_HEARTBEAT_S", 0.05)
    mon = _Ledger()
    session = SimpleNamespace(_direct_branch="ai", worktree=toolkit, _monitor=mon,
                              last_commit_sha="c0ffee")

    note = asyncio.run(AuthoringSession._build_environments(session, {"envs/built/env.yaml": "added"}))

    assert "Environment built: ar-built." in note
    [start] = mon.of("env.build.start")
    assert start["data"] == {"commit_sha": "c0ffee", "env": "built", "cause": "missing"}
    assert len(mon.of("env.build.progress")) >= 2
    [built] = mon.of("env.build")
    assert built["data"]["fresh"] == "missing"


def test_a_rebuild_the_reconcile_read_as_intact_is_announced_and_reads_built(tmp_path, monkeypatch):
    """D1b: the definition and prefix are intact, but the record was built unjailed and the jail is
    now on, so `build` rebuilds; the launch says so once (level 2, cause `unjailed`) and the
    result reads `built` with that cause."""
    from tests.test_env_build import _fake_conda

    from auto_research.graph import runtime
    from auto_research.sandbox import env_build, jail
    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    fake = _fake_conda([])
    reconcile_environments(toolkit, runner=fake)  # built with the jail off
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: True)
    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    from tests.test_env_build import _conda_install

    conda_exe = str(_conda_install(tmp_path / "opt" / "conda"))  # a jailed build binds its install
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: conda_exe)

    class _Conda(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _Conda)
    mon = _Ledger()
    asyncio.run(runtime._reconcile_environments(toolkit, mon))

    [start] = mon.of("env.build.start")
    assert start["data"] == {"env": "built", "cause": "unjailed"} and start["level"] == 2
    assert not mon.of("env.build.error"), mon.of("env.build.error")
    [built] = mon.of("env.build")
    assert built["data"]["status"] == "built" and built["data"]["cause"] == "unjailed"
    assert built["summary"] == "built ar-built (its build ran unjailed and the jail is on)"
