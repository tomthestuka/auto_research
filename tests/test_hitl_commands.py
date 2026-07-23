"""Deterministic tests for the channel-agnostic HITL command dispatcher (Stage 3.2, §12)."""

from __future__ import annotations

import asyncio
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
    assert ctx.run_control.stop_requested and "Stop requested" in out


# ── /steer ────────────────────────────────────────────────────────────────────

def test_steer_queues_for_named_role():
    ctx = _ctx()
    out = _run(dispatch_command("/steer analyst_worker_0 focus on DU3 and rerun", ctx))
    assert "queued for `analyst_worker_0`" in out
    assert ctx.run_control.drain_steer("analyst_worker_0") == ["focus on DU3 and rerun"]


def test_steer_all_broadcasts_to_every_role():
    ctx = _ctx()
    _run(dispatch_command("/steer all wrap up soon", ctx))
    for _, key, _ in _roster():
        assert ctx.run_control.drain_steer(key) == ["wrap up soon"]


def test_steer_unknown_role_reports_known():
    ctx = _ctx()
    out = _run(dispatch_command("/steer nobody hello", ctx))
    assert "Unknown role" in out and "analyst_lead" in out
    assert ctx.run_control.drain_steer("analyst_lead") == []   # nothing queued


def test_steer_usage_error_on_missing_message():
    out = _run(dispatch_command("/steer analyst_lead", _ctx()))
    assert "Usage:" in out


# ── /stop ─────────────────────────────────────────────────────────────────────

def test_stop_requests_run_control():
    ctx = _ctx()
    out = _run(dispatch_command("/stop", ctx))
    assert ctx.run_control.stop_requested and ctx.run_control.stop_event.is_set()
    assert "Stop requested" in out


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
