# `auto_research.graph` — LangGraph orchestration

## Purpose
Assembles the agents into the process loop described in
[CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop) and
exposes a single async entry point that takes a `ResearchRequest` and returns a
`ReviewVerdict`.

## Architecture
A LangGraph state graph with two real feedback edges, **each capped at 3
iterations**:
1. **Paper-search review** — reviewer → librarian ("literature insufficient/
   obsolete — search again").
2. **Analysis-result review** — reviewer → analyst ("rework / address this
   concern").

The state object carries: the request, the literature set (refs), analyst
reports, the reviewer's notes, and per-edge iteration counters.

## Interfaces / Contracts
*To be populated in step 10.* Planned:
- `build_graph() -> CompiledGraph` — pure constructor; reads no env at import.
- `async run(request: ResearchRequest) -> ReviewVerdict`.

## Dependencies
- External: `langgraph`.
- Internal: every other `auto_research.*` submodule.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.
