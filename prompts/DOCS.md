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
- `librarian_io.md` — the librarian's research loop: plan the literature questions, search
  (short keyword queries), read papers and pages in full, follow the citation trail, and file a
  `LiteratureReport` whose findings cite what was read by handle and quote every number exactly
  as the document's text shows it (including PDF-spaced numbers). Scope stays literature-only;
  observation and data work belongs to the analysts. Examples are domain-neutral.
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
- `librarian_io.md` — system prompt for the librarian (research loop → `turn_end`).
- `analyst_worker.md` — system prompt for analyst worker roles.
- `analyst_lead.md` — system prompt for the analyst lead role.
- `analyst_colead.md` — system prompt for the Phase-2 provider-decorrelated co-lead reviewer.
- `coder.md` — system prompt for the Phase-2 tool author (Claude Agent SDK).
- `status_monitor.md` — system prompt for the `/status` reporter over the monitor ledger.

## Changelog
Compacted 2026-09-01: entries through 2026-07-29 folded into themed ranges (detail in git history).

- 2026-05-26 -> 2026-06-22 — **The runtime prompt set, then synced to the real toolkit + flow.**
  Prompts for reviewer, librarian I/O, analyst worker, analyst lead and the Phase 2 `coder`;
  tool-specific argument guidance lives in manifests/tool schemas, prompts carry role scope, ReAct
  discipline, figure-path omission, toolkit-inspection triage, web/lead/human escalation,
  no-fabrication rules and `tool_request` reporting. New `analyst_colead.md` — provider-decorrelated,
  **code-review-only**: brief-match, Layer-B check adequacy, the independent no-fabrication second
  pass, subtle-bug hunting, read-only worktree/web investigation; on approval it drafts the one-line
  `commit_summary` used as the working-branch commit subject. Worker/lead gained §9.6 failure
  classification + the `ask_human` raise-to-human path (set `status`, never fabricate on
  exhaustion). `coder.md`: the real package shape (`lib/` + `skills/` + wrapped-asset subdirs +
  `tests/` + `README.md`) in place of the "Python toolkit" framing; `obsdb_catalog` as the trusted
  catalog writer (call its upsert helper, read the live signature, never hand-roll `catalog.json`);
  the README↔`TOOL_MANIFEST` audience split; design-for-extension (per-mission strategy modules +
  thin `<domain>_help.py` dispatch); fast-and-quiet tests (the gate runs them twice under a time
  cap); **"Build the general capability, not the one-off"** (parameterize the variable axis with
  sensible defaults); and a **slow-tool call-timeout** section — every call is SIGTERM-killed by the
  sandbox at ~300s (`exit 124`) independent of any in-code timeout, so a slow tool declares
  `call_timeout_s` in its package `tool_config.yaml`, overriding the `orchestration.yaml`
  `sandbox_call_timeout_s` default with no upper clamp (pairs with
  `ToolkitRegistry.call_timeout_ceiling` + `SandboxToolProvider`). Worker/lead adopted
  **analysis-is-tool-driven / authors-no-code** (every real step runs through tools; `bash`/`python
  -c` is read-only inspection; a missing capability is a `tool_request`), "tool returns are not
  instantaneous — wait, never assume", wrong-call → retry vs broken-tool → `fix_existing`, and the
  lead phrasing `ToolSpec.expected_behavior` as a general parameterized capability (run
  targets/ObsIDs are validation data, not tool design). Local data is **auto-recognised** via each
  mission's `catalog.json`/`data_catalog`, so no role resolves an `obs_db_path` (`reviewer.md`
  stopped telling the reviewer to look for one). All six prompts were swept to the
  **write-only-positive-actions** style (CLAUDE.md §3), sole exception the explicit no-fabrication
  ban; the reviewer's dispatch mechanics moved into the `ReviewVerdict` schema (`follow_up` field
  description) rather than the prompt.

- 2026-06-24 -> 2026-06-26 — **Dispatch mapping, lead-drives-design, the reproducibility
  convention.** *(fix-list #1)* `reviewer.md` teaches the **flag→edge mapping**: the graph always
  runs an initial literature pass before the first verdict, so `needs_more_literature` is the
  literature **re-loop** switch (true only for a real literature gap, false to advance to analysis)
  and `needs_analysis_rework` the analysis re-loop switch, the analyst dispatch riding in
  `follow_up` with `needs_more_literature` false — removing the misleading "set both on a fresh
  request to start both at once" guidance that mis-looped the graph to the librarian 3×. *(§5.5)*
  `analyst_lead.md` gained "Settling the design with the human", which **matches effort to stakes**:
  *non-trivial* design (a new package, a substantial capability, a non-obvious architecture/
  interface choice) is debated with the human on the budget-free `plan_and_debate` channel before
  the brief is finalised, a *trivial/mechanical* change (exposing an existing function, adding an
  optional parameter or a configurable timeout) just proceeds with no debate, and `ask_human` stays
  reserved for a hard block / single expert point; `coder.md` reframes the brief as a **settled
  plan** — execute the lead's architecture, `ask_lead` rather than re-open/improvise the design.
  *(fix-list #1–#4)* `analyst_lead.md` then gained, at design altitude: the **two-halves toolkit
  structure** (human-half `lib/` strategy modules + abstracted entry API; ai-half
  `skills/TOOL_MANIFEST`; extend rather than fork; keep error-prone choices in `lib/`) (#2); an
  **`obsdb_catalog` data policy** (record acquired/produced data through the single trusted writer;
  validation data is **data, not code** — a real or marked `synth…` ObsID, never data-fabricating
  toolkit code) (#1); closing a non-trivial debate by **reporting the settled design back** via
  `plan_and_debate` before dispatching the coder (#3); and **weighing the human's steer against
  grounded evidence**, challenging it with cited authoritative documentation when they conflict
  rather than treating steer as an override (#4). *(handoff #2)* All three coding-relevant prompts
  learned the **reproducibility-test convention**: a **science tool** (computes/derives a
  measurement reproducible within tolerance — fit, polarization, flux, derived quantity) ships a
  `test_repro…` test pinning its core output (call-twice / injection-recovery within tolerance),
  data-acquisition and literature-search tools exempt; the lead plans it in the brief's
  `proposed_checks`, `coder.md` writes it, `analyst_colead.md` reviews it **against** those
  `proposed_checks` (consistent with the agreed plan by default, still flagging a genuine gap as
  `checks_adequate=false`). `coder.md`'s determinism guidance became a **per-test-outcome** compare
  (a verdict that flips between the two runs fails; harmless log/stdout volatility no longer
  matters), retiring "make the two runs' output identical", and a slow test declares
  `gate_test_timeout_s` in its package `tool_config.yaml`. Validation is live-only (prompt
  behaviour); examples stay domain-neutral.

- 2026-06-30 -> 2026-07-02 — **The co-lead's single relaxed trust bar; the plotter-tool figure
  guardrail [A6].** `analyst_colead.md` reframed to one bar with **no advisory tier** (live: sonnet-5
  loader run `…140117`, 4× rejection of a correct gate-passing tool until the lead's budget
  exhausted → honest `failed`; cause = three rejection-biasing clauses with no counterweight). The
  bar is *"would a competent domain scientist trust this tool's output?"* — **approve-or-reject only** (an advisory
  note is a no-op here: the human merges and never refactors, so anything worth fixing must be a
  rejection driving a rework and anything below the bar is not raised at all); `proposed_checks` are
  "neither a ceiling nor a floor" (merits, not plan-conformance — committed tests stay
  authoritative); the "subtly wrong impl" clause is scoped to *plausible* implementations; **raise
  all blockers in round 1, then stop** (no goalpost-moving); a **symmetric cost** (over-rejecting a
  sound tool is also expensive); rebuttal hold-firm scoped to real blockers + "don't manufacture a
  new blocker to avoid conceding"; anti-sycophancy on fabrication / real-bug /
  unproven-core-capability preserved. **[A6]** (live run `151419`: a data tool correctly returning
  data only, no companion plotter, so analysts improvised `bash_exec` matplotlib — off-design,
  uncaptured as `FigureRef`s, fs-guard blocked → a figureless report) keeps the data/plots-separated
  guardrail in the prompts: `analyst_worker.md` — a review figure comes only
  from a **plotter tool** (one PNG → `FigureRef`), raise a `tool_request` for a dedicated plotter
  when none covers the product rather than drawing it inline; `analyst_lead.md` — authoring
  dependency order extended to `…then plot it`, author a **separate** plotter tool (mirroring
  `plot_fit`/`plot_iqu`) for a plottable product, never folding figure-writing into the data tool;
  `coder.md` — a **FigureRef capture convention** (grounded in `xspec_utils/skills/session.py` +
  `agents/react.py:_redirect_figure_args` / `sandbox/runner.py`): return a top-level `"figures"`
  list of dicts carrying exactly `{path, caption, mime_type, produced_by, created_at, sha256}`
  (validated into `FigureRef`), and name the output-path arg **`save_path`**/**`save_path_prefix`**
  so the harness re-homes the PNG under the run's figures dir and tracks it — an arg by any other
  name is not redirected. Validation is live-only; examples stay domain-neutral.

- 2026-07-29 — **`status_monitor.md` added.** The `/status` reporter's system prompt had been a
  string literal inside `hitl/commands.py` — the one runtime prompt not coming from `prompts/` (§4),
  and therefore the one that never passed `load_prompt`'s governance-reference check. Moved here
  verbatim in substance and rewritten to the house shape (role line + `Responsibilities:`), with the
  reporter framing made explicit: ground every number in an event actually read, and distinguish "it
  did not happen" from "the log does not record it".

- 2026-09-01 — **The human-only tools are now named conditionally, in `analyst_lead.md`,
  `reviewer.md` and `coder.md`.** `ask_human` and `plan_and_debate` are appended to a role's tool
  definitions **only when a `human_handler` is wired**, so on an unattended run they do not exist
  — but three prompts named them unconditionally: `analyst_lead.md` instructed the lead to settle
  a non-trivial design *via* `plan_and_debate` and to close it with a final `plan_and_debate`
  turn, `reviewer.md` told the reviewer to reach the human for a request ambiguity, and
  `coder.md` listed `ask_human` as escalation rung 3. Headless, each was an instruction to use a
  tool the role was never given — and that was true of both already-published benchmark batches,
  so it is a stated difference in any cross-batch comparison. All three now take the conditional,
  positive-action form `analyst_worker.md` already used ("when an `ask_human` tool is offered …;
  when it is absent …"), and each names what to do instead: settle the design on the evidence and
  write it into the brief; adopt a stated reading and carry it as a limitation; search the web
  hard, settle with `ask_lead`, and state the assumption in the code and its test. Pinned by
  `tests/test_prompts.py::test_a_prompt_naming_a_human_only_tool_says_it_may_be_absent`, with a
  companion test asserting the guard actually reaches all four roles rather than silently
  skipping. Tests: `tests/test_prompts.py` 15 → 20 (+3 skips).

- 2026-09-09 — **F8: the long prompts are sectioned and line-bounded; F5/F9 content folded in.**
  Measured before touching anything: `coder.md` and `analyst_lead.md` were the same length
  (22.5k / 20.0k) but one had 13 sections and no line over 159 characters while the other had
  **zero sections** and a longest line of **2,144**. `analyst_worker.md` was the second offender
  (zero sections, 1,319) and is the prompt whose report contract failed in slots 4 and 5. The
  one-capability rule sat as the *third sentence* of the lead's single longest line, sharing that
  paragraph with the chain-planning procedure and the disposition decision, and two live runs
  violated it. Now: lead 13 `##` + 2 `###` (the one-capability rule and the data/plots rule each
  got their own subsection — the point of the exercise), worker 8 `##`, every prompt's longest
  line ≤ 159. **Content changes F5/F9 require:** both prompts describe ending a turn with
  `turn_end` and what a validation rejection means (recoverable, with the reason, tools retained),
  and the report status is the agent-declared `succeeded`/`failed` with `failed` as the default.
  **Verified no clause was lost**, which is the real risk in restructuring accumulated rulings: a
  whitespace-normalised sentence-level diff against HEAD reports zero missing sentences for
  `reviewer.md` and `librarian_io.md`, and for the lead and worker only the `Responsibilities:`
  label (replaced by `## Your job`), the retired `ok`/`needs_human` vocabulary, and sentences I
  deliberately extended.
  `tests/test_prompts.py` gains `test_a_long_runtime_prompt_is_sectioned_and_line_bounded`. Its
  two halves have different warrants and are held to different bars on purpose: the **line bound
  applies to every prompt**, because a wall of text is the defect with an incident behind it and a
  model reads linearly; **sections are required only above 12k**, because `analyst_colead.md` is
  7.7k of well-wrapped short lines that reads fine unsectioned and a lower threshold would be a
  style rule invented to catch it. The existing `Responsibilities:` assertion now also accepts a
  `## ` heading — asserting one legacy literal made a structural *improvement* fail.
- 2026-09-24 — `librarian_io.md` rewritten for the research loop (95): what the report contains,
  the plan→search→read→follow→stop method, citation by handle and quoting by exact copy. The old
  query examples named the live test scenario and were replaced with domain-neutral ones.

- 2026-09-24 — **`analyst_worker.md` and `analyst_lead.md`: every number comes from a receipt
  (provenance P2, session 0f).** One paragraph each, in the report section: tool results arrive
  labelled `[receipt <handle>]`; a finding cites the receipts its numbers come from and copies
  each number exactly; `{{handle "…"}}` pins a span; a derived value comes from a toolkit tool
  (worker: or a `tool_request`; lead: a probe back to the workers), consistent with the worker's
  standing rule that inline `python`/`bash` only looks. Domain-neutral example values.
- 2026-09-24 (second) — **`analyst_worker.md`: plain arithmetic on receipted numbers is allowed as a
  `python -c` one-liner** (human ruling). Under scoped grounding a derived value needs a receipt, and
  every shell call now mints one, so a difference, ratio, significance or unit conversion computed
  that way is recorded; data operations still go through toolkit tools or a `tool_request`.
- 2026-09-24 — `librarian_io.md`: 3–6-term queries (arXiv now requires all terms), field syntax to
  find a known paper, read the original behind a cited result, and page past the first window.
- 2026-09-24 — `librarian_io.md`: papers are read as attached PDFs (no paging); quote exactly as
  printed, a single table cell rather than a reconstructed row, and re-copy the closest passage a
  refusal shows.
- 2026-09-24 — `librarian_io.md`: write identifiers with their prefix (`arXiv:…`, `doi:…`).
- 2026-09-24 — `librarian_io.md`: a paper is cited, not quoted (value exactly as printed, with its
  handle and place); a web page is quoted (option A).
- 2026-09-24 — `librarian_io.md`: a paper value is attributed with its place
  (`{{lib:p3 "value" Table 2}}`); any number outside an attribution is checked.
