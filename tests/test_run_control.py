"""Deterministic tests for RunControl + its consumption in the ReAct loop (Stage 3.1, §12).

RunControl is the shared object the HITL slash-commands write and the agents read. Here we test
the object itself and its ReAct-loop effect with a fake model (no LLM): a queued `/steer` is
injected as a user turn before the next model call.

A `/stop` has **no** ReAct-loop effect since 2026-07-31 — it is an interrupt that cancels the run
task, so the tests below pin the *absence* of the old cooperative behaviour (the flag alone does
nothing) and that a cancellation propagates out of the loop rather than being swallowed.
"""

from __future__ import annotations

import asyncio

import pytest

from auto_research.agents.roles import AgentRole
from auto_research.models.types import ModelMessage, ModelResponse
from auto_research.run_control import STOP_SENTINEL, RunControl


def test_run_control_steer_inbox_drains_once_per_key():
    rc = RunControl()
    rc.add_steer("analyst_worker_0", "focus on DU3")
    rc.add_steer("analyst_worker_0", "check exposure")
    rc.add_steer("reviewer", "be strict")
    assert rc.drain_steer("analyst_worker_0") == ["focus on DU3", "check exposure"]
    assert rc.drain_steer("analyst_worker_0") == []      # drained
    assert rc.drain_steer("reviewer") == ["be strict"]
    assert rc.drain_steer(None) == []                    # unkeyed → nothing


def test_run_control_request_stop_sets_flag_and_event():
    rc = RunControl()
    assert not rc.stop_requested and not rc.stop_event.is_set()
    rc.request_stop("user asked", by="human")
    assert rc.stop_requested and rc.stop_event.is_set()
    assert rc.stop_reason == "user asked" and rc.stopped_by == "human"
    assert STOP_SENTINEL == "[run stopped by human]"


class _FakeModel:
    """Records the messages of each call; returns queued responses (default = no tool calls)."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[list] = []

    async def call(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self.responses.pop(0) if self.responses else ModelResponse(content="done")


class _FakeTools:
    def tool_definitions(self):
        return [{"name": "noop", "description": "", "input_schema": {"type": "object"}}]

    async def execute(self, name, args):  # pragma: no cover — not reached in these tests
        raise AssertionError("execute should not run")


def _bare_role(model, run_control=None, agent_key=None) -> AgentRole:
    inst = object.__new__(AgentRole)
    inst.role = "analyst_worker"
    inst.model = model
    inst.monitor = None
    inst.tools = None
    inst.last_response = None
    inst.run_control = run_control
    inst.agent_key = agent_key
    return inst


def _seed():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="task")]


def test_react_injects_steer_before_next_model_call():
    rc = RunControl()
    rc.add_steer("w", "prioritize the DU3 fix")
    model = _FakeModel()  # returns a no-tool-call response → loop ends after one turn
    role = _bare_role(model, run_control=rc, agent_key="w")

    asyncio.run(role._react(seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5))

    assert len(model.calls) == 1
    injected = [
        m for m in model.calls[0]
        if isinstance(m, ModelMessage) and m.content.startswith("[Human steer]")
    ]
    assert injected and "prioritize the DU3 fix" in injected[0].content
    assert rc.drain_steer("w") == []  # consumed


def test_react_does_not_consult_the_stop_flag():
    """A stop is an interrupt, not a flag the loop polls (2026-07-31).

    The cooperative wind-down this used to drive stopped a role's *tool use* and then let it —
    and the whole pipeline behind it — keep making model calls. `/stop` now cancels the run task,
    so `_react` has nothing to notice: with the flag set and no cancellation, the loop is
    byte-identical to an ordinary one.
    """
    rc = RunControl()
    rc.request_stop("human stop")
    model = _FakeModel()
    role = _bare_role(model, run_control=rc, agent_key="w")

    asyncio.run(
        role._react(seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5)
    )

    assert len(model.calls) == 1  # the flag alone changes nothing


def test_react_cancellation_propagates_out_of_the_loop():
    """The interrupt reaches a role mid-await and is not swallowed on the way out."""

    class _Hanging:
        async def call(self, *a, **k):
            await asyncio.sleep(3600)

    async def scenario():
        role = _bare_role(_Hanging(), run_control=RunControl(), agent_key="w")
        task = asyncio.create_task(
            role._react(seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5)
        )
        await asyncio.sleep(0)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scenario())


def test_react_no_run_control_is_unchanged():
    """A role without run_control behaves exactly as before (one model turn, no injection)."""
    model = _FakeModel()
    role = _bare_role(model, run_control=None, agent_key=None)
    asyncio.run(role._react(seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5))
    assert len(model.calls) == 1
    assert not any(
        isinstance(m, ModelMessage) and m.content.startswith("[Human steer]")
        for m in model.calls[0]
    )


# ── mid-batch delivery: a model turn can emit MANY tool calls (2026-07-29 live finding) ───────


class _BatchTools:
    """Tools that record dispatches; the human acts while the first call is 'running'."""

    def __init__(self, on_first=None):
        self.dispatched: list[str] = []
        self._on_first = on_first

    def tool_definitions(self):
        return [{"name": "slow", "description": "", "input_schema": {"type": "object"}}]

    async def execute(self, name, args):
        from auto_research.agents.react import ToolOutcome

        self.dispatched.append(name)
        if len(self.dispatched) == 1 and self._on_first is not None:
            self._on_first()          # the human sends /stop or /steer during the first call
        return ToolOutcome(text="ok", is_error=False)


def _batched_response(names):
    from auto_research.models.types import ToolCall

    return ModelResponse(
        content="",
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments={}) for i, n in enumerate(names)],
    )


def test_stop_flag_mid_batch_does_not_skip_calls():
    """The mid-batch skip went with the cooperative wind-down: an interrupt cancels the awaits
    instead, so nothing has to synthesize a result for a call it declined to make."""
    from auto_research.models.types import ToolResultMessage

    rc = RunControl()
    tools = _BatchTools(on_first=lambda: rc.request_stop("human /stop"))
    model = _FakeModel([_batched_response(["slow", "slow", "slow"])])
    role = _bare_role(model, run_control=rc, agent_key="w")

    messages, _, _ = asyncio.run(
        role._react(seed_messages=_seed(), tools=tools, action="analyze", cap=5)
    )

    assert tools.dispatched == ["slow", "slow", "slow"]  # the flag no longer short-circuits
    results = [m for m in messages if isinstance(m, ToolResultMessage)][-1].results
    assert [r.is_error for r in results] == [False, False, False]


def test_steer_arriving_mid_batch_reaches_the_next_model_call():
    """A steer queued while a batch is running is delivered right after that batch."""
    rc = RunControl()
    tools = _BatchTools(on_first=lambda: rc.add_steer("w", "stop downloading locked data"))
    model = _FakeModel([_batched_response(["slow", "slow"]), ModelResponse(content="done")])
    role = _bare_role(model, run_control=rc, agent_key="w")

    asyncio.run(role._react(seed_messages=_seed(), tools=tools, action="analyze", cap=5))

    assert tools.dispatched == ["slow", "slow"]  # a plain steer does not cancel the batch
    injected = [
        m for m in model.calls[-1]
        if isinstance(m, ModelMessage) and m.content.startswith("[Human steer]")
    ]
    assert injected and "stop downloading locked data" in injected[0].content
    assert rc.drain_steer("w") == []             # consumed, not re-delivered next iteration


def test_events_are_actored_by_the_steerable_agent_key():
    """A log line must name the identity `/steer` addresses, so two workers are distinguishable.

    Belongs with run control: the actor is what a human reads to decide which worker to steer.
    """

    class _Recorder:
        def __init__(self):
            self.rows: list[tuple[str, str | None]] = []

        async def record(self, team, event, *, level, summary, actor=None, data=None):
            self.rows.append((event, actor))
            return None

    monitor = _Recorder()
    keyed = _bare_role(_FakeModel(), agent_key="analyst_worker_1")
    keyed.monitor = monitor
    unkeyed = _bare_role(_FakeModel(), agent_key=None)
    unkeyed.monitor = monitor

    asyncio.run(keyed._record("analyst_worker.analyze.step", level=7, summary="s"))
    asyncio.run(unkeyed._record("analyst_worker.analyze.step", level=7, summary="s"))

    assert monitor.rows[0][1] == "analyst_worker_1"   # the key the steer channel accepts
    assert monitor.rows[1][1] == "analyst_worker"     # no key → the role, as before


# ── forced stop: cancel in-flight work instead of waiting for a safe point ─────────────────────


def test_stop_cancels_in_flight_work_through_registered_aborters():
    """A stop that waits for a safe point is not a stop (the safe point was 25 minutes away)."""
    rc = RunControl()
    calls: list[str] = []

    async def busy_aborter():
        calls.append("worker_0")
        return True          # it WAS mid-call

    async def idle_aborter():
        calls.append("worker_1")
        return False         # nothing to cancel

    rc.register_aborter("analyst_worker_0", busy_aborter)
    rc.register_aborter("analyst_worker_1", idle_aborter)

    assert asyncio.run(rc.abort_all()) == 1      # one was busy
    assert sorted(calls) == ["worker_0", "worker_1"]
    assert asyncio.run(rc.abort("analyst_worker_0")) == 1
    assert asyncio.run(rc.abort("nobody")) == 0  # unknown key is a no-op, not an error


def test_a_failing_aborter_cannot_defeat_the_stop():
    rc = RunControl()

    async def broken():
        raise RuntimeError("sandbox already gone")

    async def fine():
        return True

    rc.register_aborter("a", broken)
    rc.register_aborter("b", fine)
    assert asyncio.run(rc.abort_all()) == 1      # the broken one is swallowed, b still runs
