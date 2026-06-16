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
import os
from typing import Any

from auto_research.hitl.resolve import response_timeout_s
from auto_research.schemas import HumanQuery, HumanResponse


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

        if app is not None:
            self.app = app
            self._socket = socket_handler
        else:
            self.app, self._socket = _build_slack_app()
        self._register_listener()

    def _register_listener(self) -> None:
        @self.app.event("message")
        async def _on_message(event: dict[str, Any], **_: Any) -> None:  # pragma: no cover - wiring
            self.on_message(event)

    def on_message(self, event: dict[str, Any]) -> None:
        """Resolve the pending Future for a human reply in a tracked thread.

        Separated from the decorator so tests can drive it directly without slack_bolt.
        """
        if event.get("bot_id"):  # ignore our own posts
            return
        thread = event.get("thread_ts")
        text = (event.get("text") or "").strip()
        fut = self._pending.get(thread) if thread else None
        if fut is not None and not fut.done() and text:
            fut.set_result(text)

    async def start(self) -> None:
        if not self._started and self._socket is not None:
            await self._socket.connect_async()
            self._started = True

    async def aclose(self) -> None:
        if self._started and self._socket is not None:
            await self._socket.disconnect_async()
            self._started = False

    async def __call__(self, query: HumanQuery) -> HumanResponse:
        await self.start()
        body = f"*[auto_research · {query.kind} · {query.sender_label}]*\n{query.question}"
        if query.context:
            body += f"\n_context: {query.context}_"
        posted = await self.app.client.chat_postMessage(channel=self.channel, text=body)
        thread_ts = posted["ts"]

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending[thread_ts] = fut
        try:
            answer = await asyncio.wait_for(fut, timeout=self.timeout_s)
        finally:
            self._pending.pop(thread_ts, None)
        return HumanResponse(query_id=query.id, answer=answer)


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
