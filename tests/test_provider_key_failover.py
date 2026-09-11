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
    OPENAI,
    KeyPool,
    active_key,
    check_api_key_fallbacks,
    key_pool,
    make_rotator,
    reset_pools,
)
from auto_research.models.retry import (
    call_with_transient_retry,
    is_exhausted_key_error,
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

    for provider in ("ANTHROPIC", "OPENAI", "GOOGLE"):
        monkeypatch.delenv(f"{provider}_API_KEY", raising=False)
        monkeypatch.delenv(f"{provider}_API_KEY_FALLBACK", raising=False)
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

    def __init__(self, message: str, *, status_code: int | None = None, type_: str | None = None,
                 code: str | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if type_ is not None:
            self.type = type_
        if code is not None:
            self.code = code


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


def _workspace_usage_limit() -> _ProviderError:
    """The spend cap's **400** face, recorded verbatim from a live failure on 2026-09-11.

    Its three structured fields are indistinguishable from an ordinary bad request — status 400,
    `type: invalid_request_error`, no `code` — so only the message separates it. It killed two
    trials of one batch at $80.24 and $73.61, each with four tools already committed, while a
    funded key in a different workspace went untouched.
    """
    return _ProviderError(
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'You have reached your specified workspace API usage limits. You will "
        "regain access on 2026-10-01 at 00:00 UTC.'}}",
        status_code=400,
        type_="invalid_request_error",
    )


def _overloaded() -> _ProviderError:
    return _ProviderError("overloaded", status_code=529, type_="overloaded_error")


# OpenAI reports every one of these as a 429 (developers.openai.com/api/docs/guides/error-codes).
# The field values are the ones the SDK actually sets — openai 2.38.0 parses `.type`/`.code` out
# of `body["error"]` — and were confirmed against a live failure on 2026-08-18.
def _openai_spent_balance() -> _ProviderError:
    return _ProviderError(
        "You have no credits remaining. Add credits to continue using the API.",
        status_code=429, type_="insufficient_quota", code="credit_balance_exhausted",
    )


def _openai_rate_limit() -> _ProviderError:
    return _ProviderError("Rate limit reached for requests", status_code=429,
                          type_="requests", code="rate_limit_exceeded")


def _openai_spend_limit() -> _ProviderError:
    return _ProviderError("Your organization reached its enforced spend limit",
                          status_code=429, type_="insufficient_quota",
                          code="organization_spend_limit_exceeded")


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


def test_a_spent_balance_retires_that_key_but_a_rate_limit_does_not(monkeypatch):
    """A balance never refills; a 429 clears in a minute. Only the first takes a key away."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    make_rotator(ANTHROPIC)(True)  # left the primary on a spent balance
    assert active_key(ANTHROPIC) == FALLBACK
    assert make_rotator(ANTHROPIC)() is None  # the retired primary is never offered back

    _set_keys(monkeypatch, PRIMARY, FALLBACK)  # fresh pool
    make_rotator(ANTHROPIC)(False)  # left the primary on a rate limit
    assert make_rotator(ANTHROPIC)() == "primary"  # still eligible — that quota refills


def test_a_workspace_usage_limit_rotates_and_retires_like_a_spent_balance():
    """Anthropic's spend cap has three faces and only the message tells this one apart.

    `billing_error` (type) and `enforced_spend_limit_reached` (429 code) were already wired.
    This is the 400 `invalid_request_error` form: `.code` is None and `.type` is the same one an
    ordinary malformed request carries, so all three structured arms of the predicate miss it and
    a message marker is the only thing that can catch it. Before this it rotated nowhere and
    killed the run.
    """

    exc = _workspace_usage_limit()
    assert is_key_scoped_provider_error(exc, ANTHROPIC), "must rotate to the other workspace's key"
    assert is_exhausted_key_error(exc, ANTHROPIC), (
        "the message names a date weeks out, so this quota does not refill and the key must be "
        "retired rather than rotated back onto"
    )
    # It is emphatically not the cooldown class: waiting cannot clear a monthly cap.
    assert not is_transient_provider_error(exc, ANTHROPIC)


def test_every_anthropic_message_marker_is_lowercase():
    """Markers are matched against `str(exc).lower()`, so a capitalised entry is dead on arrival.

    The vendor writes "workspace API usage limits"; storing it that way would read as a fix and
    never fire — the same inert-rule shape that sent the spend-cap 429 to `exhausted_codes`.
    """
    from auto_research.models.retry import _ANTHROPIC_RULES

    for marker in _ANTHROPIC_RULES.exhausted_message_markers:
        assert marker == marker.lower(), marker


def test_a_dead_primary_does_not_turn_a_fallback_rate_limit_into_a_fatal_error(monkeypatch):
    """The Q21 shape: primary out of credit, fallback healthy but occasionally throttled.

    Without retirement the second call rotates *back* onto the dead primary, whose 400 is
    non-transient and therefore becomes the exception that raises — a one-minute wait turned
    into a dead run, and strictly worse than having no fallback at all.
    """

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    # Call 1: the primary's balance is spent — rotate onto the fallback and retire it.
    assert (
        asyncio.run(
            call_with_transient_retry(
                _runner([_credit_balance()]),
                describe="role/member",
                provider=ANTHROPIC,
                policy=_NO_WAIT_POLICY,
                sleep=sleep,
                rotate_key=make_rotator(ANTHROPIC),
            )
        )
        == "ok"
    )
    assert active_key(ANTHROPIC) == FALLBACK
    assert waits == []

    # Call 2: a routine 429 on the fallback. The only other key is retired, so this must
    # cool down and succeed — never rotate back and raise the dead key's 400.
    assert (
        asyncio.run(
            call_with_transient_retry(
                _runner([_rate_limit()]),
                describe="role/member",
                provider=ANTHROPIC,
                policy=_NO_WAIT_POLICY,
                sleep=sleep,
                rotate_key=make_rotator(ANTHROPIC),
            )
        )
        == "ok"
    )
    assert len(waits) == 1  # waited it out
    assert active_key(ANTHROPIC) == FALLBACK  # never went back to the dead key


def test_every_key_retired_raises_honestly(monkeypatch):
    """Both balances spent: no key left, so the run fails rather than looping."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    with pytest.raises(_ProviderError):
        asyncio.run(
            call_with_transient_retry(
                _runner([_credit_balance(), _credit_balance()]),
                describe="role/member",
                provider=ANTHROPIC,
                policy=_NO_WAIT_POLICY,
                rotate_key=make_rotator(ANTHROPIC),
            )
        )
    assert make_rotator(ANTHROPIC) is not None
    assert make_rotator(ANTHROPIC)() is None  # nothing left to try


@pytest.mark.parametrize(
    "exc_factory, retires",
    [(_credit_balance, True), (_billing, True), (_rate_limit, False), (_overloaded, False)],
    ids=["credit", "billing", "429", "529"],
)
def test_only_non_refilling_quotas_retire_a_key(exc_factory, retires):
    from auto_research.models.retry import is_exhausted_key_error

    assert is_exhausted_key_error(exc_factory(), ANTHROPIC) is retires


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
    assert is_key_scoped_provider_error(exc_factory(), ANTHROPIC)


@pytest.mark.parametrize(
    "exc_factory, transient",
    [(_overloaded, True), (lambda: _ProviderError("boom", status_code=500), True),
     (lambda: _ProviderError("bad request", status_code=400, type_="invalid_request_error"), False)],
    ids=["529", "500", "plain-400"],
)
def test_key_independent_errors_do_not_rotate(exc_factory, transient):
    """Provider capacity and ordinary bad requests are not the key's fault."""

    exc = exc_factory()
    assert not is_key_scoped_provider_error(exc, ANTHROPIC)
    assert is_transient_provider_error(exc) is transient


def test_auth_errors_are_deliberately_excluded():
    """A revoked key is a configuration fault a human must see, not one to route around."""

    assert not is_key_scoped_provider_error(
        _ProviderError("invalid x-api-key", status_code=401, type_="authentication_error"),
        ANTHROPIC,
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
            provider=ANTHROPIC,
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
                provider=ANTHROPIC,
                policy=_NO_WAIT_POLICY,
                rotate_key=None,  # no fallback configured — the pre-existing behaviour
            )
        )

    assert (
        asyncio.run(
            call_with_transient_retry(
                _runner([_billing()]),
                describe="role/member",
                provider=ANTHROPIC,
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
            provider=ANTHROPIC,
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
                provider=ANTHROPIC,
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
            provider=ANTHROPIC,
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
            provider=ANTHROPIC,
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
    skipped its cooldown — a failover that looks like it works and silently does not. Google
    is the standing example: its documented error vocabulary cannot be cited, so it has no
    taxonomy and never rotates however its environment is configured.
    """

    monkeypatch.setenv("GOOGLE_API_KEY", "g-a")
    monkeypatch.setenv("GOOGLE_API_KEY_FALLBACK", "g-b")
    reset_pools()
    assert KeyPool("google").has_fallback()  # configured…
    assert make_rotator("google") is None  # …but not honoured, so not offered
    assert "google" not in keys_mod.ROTATING_PROVIDERS


def test_preflight_only_validates_providers_that_can_rotate(monkeypatch):
    """A fallback is checked exactly where it can be honoured — the same tuple drives both."""

    _set_keys(monkeypatch, PRIMARY, FALLBACK)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-a")
    monkeypatch.setenv("GOOGLE_API_KEY_FALLBACK", "g-a")  # a duplicate, but unwired
    check_api_key_fallbacks()


# ── the taxonomy is per provider, not global ────────────────────────────────────────

@pytest.mark.parametrize(
    "exc_factory, exhausted",
    [(_openai_spent_balance, True), (_openai_spend_limit, True), (_openai_rate_limit, False)],
    ids=["spent-balance", "spend-limit", "rate-limit"],
)
def test_openai_reports_every_key_quota_as_a_429_and_the_code_separates_them(exc_factory, exhausted):
    """All three are 429s, so only the error code can say which one refills."""

    exc = exc_factory()
    assert is_key_scoped_provider_error(exc, OPENAI)
    assert is_exhausted_key_error(exc, OPENAI) is exhausted


def test_the_same_429_is_read_differently_under_each_provider():
    """The trap this split exists to close.

    Anthropic never reports a spent balance as a 429, so under its taxonomy a 429 always
    refills and must not retire a key. OpenAI's spent balance *is* a 429. Classifying an
    OpenAI failure under Anthropic's rule would leave a permanently dead key in rotation, and
    because rotation is bidirectional the run would return to it on the fallback's next
    routine rate limit.
    """

    exc = _openai_spent_balance()
    assert is_exhausted_key_error(exc, OPENAI) is True
    assert is_exhausted_key_error(exc, ANTHROPIC) is False


def test_a_provider_without_cited_rules_never_classifies():
    """No taxonomy, no rotation — an uncitable vocabulary is not guessed at."""

    assert not is_key_scoped_provider_error(_openai_spent_balance(), "google")
    assert not is_exhausted_key_error(_openai_spent_balance(), "google")
    assert not is_key_scoped_provider_error(_rate_limit(), "")


def test_openai_rotates_onto_its_fallback_and_retires_the_spent_key(monkeypatch):
    """The batch-killing failure of 2026-08-18, now survivable."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "sk-b")
    reset_pools()
    rotations: list[str] = []

    async def on_rotation(label: str, exc: BaseException) -> None:
        rotations.append(label)

    result = asyncio.run(
        call_with_transient_retry(
            _runner([_openai_spent_balance()]),
            describe="analyst_colead/gpt",
            provider=OPENAI,
            policy=_NO_WAIT_POLICY,
            rotate_key=make_rotator(OPENAI),
            on_key_rotation=on_rotation,
        )
    )

    assert result == "ok"
    assert rotations == ["fallback1"]
    assert active_key(OPENAI) == "sk-b"
    assert make_rotator(OPENAI)() is None  # the spent primary is retired, not re-offered


def test_an_unrefillable_quota_raises_instead_of_cooling_down(monkeypatch):
    """A spent balance cannot be waited out, and OpenAI delivers it as a retryable-looking 429.

    Before this, the transient path owned it: a benchmark batch spent 48 minutes in backoff
    (42 s, 64 s, 225 s, 341 s) against a balance no wait could restore.
    """

    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.delenv("OPENAI_API_KEY_FALLBACK", raising=False)
    reset_pools()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    with pytest.raises(_ProviderError):
        asyncio.run(
            call_with_transient_retry(
                _runner([_openai_spent_balance()] * 4),
                describe="analyst_colead/gpt",
                provider=OPENAI,
                policy=_NO_WAIT_POLICY,
                sleep=sleep,
                rotate_key=make_rotator(OPENAI),
            )
        )
    assert waits == []


def test_an_openai_rate_limit_still_waits_when_no_key_is_left(monkeypatch):
    """The refilling half keeps the cooldown path: waiting is the right answer for it."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-a")
    monkeypatch.delenv("OPENAI_API_KEY_FALLBACK", raising=False)
    reset_pools()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    result = asyncio.run(
        call_with_transient_retry(
            _runner([_openai_rate_limit()]),
            describe="analyst_colead/gpt",
            provider=OPENAI,
            policy=_NO_WAIT_POLICY,
            sleep=sleep,
            rotate_key=make_rotator(OPENAI),
        )
    )
    assert result == "ok"
    assert len(waits) == 1


def test_both_wired_providers_send_their_key_explicitly():
    """Rotation is invisible to a client that lets the SDK resolve the environment itself."""

    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/auto_research/models/providers"
    assert "AsyncAnthropic(api_key=active_key(ANTHROPIC))" in (root / "anthropic.py").read_text()
    # Every Responses-dialect vendor shares one client construction, so the check is on the
    # shared layer. `resolve_key` reads the live pool for a rotating provider, which is what
    # `active_key` did here before the vendors were factored out.
    # Matched without the constructor name glued on: this assertion is about a *property* (the key
    # is passed explicitly rather than resolved from the environment by the SDK), and gluing it to
    # a one-line call spelling made it fail the moment the constructor gained a `timeout` and wrapped
    # across lines, with the property itself untouched. A guard that breaks on reformatting trains
    # people to edit the guard.
    responses_src = (root / "_responses.py").read_text()
    assert "AsyncOpenAI(" in responses_src
    assert "api_key=key or None" in responses_src
    for provider in keys_mod.ROTATING_PROVIDERS:
        assert provider in ("anthropic", "openai")


def test_every_responses_vendor_names_its_key_variable():
    """A vendor with no ``key_env`` would silently fall back to the SDK's own env lookup.

    That is the failure this whole module exists to prevent, and factoring the adapter into
    per-vendor descriptors makes it expressible by omission: a new vendor module that forgets the
    field gets a client the pool cannot reach. Asserting it over the registry rather than over one
    adapter's source is what keeps the guarantee as vendors are added.
    """

    from auto_research.models.providers import responses_vendors

    vendors = responses_vendors()
    assert vendors, "no Responses-dialect vendors registered"
    for vendor in vendors:
        assert vendor.key_env, f"vendor {vendor.name!r} names no API-key variable"


def test_a_racing_call_takes_the_key_another_already_rotated_to(monkeypatch):
    """Concurrency: the pool is process-wide, the tried-set is per call.

    A call that fails on the old key just after another call rotated must take the live key, not
    rotate past it. With two keys, rotating past the live one lands on the retired one and reports
    "nothing left to try" while the pool is perfectly healthy.
    """
    _set_keys(monkeypatch, "dead", "live")
    rotator = make_rotator(ANTHROPIC)          # constructed while the primary was live
    other = make_rotator(ANTHROPIC)            # a second in-flight call
    assert other(retire_current=True) == "fallback1"   # it rotates first, retiring the primary

    # Ours failed on the primary, which is no longer live. It must be handed the live key.
    assert rotator(retire_current=True) == "fallback1"
    assert key_pool(ANTHROPIC).active() == "live"


def test_a_racing_call_never_retires_the_key_it_did_not_use(monkeypatch):
    """The failure that cost a batch: ``retire_active`` retires whatever is live *now*.

    A call failing on the old key would retire the replacement a racing call had just switched to.
    With two keys that leaves everything retired, and every later call raises even though one key
    still had credit.
    """
    _set_keys(monkeypatch, "dead", "live")
    rotator = make_rotator(ANTHROPIC)
    other = make_rotator(ANTHROPIC)
    other(retire_current=True)                 # primary retired, pool now on the fallback

    rotator(retire_current=True)               # must not retire the fallback

    pool = key_pool(ANTHROPIC)
    assert pool.active() == "live", "the healthy key was retired by a call that never used it"
