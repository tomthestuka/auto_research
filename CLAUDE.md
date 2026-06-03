# CLAUDE.md

**Single source of truth for all coding agents** (Claude Code, Codex, and any other
agent harness) working on the `auto_research` project. All `AGENTS.md`-style files are
symlinks to this file (see [§12](#12-file-linking)).

> This file governs **build-time coding agents** — the assistants that construct and
> maintain the source code. It is **not** for the runtime LLM agents that the system is
> made of. See [§2](#2-the-two-repository-trust-boundary) and [§4](#4-claudemd-and-docsmd-are-build-time-only).

---

## 1. Project overview

`auto_research` is an automated research assistant: a team of LLM agents, orchestrated
with **LangGraph**, that takes a research request, searches the literature, runs
analyses, and reviews the results. The agent team has three roles:

- **Librarians** — find and curate literature (I/O sub-team) and retrieve from local
  memory (searcher sub-team).
- **Analysts** — perform analysis tasks; workers debate to convergence, a lead model
  adjudicates and maintains library discipline.
- **Reviewer** — top-tier models that triage research requests, dispatch work, and
  review results against prior literature. In Phase 1 requests come from the human; in
  Phase 2 they may also come from an authorized lead-scientist planning team.

**Phase 1 (this phase): a working, minimal, modular skeleton** — a full team that runs
end-to-end with real LLM calls, but with the smallest sensible code surface, clean
interfaces, and stubs where we explicitly deferred work. **Phase 2 (deferred):** a
lead-scientist group that mines the whole knowledge base to generate new research ideas;
vector retrieval; autonomous tool-merge. Do not build Phase 2 features now — leave the
documented interface stubs. Build the skeleton in the dependency order given in
[§14](#14-build-order-phase-1).

**What "minimal" means here:** prefer the fewest modules, functions, and lines that
satisfy the interface. Do not add configuration knobs, abstraction layers, retries,
caching, or features that were not requested. When in doubt, write less and document the
extension point in `DOCS.md`.

---

## 2. The two-repository trust boundary

The project is **two separate repositories/packages**. This separation is a safety
mechanism, not a stylistic choice: it is what makes "the system cannot modify its own
code" true *by construction*.

| Repo | Contents | Who commits | Runtime access |
|---|---|---|---|
| **`auto_research`** | Orchestration: LangGraph graph, agent roles, runtime `prompts/`, Pydantic schemas, SQLite layer, librarian source modules (arxiv / NASA ADS / web). | **The human only.** | **Read-only.** Runtime agents never write here. |
| **`research_toolkit`** | The analysts' domain tool library. Houses `xspec_utils` and any tools the human adds. Phase 2: tools the runtime proposes via Claude Agent SDK, after human approval. | **The human only** (Phase 1). Phase 2: runtime may open PRs via Claude Agent SDK; human still approves merge. | **Read-only** for the runtime in Phase 1 — agents discover and call tools but never write here. Phase 2: the gated propose→review→merge flow grants write access on approved PRs only. |

Consequences:

- The system's *own* code is `auto_research`, a repo the runtime cannot touch. In
  Phase 1, the runtime also cannot write to `research_toolkit` — it only discovers and
  invokes existing tools via `ToolkitRegistry`. Phase 2 opens a narrow, gated write
  path through `merge_flow` (Claude Agent SDK + human approval). Self-modification
  remains impossible by construction.
- Recommended placement: a development workspace directory containing both repos side by
  side, with this `CLAUDE.md` at the workspace root.

---

## 3. Coding-agent contract (read this every session)

**Start-of-session checklist — do these before writing any code:**

1. Read this `CLAUDE.md` in full.
2. Read the `DOCS.md` of every module you will touch (and its parents). `DOCS.md` is the
   current ground truth for that module — trust it over assumptions.
3. State, in one or two sentences, what you intend to change and which `DOCS.md` files
   you will update.

**Hard rules:**

- **You perform ZERO git operations.** No `git add`, `git commit`, `git push`, `git
  branch`, `git checkout`, `git stash`, tags — nothing, in either repo. You only create
  and edit files in the working tree. **The human runs all git, for both repos.** When
  you finish a unit of work, summarize what changed and which files, so the human can
  stage and commit. (This is separate from the *runtime* git workflow you implement as a
  feature in [§8](#8-runtime-tool-authoring--sandbox) — that is application code you
  write, not git you execute.)
- **Keep every `DOCS.md` synced.** Any code change must be reflected in the relevant
  `DOCS.md` in the same session, including a changelog entry. Out-of-sync docs are a bug.
- **Keep `README.md` synced.** After every change, update `README.md` in the same
  session whenever the change touches anything the README describes — the build
  status table ([§14](#14-build-order-phase-1)), architecture diagram, model
  defaults, getting-started, environment, or contributor rules. A change is not
  complete until the README reflects it. Apply the same discipline as for
  `DOCS.md`: prefer a precise edit over a sweeping rewrite.
- **Never leak governance files into runtime.** `CLAUDE.md` and any `DOCS.md` must never
  be loaded into a runtime LLM's context — not as a system prompt, tool input, retrieved
  document, or otherwise. Runtime prompts come *only* from `prompts/` (see
  [§4](#4-claudemd-and-docsmd-are-build-time-only)).
- **Never hardcode model identifiers.** All model strings live in config
  ([§6](#6-config-driven-model-registry)).
- **Never commit secrets.** API keys live in `.env` (gitignored); ship a `.env.example`.
- **Minimal code** ([§1](#1-project-overview)).
- **Consult the human before fixing unexpected issues.** When an unexpected failure,
  environment/tooling oddity, ambiguous runtime behavior, or suspected dependency issue
  appears, collect only the minimal facts needed to describe it, then ask the human
  before attempting a fix, workaround, or architectural change.

---

## 4. `CLAUDE.md` and `DOCS.md` are build-time only

No runtime LLM agent may ever see `CLAUDE.md` or any `DOCS.md`. Runtime agents are
configured exclusively from:

- `prompts/` — one system-prompt file per role (e.g. `prompts/reviewer.md`,
  `prompts/librarian_io.md`, `prompts/analyst_worker.md`,
  `prompts/analyst_lead.md`). The runtime loads these; nothing else.

When implementing any agent, load its prompt from `prompts/` and pass only task data and
schemas into the model. If you ever feel tempted to give a runtime agent "context about
the project," that is a signal you are about to violate this rule — put the needed
information into its `prompts/` file or its task schema instead.

---

## 5. `DOCS.md` convention (every module/folder)

Every module folder maintains a `DOCS.md` with these sections, in order:

1. **Purpose** — what this module is for, in plain language.
2. **Architecture** — how it is designed and why; how it fits the larger system.
3. **Interfaces / Contracts** — the public interface: classes, functions, their
   signatures, the Pydantic schemas in/out, and invariants callers can rely on. This is
   the contract that keeps module boundaries honest.
4. **Dependencies** — what this module imports/relies on (internal and external), and
   what depends on it.
5. **Dictionary-level reference** — every public symbol (function, class, method,
   constant) with its meaning, parameters, and return.
6. **Changelog** — at the **end**, dated entries for every meaningful change. New entries
   are always **appended at the very bottom** of the Changelog section — never inserted
   before existing entries. This applies to both `DOCS.md` and `CLAUDE.md` changelogs.

Rules: `DOCS.md` must always reflect the **current** state of the code. Update it in the
same session as the code change. Coding agents read the relevant `DOCS.md` at the start
of every session (see [§3](#3-coding-agent-contract-read-this-every-session)).

---

## 6. Config-driven model registry

LLM teams must be upgradable without code changes. All model selection lives in config
(e.g. `config/models.yaml`), mapping **role → one or more model members**. Each member
has a provider, model id, vision flag, and provider-specific parameters. No model
identifier appears in source code.

Providers: **OpenAI, Anthropic, Google.** Reviewer-tier and analyst models **must be
vision-capable** (figures, [§7](#7-schemas--protocols)).

Default tier mapping (a *config default*, edit freely — not code):

| Role | Tier | Example defaults |
|---|---|---|
| Reviewer group | top | `claude-opus-4-7`, `gpt-5.5` (reasoning effort high), `gemini-3.5-flash` (thinking high) |
| Analyst lead | top | `claude-opus-4-7` |
| Analyst worker group | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning effort medium) |
| Librarian I/O group | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning effort medium), `gemini-3.5-flash` (thinking high) |
| Librarian searcher group | light | `gpt-5.4-nano` |
| Embeddings (Phase 2) | — | `text-embedding-3-small` *(deferred — see [§10](#10-phase-2-readiness))* |

Implement a thin provider abstraction so a role can be backed by any provider via config.
Keep it minimal; do not build a general model-routing framework.

---

## 7. Schemas & protocols

All inter-agent messages are **Pydantic v2** models in a shared `schemas` module. They
are the protocol and double as documentation. Bad data must fail loudly and early.

Representative messages (refine signatures in the module's `DOCS.md`): `ResearchRequest`,
`SearchRequest` / `SearchResult`, `AnalysisTask` / `AnalystReport`, `ReviewVerdict`. Keep
the set minimal.

**Figures — by reference, never by value.** Figures are critical to review, but live
image objects must never live inside messages (they break JSON serialization and bloat
logs, LangSmith traces, and SQLite). Pattern:

- A figure-producing tool writes the image to the run's **artifacts directory** (in the
  runtime data dir, [§9](#9-tech-stack--conventions)) and returns a lightweight
  `FigureRef`: `path`, `caption`, `mime_type`, `produced_by`, `created_at`, `sha256`, and
  optional small `thumbnail_b64`.
- Messages carry `list[FigureRef]` — clean and serializable.
- Bytes are loaded and passed as image content blocks **only at the multimodal-call
  boundary** — i.e. exactly when the reviewer, or analysts during debate, need to *see*
  the figures.

---

## 8. Runtime tool-authoring & sandbox

**Phase 1 execution sandbox:** every tool call from analyst workers runs in an
**isolated subprocess** with a timeout, a confined working directory, and `PYTHONPATH`
limited to `research_toolkit`. No external harness is used.

**Tool discovery:** research_toolkit packages expose a `TOOL_MANIFEST` dict at their
top level. The `ToolkitRegistry` (`auto_research/sandbox/registry.py`) discovers tools
by importing these manifests. Analyst workers receive the tool list and descriptions at
dispatch time.

**Error-feedback loop:** when a tool call fails, the error (stderr + exit code) is fed
back to the analyst worker, which self-corrects and re-issues the `ToolCallRequest`.
Capped at `tool_retry_cap` (default 3, in `orchestration.yaml`).

**Human tool-writing (Phase 1):** the human writes new tools in `research_toolkit` and
exposes them via `TOOL_MANIFEST`. Agents discover and invoke them autonomously.

**Tool-authoring (Phase 2):** `merge_flow.propose_and_pr()` is a stub that raises
`NotImplementedError`. Phase 2 implementation: analyst worker emits a tool spec →
Claude Agent SDK writes and tests the code → lead reviews → human approves merge on
GitHub. Deeper hardening (containers, seccomp, egress control) is also Phase 2.

---

## 9. Tech stack & conventions

- **Environment:** conda, latest stable Python (pin the exact version in
  `environment.yml`; conda-forge channel).
- **Orchestration:** LangGraph. The reviewer→dispatch→review loop is a graph with
  conditional/cyclic edges and **hard iteration caps**.
- **Async-first:** write interfaces as `async`. I/O-bound work (LLM calls, source
  queries, DB) should be concurrency-friendly even if Phase 1 execution is largely
  sequential.
- **Schemas:** Pydantic v2 ([§7](#7-schemas--protocols)).
- **Storage:** SQLite via Python. Phase 1 schema (papers metadata/abstract/keywords;
  memory items; query log) is the DB module's `DOCS.md` to define — **reserve space for an
  embedding column/table** for Phase 2.
- **Observability:** wire **LangSmith tracing** and per-call cost logging from the start
  (cost-tiering is central to the design).
- **Secrets:** `.env` (gitignored) + `.env.example`. NASA ADS token and all API keys here.
- **Runtime data dir:** a configurable directory **outside both repos**, not committed,
  holding the SQLite file, the figures/artifacts store, and run logs.
- **Testing:** minimal `pytest` on deterministic parts only (DB ops, query parsing,
  schema validation). Do not assert subjective LLM behavior.
- **Live API tests:** every module that wraps an external API must leave an opt-in
  real-API test stub alongside deterministic tests. Coding agents should default to
  running the relevant live API test when working on that integration, unless the human
  says not to, credentials are unavailable, or network/API spend is inappropriate for the
  requested change. Live tests must be explicitly gated by an environment variable and
  write their logs under `logs/dev/step-N/`.
- **Development test logs:** any test command that intentionally emits logs, live model
  outputs, usage, or cost data writes those artifacts under `logs/dev/step-N/`, sorted by
  build-order step. Prefer JSONL for machine-readable records plus Markdown summaries
  for human inspection. These logs are local development artifacts, not runtime memory.

---

## 10. Agent architecture & process loop

**Librarians.**
- *I/O sub-team* (medium models): build search queries; read/analyze downloaded papers;
  produce abstractions, keywords, and (Phase 2) vectorized memory items; own the
  read/write DB modules. **External source tools — each its own module behind a shared
  `QuerySource` interface** so new sources drop in cleanly: `arxiv` (paper search, via the
  `arxiv` Python lib), `nasa_ads` (paper search, token in `.env`), `web` (generic field
  updates backing paper searches, via **Tavily**). The analysts' "read authoritative
  documentation" reuses the same `web` module.
- *Searcher sub-team* (light models): query local memory; check last-update dates; if
  relevant entries are obsolete, escalate to the I/O team for fresh online queries.
  Retrieval is behind a `RetrievalBackend` interface: implement **`text_search()` now**;
  **`vector_search()` is a stub that raises `NotImplementedError`** (Phase 2).

**Analysts.**
- Workers (medium/small) own tools (`xspec_utils`, web search for docs, and — in Phase 2
  — new tools they author via Claude Agent SDK). They run **debate-to-converge**
  ([§11](#11-consensus-debate-to-converge)). In Phase 1, workers discover available tools
  via `ToolkitRegistry`, call them via the sandbox, and self-correct from execution errors.
- Lead (top tier) judges convergence, adjudicates, maintains library discipline.
  In Phase 2, drives the `research_toolkit` review/merge flow
  ([§8](#8-runtime-tool-authoring--sandbox)).

**Reviewer** (top tier): triages the research request, dispatches analysis tasks
(→ analysts) and literature tasks (→ librarians), digests results, and cross-checks
against prior publications. Phase 1 requests enter from the human; Phase 2 may also
route authorized lead-scientist planning requests through the same reviewer gate.

**Process loop:** request → reviewer dispatches analysis + literature tasks →
reviewer digests analysis results and compares with prior publications → human review.
Implement **two real feedback edges**, each with a hard iteration cap (default 3):

1. **Paper-search review:** reviewer → librarian ("literature insufficient/obsolete —
   search again").
2. **Analysis-result review:** reviewer → analyst ("analysis needs rework / address this
   concern").

(The human/lead-scientist review stage is the entry/exit; the lead-scientist *group* that
generates ideas is Phase 2.)

---

## 11. Consensus: debate-to-converge

Analyst workers reach agreement via bounded debate, behind a swappable
`ConsensusStrategy` interface:

1. Each worker produces an analysis.
2. Each round, workers see peers' analyses (and figures) and critique/revise.
3. After each round, the **lead (Opus) judges whether they have converged** and
   summarizes any remaining disagreement.
4. Stop on convergence **or** at a **hard cap of 3 rounds**, at which point the **lead
   adjudicates/synthesizes** the final report and flags residual uncertainty.

---

## 12. File linking

- This `CLAUDE.md` is the single source of truth, placed at the development workspace
  root.
- Other agent-instruction files are **symlinks** to it, e.g.:
  ```
  ln -s CLAUDE.md AGENTS.md
  ```
- If a tool requires the file inside a specific repo, symlink it there too rather than
  copying. Never fork the content — one file, many links.

---

## 13. Phase 2 readiness (do NOT build now — leave stubs)

- **Vector retrieval:** keep `vector_search()` stubbed; reserve the embedding
  column/table in the DB schema; intended backend **sqlite-vec** (stays in the existing
  SQLite file, no new infra); embedding model is config-driven. Estimated effort to
  activate later: low-to-moderate (~1–3 days) given these hooks.
- **Tool authoring via Claude Agent SDK:** `sandbox/merge_flow.propose_and_pr()` raises
  `NotImplementedError`. Phase 2: replace with Claude Agent SDK invocation — analyst
  worker emits a tool spec; the SDK agent writes, tests, and proposes a PR on
  `research_toolkit`; lead reviews; human approves merge.
- **Lead-scientist group:** the idea-generation team that reads the whole knowledge base.
- **Sandbox hardening:** containers / seccomp / egress control beyond the Phase 1
  subprocess sandbox.
- **IO search iteration:** `LibrarianIOAgent.evaluate_search_results()` returns
  `accepted=True` in Phase 1. When activated: the IO lead evaluates the quality of
  online search results, re-calls `compose_search_strategy()` with refinement context,
  and re-searches up to `orchestration.yaml::io_search_max_iterations` additional times.
  The loop lives inside `ingest_results()`, guarded by the stub verdict. Activate by
  replacing the stub with a real LLM call and raising `io_search_max_iterations` above 1.
- **`bash_exec` figure capture (option A):** Inline `python -c` plots saved by a
  worker land in the worker's `cwd` (auto-injected `memory_path.parent`) but are
  invisible to the system — `mcp.builtin.bash_exec` returns only
  `{exit_code, stdout, stderr}` and never scans for new image files. Phase 2:
  snapshot the worker `cwd` before each `bash_exec`, diff for new `*.png`/image
  files after, and synthesize `FigureRef`s into the `ToolResult.figures` list (the
  same shape `plot_fit`/`plot_iqu` already return). Complementary to the P1
  figure-propagation TODO: that fix carries `ToolResult.figures` into the report;
  this makes `bash_exec` actually populate `ToolResult.figures`. Left as-is for now
  per decision.

---

## 14. Build order (Phase 1)

Build bottom-up: foundation → schemas → models → data → sources → agents → graph →
tooling → entry. This table is both the dependency order and the build ledger: it records
what each step builds, what must work afterward, and whether the step is done. Each step
must leave a **working, documented increment** — update the relevant `DOCS.md` (and the
README status table) before moving on. Stop and verify the checkpoint at each step.

Statuses: **Todo** = in Phase 1 scope, not yet built · **Done** = built & documented.

| # | Status | What is being built | Checkpoint (what works after) |
|---|---|---|---|
| 0 | Done | **Scaffolding & environment** — both repo skeletons, `environment.yml` (conda), `.env.example`, `.gitignore`, root `CLAUDE.md` + `AGENTS.md` symlink, `config/` dir, seed `DOCS.md` in each module folder. | Conda env builds; package imports cleanly. |
| 1 | Done | **Schemas** — Pydantic v2 models incl. `FigureRef`. | Schema validation tests pass. |
| 2 | Done | **Config + model registry** — `config/models.yaml`, provider abstraction (OpenAI / Anthropic / Google), role→model resolution. | A real round-trip call succeeds for each provider via config. |
| 3 | Done | **Observability** — LangSmith tracing + per-call cost logging wired into the provider layer. | A traced call appears in LangSmith with recorded cost. |
| 4 | Done | **Storage** — SQLite layer (papers / memory / query_log), embedding space reserved; read/write modules. | Write then read back a paper record (pytest). |
| 5 | Done | **Librarian sources** — `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. | Each source returns results for a sample query. |
| 6 | Done | **Retrieval** — `RetrievalBackend` with `text_search()` over SQLite; `vector_search()` stub raising `NotImplementedError`. | Store, then text-retrieve, a record. |
| 7 | Done | **Runtime prompts** — one `prompts/<role>.md` per role. | Each prompt loads at runtime; no governance file referenced. |
| 8 | Done | **Agent roles** — thin wrappers: librarian I/O, analyst worker, analyst lead, reviewer (each loads its prompt, uses schemas + config models); local retrieval is pure Python. | Role plumbing is verified: prompt loads, model resolves via config, structured Pydantic input goes in, structured output comes back, and trace/cost logging is exercised. This does **not** mean every role can do its full production job yet. |
| 9 | Done | **Consensus** — `ConsensusStrategy` = debate-to-converge (cap 3) + lead adjudication. | Workers converge (or hit the cap and the lead adjudicates) on a bounded task. |
| 10 | Done | **Orchestration** — assemble the LangGraph graph and the two capped feedback edges (paper-search review, analysis review). | Full loop runs end-to-end on a sample request. |
| 11 | Done | **Sandbox & tool execution** — `ToolkitRegistry` (manifest-based discovery), persistent per-worker `PersistentSandbox`, built-in MCP tools, and tool execution inside analyst consensus with error-feedback retries capped at `tool_retry_cap`. `merge_flow.py` stub raises `NotImplementedError` (Phase 2 → Claude Agent SDK). **LangGraph Studio wiring** — `serve.py` + `langgraph.json`; `langgraph dev` exposes live `ResearchState` per node via Studio. | Known tools execute in sandbox; error-feedback loop self-corrects bad args; unknown tool triggers stub cleanly; `xspec_utils` runs on real Cyg X-1 data. `langgraph dev` starts without error; Studio shows live graph state at each node. |
| 12 | Done | **Entry points & tests** — importable monitored library entry (`run_research()`), CLI (`python -m auto_research "…" --chatter 10`), notebook for inspection, minimal pytest on deterministic parts. | CLI runs a request end-to-end; notebook inspects a run with full-chatty monitor JSONL/Markdown logs grouped by team. |

After step 12 the Phase 1 skeleton is complete: a full team running real LLM calls
end-to-end, with vector search, autonomous merge, and the lead-scientist group left as
documented stubs.

---

## Changelog

- 2026-05-27/28 — Phase 1 steps 0–12 complete. Scaffolding, schemas, config/model-registry (OpenAI/Anthropic/Google), observability (LangSmith + cost log), SQLite storage, librarian sources (arXiv/NASA ADS/Tavily), retrieval (`text_search()`; `vector_search()` stub), runtime prompts, agent roles (all four), consensus (`DebateToConverge` cap-3 + lead adjudication), `IODebateToConverge`, `evaluate_search_results()` stub. LangGraph graph with two capped feedback edges, Studio wiring, monitored `run_research()` / `ResearchRun`, chatter system, CLI, notebook. Persistent per-worker REPL sandbox (`PersistentSandbox`); tool execution inside `DebateToConverge._run_tools()`; `merge_flow` Phase 2 stub.
- 2026-06-01 — Post-skeleton hardening. `xspec_utils` AI interface extracted into `skills/` sub-package (`TOOL_MANIFEST` + `session.py`). Sandbox: fd-redirect console capture prevents protocol corruption; XSPEC non-interactive policy at boot (`Fit.query='no'`, `Xset.allowPrompting=False`, `Xset.chatter=5`); subprocess auto-restart on timeout/death. Tool-output normalization (`_normalize_tool_raw`) and per-write IO truncation (stdout 8 KB, stderr 2 KB). MCP builtin server spawned in astropy env (`_builtin_server_config`). `execute_tools()` dispatch bugs fixed (command-forward, retry-reroute, obs-path auto-fill). Smoke, isolation, and policy tests added.
- 2026-06-03 — P1 fixes and env hardening. `fit_tbabs_hss` removed from TOOL_MANIFEST (broken model expression); `disabled_tools` filter added to `orchestration.yaml` + `ToolkitRegistry`. MCP server env built from `_source_ini_env` only — no `os.environ` fallback; `ValueError` if `analyze_ini_sh` missing; PYTHONPATH covers `toolkit_root` and parent; HEASoft PATH preserved. `execute_tools` auto-injects `cwd=memory_path.parent` into `bash_exec` calls. Analyst worker prompt: resp-path warnings expected; `BKG: None` correct for bright sources; `python -c` permitted (no disk writes); fit-arg rules. `test_full_suite` reviewer context added. 86 deterministic + 3 live policy tests pass.
- 2026-06-03 — Analyst prompt hardening from full-suite log analysis. `load_data` now required in its own batch (not batched with dependents using placeholders); `python -c` multi-statement guidance (use `;` not `\n`); `xspec.__version__` non-existence documented (probe with `print("OK")`); `group_pha` `e_edges` argument name reinforced in prompt and TOOL_MANIFEST schema description; `BKG: None` explanation corrected (negligible contribution for bright sources, not data-reduction stage).
- 2026-06-03 — Documented figure-visibility gap from full-suite log analysis: analyst `ToolResult` figures are flattened to path-strings and never reach `AnalystReport.figures`, so the reviewer sees `image_count: 0`. Added P1 TODO (propagate tool figures to reviewer), P3 TODO (reviewer report composition — machine JSONL + human LaTeX handles), and a §13 Phase 2 note for deferred `bash_exec` figure capture (option A).
- 2026-06-03 — P3 reviewer report composition. New `report/` module (`references.py`, `figures.py`, `writers.py`): `build_references()`, `stage_figures()` (symlinks), `write_report_jsonl/latex()`, `validate_latex_refs()`, `compile_latex()` (pdflatex + error parsing; degrades gracefully when compiler absent). New schemas `ReportSection`, `Reference`, `ResearchReport`, `LatexReport`. `ReviewerAgent` gains `compose_machine_report()` and `compose_latex_report()`. New `compose_report` in-graph node (after `finalize_run`, conditional on `verdict.accepted and report_formats`): error-feedback loop (compile + ref check, capped at `report_retry_cap`). Harness param `report_formats` (`'jsonl'`/`'tex'`/both) in `run_research()`, `run_request()`, and `--report-format` CLI flag. `report_dir()` in `workspace.py`. `ResearchRun` and `ResearchState` extended with four report artifact fields. Config keys `report_formats` / `report_retry_cap` in `orchestration.yaml`. 15 new deterministic tests. 144 tests pass.
- 2026-06-03 — Fixed figure propagation (P1). `_run_tools()` in `consensus/strategy.py` now returns per-worker `FigureRef` lists; `_merge_figures()` merges them onto worker `AnalystReport.figures` after each tool phase (deduped by path). `run()` copies all worker figures onto the final adjudicated report so `ReviewerAgent.review(images=report.figures)` receives real `FigureRef`s. Two new deterministic tests cover propagation and deduplication. Removed Done rows from TODO table.

---

## TODO

Statuses: **Todo** = not yet started · **In progress** = actively being worked · **Done** = complete & tested.
Priorities: **P1** = blocking correctness · **P2** = important reliability · **P3** = quality / architecture.

| Priority | Status | What | Checkpoint |
|---|---|---|---|
| P1 | Done | **Propagate analyst tool figures to the reviewer** — `ToolResult.figures` (from `plot_fit`/`plot_iqu`) are flattened to path-strings in `consensus/strategy.py` (`_peer_tool_summary`, ~line 244) and `_append_tool_results_to_memory`; they never reach `AnalystReport.figures`, so the reviewer's `review()` gets `images=[]` (full-suite run: final `analysis_report.figures=[]`, reviewer `image_count: 0`). Collect each round's `ToolResult.figures` and attach them to the matching worker `AnalystReport.figures`, carry them through `lead.adjudicate()` into the final report, so `ReviewerAgent.review(images=report.figures)` (already wired at `roles.py:174`) receives real `FigureRef`s. | Full-suite run: final `analysis_report.figures` is non-empty; reviewer `review.input` shows `image_count > 0`; reviewer rationale references the actual plots. |
| P2 | Todo | **Restrict MCP tool usage to file-tree exploration only** — Runtime agents must not use MCP for network access, shell execution, or writes. Restrict registered MCP tools to read-only filesystem traversal (directory listing, path/metadata reading). Enforce via prompt instructions and/or MCP tool registration filter at role-init time. | Agent prompt tests confirm no network/write MCP tools are reachable; file-tree MCP calls succeed. |
| P3 | Done | **Reviewer composes a report after acceptance** — After the reviewer returns `accepted=true`, generate a research report via two handles: (1) **machine handle** → structured JSONL for an upstream planning team, including `FigureRef`/file references (paths, captions, sha256) — not inline bytes; (2) **human handle** → LaTeX document embedding the correct figures and bibliographic references. Depends on the P1 figure-propagation fix (figures must reach the reviewer first). | On an accepted run, both artifacts are produced: a valid JSONL report with resolvable figure/file refs, and a compilable LaTeX report whose `\includegraphics`/citations resolve to real run artifacts. |
| P3 | Todo | **Implement consensus for the librarian IO ingest team** — IO ingest currently runs without a debate loop. Activate `IODebateToConverge` (stub exists in `consensus.py`): multiple IO workers each produce abstract/keyword extractions; IO lead judges convergence; hard cap at 3 rounds. Replace `evaluate_search_results()` stub (`accepted=True`) with a real IO-lead LLM call. | IO consensus test converges (or hits cap and lead adjudicates) on a sample ingest task; `test_full_suite.py` passes. |
