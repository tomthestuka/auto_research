"""Deterministic tests for the raise-to-human channel (Phase 2 step 5).

No live LLM / network: connectors use fake Twilio/Slack clients, and handlers are fakes.
"""

import asyncio
import builtins
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from auto_research.agents.react import ReadOnlyToolProvider, SandboxToolProvider
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


# ── the providers carry no human tool: the seat's channel layer adds `message` (E12) ────────


def test_providers_declare_no_human_tool(tmp_path):
    readonly = {d["name"] for d in ReadOnlyToolProvider(roots=[tmp_path]).tool_definitions()}
    registry = SimpleNamespace(tool_definitions=lambda: [{"name": "xspec_fit"}])
    sandbox = {d["name"] for d in SandboxToolProvider(registry, sandbox=None).tool_definitions()}
    for names in (readonly, sandbox):
        assert not names & {"ask_human", "plan_and_debate", "message"}
    assert sandbox == {"xspec_fit", "read_file"}


# ── cli handler ───────────────────────────────────────────────────────────────


def test_cli_stdin_handler_reads_input(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda *a, **k: "  use IXPE-first load order  ")
    resp = asyncio.run(cli_stdin_handler(_query()))
    assert resp.answer == "use IXPE-first load order"


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
    """A prompt nobody answers times out — and leaves nothing that can hold the process open.

    The stimulus is a genuinely blocked `input()`, not a stubbed coroutine: the defect this
    guards was never in the timeout (which always fired) but in what survived it. A reader on the
    default executor is non-daemon and is joined at interpreter exit with no timeout
    (bpo-39812), so the run ended and the process did not (measured 2026-09-12).
    """
    import builtins
    import threading

    monkeypatch.setattr("auto_research.hitl.cli.response_timeout_s", lambda: 0.05)
    release = threading.Event()
    monkeypatch.setattr(builtins, "input", lambda *a, **k: (release.wait(30), "late")[1])

    before = {t.ident for t in threading.enumerate() if t.name == "hitl-stdin"}
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(cli_stdin_handler(_query()))

    stranded = [t for t in threading.enumerate() if t.name == "hitl-stdin" and t.ident not in before]
    assert stranded, "the read did not run on its own named thread"
    assert all(t.daemon for t in stranded), (
        "the stdin read is on a non-daemon thread; an unanswered prompt would hang the process at "
        "exit even though the timeout returned on time"
    )
    # A human typing after the timeout must land on a no-op, not an InvalidStateError on the
    # future `wait_for` already cancelled.
    release.set()
    for thread in stranded:
        thread.join(timeout=5.0)
        assert not thread.is_alive()


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

    async def send_message(self, *, chat_id, text, parse_mode=None, reply_markup=None,
                           disable_notification=None):
        # Shape-complete with the real bot: `reply_markup` is how an inline keyboard rides along,
        # and a fake that omitted it hid the button path from the suite entirely.
        self._id += 1
        self.sent.append({
            "chat_id": chat_id, "text": text, "parse_mode": parse_mode,
            "reply_markup": reply_markup,
        })
        return SimpleNamespace(message_id=self._id)

    async def set_my_commands(self, commands):
        self.menu = list(commands)


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


def test_telegram_reply_during_send_is_not_dropped():
    """A reply-to arriving mid-send (chunk posted, later chunks still going out) resolves the
    query — the registration-after-each-send fix ([E4]); the old register-after-all ordering
    left ``_pending`` empty during the sends and dropped the reply, hanging to the timeout."""
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555", timeout_s=0.5)

    first_id: list[int] = []
    orig_send = handler.app.bot.send_message

    async def _send_then_reply_on_second(*, chat_id, text, parse_mode=None):
        posted = await orig_send(chat_id=chat_id, text=text, parse_mode=parse_mode)
        if not first_id:
            first_id.append(posted.message_id)
        else:
            # second send: a human replies-to the first message before this send returns
            handler.on_message(_tg_update("mid-send answer", reply_to_id=first_id[0]))
        return posted

    handler.app.bot.send_message = _send_then_reply_on_second

    async def _run():
        # a >_CHUNK body forces at least two messages, so there is a genuine mid-send window
        long_md = "para one.\n\n" + ("word " * 800) + "\n\npara two."
        return await handler(_query(asked_by="reviewer", message=long_md))

    resp = asyncio.run(_run())
    assert resp.answer == "mid-send answer"
    assert len(handler.app.bot.sent) >= 2  # actually chunked


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


def test_telegram_plain_reply_resolves_chunked_single_open():
    """A plain reply resolves a single open query even when it was CHUNKED into several messages.
    Regression: `_pending` maps every chunk's message_id to the SAME Future, so counting entries
    (not distinct Futures) made one multi-chunk question look like "many open" and dropped the
    plain reply — forcing reply-to (seen live: 4 plain sends ignored on a 2-chunk design review)."""
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    long_md = "para one.\n\n" + ("word " * 800) + "\n\npara two."  # > _CHUNK ⇒ multiple messages

    async def _run():
        task = asyncio.create_task(handler(_query(message=long_md)))
        while len(handler._pending) < 2:  # wait until it has actually chunked into ≥2 messages
            await asyncio.sleep(0)
        handler.on_message(_tg_update("plain answer to chunked", reply_to_id=None))
        return await task

    assert asyncio.run(_run()).answer == "plain answer to chunked"


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
    # blockquote: a leading '>' (escaped to '&gt;' by the escape pass) → <blockquote>
    assert _md_to_html("> quoted line") == "<blockquote>quoted line</blockquote>"
    # a mid-line '>' (a comparison) stays escaped, not a blockquote
    assert _md_to_html("a > b") == "a &gt; b"


def test_telegram_render_chunks_header_hint_and_split():
    from auto_research.hitl.telegram import _TG_MSG_LIMIT, _render_chunks

    chunks = _render_chunks(_query(kind="clarify_science", message="para.\n\n" * 4000))
    assert chunks[0].startswith("<b>Human input needed</b> · <code>clarify_science</code>")
    assert chunks[-1].endswith("<i>Reply to this message to answer.</i>")
    assert len(chunks) >= 2 and all(len(c) <= _TG_MSG_LIMIT for c in chunks)


# ── HITL slash-commands over the connectors (§12 Stage 3) ──────────────────────

def _cmd_ctx(rc=None):
    from auto_research.hitl.commands import CommandContext
    from auto_research.run_control import RunControl

    return CommandContext(
        run_control=rc or RunControl(),
        roster=lambda: [
            ("analyst", "analyst_lead", "claude-opus-4-8"),
            ("analyst", "analyst_worker_0", "claude-sonnet-5"),
        ],
    )


def test_telegram_command_steer_dispatch():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    ctx = _cmd_ctx()
    handler.attach_commands(ctx)
    # id 1 = analyst_lead in this roster (targeting is by the id /help prints)
    asyncio.run(handler.handle_command(_tg_update("/steer 1 prioritize DU3")))
    assert ctx.run_control.drain_steer("analyst_lead") == ["prioritize DU3"]
    assert handler.app.bot.sent and "queued" in handler.app.bot.sent[-1]["text"].lower()


def test_telegram_command_ignores_other_chat():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    ctx = _cmd_ctx()
    handler.attach_commands(ctx)
    asyncio.run(handler.handle_command(_tg_update("/stop", chat_id="999")))  # not our chat
    assert not ctx.run_control.stop_requested


def test_telegram_stop_unblocks_pending_ask():
    from auto_research.run_control import STOP_SENTINEL

    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    ctx = _cmd_ctx()
    handler.attach_commands(ctx)

    async def _run():
        task = asyncio.create_task(handler(_query(asked_by="reviewer")))
        for _ in range(200):
            if handler._pending:
                break
            await asyncio.sleep(0)
        await handler.handle_command(_tg_update("/stop"))
        return await task

    resp = asyncio.run(_run())
    assert resp.answer == STOP_SENTINEL and ctx.run_control.stop_requested


def test_slack_command_steer_and_stop_unblocks():
    from auto_research.run_control import STOP_SENTINEL

    handler = SlackHandler(app=_FakeSlackApp(), socket_handler=None, channel="C0")
    ctx = _cmd_ctx()
    handler.attach_commands(ctx)
    asyncio.run(handler.handle_command({"channel": "C0", "text": "/steer 2 focus"}))  # id 2
    assert ctx.run_control.drain_steer("analyst_worker_0") == ["focus"]
    assert handler.app.client.posted and "queued" in handler.app.client.posted[-1]["text"].lower()

    async def _run():
        task = asyncio.create_task(handler(_query(asked_by="reviewer")))
        for _ in range(200):
            if handler._pending:
                break
            await asyncio.sleep(0)
        await handler.handle_command({"channel": "C0", "text": "/stop"})
        return await task

    resp = asyncio.run(_run())
    assert resp.answer == STOP_SENTINEL


# ── buttons: a tap is a third channel, distinct from a reply and from a command ────────────────


def test_telegram_steer_ack_attaches_buttons_and_a_tap_acts():
    """A tap sends the payload back without posting a chat message, so it can never be read as a
    reply to an agent — and it needs no typing, which is the whole point on a phone."""
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    ctx = _cmd_ctx()
    ctx.run_id = "run-a"
    handler.attach_commands(ctx)

    asyncio.run(handler.handle_command(_tg_update("/steer 1 stop using locked data")))
    sent = handler.app.bot.sent[-1]
    keyboard = sent["reply_markup"].inline_keyboard[0]
    assert [b.text for b in keyboard] == ["⚡ Deliver now", "🗑 Clear"]
    assert keyboard[0].callback_data.startswith("steer:now:1:run-a:")

    # Drive the callback with the payload the ack would have carried.
    from auto_research.hitl.commands import dispatch_command

    reply = asyncio.run(dispatch_command("/steer 1 again", ctx))
    tapped = SimpleNamespace(
        callback_query=SimpleNamespace(
            data=reply.buttons[1][1],                  # 🗑 Clear
            answer=_noop_answer,
        )
    )
    asyncio.run(handler.handle_callback(tapped))
    assert "Cleared" in handler.app.bot.sent[-1]["text"]
    assert ctx.run_control.drain_steer("analyst_lead") == []   # both steers dropped


def test_telegram_publishes_the_command_menu():
    """Telegram's own discovery mechanism: a registered command autocompletes in the client."""
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    asyncio.run(handler._publish_command_menu())
    names = [c.command for c in handler.app.bot.menu]
    assert names == ["help", "status", "steer", "stop"]
    assert all(c.description for c in handler.app.bot.menu)


async def _noop_answer(*a, **k):
    return None


# ── Telegram rich messages + fallback rendering (E8, 2026-09-25) ─────────────


class _FakeRichBot(_FakeTGBot):
    """A bot that also speaks `sendRichMessage` through `do_api_request`, optionally refusing."""

    def __init__(self, refuse: bool = False):
        super().__init__()
        self.rich = []
        self.refuse = refuse

    async def do_api_request(self, endpoint, api_kwargs=None, return_type=None, **_):
        from telegram.error import BadRequest

        assert endpoint == "sendRichMessage"
        if self.refuse:
            raise BadRequest("can't parse rich message")
        self._id += 1
        self.rich.append(api_kwargs)
        return SimpleNamespace(message_id=self._id)


def _rich_handler(refuse=False):
    app = _FakeTGApp()
    app.bot = _FakeRichBot(refuse=refuse)
    return TelegramHandler(app=app, chat_id="555")


_TABLE_MD = "## Bands\n\n| Band | PD |\n|---|---:|\n| 2-3 keV | 3.5 |\n| 3-4 keV | 3.5 |\n\n$$E = mc^2$$"


def test_telegram_sends_a_query_as_one_rich_message_with_markdown_intact():
    handler = _rich_handler()

    async def _run():
        task = asyncio.create_task(handler(_query(message=_TABLE_MD)))
        for _ in range(200):
            if handler._pending:
                break
            await asyncio.sleep(0)
        mid = next(iter(handler._pending))
        handler.on_message(_tg_update("ok", reply_to_id=mid))
        return await task

    assert asyncio.run(_run()).answer == "ok"
    (sent,) = handler.app.bot.rich
    md = sent["rich_message"]["markdown"]
    assert "| 2-3 keV | 3.5 |" in md and "$$E = mc^2$$" in md  # the table and math travel natively
    assert handler.app.bot.sent == []  # no HTML fallback message


def test_telegram_refused_rich_message_falls_back_to_html():
    handler = _rich_handler(refuse=True)
    asyncio.run(handler._send_md(_TABLE_MD))
    (html_msg,) = handler.app.bot.sent
    assert html_msg["parse_mode"] == "HTML" and "<pre>" in html_msg["text"]
    assert handler._rich  # a content refusal does not disable rich messages for later sends


def test_telegram_without_the_rich_method_stops_trying_it():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")  # the fake bot has no do_api_request
    asyncio.run(handler._send_md("hello"))
    assert handler._rich is False and len(handler.app.bot.sent) == 1


def test_telegram_rich_messages_follow_the_config_flag(monkeypatch):
    monkeypatch.setattr("auto_research.hitl.telegram._rich_messages_enabled", lambda: False)
    handler = _rich_handler()
    asyncio.run(handler._send_md(_TABLE_MD))
    assert handler.app.bot.rich == [] and len(handler.app.bot.sent) == 1


def test_chunk_md_never_ends_a_chunk_on_a_heading_and_reopens_fences():
    from auto_research.hitl.telegram import _chunk_md

    doc = ("## Section\n\n" + "a sentence of text\n" * 40 + "\n") * 20
    doc += "```\n" + "code line\n" * 600 + "```"
    chunks = _chunk_md(doc, 1000)
    for chunk in chunks:
        assert len(chunk) <= 1000
        assert not chunk.rstrip().split("\n")[-1].lstrip().startswith("#")
        assert chunk.count("```") % 2 == 0  # every fenced piece is closed in its own chunk


def test_md_to_html_turns_a_narrow_table_into_pre_and_a_wide_one_into_bullets():
    from auto_research.hitl.telegram import _md_to_html

    narrow = _md_to_html("| a | b |\n|---|---|\n| 1 | **2** |")
    assert narrow.startswith("<pre>") and "**" not in narrow
    wide = _md_to_html(
        "| quantity | value | reference |\n|---|---|---|\n"
        "| polarization degree 2-8 keV | 4.0 +/- 0.2 per cent | Krawczynski et al. 2022 Table 2 |"
    )
    assert "<pre>" not in wide and "• <b>quantity</b>: polarization degree 2-8 keV" in wide


def test_md_to_html_groups_quote_lines_and_collapses_long_ones():
    from auto_research.hitl.telegram import _md_to_html

    assert _md_to_html("> a\n> b") == "<blockquote>a\nb</blockquote>"
    assert _md_to_html("> a\n> b\n> c\n> d").startswith("<blockquote expandable>")


def test_telegram_routes_edits_apart_from_new_messages_and_commands():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")
    names = [str(getattr(h, "filters", "")) for h in handler.app.handlers]
    assert any("EDITED_MESSAGE" in n for n in names)
    command = [n for n in names if "COMMAND" in n and "~" not in n]
    assert command and all("UpdateType.MESSAGE" in n for n in command)  # an edited /stop is not re-run


def test_telegram_edit_to_a_taken_answer_gets_a_notice():
    handler = TelegramHandler(app=_FakeTGApp(), chat_id="555")

    async def _run():
        task = asyncio.create_task(handler(_query(message="q")))
        for _ in range(200):
            if handler._pending:
                break
            await asyncio.sleep(0)
        mid = next(iter(handler._pending))
        reply = _tg_update("first answer", reply_to_id=mid)
        reply.effective_message.message_id = 900
        handler.on_message(reply)
        answer = await task
        before = len(handler.app.bot.sent)
        edit = _tg_update("amended answer")
        edit.effective_message.message_id = 900
        await handler.handle_edit(edit)
        other = _tg_update("unrelated")
        other.effective_message.message_id = 901
        await handler.handle_edit(other)
        return answer, handler.app.bot.sent[before:]

    answer, after = asyncio.run(_run())
    assert answer.answer == "first answer"
    assert len(after) == 1 and "already taken as the answer" in after[0]["text"]


def test_chunk_md_repeats_a_split_tables_header_in_every_part():
    from auto_research.hitl.telegram import _chunk_md

    table = "| n | value |\n|---|---|\n" + "".join(f"| {i} | v{i} |\n" for i in range(450))
    parts = _chunk_md(table, 2000)
    assert len(parts) > 1
    for part in parts:
        assert part.startswith("| n | value |\n|---|---|\n")
    rows = [l for p in parts for l in p.split("\n")[2:]]
    assert rows == [f"| {i} | v{i} |" for i in range(450)]  # nothing lost or duplicated
