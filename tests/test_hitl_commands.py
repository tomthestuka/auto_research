"""Deterministic tests for the channel-agnostic HITL command dispatcher (Stage 3.2, §12)."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from auto_research.hitl.commands import (
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
    out = _run(dispatch_command("/stop@my_bot", ctx))
    assert ctx.run_control.stop_requested and "Stopping now" in out


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
    out = _run(dispatch_command("/stop", ctx))
    assert ctx.run_control.stop_requested and ctx.run_control.stop_event.is_set()
    assert "Stopping now" in out


# ── /status ───────────────────────────────────────────────────────────────────

def test_status_deterministic_digest_from_jsonl(tmp_path):
    jsonl = tmp_path / "monitor.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(e) for e in [
                {"team": "analysts", "event": "analyst.analysis.start", "level": 3, "summary": "started"},
                {"team": "analysts", "event": "chatter", "level": 8, "summary": "noise"},
                {"team": "reviewer", "event": "reviewer.analysis_review.verdict", "level": 2, "summary": "verdict"},
            ]
        ),
        encoding="utf-8",
    )
    out = _run(dispatch_command("/status", _ctx(jsonl=jsonl)))
    assert "3 events" in out
    assert "analyst.analysis.start" in out and "verdict" in out
    assert "chatter" not in out   # level 8 filtered from the significant tail


def test_status_uses_llm_answer_when_wired():
    async def _answer(question: str) -> str:
        return f"LLM says: {question or 'general report'}"

    out = _run(dispatch_command("/status what is the sonnet worker doing?", _ctx(status_answer=_answer)))
    assert out == "LLM says: what is the sonnet worker doing?"


def test_status_no_activity():
    out = _run(dispatch_command("/status", _ctx(jsonl=None)))
    assert "No run activity" in out


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
    assert "Stopping now" in out and "No report is composed" in out


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
    assert still_running and "Stopping now" in reply


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
