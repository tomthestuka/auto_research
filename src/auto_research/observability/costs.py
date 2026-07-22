"""Per-call cost ledger helpers.

Cost is priced **litellm-first, config-fallback** ([I5]): litellm ships a community-maintained
per-model price map (``litellm.model_cost``) that encodes input/output **and** cache-read /
cache-creation per-token costs, kept current far more reliably than a hand-edited table. When
litellm does not know a model (our bleeding-edge / renamed ids can outrun its bundled map), we fall
back to ``config/models.yaml``'s ``priceMTk`` — with cache tiers defaulted from the input rate
(0.1x read, 1.25x write) when not pinned. Either way the four neutral :class:`ModelUsage` token
classes are non-overlapping, so cost is a plain weighted sum.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from auto_research.env import runtime_data_dir
from auto_research.models.types import ModelUsage, ProviderName, RoleModelConfig


class TokenRates(BaseModel):
    """Per-token USD rates for one model's four billable token classes."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    source: str  # "litellm" or "config" — which table priced this call


class CostLedgerEntry(BaseModel):
    """One JSON-serializable LLM call cost row.

    Token classes mirror :class:`ModelUsage` (non-overlapping). Cost is broken out per class so the
    rollup can attribute spend to caching vs. fresh input vs. output. New fields default to 0, so
    rows written before [I5] still validate.
    """

    model_config = ConfigDict(extra="forbid")

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    role: str
    member: str
    provider: ProviderName
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    input_cost_usd: float = Field(ge=0)
    output_cost_usd: float = Field(ge=0)
    cache_read_cost_usd: float = Field(default=0.0, ge=0)
    cache_write_cost_usd: float = Field(default=0.0, ge=0)
    total_cost_usd: float = Field(ge=0)
    price_source: str = "config"  # which price table priced this row (litellm | config)


def cost_ledger_path(data_dir: str | Path | None = None) -> Path:
    """Return the JSONL cost ledger path."""

    root = Path(data_dir).expanduser() if data_dir is not None else runtime_data_dir()
    return root / "cost_ledger.jsonl"


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
        return TokenRates(
            input=in_rate,
            output=float(entry.get("output_cost_per_token", 0.0) or 0.0),
            # litellm omits cache costs on models without caching → derive from input.
            cache_read=float(entry.get("cache_read_input_token_cost") or in_rate * 0.1),
            cache_write=float(entry.get("cache_creation_input_token_cost") or in_rate * 1.25),
            source="litellm",
        )
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


def cost_entry_for(role: str, config: RoleModelConfig, usage: ModelUsage) -> CostLedgerEntry:
    """Calculate a ledger entry from resolved pricing and token usage.

    Prices each non-overlapping token class at its own rate; ``reasoning_tokens`` carries no separate
    charge (it is a subset of ``output_tokens``, already priced).
    """

    rates = token_rates_for(config)
    input_cost = usage.input_tokens * rates.input
    output_cost = usage.output_tokens * rates.output
    cache_read_cost = usage.cache_read_tokens * rates.cache_read
    cache_write_cost = usage.cache_write_tokens * rates.cache_write
    return CostLedgerEntry(
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


def append_cost_entry(entry: CostLedgerEntry, path: str | Path | None = None) -> Path:
    """Append a cost entry as one JSONL row and return the path used."""

    ledger_path = Path(path).expanduser() if path is not None else cost_ledger_path()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True))
        stream.write("\n")
    return ledger_path
