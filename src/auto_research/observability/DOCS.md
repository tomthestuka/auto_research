# `auto_research.observability` — tracing & cost

## Purpose
Wires every LLM call through LangSmith for tracing and records per-call token
counts and dollar cost. Cost-tiering is a design pillar
([CLAUDE.md §6](../../../CLAUDE.md#6-config-driven-model-registry)), so visibility
into per-role spend must exist from day one.

## Architecture
Decorator/wrapper around the provider call layer. The provider adapters in
`auto_research.models` emit `(response, usage)`; this module turns `usage` into
LangSmith metadata and a cost ledger row (written to the runtime data dir).

## Interfaces / Contracts
*To be populated in step 3.* Planned:
- `traced(role: str)` — async wrapper that opens a LangSmith run, attaches role
  metadata, and records cost on completion.
- A cost-ledger writer (JSONL in the runtime data dir; one line per call).

## Dependencies
- External: `langsmith`.
- Internal: hooks into `auto_research.models`.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.