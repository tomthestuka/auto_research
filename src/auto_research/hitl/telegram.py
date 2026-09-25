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
* **Rich messages first** — Bot API 10.1 ``sendRichMessage`` renders the agents' Markdown natively
  (headings, tables, ``$…$`` math, details; 32768 chars / 500 blocks), sent through
  ``Bot.do_api_request`` because python-telegram-bot 22.8 does not wrap it yet
  (``orchestration.yaml: telegram_rich_messages``, default on).
* **HTML fallback** — when a rich send is refused, the Markdown is chunked at block boundaries (a
  heading stays with its section, a fence or table is never cut) and converted to Telegram's small
  HTML subset; tables become an aligned ``<pre>`` block, or a bullet list when too wide for a phone.
  A parse rejection falls back to plain text so a message always sends.
* **Edits** — an edited message is routed on its own, so editing a command never re-runs it; an edit
  to a reply already taken as the answer gets a notice asking for a new message.
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
# Rich messages: 32768 chars and 500 blocks per message (Bot API "Rich Message Limits"). A
# non-blank line is at most one block, so capping lines keeps a part under the block limit.
_RICH_CHUNK = 30000
_RICH_MAX_LINES = 400
_TABLE_PRE_WIDTH = 60  # the widest aligned table row that still reads on a phone
_ANSWERED_KEEP = 200  # reply message ids remembered for the edit notice


def _rich_messages_enabled() -> bool:
    from auto_research.hitl.resolve import _orchestration_config

    return bool(_orchestration_config().get("telegram_rich_messages", True))


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
        # Message ids of replies that resolved a question (insertion-ordered, bounded), so an
        # edit to one can be answered with a notice instead of being silently dropped.
        self._answered: dict[int, None] = {}
        # False once the server or library lacks sendRichMessage, so later sends skip straight
        # to HTML instead of paying a refused call each time.
        self._rich = True
        # HITL slash-command context (§12 Stage 3), wired by run_research via attach_commands.
        # When set, a leading-'/' message is dispatched as a command (never a reply), and a
        # pending ask is released when /stop fires.
        self._cmd_ctx: Any | None = None

        # Defer library construction to start() (Application.initialize needs a running loop); the
        # handler itself is built synchronously in resolve_handler. An injected app (tests) wires now.
        self.app = app
        if self.app is not None:
            self._register_listener()

    def _register_listener(self) -> None:
        from telegram.ext import CallbackQueryHandler, MessageHandler, filters

        # Replies (plain text, NOT commands) resolve pending queries; slash-commands are a
        # separate handler — so a command is structurally never taken as a reply to an LLM.
        # A button tap arrives as a callback query, a third channel again distinct from both.
        # `filters.TEXT` alone also matches EDITED messages, which re-ran an edited command;
        # new messages and edits are therefore routed separately.
        new = filters.UpdateType.MESSAGE
        self.app.add_handler(MessageHandler(new & filters.TEXT & ~filters.COMMAND, self._on_update))
        self.app.add_handler(MessageHandler(new & filters.COMMAND, self._on_command))
        self.app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE, self._on_edit))
        self.app.add_handler(CallbackQueryHandler(self._on_callback))

    async def _on_update(self, update: Any, context: Any = None) -> None:  # pragma: no cover - wiring
        self.on_message(update)

    async def _on_command(self, update: Any, context: Any = None) -> None:  # pragma: no cover - wiring
        await self.handle_command(update)

    async def _on_callback(self, update: Any, context: Any = None) -> None:  # pragma: no cover - wiring
        await self.handle_callback(update)

    async def _on_edit(self, update: Any, context: Any = None) -> None:  # pragma: no cover - wiring
        await self.handle_edit(update)

    async def handle_edit(self, update: Any) -> None:
        """Answer an edit to a reply that was already taken as an answer; ignore every other edit.

        The answer was consumed the moment the original reply arrived, so the edit cannot change
        it; the notice tells the human to send a new message instead. Edits to commands and to
        unrelated messages do nothing."""
        msg = getattr(update, "edited_message", None) or getattr(update, "effective_message", None)
        if msg is None:
            return
        chat = getattr(update, "effective_chat", None) or getattr(msg, "chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        if self.chat_id and chat_id and chat_id != self.chat_id:
            return
        if getattr(msg, "message_id", None) in self._answered:
            await self._send(
                "<i>That reply was already taken as the answer before the edit. "
                "Send a new message to amend it.</i>"
            )

    def attach_commands(self, ctx: Any) -> None:
        """Wire the run-scoped HITL command context (§12 Stage 3); enables /help,/status,/steer,/stop."""
        self._cmd_ctx = ctx

    async def handle_command(self, update: Any) -> None:
        """Dispatch a slash-command message and post the reply. Ignores other chats / non-'/' text /
        commands arriving before the run wired its context. Separated from the handler for testing."""
        if self._cmd_ctx is None:
            return
        msg = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if msg is None:
            return
        chat = getattr(update, "effective_chat", None) or getattr(msg, "chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        if self.chat_id and chat_id and chat_id != self.chat_id:
            return  # only our chat
        text = (getattr(msg, "text", None) or "").strip()
        if not text.startswith("/"):
            return
        from auto_research.hitl.commands import dispatch_command

        reply = await dispatch_command(text, self._cmd_ctx)
        await self._send_md(str(reply), buttons=getattr(reply, "buttons", ()))

    async def handle_callback(self, update: Any) -> None:
        """Handle a button tap: run the action, acknowledge it, and post the result.

        Telegram delivers the button's hidden payload rather than a chat message, so a tap is
        never mistaken for a reply to an agent. Separated from the handler wiring for testing.
        """
        if self._cmd_ctx is None:
            return
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        data = getattr(query, "data", "") or ""
        from auto_research.hitl.commands import dispatch_callback

        reply = await dispatch_callback(data, self._cmd_ctx)
        # Answer the tap first so the client's spinner stops even if sending the result is slow.
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 — an un-answered tap must not lose the action
            pass
        await self._send_md(str(reply))

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
            mid = getattr(msg, "message_id", None)
            if mid is not None:
                self._answered[mid] = None
                while len(self._answered) > _ANSWERED_KEEP:
                    self._answered.pop(next(iter(self._answered)))

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
        await self._publish_command_menu()
        self._started = True

    async def _publish_command_menu(self) -> None:
        """Register the commands with Telegram so they autocomplete in the client's menu.

        This is the platform's own discovery mechanism, and the reason the surface is verbs with a
        numbered roster rather than flags: a command the client offers cannot be misremembered,
        while a flag is invisible until someone reads the source. Best-effort — a failed
        registration costs discoverability, never the channel."""
        from auto_research.hitl.commands import menu_commands

        try:
            from telegram import BotCommand

            await self.app.bot.set_my_commands(
                [BotCommand(name, description) for name, description in menu_commands()]
            )
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        if self._started and self.app is not None:
            try:
                if getattr(self.app, "updater", None) is not None:
                    await self.app.updater.stop()
            finally:
                await self.app.stop()
                await self.app.shutdown()
            self._started = False

    async def _send(self, text_html: str, *, buttons: tuple = ()) -> Any:
        """Send one message as HTML; on a parse rejection, retry as plain text (tags stripped)
        so a malformed-render never blocks the human channel.

        ``buttons`` (``(label, callback_data)`` pairs) attach an inline keyboard to this message —
        a tap sends the payload back without posting anything in the chat, which is how the
        escalations on a steer ack need no typing and cannot be mistyped."""
        try:
            from telegram.error import BadRequest
        except Exception:  # noqa: BLE001 - library absent in unit tests
            BadRequest = ()  # type: ignore[assignment]
        extra = {"reply_markup": _inline_keyboard(buttons)} if buttons else {}
        try:
            return await self.app.bot.send_message(
                chat_id=self.chat_id, text=text_html, parse_mode="HTML", **extra
            )
        except BadRequest:
            plain = _html.unescape(re.sub(r"<[^>]+>", "", text_html))
            return await self.app.bot.send_message(chat_id=self.chat_id, text=plain, **extra)

    async def _send_rich(self, md: str, *, buttons: tuple = ()) -> Any | None:
        """Send ``md`` as one rich message; return the sent message, or ``None`` when refused.

        A refusal (content the rich parser rejects, or a server/library without the method)
        returns ``None`` so the caller falls back to HTML; any other error propagates exactly
        as a plain send's would."""
        if not (self._rich and _rich_messages_enabled()):
            return None
        try:
            from telegram import Message
            from telegram.error import BadRequest, EndPointNotFound
        except Exception:  # noqa: BLE001 - library absent in unit tests
            return None
        kwargs: dict[str, Any] = {"chat_id": self.chat_id, "rich_message": {"markdown": md}}
        markup = _inline_keyboard(buttons) if buttons else None
        if markup is not None:
            kwargs["reply_markup"] = markup.to_dict()
        try:
            return await self.app.bot.do_api_request(
                "sendRichMessage", api_kwargs=kwargs, return_type=Message
            )
        except (AttributeError, EndPointNotFound):
            self._rich = False
            return None
        except BadRequest:
            return None

    async def _send_md(self, md: str, *, buttons: tuple = (), on_posted: Any = None) -> list[Any]:
        """Send Markdown as rich message(s), falling back to HTML chunks part by part.

        ``on_posted(message)`` runs the instant each message exists, so a fast reply can resolve
        a question while later parts are still being sent ([E4]). Buttons ride on the last message."""
        parts = _chunk_md(md, _RICH_CHUNK, max_lines=_RICH_MAX_LINES) if self._rich else [md]
        posted_all: list[Any] = []
        for p_i, part in enumerate(parts):
            last_part = p_i == len(parts) - 1
            posted = await self._send_rich(part, buttons=buttons if last_part else ())
            if posted is not None:
                sent = [posted]
            else:
                sent = []
                html_chunks = _html_chunks(part)
                for c_i, chunk in enumerate(html_chunks):
                    last = last_part and c_i == len(html_chunks) - 1
                    sent.append(await self._send(chunk, buttons=buttons if last else ()))
                    if on_posted is not None:
                        on_posted(sent[-1])
                posted_all.extend(sent)
                continue
            if on_posted is not None:
                on_posted(posted)
            posted_all.extend(sent)
        return posted_all

    async def __call__(self, query: HumanQuery) -> HumanResponse:
        await self.start()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        ids: list[int] = []

        # Register each message_id -> Future the instant its message exists, so a fast reply-to
        # (or a single-open plain reply) arriving mid-send resolves the query rather than being
        # dropped — which would otherwise block the run to the full response timeout ([E4]).
        def _register(posted: Any) -> None:
            self._pending[posted.message_id] = fut
            ids.append(posted.message_id)

        await self._send_md(_query_markdown(query), on_posted=_register)
        from auto_research.hitl.commands import wait_reply_or_stop

        rc = getattr(self._cmd_ctx, "run_control", None)
        try:
            answer = await wait_reply_or_stop(fut, rc, self.timeout_s)
        finally:
            for mid in ids:
                self._pending.pop(mid, None)
        return HumanResponse(query_id=query.id, answer=answer)


def _inline_keyboard(buttons: tuple) -> Any:
    """Build a one-row inline keyboard from ``(label, callback_data)`` pairs.

    Returns ``None`` when the library is absent (unit tests) so the message still sends as text —
    a missing button layer never costs the human the reply itself.
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:  # noqa: BLE001
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in buttons]]
    )


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
    md = _convert_tables(md)  # a table becomes a fence (stashed next) or a bullet list
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
    quote: list[str] = []

    def flush_quote() -> None:
        # Consecutive quoted lines are one quotation; a long one collapses so the question
        # around it stays readable.
        if quote:
            tag = "<blockquote expandable>" if len(quote) > 3 else "<blockquote>"
            out.append(f"{tag}{chr(10).join(quote)}</blockquote>")
            quote.clear()

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
            quote.append(q.group(1))
            continue
        flush_quote()
        out.append(line)
    flush_quote()
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


_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _md_blocks(text: str) -> list[str]:
    """Split Markdown into blocks: a fenced code block, or a run of non-blank lines (a paragraph,
    list or table). A heading is glued to the block after it, so no split ever orphans one."""
    lines = text.strip().split("\n")
    blocks: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            blocks.append("\n".join(cur))
            cur.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            mark = fence.group(1)
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(mark):
                j += 1
            blocks.append("\n".join(lines[i : j + 1]))
            i = j + 1
            continue
        if not line.strip():
            flush()
        elif _HEADING_RE.match(line):
            flush()
            blocks.append(line)
        else:
            cur.append(line)
        i += 1
    flush()

    glued: list[str] = []
    heading: str | None = None
    for block in blocks:
        if _HEADING_RE.match(block) and "\n" not in block:
            heading = block if heading is None else f"{heading}\n\n{block}"
            continue
        glued.append(block if heading is None else f"{heading}\n\n{block}")
        heading = None
    if heading is not None:
        glued.append(heading)
    return glued


def _split_block(block: str, size: int, max_lines: int | None) -> list[str]:
    """Split one oversized block by lines; a fenced block is re-opened in every piece."""
    lines = block.split("\n")
    fence = _FENCE_RE.match(lines[0])
    if fence:
        max_lines = None  # a fenced block is ONE rich block however many lines it holds
    if len(block) <= size and (max_lines is None or len(lines) <= max_lines):
        return [block]
    opener, closer = (lines[0], fence.group(1)) if fence else ("", "")
    body = lines[1:-1] if fence and len(lines) > 1 and lines[-1].strip().startswith(closer) else (
        lines[1:] if fence else lines
    )
    if not fence and len(lines) > 2 and _TABLE_ROW_RE.match(lines[0]) and _TABLE_SEP_RE.match(lines[1]):
        # A split table repeats its header and separator in every piece, so each part renders
        # as a table rather than as bare pipe rows.
        head = lines[:2]
        pieces = _split_block("\n".join(lines[2:]), size - len("\n".join(head)) - 1,
                              None if max_lines is None else max_lines - 2)
        return ["\n".join(head + [p]) for p in pieces]
    budget = size - (len(opener) + len(closer) + 2 if fence else 0)
    line_cap = (max_lines - 2) if (max_lines and fence) else max_lines
    pieces: list[list[str]] = [[]]
    used = 0
    for line in body:
        while len(line) > budget:  # a single line longer than the whole budget is hard-sliced
            if pieces[-1]:
                pieces.append([])
            pieces[-1].append(line[:budget])
            pieces.append([])
            line = line[budget:]
            used = 0
        if pieces[-1] and (used + len(line) + 1 > budget or (line_cap and len(pieces[-1]) >= line_cap)):
            pieces.append([])
            used = 0
        pieces[-1].append(line)
        used += len(line) + 1
    out = ["\n".join(p) for p in pieces if p]
    return [f"{opener}\n{p}\n{closer}" for p in out] if fence else out


def _block_lines(md: str) -> int:
    """Lines that can each become a rich block: non-blank lines outside fences, a fence counting 1."""
    count, in_fence = 0, False
    for line in md.split("\n"):
        if _FENCE_RE.match(line):
            if not in_fence:
                count += 1
            in_fence = not in_fence
        elif not in_fence and line.strip():
            count += 1
    return count


def _chunk_md(text: str, size: int = _CHUNK, *, max_lines: int | None = None) -> list[str]:
    """Pack Markdown blocks into chunks of at most ``size`` chars (and ``max_lines`` lines).

    Cuts fall only between blocks, so a heading stays with its section and a fence or table is
    never cut; a single block larger than a chunk is split by lines (a fence re-opened)."""
    chunks: list[str] = []
    cur = ""
    for block in _md_blocks(text):
        for piece in _split_block(block, size, max_lines):
            cand = f"{cur}\n\n{piece}" if cur else piece
            too_many = max_lines is not None and _block_lines(cand) > max_lines
            if cur and (len(cand) > size or too_many):
                chunks.append(cur)
                cur = piece
            else:
                cur = cand
    if cur:
        chunks.append(cur)
    return chunks


def _html_chunks(md: str, size: int = _CHUNK) -> list[str]:
    """Render Markdown to HTML messages that each fit Telegram's cap, measured AFTER rendering
    (escaping can grow a chunk past the limit); an overflowing chunk is re-split smaller."""
    out: list[str] = []
    for chunk in _chunk_md(md, size) or ["(no message)"]:
        html_text = _md_to_html(chunk)
        if len(html_text) > _TG_MSG_LIMIT and size > 200:
            out.extend(_html_chunks(chunk, size // 2))
        else:
            out.append(html_text)
    return out


def _query_markdown(query: HumanQuery) -> str:
    """A :class:`HumanQuery` as one Markdown document: header, body, reply hint.

    Identifiers go in inline code so an underscore in a role name reads literally on both paths."""
    return (
        f"**Human input needed** · `{query.kind}`\nfrom `{query.sender_label}`\n\n"
        f"{query.message.strip() or '(no message)'}\n\n"
        "*Reply to this message to answer.*"
    )


def _render_chunks(query: HumanQuery) -> list[str]:
    """The HTML fallback rendering of a query (header on the first message, hint on the last)."""
    return _html_chunks(_query_markdown(query))


# ── Tables: the fallback ladder (aligned <pre>, else a bullet list) ─────────────────────


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _plain_cell(cell: str) -> str:
    return re.sub(r"(\*\*|__|`)", "", cell)


def _table_to_md(lines: list[str]) -> str:
    """Rewrite a GFM table for a surface without tables: an aligned monospace block when it fits
    on a phone, otherwise one bullet per row with the header as each value's label."""
    header = _table_cells(lines[0])
    rows = [_table_cells(l) for l in lines[2:]]
    widths = [
        max([len(_plain_cell(h))] + [len(_plain_cell(r[k])) for r in rows if k < len(r)])
        for k, h in enumerate(header)
    ]
    if sum(widths) + 2 * (len(widths) - 1) <= _TABLE_PRE_WIDTH:
        def fmt(cells: list[str]) -> str:
            padded = [_plain_cell(c).ljust(w) for c, w in zip(cells, widths)]
            return "  ".join(padded).rstrip()

        body = [fmt(header), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows]
        return "```\n" + "\n".join(body) + "\n```"
    bullets = []
    for r in rows:
        pairs = [f"**{h}**: {v}" for h, v in zip(header, r) if v]
        bullets.append("- " + " · ".join(pairs))
    return "\n".join(bullets)


def _convert_tables(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (
            _TABLE_ROW_RE.match(lines[i])
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            j = i + 2
            while j < len(lines) and _TABLE_ROW_RE.match(lines[j]):
                j += 1
            out.append(_table_to_md(lines[i:j]))
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)
