"""Deterministic tests for RunControl + its consumption in the ReAct loop (Stage 3.1, §12).

RunControl is the shared object the HITL slash-commands write and the agents/graph read. Here we
test the object itself and its two ReAct-loop effects with a fake model (no LLM): a queued
`/steer` is injected as a user turn before the next model call, and a `/stop` ends the loop
before any further model call.
"""

from __future__ import annotations

import asyncio

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


def test_react_stop_ends_loop_before_model_call():
    rc = RunControl()
    rc.request_stop("human stop")
    model = _FakeModel()
    role = _bare_role(model, run_control=rc, agent_key="w")

    messages, figures, docs = asyncio.run(
        role._react(seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5)
    )

    assert model.calls == []          # stop checked at the loop top → model never called
    assert messages == _seed()        # trajectory unchanged (seed only)


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
