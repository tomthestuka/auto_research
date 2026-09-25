# `auto_research.monitor` - run monitor

## Purpose
Records a durable, human-readable trace of a research run: inputs, agent
outputs, tool calls and results, consensus rounds, lead summaries, graph
transitions, and final verdicts. It can also emit concise live progress lines to
a caller-provided stream. It is for library/CLI/notebook inspection, not runtime
agent context.

## Architecture
`events.py` provides a `RunMonitor` that stores sequence-numbered
`MonitorEvent` rows in memory and can optionally stream JSONL rows to a file
and compact progress lines to a terminal/notebook stream.
Recording is protected by a lock so async graph nodes and agent calls do not
interleave writes. Markdown rendering groups events into logical team blocks in
this order: run, reviewer, librarians, analysts, tools, orchestration.
Live stream lines use the compact shape
`[HH:MM:SS] #NNNN $cost team(model) Llevel event actor: summary | detail` when the
event payload includes a model identifier; non-model events use just `team`. The `$cost`
column is the run's **accumulated** LLM spend at that event and is present only when a
cost meter is attached (otherwise the line is the pre-cost format, byte for byte).

`summary.py` derives the **end-of-run digest** from the event stream and renders it: what the
verdict was, how long the run took, what it cost (total, by role, and the coder's Agent-SDK
share), which tools were authored and committed, how much gate/co-lead rework was demanded, how
often a human was raised to, and — one labelled line each, full paths — where the report artifacts,
the run dir and the ledger itself are (recorded on `run.start`, so a run that produced no report
still says where to look). It is computed from the
ledger — never assembled at a call site — recorded back as one level-1 `run.summary` event, and
rendered from that stored dict by every consumer (live stream, detached follower,
`ResearchRun.summary()`, a replay of a run that died). `_format_stream_event` special-cases
`run.summary` and prints the block rather than a one-line summary.

**Cost lives here, and only here.** Each priced LLM call is one level-10 `cost.call` event
carrying the `CallCost` payload (role, member, provider, model, the four token classes, per-class
cost, `price_source`), and *every* event carries `cost_usd_total`. The monitor JSONL is therefore
the single source of truth for a run's spend as well as its trace ([J1]); there is no separate
cost ledger, so per-run attribution is intrinsic and cannot drift. The accumulator itself lives in
`observability.costs.CostMeter` and is attached as `RunMonitor.cost_meter` (duck-typed, so this
module imports nothing from `observability`).

The monitor uses an XSPEC-style `chatter` level from 1 to 10. Higher values
record more detail; `chatter=10` is the full-chatty mode used for live run
inspection.

**The ledger is append-only.** Constructing a monitor over an existing JSONL **appends** and
continues its numbering (`_last_sequence`, plus a newline first if a hard kill left a torn line) —
it never truncates. A resumed run builds a new monitor over the same file, so erasing here threw
away everything recorded before the interruption. Only a deliberate fresh start is destructive, and
that is `run_research_nohup(overwrite=True)`, which removes the whole run dir. `write_jsonl()`
refuses the live path for the same reason (it writes chatter-filtered in-memory events, so it is an
export, not the record).

**JSONL vs human-visible split:** when a `jsonl_path` is set, ALL events (levels
1–10) are always written to the JSONL file regardless of `chatter`. Only events
at `level <= chatter` are kept in memory, rendered in Markdown, and written to
the optional live stream. This means the log file is always a complete
diagnostic trace, while terminal/notebook output is filtered by `chatter`.

## Interfaces / Contracts
- `RunMonitor(chatter=10, run_id=None, jsonl_path=None, stream=None, cost_meter=None)` creates one
  monitor. `chatter` must be in `1..10`; `stream` is an optional text stream that receives
  one concise line per visible event, prefixed with the event team and optional
  model string; `cost_meter` is any object exposing `.total_usd` (the run's
  `observability.costs.CostMeter`), whose reading is stamped on every event as `cost_usd_total`.
  `run_research` sets both the meter and the monitor's `run_id` (the **run's** id, so a log line
  names the run dir a reader can open).
- `await RunMonitor.record(team, event, *, level, summary, actor=None,
  data=None)` always streams to JSONL; stores in memory and returns the event
  only when `level <= chatter`; when `stream` is set, also writes one compact
  progress line for visible events.
- `RunMonitor.events` returns a stable tuple snapshot.
- `RunMonitor.events_by_team()` returns events grouped in report order.
- `RunMonitor.render_markdown(grouped=True)` returns a Markdown report.
- `RunMonitor.write_jsonl(path=None)` writes all events as JSONL.
- `RunMonitor.write_markdown(path, *, grouped=True)` writes a Markdown report. Pass `grouped=False` for sequential (pipeline-order) output.
- `RunMonitor.from_jsonl(path, *, chatter=10)` reconstructs a RunMonitor from a previously written JSONL file (for offline rendering).
- Monitor payloads are converted to JSON-safe values. Pydantic models are
  dumped in JSON mode, paths become strings, and unknown objects fall back to
  `repr()`.

## Dependencies
- External: `pydantic`.
- Internal: used by `auto_research.agents`, `auto_research.consensus`,
  `auto_research.graph`, and `auto_research.cli`.

## Dictionary-level reference
- `TEAM_ORDER` - canonical team grouping order for reports.
- `TEAM_LABELS` - display labels for known team names.
- `validate_chatter(chatter: int) -> int` - validates the `1..10` chatter
  range.
- `default_monitor_jsonl_path(stem: str = "run") -> Path` - returns a
  timestamped monitor JSONL path under `<runtime_dir>/monitors/`
  (`runtime_dir` from `config/orchestration.yaml`).
- `MonitorEvent` - Pydantic event row with `run_id`, `sequence`,
  `created_at`, `team`, `event`, `level`, `summary`, optional `actor`,
  JSON-safe `data`, and `cost_usd_total` (the run's accumulated spend at that event; `None`
  without a meter, so pre-cost logs still validate).
- `RunMonitor` - lock-protected event collector, Markdown/JSONL renderer, and optional
  live progress-line streamer.
- `summary.build_run_summary(events) -> dict` - derive the end-of-run digest from monitor events
  (`MonitorEvent`s or plain JSONL dicts).
- `summary.summarize_jsonl(path) -> dict` - same, read from a monitor JSONL (needed below
  `chatter=10`, where `cost.call` events exist only in the file).
- `summary.render_run_summary(summary) -> str` - render a stored digest as the plain-text block.
- `summary.summary_from_events(events) -> dict | None` - the `run.summary` payload already
  recorded, so consumers render what the run computed instead of recomputing it.

## Changelog
Compacted 2026-08-05 and again 2026-09-24 (detail in git history).

- 2026-05-28 -> 2026-06-23 — `RunMonitor` built: async-safe JSONL streaming, chatter-filtered events,
  grouped Markdown, offline reconstruction, live `team(model)` progress lines, a `tooling`-team stream
  (`coder.*`, `gate.result`, `review.result`, `commit`), and paired `*.tool_call`/`*.tool_result` events.
- 2026-07-29 — [J1] the ledger is the single source of truth: `Preflight.run_checklist(monitor=…)` records
  `preflight.<name>` + `preflight.summary` (guarded, [J2]); `MonitorEvent.cost_usd_total`,
  `RunMonitor.cost_meter` and per-call `cost.call` replace `cost_ledger.jsonl`.
- 2026-07-29 — `summary.py` derives `run.summary` from the event stream (`cost_usd` = sum of `cost.call`).
  The monitor's `run_id` is the run's id; `__init__` appends and continues numbering; `write_jsonl()`
  refuses to rewrite the live ledger.
- 2026-09-01 — Per-field ledger caps are config (`ledger_arg_cap`/`ledger_result_cap`/
  `ledger_coder_text_cap` via `env.ledger_caps()`), raised 2,000/4,000 → 100,000; trimmed fields keep
  their `"[+N chars]"` marker.
- 2026-09-01 — New events `coder.ask_human.unattended` (L3) and `coder.ask_human.error` (L1); neither is
  followed by `.answer`.

