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
  tool), derivations in sympy inside toolkit tools carrying their conventions, writing prose (a
  scientist's paper register; terms coined during the run are described in plain words), the team
  channel, and honest outcomes. Each role file then holds its job and the scope it owns.
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
  **briefs as reports** (each `author_chain` link is a brief `specN`, revised with
  `edit_report`/`write_report` and `report=specN`; revisions reach the running coder as a diff and
  the gate and co-lead at each submission; name and package fixed while the link runs; commit and
  `turn_end` lock), and **raise-to-human** (`message` to `human` / final
  report `status`) on an honest failure or authoring exhaustion.
- `analyst_colead.md` — the Phase-2 provider-decorrelated **co-lead code reviewer** (loaded
  by the `review` hook for the structured `ToolReviewVerdict`): code-review-only scope (never
  the science finding, never a merge), brief-match + Layer-B check-adequacy judgment, the
  independent no-fabrication second pass, subtle-bug hunting, and read-only worktree/web
  investigation, and the three output classes (a science artifact written into the obs-db or
  recorded in its catalog is a reject). On approval it also drafts `commit_summary` — the one-line commit subject
  `merge_flow.approve_edit` uses for the working-branch commit (step 6). The role name
  `analyst_colead` mirrors `config/models.yaml`.
- `status_monitor.md` — the light role that answers the HITL `/status` command over the run's own
  event log (loaded by `hitl/commands.py::make_status_answer`). A **reporter, not a participant**:
  it describes what the ledger shows, grounds every number in an event it read, and says when the
  log is silent rather than guessing.
- `coder.md` — the Phase-2 tool author's system prompt (loaded by `merge_flow` and passed
  to the Claude Agent SDK): the three output classes and how the gate checks each (an artifact
  under an `output` argument with its `shape`, returned under `products`/`figures`; an obs-db item
  recorded through `obsdb_catalog`, tested on a fixture obs-db under `tmp_path/<MISSION>/`; scratch),
  the real package layout (`lib/` + `skills/` + wrapped-asset subdirs
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
- 2026-11-03 — One citing syntax (build item 9): `conventions.md` states that every citation is
  inside `{{ }}` and every number is its own citation (a whole result `{{handle}}`, a printed value
  quoted, the request quoted); worker, lead, reviewer and compactor prompts follow. Earlier the same
  day: argument kinds (a parameter typed with its reason, a path as text or a file reference, data
  from one's own tools), `{{h#/$args/<name>}}` for a chosen value, a brief's value typed with its
  reason; brief parameters purged.
- 2026-11-03 — `conventions.md` § Writing prose (user instruction): every unit writes reports, briefs
  and messages in the manuscript register the user and the PI's style set (short active sentences,
  plain connectives, one point per paragraph, results at their true strength, no defensive asides,
  a table or figure carries its own detail, positive wording, no implementation history, no
  dashes), and replaces any term coined during the run with a plain description of what it names.
- 2026-11-03 — `conventions.md` § Evidence (user instruction): everything a unit sees, its own earlier
  conclusions included, is an assumption until a receipt it opened states that very thing (same
  quantity, same input, same value); a citation beside a claim, a matching number elsewhere, a
  neighbouring input or agreement among members backs nothing. A unit checks a claim (opens the
  receipt or reproduces it) before using it, and states an unchecked one as an assumption.
- 2026-11-04 — `reviewer.md`, `librarian_io.md`, `conventions.md`: a paper "follows as your model reads it (the
  PDF, or its text with a note)" — the model-input sanitiser (A2/A3) may hand a member its parse.
- 2026-11-04 — `reviewer.md` ("Honest failures"): a pass may end without a report, the runtime
  names the unit and the failure, and the pass counts as one of the team's (A4b).
- 2026-11-04 — `analyst_lead.md` ("Each link is a brief report of its own", C3): briefs `specN`,
  revised with `report` set to the id; a later chain appends; revisions reach the coder as a diff,
  the gate and the co-lead read the current version; name and package fixed while the link runs;
  commit and `turn_end` lock, a locked brief is a viewable receipt.
- 2026-11-04 — `reviewer.md` ("Honest failures", A4c): the failed-pass notice names its kind; a
  vendor capacity/rate error or a network failure may clear on a later pass; a vendor refusal or a
  runtime failure is likely to recur and is reported to the human.
- 2026-11-04 — Three output classes taught where models read them (plan item B4): `coder.md` (the
  `output` kind and `shape`, the `products` return, figures in the call's own folder, the obs-db
  class through `obsdb_catalog` with a fixture obs-db, Q1 and the in-place keyword of Q2; the old
  "print its absolute path" rule and the figures-directory text removed), `analyst_worker.md`
  (outputs land in the call's own folder), `analyst_lead.md` (the brief's data policy),
  `analyst_colead.md` (review against the classes), `conventions.md` (a receipt for each returned
  product). Tests: `test_prompts.py` (no prompt teaches the removed rules; the coder and the roles
  around it read the classes).
- 2026-11-04 — Stateless review of B4 (15 findings): fixed — the fixture obs-db recipe (copy what the
  tool changes, link what it reads, seed at the level the real catalog records: `neutral.new` for
  archive files, `neutral.advance(level=…)` for reduced ones), the artifact test's folder per output
  shape (`out`, `out / "fit.log"`, `out / "iqu"`, made by the test), the templates' figure dict
  versus their legacy `path` kind, `path` covering the obs-db a tool records into, "declare every file
  inside its output", the `<mission>_path` auto-fill, a default with its extension for every file
  output, the worker's standard data coming back as a typed path, the Q1 wording ("beyond the
  mission team's standard recipe"; a tool making both writes each to its own path); tests built
  from `OUTPUT_SHAPES`, `PRODUCT_KEYS`, `_MAX_PRODUCTS`.
- 2026-11-04 — Rulings on B4 (main session): Q1 as the checkable rule (an output that depends on a `parameter`-kind argument, or on a setting outside the mission team's defaults, is a science artifact; obs-db items use the team's recommended settings only) in `coder.md`, `analyst_lead.md`, `analyst_colead.md`; synthetic validation data is a science artifact, written under an output argument in a marked `synth_<what>` folder (so results built on it are flagged synthetic) and handed to the tool under test by path, replacing "stored under the mission path".
- 2026-11-04 — Review of the rulings: the synthetic marker is a folder the generating tool writes inside its output, `synth_` and then letters, digits or underscores (`synth_flat_spectrum`, what `provenance.store.is_synthetic_path` flags); `coder.md`'s leftover "marked ObsID" and "(a data input)" reworded; the tests check the prompts' marker example against `is_synthetic_path`.
- 2026-11-04 — `compactor.md`, `session_compactor.md` (C4b): a text from the human reaches the
  summariser as a placeholder (`compaction.HUMAN_PLACEHOLDER`), or raw with its head on a
  provider that compacts on its side; the prompt says to note that the human sent a text there,
  since the runtime restates the words after the summary.
- 2026-11-04 — User ruling S1 (synthetic validation data, option a): a marked synthetic ObsID (`synth<YYMMDD>-<hex>`) is an obs-db item the coding session's tests build in their fixture obs-db (`tmp_path/<MISSION>/`, recorded through `obsdb_catalog.neutral`); `coder.md`, `analyst_lead.md`, `analyst_colead.md` say so, and the artifact route (a `synth_<name>` folder under an output, handed over by path) is withdrawn.
- 2026-11-04 — Review of S1: the no-fabrication ban names the tool's code (`lib/`, `skills/`) in `coder.md` and `analyst_colead.md`, so a test building a synthetic fixture is not caught by it; the fixture recipe for a synthetic ObsID (its files at the layout's paths, `neutral.new`/`.advance`), lowercase hex, naming a fixture by what it holds; the lead adds a synthetic ObsID beside a real one (the `ToolSpec.validation_data` rule: at least one real input).
- 2026-11-04 — E1h: `coder.md` states the gate's rule that every file a call writes is in exactly one class (everything inside an output argument declared; scratch in a temporary folder or the home's caches; any other file rejected unless it is exactly a path the test passed for the tool to write).
- 2026-11-04 — Review of E1h: `coder.md` sends the tool's own scratch to `tempfile.mkdtemp()` (the home caches are where libraries keep theirs), says a relative path lands in the toolkit tree during tests, and states the output rule as "every file the call writes inside its output argument is declared".
- 2026-11-04 — `coder.md`: a file written at a `path` argument fails the gate; a file the tool writes for a caller is an output ("asked for" retired).
- 2026-11-04 — H2: `coder.md` names the `obs_db_root` argument (filled with the obs-db root, where an acquiring tool creates a mission's folder).
- 2026-11-04 — H1: `coder.md`'s fixture obs-db: a catalog seeded through `neutral` makes the folder an obs-db; a mission name `obsdb_catalog` knows adds the uncatalogued scan.
- 2026-11-04 — Item 2: `coder.md` says the gate runs the toolkit's earlier fast tests too (`layer_b.regression`), suggests the package's fast tests when shared `lib/` code changes, and that unmarked tests run at every later gate.
