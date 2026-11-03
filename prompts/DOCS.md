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
- `conventions.md` — what every unit follows, loaded before the role's own file for the
  reviewer, librarian, worker and lead (`agents.prompts.UNIT_ROLES`): the report and `turn_end`
  as the one exit, receipts and handles (the same string for everyone; `view_receipt` opens any),
  evidence versus where to look, the four ways to write a number (a value reference
  `{{h#/key}}`, a printed value cited and copied, a paper attribution, a derived number from a
  tool), derivations in sympy inside toolkit tools carrying their conventions, the team channel,
  and honest outcomes. Each role file then holds its job and the scope it owns.
- `reviewer.md` — the sole I/O: scopes each request (science case, distinguishing observables,
  staged plan with feasibility checks, every named resource confirmed), assigns each step with
  `assign_to` (librarian as often as needed, analysts, or the human), follows up results that
  open a sharper in-scope question, ends every report to the human with a Next steps section, and
  reads the human's reply as a nudge (direction, not evidence). Consults the librarian before each
  decision for the community's approach (`message` for a quick check, `assign_to` for a researched
  answer).
- `librarian_io.md` — the librarian as a deep research agent: plan the research questions
  (prior measurements, methods and pitfalls, standard practice, tool usage), search (short keyword
  queries; the `web` source for documentation, code, data-product notes and discussions), read
  papers and pages in full, follow the citation trail, and file a `LiteratureReport` whose findings
  cite what was read by handle. A web page supports a finding stated in words; a value is
  attributed to the paper that published it. Observation and data work belongs to the analysts.
  Examples are domain-neutral.
- `analyst_worker.md` — task analysis, debate revisions, assumptions, figure
  reference handling, ReAct tool use, the **analysis-is-tool-driven / authors-no-code policy**
  (real steps run through tools; `bash`/`python -c` is read-only inspection only; missing logic
  → `tool_request`), `tool_request` reporting for missing or broken toolkit capabilities, and
  **§9.6 failure handling** (wrong call → retry in the loop; broken tool → `fix_existing`
  request; env/data/human-fault → `message` to `human` / set `status=failed|needs_human`, never
  fabricate). Tool returns are not instantaneous (slow downloads/reductions, human pauses) —
  wait, never assume an output.
- `analyst_lead.md` — convergence judgment, adjudication, synthesis, the same
  tool-driven/inspection-only analysis policy, **tool-request triage** (new-capability
  disposition + `fix_existing` wrongly-called vs buggy), **general-capability brief framing**
  (write `expected_behavior` as a parameterized capability serving the class of question — the
  run's concrete values become argument defaults — not this run's instance),
  authoritative-doc consultation (`web_search`), the team channel (`message` / `wait_for_reply`),
  and **raise-to-human** (`message` to `human` / final
  report `status`) on an honest failure or authoring exhaustion.
- `analyst_colead.md` — the Phase-2 provider-decorrelated **co-lead code reviewer** (loaded
  by the `review` hook for the structured `ToolReviewVerdict`): code-review-only scope (never
  the science finding, never a merge), brief-match + Layer-B check-adequacy judgment, the
  independent no-fabrication second pass, subtle-bug hunting, and read-only worktree/web
  investigation. On approval it also drafts `commit_summary` — the one-line commit subject
  `merge_flow.approve_edit` uses for the working-branch commit (step 6). The role name
  `analyst_colead` mirrors `config/models.yaml`.
- `status_monitor.md` — the light role that answers the HITL `/status` command over the run's own
  event log (loaded by `hitl/commands.py::make_status_answer`). A **reporter, not a participant**:
  it describes what the ledger shows, grounds every number in an event it read, and says when the
  log is silent rather than guessing.
- `coder.md` — the Phase-2 tool author's system prompt (loaded by `merge_flow` and passed
  to the Claude Agent SDK): the real package layout (`lib/` + `skills/` + wrapped-asset subdirs
  + `tests/` + `README.md`), the two-halves split and design-for-extension principle, the
  **build-the-general-capability principle** (parameterize the variable axis with sensible
  defaults — a concrete value the brief names becomes an argument default — so the tool serves
  the class of question, not the one-off), a **slow-tool call-timeout** note (declare
  `call_timeout_s` in the package `tool_config.yaml` so the sandbox doesn't SIGTERM a legitimately
  long tool at ~300s), the
  README↔`TOOL_MANIFEST` audience split, `obsdb_catalog` as the sole catalog writer,
  no-fabrication rules, the web → `message` to `lead` → `message` to `human` escalation ladder, and the committed-tests
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
- `librarian_io.md` — system prompt for the librarian (research loop → `turn_end`).
- `analyst_worker.md` — system prompt for analyst worker roles.
- `analyst_lead.md` — system prompt for the analyst lead role.
- `analyst_colead.md` — system prompt for the Phase-2 provider-decorrelated co-lead reviewer.
- `coder.md` — system prompt for the Phase-2 tool author (Claude Agent SDK).
- `status_monitor.md` — system prompt for the `/status` reporter over the monitor ledger.

## Changelog
Compacted 2026-09-01, again 2026-09-24, 2026-10-07 and 2026-10-19 (every entry); detail in git history.

- 2026-05-26 -> 2026-07-02 — Prompt set (reviewer, librarian, worker, lead, `coder`, `analyst_colead.md`);
  §9.6 failure classification; positive-action style; fix-list #1–#4; `test_repro…` convention;
  co-lead one approve-or-reject bar; [A6] separate plotter tool, `coder.md` FigureRef capture.
- 2026-07-29 -> 2026-09-09 — `status_monitor.md` (§4); `ask_human`/`plan_and_debate` conditional; F8
  sections, lines ≤159 chars; F5/F9 `turn_end`, status `succeeded`/`failed`, default `failed`.
- 2026-09-24 — `librarian_io.md` research loop, paper values attributed; P2: a receipt for every
  number, `{{handle "…"}}`; ruling: worker arithmetic via `python -c` (superseded by R7).
- 2026-09-25 — [G15] `coder.md` return contract (rules 1–5); [R1/R2] `reviewer.md` for `assign_to`;
  [E12] `message` + `wait_for_reply` replace `ask_human`/`plan_and_debate`/`ask_lead`/`drive_coder`;
  [R7] evidence is produced, not read (derived values via `tool_request`).
- 2026-10-07 — `analyst_colead.md`: production-threatening issues only (user, after the B5 live cell);
  `coder.md` (T1): tests marked unmarked/`slow`/`live`, offline tests.
- 2026-10-17 — `status_monitor.md` names `read_file(offset, limit)`; (user) reviewer consults the
  librarian before deciding, librarian a deep research agent (+ stateless review); every number in
  digits (user ruling); "in words" → "without a value".
- 2026-10-18 — [A1] the report form; [B6/B7] `session_compactor.md` (role `session_compactor`); [A7]
  report as one text document (`write_report`, `edit_report`, `# <block>`); [B18] `review_report` erased.
- 2026-10-26 — Evidence-gated debate and receipt references (audit of run 32814459; research on
  evidence-gated debate): `analyst_worker.md` — "Pass a tool's output on by reference" (`$ref`),
  "Weighing what you are told" (every incoming text a claim to check: open its receipt or reproduce
  it; settle your own result first; change a result for a receipt, with old value, new value and
  the receipt; a questioned choice runs both ways). `analyst_lead.md` — "Adjudicating on evidence"
  (open receipts before deciding; checked evidence outweighs agreement; a probe names the deciding
  computation and keeps the dissent; handles, not numbers, in probes and briefs). `reviewer.md` —
  name results by handle in `follow_up`; weigh claims by their receipts.
- 2026-10-26 — `analyst_worker.md`: a call that types a value a tool result holds is answered with the
  reference to write (user ruling, the runtime refuses the copy).
- 2026-10-27 — `conventions.md` added and loaded first for every unit role; the four role prompts
  trimmed to their job and scope (report mechanics, the one exit, evidence and number forms, the
  channel moved to it). `reviewer.md`: "Writing for the human" (a plain-English briefing), numbers
  as value references, derived results checked against the source by a tool. Number forms in
  every role follow value references (`{{h#/key}}`).
- 2026-11-03 — `analyst_worker.md`, `analyst_lead.md`, `reviewer.md`: a `$ref` names the worker's own
  results; a peer's value enters a worker's calls once its own tool run reproduces it.
- 2026-11-03 — `conventions.md`: a report a unit receives shows each referenced value followed by
  its source, `1.23 [h#/k]`; a number with no source was typed by its writer.
