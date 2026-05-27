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
- **Reviewer** — top-tier models that triage human requests, dispatch work, and review
  results against prior literature.

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
| **`research_toolkit`** | The analysts' domain tool library. Houses the human-authored `xspec_utils` and any tools the system creates at runtime. | The human (build-time) **and** the runtime system (via the gated flow in [§8](#8-runtime-tool-authoring--sandbox)). | **Writable**, but only through the gated propose→review→merge flow. |

Consequences:

- The system's *own* code is `auto_research`, a repo the runtime cannot touch. The
  runtime can only **extend `research_toolkit`**. Self-modification is therefore
  impossible, not merely forbidden.
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

---

## 4. `CLAUDE.md` and `DOCS.md` are build-time only

No runtime LLM agent may ever see `CLAUDE.md` or any `DOCS.md`. Runtime agents are
configured exclusively from:

- `prompts/` — one system-prompt file per role (e.g. `prompts/reviewer.md`,
  `prompts/librarian_io.md`, `prompts/librarian_searcher.md`, `prompts/analyst_worker.md`,
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
6. **Changelog** — at the **end**, dated entries for every meaningful change.

Rules: `DOCS.md` must always reflect the **current** state of the code. Update it in the
same session as the code change. Coding agents read the relevant `DOCS.md` at the start
of every session (see [§3](#3-coding-agent-contract-read-this-every-session)).

---

## 6. Config-driven model registry

LLM teams must be upgradable without code changes. All model selection lives in config
(e.g. `config/models.yaml`), mapping **role → provider + model id + parameters**. No
model identifier appears in source code.

Providers: **OpenAI, Anthropic, Google.** Reviewer-tier and analyst models **must be
vision-capable** (figures, [§7](#7-schemas--protocols)).

Default tier mapping (a *config default*, edit freely — not code):

| Role | Tier | Example defaults |
|---|---|---|
| Reviewer | top | `claude-opus-4-7`, `gpt-5.5` (reasoning effort high/xhigh), `gemini-3.1-pro` |
| Analyst lead | top | same as reviewer tier |
| Analyst worker | medium | `claude-sonnet-4-6`, `gpt-5.5` (effort medium), `gemini-3-flash` |
| Librarian I/O | medium | `claude-sonnet-4-6`, `gpt-5.5` (effort medium), `gemini-3-flash` |
| Librarian searcher | light | `claude-haiku-4-5`, `gpt-5.4-nano`, `gemini-3.1-flash-lite` |
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

Analysts will need to build new modules in `research_toolkit`. Because LLM-authored code
that other agents import and run is arbitrary code execution, this flow is **gated**.

**Workflow (CLI-style, like Claude Code):**

1. An analyst proposes a new/changed tool → written to a **branch** in `research_toolkit`.
2. The analyst-lead (Opus tier) reviews it for library discipline and hygiene.
3. **Gate before merge to main** (Phase 1): **a human approves the merge.** Only after
   merge does the tool become importable.
4. Autonomous merge (lead approves + mandatory sandboxed test pass, no human) is a
   **deferred config toggle**, not built now ([§10](#10-phase-2-readiness)).

**Execution sandbox (Phase 1, applies to every tool — new *or* existing):** run tool code
in an **isolated subprocess** with a timeout, a confined working directory, and no network
unless explicitly whitelisted. Deeper hardening (containers, seccomp, egress control) is
documented as a deferred task; do not build it now.

The git operations *inside this workflow* are part of the application you write; they act
only on `research_toolkit`. They are **not** the coding agent running git
([§3](#3-coding-agent-contract-read-this-every-session)).

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
  schema validation). Do not test LLM behavior.

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
- Workers (medium/small) own tools (`xspec_utils`, web search for docs, and — via the
  gated flow — new tools they author). They run **debate-to-converge**
  ([§11](#11-consensus-debate-to-converge)).
- Lead (top tier) judges convergence, adjudicates, maintains library discipline, and
  drives the `research_toolkit` review/merge flow ([§8](#8-runtime-tool-authoring--sandbox)).

**Reviewer** (top tier): triages the human request, dispatches analysis tasks (→ analysts)
and literature tasks (→ librarians), digests results, and cross-checks against prior
publications.

**Process loop:** human request → reviewer dispatches analysis + literature tasks →
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
- **Autonomous tool-merge:** the config toggle described in [§8](#8-runtime-tool-authoring--sandbox).
- **Lead-scientist group:** the idea-generation team that reads the whole knowledge base.
- **Sandbox hardening:** containers / seccomp / egress control beyond the Phase 1
  subprocess sandbox.

---

## 14. Build order (Phase 1)

Build bottom-up: foundation → schemas → models → data → sources → agents → graph →
tooling → entry. Each step must leave a **working, documented increment** — update the
relevant `DOCS.md` (and the README status table) before moving on. Stop and verify the
checkpoint at each step.

| # | Step | Checkpoint (what works after) |
|---|---|---|
| 0 | **Scaffolding & environment** — both repo skeletons, `environment.yml` (conda), `.env.example`, `.gitignore`, root `CLAUDE.md` + `AGENTS.md` symlink, `config/` dir, seed `DOCS.md` in each module folder. | Conda env builds; package imports cleanly. |
| 1 | **Schemas** — Pydantic v2 models incl. `FigureRef`. | Schema validation tests pass. |
| 2 | **Config + model registry** — `config/models.yaml`, provider abstraction (OpenAI / Anthropic / Google), role→model resolution. | A real round-trip call succeeds for each provider via config. |
| 3 | **Observability** — LangSmith tracing + per-call cost logging wired into the provider layer. | A traced call appears in LangSmith with recorded cost. |
| 4 | **Storage** — SQLite layer (papers / memory / query_log), embedding space reserved; read/write modules. | Write then read back a paper record (pytest). |
| 5 | **Librarian sources** — `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. | Each source returns results for a sample query. |
| 6 | **Retrieval** — `RetrievalBackend` with `text_search()` over SQLite; `vector_search()` stub raising `NotImplementedError`. | Store, then text-retrieve, a record. |
| 7 | **Runtime prompts** — one `prompts/<role>.md` per role. | Each prompt loads at runtime; no governance file referenced. |
| 8 | **Agent roles** — thin wrappers: librarian I/O, librarian searcher, analyst worker, analyst lead, reviewer (each loads its prompt, uses schemas + config models). | Each role runs one task in isolation. |
| 9 | **Consensus** — `ConsensusStrategy` = debate-to-converge (cap 3) + lead adjudication. | Workers converge (or hit the cap and the lead adjudicates) on a toy task. |
| 10 | **Orchestration** — assemble the LangGraph graph and the two capped feedback edges (paper-search review, analysis review). | Full loop runs end-to-end on a sample request. |
| 11 | **Tool-authoring & sandbox** — `research_toolkit` propose→branch→lead-review→**human-approve merge** flow; subprocess sandbox (timeout, workdir confinement). | Proposing a trivial tool is blocked until approval; `xspec_utils` runs inside the sandbox. |
| 12 | **Entry points & tests** — importable library + CLI (`python -m auto_research "…"`), notebook for inspection, minimal pytest on deterministic parts. | CLI runs a request end-to-end; notebook inspects a run. |

After step 12 the Phase 1 skeleton is complete: a full team running real LLM calls
end-to-end, with vector search, autonomous merge, and the lead-scientist group left as
documented stubs.

---

## Changelog

- *(unreleased)* — Initial `CLAUDE.md` authored for Phase 1 (modular minimal skeleton);
  added §14 build order.
- 2026-05-26 — Added a hard rule in §3 requiring `README.md` to be kept synced in the
  same session as any change that touches what the README describes (status table,
  architecture, defaults, getting-started, contributor rules). Parallels the
  pre-existing `DOCS.md` sync rule.
