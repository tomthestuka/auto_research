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
) -> Any:
    """Await ``provider_call()``, waiting out transient provider failures.

    Retries up to ``policy.max_retries`` times, cooling down before each retry
    (``base_cooldown_s`` doubling per attempt, capped at ``max_cooldown_s``, jittered
    to 50–100% of that value). A non-transient error, or exhaustion of the retry
    budget, re-raises the original exception unchanged. ``describe`` labels the log
    line (e.g. ``role/member``); ``policy`` and ``sleep`` are injectable for tests.
    """
    active = policy or provider_retry_policy()
    attempt = 0
    while True:
        try:
            return await provider_call()
        except Exception as exc:
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
