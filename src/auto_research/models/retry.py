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
from typing import Any

from auto_research.env import ProviderRetryPolicy, provider_retry_policy

logger = logging.getLogger(__name__)

_TRANSIENT_STATUS = (408, 409, 429)

# Anthropic's own error taxonomy (platform.claude.com/docs/en/api/errors): 429 is
# `rate_limit_error`, and `.type` separates a `billing_error` from the `permission_error`
# that shares its 403. A spent balance also arrives as a 400 `invalid_request_error`
# whose message names the balance ("Your credit balance is too low to access the
# Anthropic API…") — the shape that killed a run on 2026-07-22 — so the message is read
# as well as the type.
_KEY_SCOPED_TYPES = frozenset({"rate_limit_error", "billing_error"})
_KEY_SCOPED_MESSAGE_MARKERS = ("credit balance",)


def is_key_scoped_provider_error(exc: BaseException) -> bool:
    """True when ``exc`` is a quota of the API **key** rather than of the provider.

    A different organization's key clears these immediately; waiting does not. Deliberately
    excluded: 529/5xx and connection failures (provider capacity — key-independent, so
    :func:`is_transient_provider_error` and its cooldown own them), and 401
    ``authentication_error`` (a revoked or mistyped key is a configuration fault a human
    must see, not something to route around silently).
    """

    status = getattr(exc, "status_code", None)
    error_type = getattr(exc, "type", None)
    if isinstance(status, int) and status == 429:
        return True
    if isinstance(error_type, str) and error_type in _KEY_SCOPED_TYPES:
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _KEY_SCOPED_MESSAGE_MARKERS)


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
    rotate_key: Callable[[], str | None] | None = None,
    on_key_rotation: Callable[[str, BaseException], Awaitable[None]] | None = None,
) -> Any:
    """Await ``provider_call()``, waiting out transient provider failures.

    Retries up to ``policy.max_retries`` times, cooling down before each retry
    (``base_cooldown_s`` doubling per attempt, capped at ``max_cooldown_s``, jittered
    to 50–100% of that value). A non-transient error, or exhaustion of the retry
    budget, re-raises the original exception unchanged. ``describe`` labels the log
    line (e.g. ``role/member``); ``policy`` and ``sleep`` are injectable for tests.

    ``rotate_key`` (when the provider has a second key configured) switches to a key this
    call has not used yet and returns its label, or ``None`` once every key has been
    tried. It is consulted **first**, and only for :func:`is_key_scoped_provider_error`
    failures: the retry that follows is immediate and costs no cooldown and no retry
    budget, because the new key's quota is already available — there is nothing to wait
    for. When rotation is exhausted the same exception falls through to the ordinary path,
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
            if rotate_key is not None and is_key_scoped_provider_error(exc):
                label = rotate_key()
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
