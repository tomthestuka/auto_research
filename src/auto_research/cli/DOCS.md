# `auto_research.cli` — CLI entry point

## Purpose
Lets a human run a full research request without writing Python:
`python -m auto_research "your question here"`.

## Architecture
A `__main__.py` delegates to `cli.main`. The CLI parses args, loads `.env`,
calls `auto_research.graph.run_research()`, writes the full monitor JSONL plus
a grouped Markdown report, and prints the final `ReviewVerdict`.

## Interfaces / Contracts
- Command: `python -m auto_research "your question here"`.
- Positional `request`: research question.
- `--context TEXT`: optional request context.
- `--constraint TEXT`: optional repeatable request constraint.
- `--chatter N`: monitor verbosity from 1 to 10. `10` records the full
  agent/tool/consensus narrative.
- `--monitor-jsonl PATH`: optional JSONL output path. Defaults to a timestamped
  file under the runtime data dir.
- `--monitor-md PATH`: optional grouped Markdown report path. Defaults beside
  the JSONL file.
- `--format text|json`: print human text or a JSON object with verdict and
  monitor paths.

## Dependencies
- External: `python-dotenv`.
- Internal: `auto_research.graph`, `auto_research.monitor`,
  `auto_research.schemas`.

## Dictionary-level reference
- `build_parser() -> argparse.ArgumentParser` — constructs the command-line
  parser.
- `main(argv=None) -> int` — CLI entry. Loads `.env`, runs one monitored request,
  writes monitor artifacts, and prints the final verdict.
- `auto_research.__main__` — module entry point for `python -m auto_research`.

## Changelog
- 2026-05-26 — Folder seeded during step 0.
- 2026-05-28 — Step 12: implemented the CLI with `--chatter`, JSON/text final
  output, and monitor JSONL/Markdown artifact paths.
