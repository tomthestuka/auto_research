"""D2 — the Telegram connector's bot survives a slow Bot API answer (python-telegram-bot's own
``read_timeout``, set by ``hitl.telegram._build_telegram_app``).

Observed 2026-11-04: the API answered one launch ping in 26.6 s and the next in 0.45 s, and the
library's default 5 s read timeout failed the preflight on the first. The test runs the production
app builder against a local stand-in for the Bot API (only the network is faked: a real HTTP
server on localhost) whose first ``sendMessage`` answers after more than the default timeout.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("telegram")

#: Longer than the library's default read timeout (5 s), well inside the connector's.
SLOW_S = 5.6


async def _bot_api(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer Bot API calls over one keep-alive connection; ``sendMessage`` with text "slow"
    answers after ``SLOW_S``."""
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1]
            length = next((int(line.split(":", 1)[1]) for line in lines
                           if line.lower().startswith("content-length:")), 0)
            body = (await reader.readexactly(length)).decode("utf-8") if length else ""
            method = path.rsplit("/", 1)[-1]
            if method == "getMe":
                result = {"id": 1, "is_bot": True, "first_name": "bot", "username": "bot"}
            elif method == "sendMessage":
                if "slow" in body:
                    await asyncio.sleep(SLOW_S)
                result = {"message_id": 7, "date": 0, "chat": {"id": 42, "type": "private"},
                          "text": "ok"}
            else:
                result = True
            payload = json.dumps({"ok": True, "result": result}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


def _point_builder_at(monkeypatch, url: str) -> None:
    """Every app the builder makes talks to ``url`` instead of api.telegram.org (the network)."""
    from telegram.ext import ApplicationBuilder

    build = ApplicationBuilder.build

    def _build(self):
        self.base_url(url)
        return build(self)

    monkeypatch.setattr(ApplicationBuilder, "build", _build)


def test_a_slow_bot_api_answer_reaches_the_connector_instead_of_timing_out(monkeypatch):
    # The stand-in is on localhost: a shell's proxy (httpx trusts the environment) must not see it.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    from telegram.error import TimedOut
    from telegram.ext import ApplicationBuilder

    from auto_research.hitl.telegram import _build_telegram_app

    async def scenario():
        server = await asyncio.start_server(_bot_api, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _point_builder_at(monkeypatch, f"http://127.0.0.1:{port}/bot")
        try:
            # The library's default fails on this answer, which is the observed launch failure.
            default = ApplicationBuilder().token("1:test").build()
            await default.initialize()
            with pytest.raises(TimedOut):
                await default.bot.send_message(chat_id=42, text="slow")
            await default.shutdown()

            app = _build_telegram_app("1:test")
            await app.initialize()
            sent = await app.bot.send_message(chat_id=42, text="slow")
            await app.shutdown()
            return sent
        finally:
            server.close()
            await server.wait_closed()

    sent = asyncio.run(scenario())
    assert sent.message_id == 7
