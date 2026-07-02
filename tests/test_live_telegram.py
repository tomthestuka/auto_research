"""Opt-in live Telegram round-trip (CLAUDE.md §9.8 / §10 live-API stub).

    AUTO_RESEARCH_LIVE_TELEGRAM=1 pytest tests/test_live_telegram.py -s

Sends a real Markdown ``HumanQuery`` to your Telegram chat over long polling and **blocks for
your reply**, then records the exchange under ``logs/dev/``. Requires the Telegram env keys
(``TELEGRAM_BOT_TOKEN`` from @BotFather and ``TELEGRAM_CHAT_ID``) and an installed
``python-telegram-bot``. Never runs in the default suite.

Telegram is decoupled from the LLM provider keys: unlike ``test_live_whatsapp.py`` this stub
does not call ``check_required_env()`` (which requires the model/ADS keys), so the channel can
be exercised with only the two Telegram vars set. The sender-model label is resolved
best-effort and left blank if the model registry is unavailable.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.schemas import HumanQuery

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_TELEGRAM") != "1",
    reason="set AUTO_RESEARCH_LIVE_TELEGRAM=1 to send a real Telegram clarification",
)


_ROUND_TRIP_MD = """\
## auto_research live test

A **bold** claim with `inline code` for an ObsID like `02008401`, and a fenced block:

```python
recovered = (q**2 + u**2) ** 0.5
```

Reply to this message with any text to confirm the Telegram channel is live.

_source: tests/test_live_telegram.py_
"""


def test_live_telegram_round_trip():
    load_dotenv()  # surface the configured Telegram creds (never read .env here, just load it)
    from auto_research.hitl import TelegramHandler

    # Best-effort sender tag so the message header shows the "<role> (<model>)" label a real
    # run would; cosmetic, so any resolution failure leaves it blank.
    model = ""
    try:
        from auto_research.models import resolve

        model = resolve("analyst_worker").handles[0].config.model
    except Exception:  # noqa: BLE001 — the label is cosmetic; never block the send
        pass

    query = HumanQuery(
        kind="clarify_science",
        asked_by="analyst_worker",
        model=model,
        message=_ROUND_TRIP_MD,
    )

    async def _round_trip() -> "object":
        handler = TelegramHandler(timeout_s=600)  # start()/polling run inside the loop
        try:
            return await handler(query)
        finally:
            await handler.aclose()

    response = asyncio.run(_round_trip())

    assert response.answer.strip(), "no answer text returned"

    log_dir = Path("logs/dev")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"telegram_roundtrip_{stamp}.jsonl").write_text(
        json.dumps(
            {
                "query": query.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
