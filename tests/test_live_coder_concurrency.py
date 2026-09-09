"""Opt-in LIVE probe: how many concurrent DeepSeek/Codex author sessions actually hold?

    AUTO_RESEARCH_LIVE_CODER_CONCURRENCY=1 pytest tests/test_live_coder_concurrency.py -q -s

**Why the coder is the seat this question is really about.** Every other seat in a trial is one
HTTP call on a shared client, and a trial's pipeline is sequential — workers run serially, then the
lead, then the reviewer — so N concurrent trials put at most N chat calls in flight. The author is
different in kind: it is a **real subprocess** with its own app-server, its own jail, its own
temp home and its own vendor session. Five concurrent trials therefore mean five concurrent Codex
processes, and the ceiling could be the vendor's session limit, the account's rate limit, the
jail's resources, or the app-server's own startup — four different failures that all present as
"the batch got slower".

`author_bench` ran DeepSeek at concurrency four with nothing recorded against it, so five is one
step past measured ground. That is exactly the sort of inherited number worth re-deriving rather
than repeating.

**Built on the production path, not a hand-rolled approximation** (the 2026-09-08 lesson): the
backend, the jail policy, the escalation server, the path guard and the author environment all come
from `test_live_coder_matrix`'s own builder, which is the flow's builder. Each session gets its own
throwaway toolkit in the §9.2 shape and its own git repo; nothing touches the configured
`toolkit_root`.

**What it measures.** One session alone as a baseline, then `FANOUT` at once, each asked to do the
same small real piece of work: read the manifest and add one function to the human half. Recorded
per session: whether the process came up, whether the instruction returned text, whether the
working tree actually changed (the only proof the author *worked* rather than merely answered),
how long it took, and any vendor refusal verbatim. Model quality is never asserted.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import pytest
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_CODER_CONCURRENCY") != "1",
    reason="set AUTO_RESEARCH_LIVE_CODER_CONCURRENCY=1 to spend tokens on concurrent authors",
)

load_dotenv()

#: Concurrent author sessions to open. Five is what the science demonstration asks for if all five
#: remaining trials run in one batch, and one more than `author_bench` has ever run.
FANOUT = int(os.getenv("AUTO_RESEARCH_CODER_FANOUT", "5"))

#: One small, real piece of work. Small so five of them are cheap; real so the session has to read
#: files, think and edit, which is what exercises a vendor session rather than a single round trip.
INSTRUCTION = (
    "Read demo_utils/skills/__init__.py and demo_utils/lib/demo_help.py. Add ONE new function "
    "`row_count(path: str) -> dict` to demo_utils/lib/demo_help.py that returns "
    "{'path': path, 'rows': 0} without reading the file, and register it in TOOL_MANIFEST as "
    "'demo.row_count' mirroring the existing entry. Do not run git. Reply with one sentence when "
    "the edit is on disk."
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True
    ).stdout.strip()


def _coder_member():
    """The demonstration's pinned author, straight off the sweep roster."""
    from auto_research.models.types import RoleModelConfig
    from benchmarks.science_bench import arms

    return RoleModelConfig(
        name="coder_concurrency_probe",
        provider=arms.CODER.provider,
        model=arms.CODER.model,
        harness=arms.CODER_HARNESS,
        base_url=arms.CODER_BASE_URL,
        key_env=arms.CODER_KEY_ENV,
    )


def _world(index: int) -> Path:
    """One session's private throwaway toolkit, in the §9.2 shape, as its own git repo.

    An opaque directory name: a per-session label would be the 2026-08-27 leak shape if this ever
    became a measurement an actor could read.
    """
    from tests.test_live_coder_matrix import _mini_toolkit

    root = Path(mkdtemp(prefix="cc-"))
    _mini_toolkit(root)
    return root


class _Ledger:
    """Collects the events the backend records, so a refusal is read rather than inferred."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record(self, actor: str, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs.get("data") or {}))

    def named(self, needle: str) -> list[dict]:
        return [data for event, data in self.events if needle in event]


async def _one_session(index: int) -> dict[str, Any]:
    """Bring one author up on the production path, send one instruction, take it down."""
    from tests.test_live_coder_matrix import _make_backend

    class _Leg:
        harness = "codex"
        provider = "deepseek"
        model = _coder_member().model

        @staticmethod
        def member():
            return _coder_member()

    root = _world(index)
    ledger = _Ledger()

    async def _ask_lead(question: str) -> str:
        return "Proceed on your own judgement; this is a concurrency probe."

    async def _ask_human(query: Any) -> Any:  # pragma: no cover - not expected to fire
        raise RuntimeError("no human is wired in a concurrency probe")

    started = time.monotonic()
    row: dict[str, Any] = {"session": index, "connected": False, "replied": False}
    backend = None
    try:
        backend = _make_backend(_Leg(), root, ledger, _ask_lead, _ask_human)
        await backend.connect()
        row["connected"] = True
        row["connect_s"] = round(time.monotonic() - started, 2)
        reply = await backend.drive(INSTRUCTION)
        row["replied"] = bool((reply or "").strip())
        row["reply_chars"] = len(reply or "")
        # The only proof the author *worked* rather than answered: the tree moved.
        row["tree_changed"] = bool(_git(root, "status", "--porcelain").strip())
        row["row_count_present"] = "row_count" in (
            root / "demo_utils" / "lib" / "demo_help.py"
        ).read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — a refusal is the measurement
        row["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception:  # noqa: BLE001 — teardown never fails a measured cell
                pass
        shutil.rmtree(root, ignore_errors=True)

    row["seconds"] = round(time.monotonic() - started, 2)
    row["vendor_refusals"] = [
        data for data in ledger.named("error") if "429" in str(data) or "Access" in str(data)
    ][:2]
    return row


def test_live_coder_concurrency_ceiling():
    """One author alone, then `FANOUT` at once. Records the ceiling; asserts only that it ran."""
    if not os.getenv(_coder_member().key_env or "DEEPSEEK_API_KEY"):
        pytest.skip(f"{_coder_member().key_env} is not set")

    async def _run() -> dict[str, Any]:
        alone = await _one_session(0)
        together = await asyncio.gather(
            *[_one_session(i + 1) for i in range(FANOUT)], return_exceptions=False
        )
        return {"alone": alone, "together": list(together)}

    result = asyncio.run(_run())
    alone, together = result["alone"], result["together"]

    worked = [row for row in together if row.get("tree_changed")]
    times = [row["seconds"] for row in together if row.get("replied")]
    summary = {
        "model": _coder_member().model,
        "harness": _coder_member().harness,
        "fanout": FANOUT,
        "alone": alone,
        "connected": sum(1 for row in together if row["connected"]),
        "replied": sum(1 for row in together if row["replied"]),
        "tree_changed": len(worked),
        "median_s": round(statistics.median(times), 2) if times else None,
        "max_s": max(times) if times else None,
        "slowdown": (
            round(statistics.median(times) / (alone.get("seconds") or 0.01), 2) if times else None
        ),
        "errors": [row["error"] for row in together if row.get("error")][:5],
        "sessions": together,
    }

    path = dev_log_path("coder-concurrency", f"fanout{FANOUT}", ".json")
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"\n[coder-concurrency] {summary['model']} on {summary['harness']}, fanout {FANOUT}\n"
        f"  alone      : connected={alone['connected']} replied={alone['replied']} "
        f"tree_changed={alone.get('tree_changed')} {alone.get('seconds')}s\n"
        f"  at {FANOUT} at once: connected={summary['connected']}/{FANOUT} "
        f"replied={summary['replied']}/{FANOUT} tree_changed={summary['tree_changed']}/{FANOUT}\n"
        f"  median {summary['median_s']}s  max {summary['max_s']}s  "
        f"slowdown {summary['slowdown']}x"
    )
    for error in summary["errors"]:
        print(f"    ! {error}")
    print(f"  -> {path}")

    # A ceiling is a fact to learn, so the only assertion is that the probe itself ran: one author
    # alone must have come up, else the measurement says nothing about concurrency.
    assert alone["connected"], f"a single author could not start: {alone.get('error')}"
