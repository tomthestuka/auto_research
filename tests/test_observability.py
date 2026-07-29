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
from auto_research.monitor import RunMonitor
from auto_research.observability import cost_for_call, install_cost_meter, token_rates_for
from auto_research.observability.costs import CallCost, CostMeter
from auto_research.observability.usage import _parse_since, load_cost_records, render_report


def test_cost_entry_uses_configured_token_rates():
    config = RoleModelConfig(
        name="priced",
        provider="openai",
        model="priced-model",
        priceMTk={"input": 2.0, "output": 8.0},
    )

    entry = cost_for_call(
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


def _metered_handle(monitor: RunMonitor) -> ModelHandle:
    return ModelHandle(
        role="analyst_worker",
        config=RoleModelConfig(
            name="fake",
            provider="anthropic",
            model="fake-model",
            priceMTk={"input": 3.0, "output": 15.0},
        ),
        provider=FakeProvider(),
    )


def test_model_call_records_cost_event_in_the_monitor_ledger(tmp_path, monkeypatch):
    """A priced call lands as one `cost.call` event — the monitor JSONL is the only cost record."""
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monitor = RunMonitor(chatter=10, jsonl_path=tmp_path / "monitor.jsonl")

    async def _run() -> ModelResponse:
        meter = install_cost_meter(run_id="r1", monitor=monitor)
        monitor.cost_meter = meter
        return await _metered_handle(monitor).call([{"role": "user", "content": "hello"}])

    response = asyncio.run(_run())

    assert response.content == "done"
    rows = [
        json.loads(line)
        for line in (tmp_path / "monitor.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cost_events = [row for row in rows if row["event"] == "cost.call"]
    assert len(cost_events) == 1
    data = cost_events[0]["data"]
    assert (data["role"], data["member"], data["provider"], data["model"]) == (
        "analyst_worker", "fake", "anthropic", "fake-model"
    )
    assert (data["input_tokens"], data["output_tokens"]) == (100, 25)
    assert data["total_cost_usd"] == 0.000675
    # The accumulated total rides on the event itself (and on every event after it).
    assert cost_events[0]["cost_usd_total"] == 0.000675


def test_unmetered_call_runs_without_recording_cost(monkeypatch):
    """Outside a metered run (a probe, an isolated test) a call still works, unaccumulated."""
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr("auto_research.observability.tracing.current_cost_meter", lambda: None)
    response = asyncio.run(
        _metered_handle(RunMonitor(chatter=10)).call([{"role": "user", "content": "hi"}])
    )
    assert response.content == "done"


def test_meter_folds_coder_sdk_session_totals_as_deltas():
    """The coder's Agent SDK reports a session-CUMULATIVE cost; the meter must add only deltas."""
    monitor = RunMonitor(chatter=10)
    meter = CostMeter(run_id="r1", monitor=monitor)
    monitor.cost_meter = meter

    async def _run() -> list[float]:
        return [
            await meter.record_session_total(
                role="coder", member="claude_sdk", provider="anthropic", model="sonnet",
                session_id="s1", cumulative_usd=value,
            )
            for value in (4.0, 6.5, 6.5, 9.25)
        ]

    deltas = asyncio.run(_run())

    assert deltas == [4.0, 2.5, 0.0, 2.75]
    assert meter.total_usd == 9.25
    events = [e for e in monitor.events if e.event == "cost.call"]
    assert [e.data["price_source"] for e in events] == ["claude_sdk"] * 4
    assert events[-1].cost_usd_total == 9.25


# --- [I5] cache/reasoning pricing (config-fallback path, litellm map forced empty) ---


def test_config_fallback_prices_all_token_classes(monkeypatch):
    monkeypatch.setattr("auto_research.observability.costs._litellm_model_cost", lambda: {})
    config = RoleModelConfig(
        name="c", provider="anthropic", model="unknown-x",
        priceMTk={"input": 10.0, "output": 20.0},
    )
    entry = cost_for_call(
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


def test_partial_cost_payload_validates_via_defaults():
    """A payload written by an older/newer build still validates — every field defaults."""
    entry = CallCost.model_validate(
        {
            "role": "analyst_worker", "member": "m", "provider": "anthropic", "model": "x",
            "input_tokens": 100, "output_tokens": 50,
            "input_cost_usd": 0.1, "output_cost_usd": 0.2, "total_cost_usd": 0.3,
        }
    )
    assert entry.cache_read_tokens == 0 and entry.reasoning_tokens == 0
    assert entry.price_source == "config"


# --- [I5] spend rollup over monitor ledgers ---


def _cost_event(seq: int, when: datetime, run_id: str, **payload) -> str:
    event = {
        "run_id": run_id, "sequence": seq, "created_at": when.isoformat(), "team": "run",
        "event": "cost.call", "level": 10, "summary": "cost", "data": payload,
    }
    return json.dumps(event, sort_keys=True)


def test_usage_rollup_aggregates_monitor_ledgers_and_filters_since(tmp_path):
    ledger = tmp_path / "monitor.jsonl"
    ledger.write_text(
        "\n".join(
            [
                _cost_event(
                    1, datetime(2026, 7, 1, tzinfo=UTC), "run-a", role="reviewer", member="a",
                    provider="anthropic", model="m1", input_tokens=10, output_tokens=5,
                    input_cost_usd=1.0, output_cost_usd=2.0, total_cost_usd=3.0,
                ),
                _cost_event(
                    2, datetime(2026, 7, 20, tzinfo=UTC), "run-b", role="analyst_worker",
                    member="b", provider="openai", model="m2", input_tokens=20, output_tokens=10,
                    cache_read_tokens=100, input_cost_usd=0.5, output_cost_usd=1.5,
                    cache_read_cost_usd=0.25, total_cost_usd=2.25,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = render_report(load_cost_records([ledger]), None, 15, by_run=True)
    assert "TOTAL: $5.2500" in report
    assert "anthropic" in report and "openai" in report
    assert "run-a" in report and "2 run(s)" in report

    since = _parse_since("2026-07-10")
    recent = load_cost_records([ledger], since)
    assert len(recent) == 1
    assert "TOTAL: $2.2500" in render_report(recent, since, 15)


def test_usage_rollup_deduplicates_a_copied_ledger(tmp_path):
    """A run's ledger copied to logs/dev must not double-count (dedup on run_id+sequence)."""
    row = _cost_event(
        1, datetime(2026, 7, 1, tzinfo=UTC), "run-a", role="reviewer", member="a",
        provider="anthropic", model="m1", total_cost_usd=3.0,
    )
    first = tmp_path / "monitor.jsonl"
    second = tmp_path / "copy_monitor.jsonl"
    first.write_text(row + "\n", encoding="utf-8")
    second.write_text(row + "\n", encoding="utf-8")
    assert len(load_cost_records([first, second])) == 1
