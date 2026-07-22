# `auto_research.observability` — tracing & cost

## Purpose
Wires every LLM call through LangSmith for tracing and records per-call token
counts and dollar cost. Cost-tiering is a design pillar
([CLAUDE.md §6](../../../CLAUDE.md#6-config-driven-model-registry)), so visibility
into per-role spend must exist from day one. Also houses `plot_run.py` (per-run Gantt + cost pie)
and `usage.py` (cumulative spend rollup CLI).

## Architecture
- `tracing.py` — wraps provider calls from `ModelHandle.call()`. If `LANGCHAIN_TRACING_V2=true`, it
  uses LangSmith `traceable()` with role/member/provider/model + `ls_provider`/`ls_model_name`
  metadata, and after the call attaches `usage_metadata` (LangSmith-standard token shape, incl.
  cache/reasoning) plus our computed `cost_usd`/breakdown to the run — so the LangSmith dashboard
  shows spend even for models its own price map doesn't know ([I5]). Attachment is best-effort and
  never fatal. When tracing is off, it runs the provider call directly. Either way it appends one
  cost-ledger row.
- `costs.py` — turns normalized provider `ModelUsage` into a JSONL cost ledger row. The ledger
  accretes across **all** runs at `<runtime_dir>/cost_ledger.jsonl` (runtime dir set in
  `config/orchestration.yaml::runtime_dir`; defaults to `~/.auto_research`).
- **Pricing is litellm-first, config-fallback** ([I5]): `token_rates_for` looks a model up in
  litellm's community-maintained `model_cost` map (lazy-imported once, cached; correct per-model
  input/output/cache-read/cache-creation rates) and falls back to the member's `priceMTk` config
  when litellm doesn't know the model (our bleeding-edge/renamed ids can outrun its bundled map).
  On the config path `cache_read`/`cache_write` rates are optional `priceMTk` keys defaulting to
  0.1x / 1.25x the input rate. Each row records which table priced it (`price_source`). Missing
  rates → 0 (tokens still recorded).
- `usage.py` — reads the cumulative ledger and reports total + by provider/model/role/day, with an
  optional `--since` cutoff. Answers "how much have I burnt" without a provider admin panel; it is
  an **estimate vs. list prices** (cannot reconcile to a negotiated bill, which needs an admin key).

## Interfaces / Contracts
- Every `ModelHandle.call()` records one ledger row after the provider returns.
- LangSmith tracing is opt-in via environment (`LANGCHAIN_TRACING_V2=true`) so
  offline tests and local calls do not block on tracing network behavior.
- Cost ledger rows are append-only JSONL and contain no prompt or response text. They include role,
  member, provider, model, the five token classes, per-class + total dollar cost, and `price_source`.
- Spend rollup: `python -m auto_research.observability.usage [--since YYYY-MM-DD] [--ledger PATH]
  [--top N]` — total, by provider/model/role/day; sums each row's stored `total_cost_usd` (so it is
  correct regardless of whether litellm is installed at rollup time).
- Live LangSmith verification is opt-in:
  `AUTO_RESEARCH_LIVE_LANGSMITH_TESTS=1 pytest tests/test_live_langsmith.py -s`.
  It sends a short poem prompt to each unique configured model call, creates
  LangSmith traces, prints the local cost ledger rows, and writes timestamped
  response/cost logs under `logs/dev/step-3/`.
- Runtime LLM agents must not read this `DOCS.md`; this module only records
  runtime metadata emitted by provider calls.

## Dependencies
- External: `langsmith`, `pydantic`, `litellm` (lazy-imported, price map only), `matplotlib`/`pyyaml`
  (`plot_run.py` only).
- Internal: hooks into `auto_research.models` via `ModelHandle.call()`.

## Dictionary-level reference
- `TokenRates(input, output, cache_read, cache_write: float, source: str)` — resolved per-token USD
  rates for one model; `source` is `"litellm"` or `"config"`.
- `token_rates_for(config: RoleModelConfig) -> TokenRates` — litellm map first, `priceMTk` fallback.
- `CostLedgerEntry(created_at, role, member, provider, model, input_tokens, output_tokens,
  cache_read_tokens=0, cache_write_tokens=0, reasoning_tokens=0, input_cost_usd, output_cost_usd,
  cache_read_cost_usd=0, cache_write_cost_usd=0, total_cost_usd, price_source="config")` — one
  serialized ledger row. New fields default so pre-[I5] rows still validate.
- `runtime_data_dir() -> Path` — re-exported from `auto_research.env`.
- `cost_ledger_path(data_dir=None) -> Path` — the ledger file path.
- `cost_entry_for(role, config, usage) -> CostLedgerEntry` — prices each non-overlapping token class
  via `token_rates_for`; `reasoning_tokens` carries no separate charge.
- `append_cost_entry(entry, path=None) -> Path` — appends one JSONL row.
- `traced_model_call(role, config, provider_call) -> ModelResponse` — optional LangSmith tracing
  (with usage/cost annotation) + mandatory cost-ledger logging.
- `usage.load_entries(path, since=None)` / `usage.render_report(entries, since, top)` — rollup
  helpers behind the `python -m auto_research.observability.usage` CLI.

## Changelog
- 2026-05-26 -> 2026-06-12 — LangSmith tracing, JSONL cost ledger, config-priced per-call costing,
  config-driven runtime data dir, opt-in live LangSmith verification, and `plot_run.py` for
  monitor Gantt/cost visualization.
- 2026-07-22 — [I5] cost visibility: pricing is now **litellm-first, config-fallback**
  (`token_rates_for`/`TokenRates`, litellm `model_cost` lazy-cached); `CostLedgerEntry` gains the
  cache/reasoning token classes + per-class cost + `price_source`; `traced_model_call` annotates the
  LangSmith run with `usage_metadata` + `cost_usd` so the dashboard shows spend; new `usage.py`
  rollup CLI (total / by provider·model·role·day / `--since`); `plot_run.py` prices via the same
  path (cache/reasoning-aware). Fixes the under-count where cached ReAct input was uncounted. Tests
  in `tests/test_observability.py`. `litellm>=1.93` added to `environment.yml` (conda-forge).
