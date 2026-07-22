"""Deterministic tests for step-3 observability."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

from auto_research.models import ModelHandle, ModelResponse, ModelUsage
from auto_research.models.providers.anthropic import _anthropic_usage
from auto_research.models.providers.google import _gemini_usage
from auto_research.models.providers.openai import _openai_usage
from auto_research.models.types import RoleModelConfig
from auto_research.observability import cost_entry_for, cost_ledger_path, token_rates_for
from auto_research.observability.costs import CostLedgerEntry
from auto_research.observability.usage import _parse_since, load_entries, render_report


def test_cost_entry_uses_configured_token_rates():
    config = RoleModelConfig(
        name="priced",
        provider="openai",
        model="priced-model",
        priceMTk={"input": 2.0, "output": 8.0},
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
    async def call(self, messages, config, *, schema=None, tools=None, tool_choice="auto", images=(), documents=(), cache_prefix=False):
        return ModelResponse(
            content="done",
            usage=ModelUsage(input_tokens=100, output_tokens=25),
        )


def test_model_handle_appends_cost_ledger_row(tmp_path, monkeypatch):
    monkeypatch.setattr("auto_research.observability.costs.runtime_data_dir", lambda: tmp_path)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    handle = ModelHandle(
        role="analyst_worker",
        config=RoleModelConfig(
            name="fake",
            provider="anthropic",
            model="fake-model",
            priceMTk={"input": 3.0, "output": 15.0},
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


# --- [I5] cache/reasoning pricing (config-fallback path, litellm map forced empty) ---


def test_config_fallback_prices_all_token_classes(monkeypatch):
    monkeypatch.setattr("auto_research.observability.costs._litellm_model_cost", lambda: {})
    config = RoleModelConfig(
        name="c", provider="anthropic", model="unknown-x",
        priceMTk={"input": 10.0, "output": 20.0},
    )
    entry = cost_entry_for(
        "analyst_lead",
        config,
        ModelUsage(
            input_tokens=1_000_000, output_tokens=1_000_000,
            cache_read_tokens=1_000_000, cache_write_tokens=1_000_000, reasoning_tokens=500_000,
        ),
    )
    assert entry.input_cost_usd == 10.0
    assert entry.output_cost_usd == 20.0
    assert entry.cache_read_cost_usd == 1.0  # default 0.1x input
    assert entry.cache_write_cost_usd == 12.5  # default 1.25x input
    assert entry.total_cost_usd == 43.5
    assert entry.reasoning_tokens == 500_000  # recorded, never separately charged
    assert entry.price_source == "config"


def test_explicit_cache_rates_override_defaults(monkeypatch):
    monkeypatch.setattr("auto_research.observability.costs._litellm_model_cost", lambda: {})
    config = RoleModelConfig(
        name="c", provider="openai", model="unknown-y",
        priceMTk={"input": 10.0, "output": 20.0, "cache_read": 5.0, "cache_write": 30.0},
    )
    rates = token_rates_for(config)
    assert rates.cache_read == 5.0 / 1_000_000
    assert rates.cache_write == 30.0 / 1_000_000


# --- [I5] adapter usage normalization (pure functions, fake provider usage objects) ---


def test_anthropic_usage_reports_uncached_input_directly():
    u = _anthropic_usage(SimpleNamespace(
        input_tokens=100, output_tokens=40,
        cache_read_input_tokens=900, cache_creation_input_tokens=50,
    ))
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens) == (100, 900, 50, 40)
    assert u.reasoning_tokens == 0  # Anthropic folds thinking into output_tokens


def test_openai_usage_subtracts_cached_from_total():
    u = _openai_usage(SimpleNamespace(
        input_tokens=1000, output_tokens=200,
        input_tokens_details=SimpleNamespace(cached_tokens=800),
        output_tokens_details=SimpleNamespace(reasoning_tokens=120),
    ))
    assert u.input_tokens == 200  # 1000 total - 800 cached
    assert u.cache_read_tokens == 800
    assert u.cache_write_tokens == 0
    assert u.output_tokens == 200
    assert u.reasoning_tokens == 120


def test_gemini_usage_adds_thoughts_to_output():
    u = _gemini_usage(SimpleNamespace(
        prompt_token_count=1000, cached_content_token_count=300,
        candidates_token_count=200, thoughts_token_count=50,
    ))
    assert u.input_tokens == 700  # 1000 - 300 cached
    assert u.cache_read_tokens == 300
    assert u.output_tokens == 250  # 200 candidates + 50 thoughts
    assert u.reasoning_tokens == 50


def test_legacy_ledger_row_validates_without_cache_fields():
    row = {
        "created_at": "2026-05-27T18:00:50.881082Z", "role": "analyst_worker",
        "member": "m", "provider": "anthropic", "model": "x",
        "input_tokens": 100, "output_tokens": 50,
        "input_cost_usd": 0.1, "output_cost_usd": 0.2, "total_cost_usd": 0.3,
    }
    entry = CostLedgerEntry.model_validate(row)
    assert entry.cache_read_tokens == 0 and entry.reasoning_tokens == 0
    assert entry.price_source == "config"


# --- [I5] spend rollup ---


def test_usage_rollup_aggregates_and_filters_since(tmp_path):
    ledger = tmp_path / "cost_ledger.jsonl"
    rows = [
        CostLedgerEntry(
            created_at=datetime(2026, 7, 1, tzinfo=UTC), role="reviewer", member="a",
            provider="anthropic", model="m1", input_tokens=10, output_tokens=5,
            input_cost_usd=1.0, output_cost_usd=2.0, total_cost_usd=3.0,
        ),
        CostLedgerEntry(
            created_at=datetime(2026, 7, 20, tzinfo=UTC), role="analyst_worker", member="b",
            provider="openai", model="m2", input_tokens=20, output_tokens=10,
            cache_read_tokens=100, input_cost_usd=0.5, output_cost_usd=1.5,
            cache_read_cost_usd=0.25, total_cost_usd=2.25,
        ),
    ]
    ledger.write_text(
        "\n".join(json.dumps(r.model_dump(mode="json"), sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    report = render_report(load_entries(ledger), None, 15)
    assert "TOTAL: $5.2500" in report
    assert "anthropic" in report and "openai" in report

    since = _parse_since("2026-07-10")
    recent = load_entries(ledger, since)
    assert len(recent) == 1
    assert "TOTAL: $2.2500" in render_report(recent, since, 15)
