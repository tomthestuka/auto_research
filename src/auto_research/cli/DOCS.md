# `auto_research.cli` — CLI entry point

## Purpose
Lets a human run a full research request without writing Python:
`python -m auto_research "your question here"`.

## Architecture
A `__main__.py` that parses args, loads `.env`, constructs the graph from
`auto_research.graph`, runs it, and prints the `ReviewVerdict` (plus references
to figures in the runtime data dir).

## Interfaces / Contracts
*To be populated in step 12.* Single command, positional `request` argument; flags
for output format and run id.

## Dependencies
- Internal: `auto_research.graph`.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.
