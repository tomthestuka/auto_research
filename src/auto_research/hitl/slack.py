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
            self.on_message(event)

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
        if self._started and self._socket is not None:
            await self._socket.disconnect_async()
            self._started = False

    async def __call__(self, query: HumanQuery) -> HumanResponse:
        await self.start()
        summary = f"Human input needed ({query.kind}) from {query.sender_label}"
        posted = await self.app.client.chat_postMessage(
            channel=self.channel, text=summary, blocks=_blocks(query)
        )
        thread_ts = posted["ts"]

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending[thread_ts] = fut
        try:
            answer = await asyncio.wait_for(fut, timeout=self.timeout_s)
        finally:
            self._pending.pop(thread_ts, None)
        return HumanResponse(query_id=query.id, answer=answer)


_SLACK_TEXT_LIMIT = 2900  # Block Kit caps a section text object at 3000 chars.


def _truncate(text: str, limit: int = _SLACK_TEXT_LIMIT) -> str:
    """Return ``text`` within Slack's per-block character budget, marking any cut."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _blocks(query: HumanQuery) -> list[dict]:
    """Render a :class:`HumanQuery` as a Block Kit message.

    A header, a sender line, the question, and (when present) the context each get their
    own block, so the message stays readable in Slack and the reply lands in this thread.
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
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Question*\n{_truncate(query.question)}"},
        },
    ]
    if query.context:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Context*\n{_truncate(query.context)}"},
            }
        )
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
