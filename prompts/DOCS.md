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
  reference handling, ReAct tool use, `tool_request` reporting for missing or
  broken toolkit capabilities, and **§9.6 failure classification** (my-bug → retry;
  env/data/human-fault → `ask_human` / set `status=failed|needs_human`, never fabricate).
- `analyst_lead.md` — convergence judgment, adjudication, synthesis, **tool-request
  triage** (new-capability disposition + `fix_existing` wrongly-called vs buggy),
  authoritative-doc consultation (`web_search`), and **raise-to-human** (`ask_human` / final
  report `status`) on an honest failure or authoring exhaustion.
- `analyst_colead.md` — the Phase-2 provider-decorrelated **co-lead code reviewer** (loaded
  by the `review` hook for the structured `ToolReviewVerdict`): code-review-only scope (never
  the science finding, never a merge), brief-match + Layer-B check-adequacy judgment, the
  independent no-fabrication second pass, subtle-bug hunting, and read-only worktree/web
  investigation. The role name `analyst_colead` mirrors `config/models.yaml`.
- `coder.md` — the Phase-2 tool author's system prompt (loaded by `merge_flow` and passed
  to the Claude Agent SDK): the two-halves layout, no-fabrication rules, the
  web/`ask_lead`/`ask_human` escalation ladder, and the committed-tests requirement. The
  role name `coder` mirrors `config/models.yaml`.

Each file is the complete system prompt for that role: identity, responsibilities,
output expectations, and scope/escalation rules. **Prompts do not enumerate tools
or tool arguments** — the available tools, their argument schemas, and their
descriptions are supplied to the model through tool loading (native function
calling, `ToolkitRegistry.tool_definitions()` + manifest descriptions), not the
prompt. The agent layer loads each prompt verbatim — do not embed templating
logic here.

## Dependencies
- Read by: `auto_research.agents` (one prompt per role wrapper).
- Read at: agent construction.

## Dictionary-level reference
- `reviewer.md` — system prompt for the reviewer role.
- `librarian_io.md` — system prompt for the librarian I/O role.
- `analyst_worker.md` — system prompt for analyst worker roles.
- `analyst_lead.md` — system prompt for the analyst lead role.
- `analyst_colead.md` — system prompt for the Phase-2 provider-decorrelated co-lead reviewer.
- `coder.md` — system prompt for the Phase-2 tool author (Claude Agent SDK).

## Changelog
- 2026-05-26 -> 2026-06-12 — Runtime prompts now cover reviewer, librarian I/O,
  analyst worker, analyst lead, and Phase 2 `coder`. Tool-specific argument guidance lives
  in manifests/tool schemas; prompts carry role scope, ReAct discipline, figure-path
  omission, toolkit-inspection triage, web/lead/human escalation, no-fabrication rules, and
  `tool_request` reporting.
- 2026-06-12 — Compacted changelog and updated the analyst-worker contract wording to match
  the current ReAct/tool-request flow.
- 2026-06-15 — Added `analyst_colead.md`, the Phase-2 step-4 provider-decorrelated co-lead
  code-review prompt (code-review-only; brief-match, Layer-B check adequacy, independent
  no-fabrication pass, subtle-bug hunting, read-only worktree/web investigation).
- 2026-06-15 — Phase 2 step 5: analyst worker/lead prompts gained §9.6 failure classification
  and the `ask_human` raise-to-human path (set `status`, never fabricate on exhaustion).
