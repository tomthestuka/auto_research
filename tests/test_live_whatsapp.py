"""Opt-in live WhatsApp round-trip (CLAUDE.md §9.8 / §10 live-API stub).

    AUTO_RESEARCH_LIVE_WHATSAPP=1 pytest tests/test_live_whatsapp.py -s

Sends a real ``HumanQuery`` to your WhatsApp via Twilio and **blocks for your reply**, then
records the exchange under ``logs/dev/``. Requires the Twilio WhatsApp env keys
(TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM / WHATSAPP_TO) and that you have
joined the Twilio WhatsApp sandbox (or own the number). Never runs in the default suite.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from auto_research.env import check_required_env
from auto_research.schemas import HumanQuery

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_WHATSAPP") != "1",
    reason="set AUTO_RESEARCH_LIVE_WHATSAPP=1 to send a real WhatsApp clarification",
)


def test_live_whatsapp_round_trip():
    check_required_env()
    from auto_research.hitl import WhatsAppHandler

    handler = WhatsAppHandler(timeout_s=600, poll_interval_s=5)
    query = HumanQuery(
        kind="clarify_science",
        asked_by="analyst_worker",
        question="auto_research live test — reply with any text to confirm the channel.",
        context="tests/test_live_whatsapp.py",
    )
    response = asyncio.run(handler(query))

    assert response.answer.strip(), "no answer text returned"

    log_dir = Path("logs/dev")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"whatsapp_roundtrip_{stamp}.jsonl").write_text(
        json.dumps({"query": query.model_dump(mode="json"),
                    "response": response.model_dump(mode="json")}, default=str) + "\n",
        encoding="utf-8",
    )
