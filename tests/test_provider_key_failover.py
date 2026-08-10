"""Key-scoped provider failures rotate to a fallback API key instead of waiting.

A rate limit and a spent credit balance are quotas of the *key*; a key from another
organization clears them at once, while the cooldown path clears them slowly (429) or
never (a spent balance is not transient — before this it killed the run). These tests pin
which failures rotate, that rotation costs no cooldown and no retry budget, that it stops
after every key has been tried, and that key-independent failures (529/5xx) still wait.
"""

from __future__ import annotations

import asyncio

import pytest

from auto_research.models import keys as keys_mod
from auto_research.models.keys import (
    ANTHROPIC,
    KeyPool,
    active_key,
    check_api_key_fallbacks,
    key_pool,
    make_rotator,
    reset_pools,
)
from auto_research.models.retry import (
    call_with_transient_retry,
    is_key_scoped_provider_error,
    is_transient_provider_error,
)
from auto_research.env import ProviderRetryPolicy


PRIMARY = "sk-ant-primary"
FALLBACK = "sk-ant-fallback"


@pytest.fixture(autouse=True)
def _isolate_pools(monkeypatch):
    """Every test starts from a clean, explicitly-set key environment.

    The pool is process-global by design (adapters build a client per call), so without
    this a rotation in one test would leak into the next.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_FALLBACK", raising=False)
    reset_pools()
    yield
    reset_pools()


def _set_keys(monkeypatch, primary: str | None, fallback: str | None) -> None:
    if primary is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", primary)
    if fallback is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY_FALLBACK", fallback)
    reset_pools()


class _ProviderError(Exception):
    """Stand-in for an SDK error: the two attributes our classifiers actually read."""

    def __init__(self, message: str, *, status_code: int | None = None, type_: str | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if type_ is not None:
            self.type = type_


def _rate_limit() -> _ProviderError:
    return _ProviderError("rate limited", status_code=429, type_="rate_limit_error")


def _billing() -> _ProviderError:
    return _ProviderError("billing problem", status_code=403, type_="billing_error")


def _credit_balance() -> _ProviderError:
    return _ProviderError(
        "Your credit balance is too low to access the Anthropic API.",
        status_code=400,
        type_="invalid_request_error",
    )


def _overloaded() -> _ProviderError:
    return _ProviderError("overloaded", status_code=529, type_="overloaded_error")


# ── the pool ────────────────────────────────────────────────────────────────────────

def test_pool_reads_primary_and_fallback(monkeypatch):
    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    pool = key_pool(ANTHROPIC)
    assert pool.has_fallback()
    assert pool.active() == PRIMARY
    assert pool.active_label() == "primary"


def test_pool_without_fallback_has_nothing_to_rotate_to(monkeypatch):
    _set_keys(monkeypatch, PRIMARY, None)
    assert not key_pool(ANTHROPIC).has_fallback()
    assert make_rotator(ANTHROPIC) is None


def test_pool_deduplicates_a_fallback_identical_to_the_primary(monkeypatch):
    """A duplicate is not a fallback — rotating onto it would be a no-op that looks like one."""

    _set_keys(monkeypatch, PRIMARY, PRIMARY)
    assert not key_pool(ANTHROPIC).has_fallback()


def test_pool_with_no_keys_returns_none_not_empty_string(monkeypatch):
    """``None`` hands resolution back to the SDK; an empty string would authenticate as one."""

    reset_pools()
    assert active_key(ANTHROPIC) is None


def test_one_call_offers_each_key_at_most_once(monkeypatch):
    """The key that just failed is not re-offered inside the same call — nothing changed for it."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    rotate = make_rotator(ANTHROPIC)
    assert rotate() == "fallback1"
    assert active_key(ANTHROPIC) == FALLBACK
    assert rotate() is None  # primary already failed this call; exhausted


def test_rotation_is_bidirectional_across_calls(monkeypatch):
    """The live key is process state, so the *next* call rotates back and neither is stranded.

    This is what lets a per-minute bucket refill on one key while the other is in use.
    """

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    assert make_rotator(ANTHROPIC)() == "fallback1"
    assert active_key(ANTHROPIC) == FALLBACK

    assert make_rotator(ANTHROPIC)() == "primary"
    assert active_key(ANTHROPIC) == PRIMARY


# ── the classifier ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "exc_factory", [_rate_limit, _billing, _credit_balance], ids=["429", "billing", "credit"]
)
def test_key_scoped_errors_are_recognized(exc_factory):
    assert is_key_scoped_provider_error(exc_factory())


@pytest.mark.parametrize(
    "exc_factory, transient",
    [(_overloaded, True), (lambda: _ProviderError("boom", status_code=500), True),
     (lambda: _ProviderError("bad request", status_code=400, type_="invalid_request_error"), False)],
    ids=["529", "500", "plain-400"],
)
def test_key_independent_errors_do_not_rotate(exc_factory, transient):
    """Provider capacity and ordinary bad requests are not the key's fault."""

    exc = exc_factory()
    assert not is_key_scoped_provider_error(exc)
    assert is_transient_provider_error(exc) is transient


def test_auth_errors_are_deliberately_excluded():
    """A revoked key is a configuration fault a human must see, not one to route around."""

    assert not is_key_scoped_provider_error(
        _ProviderError("invalid x-api-key", status_code=401, type_="authentication_error")
    )


# ── the retry loop ──────────────────────────────────────────────────────────────────

_NO_WAIT_POLICY = ProviderRetryPolicy(max_retries=3, base_cooldown_s=1.0, max_cooldown_s=1.0)


def _runner(errors: list[BaseException]):
    """A provider call that raises ``errors`` in order, then succeeds."""

    remaining = list(errors)

    async def call():
        if remaining:
            raise remaining.pop(0)
        return "ok"

    return call


@pytest.mark.parametrize(
    "exc_factory", [_rate_limit, _billing, _credit_balance], ids=["429", "billing", "credit"]
)
def test_key_scoped_error_retries_immediately_on_the_other_key(monkeypatch, exc_factory):
    """No cooldown, no retry budget — the new key's quota is already available."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    waits: list[float] = []
    rotations: list[str] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    async def on_rotation(label: str, exc: BaseException) -> None:
        rotations.append(label)

    result = asyncio.run(
        call_with_transient_retry(
            _runner([exc_factory()]),
            describe="analyst_lead/claude",
            policy=_NO_WAIT_POLICY,
            sleep=sleep,
            rotate_key=make_rotator(ANTHROPIC),
            on_key_rotation=on_rotation,
        )
    )

    assert result == "ok"
    assert waits == []  # the whole point: waiting cannot refill another key's quota
    assert rotations == ["fallback1"]
    assert active_key(ANTHROPIC) == FALLBACK


def test_a_spent_balance_no_longer_kills_the_run(monkeypatch):
    """A billing error is *not* transient, so without a fallback it raised on attempt one."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    with pytest.raises(_ProviderError):
        asyncio.run(
            call_with_transient_retry(
                _runner([_billing()]),
                describe="role/member",
                policy=_NO_WAIT_POLICY,
                rotate_key=None,  # no fallback configured — the pre-existing behaviour
            )
        )

    assert (
        asyncio.run(
            call_with_transient_retry(
                _runner([_billing()]),
                describe="role/member",
                policy=_NO_WAIT_POLICY,
                rotate_key=make_rotator(ANTHROPIC),
            )
        )
        == "ok"
    )


def test_rotation_exhausted_falls_through_to_the_cooldown_path(monkeypatch):
    """A 429 on *every* key is a real wait — rotation stops, the existing policy resumes."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    # One rotation (onto the fallback); the rotator is then spent for this call, so the
    # remaining failures take the cooldown path before the call finally succeeds.
    result = asyncio.run(
        call_with_transient_retry(
            _runner([_rate_limit(), _rate_limit(), _rate_limit()]),
            describe="role/member",
            policy=_NO_WAIT_POLICY,
            sleep=sleep,
            rotate_key=make_rotator(ANTHROPIC),
        )
    )

    assert result == "ok"
    assert len(waits) == 2


def test_rotation_exhausted_on_a_non_transient_error_still_raises(monkeypatch):
    """Both balances spent is an honest failure, not an infinite ping-pong."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    with pytest.raises(_ProviderError):
        asyncio.run(
            call_with_transient_retry(
                _runner([_billing(), _billing(), _billing()]),
                describe="role/member",
                policy=_NO_WAIT_POLICY,
                rotate_key=make_rotator(ANTHROPIC),
            )
        )


def test_overload_waits_instead_of_rotating(monkeypatch):
    """529 is Anthropic's capacity, identical on every key — burning a rotation helps nothing."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    result = asyncio.run(
        call_with_transient_retry(
            _runner([_overloaded()]),
            describe="role/member",
            policy=_NO_WAIT_POLICY,
            sleep=sleep,
            rotate_key=make_rotator(ANTHROPIC),
        )
    )

    assert result == "ok"
    assert len(waits) == 1
    assert active_key(ANTHROPIC) == PRIMARY  # unrotated


def test_a_failing_rotation_hook_never_breaks_the_call(monkeypatch):
    """Recording is observability; it may not decide whether a model call succeeds [J2]."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)

    async def broken(label: str, exc: BaseException) -> None:
        raise RuntimeError("ledger is full")

    result = asyncio.run(
        call_with_transient_retry(
            _runner([_rate_limit()]),
            describe="role/member",
            policy=_NO_WAIT_POLICY,
            rotate_key=make_rotator(ANTHROPIC),
            on_key_rotation=broken,
        )
    )
    assert result == "ok"


# ── preflight ───────────────────────────────────────────────────────────────────────

def test_preflight_passes_without_a_fallback(monkeypatch):
    """A fallback is optional; a run without one is a normal run."""

    _set_keys(monkeypatch, PRIMARY, None)
    check_api_key_fallbacks()


def test_preflight_passes_with_a_distinct_fallback(monkeypatch):
    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    check_api_key_fallbacks()


def test_preflight_rejects_a_duplicate_fallback(monkeypatch):
    """Silently deduplicated otherwise — the human would believe a run is protected."""

    _set_keys(monkeypatch, PRIMARY, PRIMARY)
    with pytest.raises(RuntimeError, match="identical"):
        check_api_key_fallbacks()


def test_preflight_rejects_a_blank_fallback(monkeypatch):
    _set_keys(monkeypatch, PRIMARY, "   ")
    with pytest.raises(RuntimeError, match="empty"):
        check_api_key_fallbacks()


def test_preflight_rejects_a_fallback_without_a_primary(monkeypatch):
    _set_keys(monkeypatch, None, FALLBACK)
    with pytest.raises(RuntimeError, match="is not"):
        check_api_key_fallbacks()


def test_preflight_is_registered_after_required_env():
    """Order matters: ``required_env`` is what loads ``.env``, and this check reads it."""

    from auto_research.preflight import build_default_preflight

    names = build_default_preflight().names()
    assert "api_key_fallbacks" in names
    assert names.index("required_env") < names.index("api_key_fallbacks")


# ── the coder subprocess ────────────────────────────────────────────────────────────

def test_coder_env_is_untouched_while_the_primary_key_is_live(monkeypatch):
    """The common path stays byte-identical: nothing to override, so nothing is built."""

    from auto_research.sandbox.merge_flow import _with_live_anthropic_key

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    assert _with_live_anthropic_key(None) is None
    assert _with_live_anthropic_key({"PATH": "/bin"}) == {"PATH": "/bin"}


def test_coder_env_carries_the_rotated_key(monkeypatch):
    """The coder is a separate process; a rotation here reaches it only by being stamped."""

    from auto_research.sandbox.merge_flow import _with_live_anthropic_key

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    make_rotator(ANTHROPIC)()

    sourced = _with_live_anthropic_key({"PATH": "/bin"})
    assert sourced == {"PATH": "/bin", "ANTHROPIC_API_KEY": FALLBACK}

    inherited = _with_live_anthropic_key(None)
    assert inherited is not None and inherited["ANTHROPIC_API_KEY"] == FALLBACK


def test_env_var_names_are_derived_not_hardcoded_per_provider():
    assert keys_mod.env_var_names("anthropic") == (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_FALLBACK",
    )
    assert keys_mod.env_var_names("openai") == ("OPENAI_API_KEY", "OPENAI_API_KEY_FALLBACK")


def test_keypool_is_constructible_directly_for_any_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "sk-b")
    pool = KeyPool("openai")
    assert pool.has_fallback() and pool.active() == "sk-a"


def test_an_unwired_provider_never_rotates(monkeypatch):
    """Rotating a provider whose adapter ignores the pool is worse than doing nothing.

    The client would keep the key the SDK resolved from the environment while the caller
    skipped its cooldown — a failover that looks like it works and silently does not. Only
    Anthropic sends ``active_key`` today, so only Anthropic may rotate.
    """

    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "sk-b")
    reset_pools()
    assert KeyPool("openai").has_fallback()  # configured…
    assert make_rotator("openai") is None  # …but not honoured, so not offered
    assert keys_mod.ROTATING_PROVIDERS == (ANTHROPIC,)


def test_preflight_only_validates_providers_that_can_rotate(monkeypatch):
    """A fallback is checked exactly where it can be honoured — the same tuple drives both."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "sk-a")  # a duplicate, but unwired
    check_api_key_fallbacks()
