"""Deterministic tests for step-3 observability."""

import asyncio
import json

from auto_research.models import ModelHandle, ModelResponse, ModelUsage
from auto_research.models.types import RoleModelConfig
from auto_research.observability import cost_entry_for, cost_ledger_path


def test_cost_entry_uses_configured_token_rates():
    config = RoleModelConfig(
        name="priced",
        provider="openai",
        model="priced-model",
        pricing_usd_per_million_tokens={"input": 2.0, "output": 8.0},
    )

    entry = cost_entry_for(
        "reviewer",
        config,
        ModelUsage(input_tokens=1_000_000, output_tokens=500_000),
    )

    assert entry.input_cost_usd == 2.0
    assert entry.output_cost_usd == 4.0
    assert entry.total_cost_usd == 6.0


class FakeProvider:
    async def call(self, messages, config, *, schema=None, images=()):
        return ModelResponse(
            content="done",
            usage=ModelUsage(input_tokens=100, output_tokens=25),
        )


def test_model_handle_appends_cost_ledger_row(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    handle = ModelHandle(
        role="analyst_worker",
        config=RoleModelConfig(
            name="fake",
            provider="anthropic",
            model="fake-model",
            pricing_usd_per_million_tokens={"input": 3.0, "output": 15.0},
        ),
        provider=FakeProvider(),
    )

    response = asyncio.run(handle.call([{"role": "user", "content": "hello"}]))

    assert response.content == "done"
    ledger_path = cost_ledger_path()
    rows = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["role"] == "analyst_worker"
    assert row["member"] == "fake"
    assert row["provider"] == "anthropic"
    assert row["model"] == "fake-model"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 25
    assert row["total_cost_usd"] == 0.000675
