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
- `analyst_worker.md` — system prompt for analyst worker roles.
- `analyst_lead.md` — system prompt for the analyst lead role.

## Changelog
- 2026-05-26/27 — Step 7 built: four runtime prompt files (reviewer, librarian_io, analyst_worker, analyst_lead). Scope boundaries enforced: librarians own literature only; analysts drive all observation-archive triage and data work via tool requests; reviewer dispatches accordingly. Librarian searcher role retired; local retrieval is pure-Python.
- 2026-06-03 — `analyst_worker.md` hardened: `load_data` resp-path warnings expected (ignore); `BKG: None` correct for bright sources like Cyg X-1; `python -c "..."` via `bash_exec` permitted for inline computation (no file writes); `run_fit` takes no `indices`; all `fit_*` require `indices` from `load_data`.
- 2026-06-03 — `analyst_worker.md` further hardened from full-suite run analysis: `load_data` must be called alone in its own request batch before dependents (plot_fit, plot_iqu, fit_*); `python -c` multi-statement must use `;` not `\n` escapes; `xspec` has no `__version__` (probe with `print("OK")`); `group_pha` argument is `e_edges` not `bin_edges`; `BKG: None` explanation corrected (negligible contribution, not data-reduction stage).
