"""Opt-in live LangSmith + cost-ledger check.

Run with:
    AUTO_RESEARCH_LIVE_LANGSMITH_TESTS=1 pytest tests/test_live_langsmith.py -s
"""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.models import ModelMessage, load_registry
from auto_research.observability import cost_ledger_path


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_LANGSMITH_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_LANGSMITH_TESTS=1 to spend provider tokens and create LangSmith traces",
)


def _unique_handles():
    seen = set()
    registry = load_registry()
    for role in sorted(registry.config.roles):
        group = registry.resolve(role)
        for handle in group.handles:
            key = (
                handle.config.provider,
                handle.config.model,
                json.dumps(handle.config.parameters, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            yield handle


def test_live_langsmith_traces_and_cost_ledger(tmp_path, monkeypatch):
    load_dotenv()
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert os.getenv("LANGCHAIN_API_KEY"), "LANGCHAIN_API_KEY is required for live LangSmith tracing"

    prompt = [
        ModelMessage(
            role="user",
            content="Write a two-line poem about careful science. Keep it under 25 words.",
        )
    ]

    async def run_all():
        failures = []
        for handle in _unique_handles():
            label = f"{handle.role}/{handle.config.name}:{handle.config.model}"
            print(f"poem {label}", flush=True)
            try:
                response = await asyncio.wait_for(handle.call(prompt), timeout=60)
            except Exception as exc:
                failures.append((label, repr(exc)))
                continue
            print(f"response {label}: {response.content!r}", flush=True)
            print(
                f"usage {label}: input={response.usage.input_tokens} output={response.usage.output_tokens}",
                flush=True,
            )
        return failures

    failures = asyncio.run(run_all())
    assert not failures, "\n".join(f"{label} failed with {error}" for label, error in failures)

    rows = [
        json.loads(line)
        for line in cost_ledger_path(tmp_path).read_text(encoding="utf-8").splitlines()
    ]
    assert rows
    print("cost ledger rows:", flush=True)
    for row in rows:
        print(json.dumps(row, sort_keys=True), flush=True)
        assert row["input_tokens"] >= 0
        assert row["output_tokens"] >= 0
