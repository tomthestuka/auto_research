# `auto_research.cli` — CLI entry point

## Purpose
Lets a human run a full research request without writing Python:
`python -m auto_research "your question here"`.

## Architecture
A `__main__.py` delegates to `cli.main`. The CLI parses args and calls
`auto_research.graph.run_research()` (which owns the now-async launch preflight: `.env` +
keys, author↔reviewer decorrelation, and the human-channel ping), writes the full monitor
JSONL plus a
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
- `--human {auto,cli,none,slack,whatsapp}` (default `auto`): the raise-to-human channel a
  blocked agent uses, passed as `run_research(human_channel=…)`. `auto` picks the terminal
  (stdin) when interactive, else the configured connector; `none` runs headless.

## Dependencies
- External: `python-dotenv`.
- Internal: `auto_research.graph`, `auto_research.monitor`,
  `auto_research.schemas`.

## Dictionary-level reference
- `build_parser() -> argparse.ArgumentParser` — constructs the command-line
  parser.
- `main(argv=None) -> int` — CLI entry. Parses args (incl. `--human`), runs one monitored
  request via `run_research(human_channel=args.human)` — which owns the async launch preflight
  (env + decorrelation + channel ping; crashes with a clear aggregated message on any failure)
  — with live stderr progress, writes monitor artifacts, and prints the end-of-run summary block
  (`ResearchRun.summary()`: cost, wall clock, tools authored, report paths) followed by the final
  verdict fields.
- `auto_research.__main__` — module entry point for `python -m auto_research`.

## Changelog
- 2026-05-26 -> 2026-06-15 — CLI built: chatter, JSON/text output, monitor paths, report formats,
  `.env` + `check_required_env()`; streaming `RunMonitor`; preflight owned by `run_research`;
  `--human {auto,cli,none,slack,whatsapp}`.
- 2026-07-29 -> 2026-07-31 — `--format text` leads with `ResearchRun.summary()`. Ctrl-C/SIGTERM
  route through `RunControl.interrupt` (`install_signal_handlers=True`); `UserInterruptionError`
  writes `result.json` status `interrupted` and exits 130.
- 2026-09-25 — [R1] Text output prints `Assigned to:` in place of the two removed verdict flags.
