"""Transient-failure retry for LLM provider calls.

Every role's model call flows through ``ModelHandle.call``; this module gives that
single choke point patience against transient provider failures — overloaded (529),
rate limits (429), other server errors (5xx), and connection/timeout drops. The
vendor SDKs retry those themselves, but their default budget is two quick attempts
spent within seconds, so a capacity incident lasting minutes surfaces as an unhandled
exception and kills the whole run. Non-transient errors (auth, bad request, our own
bugs) raise immediately and are never masked.

The policy (retry count and cooldown bounds) comes from ``config/orchestration.yaml``
(the ``provider_retry_*`` keys in the "Internet stability" section) via
``auto_research.env.provider_retry_policy``. Cooldowns grow exponentially from the
base to the ceiling and are jittered downward so concurrent agents come back
decorrelated rather than re-hitting the provider in lockstep. Each wait is logged at
WARNING so a paused run's log says why it is waiting.

**Waiting is the wrong answer for one class of failure.** A rate limit and a spent
credit balance are quotas of the *key*, not of the provider's capacity, so a key issued
by a different organization clears them immediately while a cooldown clears them slowly
or never (a spent balance is not transient at all — before this it killed the run).
:func:`is_key_scoped_provider_error` separates that class, and a caller that supplies
``rotate_key`` gets an **immediate** retry on the next key, spending no cooldown and no
retry budget. Everything else — 529 overload, 5xx, connection drops — is key-independent
and stays on the cooldown path. See ``auto_research.models.keys``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from auto_research.env import ProviderRetryPolicy, provider_retry_policy

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS = (408, 409, 429)

@dataclass(frozen=True)
class _KeyErrorRules:
    """How one provider reports a quota of the **key**, in that provider's own vocabulary.

    There is no shared taxonomy to generalise over: the same condition arrives with a
    different status, a different field, and a different word from each provider. A spent
    balance is a 400/403 ``billing_error`` at Anthropic and a **429** at OpenAI, so a rule
    like "a 429 always refills" is true for one and false for the other. Encoding one
    provider's mapping as if it were universal is what makes a dead key look retryable.
    """

    key_scoped_statuses: frozenset[int]
    key_scoped_types: frozenset[str]
    exhausted_types: frozenset[str]
    exhausted_codes: frozenset[str]
    exhausted_message_markers: tuple[str, ...]


# Anthropic (platform.claude.com/docs/en/api/errors): 429 is `rate_limit_error`, and `.type`
# separates a `billing_error` from the `permission_error` that shares its 403. A spent balance
# also arrives as a 400 `invalid_request_error` whose message names the balance ("Your credit
# balance is too low to access the Anthropic API…") — the shape that killed a run on
# 2026-07-22 — so the message is read as well as the type.
_ANTHROPIC_RULES = _KeyErrorRules(
    key_scoped_statuses=frozenset({429}),
    key_scoped_types=frozenset({"rate_limit_error", "billing_error"}),
    exhausted_types=frozenset({"billing_error"}),
    exhausted_codes=frozenset(),
    exhausted_message_markers=("credit balance",),
)

# OpenAI (developers.openai.com/api/docs/guides/error-codes): **every** one of these is a 429,
# and only the error code separates the one that refills from the ones that do not. The docs
# give "Rate limit reached for requests" as self-correcting, against `credit_balance_exhausted`
# ("no prepaid credits remaining"), `organization_spend_limit_exceeded`,
# `project_spend_limit_exceeded` and `organization_usage_limit_exceeded`, each of which the docs
# resolve with an action rather than with time. The shape is version-exact from the SDK
# (openai 2.38.0 `_exceptions.APIError` parses `.type`/`.code`/`.param` out of `body["error"]`,
# `_client._make_status_error` supplying that unwrapping) and was confirmed live on 2026-08-18:
# `.type == "insufficient_quota"`, `.code == "credit_balance_exhausted"`.
_OPENAI_RULES = _KeyErrorRules(
    key_scoped_statuses=frozenset({429}),
    key_scoped_types=frozenset(),
    exhausted_types=frozenset({"insufficient_quota"}),
    exhausted_codes=frozenset({
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }),
    exhausted_message_markers=(),
)

# Google is deliberately absent. Its two documentation pages disagree on the vocabulary — the
# troubleshooting guide quotes `429 RESOURCE_EXHAUSTED` while the error reference names
# `rate_limit_exceeded` and `quota_exceeded` — and neither publishes the response body, so the
# field carrying the identifier cannot be cited. The SDK adds a third hazard: google-genai's
# `APIError.code` is the HTTP **status integer** while OpenAI's `.code` is a **string** error
# code, so a shared field read would misclassify silently. Gemini is off the critical authoring
# and review path (CLAUDE.md §6), so the honest position is no rule rather than a guessed one.
_PROVIDER_KEY_ERROR_RULES: dict[str, _KeyErrorRules] = {
    "anthropic": _ANTHROPIC_RULES,
    "openai": _OPENAI_RULES,
}


def _rules_for(provider: str) -> _KeyErrorRules | None:
    return _PROVIDER_KEY_ERROR_RULES.get((provider or "").lower())


def _error_code(exc: BaseException) -> str:
    """The provider's string error code, or empty when it carries none.

    Read as a string on purpose: a provider whose ``code`` is an HTTP status integer
    contributes nothing here rather than being compared against a vocabulary it does not use.
    """

    code = getattr(exc, "code", None)
    return code.lower() if isinstance(code, str) else ""


def is_exhausted_key_error(exc: BaseException, provider: str) -> bool:
    """True when this key's quota does **not** refill on its own — a spent balance or a
    hard spend/usage ceiling, in ``provider``'s vocabulary.

    The complement within the key-scoped class is a rate limit, which clears in a minute and
    must never retire a key. This distinction is what stops rotation from returning to a dead
    key: without it, a run whose primary is out of credit rotates back onto it the first time
    the fallback hits a routine 429, and that dead key's error becomes the one that raises — a
    one-minute wait turned fatal.

    ``provider`` is required, with no default. Defaulting it would silently apply one
    provider's mapping to another, which is the specific mistake this function exists to
    prevent: OpenAI's spent balance is a 429, so Anthropic's "a 429 always refills" rule
    would classify a permanently dead key as retryable.
    """

    rules = _rules_for(provider)
    if rules is None:
        return False
    error_type = getattr(exc, "type", None)
    if isinstance(error_type, str) and error_type in rules.exhausted_types:
        return True
    if _error_code(exc) in rules.exhausted_codes and rules.exhausted_codes:
        return True
    if isinstance(error_type, str) and error_type in rules.key_scoped_types:
        # A type this provider declares key-scoped but not exhausting — its rate limit.
        return False
    if getattr(exc, "status_code", None) in rules.key_scoped_statuses and not rules.exhausted_codes:
        # A provider whose key-scoped statuses carry no exhaustion codes reports exhaustion
        # some other way, so a bare status in that class is the refilling kind.
        return False
    text = str(exc).lower()
    return any(marker in text for marker in rules.exhausted_message_markers)


def is_key_scoped_provider_error(exc: BaseException, provider: str) -> bool:
    """True when ``exc`` is a quota of the API **key** rather than of the provider.

    A different organization's key clears these immediately; waiting does not. Deliberately
    excluded: 529/5xx and connection failures (provider capacity — key-independent, so
    :func:`is_transient_provider_error` and its cooldown own them), and 401
    ``authentication_error`` (a revoked or mistyped key is a configuration fault a human
    must see, not something to route around silently). A provider with no cited rules never
    rotates, whatever its environment holds.
    """

    rules = _rules_for(provider)
    if rules is None:
        return False
    status = getattr(exc, "status_code", None)
    error_type = getattr(exc, "type", None)
    if isinstance(status, int) and status in rules.key_scoped_statuses:
        return True
    if isinstance(error_type, str) and error_type in rules.key_scoped_types:
        return True
    if _error_code(exc) in rules.exhausted_codes and rules.exhausted_codes:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in rules.exhausted_message_markers)


def is_transient_provider_error(exc: BaseException) -> bool:
    """True when ``exc`` is worth waiting out: overload/rate-limit/5xx/connection.

    Provider SDK errors carry an HTTP status as ``status_code`` (Anthropic/OpenAI)
    or ``code`` (Google genai); connection and timeout failures are the SDKs'
    connection-error types or raw httpx transport errors.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "code", None)
    if isinstance(status, int):
        return status in _TRANSIENT_STATUS or 500 <= status <= 599
    import httpx  # local import — keep module load light (mirrors provider adapters)

    if isinstance(exc, httpx.TransportError):
        return True
    import anthropic
    import openai

    return isinstance(exc, (anthropic.APIConnectionError, openai.APIConnectionError))


async def call_with_transient_retry(
    provider_call: Callable[[], Awaitable[Any]],
    *,
    describe: str,
    policy: ProviderRetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    provider: str = "",
    rotate_key: Callable[[bool], str | None] | None = None,
    on_key_rotation: Callable[[str, BaseException], Awaitable[None]] | None = None,
) -> Any:
    """Await ``provider_call()``, waiting out transient provider failures.

    Retries up to ``policy.max_retries`` times, cooling down before each retry
    (``base_cooldown_s`` doubling per attempt, capped at ``max_cooldown_s``, jittered
    to 50–100% of that value). A non-transient error, or exhaustion of the retry
    budget, re-raises the original exception unchanged. ``describe`` labels the log
    line (e.g. ``role/member``); ``policy`` and ``sleep`` are injectable for tests.

    ``rotate_key(retire_current)`` (when the provider has a second key configured) switches
    to a key this call has not used yet and returns its label, or ``None`` once every key has
    been tried. It is consulted **first**, and only for :func:`is_key_scoped_provider_error`
    failures: the retry that follows is immediate and costs no cooldown and no retry
    budget, because the new key's quota is already available — there is nothing to wait
    for. ``retire_current`` is :func:`is_exhausted_key_error`, so a spent balance takes that
    key out of rotation for good while a rate limit leaves it eligible. When rotation is
    exhausted the same exception falls through to the ordinary path,
    so a 429 on every key still cools down and a spent balance on every key still raises.
    ``on_key_rotation`` observes each switch (label, the error that caused it) for the run
    ledger; it never affects control flow.
    """
    active = policy or provider_retry_policy()
    attempt = 0
    while True:
        try:
            return await provider_call()
        except Exception as exc:
            exhausted = is_exhausted_key_error(exc, provider)
            if rotate_key is not None and is_key_scoped_provider_error(exc, provider):
                # A spent balance retires the key it came from; a rate limit never does.
                label = rotate_key(exhausted)
                if label is not None:
                    logger.warning(
                        "key-scoped provider error on %s (%r) — switching to the %s API key "
                        "and retrying immediately",
                        describe,
                        exc,
                        label,
                    )
                    if on_key_rotation is not None:
                        try:
                            await on_key_rotation(label, exc)
                        except Exception:  # noqa: BLE001 — recording never breaks the call
                            logger.debug("key-rotation hook failed", exc_info=True)
                    continue
            if exhausted:
                # A quota that does not refill cannot be waited out, and every key carrying it
                # has already been tried. Cooling down here is pure delay: on 2026-08-18 an
                # OpenAI balance error, which arrives as a 429 and so reads as transient, held
                # a benchmark batch in backoff for 48 minutes before anyone could act on it.
                raise
            if attempt >= active.max_retries or not is_transient_provider_error(exc):
                raise
            cooldown = min(
                active.max_cooldown_s, active.base_cooldown_s * (2.0**attempt)
            ) * random.uniform(0.5, 1.0)
            attempt += 1
            logger.warning(
                "transient provider error on %s (%r) — waiting %.0fs, retry %d/%d",
                describe,
                exc,
                cooldown,
                attempt,
                active.max_retries,
            )
            await sleep(cooldown)
