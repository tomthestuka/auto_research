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
from auto_research.hitl.telegram import TelegramHandler
from auto_research.hitl.whatsapp import WhatsAppHandler
from auto_research.schemas import HumanQuery, HumanResponse


def _query(**kw):
    base = dict(message="Is the IXPE response file current?", asked_by="analyst_worker")
    base.update(kw)
    return HumanQuery(**base)


# ── schema ──────────────────────────────────────────────────────────────────


def test_humanquery_humanresponse_roundtrip():
    q = _query(kind="clarify_science", message="cyg x-1 response check")
    again = HumanQuery.model_validate(q.model_dump(mode="json"))
    assert again.kind == "clarify_science" and again.asked_by == "analyst_worker"
    assert again.message == "cyg x-1 response check"
    r = HumanResponse(query_id=q.id, answer="yes, v13")
    assert HumanResponse.model_validate(r.model_dump(mode="json")).answer == "yes, v13"


def test_humanquery_headline_is_first_meaningful_line():
    q = _query(message="## Plan\n\n- extend acquire\n- author new\n\nWhich one?")
    assert q.headline == "Plan"
    long = _query(message="x" * 200)
    assert len(long.headline) == 120 and long.headline.endswith("…")


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
        execute_ask_human(handler, "reviewer", {"message": "ok?", "kind": "clarify_code"})
    )
    assert outcome.text == "the human answer" and outcome.is_error is False
    assert seen == {"kind": "clarify_code", "asked_by": "reviewer"}


def test_execute_ask_human_no_handler_raises():
    with pytest.raises(HumanInputRequired):
        asyncio.run(execute_ask_human(None, "reviewer", {"message": "ok?"}))


def test_readonly_provider_offers_ask_human_only_with_handler(tmp_path):
    no_handler = ReadOnlyToolProvider(roots=[tmp_path])
    assert "ask_human" not in {d["name"] for d in no_handler.tool_definitions()}

    with_handler = ReadOnlyToolProvider(
        roots=[tmp_path], human_handler=lambda q: "hi", asked_by="analyst_lead"
    )
    assert "ask_human" in {d["name"] for d in with_handler.tool_definitions()}
    out = asyncio.run(with_handler.execute("ask_human", {"message": "q?"}))
    assert out.text == "hi"


def test_sandbox_provider_offers_ask_human_only_with_handler():
    registry = SimpleNamespace(tool_definitions=lambda: [{"name": "xspec_fit"}])
    no_handler = SandboxToolProvider(registry, sandbox=None)
    assert "ask_human" not in {d["name"] for d in no_handler.tool_definitions()}

    with_handler = SandboxToolProvider(registry, sandbox=None, human_handler=lambda q: "ok")
    names = {d["name"] for d in with_handler.tool_definitions()}
    # data_catalog is always exposed (read-only local-data check); ask_human only with a handler.
    assert names == {"xspec_fit", "data_catalog", "ask_human"}
    out = asyncio.run(with_handler.execute("ask_human", {"message": "blocked"}))
    assert out.text == "ok"


def test_ask_human_tool_def_shape():
    d = ask_human_tool_def()
    assert d["name"] == "ask_human"
    assert d["input_schema"]["required"] == ["message"]


# ── cli handler ───────────────────────────────────────────────────────────────


def test_cli_stdin_handler_reads_input(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "  use IXPE-first load order  ")
    resp = asyncio.run(cli_stdin_handler(_query()))
    assert resp.answer == "use IXPE-first load order"


# ── shared human-response timeout (orchestration.yaml) ───────────────────────


def test_response_timeout_s_default_is_one_hour():
    from auto_research.hitl import response_timeout_s

    assert response_timeout_s() == 3600.0


def test_slack_rotation_log_downgrade_filter():
    """The benign Socket Mode rotation ERROR is rewritten to WARNING; other errors pass through."""
    import logging

    from auto_research.hitl.slack import _SocketRotationDowngradeFilter

    filt = _SocketRotationDowngradeFilter()

    def _record(msg):
        return logging.LogRecord("slack_sdk.socket_mode.aiohttp", logging.ERROR,
                                 __file__, 1, msg, None, None)

    rotation = _record("Failed to check the current session (s1) or reconnect to the server "
                       "(error: ClientConnectionError, message: Cannot write to closing transport)")
    assert filt.filter(rotation) is True
    assert rotation.levelno == logging.WARNING and rotation.levelname == "WARNING"

    genuine = _record("Failed to retrieve WSS URL: boom")
    assert filt.filter(genuine) is True
    assert genuine.levelno == logging.ERROR and genuine.levelname == "ERROR"


def test_channels_default_to_shared_response_timeout(monkeypatch):
    # All channels read the shared window when no explicit timeout_s is passed.
    monkeypatch.setattr("auto_research.hitl.slack.response_timeout_s", lambda: 1234.0)
    monkeypatch.setattr("auto_research.hitl.whatsapp.response_timeout_s", lambda: 1234.0)
    slack = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")
    wa = WhatsAppHandler(client=_FakeTwilio(), from_number="+1", to_number="+2")
    assert slack.timeout_s == 1234.0 and wa.timeout_s == 1234.0
    # An explicit value still overrides the shared default.
    slack2 = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0", timeout_s=7.0)
    assert slack2.timeout_s == 7.0


def test_cli_stdin_handler_times_out(monkeypatch):
    monkeypatch.setattr("auto_research.hitl.cli.response_timeout_s", lambda: 0.05)

    async def _never():
        await asyncio.sleep(10)

    monkeypatch.setattr(asyncio, "to_thread", lambda *a, **k: _never())
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(cli_stdin_handler(_query()))


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


def test_whatsapp_handler_is_deprecated():
    with pytest.warns(DeprecationWarning):
        WhatsAppHandler(client=_FakeTwilio(), from_number="+1", to_number="+2")


def test_whatsapp_body_passes_markdown_message_through():
    handler = WhatsAppHandler(client=_FakeTwilio(), from_number="+1", to_number="+2")
    body = handler._body(_query(message="## Plan\n\n- a\n- b"))
    assert "## Plan" in body and "- a" in body and "(reply to this message)" in body


# ── Slack connector (fake app) ───────────────────────────────────────────────


class _FakeSlackClient:
    def __init__(self):
        self.posted = []

    async def chat_postMessage(self, *, channel, text, blocks=None):
        self.posted.append({"channel": channel, "text": text, "blocks": blocks})
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
    # The notification text carries the headline so a phone push shows the actual ask.
    assert handler.app.client.posted[0]["text"].endswith(_query().headline)


def test_slack_blocks_use_markdown_block_and_chunk():
    from auto_research.hitl.slack import _MARKDOWN_BLOCK_LIMIT, _blocks, _chunk

    q = _query(kind="clarify_science", message="## Plan\n\nbody text\n\n```py\nx=1\n```")
    blocks = _blocks(q)
    assert blocks[0]["type"] == "header"
    md = [b for b in blocks if b["type"] == "markdown"]
    assert len(md) == 1 and md[0]["text"] == q.message.strip()

    big = _query(message="para.\n\n" * 4000)  # well over the per-block limit
    md_big = [b for b in _blocks(big) if b["type"] == "markdown"]
    assert len(md_big) >= 2
    assert all(len(b["text"]) <= _MARKDOWN_BLOCK_LIMIT for b in md_big)


def test_chunk_prefers_paragraph_boundaries():
    from auto_research.hitl.slack import _chunk

    text = "A" * 100 + "\n\n" + "B" * 100
    assert _chunk(text, size=150) == ["A" * 100, "B" * 100]
    # a single run with no break is hard-sliced
    assert _chunk("C" * 250, size=100) == ["C" * 100, "C" * 100, "C" * 50]


def test_slack_on_message_ignores_untracked_and_bot():
    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")
    # No tracked thread / bot echo → no crash, nothing resolved.
    handler.on_message({"thread_ts": "nope", "text": "x"})
    handler.on_message({"bot_id": "B1", "thread_ts": "t1", "text": "x"})


def test_slack_resolves_plain_channel_reply():
    """A human who replies in the channel (no thread_ts) still unblocks the newest question."""
    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")

    async def _run():
        task = asyncio.create_task(handler(_query(asked_by="reviewer")))
        for _ in range(200):
            if "t1" in handler._pending:
                break
            await asyncio.sleep(0)
        # No thread_ts — typed straight into the channel.
        handler.on_message({"channel": "C0", "text": "reply in channel", "user": "U"})
        return await task

    assert asyncio.run(_run()).answer == "reply in channel"


def test_slack_channel_reply_ambiguous_when_multiple_pending():
    """With 2+ open questions, a bare channel reply is ambiguous → ignored; an in-thread reply
    still resolves its own question."""
    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")

    async def _run():
        loop = asyncio.get_event_loop()
        a, b = loop.create_future(), loop.create_future()
        handler._pending["tA"], handler._pending["tB"] = a, b
        handler.on_message({"channel": "C0", "text": "which one?", "user": "U"})  # no thread → ignored
        ambiguous = (a.done(), b.done())
        handler.on_message({"channel": "C0", "thread_ts": "tB", "text": "for B", "user": "U"})  # in-thread
        return ambiguous, b.done(), (b.result() if b.done() else None), a.done()

    ambiguous, b_done, b_val, a_done = asyncio.run(_run())
    assert ambiguous == (False, False)  # bare reply attributed to neither
    assert b_done and b_val == "for B" and not a_done  # in-thread still works, doesn't touch A


def test_slack_on_message_ignores_other_channel_and_subtype():
    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")

    async def _run():
        fut = asyncio.get_event_loop().create_future()
        handler._pending["t1"] = fut
        handler.on_message({"channel": "CXX", "text": "elsewhere", "user": "U"})  # wrong channel
        handler.on_message({"channel": "C0", "subtype": "channel_join", "text": "U joined"})  # not a reply
        return fut.done()

    assert asyncio.run(_run()) is False


# ── Telegram connector (fake Application/bot) ────────────────────────────────


class _FakeTGBot:
    def __init__(self):
        self.sent = []
        self._id = 100

    async def send_message(self, *, chat_id, text, parse_mode=None):
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
        return SimpleNamespace(message_id=self._id)


async def _tg_anoop(*a, **k):
    return None


class _FakeTGApp:
    def __init__(self):
        self.bot = _FakeTGBot()
        self.handlers = []
        self.updater = SimpleNamespace(start_polling=_tg_anoop, stop=_tg_anoop)

    def add_handler(self, h):
        self.handlers.append(h)

    async def initialize(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def shutdown(self):
        pass


def _tg_update(text, chat_id="555", reply_to_id=None, is_bot=False):
    msg = SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(is_bot=is_bot),
        reply_to_message=(SimpleNamespace(message_id=reply_to_id) if reply_to_id is not None else None),
    )
    return SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=chat_id), message=msg)


def test_telegram_posts_html_and_resolves_on_reply():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")

    async def _run():
        task = asyncio.create_task(handler(_query(asked_by="reviewer")))
        for _ in range(200):
            if handler._pending:
                break
            await asyncio.sleep(0)
        mid = next(iter(handler._pending))
        handler.on_message(_tg_update("answer from human", reply_to_id=mid))
        return await task

    resp = asyncio.run(_run())
    assert resp.answer == "answer from human"
    assert handler.app.bot.sent[0]["chat_id"] == "555"
    assert handler.app.bot.sent[0]["parse_mode"] == "HTML"


def test_telegram_routes_concurrent_questions_by_reply_to():
    """Two questions open at once → a reply-to each message resolves the right one (multi-question)."""
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")

    async def _run():
        t1 = asyncio.create_task(handler(_query(asked_by="w1")))
        while len(handler._pending) < 1:
            await asyncio.sleep(0)
        id1 = next(iter(handler._pending))
        t2 = asyncio.create_task(handler(_query(asked_by="w2")))
        while len(handler._pending) < 2:
            await asyncio.sleep(0)
        id2 = next(i for i in handler._pending if i != id1)
        handler.on_message(_tg_update("answer-2", reply_to_id=id2))
        handler.on_message(_tg_update("answer-1", reply_to_id=id1))
        return await asyncio.gather(t1, t2)

    r1, r2 = asyncio.run(_run())
    assert r1.answer == "answer-1" and r2.answer == "answer-2"


def test_telegram_plain_reply_resolves_single_open():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")

    async def _run():
        task = asyncio.create_task(handler(_query()))
        while not handler._pending:
            await asyncio.sleep(0)
        handler.on_message(_tg_update("plain answer", reply_to_id=None))
        return await task

    assert asyncio.run(_run()).answer == "plain answer"


def test_telegram_on_message_ignores_other_chat_bot_empty():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")

    async def _run():
        task = asyncio.create_task(handler(_query()))
        while not handler._pending:
            await asyncio.sleep(0)
        mid = next(iter(handler._pending))
        handler.on_message(_tg_update("x", chat_id="999", reply_to_id=mid))  # wrong chat
        handler.on_message(_tg_update("x", reply_to_id=mid, is_bot=True))  # our own echo
        handler.on_message(_tg_update("   ", reply_to_id=mid))  # empty
        await asyncio.sleep(0)
        assert all(not f.done() for f in handler._pending.values())
        handler.on_message(_tg_update("real", reply_to_id=mid))  # valid → resolves
        return await task

    assert asyncio.run(_run()).answer == "real"


def test_telegram_times_out_without_reply():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555", timeout_s=0.05)
    with pytest.raises(TimeoutError):
        asyncio.run(handler(_query()))


def test_telegram_uses_shared_response_timeout(monkeypatch):
    monkeypatch.setattr("auto_research.hitl.telegram.response_timeout_s", lambda: 1234.0)
    assert TelegramHandler(app=_FakeTGApp(), chat_id="555").timeout_s == 1234.0
    assert TelegramHandler(app=_FakeTGApp(), chat_id="555", timeout_s=7.0).timeout_s == 7.0


def test_build_human_handler_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "555")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    assert isinstance(build_human_handler("telegram"), TelegramHandler)


def test_md_to_html_renders_telegram_subset():
    from auto_research.hitl.telegram import _md_to_html

    out = _md_to_html("# Title\n\n**bold** and *it* and `x<y` and [t](https://e.com)\n- one\n- two")
    assert "<b>Title</b>" in out  # heading → bold
    assert "<b>bold</b>" in out and "<i>it</i>" in out
    assert "<code>x&lt;y</code>" in out  # inline code, escaped
    assert '<a href="https://e.com">t</a>' in out
    assert "• one" in out and "• two" in out  # bullets
    # fenced code: content escaped, wrapped in <pre>
    assert "<pre>a &lt; b &amp; c\n</pre>" in _md_to_html("```py\na < b & c\n```")
    # raw prose angle brackets are escaped, never emitted as tags
    assert "&lt;" in _md_to_html("a < b") and "<b>" not in _md_to_html("a < b")


def test_telegram_render_chunks_header_hint_and_split():
    from auto_research.hitl.telegram import _TG_MSG_LIMIT, _render_chunks

    chunks = _render_chunks(_query(kind="clarify_science", message="para.\n\n" * 4000))
    assert chunks[0].startswith("<b>Human input needed · clarify_science</b>")
    assert chunks[-1].endswith("<i>Reply to this message to answer.</i>")
    assert len(chunks) >= 2 and all(len(c) <= _TG_MSG_LIMIT for c in chunks)
