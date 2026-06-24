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
  reference handling, ReAct tool use, the **analysis-is-tool-driven / authors-no-code policy**
  (real steps run through tools; `bash`/`python -c` is read-only inspection only; missing logic
  → `tool_request`), `tool_request` reporting for missing or broken toolkit capabilities, and
  **§9.6 failure handling** (wrong call → retry in the loop; broken tool → `fix_existing`
  request; env/data/human-fault → `ask_human` / set `status=failed|needs_human`, never
  fabricate). Tool returns are not instantaneous (slow downloads/reductions, human pauses) —
  wait, never assume an output.
- `analyst_lead.md` — convergence judgment, adjudication, synthesis, the same
  tool-driven/inspection-only analysis policy, **tool-request triage** (new-capability
  disposition + `fix_existing` wrongly-called vs buggy), **general-capability brief framing**
  (write `expected_behavior` as a parameterized capability serving the class of question — the
  run's concrete values become argument defaults — not this run's instance),
  authoritative-doc consultation (`web_search`), and **raise-to-human** (`ask_human` / final
  report `status`) on an honest failure or authoring exhaustion.
- `analyst_colead.md` — the Phase-2 provider-decorrelated **co-lead code reviewer** (loaded
  by the `review` hook for the structured `ToolReviewVerdict`): code-review-only scope (never
  the science finding, never a merge), brief-match + Layer-B check-adequacy judgment, the
  independent no-fabrication second pass, subtle-bug hunting, and read-only worktree/web
  investigation. On approval it also drafts `commit_summary` — the one-line commit subject
  `merge_flow.approve_edit` uses for the working-branch commit (step 6). The role name
  `analyst_colead` mirrors `config/models.yaml`.
- `coder.md` — the Phase-2 tool author's system prompt (loaded by `merge_flow` and passed
  to the Claude Agent SDK): the real package layout (`lib/` + `skills/` + wrapped-asset subdirs
  + `tests/` + `README.md`), the two-halves split and design-for-extension principle, the
  **build-the-general-capability principle** (parameterize the variable axis with sensible
  defaults — a concrete value the brief names becomes an argument default — so the tool serves
  the class of question, not the one-off), a **slow-tool call-timeout** note (declare
  `call_timeout_s` in the package `tool_config.yaml` so the sandbox doesn't SIGTERM a legitimately
  long tool at ~300s), the
  README↔`TOOL_MANIFEST` audience split, `obsdb_catalog` as the sole catalog writer,
  no-fabrication rules, the web/`ask_lead`/`ask_human` escalation ladder, and the committed-tests
  requirement. The role name `coder` mirrors `config/models.yaml`.

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
- 2026-06-12 -> 2026-06-16 — **Phase 2 steps 4–6 prompts added.** New `analyst_colead.md`, the
  provider-decorrelated co-lead **code-review-only** prompt (brief-match, Layer-B check adequacy,
  the independent no-fabrication second pass, subtle-bug hunting, read-only worktree/web
  investigation; on approval it drafts the one-line `commit_summary` used as the working-branch
  commit subject). The analyst worker/lead prompts gained §9.6 failure classification and the
  `ask_human` raise-to-human path (set `status`, never fabricate on exhaustion). Also compacted the
  changelog and synced the analyst-worker contract to the ReAct/tool-request flow.
- 2026-06-17 -> 2026-06-22 — **Prompts synced to the real `research_toolkit` structure + flow.**
  `coder.md` dropped the "Python toolkit" framing for the actual package shape (`lib/` + `skills/` +
  wrapped-asset subdirs + `tests/` + `README.md`); the trusted catalog writer is `obsdb_catalog`
  (call its upsert helper, read the live signature; never hand-roll `catalog.json`); added the
  README↔`TOOL_MANIFEST` audience split, a design-for-extension note (per-mission strategy modules +
  thin `<domain>_help.py` dispatch), a fast-and-quiet tests note (the gate runs tests twice under a
  time cap), and a **"Build the general capability, not the one-off"** section (parameterize the
  variable axis with sensible defaults). `analyst_worker.md`/`analyst_lead.md` adopted the
  **analysis-is-tool-driven / authors-no-code** policy (every real step runs through tools;
  `bash`/`python -c` is read-only inspection; a missing capability is a `tool_request`), tool returns
  are no longer instantaneous (wait, never assume), failure handling reframed to wrong-call → retry
  vs broken-tool → `fix_existing`, and the lead phrases `ToolSpec.expected_behavior` as a general
  parameterized capability (run targets/ObsIDs are validation data, not tool design). Local data is
  **auto-recognised** via each mission's `catalog.json`/`data_catalog` tool, so no role resolves an
  `obs_db_path` (`reviewer.md` stopped telling the reviewer to look for one). All six runtime prompts
  were swept to the **write-only-positive-actions** style (CLAUDE.md §3), the sole exception being the
  explicit no-fabrication ban; the reviewer's dispatch mechanics moved into the `ReviewVerdict` schema
  (`follow_up` field description) rather than the prompt.
- 2026-06-22 — `coder.md` gained a **slow-tool call-timeout** section: every tool call is
  SIGTERM-killed by the sandbox at ~300s (`exit 124`) independent of any in-code timeout, so a
  legitimately slow tool (large download, long reduction) must declare a higher ceiling in its
  package `tool_config.yaml` (`call_timeout_s`; overrides the `orchestration.yaml`
  `sandbox_call_timeout_s` default, no upper clamp). Pairs with the auto_research-side mechanism
  (`ToolkitRegistry.call_timeout_ceiling` + `SandboxToolProvider`, see `sandbox/DOCS.md`).
- 2026-06-24 — `reviewer.md` "How you dispatch" rewritten to teach the **flag→edge mapping**
  (fix-list #1): the graph always runs an initial literature pass before the first verdict, so
  `needs_more_literature` is the literature **re-loop** switch (set true only for a real literature
  gap; leave false to advance to analysis) and `needs_analysis_rework` is the analysis re-loop
  switch; the analyst dispatch rides in `follow_up` with `needs_more_literature` left false. Removes
  the misleading "set both on a fresh request to start both at once" guidance that mis-looped the
  graph to the librarian 3× on an analyst dispatch.
