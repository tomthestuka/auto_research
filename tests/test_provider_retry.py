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


# ── F7: a documented-transient condition wearing a non-transient status ──────


class _BodyError(Exception):
    """A vendor error with an HTTP status and a string error code, like the OpenAI SDK's."""

    def __init__(self, message, *, status_code, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


_SLOT6 = (
    "Error code: 400 - {'request_id': '8a67e9ac', 'code': 'ServiceUnavailable', "
    "'message': '<503> InternalError.Algo: An error occurred in model serving, error message "
    "is: [Too many requests. Your requests are being throttled due to system capacity limits. "
    "Please try again later.]'}"
)


def test_f7_the_slot6_error_is_transient_for_dashscope():
    """The exact body that killed slot 6 in one attempt: HTTP 400 carrying a documented-500
    condition. Alibaba's error reference puts `InternalError.Algo` at 500 with "retry first",
    and the whole `Throttling*` family at 429."""
    exc = _BodyError(_SLOT6, status_code=400, code="ServiceUnavailable")
    assert is_transient_provider_error(exc, "dashscope")


def test_f7_the_same_error_stays_fatal_without_the_provider():
    """Status-first remains the rule: the override is per-vendor and opt-in, so no other
    provider's 400 becomes retryable by accident."""
    exc = _BodyError(_SLOT6, status_code=400, code="ServiceUnavailable")
    assert not is_transient_provider_error(exc)
    assert not is_transient_provider_error(exc, "openai")
    assert not is_transient_provider_error(exc, "anthropic")


def test_f7_a_genuine_dashscope_400_stays_fatal():
    """The same code family contains a permanent member — the vendor documents
    `400-ServiceUnavailableError` as a malformed request — so the message decides, and a real
    request-shape defect must still raise at once."""
    exc = _BodyError(
        "Error code: 400 - Role must be user or assistant and Content length must be "
        "greater than 0.",
        status_code=400, code="ServiceUnavailableError",
    )
    assert not is_transient_provider_error(exc, "dashscope")
    assert not is_transient_provider_error(
        _BodyError("Error code: 400 - invalid_request_error: unknown parameter 'foo'",
                   status_code=400, code="invalid_request_error"),
        "dashscope",
    )


def test_f7_a_bare_three_digit_number_in_output_is_not_a_marker():
    """Markers are vocabulary, never a bare status substring: real scientific output is full of
    three-digit runs (`0.36692140104299825` contains "429")."""
    exc = _BodyError(
        "Error code: 400 - fit failed: chi2=429.5, PD=0.36692140104299825 out of range",
        status_code=400, code="invalid_request_error",
    )
    assert not is_transient_provider_error(exc, "dashscope")


def test_f7_deepseek_insufficient_system_resource_is_transient():
    """DeepSeek documents an overloaded cluster as `insufficient_system_resource`."""
    exc = _BodyError("Error code: 400 - insufficient_system_resource", status_code=400)
    assert is_transient_provider_error(exc, "deepseek")


def test_f7_anthropic_spend_cap_429_is_treated_as_non_refilling():
    """`enforced_spend_limit_reached` carries no retry-after and never clears, so it must not be
    waited out as if it were a refilling rate limit."""
    from auto_research.models.retry import is_exhausted_key_error

    exc = _BodyError("Error code: 429 - enforced_spend_limit_reached", status_code=429,
                     code="enforced_spend_limit_reached")
    exc.type = "rate_limit_error"
    assert is_exhausted_key_error(exc, "anthropic")
