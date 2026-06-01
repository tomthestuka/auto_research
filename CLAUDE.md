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

- 2026-05-27 — Steps 0–9 complete: scaffolding, schemas, config/model-registry (OpenAI/Anthropic/Google), observability (LangSmith + JSONL cost log), SQLite storage, librarian sources (arXiv/NASA ADS/Tavily), retrieval (`text_search()`; `vector_search()` stub), runtime prompts, agent roles (all five), consensus (`DebateToConverge` cap-3 + lead adjudication). IO team owns full literature pipeline (`compose_search_strategy` → `search` → `ingest_results` → local SQLite); `IODebateToConverge` added; `evaluate_search_results()` deferred stub; `LibrarianLike` → `IOAgentLike` + `SearcherLike`. Governance: README sync rule, changelog-append-only rule, `logs/dev/step-N/` convention, real-API test stubs.
- 2026-05-28 — Steps 10–12 complete: LangGraph graph with two capped feedback edges; LangGraph Studio wiring (`serve.py`, `langgraph.json`); monitored library entry `run_research()` / `ResearchRun`, 1-10 chatter system, CLI, notebook. Analyst graph fixes: `_build_task` always injects `follow_up`; `observations.yaml` loaded at graph-build time; tool catalogue passed to `analysis_node`. Tool-authoring deferred to Phase 2 (`merge_flow.propose_and_pr()` stub). Rule added: consult human before fixing unexpected issues.
- 2026-05-28 — Sandbox overhauled: one-shot subprocess replaced by persistent per-worker REPL (`PersistentSandbox`; `sandbox/persistent.py`). Subprocess env sourced from `analyze_ini_sh` init script (HEADAS, CALDB, HEASoft vars). Tool execution moved into `DebateToConverge._run_tools()`; `tool_dispatch_node`/`tool_execution_node` removed. `xspec_utils` split into six stateful tools: `xspec_load_data`, `xspec_set_model`, `xspec_fit`, `xspec_plot`, `xspec_status`, `xspec_group_pha`. Workers gain `sandbox`, `memory_path`, `worker_index`; `analyze()` reads private `memory.md` before each LLM call.
- 2026-06-01 — Dedicated analyst env script: `sandbox/analyst_env.sh` replaces the personal login `ini.sh` as the value of `analyze_ini_sh`. Script is headless-only (HEADAS + CALDB vars; no SSH agent, no mismatched conda activation). `config/orchestration.yaml` repointed. Verified PyXSPEC imports cleanly with the new script.
- 2026-06-01 — `xspec_utils` refactored: AI interface moved from `__init__.py` + `lib/sandbox_tools.py` into a new `xspec_utils.skills` sub-package. `skills/__init__.py` is pure-data `TOOL_MANIFEST` (no xspec import; importable in orchestration env). `skills/session.py` holds xspec-dependent wrappers (`run_fit`, `xspec_status`, `plot_fit`, `plot_iqu`). `plot_fit` wraps `inspect_fit` (one PNG); `plot_iqu` wraps `plot_iqu_decomp` (one PNG per DU). `lib/sandbox_tools.py` deleted. `__init__.py` gutted to re-export from skills via relative import. Fixed wrong function name in fit_tbabs_hss entry. Registry discovers 8 tools.
- 2026-06-01 — Added `tests/test_smoke.py`: gated by `AUTO_RESEARCH_SMOKE_TESTS=1`, log dir `logs/dev/step-11/`. Prompt directs analyst to call MCP `list_dir`, `load_data` (IXPE 02008401 only), and `xspec_status`. Post-run scan asserts zero tool failures, bucketed by `mcp.*` vs `research_toolkit.xspec_utils.*`.
- 2026-06-01 — Synced `sandbox/DOCS.md` to cover `builtin_server.py`, `mcp_client.py`, `analyst_env.sh`: MCP architecture, tool-kind convention, `MCPToolClient` / `call_mcp_tool` / `load_mcp` / `is_mcp` interfaces, built-in server tool list, `analyst_env.sh` purpose. Updated Dependencies to include `mcp` package.
- 2026-06-01 — Smoke test surfaced a `PersistentSandbox` protocol bug: tools that emit heavy stdout (XSPEC/ixpeobssim C-level writes), e.g. `xspec_utils.load_data`, polluted the single-line JSON protocol → parse error → sandbox permanently marked dead → cascade of "sandbox is not alive". Fixed the REPL in `persistent.py`: keep a private dup of the original stdout fd for protocol responses only; redirect fd 1/2 to a capture file during each tool call; attach truncated `console_tail` to the result. Validated via `tests/dry_sandbox_protocol.py` (load_data + xspec_status succeed, sandbox stays alive). Also fixed `xspec_status` div-by-zero when data is loaded but no model is set, and `red_ixpe.pha()` to regenerate Stokes Q/U (`PHA1Q`/`PHA1U`) alongside I.
- 2026-06-01 — Smoke test passed (env health). Line-by-line log analysis then found two `execute_tools()` dispatch bugs causing most "failed tool/MCP calls": (1) `ToolCallRequest.command` (where the model puts shell commands) was never forwarded — only `arguments` was — so every `mcp.builtin.bash_exec` failed first attempt with "command Field required"; (2) routing was computed once from the original request kind, so a self-correction retry returning a different kind was misrouted (toolkit kind sent to MCP → "MCP tool not found") and mislabeled in logs. Fixed both: merge `command` into args; recompute `is_known`/`is_mcp`/`spec` and use `current_req` kind/timeout per attempt. Verified with a deterministic fake-registry/sandbox test (command forwarded; retry reroutes mcp→toolkit correctly). Re-ran smoke test: passed; `bash_exec` failures 21→5, MCP-misroute and sandbox-death classes eliminated. Replaced stale `TestGraphToolRouting` with `TestExecuteToolsDispatch`.
- 2026-06-01 — Remaining tool-call friction (model noise) addressed two ways: (1) `execute_tools()` auto-fills instrument data-path args (`ixpe_path`/`nicer_path`/`nustar_path`) from `config/observations.yaml` (cached `_observation_path_args()`) when the tool's `args_schema` declares them and the model omitted them — model-supplied paths are respected; (2) strengthened `prompts/analyst_worker.md` with tool-argument discipline (use exact schema arg names — no aliases like `cmd` for `command`; supply all required args; instrument paths are auto-filled). Added obs-path injection tests. Final smoke run: passed; tool-call failures down to 1 (a benign command timeout) from 24 in the first run.
- 2026-06-01 — Marked the first §TODO P1 (analyst sandbox env + smoke test) **Done**: smoke test passes, all four tool paths succeed, friction reduced to a single benign timeout. The second P1 (analyst workers own subprocesses exclusively — XSPEC-singleton isolation audit + deterministic isolation test) remains **Todo**.
- 2026-06-01 — Synced overview ledgers and module docs with the current four-role
  prompt/model registry and persistent-sandbox architecture: local retrieval is
  pure Python, graph tool nodes are removed, and tool execution lives inside
  analyst consensus with per-worker sandbox memory.
- 2026-06-01 — Corrected MCP documentation to match current code: the built-in
  MCP server still exposes `bash_exec`, `list_dir`, and `read_file`. The
  read-only MCP restriction remains the separate P2 TODO.

---

## TODO

Statuses: **Todo** = not yet started · **In progress** = actively being worked · **Done** = complete & tested.
Priorities: **P1** = blocking correctness · **P2** = important reliability · **P3** = quality / architecture.

| Priority | Status | What | Checkpoint |
|---|---|---|---|
| P1 | Done | **Fix analyst sandbox env + smoke test** — `analyst_env.sh` created; `analyze_ini_sh` repointed; `tests/test_smoke.py` added (IXPE 02008401 load + MCP list_dir + xspec_status; env-health criteria: key tools succeed ≥once, no infra-signature errors). Along the way fixed: `PersistentSandbox` protocol corruption (fd-redirect), `xspec_status` div-by-zero, `execute_tools` command-forward + per-attempt routing, and obs-path auto-fill from `observations.yaml`; strengthened `prompts/analyst_worker.md` arg discipline. | `test_smoke.py` passes; all four tool paths succeed; tool-call friction reduced to a single benign command timeout. |
| P1 | Todo | **Analyst workers own subprocesses exclusively** — XSPEC kernel processes are singletons; shared sandboxes corrupt state. Audit `DebateToConverge` and `run_research()`: each worker gets exactly one `PersistentSandbox` instance (started once, torn down in `finally`); no sandbox shared between workers; `is_alive` guard prevents double-start. | Deterministic isolation test passes; two workers running in parallel do not interfere. |
| P2 | Done | **Refactor `xspec_utils`: separate human and AI interfaces** — `xspec_utils.skills` sub-package created alongside human `lib/`. `skills/__init__.py` is pure-data `TOOL_MANIFEST` (no xspec import). `skills/session.py` holds xspec wrappers (`run_fit`, `xspec_status`, `plot_fit`, `plot_iqu`). Human `lib/*.py` and `*/*.sh` untouched. Registry discovers 8 tools. | `ToolkitRegistry` discovers 8 tools in orchestration env with no xspec import. |
| P2 | Todo | **Restrict MCP tool usage to file-tree exploration only** — Runtime agents must not use MCP for network access, shell execution, or writes. Restrict registered MCP tools to read-only filesystem traversal (directory listing, path/metadata reading). Enforce via prompt instructions and/or MCP tool registration filter at role-init time. | Agent prompt tests confirm no network/write MCP tools are reachable; file-tree MCP calls succeed. |
| P3 | Todo | **Implement consensus for the librarian IO ingest team** — IO ingest currently runs without a debate loop. Activate `IODebateToConverge` (stub exists in `consensus.py`): multiple IO workers each produce abstract/keyword extractions; IO lead judges convergence; hard cap at 3 rounds. Replace `evaluate_search_results()` stub (`accepted=True`) with a real IO-lead LLM call. | IO consensus test converges (or hits cap and lead adjudicates) on a sample ingest task; `test_full_suite.py` passes. |
