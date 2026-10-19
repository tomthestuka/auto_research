"""Opt-in live cell for ruling D8: `/status` and `/stop` through the real Telegram path.

    AUTO_RESEARCH_LIVE_STOP_TELEGRAM=1 pytest tests/test_live_stop_telegram.py -s

A real `run_research` on the configured roles, with the reviewer and the status monitor seated on
`anthropic:claude-sonnet-5` (the config-dir seam, `tests/seats.py`), a real Telegram connector
(`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) and its real MCP shell server and sandboxes. Once the
reviewer's turn starts, `/status` and then `/stop` enter the connector the way the bot receives
them — as Telegram updates on the Application's own update queue, through python-telegram-bot's
dispatch — so nobody has to type. Checks: both acks are sent at once, `/stop` lands while
`/status` is still investigating, the landing checklist is sent before the connector closes, the
caller gets `UserInterruptionError`, and no process the run started survives. Spend: one reviewer
turn cut short plus one status investigation. Writes `logs/dev/stop_telegram_<stamp>.{jsonl,md}`.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_STOP_TELEGRAM") != "1",
    reason="set AUTO_RESEARCH_LIVE_STOP_TELEGRAM=1 to send /status and /stop through Telegram",
)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev"


def _is_ack(text: str) -> bool:
    from auto_research.hitl.commands import STATUS_ACK, STOP_ACK

    return text.startswith((STATUS_ACK, STOP_ACK))


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _update(bot, text: str, update_id: int, chat_id: str):
    from telegram import Update

    command = text.split()[0]
    return Update.de_json({
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": int(time.time()), "text": text,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(chat_id), "is_bot": False, "first_name": "live-test"},
            "entities": [{"type": "bot_command", "offset": 0, "length": len(command)}],
        },
    }, bot)


def _reply(bot, to_message_id: int, update_id: int, chat_id: str):
    from telegram import Update

    chat = {"id": int(chat_id), "type": "private"}
    return Update.de_json({
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": int(time.time()), "text": "ok", "chat": chat,
            "from": {"id": int(chat_id), "is_bot": False, "first_name": "live-test"},
            "reply_to_message": {"message_id": to_message_id, "date": int(time.time()),
                                 "chat": chat, "text": "ping"},
        },
    }, bot)


def test_live_status_and_stop_through_telegram(tmp_path):
    _live_cell(tmp_path, wait_for_answer=False)


def test_live_status_answer_then_stop_through_telegram(tmp_path):
    """`/status` alone first: the monitor's answer arrives in the chat; then `/stop`."""
    _live_cell(tmp_path, wait_for_answer=True)


def _live_cell(tmp_path, *, wait_for_answer: bool):
    load_dotenv()
    from auto_research import run_research
    from auto_research.hitl import TelegramHandler
    from auto_research.run_control import UserInterruptionError
    from auto_research.schemas import ResearchRequest
    from auto_research.hitl.commands import STATUS_ACK
    from tests.seats import seat, seated

    handler = TelegramHandler()
    sent: list[tuple[float, str]] = []  # every message the connector sent, with when
    original_send = handler._send_md

    async def _recording_send(text, **kwargs):
        sent.append((time.monotonic(), str(text)))
        return await original_send(text, **kwargs)

    handler._send_md = _recording_send
    ledger = tmp_path / "monitor.jsonl"
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    marks: dict = {}

    async def _drive():
        # The launch preflight pings the channel and waits for a reply: answer it the way the
        # bot receives one, a Telegram message replying to the ping.
        while not handler._pending:
            await asyncio.sleep(0.2)
        ping = next(iter(handler._pending))
        await handler.app.update_queue.put(_reply(handler.app.bot, ping, 900000, handler.chat_id))
        while not ledger.exists() or "reviewer.review.start" not in ledger.read_text():
            await asyncio.sleep(0.5)
        marks["started"] = sorted({p.pid for p in psutil.Process().children(recursive=True)} - before)
        app = handler.app
        await app.update_queue.put(_update(app.bot, "/status what is the run doing?", 900001,
                                           handler.chat_id))
        while not any("/status received" in t for _, t in sent):
            await asyncio.sleep(0.1)
        if wait_for_answer:
            acked = next(t for t, x in sent if x.startswith(STATUS_ACK))
            while not [t for t, x in sent if t > acked and not _is_ack(x)]:
                await asyncio.sleep(0.5)
        await app.update_queue.put(_update(app.bot, "/stop", 900002, handler.chat_id))
        marks["stop_put"] = time.monotonic()

    async def _main():
        driver = asyncio.ensure_future(_drive())
        try:
            await run_research(
                ResearchRequest(question="What is the spin of the black hole in GX 339-4?"),
                human_handler=handler, monitor_jsonl=ledger, chatter=10,
            )
        finally:
            driver.cancel()
            marks["cancelling"] = asyncio.current_task().cancelling()

    with seated(reviewer=seat("anthropic", "claude-sonnet-5"),
                status_monitor=seat("anthropic", "claude-sonnet-5")):
        with pytest.raises(UserInterruptionError) as caught:
            asyncio.run(asyncio.wait_for(_main(), timeout=900))

    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    texts = [t for _, t in sent]
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"stop_telegram_{stamp}.jsonl").write_text(
        "\n".join(json.dumps({"t": t, "text": x}) for t, x in sent) + "\n", encoding="utf-8")

    status_ack = next(t for t, x in sent if "/status received" in x)
    stop_ack = next(t for t, x in sent if "/stop received, cleaning up" in x)
    checklist = next(x for x in texts if "/stop landed" in x)
    status_answer = [t for t, x in sent if t > status_ack and not _is_ack(x)
                     and "/stop landed" not in x]
    survivors = [pid for pid in marks.get("started", []) if _alive(pid)]
    (LOG_DIR / f"stop_telegram_{stamp}.md").write_text("\n".join([
        "# Live /status + /stop through Telegram (D8)",
        f"- caller got: {type(caught.value).__name__}, artifacts {caught.value.artifacts}",
        f"- status ack +0s; stop ack {stop_ack - status_ack:+.1f}s; status answer "
        + (f"{status_answer[0] - status_ack:+.1f}s" if status_answer else "never (cancelled at close)"),
        f"- processes started by the run: {marks.get('started')}; surviving: {survivors}",
        "", "## Checklist", checklist, "",
        "## Teardown steps", *[f"- {e['event']} {e['data'].get('step', '')} "
                               f"{e['data'].get('reason', e['data'].get('error', ''))}"
                               for e in events if e["event"].startswith("run.interrupt.step")],
    ]) + "\n", encoding="utf-8")

    assert caught.value.artifacts.get("ledger") and marks["cancelling"] == 0
    if wait_for_answer:
        # The monitor's answer reached the chat before the stop was sent, and nothing was left
        # running for the stop to cancel.
        assert status_answer and status_answer[0] < stop_ack
        assert "cancelled by /stop" not in checklist
    else:
        assert stop_ack - status_ack < 30  # `/stop` did not wait for the status investigation
        # `/status` was still investigating when the stop landed, and the checklist says so.
        assert not status_answer and "/status answer – cancelled by /stop" in checklist
    assert checklist.rstrip().endswith("cleanup complete"), checklist
    names = [e["event"] for e in events]
    assert names[-1].startswith("run.interrupt.step") and events[-1]["data"]["step"] == "hitl_shutdown"
    assert marks.get("started") and not survivors
