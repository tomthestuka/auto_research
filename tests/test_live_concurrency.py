"""Opt-in LIVE probe: how many concurrent streams does each vendor in a batch actually hold?

    AUTO_RESEARCH_LIVE_CONCURRENCY=1 pytest tests/test_live_concurrency.py -q -s

**Why this exists.** A batch's concurrency is a per-vendor question, and getting it wrong is not a
slow batch, it is a **corrupted** one: Z.AI throttled 87 to 97 per cent of turns above two in
flight, which voided every GLM trial run at concurrency three or four. That ceiling was discovered
by losing trials. The two published batches carry ceilings for Anthropic, OpenAI, Z.AI and
DeepSeek, but the reasons behind them are not interchangeable — the Claude cap of two is the
*coder's subscription session cap*, shared across concurrent author subprocesses, and says nothing
about an API-keyed chat role — and **Alibaba has never been measured at all**, while the science
demonstration puts two Qwen seats in every trial of one tree.

So this measures, cheaply, before a batch spends: N concurrent real calls per vendor, timed, with
throttling counted rather than inferred from a slow wall clock. It costs cents, it is re-runnable,
and it answers the only question that matters — does adding a trial make each vendor's calls
slower, or does it make them fail.

**What it asserts and what it records.** It asserts only that the calls came back at all, since a
vendor's ceiling is a fact to *learn*, not a property to require. The measurement lands under
``logs/dev/concurrency/`` and prints a table, and the roster's ``max_concurrency`` is set from what
it shows. A slowdown ratio near one means headroom; a ratio far above one, or any refusal, is a
ceiling.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from typing import Any

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from tests.dev_logs import dev_log_path
from tests.seats import from_seat, seated

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_CONCURRENCY") != "1",
    reason="set AUTO_RESEARCH_LIVE_CONCURRENCY=1 to spend a few cents measuring vendor ceilings",
)

load_dotenv()

#: Streams to open per vendor. Six is what the demonstration's contrast tree asks of Alibaba at
#: concurrency three (two Qwen seats per trial), which is the number in question.
FANOUT = 6

#: A prompt long enough that a throttled call is visibly slower than an unthrottled one, and short
#: enough that six of them cost cents. It asks for structure so the reply travels the same path a
#: real seat's does.
ASK = (
    "In two sentences, state what a polarization degree of 4 per cent at 2-8 keV implies about "
    "the geometry of an X-ray corona. Then give your confidence as a single word."
)


class Reply(BaseModel):
    answer: str
    confidence: str


def _vendors() -> list[tuple[str, Any]]:
    """Every vendor the demonstration's two trees actually call, one representative seat each."""
    from benchmarks.science_bench import arms

    seen: dict[str, Any] = {}
    for lead, reviewer, workers, _ in arms.DEMO_TREES:
        arm = arms.arm(lead, reviewer, workers)
        for seat in (*arm.seats(), *arm.workers.members):
            # The coder is an author subprocess, not a chat member, so it has no handle here.
            if seat.provider == arm.coder.provider:
                continue
            seen.setdefault(seat.provider, seat)
    return sorted(seen.items())


async def _timed_call(handle: Any) -> dict[str, Any]:
    from auto_research.models.types import ModelMessage

    started = time.monotonic()
    try:
        response = await handle.call([ModelMessage(role="user", content=ASK)], schema=Reply)
        return {
            "seconds": round(time.monotonic() - started, 2),
            "ok": response.parsed is not None,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — a refusal is the measurement, not a test failure
        return {
            "seconds": round(time.monotonic() - started, 2),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


def _measure(seat: Any, fanout: int) -> dict[str, Any]:
    """One call alone, then ``fanout`` at once, through the production handle."""
    from auto_research.models import load_registry

    async def _run() -> dict[str, Any]:
        handle = load_registry().resolve("analyst_worker").handles[0]
        alone = await _timed_call(handle)
        together = await asyncio.gather(*[_timed_call(handle) for _ in range(fanout)])
        return {"alone": alone, "together": list(together)}

    # Seated as a *worker*, the seat with no special wiring, so what is measured is the vendor
    # rather than a role. Routed through `ModelHandle`, so key rotation and retry are in the path
    # exactly as they are in a batch (the 2026-08-25 lesson: a live test below the handle skips
    # the layer the failures live in).
    with seated(analyst_worker=from_seat(seat)):
        result = asyncio.run(_run())

    times = [row["seconds"] for row in result["together"] if row["ok"]]
    failures = [row["error"] for row in result["together"] if not row["ok"]]
    alone_seconds = result["alone"]["seconds"] or 0.01
    return {
        "provider": seat.provider,
        "model": seat.model,
        "fanout": fanout,
        "alone_s": result["alone"]["seconds"],
        "alone_ok": result["alone"]["ok"],
        "alone_error": result["alone"]["error"],
        "median_s": round(statistics.median(times), 2) if times else None,
        "max_s": max(times) if times else None,
        "slowdown": round((statistics.median(times) / alone_seconds), 2) if times else None,
        "ok": len(times),
        "failed": len(failures),
        "errors": failures[:3],
    }


def test_live_vendor_concurrency_ceilings():
    """Measure every vendor the demonstration calls, and print the ceiling evidence."""
    rows = [_measure(seat, FANOUT) for _, seat in _vendors()]

    path = dev_log_path("concurrency", f"fanout{FANOUT}", ".json")
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    print(f"\n{'provider':<12} {'model':<18} {'alone':>7} {'median':>8} {'max':>7} "
          f"{'slowdown':>9} {'ok':>4} {'fail':>5}")
    for row in rows:
        print(
            f"{row['provider']:<12} {row['model']:<18} {row['alone_s']:>7} "
            f"{str(row['median_s']):>8} {str(row['max_s']):>7} {str(row['slowdown']):>9} "
            f"{row['ok']:>4} {row['failed']:>5}"
        )
        for error in row["errors"]:
            print(f"    ! {error}")
    print(f"\n[concurrency] {path}")

    # The only assertion: every vendor answered at least once, so a zero row is a real finding
    # about that vendor rather than a missing key or a typo in the roster.
    dead = [row for row in rows if not row["ok"] and not row["alone_ok"]]
    assert not dead, f"vendors that answered nothing at all: {[r['model'] for r in dead]}"
