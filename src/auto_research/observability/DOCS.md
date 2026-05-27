# `auto_research.observability` — tracing & cost

## Purpose
Wires every LLM call through LangSmith for tracing and records per-call token
counts and dollar cost. Cost-tiering is a design pillar
([CLAUDE.md §6](../../../CLAUDE.md#6-config-driven-model-registry)), so visibility
into per-role spend must exist from day one.

## Architecture
- `tracing.py` — wraps provider calls from `ModelHandle.call()`. If
  `LANGCHAIN_TRACING_V2=true`, it uses LangSmith `traceable()` with role,
  member, provider, and model metadata. When tracing is not enabled, it runs the
  provider call directly.
- `costs.py` — turns normalized provider `ModelUsage` into a JSONL cost ledger
  row. The ledger lives in `AUTO_RESEARCH_RUNTIME_DIR/cost_ledger.jsonl`, or
  `~/.auto_research/cost_ledger.jsonl` by default.
- Pricing is read from each model member's `priceMTk` config. Missing rates default to zero, so
  token counts are still recorded even before exact pricing is configured.

## Interfaces / Contracts
- Every `ModelHandle.call()` records one ledger row after the provider returns.
- LangSmith tracing is opt-in via environment (`LANGCHAIN_TRACING_V2=true`) so
  offline tests and local calls do not block on tracing network behavior.
- Cost ledger rows are append-only JSONL and contain no prompt or response text.
  They include role, member, provider, model, token counts, and calculated
  dollar cost.
- Live LangSmith verification is opt-in:
  `AUTO_RESEARCH_LIVE_LANGSMITH_TESTS=1 pytest tests/test_live_langsmith.py -s`.
  It sends a short poem prompt to each unique configured model call, creates
  LangSmith traces, prints the local cost ledger rows, and writes timestamped
  response/cost logs under `logs/dev/step-3/`.
- Runtime LLM agents must not read this `DOCS.md`; this module only records
  runtime metadata emitted by provider calls.

## Dependencies
- External: `langsmith`, `pydantic`.
- Internal: hooks into `auto_research.models` via `ModelHandle.call()`.

## Dictionary-level reference
- `CostLedgerEntry(created_at: datetime, role: str, member: str,
  provider: ProviderName, model: str, input_tokens: int, output_tokens: int,
  input_cost_usd: float, output_cost_usd: float, total_cost_usd: float)` — one
  serialized ledger row for an LLM call.
- `runtime_data_dir() -> Path` — returns `AUTO_RESEARCH_RUNTIME_DIR`, expanded,
  or `~/.auto_research` when unset.
- `cost_ledger_path(data_dir: str | Path | None = None) -> Path` — returns the
  ledger file path.
- `cost_entry_for(role: str, config: RoleModelConfig, usage: ModelUsage) ->
  CostLedgerEntry` — calculates per-call cost from configured token rates.
- `append_cost_entry(entry: CostLedgerEntry, path: str | Path | None = None) ->
  Path` — appends one JSONL row and returns the path used.
- `traced_model_call(role: str, config: RoleModelConfig,
  provider_call: Callable[[], Awaitable[ModelResponse]]) -> ModelResponse` —
  wraps a provider call with optional LangSmith tracing and mandatory cost
  ledger logging.

## Changelog
- 2026-05-27 — Renamed per-model unit pricing to `priceMTk` and made live
  observability tests write timestamped logs under `logs/dev/step-3/`.
- 2026-05-27 — Added an opt-in live LangSmith test that sends short poem prompts
  through configured model calls and prints cost ledger rows.
- 2026-05-27 — Implemented step 3: provider-call tracing hook, JSONL cost
  ledger, runtime data-dir helper, and deterministic observability tests.
- 2026-05-26 — Folder seeded during step 0.
