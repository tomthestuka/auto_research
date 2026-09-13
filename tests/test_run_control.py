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


# ============================================================ A2: the injection hold rule
#
# An injected user turn is only legal at a turn boundary. When the assistant turn being answered
# left a provider-executed tool call unresolved, Anthropic documents that the following message
# "must contain nothing except the `tool_result` blocks" and that a block added after them ends
# the turn — which strands the server call and 400s. That killed run 8c75990e on 2026-09-10 at
# $42.11 with four tools already committed.
#
# Asserted on the BUILT MESSAGE ARRAY (what the model was actually sent), not on the queue: the
# 2026-08-06 lesson is that a test reading our own bookkeeping cannot see what reached the wire.

from auto_research.models.types import ToolCall, ToolResultMessage  # noqa: E402


def _pending_turn() -> ModelResponse:
    """The real 2026-09-10 turn shape: a server-tool call with no result, beside a client call.

    Frozen from logs/dev/science-bench/20260910T022054Z/slot-01/prompts.jsonl line 279 message 12
    — the `srvtoolu_` id in that turn is the one the 400 named.
    """
    return ModelResponse(
        content="",
        tool_calls=[ToolCall(id="toolu_01AnT188qU4FY2rnaWLtEv6G", name="noop", arguments={})],
        stop="tool_use",
        has_unresolved_server_tool=True,
        provider_parts=[
            {"type": "thinking", "thinking": "", "signature": "<elided>"},
            {"type": "server_tool_use", "id": "srvtoolu_01PCDhG72zkmP2EY5L4D1nmX",
             "name": "tool_search_tool_bm25", "caller": None, "input": {}},
            {"type": "tool_use", "id": "toolu_01AnT188qU4FY2rnaWLtEv6G", "name": "noop",
             "caller": {"type": "direct"}, "input": {}},
        ],
    )


def _resolved_turn() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=[ToolCall(id="toolu_2", name="noop", arguments={})],
        stop="tool_use",
        has_unresolved_server_tool=False,
    )


class _ExecTools(_FakeTools):
    async def execute(self, name, args):
        from auto_research.agents.react import ToolOutcome

        return ToolOutcome(text="ok")


def _steer_positions(sent: list) -> list[int]:
    return [
        i for i, m in enumerate(sent)
        if isinstance(m, ModelMessage) and m.content.startswith("[Human steer]")
    ]


def test_the_hold_decision_reads_the_last_provider_turn():
    """The decision itself, as a unit.

    Our own `ModelMessage`/`ToolResultMessage` turns sit between the provider's turn and the point
    the loop asks, so the scan has to walk back past them to the last `ModelResponse` — reading
    only the final element would see a tool-result turn and always answer False.
    """
    from auto_research.agents.roles import _last_turn_has_unresolved_server_tool as pending

    assert pending([]) is False                       # first iteration: nothing received yet
    assert pending(_seed()) is False                  # only our own turns
    assert pending([*_seed(), _resolved_turn()]) is False
    assert pending([*_seed(), _pending_turn()]) is True
    # ...and still True with our turns stacked on top, which is where the loop actually asks.
    assert pending([
        *_seed(), _pending_turn(), ToolResultMessage(results=[]),
    ]) is True
    # The LAST provider turn decides: a resolved turn after a pending one clears it.
    assert pending([
        *_seed(), _pending_turn(), ToolResultMessage(results=[]), _resolved_turn(),
    ]) is False


def test_a_steer_that_arrives_mid_pending_turn_is_delivered_on_the_next_boundary():
    """Held, not dropped: the delivery moves one iteration later, and happens exactly once."""
    rc = RunControl()
    model = _FakeModel([_pending_turn(), _resolved_turn(), ModelResponse(content="done")])
    role = _bare_role(model, run_control=rc, agent_key="w")

    original_call = model.call
    async def call_and_steer(messages, **kw):
        result = await original_call(messages, **kw)
        if len(model.calls) == 1:          # the steer lands while the pending turn is in flight
            rc.add_steer("w", "look at DU3")
        return result
    model.call = call_and_steer

    asyncio.run(role._react(seed_messages=_seed(), tools=_ExecTools(), action="analyze", cap=5))

    assert not _steer_positions(model.calls[1]), "delivered into the forbidden position"
    assert _steer_positions(model.calls[2]), "held and then never delivered — a steer was lost"
    # Exactly once, across every request that carries it.
    final = model.calls[-1]
    assert sum(1 for m in final if isinstance(m, ModelMessage)
               and m.content.startswith("[Human steer]")) == 1


def test_no_injected_turn_ever_splits_a_tool_use_from_its_tool_result():
    """The structural invariant, over every request built in a normal (unpending) loop.

    The mid-batch drain used to append a user turn immediately after the tool results it had just
    built; there is now one drain, at the top of the iteration, after the hold check. This holds
    for BOTH drains' output, so it does not discriminate the hold rule (the two cells above do) —
    it pins the arrangement so a future third injection site cannot quietly reintroduce the shape.
    """
    rc = RunControl()
    model = _FakeModel([_resolved_turn(), _resolved_turn(), ModelResponse(content="done")])
    role = _bare_role(model, run_control=rc, agent_key="w")

    original_call = model.call
    async def call_and_steer(messages, **kw):
        result = await original_call(messages, **kw)
        rc.add_steer("w", f"steer {len(model.calls)}")
        return result
    model.call = call_and_steer

    asyncio.run(role._react(seed_messages=_seed(), tools=_ExecTools(), action="analyze", cap=6))

    for sent in model.calls:
        for i, message in enumerate(sent):
            if isinstance(message, ModelResponse) and message.tool_calls:
                following = sent[i + 1] if i + 1 < len(sent) else None
                assert following is None or isinstance(following, ToolResultMessage), (
                    f"a {type(following).__name__} was appended between an assistant turn's "
                    "tool calls and their results"
                )


def test_several_held_injections_keep_their_order():
    rc = RunControl()
    model = _FakeModel([_pending_turn(), _resolved_turn(), ModelResponse(content="done")])
    role = _bare_role(model, run_control=rc, agent_key="w")

    original_call = model.call
    async def call_and_steer(messages, **kw):
        result = await original_call(messages, **kw)
        if len(model.calls) == 1:
            rc.add_steer("w", "first")
            rc.add_steer("w", "second")
        return result
    model.call = call_and_steer

    asyncio.run(role._react(seed_messages=_seed(), tools=_ExecTools(), action="analyze", cap=5))

    texts = [
        m.content for m in model.calls[2]
        if isinstance(m, ModelMessage) and m.content.startswith("[Human steer]")
    ]
    assert texts == ["[Human steer] first", "[Human steer] second"]


def test_a_paused_turn_mid_server_tool_is_resent_with_nothing_appended():
    """The third injection site (ruling 13).

    `paused` is the provider's own server-tool loop hitting its iteration limit, and the turn may
    carry a call with no result. The documented continuation is to send the assistant content back
    as-is — so the nudge that normally answers a silent turn must not be written here.
    """
    paused = ModelResponse(content="", stop="paused", has_unresolved_server_tool=True)
    model = _FakeModel([paused, ModelResponse(content="done enough to end the loop" * 3)])
    role = _bare_role(model, agent_key="w")
    role.tools = _FakeTools()

    asyncio.run(
        role._react(
            seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5,
            terminal_tools=frozenset({"turn_end"}),
        )
    )

    sent = model.calls[1]
    assert isinstance(sent[-1], ModelResponse), (
        "a continuation message was appended after a paused turn holding an unresolved "
        "server-tool call; the vendor documents re-sending the response as-is"
    )
    assert sent[-1].stop == "paused"


def test_a_paused_turn_with_no_pending_server_tool_is_still_nudged():
    """The guard is narrow: an ordinary paused turn keeps its continuation message."""
    paused = ModelResponse(content="", stop="paused", has_unresolved_server_tool=False)
    model = _FakeModel([paused, ModelResponse(content="a real answer that ends the loop" * 3)])
    role = _bare_role(model, agent_key="w")

    asyncio.run(
        role._react(
            seed_messages=_seed(), tools=_FakeTools(), action="analyze", cap=5,
            terminal_tools=frozenset({"turn_end"}),
        )
    )

    sent = model.calls[1]
    assert isinstance(sent[-1], ModelMessage) and "paused" in sent[-1].content.lower()


# ═══════════════════════════════════════════ B4: a coder event reaches the lead as a turn


class _StoppedCoder:
    """A provider holding a session whose background drive has finished."""

    def __init__(self, *, error=None, name="ixpe_pcube_bands"):
        from types import SimpleNamespace

        self.authoring_session = SimpleNamespace(
            drive_status="stopped",
            _drive_result=None if error else "wrote the wrapper and its tests",
            _drive_error=error,
            tool_spec=SimpleNamespace(proposed_tool_name=name),
        )

    def tool_definitions(self):
        return [{"name": "noop", "description": "", "input_schema": {"type": "object"}}]

    async def execute(self, name, args):
        from auto_research.agents.react import ToolOutcome

        return ToolOutcome(text="ok")


def _coder_notes(sent) -> list[str]:
    return [
        m.content for m in sent
        if isinstance(m, ModelMessage) and m.content.startswith("[coder]")
    ]


def test_a_stopped_coder_reaches_the_lead_as_its_own_user_turn():
    """The lead does not have to poll: when the coder stops, the loop tells it at the next legal
    boundary — and points at check_coder rather than pasting a transcript into the turn."""
    model = _FakeModel([_resolved_turn(), ModelResponse(content="done")])
    role = _bare_role(model, agent_key="w")

    asyncio.run(
        role._react(seed_messages=_seed(), tools=_StoppedCoder(), action="run_cycle", cap=5)
    )

    notes = [n for sent in model.calls for n in _coder_notes(sent)]
    assert notes, "the coder stopped and the lead was never told"
    assert "check_coder" in notes[0]
    assert "ixpe_pcube_bands" in notes[0], "the notice must name which link stopped"


def test_a_failed_coder_session_reaches_the_lead_as_a_failure():
    """A dead session is not narration. On 2026-09-09 the lead spent three drives steering one."""
    model = _FakeModel([_resolved_turn(), ModelResponse(content="done")])
    role = _bare_role(model, agent_key="w")

    asyncio.run(
        role._react(
            seed_messages=_seed(),
            tools=_StoppedCoder(error="RuntimeError('Credit balance is too low')"),
            action="run_cycle", cap=5,
        )
    )

    notes = [n for sent in model.calls for n in _coder_notes(sent)]
    assert notes and "FAILED" in notes[0]
    assert "Do not keep steering it" in notes[0]


def test_a_coder_event_is_held_by_the_same_rule_as_a_steer():
    """THE reason B depends on A2. A pushed coder event is exactly what makes the injection race
    reachable, so it goes through the same gate — not a second, unguarded path."""
    model = _FakeModel([_pending_turn(), _resolved_turn(), ModelResponse(content="done")])
    role = _bare_role(model, agent_key="w")

    asyncio.run(
        role._react(seed_messages=_seed(), tools=_StoppedCoder(), action="run_cycle", cap=5)
    )

    # Request 2 answers the PENDING turn: nothing but tool results may follow it.
    sent2 = model.calls[1]
    pending_index = next(
        i for i, m in enumerate(sent2)
        if isinstance(m, ModelResponse) and m.has_unresolved_server_tool
    )
    window = []
    for following in sent2[pending_index + 1:]:
        if isinstance(following, ModelResponse):
            break
        window.append(following)
    assert not [m for m in window if isinstance(m, ModelMessage)], (
        "a coder event was injected into the answer to a turn holding an unresolved server-tool "
        "call — B would have reintroduced the 400 that A2 exists to prevent"
    )
    # ...and it is not lost: it lands once the pending call resolves.
    assert [n for sent in model.calls for n in _coder_notes(sent)], "the coder event was dropped"
