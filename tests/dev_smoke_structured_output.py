"""One-off live smoke for native structured output + function calling.

Exercises both provider mechanisms against each configured provider with tiny
prompts. Opt-in (spends a few tokens). Writes a JSONL + Markdown log under
logs/dev/structured-output/.

    AUTO_RESEARCH_LIVE_STRUCTURED_SMOKE=1 \\
      /grad/fzhou/.conda/envs/auto_research/bin/python tests/dev_smoke_structured_output.py
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

from auto_research.models import load_registry
from auto_research.models.types import ModelMessage


class Verdict(BaseModel):
    accepted: bool
    rationale: str


_TOOL = {
    "name": "record_measurement",
    "description": "Record a measurement value and its unit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "value": {"type": "number", "description": "the numeric value"},
            "unit": {"type": "string", "description": "the unit, e.g. keV"},
        },
        "required": ["value", "unit"],
    },
}


async def main() -> None:
    load_dotenv()
    if os.getenv("AUTO_RESEARCH_LIVE_STRUCTURED_SMOKE") != "1":
        raise SystemExit("set AUTO_RESEARCH_LIVE_STRUCTURED_SMOKE=1 to spend tokens")

    out_dir = Path(__file__).resolve().parents[1] / "logs" / "dev" / "structured-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    jsonl = out_dir / f"{stamp}_smoke.jsonl"
    md = out_dir / f"{stamp}_smoke.md"

    registry = load_registry()
    rows: list[dict] = []

    # One handle per provider (reviewer group spans all three).
    handles = {h.config.provider: h for h in registry.resolve("reviewer").handles}

    for provider, handle in handles.items():
        # 1) structured output
        try:
            resp = await handle.call(
                [
                    ModelMessage(role="system", content="You are a terse reviewer."),
                    ModelMessage(role="user", content="Reply that the result is acceptable, one short reason."),
                ],
                schema=Verdict,
            )
            v = Verdict.model_validate(resp.parsed)
            rows.append({
                "provider": provider, "model": handle.config.model, "mode": "structured_output",
                "ok": True, "parsed": resp.parsed, "validated_accepted": v.accepted,
                "usage": resp.usage.model_dump(),
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"provider": provider, "model": handle.config.model,
                         "mode": "structured_output", "ok": False, "error": repr(exc)})

        # 2) function calling
        try:
            resp = await handle.call(
                [
                    ModelMessage(role="system", content="Use the provided tool to record the measurement."),
                    ModelMessage(role="user", content="Record a measurement of 2.5 keV using the tool."),
                ],
                tools=[_TOOL],
                tool_choice="required",
            )
            calls = [{"name": c.name, "arguments": c.arguments} for c in resp.tool_calls]
            rows.append({
                "provider": provider, "model": handle.config.model, "mode": "function_calling",
                "ok": bool(calls), "tool_calls": calls, "usage": resp.usage.model_dump(),
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"provider": provider, "model": handle.config.model,
                         "mode": "function_calling", "ok": False, "error": repr(exc)})

    with jsonl.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    lines = ["# Native structured-output + function-calling live smoke", "", f"- {stamp}", ""]
    for r in rows:
        status = "✅" if r.get("ok") else "❌"
        lines.append(f"## {status} {r['provider']} / {r['model']} — {r['mode']}")
        if r.get("ok"):
            detail = r.get("parsed") if r["mode"] == "structured_output" else r.get("tool_calls")
            lines.append(f"```json\n{json.dumps(detail, indent=2)}\n```")
        else:
            lines.append(f"ERROR: {r.get('error')}")
        lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")

    print(f"Wrote {jsonl}")
    print(f"Wrote {md}")
    for r in rows:
        print(("OK  " if r.get("ok") else "FAIL") + f" {r['provider']:9s} {r['mode']}")


if __name__ == "__main__":
    asyncio.run(main())
