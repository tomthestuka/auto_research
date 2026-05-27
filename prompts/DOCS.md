# `prompts/` — runtime system prompts

## Purpose
Holds the **only** text that runtime LLM agents see as their system prompt. One
file per agent role.

Per [§3](../CLAUDE.md#3-coding-agent-contract-read-this-every-session) and
[§4](../CLAUDE.md#4-claudemd-and-docsmd-are-build-time-only) of the project
`CLAUDE.md`: **`CLAUDE.md` and any `DOCS.md` must never enter a runtime LLM's
context.** Build-time governance lives in `CLAUDE.md` / `DOCS.md`; runtime
governance lives here.

## Architecture
Plain Markdown files, one per role. The agent layer
(`auto_research.agents`) loads its role's file at construction time and passes the
contents as the system prompt to the provider call. Task-specific information is
injected via the Pydantic schemas in `auto_research.schemas` — not by editing the
prompt at runtime.

## Interfaces / Contracts
Expected files (added in step 7):
- `reviewer.md`
- `librarian_io.md`
- `librarian_searcher.md`
- `analyst_worker.md`
- `analyst_lead.md`

Each file is the complete system prompt for that role: identity, responsibilities,
tools available, output schema expectations, escalation rules. The agent layer
loads it verbatim — do not embed templating logic here.

## Dependencies
- Read by: `auto_research.agents` (one prompt per role wrapper).
- Read at: agent construction.

## Dictionary-level reference
*(none — pure prompt files)*

## Changelog
- 2026-05-26 — Folder seeded as part of step 0 scaffolding. Per-role prompts to be
  authored in step 7.