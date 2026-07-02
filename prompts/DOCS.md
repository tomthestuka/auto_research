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
- 2026-06-24 — **§5.5 lead-drives-design.** `analyst_lead.md` gained a "Settling the design with the
  human" paragraph that **matches effort to stakes**: *non-trivial* design (a new package, a
  substantial capability, a non-obvious architecture/interface choice) is debated with the human via
  the budget-free `plan_and_debate` channel before the brief is finalised; a *trivial/mechanical*
  change (exposing an existing function, adding an optional parameter or a configurable timeout to an
  existing tool) just proceeds, no debate. `ask_human` stays reserved for a hard block / single expert
  point. `coder.md` reframes the brief as a **settled plan** — execute the lead's architecture,
  `ask_lead` rather than re-open/improvise the design. (Tool + budget mechanics in `agents/DOCS.md`;
  examples stay domain-neutral.)
- 2026-06-25 — **Analyst-lead knowledge + behaviour (fix-list #1–#4).** `analyst_lead.md` gained,
  at design altitude (the coder keeps the implementation detail): the **two-halves toolkit structure**
  on the "Toolkit inspection" paragraph (human-half `lib/` strategy modules + abstracted entry API;
  ai-half `skills/TOOL_MANIFEST`; extend the structure rather than fork; keep error-prone choices in
  `lib/`) so disposition + interface decisions fit what the coder builds to (#2); an **`obsdb_catalog`
  data policy** paragraph after the "general capability" brief guidance (record acquired/produced data
  through the single trusted writer; validation data is **data, not code** — a real or marked `synth…`
  ObsID, never data-fabricating toolkit code) (#1); and two additions to "Settling the design" — close
  a non-trivial debate by **reporting the settled design back** via `plan_and_debate` before dispatching
  the coder (#3), and **weigh the human's steer against grounded evidence**, challenging it with cited
  authoritative documentation when they conflict rather than treating steer as an override (#4).
  Validation is live-only (prompt behaviour). Examples stay domain-neutral; §3 positive-actions kept
  (the no-fabrication ban in the data paragraph is the sanctioned exception).
- 2026-06-26 — **Reproducibility-test convention + per-test-outcome determinism (handoff #2).** All
  three coding-relevant prompts learned the convention: a **science tool** (computes/derives a
  measurement reproducible within tolerance — fit, polarization, flux, derived quantity) ships a
  `test_repro…` test that pins its core output (call-twice / injection-recovery within tolerance);
  data-acquisition and literature-search tools are exempt. `analyst_lead.md` plans it in the brief's
  `proposed_checks` (part of the `plan_and_debate` validation strategy); `coder.md` writes it; and
  `analyst_colead.md` reviews it **against** the lead's `proposed_checks` — consistent with the agreed
  plan by default, but still flagging a genuine gap (`checks_adequate=false`). `coder.md`'s test
  guidance was corrected for the new gate: determinism is now a **per-test-outcome** compare (a
  verdict that flips between the two runs fails; harmless log/stdout volatility no longer matters), so
  the old "make the two runs' output identical" advice is gone; and a slow test declares
  `gate_test_timeout_s` in its package `tool_config.yaml`. Validation is live-only (prompt behaviour);
  examples stay domain-neutral.
- 2026-06-30 — **`analyst_colead.md` reframed to a single relaxed trust-bar (no advisory tier).**
  Live evidence (sonnet-5 loader run `…140117`): the co-lead rejected a correct, gate-passing tool 4×
  with ever-finer "a wrong impl could still pass this" objections until the lead's budget exhausted →
  honest `failed`. Root cause: three rejection-biasing clauses with no counterweight. Reframe: the bar
  is now *"would a competent domain scientist trust this tool's output?"* — **approve-or-reject only,
  no advisory** (an advisory note is a no-op here — the human merges, never refactors — so anything
  worth fixing must be a rejection that drives a rework, and anything below the bar is not raised at
  all); `proposed_checks` are "neither a ceiling nor a floor" (merits, not plan-conformance — the
  committed tests stay authoritative); the "subtly wrong impl" clause is scoped to *plausible*
  implementations; **raise all blockers in round 1, then stop** (no goalpost-moving); a **symmetric
  cost** (over-rejecting a sound tool is also expensive); rebuttal hold-firm scoped to real blockers +
  "don't manufacture a new blocker to avoid conceding". Anti-sycophancy on fabrication/real-bug/
  unproven-core-capability preserved. Validation is live-only (prompt behaviour).
- 2026-07-02 — **Data/plots-separated figure guardrail ([A6]).** Live evidence (run `151419`): an
  authored data tool correctly returns data only, but with no companion plotter tool analysts
  improvised `bash_exec` matplotlib — off-design, uncaptured as `FigureRef`s, and blocked by the
  fs-guard → a figureless report. Prompt fix, keeping the data/plots-separated guardrail: `analyst_worker.md`
  states a review figure comes only from a **plotter tool** (one PNG → `FigureRef`) and to raise a
  `tool_request` for a dedicated plotter when none covers the product rather than drawing it inline;
  `analyst_lead.md` extends the authoring dependency order to `…then plot it` and adds the rule to
  author a **separate** plotter tool (mirroring `plot_fit`/`plot_iqu`) for a plottable product, never
  folding figure-writing into the data tool. `coder.md` gained a **FigureRef capture convention** section
  (grounded in the real `xspec_utils/skills/session.py` + `agents/react.py:_redirect_figure_args` /
  `sandbox/runner.py` capture path): return a top-level `"figures"` list of dicts carrying exactly
  `{path, caption, mime_type, produced_by, created_at, sha256}` (validated into `FigureRef`), and name the
  output-path arg **`save_path`**/**`save_path_prefix`** so the harness re-homes the PNG under the run's
  figures dir and tracks it — an arg by any other name is not redirected. Validation is live-only (prompt
  behaviour); examples stay domain-neutral.
