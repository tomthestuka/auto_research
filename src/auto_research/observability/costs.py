"""Per-call cost ledger helpers."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from auto_research.models.types import ModelUsage, ProviderName, RoleModelConfig


class CostLedgerEntry(BaseModel):
    """One JSON-serializable LLM call cost row."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    role: str
    member: str
    provider: ProviderName
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    input_cost_usd: float = Field(ge=0)
    output_cost_usd: float = Field(ge=0)
    total_cost_usd: float = Field(ge=0)


def runtime_data_dir() -> Path:
    """Return the runtime data dir used for observability artifacts."""

    return Path(os.getenv("AUTO_RESEARCH_RUNTIME_DIR", "~/.auto_research")).expanduser()


def cost_ledger_path(data_dir: str | Path | None = None) -> Path:
    """Return the JSONL cost ledger path."""

    root = Path(data_dir).expanduser() if data_dir is not None else runtime_data_dir()
    return root / "cost_ledger.jsonl"


def cost_entry_for(role: str, config: RoleModelConfig, usage: ModelUsage) -> CostLedgerEntry:
    """Calculate a ledger entry from config pricing and token usage."""

    input_rate = config.pricing_usd_per_million_tokens.get("input", 0.0)
    output_rate = config.pricing_usd_per_million_tokens.get("output", 0.0)
    input_cost = usage.input_tokens * input_rate / 1_000_000
    output_cost = usage.output_tokens * output_rate / 1_000_000
    return CostLedgerEntry(
        role=role,
        member=config.name,
        provider=config.provider,
        model=config.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        input_cost_usd=input_cost,
        output_cost_usd=output_cost,
        total_cost_usd=input_cost + output_cost,
    )


def append_cost_entry(entry: CostLedgerEntry, path: str | Path | None = None) -> Path:
    """Append a cost entry as one JSONL row and return the path used."""

    ledger_path = Path(path).expanduser() if path is not None else cost_ledger_path()
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True))
        stream.write("\n")
    return ledger_path
