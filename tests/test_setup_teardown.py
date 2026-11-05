"""D1d/D1e — a `run_research` setup step that fails, after the preflight, closes what the setup
had opened, leaf to root: the checkpointer, the worker sandboxes, the tools server, then the
human-channel connector (the signal handlers and run-task registration first). A failure after
`run.start` is the run's `run.error`; a stop mid-setup takes a stop's path; a close that raises is
recorded and never replaces the run's own outcome.

Runs the real `run_research` on the hermetic wiring the stop tests use (`tests.test_react_ceiling`);
each resource's own close is wrapped to record its order and then runs. No model is called.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from tests.test_react_ceiling import _hermetic_run, _ledger


class _Connector:
    def __init__(self, order):
        self.order = order

    async def __call__(self, query):
        raise AssertionError("never asked")

    async def aclose(self):
        self.order.append("connector")

    async def notify(self, text):
        self.order.append("checklist")
        self.checklist = text


class _Model:
    provider = "openai"
    model = "stand-in"


def _spy_teardown(monkeypatch, order):
    """Record each resource's close, then run it."""
    from auto_research.graph import runtime
    from auto_research.sandbox.mcp_client import MCPToolClient
    from auto_research.sandbox.persistent import PersistentSandbox

    real_mcp_shutdown = MCPToolClient.shutdown
    real_sandbox_shutdown = PersistentSandbox.shutdown
    real_signals = runtime._install_interrupt_signals
    real_checkpointer = runtime.open_checkpointer

    async def mcp_shutdown(self):
        order.append("mcp")
        await real_mcp_shutdown(self)

    async def sandbox_shutdown(self):
        order.append("sandbox")
        await real_sandbox_shutdown(self)

    def signals(run_control):
        restore = real_signals(run_control)

        def recorded():
            order.append("signals")
            restore()

        return recorded

    @contextlib.asynccontextmanager
    async def checkpointer(path):
        async with real_checkpointer(path) as saver:
            try:
                yield saver
            finally:
                order.append("checkpointer")

    monkeypatch.setattr(MCPToolClient, "shutdown", mcp_shutdown)
    monkeypatch.setattr(PersistentSandbox, "shutdown", sandbox_shutdown)
    monkeypatch.setattr(runtime, "_install_interrupt_signals", signals)
    monkeypatch.setattr(runtime, "open_checkpointer", checkpointer)


def _worker():
    from auto_research.agents import AnalystWorkerAgent

    return AnalystWorkerAgent(model=_Model(), prompt="w")


def _run(tmp_path, order, worker=None):
    from auto_research import run_research
    from auto_research.schemas import ResearchRequest

    class Never:
        last_receipts: dict = {}

        async def review(self, request, **_):
            raise RuntimeError("the reviewer was reached")

        async def research(self, request, *, follow_up=None):
            raise AssertionError("never routed to")

        async def run(self, workers, lead, task):
            raise AssertionError("never routed to")

    worker = worker or _worker()
    return asyncio.run(asyncio.wait_for(run_research(
        ResearchRequest(question="q"), reviewer=Never(), io_agent=Never(), retrieval=None,
        workers=[worker], lead=object(), consensus=Never(), max_iterations=1, chatter=10,
        monitor_jsonl=tmp_path / "monitor.jsonl", report_formats=[],
        human_handler=_Connector(order), install_signal_handlers=True,
    ), timeout=60))


def test_a_failed_graph_build_closes_every_resource_leaf_to_root(tmp_path, monkeypatch):
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)

    async def started(self):  # the REPL is outside this test; its close is what is checked
        order.append("sandbox.start")

    def broken_build(**_):
        raise RuntimeError("the graph did not build")

    monkeypatch.setattr(PersistentSandbox, "start", started)
    monkeypatch.setattr("auto_research.graph.runtime.build_graph", broken_build)
    with pytest.raises(RuntimeError, match="did not build"):
        _run(tmp_path, order)
    assert order == ["sandbox.start", "signals", "checkpointer", "sandbox", "mcp", "connector"]
    names = [e["event"] for e in _ledger(tmp_path)]
    # After `run.start`, the setup's failure is the run's (D1e), recorded before the closes.
    assert "run.start" in names and names[-2:] == ["run.error", "run.summary"]
    (error,) = [e for e in _ledger(tmp_path) if e["event"] == "run.error"]
    assert "the graph did not build" in error["data"]["error"]


def test_a_sandbox_that_fails_to_start_closes_what_came_before(tmp_path, monkeypatch):
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)

    async def refused(self):
        raise RuntimeError("the REPL did not boot")

    monkeypatch.setattr(PersistentSandbox, "start", refused)
    with pytest.raises(RuntimeError, match="did not boot"):
        _run(tmp_path, order)
    # The half-started sandbox is shut down too; no checkpointer was opened yet.
    assert order == ["signals", "sandbox", "mcp", "connector"]


def test_a_tools_server_that_fails_to_start_is_closed(tmp_path, monkeypatch):
    from auto_research.sandbox.mcp_client import MCPToolClient

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)

    async def refused(self):
        raise RuntimeError("the tools server did not start")

    monkeypatch.setattr(MCPToolClient, "start", refused)
    with pytest.raises(RuntimeError, match="did not start"):
        _run(tmp_path, order)
    assert order == ["signals", "mcp", "connector"]
    # Before `run.start` the run never began: nothing claims it did (no ledger line at all here).
    names = [e["event"] for e in _ledger(tmp_path)] if (tmp_path / "monitor.jsonl").exists() else []
    assert "run.start" not in names and "run.error" not in names


def test_a_finished_setup_hands_its_resources_to_the_runs_own_teardown(tmp_path, monkeypatch):
    """The success path is unchanged: the graph's failure is torn down once, by the run's own
    `finally` (the channel tasks, sandboxes, coding sessions, tools server, connector), and the
    checkpointer closes as the run returns."""
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)

    async def started(self):
        order.append("sandbox.start")

    monkeypatch.setattr(PersistentSandbox, "start", started)
    with pytest.raises(RuntimeError, match="the reviewer was reached"):
        _run(tmp_path, order)
    assert order == ["sandbox.start", "signals", "sandbox", "mcp", "connector", "checkpointer"]


def test_a_stop_during_the_setups_closes_keeps_the_setups_error(tmp_path, monkeypatch):
    """A /stop arriving while the setup closes what it opened neither cuts a close short nor
    replaces the setup's error with a bare cancel: the run task is unregistered first (D8)."""
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)
    worker = _worker()
    spied_shutdown = PersistentSandbox.shutdown

    async def started(self):
        order.append("sandbox.start")

    async def stopped_while_closing(self):
        worker.run_control.interrupt("stop during the closes", by="human")
        await asyncio.sleep(0.2)  # a cancel sent now would land here
        order.append("sandbox.closed")
        await spied_shutdown(self)

    def broken_build(**_):
        raise RuntimeError("the graph did not build")

    monkeypatch.setattr(PersistentSandbox, "start", started)
    monkeypatch.setattr(PersistentSandbox, "shutdown", stopped_while_closing)
    monkeypatch.setattr("auto_research.graph.runtime.build_graph", broken_build)
    with pytest.raises(RuntimeError, match="did not build"):
        _run(tmp_path, order, worker)
    assert order == ["sandbox.start", "signals", "checkpointer", "sandbox.closed", "sandbox", "mcp",
                     "connector"]


def test_a_stop_mid_setup_takes_a_stops_path(tmp_path, monkeypatch):
    """D1e ruling (3): a /stop landing while a sandbox starts is a stop like one during the graph:
    the cleanup runs leaf to root, `run.interrupted` and `run.summary` are recorded, the landing
    checklist reaches the human, the connector closes last, and the caller gets
    `UserInterruptionError`."""
    from auto_research.run_control import UserInterruptionError
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)
    worker = _worker()
    real_kill = PersistentSandbox.kill_now

    async def stopped_while_starting(self):
        worker.run_control.interrupt("stop mid-setup", by="human")
        await asyncio.sleep(5)  # the stop's cancel lands here

    async def killed(self):
        order.append("sandbox.kill")
        await real_kill(self)

    monkeypatch.setattr(PersistentSandbox, "start", stopped_while_starting)
    monkeypatch.setattr(PersistentSandbox, "kill_now", killed)
    with pytest.raises(UserInterruptionError, match="stop mid-setup") as caught:
        _run(tmp_path, order, worker)
    assert caught.value.by == "human" and caught.value.artifacts["run_dir"]
    # The signal handlers last, as for a stop during the graph: a second signal is a no-op.
    assert order == ["sandbox.kill", "mcp", "checklist", "connector", "signals"]
    names = [e["event"] for e in _ledger(tmp_path)]
    assert "run.interrupted" in names and "run.summary" in names and "run.error" not in names


def test_a_close_that_raises_is_recorded_and_the_setups_error_stands(tmp_path, monkeypatch):
    """D1e ruling (defect 2), setup path: every later close still runs."""
    from auto_research.sandbox.mcp_client import MCPToolClient
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)
    spied_mcp = MCPToolClient.shutdown

    async def started(self):
        order.append("sandbox.start")

    async def mcp_raises(self):
        await spied_mcp(self)
        raise RuntimeError("the tools server close broke")

    def broken_build(**_):
        raise RuntimeError("the graph did not build")

    monkeypatch.setattr(PersistentSandbox, "start", started)
    monkeypatch.setattr(MCPToolClient, "shutdown", mcp_raises)
    monkeypatch.setattr("auto_research.graph.runtime.build_graph", broken_build)
    with pytest.raises(RuntimeError, match="did not build"):
        _run(tmp_path, order)
    assert order == ["sandbox.start", "signals", "checkpointer", "sandbox", "mcp", "connector"]
    (teardown,) = [e for e in _ledger(tmp_path) if e["event"] == "run.teardown.mcp_error"]
    assert "close broke" in teardown["data"]["error"]


def test_a_close_that_raises_at_the_runs_end_leaves_its_error_standing(tmp_path, monkeypatch):
    """D1e ruling (defect 2), the run's own `finally`: a sandbox and the checkpointer that fail to
    close are recorded; the run's error stands and the connector still closes."""
    from auto_research.graph import runtime
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)
    spied_checkpointer = runtime.open_checkpointer

    async def started(self):
        order.append("sandbox.start")

    async def sandbox_raises(self):
        order.append("sandbox")
        raise RuntimeError("the sandbox close broke")

    @contextlib.asynccontextmanager
    async def checkpointer_raises(path):
        async with spied_checkpointer(path) as saver:
            yield saver
        raise RuntimeError("the checkpointer close broke")

    monkeypatch.setattr(PersistentSandbox, "start", started)
    monkeypatch.setattr(PersistentSandbox, "shutdown", sandbox_raises)
    monkeypatch.setattr(runtime, "open_checkpointer", checkpointer_raises)
    with pytest.raises(RuntimeError, match="the reviewer was reached"):
        _run(tmp_path, order)
    assert order == ["sandbox.start", "signals", "sandbox", "mcp", "connector", "checkpointer"]
    names = [e["event"] for e in _ledger(tmp_path)]
    assert "run.teardown.sandbox_error" in names and "run.teardown.checkpointer_error" in names


def test_a_checkpointer_that_fails_to_close_leaves_a_finished_run_finished(tmp_path, monkeypatch):
    """Review finding: under `async with` the close's raise failed a finished run; now recorded."""
    from tests.test_connector_close import _Connector as _Answering, _run as _finished_run

    from auto_research.graph import runtime

    _hermetic_run(tmp_path, monkeypatch)
    spied_checkpointer = runtime.open_checkpointer

    @contextlib.asynccontextmanager
    async def checkpointer_raises(path):
        async with spied_checkpointer(path) as saver:
            yield saver
        raise RuntimeError("the checkpointer close broke")

    monkeypatch.setattr(runtime, "open_checkpointer", checkpointer_raises)
    connector = _Answering()
    result = _finished_run(tmp_path, connector)
    assert result.verdict is not None and connector.closed == 1
    names = [e["event"] for e in _ledger(tmp_path)]
    assert "run.finish" in names and "run.teardown.checkpointer_error" in names


def test_a_ledger_that_cannot_record_the_setups_error_still_closes_everything(tmp_path, monkeypatch):
    """Review finding: `run.error` failing to write neither skips the closes nor replaces the error."""
    from auto_research.monitor import RunMonitor
    from auto_research.sandbox.persistent import PersistentSandbox

    _hermetic_run(tmp_path, monkeypatch)
    order: list[str] = []
    _spy_teardown(monkeypatch, order)
    real_record = RunMonitor.record

    async def record(self, team, event, *args, **kwargs):
        if event == "run.error":
            raise OSError("ledger write failed")
        return await real_record(self, team, event, *args, **kwargs)

    async def started(self):
        order.append("sandbox.start")

    def broken_build(**_):
        raise RuntimeError("the graph did not build")

    monkeypatch.setattr(RunMonitor, "record", record)
    monkeypatch.setattr(PersistentSandbox, "start", started)
    monkeypatch.setattr("auto_research.graph.runtime.build_graph", broken_build)
    with pytest.raises(RuntimeError, match="did not build"):
        _run(tmp_path, order)
    assert order == ["sandbox.start", "signals", "checkpointer", "sandbox", "mcp", "connector"]
