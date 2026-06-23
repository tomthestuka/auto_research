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
`[HH:MM:SS] #NNNN team(model) Llevel event actor: summary | detail` when the
event payload includes a model identifier; non-model events use just `team`.

The monitor uses an XSPEC-style `chatter` level from 1 to 10. Higher values
record more detail; `chatter=10` is the full-chatty mode used for live run
inspection.

**JSONL vs human-visible split:** when a `jsonl_path` is set, ALL events (levels
1–10) are always written to the JSONL file regardless of `chatter`. Only events
at `level <= chatter` are kept in memory, rendered in Markdown, and written to
the optional live stream. This means the log file is always a complete
diagnostic trace, while terminal/notebook output is filtered by `chatter`.

## Interfaces / Contracts
- `RunMonitor(chatter=10, run_id=None, jsonl_path=None, stream=None)` creates one monitor.
  `chatter` must be in `1..10`; `stream` is an optional text stream that receives
  one concise line per visible event, prefixed with the event team and optional
  model string.
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
  `created_at`, `team`, `event`, `level`, `summary`, optional `actor`, and
  JSON-safe `data`.
- `RunMonitor` - lock-protected event collector, Markdown/JSONL renderer, and optional
  live progress-line streamer.

## Changelog
- 2026-05-28 -> 2026-06-12 — `RunMonitor` provides async-safe, full-fidelity JSONL
  streaming, chatter-filtered in-memory events, grouped Markdown rendering, offline JSONL
  reconstruction, and config-driven runtime paths.
- 2026-06-12 — Compacted changelog; no monitor behavior changes.
- 2026-06-15 — Added optional live progress-line streaming for visible monitor events;
  JSONL remains full fidelity while Markdown and live output stay chatter-filtered.
- 2026-06-15 — Live progress lines now prefix visible events with `team(model)`
  when model metadata is present and include compact per-tool result details
  (`tool`, `status`, `chars`, `figures`) without dumping tool output bodies.
- 2026-06-22 — **Full-fidelity diagnostic capture** (the monitor itself is unchanged; emitters
  now populate richer `data`). ReAct `*.analyze.tool_result` events carry the tool-call `args`
  and the returned `text` (capped — see `agents/DOCS.md`); the tool-authoring flow emits a
  `tooling`-team stream — `coder.session_start`/`coder.tool_use`/`coder.tool_result`/
  `coder.text`/`coder.turn_end` (the SDK author session), `coder.ask_lead`/`coder.ask_human`
  (escalations), and `gate.result`/`review.result`/`commit` (see `sandbox/DOCS.md`). The terminal
  renderer still surfaces only its select keys, so this heavy text lands in the JSONL only — the
  log is now a complete trace of a run (incl. the previously-opaque coder/gate phases) without
  flooding the terminal.
- 2026-06-23 — **Tool input/output are now separate events.** `_react` emits
  `*.{action}.tool_call` (the input) **before** dispatching each call — carrying the
  tool-call `args` and a `tool_call_id` — and `*.{action}.tool_result` (the output) when the
  call returns, carrying the returned `text` and the same `tool_call_id`. Because `record()`
  flushes each event to the JSONL immediately, a tool that hangs and never returns now leaves
  a `tool_call` with no matching `tool_result`, so a stalled run is diagnosable down to the
  exact command from the log alone. (`args` moved off `tool_result` onto `tool_call`.)
