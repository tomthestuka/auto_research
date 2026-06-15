"""Deterministic tests for the raise-to-human channel (Phase 2 step 5).

No live LLM / network: connectors use fake Twilio/Slack clients, and handlers are fakes.
"""

import asyncio
import builtins
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from auto_research.agents.react import (
    ReadOnlyToolProvider,
    SandboxToolProvider,
    ask_human_tool_def,
    execute_ask_human,
)
from auto_research.hitl import (
    HumanInputRequired,
    build_human_handler,
    cli_stdin_handler,
    resolve_handler,
    resolve_human,
)
from auto_research.hitl.slack import SlackHandler
from auto_research.hitl.whatsapp import WhatsAppHandler
from auto_research.schemas import HumanQuery, HumanResponse


def _query(**kw):
    base = dict(question="Is the IXPE response file current?", asked_by="analyst_worker")
    base.update(kw)
    return HumanQuery(**base)


# ── schema ──────────────────────────────────────────────────────────────────


def test_humanquery_humanresponse_roundtrip():
    q = _query(kind="clarify_science", context="cyg x-1")
    again = HumanQuery.model_validate(q.model_dump(mode="json"))
    assert again.kind == "clarify_science" and again.asked_by == "analyst_worker"
    r = HumanResponse(query_id=q.id, answer="yes, v13")
    assert HumanResponse.model_validate(r.model_dump(mode="json")).answer == "yes, v13"


# ── resolve_human ─────────────────────────────────────────────────────────────


def test_resolve_human_sync_async_and_str():
    q = _query()

    def sync_resp(query):
        return HumanResponse(query_id=query.id, answer="sync")

    async def async_resp(query):
        return HumanResponse(query_id=query.id, answer="async")

    def str_resp(query):
        return "bare string"

    assert asyncio.run(resolve_human(sync_resp, q)).answer == "sync"
    assert asyncio.run(resolve_human(async_resp, q)).answer == "async"
    assert asyncio.run(resolve_human(str_resp, q)).answer == "bare string"


def test_resolve_human_none_raises_fail_safe():
    with pytest.raises(HumanInputRequired):
        asyncio.run(resolve_human(None, _query()))


def test_resolve_human_bad_return_type_raises():
    with pytest.raises(TypeError):
        asyncio.run(resolve_human(lambda q: 123, _query()))


# ── ask_human tool on the providers ──────────────────────────────────────────


def test_execute_ask_human_builds_query_and_returns_answer():
    seen = {}

    def handler(query):
        seen["kind"] = query.kind
        seen["asked_by"] = query.asked_by
        return HumanResponse(query_id=query.id, answer="the human answer")

    outcome = asyncio.run(
        execute_ask_human(handler, "reviewer", {"question": "ok?", "kind": "clarify_code"})
    )
    assert outcome.text == "the human answer" and outcome.is_error is False
    assert seen == {"kind": "clarify_code", "asked_by": "reviewer"}


def test_execute_ask_human_no_handler_raises():
    with pytest.raises(HumanInputRequired):
        asyncio.run(execute_ask_human(None, "reviewer", {"question": "ok?"}))


def test_readonly_provider_offers_ask_human_only_with_handler(tmp_path):
    no_handler = ReadOnlyToolProvider(roots=[tmp_path])
    assert "ask_human" not in {d["name"] for d in no_handler.tool_definitions()}

    with_handler = ReadOnlyToolProvider(
        roots=[tmp_path], human_handler=lambda q: "hi", asked_by="analyst_lead"
    )
    assert "ask_human" in {d["name"] for d in with_handler.tool_definitions()}
    out = asyncio.run(with_handler.execute("ask_human", {"question": "q?"}))
    assert out.text == "hi"


def test_sandbox_provider_offers_ask_human_only_with_handler():
    registry = SimpleNamespace(tool_definitions=lambda: [{"name": "xspec_fit"}])
    no_handler = SandboxToolProvider(registry, sandbox=None)
    assert "ask_human" not in {d["name"] for d in no_handler.tool_definitions()}

    with_handler = SandboxToolProvider(registry, sandbox=None, human_handler=lambda q: "ok")
    names = {d["name"] for d in with_handler.tool_definitions()}
    assert names == {"xspec_fit", "ask_human"}
    out = asyncio.run(with_handler.execute("ask_human", {"question": "blocked"}))
    assert out.text == "ok"


def test_ask_human_tool_def_shape():
    d = ask_human_tool_def()
    assert d["name"] == "ask_human"
    assert d["input_schema"]["required"] == ["question"]


# ── cli handler ───────────────────────────────────────────────────────────────


def test_cli_stdin_handler_reads_input(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "  use IXPE-first load order  ")
    resp = asyncio.run(cli_stdin_handler(_query()))
    assert resp.answer == "use IXPE-first load order"


# ── resolve_handler auto-detect ──────────────────────────────────────────────


def test_resolve_handler_explicit_handler_wins():
    sentinel = lambda q: "x"  # noqa: E731
    assert resolve_handler(sentinel, "whatsapp") is sentinel


def test_resolve_handler_explicit_channel_none_is_headless():
    assert resolve_handler(None, "none") is None


def test_resolve_handler_auto_tty_is_cli(monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    assert resolve_handler(None, "auto") is cli_stdin_handler


def test_resolve_handler_auto_non_tty_uses_config_default(monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    # Project orchestration.yaml defaults human_channel to "none" → headless.
    assert resolve_handler(None, "auto") is None


def test_build_human_handler_known_and_unknown():
    assert build_human_handler("none") is None
    assert build_human_handler("cli") is cli_stdin_handler
    with pytest.raises(ValueError, match="unknown human channel"):
        build_human_handler("carrier-pigeon")


# ── WhatsApp connector (fake Twilio client) ──────────────────────────────────


class _FakeMessages:
    def __init__(self):
        self.created = []
        self._inbound = []

    def create(self, *, from_, to, body):
        self.created.append({"from_": from_, "to": to, "body": body})
        return SimpleNamespace(sid="SM1")

    def list(self, *, from_, to, date_sent_after, limit=20):
        return list(self._inbound)


class _FakeTwilio:
    def __init__(self):
        self.messages = _FakeMessages()


def test_whatsapp_sends_and_returns_polled_reply():
    client = _FakeTwilio()
    handler = WhatsAppHandler(
        client=client, from_number="+1000", to_number="+1999", poll_interval_s=0.01
    )
    # A human reply lands after the send.
    client.messages._inbound = [
        SimpleNamespace(direction="inbound", body="ObsID 02008401",
                        date_created=datetime.now(UTC) + timedelta(seconds=1))
    ]
    resp = asyncio.run(handler(_query()))
    assert resp.answer == "ObsID 02008401"
    assert client.messages.created[0]["to"] == "whatsapp:+1999"
    assert client.messages.created[0]["from_"] == "whatsapp:+1000"


def test_whatsapp_times_out_without_reply():
    client = _FakeTwilio()
    handler = WhatsAppHandler(
        client=client, from_number="+1000", to_number="+1999",
        timeout_s=0.05, poll_interval_s=0.01,
    )
    with pytest.raises(TimeoutError):
        asyncio.run(handler(_query()))


# ── Slack connector (fake app) ───────────────────────────────────────────────


class _FakeSlackClient:
    def __init__(self):
        self.posted = []

    async def chat_postMessage(self, *, channel, text):
        self.posted.append({"channel": channel, "text": text})
        return {"ts": "t1"}


class _FakeSlackApp:
    def __init__(self):
        self.client = _FakeSlackClient()

    def event(self, _name):  # identity decorator — registration is a no-op here
        def deco(fn):
            return fn
        return deco


def test_slack_posts_and_resolves_on_reply():
    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")

    async def _run():
        task = asyncio.create_task(handler(_query(asked_by="reviewer")))
        for _ in range(200):
            if "t1" in handler._pending:
                break
            await asyncio.sleep(0)
        handler.on_message({"thread_ts": "t1", "text": "answer from human", "user": "U"})
        return await task

    resp = asyncio.run(_run())
    assert resp.answer == "answer from human"
    assert handler.app.client.posted[0]["channel"] == "C0"


def test_slack_on_message_ignores_untracked_and_bot():
    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")
    # No tracked thread / bot echo → no crash, nothing resolved.
    handler.on_message({"thread_ts": "nope", "text": "x"})
    handler.on_message({"bot_id": "B1", "thread_ts": "t1", "text": "x"})
