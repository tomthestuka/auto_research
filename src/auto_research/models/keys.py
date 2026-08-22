"""Per-provider API-key pool with rotation on key-scoped failures.

One organization's key carries two independent quotas — a **rate limit** (429
``rate_limit_error``) and a **credit balance** (a ``billing_error``, or a 400 whose
message names the balance). Both are properties of the *key*, not of Anthropic's
capacity, so a second key issued by a **different organization** clears either one
immediately; waiting does not. That distinction is the whole design:
:func:`auto_research.models.retry.is_key_scoped_provider_error` decides which failures
belong here, and everything else (529 overload, 5xx, connection drops) stays on the
cooldown path where waiting is the right answer.

The pool is process-global because the provider adapters build a fresh SDK client per
call, so there is nowhere else for "which key is live" to live. Rotation is
**bidirectional**: the next key-scoped failure swaps back, so a per-minute bucket
refills on one key while the other is in use and neither key is stranded.

Reading the key **explicitly** (``AsyncAnthropic(api_key=...)``) rather than letting the
SDK resolve ``ANTHROPIC_API_KEY`` from the environment is what makes rotation visible to
the client at all — the SDK reads the variable once per client construction, and the
value we want may not be the one in the environment.

Env vars: ``<PROVIDER>_API_KEY`` and the optional ``<PROVIDER>_API_KEY_FALLBACK``.
``anthropic`` and ``openai`` are wired (:data:`ROTATING_PROVIDERS`). Wiring a provider takes
three things, not one: a cited per-provider error taxonomy in
:mod:`auto_research.models.retry` (the same condition arrives as a different status and a
different word from each provider), an adapter that sends ``api_key=`` explicitly, and
membership of that tuple. Google is absent because its documented vocabulary cannot be cited;
the reason is recorded beside the taxonomies.
"""

from __future__ import annotations

import os
import threading

ANTHROPIC = "anthropic"
OPENAI = "openai"

_ENV_PRIMARY = "{provider}_API_KEY"
_ENV_FALLBACK = "{provider}_API_KEY_FALLBACK"


def env_var_names(provider: str) -> tuple[str, str]:
    """Return the ``(primary, fallback)`` env-var names for ``provider``."""

    upper = provider.upper()
    return _ENV_PRIMARY.format(provider=upper), _ENV_FALLBACK.format(provider=upper)


class KeyPool:
    """The keys configured for one provider, and which one is currently live.

    ``keys()`` is read from the environment at construction: the primary first, then the
    fallback when it is set, non-empty, and different from the primary (a duplicate is a
    misconfiguration the preflight rejects, and rotating onto it would be a no-op that
    looks like a working failover).
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider
        primary_var, fallback_var = env_var_names(provider)
        keys: list[str] = []
        for var in (primary_var, fallback_var):
            value = (os.environ.get(var) or "").strip()
            if value and value not in keys:
                keys.append(value)
        self._keys = keys
        self._index = 0
        self._retired: set[int] = set()
        self._lock = threading.Lock()

    def has_fallback(self) -> bool:
        """True when a distinct fallback key is configured."""

        return len(self._keys) > 1

    def active(self) -> str | None:
        """The key to send on the next request, or ``None`` when none is configured.

        ``None`` lets the SDK fall back to its own credential resolution rather than
        being handed an empty string, which would authenticate as an empty key.
        """

        with self._lock:
            return self._keys[self._index] if self._keys else None

    def active_label(self) -> str:
        """A log-safe name for the live key — ``primary``/``fallback``, never the value."""

        with self._lock:
            if not self._keys:
                return "unset"
            return "primary" if self._index == 0 else f"fallback{self._index}"

    def retire_active(self) -> str:
        """Permanently take the live key out of rotation; return its label.

        For a quota that does **not** refill on its own — a spent credit balance. A rate
        limit must never retire a key: it clears in a minute, and retiring on one would
        throw away a perfectly good key.

        Why this exists: rotation is bidirectional, so without retirement a run whose
        primary is out of credit would rotate *back* onto it the next time the fallback hit
        a routine 429 — and because a balance error is non-transient, that dead key's 400
        becomes the exception that raises, converting a one-minute wait into a fatal error.
        Retirement is process-scoped: a topped-up key is live again for the next run.
        """

        with self._lock:
            self._retired.add(self._index)
            return "primary" if self._index == 0 else f"fallback{self._index}"

    def rotate(self, *, exclude: frozenset[str] = frozenset()) -> str | None:
        """Advance to a live key not in ``exclude``; return its label, or ``None`` if none is left.

        ``exclude`` carries the labels already tried during the failing call, so one call
        rotates through each key at most once and then falls through to the caller's
        normal cooldown/raise behaviour instead of spinning between two exhausted keys.
        Retired keys are skipped for the life of the process, so ``None`` — every key
        retired — is how "there is no key left to try" reaches the caller as an honest
        failure rather than a loop.
        """

        with self._lock:
            for step in range(1, len(self._keys)):
                index = (self._index + step) % len(self._keys)
                if index in self._retired:
                    continue
                label = "primary" if index == 0 else f"fallback{index}"
                if label not in exclude:
                    self._index = index
                    return label
        return None


_POOLS: dict[str, KeyPool] = {}
_POOLS_LOCK = threading.Lock()


def key_pool(provider: str) -> KeyPool:
    """Return the process-wide :class:`KeyPool` for ``provider``, building it once.

    Built lazily so the pool reads the environment *after* ``check_required_env`` has
    loaded ``.env`` — importing this module must never be what decides which keys exist.
    """

    with _POOLS_LOCK:
        pool = _POOLS.get(provider)
        if pool is None:
            pool = KeyPool(provider)
            _POOLS[provider] = pool
        return pool


def active_key(provider: str) -> str | None:
    """The live API key for ``provider`` (``None`` when unconfigured)."""

    return key_pool(provider).active()


#: Providers whose adapter actually sends :func:`active_key`. Rotating any other provider
#: would be worse than doing nothing: the client would keep using the key the SDK resolved
#: from the environment, while the caller skipped the cooldown that was its only real
#: recourse — a failover that looks like it works and silently does not. Wiring a provider
#: means one line in its adapter *and* its name here; the preflight check reads the same
#: tuple, so a configured fallback is validated exactly when it can be honoured.
ROTATING_PROVIDERS: tuple[str, ...] = (ANTHROPIC, OPENAI)


def make_rotator(provider: str):
    """Build a per-call rotation callback, or ``None`` when there is nothing to rotate to.

    The returned zero-arg callable advances :func:`key_pool` to a key this *call* has not
    used yet and returns its label, or ``None`` once every key has been tried — at which
    point the caller falls through to its normal cooldown/raise path. The set of tried
    labels is closed over rather than held in the pool, because it is a property of one
    in-flight call, while the live key is a property of the process.

    ``retire_current=True`` first takes the live key out of rotation for good — pass it when
    the failure was a quota that does not refill (a spent balance), never for a rate limit.

    ``None`` for any provider outside :data:`ROTATING_PROVIDERS`, whatever its environment
    holds.
    """

    if provider not in ROTATING_PROVIDERS:
        return None
    pool = key_pool(provider)
    if not pool.has_fallback():
        return None
    tried = {pool.active_label()}

    def rotate(retire_current: bool = False) -> str | None:
        # Another in-flight call may have rotated the pool since this one started. Two things
        # follow, and both were wrong until 2026-08-21, when one spent primary took out a healthy
        # fallback across a 14-trial batch:
        #
        # 1. The live key may be one this call has not tried, in which case there is nothing to
        #    rotate: take it and retry. Rotating instead would step *past* it and, with two keys,
        #    land back on the retired one and report "nothing left" while the pool was healthy.
        # 2. ``retire_active`` retires whatever is live **now**, not the key this call used. A call
        #    that failed on the old key would retire the *replacement* a racing call had just
        #    switched to, and with both keys then retired every later call raised.
        live = pool.active_label()
        if live not in tried:
            tried.add(live)
            return live
        if retire_current:
            tried.add(pool.retire_active())
        label = pool.rotate(exclude=frozenset(tried))
        if label is not None:
            tried.add(label)
        return label

    return rotate


def reset_pools() -> None:
    """Drop every cached pool so the next lookup re-reads the environment (tests)."""

    with _POOLS_LOCK:
        _POOLS.clear()


def check_api_key_fallbacks() -> None:
    """Launch check: every configured fallback key is usable, or say so before spending.

    A fallback is **optional** — a run without one is a normal run, so an unset variable
    passes. What fails is a fallback that is set but cannot do its job: blank after
    stripping, or byte-identical to the primary. Both are silently dropped by
    :class:`KeyPool` (deduplicated away), which would leave the human believing a run is
    protected against throttling when rotation has nowhere to go — exactly the belief a
    launch check exists to correct. Registered in ``auto_research.preflight``; runs after
    ``check_required_env`` has loaded ``.env``.

    Pools are reset first so the launch — not whichever import happened to touch a key
    earliest — is what fixes the process-wide pool from the fully-loaded environment.
    """

    reset_pools()
    problems: list[str] = []
    for provider in ROTATING_PROVIDERS:
        primary_var, fallback_var = env_var_names(provider)
        raw_fallback = os.environ.get(fallback_var)
        if raw_fallback is None:
            continue
        fallback = raw_fallback.strip()
        primary = (os.environ.get(primary_var) or "").strip()
        if not fallback:
            problems.append(f"{fallback_var} is set but empty — unset it or give it a real key")
        elif not primary:
            problems.append(f"{fallback_var} is set while {primary_var} is not")
        elif fallback == primary:
            problems.append(
                f"{fallback_var} is identical to {primary_var} — a fallback in the same slot "
                "cannot relieve a rate limit or a spent balance"
            )
    if problems:
        raise RuntimeError("; ".join(problems))
