"""Ruling D8: `/stop` cleans the tree leaf to root, in the run task, and says what it did.

Driven through the real `run_research` with its real MCP client (the builtin shell server, a
subprocess) and the real Telegram connector over a fake bot: the human's `/stop` goes through
`handle_command`, the landing checklist through `notify`, and the connector is closed last.
"""

from __future__ import annotations

import asyncio
import json

import psutil
import pytest

from auto_research.run_control import UserInterruptionError
from tests.test_hitl import _FakeTGApp, _tg_update
from tests.test_react_ceiling import _hermetic_run


def _children() -> set[int]:
    return {p.pid for p in psutil.Process().children(recursive=True)}


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _matching(command: str) -> list[int]:
    found = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = " ".join(proc.info.get("cmdline") or [])
        if command in cmd and "bash_exec" not in cmd and "pytest" not in cmd:
            found.append(proc.info["pid"])
    return [pid for pid in found if _alive(pid)]


class _Librarian:
    last_receipts: dict = {}

    async def research(self, request, *, follow_up=None):
        raise AssertionError("never routed to")


def _stop_run(tmp_path, monkeypatch, *, stops: int = 1, shell_command: str | None = None):
    from auto_research import run_research
    from auto_research.hitl.telegram import TelegramHandler
    from auto_research.schemas import ResearchRequest

    _hermetic_run(tmp_path, monkeypatch)
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    seen: dict = {"before": _children()}
    from auto_research.sandbox.mcp_client import MCPToolClient

    started = MCPToolClient.start

    async def _start(self):  # the run's own client, kept so the test can call its shell
        seen["client"] = self
        await started(self)

    monkeypatch.setattr(MCPToolClient, "start", _start)

    class Reviewer:
        async def review(self, request, **_):
            # What the run started: its MCP server (the builtin shell) and anything under it.
            seen["servers"] = sorted(_children() - seen["before"])
            if shell_command is not None:
                # A shell command in flight when the stop lands (the jail is off in this config).
                asyncio.ensure_future(seen["client"].call_tool(
                    "mcp.builtin.bash_exec", {"command": shell_command, "timeout_s": 120}))
                for _ in range(200):
                    if _matching(shell_command):
                        break
                    await asyncio.sleep(0.05)
                seen["command"] = _matching(shell_command)
            for _ in range(stops):
                await handler._on_command(_tg_update("/stop"))
                await asyncio.sleep(0)
            await asyncio.sleep(30)  # the stop ends the run long before this
            raise AssertionError("the stop never landed")

    class Consensus:
        async def run(self, workers, lead, task):
            raise AssertionError("never routed to")

    async def _main():
        try:
            await run_research(
                ResearchRequest(question="q"), reviewer=Reviewer(), io_agent=_Librarian(),
                retrieval=None, workers=[], lead=object(), consensus=Consensus(),
                max_iterations=1, chatter=10, monitor_jsonl=tmp_path / "monitor.jsonl",
                report_formats=[], human_handler=handler,
            )
        finally:
            seen["cancelling"] = asyncio.current_task().cancelling()

    with pytest.raises(UserInterruptionError) as caught:
        asyncio.run(asyncio.wait_for(_main(), timeout=30))
    events = [json.loads(line) for line in (tmp_path / "monitor.jsonl").read_text().splitlines()]
    return caught.value, events, handler, seen


def test_stop_tears_down_leaf_to_root_in_the_run_task(tmp_path, monkeypatch):
    err, events, handler, seen = _stop_run(tmp_path, monkeypatch)

    # The caller gets the interruption with its artifacts, nothing left cancelling.
    assert err.artifacts["ledger"].endswith("monitor.jsonl") and err.artifacts["run_dir"]
    assert seen["cancelling"] == 0
    # The shell server the run started is gone.
    assert seen["servers"], "the run started no MCP server process to check"
    assert not [pid for pid in seen["servers"] if _alive(pid)]

    # Every step recorded, leaf to root; skipped ones with their reason; the channel last.
    steps = [(e["event"], e["data"].get("step")) for e in events
             if e["event"].startswith("run.interrupt.step")]
    assert steps == [
        ("run.interrupt.step", "cancel_resumed_seats"),
        ("run.interrupt.step_skipped", "kill_sandboxes"),
        ("run.interrupt.step_skipped", "authoring_reset"),
        ("run.interrupt.step", "mcp_shutdown"),
        ("run.interrupt.step", "stop_checklist"),
        ("run.interrupt.step", "hitl_shutdown"),
    ]
    names = [e["event"] for e in events]
    assert names.index("run.interrupted") < names.index("run.summary")
    assert names[-1] == "run.interrupt.step" and events[-1]["data"]["step"] == "hitl_shutdown"

    # The human saw the ack at once, then the checklist, in teardown order.
    texts = [m["text"] for m in handler.app.bot.sent]
    assert any("/stop received, cleaning up" in t for t in texts)
    checklist = next(t for t in texts if "/stop landed" in t)
    order = ["agents interrupted and closed ✓",
             "sandboxes killed – skipped (this run started no sandbox)",
             "authoring branch reset – skipped (no tool was being authored)",
             "MCP closed ✓", "cleanup complete"]
    positions = [checklist.index(item) for item in order]
    assert positions == sorted(positions)
    assert "✗" not in checklist  # a justified skip is not a failure


def test_a_second_stop_does_not_cancel_the_cleanup(tmp_path, monkeypatch):
    err, events, _, seen = _stop_run(tmp_path, monkeypatch, stops=2)
    assert isinstance(err, UserInterruptionError) and seen["cancelling"] == 0
    assert not [e for e in events if e["event"] == "run.interrupt.step_error"]


def test_a_shell_command_in_flight_dies_with_the_stop(tmp_path, monkeypatch):
    """`bash_exec` runs each command in its own session, outside the server's process group; the
    server kills them as it goes, so a command the stop interrupted does not outlive the run."""
    _, _, _, seen = _stop_run(tmp_path, monkeypatch, shell_command="sleep 47.25")
    assert seen["command"], "the shell command never started"
    assert not _matching("sleep 47.25")


class _Sandbox:
    def __init__(self, order, *, hang: float = 0.0):
        self.order, self.hang = order, hang

    async def kill_now(self):
        self.order.append("sandbox")
        await asyncio.sleep(self.hang)


class _Session:
    """An authoring session whose close resets the branch and saves a patch, as `aclose` does."""

    def __init__(self, order, monitor, *, fails: bool = False):
        self.order, self.monitor, self.fails = order, monitor, fails

    async def aclose(self):
        self.order.append("reset")
        if self.fails:
            await self.monitor.record("coder", "coder.tree_reset.error", level=3,
                                      summary="working-tree reset failed: git lock", data={})
        else:
            await self.monitor.record("coder", "coder.tree_reset", level=4, summary="reset",
                                      data={"patch": "/runs/x/tool_author_failed_t1.patch"})


class _Client:
    def __init__(self, order):
        self.order = order

    async def shutdown(self):
        self.order.append("mcp")


class _Handler:
    def __init__(self, order):
        self.order, self.sent = order, []

    async def notify(self, text):
        self.order.append("checklist")
        self.sent.append(text)

    async def aclose(self):
        self.order.append("connector")


def _cleanup(tmp_path, *, session_fails=False, sandbox_hang=0.0, cancel_after=None):
    from auto_research.graph.runtime import _interrupt_cleanup
    from auto_research.monitor import RunMonitor
    from auto_research.run_control import RunControl

    monitor = RunMonitor(jsonl_path=tmp_path / "ledger.jsonl")
    order: list[str] = []
    control = RunControl()
    control.request_stop("human /stop")
    control.authoring_session = _Session(order, monitor, fails=session_fails)
    handler = _Handler(order)

    async def _main():
        task = asyncio.ensure_future(_interrupt_cleanup(
            control, sandboxes=[_Sandbox(order, hang=sandbox_hang)], mcp_client=_Client(order),
            human_handler=handler, monitor=monitor,
        ))
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            task.cancel()
        try:
            await task
            return None
        except asyncio.CancelledError as exc:
            return exc

    cancelled = asyncio.run(_main())
    return order, handler.sent, monitor, cancelled


def test_the_teardown_runs_leaf_to_root_and_reports_each_step(tmp_path):
    order, sent, _, _ = _cleanup(tmp_path)
    assert order == ["sandbox", "reset", "mcp", "checklist", "connector"]
    (checklist,) = sent
    assert "sandbox 0 killed ✓" in checklist
    assert "authoring branch reset ✓ (patch saved: `/runs/x/tool_author_failed_t1.patch`)" in checklist
    assert checklist.rstrip().endswith("cleanup complete")


def test_a_reset_that_failed_quietly_is_reported_as_failed(tmp_path):
    _, sent, _, _ = _cleanup(tmp_path, session_fails=True)
    (checklist,) = sent
    assert "authoring branch reset ✗ (working-tree reset failed: git lock)" in checklist
    assert checklist.rstrip().endswith("cleanup finished with failures")


def test_an_outer_cancel_mid_cleanup_lets_every_step_run_then_ends_cancelled(tmp_path):
    order, sent, _, cancelled = _cleanup(tmp_path, sandbox_hang=2.0, cancel_after=0.2)
    assert order == ["sandbox", "reset", "mcp", "checklist", "connector"]
    assert "sandbox 0 killed ✗ (interrupted by an outer cancel" in sent[0]
    assert isinstance(cancelled, asyncio.CancelledError)


def test_channel_tasks_that_will_not_end_time_out_instead_of_hanging():
    from auto_research.run_control import RunControl

    async def _main():
        control = RunControl()

        async def stubborn():
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.shield(asyncio.sleep(3))  # its own cleanup outlasts the bound

        task = asyncio.create_task(stubborn())
        control.register_background(task)
        await asyncio.sleep(0)
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TimeoutError, match="1 channel task"):
            await control.cancel_resumed(timeout=0.3)
        return loop.time() - start

    assert asyncio.run(_main()) < 1.0


def test_an_open_authoring_link_is_left_to_the_stop_cleanup():
    """On a stop the lead's own unwinding leaves the link alone: no summary (a model call), no
    early reset; the cleanup resets it after the sandboxes are killed."""
    from auto_research.agents.roles import _ChainDriver
    from auto_research.run_control import RunControl

    closed: list[str] = []

    class Session:
        async def aclose(self):
            closed.append("aclose")

    control = RunControl()
    session = Session()
    control.authoring_session = session
    control.request_stop("human /stop")
    driver = _ChainDriver.__new__(_ChainDriver)
    driver.lead = type("Lead", (), {"run_control": control, "_segment_cut": None})()
    driver.session, driver.spec, driver.pending_cuts = session, object(), []
    driver.provider = type("Provider", (), {"authoring_session": session})()
    driver.cycle = type("Cycle", (), {"session_open": True})()
    asyncio.run(driver.close())
    assert closed == [] and control.authoring_session is session and driver.session is None
    # Nothing on the lead's own tools still points at the session the cleanup will close.
    assert driver.provider.authoring_session is None and driver.cycle.session_open is False


def test_a_command_still_running_is_listed_as_cancelled_by_the_stop(tmp_path, monkeypatch):
    """A `/status` still investigating when the channel closes is cancelled with it — and the
    checklist says so rather than leaving the human waiting on it."""
    from auto_research.hitl.telegram import TelegramHandler

    from auto_research import run_research
    from auto_research.schemas import ResearchRequest

    _hermetic_run(tmp_path, monkeypatch)
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")

    class Reviewer:
        async def review(self, request, **_):
            ctx = handler._cmd_ctx

            async def slow(question):
                await asyncio.sleep(60)
                return "never"

            ctx.status_answer = slow
            await handler._on_command(_tg_update("/status"))
            await asyncio.sleep(0.05)
            await handler._on_command(_tg_update("/stop"))
            await asyncio.sleep(30)

    class Consensus:
        async def run(self, workers, lead, task):
            raise AssertionError("never routed to")

    with pytest.raises(UserInterruptionError):
        asyncio.run(asyncio.wait_for(run_research(
            ResearchRequest(question="q"), reviewer=Reviewer(), io_agent=_Librarian(),
            retrieval=None, workers=[], lead=object(), consensus=Consensus(),
            max_iterations=1, chatter=10, monitor_jsonl=tmp_path / "monitor.jsonl",
            report_formats=[], human_handler=handler,
        ), timeout=30))
    checklist = next(m["text"] for m in handler.app.bot.sent if "/stop landed" in m["text"])
    assert "/status answer – cancelled by /stop" in checklist

