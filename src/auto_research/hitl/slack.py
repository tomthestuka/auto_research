"""Slack human handler over Socket Mode (CLAUDE.md §9.8).

The reference two-way connector: post the query to a channel and await the next human
reply in its thread via Socket Mode (an outbound websocket — no public URL). Built per the
spec but **not live-tested** here (Slack is retired in this deployment); the send + reply-
resolution logic is unit-tested with a fake app/client.

The ``slack_bolt`` app/client is injectable so the logic is testable without ``slack_bolt``
installed or any network.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from auto_research.hitl.resolve import response_timeout_s
from auto_research.schemas import HumanQuery, HumanResponse

logger = logging.getLogger(__name__)


class _SocketRotationDowngradeFilter(logging.Filter):
    """Demote slack_sdk's benign Socket Mode rotation ERROR to WARNING.

    When Socket Mode rotates its websocket, an in-flight send hits a closing transport and
    ``slack_sdk.socket_mode.aiohttp`` logs it at ERROR ("Cannot write to closing transport").
    The connection self-heals (a new endpoint is dialed), so the ERROR is misleading noise —
    this filter rewrites only that specific rotation message to WARNING and passes everything
    else through untouched, so genuine errors keep their level.
    """

    _NEEDLE = "closing transport"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and self._NEEDLE in record.getMessage():
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


# One shared instance; installing it twice on the same logger is a no-op (idempotent start()).
_ROTATION_FILTER = _SocketRotationDowngradeFilter()


def _install_socket_rotation_log_downgrade() -> None:
    """Attach the rotation-noise downgrade filter to slack_sdk's async socket-mode logger.

    The filter must live on the *emitting* logger (so it can mutate the record's level before
    handlers render it). Idempotent — re-adding the same filter instance is harmless.
    """
    logger = logging.getLogger("slack_sdk.socket_mode.aiohttp")
    if _ROTATION_FILTER not in logger.filters:
        logger.addFilter(_ROTATION_FILTER)


class SlackHandler:
    """A ``HumanHandler`` that asks in a Slack channel and awaits a threaded reply."""

    def __init__(
        self,
        *,
        app: Any | None = None,
        socket_handler: Any | None = None,
        channel: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.channel = channel or os.environ.get("SLACK_HUMAN_CHANNEL")
        if not self.channel:
            raise RuntimeError("Slack connector needs SLACK_HUMAN_CHANNEL.")
        # Default to the shared cross-channel human-response window (orchestration.yaml).
        self.timeout_s = timeout_s if timeout_s is not None else response_timeout_s()
        self._started = False
        # Map a posted message thread ts -> the Future awaiting its reply.
        self._pending: dict[str, asyncio.Future[str]] = {}
        # HITL slash-command context (§12 Stage 3), wired by run_research via attach_commands.
        self._cmd_ctx: Any | None = None
        self._command_tasks: set[asyncio.Task] = set()
        self._command_names: dict[asyncio.Task, str] = {}  # task → its command word  # command handlers in flight (ruling D8)

        # Defer slack_bolt construction to start(): AsyncSocketModeHandler builds an aiohttp
        # session that needs a *running* event loop, but the handler is constructed
        # synchronously (resolve_handler, before the loop runs). An injected app (tests) is
        # wired immediately; the real app/socket are built lazily inside start().
        self.app = app
        self._socket = socket_handler
        if self.app is not None:
            self._register_listener()

    def _register_listener(self) -> None:
        @self.app.event("message")
        async def _on_message(event: dict[str, Any], **_: Any) -> None:  # pragma: no cover - wiring
            text = (event.get("text") or "").strip()
            is_ours = not (event.get("bot_id") or event.get("subtype"))
            in_channel = event.get("channel") in (None, self.channel)
            if self._cmd_ctx is not None and text.startswith("/") and is_ours and in_channel:
                # A slash-command is never taken as a reply, and runs in its own task so a
                # `/stop` never waits behind a slow `/status` (ruling D8).
                self._spawn(self.handle_command(event), text.split()[0].split("@", 1)[0])
            else:
                self.on_message(event)

    def attach_commands(self, ctx: Any) -> None:
        """Wire the run-scoped HITL command context (§12 Stage 3); enables /help,/status,/steer,/stop."""
        self._cmd_ctx = ctx

    async def handle_command(self, event: dict[str, Any]) -> None:
        """Dispatch a slash-command channel message and post the reply. Separated for testing."""
        if self._cmd_ctx is None:
            return
        text = (event.get("text") or "").strip()
        if not text.startswith("/"):
            return
        from auto_research.hitl.commands import dispatch_command

        reply = await dispatch_command(text, self._cmd_ctx, ack=self._post)
        if str(reply):
            await self.app.client.chat_postMessage(channel=self.channel, text=reply)

    async def _post(self, text: str) -> None:
        """Post a command's acknowledgement. Deliberately not ``notify``: that name turns on the
        seat-to-seat mirror, which Slack does not carry (§13), so the /stop checklist is recorded
        as skipped here."""
        await self.app.client.chat_postMessage(channel=self.channel, text=text)

    def in_flight_commands(self) -> list[str]:
        """The commands whose handlers are still running (each named once), for the /stop
        checklist: a channel close cancels them, and the human is told which (D8)."""
        current = asyncio.current_task()
        names = [
            self._command_names.get(task, "a command") for task in self._command_tasks
            if task is not current and not task.done()
            and self._command_names.get(task) != "/stop"
        ]
        return list(dict.fromkeys(names))

    def _spawn(self, coro: Any, name: str = "a command") -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._command_tasks.add(task)
        self._command_names[task] = name
        task.add_done_callback(self._command_done)
        return task

    def _command_done(self, task: asyncio.Task) -> None:
        self._command_tasks.discard(task)
        self._command_names.pop(task, None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Slack command handler failed: %r", task.exception())

    def on_message(self, event: dict[str, Any]) -> None:
        """Resolve a pending Future from a human reply.

        Matches a **threaded** reply to the question's own thread; **also** accepts a plain
        **channel** reply (no ``thread_ts``) and resolves the most recent pending question — so
        a human who replies in the channel instead of the message thread still unblocks the run
        (the common case). Ignores our own posts, non-message events (joins/edits carry a
        ``subtype``), empty text, and messages from other channels.

        Separated from the decorator so tests can drive it directly without slack_bolt.
        """
        if event.get("bot_id") or event.get("subtype"):  # our own posts / joins / edits
            return
        if self.channel and event.get("channel") not in (None, self.channel):
            return  # only our channel (``None`` channel kept for the direct unit tests)
        text = (event.get("text") or "").strip()
        if not text:
            return
        thread = event.get("thread_ts")
        if thread:
            fut = self._pending.get(thread)  # in-thread → always unambiguous
        else:
            # Plain channel reply: only attributable when a *single* question is open. With
            # several pending (concurrent agents) a bare reply can't be matched to one, so we
            # ignore it and rely on the in-thread match. (Today the orchestration is sequential
            # so ≤1 is ever open, but this keeps the fallback correct if that changes.)
            open_futs = [self._pending[ts] for ts in self._pending if not self._pending[ts].done()]
            fut = open_futs[0] if len(open_futs) == 1 else None
        if fut is not None and not fut.done():
            fut.set_result(text)

    async def start(self) -> None:
        if self._started:
            return
        _install_socket_rotation_log_downgrade()  # quiet the benign rotation ERROR (#8)
        if self.app is None:  # real connector: build slack_bolt now, inside the running loop
            self.app, self._socket = _build_slack_app()
            self._register_listener()
        if self._socket is not None:
            await self._socket.connect_async()
        self._started = True

    async def aclose(self) -> None:
        current = asyncio.current_task()
        for task in list(self._command_tasks):
            if task is not current:
                task.cancel()
        if self._started and self._socket is not None:
            await self._socket.disconnect_async()
            self._started = False

    async def __call__(self, query: HumanQuery) -> HumanResponse:
        await self.start()
        summary = (
            f"Human input needed ({query.kind}) from {query.sender_label}: {query.headline}"
        )
        posted = await self.app.client.chat_postMessage(
            channel=self.channel, text=summary, blocks=_blocks(query)
        )
        thread_ts = posted["ts"]

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending[thread_ts] = fut
        from auto_research.hitl.commands import wait_reply_or_stop

        rc = getattr(self._cmd_ctx, "run_control", None)
        try:
            answer = await wait_reply_or_stop(fut, rc, self.timeout_s)
        finally:
            self._pending.pop(thread_ts, None)
        return HumanResponse(query_id=query.id, answer=answer)


# Slack's dedicated ``markdown`` block accepts standard Markdown up to 12000 chars (4× the
# 3000-char ``section`` text cap it supersedes). A long design report is split across several
# markdown blocks; the per-message ceiling is 50 blocks, so the body is capped to leave room
# for the header/sender/reply context blocks.
_MARKDOWN_BLOCK_LIMIT = 11900
_MAX_BODY_BLOCKS = 45


def _chunk(text: str, size: int = _MARKDOWN_BLOCK_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``size`` chars, preserving the original Markdown.

    Each cut prefers the last paragraph break inside the window, then the last line break,
    and hard-slices only a single run with no break — so formatting survives the split.
    """
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


def _blocks(query: HumanQuery) -> list[dict]:
    """Render a :class:`HumanQuery` as a Block Kit message.

    A header, a sender line, the Markdown ``message`` (one ``markdown`` block per chunk), and a
    reply hint — so a full design report renders cleanly in Slack and the reply lands in this
    thread. A body longer than the block budget marks its final block as truncated.
    """
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Human input needed · {query.kind}"},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"from *{query.sender_label}*"}],
        },
    ]
    chunks = _chunk(query.message) or [query.message.strip() or "(no message)"]
    body = chunks[:_MAX_BODY_BLOCKS]
    truncated = len(body) < len(chunks)
    for i, chunk in enumerate(body):
        text = chunk + "\n\n…(truncated)" if (truncated and i == len(body) - 1) else chunk
        blocks.append({"type": "markdown", "text": text})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Reply in this thread to answer."}],
        }
    )
    return blocks


def _build_slack_app() -> tuple[Any, Any]:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise RuntimeError("Slack connector needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN.")
    app = AsyncApp(token=bot_token)
    socket = AsyncSocketModeHandler(app, app_token)
    return app, socket
