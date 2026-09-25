"""Opt-in live attack on the Telegram renderer: replay an adversarial Markdown corpus (E8, 2026-09-25).

    AUTO_RESEARCH_LIVE_TELEGRAM_ATTACK=1 pytest tests/test_live_telegram_attack.py -s

The corpus (``tests/fixtures/telegram_attack_payloads.json``) was written by an attack agent aimed
at the renderer's known weak points: rich-message limits, chunk boundaries, escaping, math, table
shapes, Unicode, entity auto-detection. Each payload goes through the production send path
(``_query_markdown`` → ``TelegramHandler._send_md``, what a real question sends) to the configured chat, one after another, and the test records for
each one which path carried it (rich message, or the HTML fallback and how many messages) and any
exception. A human judges the rendering in the chat; the test asserts only what a machine can:

* no payload raises out of the send path (a send that cannot render still sends something);
* every payload produced at least one accepted message.

Optional: ``AUTO_RESEARCH_TELEGRAM_ATTACK_IDS=A01,A07`` replays a subset.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_TELEGRAM_ATTACK") != "1",
    reason="set AUTO_RESEARCH_LIVE_TELEGRAM_ATTACK=1 to send the attack corpus to Telegram",
)

_CORPUS = Path(__file__).parent / "fixtures" / "telegram_attack_payloads.json"
_PAUSE_S = 1.2  # stay under Telegram's ~1 message/second per chat


def test_live_telegram_attack_corpus():
    load_dotenv()
    from auto_research.hitl.telegram import TelegramHandler, _query_markdown
    from auto_research.schemas import HumanQuery

    corpus = json.loads(_CORPUS.read_text(encoding="utf-8"))
    wanted = {s.strip() for s in os.getenv("AUTO_RESEARCH_TELEGRAM_ATTACK_IDS", "").split(",") if s.strip()}
    if wanted:
        corpus = [p for p in corpus if p["id"] in wanted]
    results: list[dict] = []

    async def _run() -> None:
        handler = TelegramHandler()
        await handler.start()
        sent_html = handler._send
        counts = {"html": 0}

        async def _counting_send(*a, **k):
            counts["html"] += 1
            return await sent_html(*a, **k)

        handler._send = _counting_send  # observe the fallback without changing it
        try:
            for payload in corpus:
                counts["html"] = 0
                row = {"id": payload["id"], "name": payload["name"],
                       "chars": len(payload["markdown"])}
                try:
                    # Exactly what a question sends: header + body + hint, through `_send_md`.
                    query = HumanQuery(kind="clarify_science", asked_by="analyst_lead",
                                       model="attack", message=payload["markdown"])
                    messages = await handler._send_md(_query_markdown(query))
                    row.update(ok=True, messages=len(messages), html_messages=counts["html"],
                               path="html" if counts["html"] else "rich")
                except Exception as exc:  # noqa: BLE001 — recorded, then asserted below
                    row.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
                results.append(row)
                print(f"{row['id']:>4} {row.get('path', 'ERROR'):>5} "
                      f"msgs={row.get('messages', 0)} {row['name']}")
                await asyncio.sleep(_PAUSE_S * max(1, row.get("messages", 1)))
        finally:
            await handler.aclose()

    asyncio.run(_run())

    log_dir = Path("logs/dev/hitl")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"telegram_attack_{stamp}.json").write_text(json.dumps(results, indent=2))

    failed = [r for r in results if not r["ok"]]
    assert not failed, failed
    assert all(r["messages"] >= 1 for r in results)
