# `auto_research.monitor` - run monitor

## Purpose
Records a durable, human-readable trace of a research run: inputs, agent
outputs, tool calls and results, consensus rounds, lead summaries, graph
transitions, and final verdicts. It is for library/CLI/notebook inspection, not
runtime agent context.

## Architecture
`events.py` provides a `RunMonitor` that stores sequence-numbered
`MonitorEvent` rows in memory and can optionally stream JSONL rows to a file.
Recording is protected by a lock so async graph nodes and agent calls do not
interleave writes. Markdown rendering groups events into logical team blocks in
this order: run, reviewer, librarians, analysts, tools, orchestration.

The monitor uses an XSPEC-style `chatter` level from 1 to 10. Higher values
record more detail; `chatter=10` is the full-chatty mode used for live run
inspection.

## Interfaces / Contracts
- `RunMonitor(chatter=10, run_id=None, jsonl_path=None)` creates one monitor.
  `chatter` must be in `1..10`.
- `await RunMonitor.record(team, event, *, level, summary, actor=None,
  data=None)` records an event when `level <= chatter`.
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
  timestamped monitor JSONL path under `AUTO_RESEARCH_RUNTIME_DIR/monitors/`
  or `~/.auto_research/monitors/`.
- `MonitorEvent` - Pydantic event row with `run_id`, `sequence`,
  `created_at`, `team`, `event`, `level`, `summary`, optional `actor`, and
  JSON-safe `data`.
- `RunMonitor` - lock-protected event collector and Markdown/JSONL renderer.

## Changelog
- 2026-05-28 — Step 12 built: `RunMonitor` (1–10 chatter scale, async-safe event collection, optional JSONL streaming, team-block Markdown reports via `write_markdown(grouped=True)`). `from_jsonl()` for offline rendering.
