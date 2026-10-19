"""Per-call LLM cost accounting, accumulated per run and recorded in the monitor ledger.

Cost is priced **litellm-first, config-fallback** ([I5]): litellm ships a community-maintained
per-model price map (``litellm.model_cost``) that encodes input/output **and** cache-read /
cache-creation per-token costs, kept current far more reliably than a hand-edited table. When
litellm does not know a model (our bleeding-edge / renamed ids can outrun its bundled map), we fall
back to ``config/models.yaml``'s ``priceMTk`` — with cache tiers defaulted from the input rate
(0.1x read, 1.25x write) when not pinned. Either way the four neutral :class:`ModelUsage` token
classes are non-overlapping, so cost is a plain weighted sum.

**Where cost is written:** into the run's **monitor JSONL**, as one ``cost.call`` event per call
plus a cumulative ``cost_usd_total`` stamped on *every* monitor event. The monitor JSONL is the
single source of truth for a run ([J1]) — recording and replay both read it, so a parallel cost log
can neither drift from it nor need per-run attribution bolted on. (The pre-2026-07-29
``<runtime_dir>/cost_ledger.jsonl`` is gone: it was a second, run-blind log whose rows also
under-counted, and nothing derived from it that the event stream cannot answer.)

A :class:`CostMeter` is installed per run by ``run_research`` and found through a
:class:`~contextvars.ContextVar`, because the cost chokepoint
(:func:`~auto_research.observability.tracing.traced_model_call`) sits far below any run object.
Calls made with no meter installed — a bare probe script, an isolated live test — are simply not
accumulated; wire a meter (``install_cost_meter(monitor=…)``) when such a caller wants the record.
"""

import json
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from auto_research.env import runtime_data_dir  # noqa: F401 — re-exported for observability callers
from auto_research.models.types import ModelUsage, ProviderName, RoleModelConfig


class TokenRates(BaseModel):
    """Per-token USD rates for one model's four billable token classes."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    source: str  # "litellm" or "config" — which table priced this call


class CallCost(BaseModel):
    """One priced LLM call — the payload of a ``cost.call`` monitor event.

    Token classes mirror :class:`ModelUsage` (non-overlapping). Cost is broken out per class so a
    rollup can attribute spend to caching vs. fresh input vs. output. Every field defaults, so a
    row recorded by an older/newer build still validates.
    """

    model_config = ConfigDict(extra="forbid")

    role: str
    member: str
    provider: ProviderName | str
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    input_cost_usd: float = Field(default=0.0, ge=0)
    output_cost_usd: float = Field(default=0.0, ge=0)
    cache_read_cost_usd: float = Field(default=0.0, ge=0)
    cache_write_cost_usd: float = Field(default=0.0, ge=0)
    total_cost_usd: float = Field(default=0.0, ge=0)
    price_source: str = "config"
    """Which table priced this row: ``litellm`` | ``config`` | ``claude_sdk`` (the coder's Agent-SDK
    session reports its own dollar cost, so no token pricing of ours is involved)."""


# Cached once: importing litellm is heavy, and its price map is static per process.
_LITELLM_MODEL_COST: dict | None = None
_LITELLM_TRIED = False


def _litellm_model_cost() -> dict:
    """Return litellm's ``model_cost`` map (lazy, cached). ``{}`` if litellm is unavailable."""

    global _LITELLM_MODEL_COST, _LITELLM_TRIED
    if not _LITELLM_TRIED:
        _LITELLM_TRIED = True
        try:
            import litellm  # heavy import — done once, only for the price map

            _LITELLM_MODEL_COST = dict(litellm.model_cost)
        except Exception:  # noqa: BLE001 — any litellm import/attr failure → config fallback
            _LITELLM_MODEL_COST = {}
    return _LITELLM_MODEL_COST or {}


def _litellm_candidates(provider: ProviderName, model: str) -> list[str]:
    """Candidate litellm price-map keys for a (provider, model), most specific first."""

    keys = [model, f"{provider}/{model}"]
    if provider == "google":
        keys.append(f"gemini/{model}")
    return keys


def _rates_from_litellm(provider: ProviderName, model: str) -> TokenRates | None:
    """Per-token rates from litellm's map, or ``None`` when the model is absent."""

    cost = _litellm_model_cost()
    for key in _litellm_candidates(provider, model):
        entry = cost.get(key)
        if not entry:
            continue
        in_rate = float(entry.get("input_cost_per_token", 0.0) or 0.0)
        out_rate = float(entry.get("output_cost_per_token", 0.0) or 0.0)
        if in_rate <= 0 and out_rate <= 0:
            # A known id carrying no rates is worse than an unknown one: it prices every token at
            # zero while reporting ``source="litellm"``, which reads as authoritative, and it
            # shadows the member's own ``priceMTk``. Treat it as absent so the fallback engages.
            # (`qwen3.7-plus` was exactly this on 2026-08-19.) A model that is genuinely free says
            # so explicitly with ``priceMTk: {input: 0, output: 0}``, which at least labels itself.
            continue
        return TokenRates(
            input=in_rate,
            output=out_rate,
            # litellm omits cache costs on models without caching → derive from input.
            cache_read=float(entry.get("cache_read_input_token_cost") or in_rate * 0.1),
            cache_write=float(entry.get("cache_creation_input_token_cost") or in_rate * 1.25),
            source="litellm",
        )
    return None


def context_window_for(config: RoleModelConfig) -> int | None:
    """The member's input limit in tokens: ``config.context_tokens`` when set (an override, e.g. a
    vendor that truncates silently before its window), else litellm's ``max_input_tokens`` — the
    same maintained map prices come from (board B5, 2026-10-18). ``None`` when neither knows."""

    if config.context_tokens:
        return int(config.context_tokens)
    cost = _litellm_model_cost()
    for key in _litellm_candidates(config.provider, config.model):
        entry = cost.get(key) or {}
        window = entry.get("max_input_tokens")
        if isinstance(window, int | float) and window > 0:
            return int(window)
    return None


def _rates_from_config(config: RoleModelConfig) -> TokenRates:
    """Per-token rates from ``config.priceMTk`` (per-MTok → per-token), cache tiers defaulted."""

    in_mtk = config.priceMTk.get("input", 0.0)
    return TokenRates(
        input=in_mtk / 1_000_000,
        output=config.priceMTk.get("output", 0.0) / 1_000_000,
        cache_read=config.priceMTk.get("cache_read", in_mtk * 0.1) / 1_000_000,
        cache_write=config.priceMTk.get("cache_write", in_mtk * 1.25) / 1_000_000,
        source="config",
    )


def token_rates_for(config: RoleModelConfig) -> TokenRates:
    """Resolve per-token rates for a model: litellm map first, config ``priceMTk`` fallback."""

    return _rates_from_litellm(config.provider, config.model) or _rates_from_config(config)


def cost_for_call(role: str, config: RoleModelConfig, usage: ModelUsage) -> CallCost:
    """Price one call from resolved rates and token usage.

    Prices each non-overlapping token class at its own rate; ``reasoning_tokens`` carries no separate
    charge (it is a subset of ``output_tokens``, already priced).
    """

    rates = token_rates_for(config)
    input_cost = usage.input_tokens * rates.input
    output_cost = usage.output_tokens * rates.output
    cache_read_cost = usage.cache_read_tokens * rates.cache_read
    cache_write_cost = usage.cache_write_tokens * rates.cache_write
    return CallCost(
        role=role,
        member=config.name,
        provider=config.provider,
        model=config.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        cache_read_cost_usd=cache_read_cost,
        cache_write_cost_usd=cache_write_cost,
        total_cost_usd=input_cost + output_cost + cache_read_cost + cache_write_cost,
        price_source=rates.source,
    )


class CostMeter:
    """Run-scoped accumulator of LLM spend, recording each call into the monitor ledger.

    Two producers feed it, and both land in the same ``cost.call`` event stream:

    - our own provider calls, priced here from token usage (``record_call``);
    - the tool-authoring **coder**, which is the Claude Agent SDK — a separate process making its
      own API calls, so it never passes through our provider layer. Its ``ResultMessage``
      reports a *session-cumulative* dollar figure, which ``record_session_total`` converts to a
      per-turn delta (``price_source="claude_sdk"``, no token attribution of ours).

    Recording is best-effort: a monitor fault degrades to "no event" rather than breaking a real
    model call (the [J2] rationale), and the running total is kept regardless.
    """

    def __init__(self, *, run_id: str = "", monitor: Any | None = None) -> None:
        self.run_id = run_id
        self.monitor = monitor  # duck-typed RunMonitor (kept loose to avoid an import cycle)
        self._lock = Lock()
        self._total_usd = 0.0
        self._calls = 0
        self._session_totals: dict[str, float] = {}

    @property
    def total_usd(self) -> float:
        """Accumulated USD spend so far in this run."""

        with self._lock:
            return self._total_usd

    @property
    def calls(self) -> int:
        """Number of priced calls accumulated so far."""

        with self._lock:
            return self._calls

    def snapshot(self) -> dict[str, float | int]:
        """Return ``{"total_usd", "calls"}`` as one consistent reading."""

        with self._lock:
            return {"total_usd": self._total_usd, "calls": self._calls}

    def seed_from_ledger(self, path: str | Path) -> float:
        """Start from the spend already recorded in ``path``; return the seeded total.

        A resumed run builds a fresh meter in a new process, so without this the accumulated
        figure stamped on every line restarts at $0.00 mid-run and reads as though the run had
        spent nothing (observed 2026-07-29: $8.80 before a kill, then a running total counting up
        from zero). The ledger is the record, so the meter reads its own history back. A fresh
        run's ledger holds no ``cost.call`` rows, which makes this a no-op there.
        """

        total, calls = 0.0, 0
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            return 0.0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "cost.call":
                continue
            total += float((event.get("data") or {}).get("total_cost_usd", 0.0) or 0.0)
            calls += 1
        if calls:
            with self._lock:
                self._total_usd += total
                self._calls += calls
        return total

    async def record_call(
        self, role: str, config: RoleModelConfig, usage: ModelUsage
    ) -> CallCost:
        """Price one provider call, add it to the running total, and log a ``cost.call`` event."""

        cost = cost_for_call(role, config, usage)
        await self._add(cost)
        return cost

    async def record_session_total(
        self,
        *,
        role: str,
        member: str,
        provider: str,
        model: str,
        session_id: str,
        cumulative_usd: float,
    ) -> float:
        """Record an externally-priced session's **delta** and return it.

        ``cumulative_usd`` is the session total reported so far (the Claude Agent SDK's
        ``ResultMessage.total_cost_usd``, which grows across a session's turns), so only the
        increment since the last reading is new spend.
        """

        with self._lock:
            previous = self._session_totals.get(session_id, 0.0)
            delta = max(0.0, float(cumulative_usd) - previous)
            self._session_totals[session_id] = max(previous, float(cumulative_usd))
        cost = CallCost(
            role=role,
            member=member,
            provider=provider,
            model=model,
            total_cost_usd=delta,
            price_source="claude_sdk",
        )
        await self._add(cost)
        return delta

    async def _add(self, cost: CallCost) -> None:
        """Accumulate one priced call, then emit its ``cost.call`` monitor event.

        The total is updated **before** the event is recorded (and the lock released before that
        call) so the event's own stamped ``cost_usd_total`` already includes this call, and the
        monitor can read the meter without contending for its lock.
        """

        with self._lock:
            self._total_usd += cost.total_cost_usd
            self._calls += 1
        monitor = self.monitor
        if monitor is None:
            return
        try:
            await monitor.record(
                "run",
                "cost.call",
                level=10,
                summary=(
                    f"{cost.role}/{cost.member} ${cost.total_cost_usd:.4f} "
                    f"({cost.price_source})"
                ),
                data=cost.model_dump(mode="json"),
            )
        except Exception:  # noqa: BLE001 — cost accounting never breaks a real model call
            pass


_CURRENT_METER: ContextVar[CostMeter | None] = ContextVar("auto_research_cost_meter", default=None)


def install_cost_meter(*, run_id: str = "", monitor: Any | None = None) -> CostMeter:
    """Create a :class:`CostMeter` for this run and make it the current one.

    Set from ``run_research`` before any model client exists: the meter object is shared by every
    task spawned afterwards (a task inherits a copy of the context, and the copy holds the same
    meter), so all of a run's spend accumulates in one place. When the monitor already has a
    ledger, the meter seeds itself from it, so a **resumed** run keeps counting from what the run
    had already spent instead of restarting at zero.
    """

    meter = CostMeter(run_id=run_id, monitor=monitor)
    ledger = getattr(monitor, "jsonl_path", None) if monitor is not None else None
    if ledger is not None:
        meter.seed_from_ledger(ledger)
    _CURRENT_METER.set(meter)
    return meter


def current_cost_meter() -> CostMeter | None:
    """Return the meter installed for the current run, or ``None`` outside a metered run."""

    return _CURRENT_METER.get()
