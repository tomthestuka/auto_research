"""Deterministic tests for the channel-agnostic HITL command dispatcher (Stage 3.2, §12)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from auto_research.hitl.commands import (
    STOP_ACK,
    CommandContext,
    dispatch_command,
    wait_reply_or_stop,
)
from auto_research.run_control import STOP_SENTINEL, RunControl


def _roster():
    return [
        ("reviewer", "reviewer", "claude-fable-5"),
        ("analyst", "analyst_lead", "claude-opus-4-8"),
        ("analyst", "analyst_worker_0", "claude-sonnet-5"),
    ]


def _ctx(tmp_path=None, jsonl=None, status_answer=None) -> CommandContext:
    return CommandContext(
        run_control=RunControl(),
        roster=_roster,
        monitor_jsonl_path=jsonl,
        status_answer=status_answer,
    )


def _run(coro):
    return asyncio.run(coro)


def _stop_in_a_run(text: str, ctx: CommandContext) -> str:
    """Dispatch a /stop while a run task is registered, as it is during a run."""

    async def scenario():
        run = asyncio.create_task(asyncio.sleep(3600))
        ctx.run_control.register_run_task(run)
        try:
            return await dispatch_command(text, ctx)
        finally:
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run

    return asyncio.run(scenario())


# ── /help ─────────────────────────────────────────────────────────────────────

def test_help_lists_commands_and_roster():
    out = _run(dispatch_command("/help", _ctx()))
    assert "/steer" in out and "/status" in out and "/stop" in out
    assert "analyst_worker_0" in out and "claude-sonnet-5" in out   # roster shown


def test_unknown_command_falls_back_to_help():
    out = _run(dispatch_command("/frobnicate x", _ctx()))
    assert "Unknown command" in out and "/help" in out


def test_botname_suffix_tolerated():
    ctx = _ctx()
    out = _stop_in_a_run("/stop@my_bot", ctx)
    assert ctx.run_control.stop_requested and STOP_ACK in out


# ── /steer ────────────────────────────────────────────────────────────────────

def test_steer_queues_for_a_roster_id():
    """Targeting is by the id `/help` prints (3 = analyst_worker_0 in this roster)."""
    ctx = _ctx()
    out = _run(dispatch_command("/steer 3 focus on DU3 and rerun", ctx))
    assert "queued" in out and "`analyst_worker_0`" in out and "id `3`" in out
    assert ctx.run_control.drain_steer("analyst_worker_0") == ["focus on DU3 and rerun"]


def test_steer_ack_says_queued_not_delivered():
    """The ack must not imply arrival: delivery waits for the role's next step."""
    out = _run(dispatch_command("/steer 2 wrap up", _ctx()))
    assert "**queued**" in out and "next step" in out


def test_steer_rejects_a_role_name_and_names_its_id():
    """A name no longer targets, and the rejection tells the human which id to use instead."""
    ctx = _ctx()
    out = _run(dispatch_command("/steer analyst_worker_0 hello", ctx))
    assert "takes a roster **id**" in out and "is id `3`" in out
    assert ctx.run_control.drain_steer("analyst_worker_0") == []  # nothing queued on a guess


def test_steer_rejects_an_out_of_range_id_with_the_roster():
    ctx = _ctx()
    out = _run(dispatch_command("/steer 99 hello", ctx))
    assert "No role with id `99`" in out and "analyst_lead" in out
    assert ctx.run_control.drain_steer("analyst_lead") == []


def test_help_numbers_the_roster():
    out = _run(dispatch_command("/help", _ctx()))
    assert "`1` · `reviewer`" in out and "`3` · `analyst`" in out
    assert "/steer <id> <message>" in out


def test_steer_all_broadcasts_to_every_role():
    ctx = _ctx()
    _run(dispatch_command("/steer all wrap up soon", ctx))
    for _, key, _ in _roster():
        assert ctx.run_control.drain_steer(key) == ["wrap up soon"]


def test_steer_usage_error_on_missing_message():
    out = _run(dispatch_command("/steer 2", _ctx()))
    assert "Usage:" in out and "⚠️" in out


def test_unknown_command_is_rejected_immediately_with_the_command_list():
    out = _run(dispatch_command("/frobnicate now", _ctx()))
    assert "⚠️" in out and "Unknown command `/frobnicate`" in out and "/steer" in out


def test_commands_are_recorded_in_the_run_ledger():
    """A rejected command, a queued steer and a delivered steer must be distinguishable later."""
    from auto_research.monitor import RunMonitor

    monitor = RunMonitor(chatter=10)
    ctx = _ctx()
    ctx.monitor = monitor

    _run(dispatch_command("/steer 3 do the thing", ctx))
    _run(dispatch_command("/steer nobody hello", ctx))

    events = [(e.event, e.data.get("command"), e.data.get("accepted")) for e in monitor.events]
    assert ("hitl.command", "steer", None) in events                 # received
    assert ("hitl.command.reply", "steer", True) in events           # accepted
    assert ("hitl.command.reply", "steer", False) in events          # rejected
    assert all(e.actor == "human" for e in monitor.events)


# ── /stop ─────────────────────────────────────────────────────────────────────

def test_stop_requests_run_control():
    ctx = _ctx()
    out = _stop_in_a_run("/stop", ctx)
    assert ctx.run_control.stop_requested and ctx.run_control.stop_event.is_set()
    assert STOP_ACK in out


def test_stop_with_no_run_in_flight_promises_no_cleanup():
    """Nothing is running (the graph finished, or none started): the stop is still flagged, so a
    reply-wait unblocks, but the human is not told a checklist is coming."""
    ctx = _ctx()
    out = _run(dispatch_command("/stop", ctx))
    assert ctx.run_control.stop_requested and "nothing to clean up" in out
    assert STOP_ACK not in out


def test_a_second_stop_says_the_run_is_already_stopping():
    ctx = _ctx()
    _stop_in_a_run("/stop", ctx)
    out = _stop_in_a_run("/stop", ctx)
    assert "already stopping" in out and STOP_ACK not in out


# ── /status ───────────────────────────────────────────────────────────────────

def test_status_is_answered_by_the_reporter():
    """`/status` has one path now: the `status_monitor` reporter reading the run's ledger. The
    deterministic digest is gone — it dropped every event's payload, which is where the answers
    to real questions live, and the role is required at preflight so it is always wired."""

    async def _answer(question: str) -> str:
        return f"reporter says: {question or 'general report'}"

    out = _run(dispatch_command("/status what is the sonnet worker doing?",
                                _ctx(status_answer=_answer)))
    assert out == "reporter says: what is the sonnet worker doing?"


def test_status_surfaces_a_reporter_failure_rather_than_a_lesser_view():
    """No fallback digest: provider errors are already retried at the model layer, and a run whose
    provider is down cannot advance — a second view of the same log would report on a stalled run
    as if it were fine."""

    async def _boom(question: str) -> str:
        raise RuntimeError("provider unavailable")

    # The failure is the answer (D8: the human was told the monitor is investigating, so it must
    # hear how that ended), never a second view of the log.
    reply = _run(dispatch_command("/status", _ctx(status_answer=_boom)))
    assert "could not answer" in reply and "provider unavailable" in reply


# ── wait_reply_or_stop ─────────────────────────────────────────────────────────

def test_wait_returns_reply():
    async def go():
        rc = RunControl()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        loop.call_soon(fut.set_result, "the human reply")
        return await wait_reply_or_stop(fut, rc, timeout=5)

    assert _run(go()) == "the human reply"


def test_wait_returns_stop_sentinel_on_stop():
    async def go():
        rc = RunControl()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()   # never resolves
        loop.call_soon(rc.request_stop, "human stop")
        return await wait_reply_or_stop(fut, rc, timeout=5)

    assert _run(go()) == STOP_SENTINEL


def test_wait_times_out():
    async def go():
        rc = RunControl()
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        await wait_reply_or_stop(fut, rc, timeout=0.05)

    with pytest.raises(TimeoutError):
        _run(go())


def test_wait_without_run_control_is_plain_wait_for():
    async def go():
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        loop.call_soon(fut.set_result, "reply")
        return await wait_reply_or_stop(fut, None, timeout=5)

    assert _run(go()) == "reply"


def test_stop_cancels_the_run_task():
    """`/stop` is Ctrl-C: it cancels the run task rather than asking the pipeline to wind down."""

    async def scenario():
        ctx = _ctx()
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def never_ends():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(never_ends())
        await started.wait()
        ctx.run_control.register_run_task(task)

        out = await dispatch_command("/stop", ctx)

        with pytest.raises(asyncio.CancelledError):
            await task
        return out, cancelled.is_set()

    out, was_cancelled = asyncio.run(scenario())
    assert was_cancelled
    assert STOP_ACK in out and "No report is composed" in out


def test_stop_replies_before_the_cancellation_lands():
    """The ack must get out before cleanup closes the connector, so the cancel is scheduled
    (call_soon) rather than delivered inside the command handler."""

    async def scenario():
        ctx = _ctx()
        task = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        ctx.run_control.register_run_task(task)
        reply = await dispatch_command("/stop", ctx)
        still_running = not task.done()  # not cancelled yet: the reply came first
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return reply, still_running

    reply, still_running = asyncio.run(scenario())
    assert still_running and STOP_ACK in reply


async def _busy_aborter() -> bool:
    """An aborter reporting the role was mid-call — still used by the ⚡ deliver-now button,
    which cancels one role's in-flight tool call so a queued steer lands at once."""
    return True


def test_stop_without_a_registered_task_still_flags_the_run():
    """A duck-typed/partial wiring must not make /stop a no-op silently."""
    ctx = _ctx()
    _run(dispatch_command("/stop", ctx))
    assert ctx.run_control.stop_requested


# ── buttons on the steer ack (the two escalations, one tap each) ────────────────────────────────


def _btn_ctx(run_id="run-a"):
    ctx = CommandContext(run_control=RunControl(), roster=_roster, run_id=run_id)
    return ctx


def test_steer_ack_carries_deliver_now_and_clear_buttons():
    ctx = _btn_ctx()
    reply = _run(dispatch_command("/steer 3 stop using locked data", ctx))
    labels = [label for label, _ in reply.buttons]
    assert labels == ["⚡ Deliver now", "🗑 Clear"]
    # Payload carries only the action + target + the two staleness fields — the message is
    # already in the inbox, so there is nothing to store and no token to expire.
    assert reply.buttons[0][1].startswith("steer:now:3:run-a:")
    assert reply.buttons[1][1].startswith("steer:clear:3:run-a:")


def test_reply_is_a_plain_string_for_connectors_without_buttons():
    """A str subclass, so a connector that only sends text is untouched."""
    reply = _run(dispatch_command("/steer 3 hello", _btn_ctx()))
    assert isinstance(reply, str) and "queued" in reply


def test_deliver_now_button_cancels_the_in_flight_call():
    from auto_research.hitl.commands import dispatch_callback

    ctx = _btn_ctx()
    ctx.run_control.register_aborter("analyst_worker_0", _busy_aborter)
    reply = _run(dispatch_command("/steer 3 stop that", ctx))

    out = _run(dispatch_callback(reply.buttons[0][1], ctx))

    assert "Forcing delivery" in out and "cancelled its in-flight tool call" in out
    # The steer itself is still queued — cancelling only removes the thing blocking delivery.
    assert ctx.run_control.drain_steer("analyst_worker_0") == ["stop that"]


def test_clear_button_drops_the_queued_steer():
    from auto_research.hitl.commands import dispatch_callback

    ctx = _btn_ctx()
    reply = _run(dispatch_command("/steer 3 never mind", ctx))
    out = _run(dispatch_callback(reply.buttons[1][1], ctx))
    assert "Cleared 1 queued steer" in out
    assert ctx.run_control.drain_steer("analyst_worker_0") == []


def test_button_from_another_run_is_refused():
    from auto_research.hitl.commands import dispatch_callback

    reply = _run(dispatch_command("/steer 3 hi", _btn_ctx(run_id="run-a")))
    out = _run(dispatch_callback(reply.buttons[0][1], _btn_ctx(run_id="run-b")))
    assert "belongs to run `run-a`" in out


def test_button_from_an_earlier_session_of_the_same_run_is_refused():
    """run_id is stable across a resume, so only a per-process nonce can catch this."""
    from auto_research.hitl.commands import dispatch_callback

    ctx = _btn_ctx()
    out = _run(dispatch_callback("steer:now:3:run-a:deadbe", ctx))   # foreign boot nonce
    assert "earlier session of this run" in out


def test_malformed_button_payload_is_refused():
    from auto_research.hitl.commands import dispatch_callback

    assert "Unrecognized button" in _run(dispatch_callback("nonsense", _btn_ctx()))


# ── ruling D8: answer at once, run commands side by side ─────────────────────────────────────


def test_stop_acknowledges_before_the_run_is_cancelled():
    from auto_research.hitl.commands import dispatch_command

    async def scenario():
        ctx = _ctx()
        run = asyncio.create_task(asyncio.sleep(3600))
        ctx.run_control.register_run_task(run)
        seen = []

        async def ack(text):
            seen.append((text, ctx.run_control.stop_requested))

        reply = await dispatch_command("/stop", ctx, ack=ack)
        await asyncio.sleep(0)
        cancelled = run.cancelled() or run.cancelling() > 0
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run
        return seen, reply, cancelled

    seen, reply, cancelled = asyncio.run(scenario())
    assert seen == [(STOP_ACK, False)]  # answered before anything was stopped
    assert reply == "" and cancelled  # the outcome is the landing checklist, not a reply


def test_status_acknowledges_before_the_monitor_answers():
    from auto_research.hitl.commands import STATUS_ACK, dispatch_command

    order = []

    async def answer(question):
        order.append("answer")
        return "all quiet"

    async def ack(text):
        order.append(text)

    ctx = _ctx()
    ctx.status_answer = answer
    reply = asyncio.run(dispatch_command("/status", ctx, ack=ack))
    assert order == [STATUS_ACK, "answer"] and reply == "all quiet"


def test_telegram_stop_never_waits_behind_a_slow_status():
    """The library runs handlers one at a time; each command runs in its own task, so `/stop`
    lands while `/status` still waits on its model."""
    from auto_research.hitl.commands import STATUS_ACK
    from auto_research.hitl.telegram import TelegramHandler
    from tests.test_hitl import _FakeTGApp, _tg_update

    async def scenario():
        handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
        ctx = _ctx()
        run = asyncio.create_task(asyncio.sleep(3600))
        ctx.run_control.register_run_task(run)
        release = asyncio.Event()

        async def slow_answer(question):
            await release.wait()
            return "the monitor's answer"

        ctx.status_answer = slow_answer
        handler.attach_commands(ctx)
        await handler._on_command(_tg_update("/status"))
        await handler._on_command(_tg_update("/stop"))
        for _ in range(50):
            await asyncio.sleep(0)
        stopped_while_status_pending = ctx.run_control.stop_requested and not release.is_set()
        sent_before = [m["text"] for m in handler.app.bot.sent]
        release.set()
        for _ in range(50):
            await asyncio.sleep(0)
        run.cancel()
        return stopped_while_status_pending, sent_before, [m["text"] for m in handler.app.bot.sent]

    stopped, before, after = asyncio.run(scenario())
    assert stopped
    assert any(STATUS_ACK in t for t in before) and any(STOP_ACK in t for t in before)
    assert any("the monitor's answer" in t for t in after)
