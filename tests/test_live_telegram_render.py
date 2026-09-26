"""Opt-in live check of the Telegram rich-message path and its HTML fallback (E8, 2026-09-25).

    AUTO_RESEARCH_LIVE_TELEGRAM=1 pytest tests/test_live_telegram_render.py -s

Sends one Markdown-rich question to the configured chat twice: once through ``sendRichMessage``
(native headings, table, math, details) and once through the forced HTML fallback (aligned
``<pre>`` table, collapsed quote, heading-safe chunks). Asserts both sends were accepted by the
real Bot API and records the message ids under ``logs/dev/hitl/``; how they LOOK is for the human
to judge in the chat.

With ``AUTO_RESEARCH_LIVE_TELEGRAM_REPLY=1`` it also waits (up to 10 min) for a reply to a real
question, so the round trip and the edit notice can be exercised by hand: reply, then edit it.

``test_live_telegram_attachments`` (E11) sends a figure and a PDF embedded in one rich message and
again as photo/document replies; with ``…_REPLY=1`` it asks for a photo or file back and checks the
connector saved it under ``logs/dev/hitl/replies/``.
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
    os.getenv("AUTO_RESEARCH_LIVE_TELEGRAM") != "1",
    reason="set AUTO_RESEARCH_LIVE_TELEGRAM=1 to send real Telegram messages",
)

_MD = """\
# Render check · rich message

A **bold** claim, *italics*, `02008401`, and inline math $x^2 + y^2$.

## Measured values

| Band | PD (%) | PA (deg) | Reference |
|:-----|------:|--------:|:----------|
| 2-3 keV | 3.5 ± 0.3 | -20.7 | Table S2 |
| 3-4 keV | 3.5 ± 0.3 | -21.1 | Table S2 |
| 2-8 keV | 4.0 ± 0.2 | -20.7 | Table 2 |

$$\\mathrm{PD} = \\sqrt{Q^2 + U^2} / I$$

> The question: should the joint fit keep IXPE as data group 1?
> It diverges otherwise on this source.
> Two runs agree on the sign.
> A fourth line so the fallback collapses this quote.

<details><summary>Method detail</summary>

- polconst over 2-8 keV
- errors from the covariance matrix

</details>

```python
fit = load_data(obsid="02008401")
```
"""


def _query(message: str) -> HumanQuery:
    return HumanQuery(kind="clarify_science", asked_by="analyst_lead", model="", message=message)


def test_live_telegram_rich_and_fallback_renders():
    load_dotenv()
    from auto_research.hitl.telegram import TelegramHandler, _query_markdown

    results: dict = {}

    async def _run() -> None:
        handler = TelegramHandler()
        await handler.start()
        try:
            md = _query_markdown(_query(_MD))
            rich = await handler._send_rich(md)
            results["rich"] = {"accepted": rich is not None,
                               "message_id": getattr(rich, "message_id", None),
                               "rich_enabled_after": handler._rich}
            handler._rich = False  # force the fallback for the second copy
            sent = await handler._send_md(md.replace("rich message", "HTML fallback"))
            results["fallback"] = {"message_ids": [m.message_id for m in sent]}
            handler._rich = True
            if os.getenv("AUTO_RESEARCH_LIVE_TELEGRAM_REPLY") == "1":
                handler.timeout_s = 600
                answer = await handler(_query("Reply to this message, then edit your reply."))
                results["round_trip"] = {"answer": answer.answer}
                await asyncio.sleep(60)  # time to edit the reply and see the notice
        finally:
            await handler.aclose()

    asyncio.run(_run())

    log_dir = Path("logs/dev/hitl")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"telegram_render_{stamp}.json").write_text(json.dumps(results, indent=2))
    print("\nTelegram render results:", json.dumps(results, indent=2))

    assert results["rich"]["accepted"], results["rich"]
    assert results["fallback"]["message_ids"]


def _attachments(tmp: Path) -> list:
    from PIL import Image, ImageDraw

    from auto_research.schemas import HumanAttachment

    tmp.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (800, 500), "white")
    draw = ImageDraw.Draw(img)
    draw.line([(60, 440), (740, 60)], fill="navy", width=5)
    draw.text((70, 30), "E11 render check: PD vs energy", fill="black")
    png = tmp / "pd_vs_energy.png"
    img.save(png)
    pdf = tmp / "e11_check.pdf"
    img.save(pdf, "PDF")
    return [
        HumanAttachment(handle="analyst_worker_0:f1", path=png, filename=png.name,
                        mime_type="image/png", caption="PD vs energy (test figure)"),
        HumanAttachment(handle="lib:p1", path=pdf, filename=pdf.name,
                        mime_type="application/pdf", caption="test PDF"),
    ]


def test_live_telegram_attachments(tmp_path):
    load_dotenv()
    from auto_research.hitl.telegram import TelegramHandler

    results: dict = {}
    atts = _attachments(tmp_path)

    async def _run() -> None:
        handler = TelegramHandler()
        await handler.start()
        try:
            rich = await handler._send_rich(
                "**E11 check** · attachments embedded in one rich message", attachments=tuple(atts)
            )
            results["embedded"] = {"accepted": rich is not None,
                                   "message_id": getattr(rich, "message_id", None)}
            head = await handler._send("<b>E11 check</b> · attachments as replies (fallback path)")
            sent = await handler._send_files(tuple(atts), head)
            results["replies"] = {"message_ids": [m.message_id for m in sent]}
            if os.getenv("AUTO_RESEARCH_LIVE_TELEGRAM_REPLY") == "1":
                handler.timeout_s = 600
                reply_dir = Path("logs/dev/hitl/replies")
                answer = await handler(HumanQuery(
                    kind="clarify_science", asked_by="analyst_lead", reply_dir=reply_dir,
                    message="Reply to this message with a photo or a file (add a caption).",
                    attachments=atts[:1],
                ))
                results["round_trip"] = {
                    "answer": answer.answer, "notes": answer.notes,
                    "files": [str(a.path) for a in answer.attachments],
                }
        finally:
            await handler.aclose()

    asyncio.run(_run())

    log_dir = Path("logs/dev/hitl")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"telegram_attachments_{stamp}.json").write_text(json.dumps(results, indent=2))
    print("\nTelegram attachment results:", json.dumps(results, indent=2))

    assert results["embedded"]["accepted"], results
    assert len(results["replies"]["message_ids"]) == 2
    if "round_trip" in results:
        assert results["round_trip"]["files"], results["round_trip"]
        assert all(Path(f).is_file() for f in results["round_trip"]["files"])
