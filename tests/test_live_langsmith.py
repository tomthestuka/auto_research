"""Opt-in live LangSmith + cost-recording check.

Run with:
    AUTO_RESEARCH_LIVE_LANGSMITH_TESTS=1 pytest tests/test_live_langsmith.py -s

Cost is recorded into a run's monitor ledger as ``cost.call`` events, so this test installs a
:class:`~auto_research.observability.costs.CostMeter` over a throwaway monitor — the same wiring
``run_research`` does — and asserts the events it produced.
"""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.models import ModelMessage, load_registry
from auto_research.monitor import RunMonitor
from auto_research.observability import install_cost_meter
from tests.dev_logs import dev_log_path


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


def test_live_langsmith_traces_and_cost_records(tmp_path, monkeypatch):
    load_dotenv()
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    assert os.getenv("LANGCHAIN_API_KEY"), "LANGCHAIN_API_KEY is required for live LangSmith tracing"
    response_log_path = dev_log_path("step-3", "live_langsmith_responses", ".jsonl")
    cost_log_path = dev_log_path("step-3", "live_langsmith_cost_records", ".jsonl")
    monitor = RunMonitor(chatter=10, jsonl_path=tmp_path / "monitor.jsonl")

    prompt = [
        ModelMessage(
            role="user",
            content="Write a two-line poem about careful science. Keep it under 25 words.",
        )
    ]

    async def run_all():
        failures = []
        meter = install_cost_meter(run_id="live-langsmith", monitor=monitor)
        monitor.cost_meter = meter
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
            with response_log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "label": label,
                            "provider": handle.config.provider,
                            "model": handle.config.model,
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "text": response.content,
                        },
                        sort_keys=True,
                    )
                )
                stream.write("\n")
        return failures

    failures = asyncio.run(run_all())
    assert not failures, "\n".join(f"{label} failed with {error}" for label, error in failures)

    rows = [
        json.loads(line)["data"]
        for line in (tmp_path / "monitor.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["event"] == "cost.call"
    ]
    assert rows
    print("cost.call rows:", flush=True)
    with cost_log_path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True))
            stream.write("\n")
    for row in rows:
        print(json.dumps(row, sort_keys=True), flush=True)
        assert row["input_tokens"] >= 0
        assert row["output_tokens"] >= 0
    print(f"wrote response log {response_log_path}", flush=True)
    print(f"wrote cost log {cost_log_path}", flush=True)
