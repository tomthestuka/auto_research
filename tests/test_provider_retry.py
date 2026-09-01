"""Deterministic tests for the transient-provider retry layer (models/retry.py)."""

from __future__ import annotations

import asyncio

import anthropic
import httpx
import pytest

import auto_research.env as env
from auto_research import config_paths
from auto_research.env import ProviderRetryPolicy
from auto_research.models.retry import call_with_transient_retry, is_transient_provider_error


class _StatusError(Exception):
    """Stand-in for an Anthropic/OpenAI SDK error carrying an HTTP status."""

    def __init__(self, status: int):
        self.status_code = status
        super().__init__(f"HTTP {status}")


class _GoogleCodeError(Exception):
    """Stand-in for a google-genai APIError, which carries ``code``."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"code {code}")


POLICY = ProviderRetryPolicy(max_retries=3, base_cooldown_s=8.0, max_cooldown_s=16.0)


# ── classification ──────────────────────────────────────────────────────────


def test_overloaded_rate_limit_and_5xx_are_transient():
    assert is_transient_provider_error(_StatusError(529))
    assert is_transient_provider_error(_StatusError(429))
    assert is_transient_provider_error(_StatusError(500))
    assert is_transient_provider_error(_StatusError(503))
    assert is_transient_provider_error(_GoogleCodeError(529))
    assert is_transient_provider_error(_GoogleCodeError(429))


def test_client_errors_and_plain_exceptions_are_not_transient():
    assert not is_transient_provider_error(_StatusError(400))
    assert not is_transient_provider_error(_StatusError(401))
    assert not is_transient_provider_error(_StatusError(404))
    assert not is_transient_provider_error(ValueError("a real bug"))
    assert not is_transient_provider_error(KeyError("also a real bug"))


def test_connection_and_timeout_errors_are_transient():
    request = httpx.Request("POST", "https://api.example.test/v1")
    assert is_transient_provider_error(httpx.ConnectError("boom", request=request))
    assert is_transient_provider_error(httpx.ReadTimeout("slow", request=request))
    assert is_transient_provider_error(anthropic.APIConnectionError(request=request))


# ── retry loop ───────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_transient_failures_retry_then_succeed():
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _StatusError(529)
        return "ok"

    result = _run(
        call_with_transient_retry(flaky, describe="t", policy=POLICY, sleep=fake_sleep)
    )
    assert result == "ok"
    assert calls == 3
    assert len(sleeps) == 2
    # cooldown k = min(cap, base * 2**k) * jitter, jitter in [0.5, 1.0]
    assert 4.0 <= sleeps[0] <= 8.0
    assert 8.0 <= sleeps[1] <= 16.0


def test_cooldown_is_capped_at_max():
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise _StatusError(529)
        return "ok"

    _run(call_with_transient_retry(flaky, describe="t", policy=POLICY, sleep=fake_sleep))
    # third cooldown would be 8 * 4 = 32 uncapped; the 16 s ceiling applies pre-jitter
    assert 8.0 <= sleeps[2] <= 16.0


def test_non_transient_error_raises_immediately():
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def broken():
        raise _StatusError(400)

    with pytest.raises(_StatusError):
        _run(call_with_transient_retry(broken, describe="t", policy=POLICY, sleep=fake_sleep))
    assert sleeps == []


def test_budget_exhaustion_reraises_original_error():
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    async def always_down():
        nonlocal calls
        calls += 1
        raise _StatusError(529)

    with pytest.raises(_StatusError):
        _run(
            call_with_transient_retry(
                always_down, describe="t", policy=POLICY, sleep=fake_sleep
            )
        )
    assert calls == POLICY.max_retries + 1
    assert len(sleeps) == POLICY.max_retries


# ── config accessor ──────────────────────────────────────────────────────────


def test_policy_defaults_when_config_absent(monkeypatch, tmp_path):
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(tmp_path))  # holds no orchestration.yaml
    assert env.provider_retry_policy() == ProviderRetryPolicy(8, 60.0, 600.0)


def test_policy_reads_config_keys(monkeypatch, tmp_path):
    cfg = tmp_path / "orchestration.yaml"
    cfg.write_text(
        "provider_retry_max: 2\n"
        "provider_retry_base_cooldown_s: 5\n"
        "provider_retry_max_cooldown_s: 20\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(cfg.parent))
    assert env.provider_retry_policy() == ProviderRetryPolicy(2, 5.0, 20.0)


def test_policy_falls_back_per_field_on_malformed_values(monkeypatch, tmp_path):
    cfg = tmp_path / "orchestration.yaml"
    cfg.write_text(
        "provider_retry_max: not-a-number\n"
        "provider_retry_base_cooldown_s: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(config_paths.CONFIG_DIR_ENV, str(cfg.parent))
    assert env.provider_retry_policy() == ProviderRetryPolicy(8, 5.0, 600.0)
