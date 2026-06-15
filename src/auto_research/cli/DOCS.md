# `auto_research.cli` — CLI entry point

## Purpose
Lets a human run a full research request without writing Python:
`python -m auto_research "your question here"`.

## Architecture
A `__main__.py` delegates to `cli.main`. The CLI parses args, validates the
launch environment via `check_required_env()` (which loads `.env`), calls
`auto_research.graph.run_research()`, writes the full monitor JSONL plus a
grouped Markdown report, streams compact monitor progress lines to stderr during
the run, and prints the final `ReviewVerdict`.

## Interfaces / Contracts
- Command: `python -m auto_research "your question here"`.
- Positional `request`: research question.
- `--context TEXT`: optional request context.
- `--constraint TEXT`: optional repeatable request constraint.
- `--chatter N`: monitor verbosity from 1 to 10. Controls live stderr summaries
  and the grouped Markdown report; the JSONL log remains full fidelity.
- `--monitor-jsonl PATH`: optional JSONL output path. Defaults to a timestamped
  file under the runtime data dir.
- `--monitor-md PATH`: optional grouped Markdown report path. Defaults beside
  the JSONL file.
- `--format text|json`: print human text or a JSON object with verdict and
  monitor paths. Live progress uses stderr so JSON stdout stays machine-readable.

## Dependencies
- External: `python-dotenv`.
- Internal: `auto_research.graph`, `auto_research.monitor`,
  `auto_research.schemas`.

## Dictionary-level reference
- `build_parser() -> argparse.ArgumentParser` — constructs the command-line
  parser.
- `main(argv=None) -> int` — CLI entry. Calls `check_required_env()` (loads `.env`
  and crashes with a clear message if any required key is absent), runs one monitored
  request with live stderr progress, writes monitor artifacts, and prints the final verdict.
- `auto_research.__main__` — module entry point for `python -m auto_research`.

## Changelog
- 2026-05-26 -> 2026-06-12 — CLI supports `python -m auto_research`, chatter control,
  JSON/text output, monitor JSONL/Markdown paths, repeatable report formats, report artifact
  printing, `.env` loading, and required-key validation before the run starts.
- 2026-06-12 — Compacted changelog; no CLI behavior changes.
- 2026-06-15 — CLI now constructs a streaming `RunMonitor`, printing concise live
  progress lines to stderr while preserving JSON stdout for `--format json`.
- 2026-06-15 — Removed duplicate CLI dotenv loading; shared `check_required_env()`
  now loads `.env` for all entry points before validating required keys.
