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

Prompt loading is centralized in `auto_research.agents.prompts`, which rejects
runtime prompt text that names build-time governance files.

## Interfaces / Contracts
Runtime prompt files:
- `reviewer.md` — input portal for human or authorized upstream research
  requests, work dispatch, result review, feedback decisions, and output portal
  for triage plans or final verdicts.
- `librarian_io.md` — external literature/source search, direct document/PDF
  attachment reading, download and ingest reporting, and bibliographic
  summaries.
- `librarian_searcher.md` — local memory retrieval, staleness checks, and
  escalation recommendations.
- `analyst_worker.md` — task analysis, debate revisions, assumptions, figure
  reference handling, and structured tool/data escalation requests.
- `analyst_lead.md` — convergence judgment, adjudication, synthesis, and
  tool-discipline review without treating requested commands as executed.

Each file is the complete system prompt for that role: identity, responsibilities,
tools available, output schema expectations, escalation rules. The agent layer
loads it verbatim — do not embed templating logic here.

## Dependencies
- Read by: `auto_research.agents` (one prompt per role wrapper).
- Read at: agent construction.

## Dictionary-level reference
- `reviewer.md` — system prompt for the reviewer role.
- `librarian_io.md` — system prompt for the librarian I/O role.
- `librarian_searcher.md` — system prompt for the librarian searcher role.
- `analyst_worker.md` — system prompt for analyst worker roles.
- `analyst_lead.md` — system prompt for the analyst lead role.

## Changelog
- 2026-05-27 — Clarified that the reviewer is both the user-input and
  agent-output portal, and should triage new user requests instead of treating
  absent downstream results as out-of-scope.
- 2026-05-27 — Updated librarian I/O prompt for direct PDF/document attachment
  ingestion and clarified analyst tool-request behavior.
- 2026-05-27 — Clarified that the reviewer prompt can triage requests from the
  human or an authorized upstream planning team, preserving Phase 2 readiness.
- 2026-05-27 — Step 7 completed: authored runtime prompts for all five Phase 1
  roles and documented their loader contract.
- 2026-05-26 — Folder seeded as part of step 0 scaffolding. Per-role prompts to be
  authored in step 7.
