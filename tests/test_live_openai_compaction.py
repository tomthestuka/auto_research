"""Opt-in live probe: does OpenAI's server-side (inline) compaction serve our turn shape? (B13)

Ruling B13 (amended 2026-10-18): use a provider's server-side compaction where it works, our own
segment boundary elsewhere; OpenAI's is used only once this probe passes. Two reasons it might not:
in August every request where compaction fired returned a 500 (undocumented, re-probed here), and
openai-python#3075 reports that no compaction is emitted on tool-call-only turns — our ReAct shape.

Docs (developers.openai.com/api/docs/guides/compaction, read 2026-10-18): enable with
``context_management=[{"type": "compaction", "compact_threshold": N}]``; with ``store=False`` the
output includes an encrypted compaction item, which is appended to the next input as is.

What it checks, on the configured GPT worker member (est. < $0.10):
  1. a request over a low threshold, ending on a tool-call-only turn, returns without a 5xx;
  2. a compaction item is emitted on that turn;
  3. the next request, replaying the output (compaction item included), is accepted.
Every row is written and flushed as it happens.

    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_openai_compaction.py -s
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)

_FILLER = "The observed count rate was steady across the interval. " * 120
_TOOL = {
    "type": "function", "name": "fit", "description": "Fit one spectrum segment.",
    "parameters": {"type": "object", "properties": {"i": {"type": "integer"}},
                   "required": ["i"], "additionalProperties": False},
    "strict": True,
}


def _items():
    """A tool-call-only trajectory: eight fit calls, each answered with a long output."""
    items = [{"role": "user", "content": [{"type": "input_text", "text": (
        "Fit segments 0 to 9 one call at a time with the fit tool, one call per turn, no prose.")}]}]
    for i in range(8):
        items.append({"type": "function_call", "call_id": f"call_{i}", "name": "fit",
                      "arguments": json.dumps({"i": i})})
        items.append({"type": "function_call_output", "call_id": f"call_{i}",
                      "output": f"segment {i}: index 1.7. {_FILLER}"})
    return items


@pytest.mark.parametrize("role, draw", [
    (role, draw) for role in ("analyst_worker", "reviewer") for draw in (1, 2)
])
def test_live_openai_inline_compaction_on_a_tool_call_only_turn(role, draw):
    """One draw per case; a seat qualifies only when every draw emits (openai-python#3075 reports
    the item missing intermittently)."""
    from openai import AsyncOpenAI

    from auto_research.models import resolve
    from auto_research.models.keys import resolve_key

    load_dotenv()
    member = next(h.config for h in resolve(role).handles if h.config.provider == "openai")
    from auto_research.models.keys import key_pool, make_rotator

    rotate = make_rotator("openai")

    key, _slot = resolve_key("openai", member.key_env or "OPENAI_API_KEY")
    state = {"client": AsyncOpenAI(api_key=key or None, max_retries=0, timeout=300)}
    log = dev_log_path("compaction", f"live_openai_compaction_{role}_{draw}", ".jsonl")
    handle = log.open("w", encoding="utf-8")

    def write(row):
        handle.write(json.dumps(row, default=str) + "\n")
        handle.flush()

    async def call(items):
        async def once():
            return await state["client"].responses.create(
                model=member.model, input=items, tools=[_TOOL], tool_choice="required",
                store=False, include=["reasoning.encrypted_content"],
                extra_body={"context_management": [
                    {"type": "compaction", "compact_threshold": 2000}
                ]},
            )
        try:
            return await once()
        except Exception as exc:  # noqa: BLE001 — a spent key moves to the fallback, as production does
            if "insufficient_quota" not in str(exc):
                raise
            # The production path: retire the spent key and rotate (`keys.make_rotator`).
            label = rotate(True)
            write({"step": "key", "note": "key spent; rotated", "label": label})
            if label is None:
                raise
            state["client"] = AsyncOpenAI(api_key=key_pool("openai").active(), max_retries=0,
                                          timeout=300)
            return await once()

    async def run():
        items = _items()
        try:
            first = await call(items)
        except Exception as exc:  # noqa: BLE001 — the probe records the failure it measures
            write({"step": "first", "error": repr(exc), "status": getattr(exc, "status_code", None)})
            raise
        types = [getattr(o, "type", None) for o in first.output]
        write({"step": "first", "output_types": types, "usage": first.usage.model_dump()})
        replay = items + [o.model_dump(exclude_none=True) for o in first.output]
        for o in first.output:
            if getattr(o, "type", None) == "function_call":
                replay.append({"type": "function_call_output", "call_id": o.call_id,
                               "output": f"segment 8: index 1.8. {_FILLER}"})
        second = await call(replay)
        write({"step": "second", "output_types": [getattr(o, "type", None) for o in second.output],
               "usage": second.usage.model_dump()})
        return types, [getattr(o, "type", None) for o in second.output]

    try:
        first_types, second_types = asyncio.run(run())
    finally:
        handle.close()
        print(f"\n[openai-compaction] {log}")
    assert "function_call" in first_types, "the probe needs a tool-call-only turn"
    assert any("compaction" in str(t) for t in first_types + second_types), (
        "no compaction item on a tool-call-only turn (openai-python#3075)"
    )
