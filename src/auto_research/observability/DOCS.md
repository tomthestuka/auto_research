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
  never fatal. When tracing is off, it runs the provider call directly. Either way it records the
  call's cost on the run's cost meter.
- `costs.py` — prices normalized provider `ModelUsage` into a `CallCost` and accumulates it on a
  run-scoped `CostMeter`, which records **one `cost.call` monitor event per call** and stamps its
  running total on every monitor event (`cost_usd_total`). **The monitor JSONL is the only cost
  record** ([J1]): recording and replay read the same file, so per-run attribution is intrinsic and
  two logs cannot drift. The pre-2026-07-29 `<runtime_dir>/cost_ledger.jsonl` — a single run-blind
  cumulative file — is gone.
  The meter is found through a `ContextVar` (`install_cost_meter` / `current_cost_meter`) because
  the cost chokepoint sits far below any run object; tasks spawned after installation share the
  same meter. A call with **no** meter installed (a probe script, an isolated live test) runs
  unchanged and is simply not accumulated.
- **The coder's Agent-SDK spend is included.** The tool-authoring coder is a separate process making
  its own API calls, so it never passes through `ModelHandle.call`; its `ResultMessage` reports a
  *session-cumulative* dollar figure, which `CostMeter.record_session_total` folds in as a per-turn
  delta (`price_source="claude_sdk"`, no token attribution of ours). Before this it was invisible to
  every total — historically the largest unaccounted line (~$9 in one observed session).
- **Pricing is litellm-first, config-fallback** ([I5]): `token_rates_for` looks a model up in
  litellm's community-maintained `model_cost` map (lazy-imported once, cached; correct per-model
  input/output/cache-read/cache-creation rates) and falls back to the member's `priceMTk` config
  when litellm doesn't know the model (our bleeding-edge/renamed ids can outrun its bundled map).
  On the config path `cache_read`/`cache_write` rates are optional `priceMTk` keys defaulting to
  0.1x / 1.25x the input rate. Each row records which table priced it (`price_source`). Missing
  rates → 0 (tokens still recorded).
- `usage.py` — rolls up `cost.call` events across monitor ledgers (`<runtime_dir>/runs/*/monitor.jsonl`
  plus `<runtime_dir>/monitors/*.jsonl` by default) and reports total + by provider/model/role/day,
  optionally by run, with an optional `--since` cutoff. Rows are de-duplicated on
  `(run_id, sequence)` so a ledger copied to `logs/dev` is never counted twice. Answers "how much
  have I burnt" without a provider admin panel; it is an **estimate vs. list prices** (cannot
  reconcile to a negotiated bill, which needs an admin key).
- `plot_run.py` — the cost pie reads the run's recorded `cost.call` events when present (true
  member/model/cost, coder sessions included) and falls back to its pair-matched token estimate for
  logs written before cost events existed.

## Interfaces / Contracts
- Every `ModelHandle.call()` records its cost on the current run's meter after the provider returns
  (no meter installed → no cost record, the call is unaffected).
- LangSmith tracing is opt-in via environment (`LANGCHAIN_TRACING_V2=true`) so
  offline tests and local calls do not block on tracing network behavior.
- `cost.call` event payloads contain no prompt or response text. They include role, member,
  provider, model, the five token classes, per-class + total dollar cost, and `price_source`.
- Spend rollup: `python -m auto_research.observability.usage [--since YYYY-MM-DD] [--jsonl PATH]
  [--ledger-dir DIR] [--runs] [--top N]` — total, by provider/model/role/day (+ by run with
  `--runs`); sums each event's stored `total_cost_usd` (so it is correct regardless of whether
  litellm is installed at rollup time).
- Live LangSmith verification is opt-in:
  `AUTO_RESEARCH_LIVE_LANGSMITH_TESTS=1 pytest tests/test_live_langsmith.py -s`.
  It sends a short poem prompt to each unique configured model call, creates
  LangSmith traces, installs a meter over a throwaway monitor, prints the `cost.call` rows it
  produced, and writes timestamped response/cost logs under `logs/dev/step-3/`.
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
- `CallCost(role, member, provider, model, input_tokens=0, output_tokens=0, cache_read_tokens=0,
  cache_write_tokens=0, reasoning_tokens=0, input_cost_usd=0, output_cost_usd=0,
  cache_read_cost_usd=0, cache_write_cost_usd=0, total_cost_usd=0, price_source="config")` — one
  priced call; the payload of a `cost.call` monitor event. Every field defaults, so a payload from
  another build still validates. `price_source` is `litellm` | `config` | `claude_sdk`.
- `runtime_data_dir() -> Path` — re-exported from `auto_research.env`.
- `cost_for_call(role, config, usage) -> CallCost` — prices each non-overlapping token class via
  `token_rates_for`; `reasoning_tokens` carries no separate charge.
- `CostMeter(run_id="", monitor=None)` — run-scoped accumulator. `.total_usd` / `.calls` /
  `.snapshot()` read the running state; `await .record_call(role, config, usage) -> CallCost` prices
  and records one provider call; `await .record_session_total(role, member, provider, model,
  session_id, cumulative_usd) -> float` folds an externally-priced session's cumulative figure in as
  a delta (the coder's Agent SDK) and returns it. Recording is best-effort — a monitor fault never
  breaks a model call ([J2]).
- `install_cost_meter(run_id="", monitor=None) -> CostMeter` / `current_cost_meter() -> CostMeter |
  None` — install the meter for this run / read the installed one. Install seeds the meter from the
  monitor's existing ledger (`CostMeter.seed_from_ledger`), so a **resumed** run keeps counting from
  what it had already spent instead of restarting at $0.00 mid-ledger.
- `traced_model_call(role, config, provider_call) -> ModelResponse` — optional LangSmith tracing
  (with usage/cost annotation) + cost recording on the run's meter.
- `usage.default_sources(data_dir=None)` / `usage.load_cost_records(paths, since=None)` /
  `usage.render_report(rows, since, top, by_run=False)` — rollup helpers behind the
  `python -m auto_research.observability.usage` CLI.

## Changelog
Compacted 2026-08-05: the 2026-07-29 entries folded into one (detail in git history).

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
- 2026-07-29 — **Cost moved into the monitor ledger; the separate ledger deleted; the coder's SDK
  spend included; totals span a resume.** `CostLedgerEntry`/`cost_entry_for`/`append_cost_entry`/
  `cost_ledger_path` are replaced by `CallCost`/`cost_for_call` plus a run-scoped `CostMeter`
  (installed by `run_research`, found via a `ContextVar`) that records one level-10 `cost.call` event
  per call and stamps the running total on **every** monitor event. `<runtime_dir>/cost_ledger.jsonl`
  is no longer written: it was a second, run-blind log whose rows under-counted, and everything
  derived from it is answerable from the event stream ([J1]). The coder's Agent-SDK spend lands in the
  same stream via `record_session_total`, converting its session-cumulative `total_cost_usd` into
  per-turn deltas — previously it reached no total anywhere. `usage.py` rolls up over monitor ledgers
  (`load_cost_records`, de-duplicated on `(run_id, sequence)`, `--jsonl`/`--ledger-dir`/`--runs`), and
  `plot_run.py`'s pie prefers recorded cost and no longer drops non-analyst teams from a chart
  claiming a total. `CostMeter.seed_from_ledger` reads the run's own `cost.call` rows back at install
  time, since a resumed run builds a fresh meter in a new process and its accumulated figure had
  restarted at $0.00 mid-run. Tests: `tests/test_observability.py`, `tests/test_monitor.py`.

- 2026-08-19 — **A litellm entry with no rates is now treated as absent.** `_rates_from_litellm`
  returned on the first entry it found, so a model litellm *knows* but does not price booked every
  token at zero while reporting `source="litellm"` — and because the resolver is litellm-first, the
  member's own `priceMTk` could never override it. Found live: a Qwen coder leg on `qwen3.7-plus`
  recorded 40 model calls at `$0.00` and no configured price could fix it. An entry whose input and
  output rates are both non-positive now falls through to `priceMTk`; a genuinely free model states
  that explicitly, which at least labels the zero as ours. Tests: `test_observability.py` 13 → 15.
