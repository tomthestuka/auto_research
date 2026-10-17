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
Compacted 2026-09-01, again 2026-09-24 and 2026-10-07 (detail in git history).

- 2026-05-26 -> 2026-06-22 — Prompt set built (reviewer, librarian, worker, lead, `coder`, and the
  code-review-only `analyst_colead.md`); §9.6 failure classification + `ask_human`; `coder.md` package
  shape, `obsdb_catalog` writer, `call_timeout_s`; analysis is tool-driven; positive-action style.
- 2026-06-24 -> 2026-06-26 — `reviewer.md` flag→edge mapping (fix-list #1); lead: two-halves
  structure, `obsdb_catalog` data policy, `plan_and_debate` for non-trivial design (fix-list #1–#4,
  §5.5); `test_repro…` convention in lead/coder/co-lead; per-test-outcome determinism in `coder.md`.
- 2026-06-30 -> 2026-07-02 — `analyst_colead.md` reduced to one approve-or-reject bar, all blockers
  in round 1 (live run `…140117`); [A6] review figures only from a separate plotter tool, and
  `coder.md`'s FigureRef capture convention (`save_path`/`save_path_prefix`).
- 2026-07-29 — `status_monitor.md` added, moved out of a string literal in `hitl/commands.py` (§4).
- 2026-09-01 — `ask_human`/`plan_and_debate` named conditionally in lead, reviewer and coder prompts
  (absent headless); both published benchmark batches ran the old unconditional text.
- 2026-09-09 — F8: lead and worker prompts sectioned, every prompt's lines ≤159 chars (guard test);
  F5/F9: turns end with `turn_end`; status is agent-declared `succeeded`/`failed`, default `failed`.
- 2026-09-24 — `librarian_io.md` rewritten for the research loop (plan→search→read→follow→stop, cite
  by handle); 3–6-term queries, prefixed identifiers; papers read as attached PDFs and attributed with
  their place (`{{lib:p3 "value" Table 2}}`), web pages quoted; domain-neutral examples.
- 2026-09-24 — Provenance P2 (0f): worker and lead cite a receipt for every number, `{{handle "…"}}`
  pins a span; ruling: the worker may do plain arithmetic on receipted numbers via `python -c`.
- 2026-09-25 — [G15] `coder.md` gains "Results the runtime can cite — the return contract every
  tool meets" (rules 1–5), matching the gate's `layer_b.returns.*` checks (peer 02).
- 2026-09-25 — [R1/R2] `reviewer.md` rewritten for `assign_to`: scoping section, following up
  results, Next steps, human reply as a nudge; `librarian_io.md`: every pass carries the
  reviewer's questions.
- 2026-09-25 — [E12] `analyst_worker.md`, `analyst_lead.md`, `coder.md`: `ask_human`,
  `plan_and_debate` and `ask_lead` became `message` (to a member, `lead` or `human`) with
  `wait_for_reply`; worker and lead gain a short "your team is one message away" paragraph (texts
  are informal, claims rest on own receipts, a finished member is resumed, a filed report stays
  filed). The conditional-address check in `tests/test_prompts.py` now keys on the `human` address.
- 2026-09-25 — [E12] `analyst_lead.md`: `drive_coder` → `message` to `coder` throughout (the fold);
  `mid_turn` kept.
- 2026-09-25 — [R7] Evidence is produced, not read: `reviewer.md` ("Every number comes from
  evidence": toolkit results, commits, papers; reads and messages are navigation; gaps in words;
  follow_up exempt), `analyst_lead.md` (commit receipts; code facts in words), `analyst_worker.md`
  (no `python -c` arithmetic: derived values from a toolkit tool, requested via `tool_request`),
  `librarian_io.md` (a web page is a lead to its paper).
- 2026-10-07 — **`analyst_colead.md`: production-threatening issues only** (user, after the B5 live
  cell: 12 co-lead rejections over 3 tools). An issue is raised only when a real run would hit it and
  nothing already catches it (gate in the package environment, jail, receipts, lead and reviewer,
  human merge), and names that run; `lead_rebuttal` handling unchanged. **`coder.md`** (env plan T1):
  each test marked by class per the toolkit's root `conftest.py` (unmarked, `slow`, `live` opt-in);
  the gate's run has no network, so a tool also needs tests that pass offline.
- 2026-10-17 — `status_monitor.md`: names its fourth tool, `read_file(offset, limit)` over the run's log.
- 2026-10-17 — Reviewer and librarian prompts (user): the reviewer consults the librarian before each decision (new section "Consult the librarian before deciding"; librarian bullet, follow-up rule and team scope updated); the librarian is a deep research agent (published literature first and the only source backing a value, then documentation, software manuals, code repositories, data-product notes, conference material and community discussions via the `web` source and `web_fetch`; a web page supports a finding stated in words). `analyst_lead.md` librarian description widened to match. No code change: the librarian already had the `web` source.
- 2026-10-17 — Stateless review of the prompt change applied: the reviewer assigns the librarian for researched answers and citable findings (keeping one pass to compare the analysis result with published values) and messages it for a quick check only after its first pass (a text to a seat that has not started is queued); a value from a librarian text becomes citable through `read_paper` or a filed report; the duplicated follow-up bullet removed; analysts look up what a data step needs, the librarian researches practice and pitfalls; `analyst_worker.md` librarian scope widened to match. Open (src/, not changed): `web_fetch` description still says to quote page text; `LiteratureFinding.claim`, `ReviewerReport` and reviewer refusal text still say literature.
- 2026-10-17 — `reviewer.md`, `analyst_worker.md`, `analyst_lead.md`, `librarian_io.md`: write every number in digits (user ruling: a number spelled out is not grounded, so the agents write digits rather than the check reading words).
- 2026-10-17 — The digits line drops its reason clause; "state … in words" became "without a value" (`analyst_lead.md`, `librarian_io.md`, and the grounding refusal in `provenance.bindings`), so a refusal never invites a spelled-out number; `reviewer.md` says a report goes to the human "as a document".
