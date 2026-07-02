"""Opt-in live HITL render/emit across every wired channel (CLAUDE.md §9.8 / §10 live stub).

    AUTO_RESEARCH_LIVE_HITL=1 pytest tests/test_live_hitl.py -s

Emits ONE Markdown-rich ``HumanQuery`` through each wired channel so a human can eyeball how
the per-channel renderers handle Markdown:

  * **CLI** — ``cli._render`` via ``rich`` (run with ``-s`` to see it in the terminal).
  * **Slack** — a real ``chat_postMessage`` with the ``markdown``-block rendering (``_blocks``).
  * **Telegram** — a real send of the HTML-rendered chunks (``_render_chunks`` + ``_send``) over
    a briefly-started ``Application``; the recommended channel.
  * **WhatsApp** — a real Twilio send of the raw Markdown body (``_body``); **deprecated** and
    expected to fail here (missing/again-deprecated creds), which is the point of the per-channel
    try/except — one channel failing never blocks the others.

This is **send-only** (it does not wait for a reply); for a blocking round-trip see
``tests/test_live_whatsapp.py``. Observational: the exchange is logged under ``logs/dev/hitl/``;
only the always-available CLI emit is asserted, the external channels are best-effort.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.schemas import HumanQuery

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_HITL") != "1",
    reason="set AUTO_RESEARCH_LIVE_HITL=1 to emit a real Markdown clarification to every channel",
)


# A deliberately broad Markdown sample so each renderer is exercised across as much syntax as
# the surface supports (CLI/rich the most, Slack a subset, WhatsApp the least).
_KITCHEN_SINK_MD = """\
# auto_research HITL render test

A **bold** claim, some *italics*, a bit of ***bold-italic***, and ~~struck~~ text, with
`inline code` for an ObsID like `02008401`.

## Why this matters

> Blockquote: the clarification channels carry free-form Markdown now — render, don't reshape.

### A bulleted list

- first point
- second point
  - a nested sub-point
  - another, with `code`
- third point

### A numbered list

1. acquire the data
2. fit the model
3. report the result

### A fenced code block

```python
def recover_pol(stokes):
    # injection-recovery sanity check
    return (stokes.q**2 + stokes.u**2) ** 0.5
```

### A small table

| instrument | data group | note            |
|------------|------------|-----------------|
| IXPE       | 1          | load IXPE first |
| NICER      | 2          |                 |

A [link](https://example.org/auto_research) and a horizontal rule:

---

The explicit ask: **does this Markdown render cleanly on your channel?** Reply to confirm.
"""


async def _emit_cli(query: HumanQuery) -> dict:
    from auto_research.hitl.cli import _render

    _render(query)  # rich → stderr; visible with `-s`
    return {"emitted": True}


async def _emit_slack(query: HumanQuery) -> dict:
    from auto_research.hitl.slack import SlackHandler, _blocks

    handler = SlackHandler()
    await handler.start()
    try:
        posted = await handler.app.client.chat_postMessage(
            channel=handler.channel,
            text=f"[live md test] {query.headline}",
            blocks=_blocks(query),
        )
        return {"emitted": True, "ts": posted.get("ts")}
    finally:
        await handler.aclose()


async def _emit_telegram(query: HumanQuery) -> dict:
    from auto_research.hitl.telegram import TelegramHandler, _render_chunks

    handler = TelegramHandler()
    await handler.start()
    try:
        ids = [(await handler._send(chunk)).message_id for chunk in _render_chunks(query)]
        return {"emitted": True, "message_ids": ids}
    finally:
        await handler.aclose()


async def _emit_whatsapp(query: HumanQuery) -> dict:
    from auto_research.hitl.whatsapp import WhatsAppHandler  # warns: deprecated

    handler = WhatsAppHandler()
    msg = await asyncio.to_thread(
        handler.client.messages.create,
        from_=handler.from_number,
        to=handler.to_number,
        body=handler._body(query),
    )
    return {"emitted": True, "sid": getattr(msg, "sid", None)}


_CHANNELS = {
    "cli": _emit_cli,
    "slack": _emit_slack,
    "telegram": _emit_telegram,
    "whatsapp": _emit_whatsapp,
}


def test_live_hitl_emits_markdown_across_channels():
    load_dotenv()  # surface the configured Slack/Twilio creds (never read here, just loaded)

    # Best-effort sender tag so the message header shows the "<role> (<model>)" label a real
    # run would; cosmetic, so any resolution failure leaves it blank.
    model = ""
    try:
        from auto_research.models import resolve

        model = resolve("analyst_lead").handles[0].config.model
    except Exception:  # noqa: BLE001 — the label is cosmetic; never block the emit
        pass

    query = HumanQuery(
        kind="clarify_science", asked_by="analyst_lead", model=model, message=_KITCHEN_SINK_MD
    )

    results: dict[str, dict] = {}

    async def _run() -> None:
        for name, emit in _CHANNELS.items():
            try:
                results[name] = {"ok": True, **(await emit(query))}
            except Exception as exc:  # noqa: BLE001 — one channel failing never blocks the rest
                results[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    asyncio.run(_run())

    log_dir = Path("logs/dev/hitl")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"emit_{stamp}.jsonl").write_text(
        json.dumps({"query": query.model_dump(mode="json"), "results": results}, default=str)
        + "\n",
        encoding="utf-8",
    )
    (log_dir / f"emit_{stamp}.md").write_text(_KITCHEN_SINK_MD, encoding="utf-8")
    print("\nHITL emit results:", json.dumps(results, indent=2))

    # Every channel is attempted and recorded; the always-available CLI emit must succeed,
    # while the external channels (Slack/Telegram/WhatsApp) are best-effort and may
    # fail-and-be-caught.
    assert set(results) == set(_CHANNELS)
    assert results["cli"]["ok"], results["cli"]
