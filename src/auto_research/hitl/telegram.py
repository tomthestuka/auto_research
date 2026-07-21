"""Telegram human handler over long polling (CLAUDE.md §9.8).

The recommended reliable two-way connector: post the query to a chat and await the human's
reply via ``getUpdates`` long polling — an **outbound-only** channel (no public URL, no inbound
endpoint), so it fits a shared cluster's network policy. Telegram retains undelivered updates
server-side for up to 24 h, so a poll gap / reconnect is lossless (unlike Slack Socket Mode).

Design mirrors :mod:`auto_research.hitl.slack`:

* **Multi-question ready** — each query's message(s) are posted and the human's reply is matched
  by Telegram's native **reply-to** (``reply_to_message.message_id`` → the pending Future); a plain
  reply resolves the query only when a single one is open. This keeps concurrent raises (future
  parallel analysts) routable without a protocol change.
* **HTML rendering** — the Markdown body is converted to Telegram's small HTML tag subset; a parse
  rejection falls back to plain text so a message always sends.
* The ``python-telegram-bot`` ``Application`` is injectable so the send + reply-resolution logic is
  unit-testable without the library installed or any network. The library is imported lazily (only
  in ``start``/``_build_telegram_app``) so importing this module never hard-requires it.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import re
from typing import Any

from auto_research.hitl.resolve import response_timeout_s
from auto_research.schemas import HumanQuery, HumanResponse

# Telegram caps a message at 4096 chars; chunk the Markdown *before* rendering so each posted
# message is self-contained valid HTML (a cut never splits a tag). Room left for header/hint.
_TG_MSG_LIMIT = 4096
_CHUNK = 3000  # headroom under the 4096 cap for HTML-tag expansion + header/hint


class TelegramHandler:
    """A ``HumanHandler`` that asks in a Telegram chat and awaits the human's reply."""

    def __init__(
        self,
        *,
        app: Any | None = None,
        token: str | None = None,
        chat_id: str | int | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.chat_id = str(chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        if not self.chat_id:
            raise RuntimeError("Telegram connector needs TELEGRAM_CHAT_ID.")
        self._token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.timeout_s = timeout_s if timeout_s is not None else response_timeout_s()
        self._started = False
        # Map every posted message_id -> the Future awaiting its reply. All chunks of one query
        # point at the same Future, so a reply-to any chunk resolves it.
        self._pending: dict[int, asyncio.Future[str]] = {}

        # Defer library construction to start() (Application.initialize needs a running loop); the
        # handler itself is built synchronously in resolve_handler. An injected app (tests) wires now.
        self.app = app
        if self.app is not None:
            self._register_listener()

    def _register_listener(self) -> None:
        from telegram.ext import MessageHandler, filters

        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_update))

    async def _on_update(self, update: Any, context: Any = None) -> None:  # pragma: no cover - wiring
        self.on_message(update)

    def on_message(self, update: Any) -> None:
        """Resolve a pending Future from a human reply.

        Routes a **reply-to** one of a query's posted messages to that query's Future
        (unambiguous even with several questions open); a plain reply resolves the single open
        question when exactly one is pending. Ignores other chats, the bot's own messages, and
        empty text. Separated from the handler wiring so tests can drive it with a fake update.
        """
        msg = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if msg is None:
            return
        chat = getattr(update, "effective_chat", None) or getattr(msg, "chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        if self.chat_id and chat_id and chat_id != self.chat_id:
            return  # only our chat
        if getattr(getattr(msg, "from_user", None), "is_bot", False):
            return  # our own posts
        text = (getattr(msg, "text", None) or "").strip()
        if not text:
            return
        reply_to = getattr(msg, "reply_to_message", None)
        rid = getattr(reply_to, "message_id", None) if reply_to is not None else None
        if rid is not None and rid in self._pending:
            fut = self._pending.get(rid)  # reply-to → unambiguous
        else:
            # Dedupe by Future identity, NOT by message_id: a chunked query registers every
            # chunk's message_id against the SAME Future, so counting entries would see one
            # multi-chunk question as "many open" and refuse a plain reply (forcing reply-to).
            open_futs = list({id(f): f for f in self._pending.values() if not f.done()}.values())
            fut = open_futs[0] if len(open_futs) == 1 else None
        if fut is not None and not fut.done():
            fut.set_result(text)

    async def start(self) -> None:
        if self._started:
            return
        # Telegram's API embeds the bot token in every request URL, and httpx logs full
        # URLs at INFO ("HTTP Request: POST https://api.telegram.org/bot<token>/…") —
        # which would write the credential into every backend log. Keep httpx/httpcore
        # at WARNING for the whole process while a Telegram connector is in use.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        if self.app is None:  # real connector: build the Application inside the running loop
            self.app = _build_telegram_app(self._token)
            self._register_listener()
        await self.app.initialize()
        await self.app.start()
        # drop_pending_updates: ignore messages that predate this run's polling start.
        await self.app.updater.start_polling(drop_pending_updates=True)
        self._started = True

    async def aclose(self) -> None:
        if self._started and self.app is not None:
            try:
                if getattr(self.app, "updater", None) is not None:
                    await self.app.updater.stop()
            finally:
                await self.app.stop()
                await self.app.shutdown()
            self._started = False

    async def _send(self, text_html: str) -> Any:
        """Send one message as HTML; on a parse rejection, retry as plain text (tags stripped)
        so a malformed-render never blocks the human channel."""
        try:
            from telegram.error import BadRequest
        except Exception:  # noqa: BLE001 - library absent in unit tests
            BadRequest = ()  # type: ignore[assignment]
        try:
            return await self.app.bot.send_message(
                chat_id=self.chat_id, text=text_html, parse_mode="HTML"
            )
        except BadRequest:
            plain = _html.unescape(re.sub(r"<[^>]+>", "", text_html))
            return await self.app.bot.send_message(chat_id=self.chat_id, text=plain)

    async def __call__(self, query: HumanQuery) -> HumanResponse:
        await self.start()
        chunks = _render_chunks(query)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        ids: list[int] = []
        # Register each message_id -> Future the instant its message exists, so a fast reply-to
        # (or a single-open plain reply) arriving mid-send resolves the query rather than being
        # dropped — which would otherwise block the run to the full response timeout ([E4]).
        for chunk in chunks:
            posted = await self._send(chunk)
            self._pending[posted.message_id] = fut
            ids.append(posted.message_id)
        try:
            answer = await asyncio.wait_for(fut, timeout=self.timeout_s)
        finally:
            for mid in ids:
                self._pending.pop(mid, None)
        return HumanResponse(query_id=query.id, answer=answer)


def _build_telegram_app(token: str | None = None) -> Any:
    from telegram.ext import ApplicationBuilder

    tok = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("Telegram connector needs TELEGRAM_BOT_TOKEN.")
    return ApplicationBuilder().token(tok).build()


# ── Markdown → Telegram HTML ──────────────────────────────────────────────────────────────
# Telegram's HTML parse mode renders only a small tag set (<b> <i> <u> <s> <code> <pre> <a>
# <blockquote>); there is no heading/table/list tag, and unknown tags are rejected. So this maps
# the Markdown our roles write onto that subset: bold/italic, inline+fenced code, links, headings
# → bold, bullets → "•", blockquotes; and escapes <, >, & everywhere else.


def _md_to_html(md: str) -> str:
    """Render a Markdown string as the Telegram HTML subset (see module note). Best-effort — the
    sender falls back to plain text if Telegram ever rejects the result."""
    fences: list[str] = []
    spans: list[str] = []

    def _stash_fence(m: "re.Match[str]") -> str:
        fences.append(m.group(1))
        return f"\x00F{len(fences) - 1}\x00"

    def _stash_span(m: "re.Match[str]") -> str:
        spans.append(m.group(1))
        return f"\x00C{len(spans) - 1}\x00"

    md = re.sub(r"```[^\n]*\n(.*?)```", _stash_fence, md, flags=re.DOTALL)
    md = re.sub(r"`([^`]+)`", _stash_span, md)

    md = _html.escape(md, quote=False)  # placeholders (control chars) survive unescaped

    md = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{_html.escape(m.group(2), quote=True)}">{m.group(1)}</a>',
        md,
    )
    md = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", md)
    md = re.sub(r"__([^_]+)__", r"<b>\1</b>", md)
    md = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", md)

    out: list[str] = []
    for line in md.split("\n"):
        h = re.match(r"\s{0,3}#{1,6}\s+(.*)$", line)
        if h:
            out.append(f"<b>{h.group(1).strip()}</b>")
            continue
        b = re.match(r"(\s*)[-*+]\s+(.*)$", line)
        if b:
            out.append(f"{b.group(1)}• {b.group(2)}")
            continue
        # the leading '>' was HTML-escaped to '&gt;' by the escape pass above, so match that
        q = re.match(r"\s{0,3}&gt;\s?(.*)$", line)
        if q:
            out.append(f"<blockquote>{q.group(1)}</blockquote>")
            continue
        out.append(line)
    text = "\n".join(out)

    text = re.sub(
        r"\x00C(\d+)\x00",
        lambda m: f"<code>{_html.escape(spans[int(m.group(1))], quote=False)}</code>",
        text,
    )
    text = re.sub(
        r"\x00F(\d+)\x00",
        lambda m: f"<pre>{_html.escape(fences[int(m.group(1))], quote=False)}</pre>",
        text,
    )
    return text


def _chunk_md(text: str, size: int = _CHUNK) -> list[str]:
    """Split Markdown into chunks of at most ``size`` chars at paragraph/line breaks, so each
    chunk renders to self-contained HTML (no tag spans a message boundary)."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > size:
        window = text[:size]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = size
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _render_chunks(query: HumanQuery) -> list[str]:
    """Render a :class:`HumanQuery` as one-or-more HTML messages: a header on the first, the
    Markdown body (chunked + converted), and a reply hint on the last."""
    md_chunks = _chunk_md(query.message) or ["(no message)"]
    html_chunks = [_md_to_html(c) for c in md_chunks]
    header = (
        f"<b>Human input needed · {_html.escape(query.kind)}</b>\n"
        f"from <b>{_html.escape(query.sender_label)}</b>\n\n"
    )
    hint = "\n\n<i>Reply to this message to answer.</i>"
    html_chunks[0] = header + html_chunks[0]
    html_chunks[-1] = html_chunks[-1] + hint
    return html_chunks
