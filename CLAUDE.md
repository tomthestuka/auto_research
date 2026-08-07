# CLAUDE.md

**Single source of truth for all coding agents** (Claude Code, Codex, and any other
agent harness) working on the `auto_research` project. All `AGENTS.md`-style files are
symlinks to this file (see [§11](#11-file-linking)).

> This file governs **build-time coding agents** — the assistants that construct and
> maintain the source code. It is **not** for the runtime LLM agents that the system is
> made of. See [§2](#2-the-two-repository-trust-boundary) and [§4](#4-claudemd-and-docsmd-are-build-time-only).

---

## 1. Project overview

`auto_research` is an automated research assistant: a team of LLM agents, orchestrated
with **LangGraph**, that takes a research request, searches the literature, runs
analyses, and reviews the results. The agent team has three roles:

- **Librarians** — find and curate literature (I/O sub-team) and retrieve from local
  memory (searcher sub-team). Librarians **use** tools; they never author them.
- **Analysts** — perform analysis tasks; workers debate to convergence, a lead model
  adjudicates and maintains library discipline. Analysts **author** new tools in Phase 2.
- **Reviewer** — top-tier models that triage research requests, dispatch work, and
  review results against prior literature.

**Phase 1 — DONE.** A working, minimal, modular skeleton: the full team runs end-to-end
with real LLM calls, real literature search, real spectral fitting on real data, and a
composed report. [§8](#8-what-phase-1-built-the-system-as-it-stands) is the retrospective
tour of what exists — read it to learn the lay of the land before touching anything.

**Phase 2 — THE ACTIVE PHASE: tool authoring.** The system extends its own analysis reach
by contributing new tools to `research_toolkit` through a gated, verifiable flow whose
output is committed to a branch and **merged to master only by a human, fully manually,
out of band**. This is the work to build now; [§9](#9-phase-2--tool-authoring-the-active-phase)
is its dedicated spec. Other deferred work is parked in
[§12](#12-deferred-beyond-phase-2) — do **not** build those now.

**What "minimal" means here:** prefer the fewest modules, functions, and lines that
satisfy the interface. Do not add configuration knobs, abstraction layers, retries,
caching, or features that were not requested. When in doubt, write less and document the
extension point in `DOCS.md`.

"Minimal" governs **code we author**, not interfaces the community has already solved.
When a capability is a common, well-trodden interface — a provider API, structured-output
enforcement, retry/backoff, a HITL connector — and a latest-stable, clean
community/official wheel exists, **use the wheel**: prefer the official SDK's native
feature over a hand-rolled equivalent. Hand-rolling a stable, widely-used primitive is
*not* "minimal"; it is extra surface to maintain and a source of bugs the wheel already
fixed. Reserve custom code for the orchestration logic that is genuinely ours.

---

## 2. The two-repository trust boundary

The project is **two separate repositories/packages**. This separation is a safety
mechanism, not a stylistic choice: it is what makes "the system cannot modify its own
code" true *by construction*.

| Repo | Contents | Who commits | Runtime access |
|---|---|---|---|
| **`auto_research`** | Orchestration: LangGraph graph, agent roles, runtime `prompts/`, Pydantic schemas, SQLite layer, sandbox, report. | **The human only.** | **None — read-only.** Runtime agents never write here, in any phase. |
| **`research_toolkit`** | The analysts' domain tool library: uniform `<domain>_utils/` packages ([§9.2](#92-toolkit-architecture--the-uniform-module-structure)). Houses `xspec_utils` today; Phase 2 adds `heasarc_utils`, `lib_utils`, etc. | **The human** + (Phase 2) the runtime, which lands one atomic commit per tool **on a branch**. | **Read-only** for discovery/invocation via `ToolkitRegistry`. The **only** write path is the gated author→gate→review flow ([§9](#9-phase-2--tool-authoring-the-active-phase)), which commits to a **branch** — never to master. |

Consequences:

- The system's *own* code is `auto_research`, a repo the runtime can **never** touch.
- The runtime is **master-blind**: it commits authored tools to a working branch and has
  no concept of, reference to, or path to `research_toolkit`'s trusted branch. A human
  reviews branches and merges to master entirely **out of band** ([§9.9](#99-git-discipline--the-runtime-is-master-blind)).
  **Self-modification — and even un-reviewed toolkit changes — remain impossible by
  construction.**
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

- **Plan and debate before coding.** Before writing or editing code, present a plan and
  debate it with the human to convergence; begin implementation only once it is agreed.
  This holds **except when the human has instructed or permitted you to proceed directly**
  — an explicit go-ahead, a plan they already approved, or a change small enough that they
  have waved you on. When a harness offers a plan mode (Claude Code's
  `EnterPlanMode`/`ExitPlanMode`), use it to structure this; otherwise state the plan in
  chat and wait for the human's agreement. Investigating, reading, and searching to *form*
  the plan are always allowed; it is landing edits ahead of agreement that this rule gates.
- **You perform ZERO git operations.** No `git add`, `git commit`, `git push`, `git
  branch`, `git checkout`, `git stash`, tags — nothing, in either repo. You only create
  and edit files in the working tree. **The human runs all git, for both repos.** When
  you finish a unit of work, summarize what changed and which files, so the human can
  stage and commit. (This is separate from the *runtime* git workflow you **implement as
  application code** in [§9.9](#99-git-discipline--the-runtime-is-master-blind) — that is
  a feature the system executes on `research_toolkit` branches, not git you execute.)
- **Keep every `DOCS.md` synced.** Any code change must be reflected in the relevant
  `DOCS.md` in the same session, including a changelog entry. Out-of-sync docs are a bug.
- **Keep `README.md` synced.** After every change, update `README.md` in the same
  session whenever the change touches anything the README describes — the build-status
  table, architecture diagram, model defaults, getting-started, environment, or
  contributor rules. A change is not complete until the README reflects it. Prefer a
  precise edit over a sweeping rewrite.
- **Never leak governance files into runtime.** `CLAUDE.md` and any `DOCS.md` must never
  be loaded into a runtime LLM's context — not as a system prompt, tool input, retrieved
  document, or otherwise. Runtime prompts come *only* from `prompts/` (see
  [§4](#4-claudemd-and-docsmd-are-build-time-only)). This includes the Phase-2 tool
  author: its conventions live in `prompts/coder.md`, never in a `DOCS.md`.
- **Never hardcode model identifiers.** All model strings live in config
  ([§6](#6-config-driven-model-registry)).
- **Never commit secrets.** API keys live in `.env` (gitignored); ship a `.env.example`.
- **Minimal code** ([§1](#1-project-overview)).
- **Write only positive actions.** Prompts and the descriptions the model reads
  (tool descriptions, schema field descriptions) state what to do and what a thing
  does. Convert every prohibition into the positive action it implies. Never write
  "do not / never / avoid" — two prohibitions are sanctioned: this rule itself, and
  the **no-fabrication ban** ("never fabricate data/results/citations", the
  banned-vocabulary list), which stays an explicit ban wherever it appears because it
  is safety-critical.
- **Consult the human before fixing unexpected issues.** When an unexpected failure,
  environment/tooling oddity, ambiguous runtime behavior, or suspected dependency issue
  appears, collect only the minimal facts needed to describe it, then ask the human
  before attempting a fix, workaround, or architectural change.

---

## 4. `CLAUDE.md` and `DOCS.md` are build-time only

No runtime LLM agent may ever see `CLAUDE.md` or any `DOCS.md`. Runtime agents are
configured exclusively from:

- `prompts/` — one system-prompt file per role (e.g. `prompts/reviewer.md`,
  `prompts/librarian_io.md`, `prompts/analyst_worker.md`, `prompts/analyst_lead.md`, and
  the Phase-2 `prompts/coder.md`). The runtime loads these; nothing else.

When implementing any agent, load its prompt from `prompts/` and pass only task data and
schemas into the model. If you ever feel tempted to give a runtime agent "context about
the project," that is a signal you are about to violate this rule — put the needed
information into its `prompts/` file or its task schema instead. **In particular, the tool
author's "local structure context" ([§9.3](#93-actors-and-the-propose--author--review-flow))
is assembled from runtime-safe sources — real toolkit source, `prompts/coder.md`,
and config — never from a `DOCS.md`.**

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
(`config/models.yaml`), mapping **role → one or more model members**. Each member has a
provider, model id, vision flag, and provider-specific parameters. No model identifier
appears in source code.

Providers: **OpenAI, Anthropic, Google.** Reviewer-tier and analyst models **must be
vision-capable** (figures, [§7](#7-schemas--protocols)).

**Phase 2 — authoring/review providers.** Only **Claude and GPT** are reliable enough to
author and review tools. The tool **author and reviewer must be different providers**
(e.g. author Claude / reviewer GPT) so a merge candidate gets two *independent* checks,
not one model checking itself ([§9.3](#93-actors-and-the-propose--author--review-flow)).
Gemini may back cheaper, non-gating roles but stays off the critical author/review path.
The **coding/author agent is Sonnet-tier** (medium), spawned by the Opus analyst lead.
Because the coder is the weaker model, *architectural* decisions — extend an existing
toolkit vs. author a new one ([§9.3](#93-actors-and-the-propose--author--review-flow)) —
stay with the more-capable proposer/lead; the Sonnet author executes them.

Default tier mapping (a *config default*, edit freely — not code):

| Role | Tier | Example defaults |
|---|---|---|
| Reviewer group | top | `claude-opus-4-8`, `gpt-5.5` (reasoning high), `gemini-3.5-flash` (thinking high) |
| Analyst lead | top | `claude-opus-4-8` |
| Analyst worker group | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning medium) |
| Librarian I/O group | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning medium), `gemini-3.5-flash` (thinking high) |
| Librarian searcher group | light | `gpt-5.4-nano` |
| Embeddings | — | `text-embedding-3-small` *(deferred — see [§12](#12-deferred-beyond-phase-2))* |

Implement a thin provider abstraction so a role can be backed by any provider via config.
Keep it minimal; do not build a general model-routing framework.

---

## 7. Schemas & protocols

All inter-agent messages are **Pydantic v2** models in a shared `schemas` module. They
are the protocol and double as documentation. Bad data must fail loudly and early.

Representative messages: `ResearchRequest`, `SearchRequest` / `SearchResult`,
`AnalysisTask` / `AnalystReport`, `ReviewVerdict`, the report types (`ReportSection`,
`Reference`, `ResearchReport`, `LatexReport`), and the Phase-2 additions `ToolSpec`,
`HumanQuery` / `HumanResponse` ([§9](#9-phase-2--tool-authoring-the-active-phase)). Keep
the set minimal; refine signatures in the module's `DOCS.md`.

**Figures — by reference, never by value.** Figures are critical to review, but live
image objects must never live inside messages (they break JSON serialization and bloat
logs, LangSmith traces, and SQLite). Pattern:

- A figure-producing tool writes the image to the run's **artifacts directory** (in the
  runtime data dir, [§10](#10-tech-stack--conventions)) and returns a lightweight
  `FigureRef`: `path`, `caption`, `mime_type`, `produced_by`, `created_at`, `sha256`, and
  optional small `thumbnail_b64`.
- Messages carry `list[FigureRef]` — clean and serializable.
- Bytes are loaded and passed as image content blocks **only at the multimodal-call
  boundary** — exactly when the reviewer, or analysts during debate, need to *see* them.

The `FigureRef` `sha256` + `produced_by` pair is the seed of the **provenance** model
Phase 2 extends to all tool results ([§9.5](#95-no-fabrication-is-absolute--synthetic-data-is-data-never-toolkit-code)).

---

## 8. What Phase 1 built (the system as it stands)

Phase 1 is complete and documented. This is the orientation tour; each module's `DOCS.md`
is the authoritative interface. Phase 2 builds **on top of** this — do not rebuild it.

**Foundation.** Pydantic v2 `schemas` (the protocol, incl. `FigureRef`); a config-driven
`models` provider abstraction (OpenAI/Anthropic/Google) resolving role→model groups from
`config/models.yaml`; `observability` (LangSmith tracing + per-call cost logging) wired
into the provider layer; `storage` (SQLite: papers / memory / query_log, with an embedding
column reserved); `observations.py` (generic mission archive paths from
`config/observations.yaml`).

**Librarians & retrieval.** `sources` exposes a shared `QuerySource` interface with
`arxiv`, `nasa_ads`, and `web` (Tavily) adapters. `retrieval` implements `text_search()`
over SQLite; `vector_search()` is a Phase-2 stub raising `NotImplementedError`. The
librarian I/O role builds queries and ingests papers; `evaluate_search_results()` is a
stub returning `accepted=True` (IO ingest consensus deferred — [§12](#12-deferred-beyond-phase-2)).
*(Phase 2 relocates literature search into a `research_toolkit/lib_utils` package —
[§9.2](#92-toolkit-architecture--the-uniform-module-structure).)*

**Agents.** Thin per-role wrappers (`agents`, `roles.py`) each load their `prompts/<role>.md`,
resolve models via config, and exchange structured Pydantic messages: reviewer, librarian
I/O, analyst worker, analyst lead.

**Consensus.** `ConsensusStrategy` = `DebateToConverge`: workers each analyze; each round
they see peers' analyses and figures and revise; the lead (top tier) judges convergence
after each round; stop on convergence or at a **hard cap of 3 rounds**, then the lead
adjudicates and flags residual uncertainty. Tool figures propagate from `ToolResult` onto
worker `AnalystReport.figures` and into the final report, so the reviewer actually sees the
plots.

**Orchestration.** The LangGraph graph wires request → reviewer dispatches analysis +
literature tasks → reviewer digests results and compares against prior work → human review.
Two **capped feedback edges** (default 3): paper-search review (reviewer→librarian) and
analysis review (reviewer→analyst). Studio wiring (`serve.py` + `langgraph.json`) exposes
live `ResearchState` per node.

**Sandbox & tooling.** Every analyst tool call runs in an **isolated subprocess**
(`PersistentSandbox`, a persistent per-worker REPL) with a timeout, a confined working
directory, and `PYTHONPATH` limited to `research_toolkit`. Tools are discovered by importing
each toolkit package's top-level `TOOL_MANIFEST` dict via `ToolkitRegistry`
(`sandbox/registry.py`); a `disabled_tools` filter is honored. On a failed call, the error
(stderr + exit code) is fed back to the worker, which self-corrects and re-issues the
`ToolCallRequest`, capped at `tool_retry_cap` (default 3, `orchestration.yaml`). XSPEC boots
non-interactive (`Fit.query='no'`, `Xset.allowPrompting=False`, `Plot.xAxis="keV"`).
**Known defect Phase 2 fixes first ([§9.10](#910-build-order-phase-2) step 0):** the
worker `cwd` currently defaults into the orchestration repo root, so `bash_exec` can write
into `auto_research`; it must be relocated to per-worker scratch under the runtime data dir.
The runtime tool-authoring path, `sandbox/merge_flow.propose_and_pr()`, is a stub raising
`NotImplementedError` — **this is what Phase 2 implements.**

**Report & entry.** The `report` module composes, after an accepted verdict, a machine
handle (structured JSONL with resolvable `FigureRef`/file references) and a human handle
(compilable LaTeX with real `\includegraphics`/citations); an in-graph `compose_report`
node runs a compile+ref-check feedback loop capped at `report_retry_cap`. The `monitor`
gives async-safe run logging (JSONL + team-grouped Markdown, 1–10 chatter). Entry points:
`run_research()` (monitored library call), `python -m auto_research "…" --chatter 10`
(CLI, incl. `--report-format`), and a notebook. Minimal `pytest` covers deterministic
parts only (DB ops, schema validation, report ref-checking). End-to-end `run_research()`
behavior is **observed via `test_full_suite.py` (a live full run), not asserted by the
deterministic suite.**

**Domain knowledge captured in `prompts/` + the toolkit** (not here): for polarized joint
fits, IXPE must be data-group 1 (IXPE-first load order) or the fit diverges; validated on
the Cyg X-1 joint fit at stat/dof ≈ 1.8. A useful *example* known-good result — but **not**
a universal yardstick: Phase-2 tools mostly do things with no prior human answer
([§9.4](#94-the-mergeability-gate-deterministic--the-real-trust-boundary)).

---

## 9. Phase 2 — tool authoring (the active phase)

The system authors new domain tools into `research_toolkit` when an analysis needs a
capability the toolkit lacks. This section is the spec to build.

### 9.1 The trust thesis (and its limit)

We do **not** "trust" tools — we make them **verifiable**: *real data in, real product
out, reproducible by a human, checked against properties that must hold, carrying a
provenance chain.* If every tool is verifiable in that sense, results built from them are
trustworthy. Trust is the expensive fallback for what we cannot cheaply verify; design
for verification.

State the limit plainly: **tool-level trust is necessary but not sufficient.** A correct
tool can still be called with wrong arguments, on the wrong data, in an invalid order, or
have its output misread. So Phase 2 verifies the *tool* **and** carries provenance through
the *composition* ([§9.5](#95-no-fabrication-is-absolute--synthetic-data-is-data-never-toolkit-code)).

### 9.2 Toolkit architecture — the uniform module structure

All domain capability lives in `research_toolkit` as **uniform packages**. `xspec_utils` is
the template every new toolkit mirrors **exactly** — this is the layout a build-time coding
agent scaffolds and what `prompts/coder.md` instructs the runtime author to produce:

```
<domain>_utils/
  __init__.py            # re-exports TOOL_MANIFEST for ToolkitRegistry; puts lib/ on sys.path.
                         #   NO heavy domain imports here — must load in the orchestration env.
  lib/                   # ── HUMAN-HALF: human-authored, importable, runnable domain code ──
    <mission|source>.py  #   thin per-mission/source strategy modules: NICER.py, NuSTAR.py,
                         #   red_ixpe.py  |  arxiv.py, nasaads.py, web.py
    <helpers>.py         #   small shared helpers (find_src.py, src_pos.py, …)
    <domain>_help.py     #   the high-level, ABSTRACTED entry API a person calls (e.g. load_data)
  skills/                # ── AI-HALF: the runtime tool interface ──
    __init__.py          #   TOOL_MANIFEST — a PURE-DATA dict (no domain imports); each entry
                         #   wraps a lib/ fn: {module, fn, description, args_schema}
    session.py           #   science-stack-dependent wrappers; imported ONLY in the REPL subprocess
```

Invariants every toolkit must hold (they are *why* the split exists):
- **`skills/__init__.py` (`TOOL_MANIFEST`) is pure data** — no XSPEC/HEASoft/heavy imports — so
  `ToolkitRegistry` discovers it in the orchestration env, which has no science stack. Each
  entry points at a `lib/` function (`module: "<domain>_utils.lib.<help>"`, `fn`, `description`,
  `args_schema`); domain code is **never duplicated** into `skills/`.
- **`lib/` is the human-half**: importable and runnable by a person; its `<domain>_help.py`
  entry points are **abstracted on purpose** — they hide the failure-prone decisions
  (data-group ordering, gain linking, response paths; instrument data-paths auto-filled from
  config) inside tested code so **neither the human nor the AI can get them wrong.** Pushing
  error-prone choices out of per-call LLM arguments and into the toolkit *is* the correctness
  strategy — the IXPE-first divergence ([§8](#8-what-phase-1-built-the-system-as-it-stands))
  should be impossible to *express*, not a rule in a prompt.
- **`session.py` holds the science-stack-dependent code**, imported only inside the persistent
  REPL subprocess (where HEASoft/XSPEC live).

**The two halves map onto this structure:** human-half = `lib/`; ai-half = `skills/`. The rule
that gives "trust the tool" teeth: **a tool is not mergeable unless a human, importing the
`lib/` interface, reproduces the ai-half's result within tolerance on the same named input**
(human merge scope, [§9.9](#99-git-discipline--the-runtime-is-master-blind)).

**Planned toolkits:** `xspec_utils` (exists), `heasarc_utils` (acquire not-yet-local data),
`lib_utils` (literature search — `arxiv.py`, `nasaads.py`, `web.py`; relocated out of
`auto_research/sources`, leaving `auto_research` as pure orchestration). **Librarians never
author or request new tools — they use existing ones, period. Analysts author every
toolkit.**

### 9.3 Actors and the propose → author → review flow

1. An **analyst worker** hits a missing capability and raises it on its `AnalystReport`
   (`tool_request`), carrying a `ToolSpec`. The `ToolSpec` is a **brief, not a frozen
   contract**: the **science context** (what is being measured, why the toolkit can't do
   it), the **expected behavior** (signature, I/O), a **real validation dataset** (an
   ObsID), a **disposition** (`extend_existing` an existing toolkit vs `new_package`), and
   **proposed verification checks** ([§9.4](#94-the-mergeability-gate-deterministic--the-real-trust-boundary))
   that are explicitly *advisory and revisable* — the worker sees only the minimal `skills/`
   stubs and is not the domain expert, so the authoritative checks (and final tolerances)
   are finalized with the tool and committed beside it. The **disposition is the
   proposer/lead's call, not the coding agent's**: because the coding sub-agent is the weaker
   Sonnet ([§6](#6-config-driven-model-registry)), the architectural "edit existing vs author
   new" decision stays with the more-capable proposer (the lead, who reads the full `lib/`
   source), and the Sonnet author executes it.
2. The **analyst lead spawns a Sonnet-tier Claude coding sub-agent** (the **author**) and
   stays in an **interactive channel** with it during authoring — to clarify science/code as
   it goes (this lead↔author loop is built in step 2 of [§9.10](#910-build-order-phase-2)).
   The author writes both halves + tests in an **isolated git worktree/branch** on
   `research_toolkit`, using `xspec_utils` as the template, and — reading the **full `lib/`
   source**, not just the minimal `skills/` stubs — either **edits existing files to build on
   current capability or scaffolds a new package**, per the `ToolSpec` disposition. The author
   is the **Claude Agent SDK** harness seeded with the `ToolSpec`. The
   harness injects **local structure context** (the `xspec_utils` template, conventions,
   obs-db paths, existing primitives) from **runtime-safe sources only** — real toolkit
   source, `prompts/coder.md`, and config — **never `DOCS.md`/`CLAUDE.md`**
   ([§4](#4-claudemd-and-docsmd-are-build-time-only)). The `ToolSpec` is small (~2–4K
   tokens) and *is* the context-control mechanism — no special token machinery needed.
3. The **deterministic gate** ([§9.4](#94-the-mergeability-gate-deterministic--the-real-trust-boundary))
   runs. This — not an LLM's opinion — is the arbiter.
4. A **provider-decorrelated reviewer** reviews the diff and the gate result. The analyst
   lead is a *group*: a **Claude science lead** (convergence, adjudication, library
   discipline) and a **GPT analyst co-lead whose Phase-2 job is tool-code review only**.
   Review is routed *from config* to the member whose **provider differs from the author's**
   (`reviewer.provider ≠ author.provider`, [§6](#6-config-driven-model-registry)) — the
   author is the Claude SDK, so the GPT co-lead reviews. It judges: does the code match the
   `ToolSpec`, are the Layer-B checks the right ones and strict enough, is there a subtle
   bug. Its **review-pass + the deterministic gate** authorize the commit.
5. On gate-pass + co-lead review-pass, `approve_edit()` lands **one atomic commit per tool on
   the working branch** ([§9.9](#99-git-discipline--the-runtime-is-master-blind)). A human
   merges to master manually, out of band.

### 9.4 The mergeability gate (deterministic — the real trust boundary)

At the frontier there is usually **no known answer** to compare against. So the gate
verifies **without** ground truth, in two layers:

This section is **entirely runtime/LLM scope** — the checks the system runs on its own before
landing a branch commit. (Replicability and golden-regression are *not* here; they are the
human's, at merge — [§9.9](#99-git-discipline--the-runtime-is-master-blind).)

**Layer A — the generic gate** (lives in `merge_flow`, domain-blind; the only checks the gate
itself implements):
- **Determinism** — re-running yields the same per-test outcomes. The gate runs the tool's
  tests twice and compares a JUnit `{test_id: PASSED/FAILED/ERROR/SKIPPED}` map (not byte-
  identical pytest output, which chatty science tooling makes a false-positive magnet); the run
  is deterministic ⇔ the two maps are equal. Result-reproducibility *within tolerance* is a
  **science tool's** own `test_repro…` Layer-B test (lead-planned, coder-written, co-lead-
  reviewed — not gate-enforced), whose verdict the same map tracks; rigorous replicability stays
  the human's at merge ([§9.9](#99-git-discipline--the-runtime-is-master-blind)).
- **No-fabrication scan** — the diff and any captured output trip no banned-vocabulary
  check ("representative", "synthetic", "illustrative", "mock", "placeholder", "for now",
  "in operational mode this would…") and contain **no data-fabricating code**
  ([§9.5](#95-no-fabrication-is-absolute--synthetic-data-is-data-never-toolkit-code)). This scan
  runs **twice, independently**: mechanically here in the harness, and again as a judgment call
  in the GPT co-lead review ([§9.3](#93-actors-and-the-propose--author--review-flow)).
- **Structural** — a `TOOL_MANIFEST` entry and a test file exist; only allowed
  `research_toolkit` paths are touched.
- **Provenance** — outputs carry input **sha256** hashes + the tool-call chain (extend the
  `FigureRef` pattern, [§7](#7-schemas--protocols), to all results), making "computed a
  downstream result from a failed/synthetic upstream step" mechanically detectable.

**Layer B — the tool's own tests**, authored *with* the tool, committed beside it (as
`xspec_utils` already carries its tests), and *run* by the gate. The `ToolSpec`'s checks are
only a **proposal** — the author and lead, with full `lib/` sight and knowledge of the real
library's limits, may revise or drop checks the worker proposed (it isn't the domain expert);
the **committed tests, not the `ToolSpec`, are authoritative**.
There is **no central per-tool framework** and **no universal invariant list** (physics
checks like `pol ≤ 1` belong to spectral fitting, not to data acquisition or literature
search). The author picks from an **open menu** of verification patterns; for genuinely
novel tools the author and reviewer may have to **invent new check families nobody listed**:

| Pattern (examples — not exhaustive) | `xspec_utils` | `heasarc_utils` | `lib_utils` |
|---|---|---|---|
| known-input → known-output | zero injected pol → recovered ≈ 0 | request ObsID X → manifest matches archive metadata | known bibcode → appears |
| differential vs. trusted code | agrees with a validated primitive in-regime | re-fetch a local ObsID → bytes match | overlaps `arxiv.py` on a shared query |
| metamorphic invariant | load-order independence; exposure scaling | request order doesn't change the set | token-order invariant |
| injection–recovery | recover injected pol on a synth ObsID | — | — |
| cross-computation agreement | pol via tool vs. hand Stokes calc | checksum vs. archive checksum | — |
| declared sanity bounds | pol ≤ 1, errors finite, χ²/dof in band | files > 0, checksums match, valid FITS | DOIs well-formed, no dup bibcodes |

So the runtime's own guarantee before a commit is exactly: the no-fabrication scan passes at
**both** levels (harness + co-lead review), the deterministic Layer-A checks pass, and the
committed Layer-B tests pass. When a check needs synthetic data, it is **generated by the
analyst in worker scratch (lead-reviewed) and stored under the mission `obs_db_path` as a
marked `synth…` ObsID — never as toolkit code** ([§9.5](#95-no-fabrication-is-absolute--synthetic-data-is-data-never-toolkit-code));
the Layer-B test then references that ObsID. The **reviewer's most leveraged job** is judging
whether Layer B's checks are the right ones and strict enough — an easier call than judging a
novel result, because invariants ("load order can't matter," "a checksum must match") are
auditable even when the answer is brand-new.

Tests ship **with** the tool; an untested tool is not mergeable. The LLM lead's review is a
*pre-filter*; the gate is the *door*.

### 9.5 No fabrication is absolute — synthetic data is data, never toolkit code

**No data-fabricating code, ever, in `research_toolkit`, for any purpose.** Any
synth-writing code in a toolkit diff is an automatic reject by the no-fabrication scan.

Synthetic data is *data, not code*:
- generated in **worker scratch** ([§10](#10-tech-stack--conventions)), using existing
  tools (e.g. `ixpeobssim`), and **reviewed by the analyst lead**;
- stored under the mission `obs_db_path` (`config/observations.yaml`) as a **marked fake
  ObsID** — e.g. synthetic IXPE data goes under the `ixpe` path as
  `…/IXPE/synth<YYMMDD>-<sha256>/`;
- a committed injection-recovery test references that **ObsID** (a data input), so the
  toolkit holds zero synth-generating code; the fixture persists under the mission path so
  the gate can find it.

Because synth ObsIDs are **marked**, provenance flags any *science result* computed on a
synthetic input. Validate-a-tool-on-synth-data: allowed. Present-synth-output-as-a-finding:
caught by construction.

### 9.6 Failure handling — honest failure is a correct outcome

There is **no obligation to return a result.** The agent must **classify** a failure
before reacting:

- **my-bug** (bad args, wrong call, code I wrote) → self-correct and retry within
  `tool_retry_cap`.
- **environment / data / human fault** (missing file, broken dependency, contradictory
  request) → **raise to the human** ([§9.7](#97-raise-to-human--clarification-only-every-role)); **never fabricate.**

`AnalystReport` gains a first-class `FAILED` / `NEEDS_HUMAN` terminal state that the lead
and reviewer treat as a *correct* outcome with **no quality penalty** — otherwise the retry
cap pressures the agent to invent something on the last attempt. "Real results or no
results" is absolute.

**The reviewer is the sole I/O — an honest failure is adjudicated, not bypassed.** When an
authoring attempt does not commit, the analyst lead composes **one terminal report**
(`finalize_authoring_outcome`) documenting the unavailable capability and any partial findings,
rather than re-authoring or re-entering debate (closing the cap-exhausted authoring *spin*). That
report — like every analysis result — flows to the **reviewer**, who decides **finalize vs.
bounded rework** (capped by `max_iterations`). On finalize a report is **always** composed
(honouring `report_formats`), so the honest failure is *documented*, never silent. The
no-fabrication guarantee for that report lives in the reviewer + compose prompts (state the
limitation; never invent a result), no longer in suppressing the report.

### 9.7 Raise-to-human — clarification only, every role

Raise-to-human is a **cross-cutting clarification channel, not a merge gate.** Every role —
reviewer, analyst lead, analyst worker, tool author — can raise a question about **science
or code** when blocked, *early*, before a corrupt dispatch drives the team into writing bad
code. It is **fully decoupled** from toolkit merging (which is manual and out of band,
[§9.9](#99-git-discipline--the-runtime-is-master-blind)).

- One schema: `HumanQuery` / `HumanResponse`, with `kind ∈ {clarify_science, clarify_code,
  blocked}`. (There is no `approve` kind — the runtime never asks to merge.)
- Mechanism: a LangGraph **`interrupt()`** (the ecosystem-standard HITL primitive) in the
  graph; inside the Claude-SDK author, the equivalent **`ask_human`** in-process tool
  (`@tool` + `create_sdk_mcp_server`), whose handler runs in the parent process.

### 9.8 Human-feedback channels (CLI vs. library)

`run_research()` **never calls `input()`** and **never fabricates** on a raise. The contract
is **handler-injection**: `run_research(..., human_handler=fn)`, `fn: (HumanQuery) ->
HumanResponse`. The handler is the interface; connectors are pluggable backends behind it.

- **CLI:** ships a stdin handler (attended terminal). Trivial.
- **Library:** the caller passes a connector-backed handler; a **blocking handler =
  hang-and-wait**, which is the accepted behavior for now.
- **Fail-safe:** no handler + headless → raise a typed `HumanInputRequired`, never silently
  hang. The `HumanQuery` also surfaces on the monitor channel.
- **Connector modules to build** (each a thin wrapper over a community library; no
  hand-rolled protocol): a **CLI stdin** handler; **`slack.py`** → `slack_bolt` (Socket
  Mode; the reference two-way backend); **`telegram.py`** → `python-telegram-bot`;
  **`whatsapp.py`** → Twilio. **Email is deferred.** A one-way broadcast layer (`Apprise`)
  is optional for `blocked`/FYI notices.
- **Durable suspend → resume** (a LangGraph checkpointer, so a raise can be answered later
  without holding the process) is a **TODO** ([§12](#12-deferred-beyond-phase-2)).

### 9.9 Git discipline — the runtime is master-blind

This is application code inside `merge_flow`, distinct from the [§3](#3-coding-agent-contract-read-this-every-session)
rule that *you, the coding agent, perform zero git*:

- The author works in an **isolated worktree/branch** on `research_toolkit`.
- The lead's **`approve_edit()`** is where git lives: it asserts the gate passed, then makes
  **one atomic commit** to the branch — explicit-path staging (strategy module + human-
  interface delta + `skills/TOOL_MANIFEST` entry + tests; never `git add -A`, so scratch
  never leaks in) — with a commit message carrying the `ToolSpec` id + gate-report hash. The
  LLM never issues git; `approve_edit()` (code) does, and it is unreachable unless the gate
  passed **and** the provider-decorrelated GPT co-lead's review passed. **Commit ⇔ gate-pass
  ∧ co-lead review-pass.** A reject → no commit, feedback to the author.
- The **coder never issues git** — it is the runtime's job, so the author's Bash carries a deny-rule
  (`disallowed_tools=["Bash(git:*)", "Bash(git)"]` on the coder's own options) and `prompts/coder.md`
  tells it to leave git to the runtime and `ask_lead` instead of working around it. Deny-rules are
  best-effort (prefix-matched; pipes/subshells can slip), so the gate-entry check and reset below are
  the robust backstops.
- As the **first action at the gate**, `_gated` compares HEAD to the per-attempt base ref; if the
  coder committed (slipped the deny-rule), the runtime does `git reset --mixed <base_ref>` — undoing
  the commit but **keeping the work in the working tree** — and emits a `coder.committed` event, so the
  gate then scans the recovered changes normally. The coder never touches git, its work is preserved,
  and the only real commit remains `approve_edit`'s (post gate+review), so `commit ⇔ gate ∧ review`
  holds. This is what makes the gate robust to commit state (vs. the old empty-changeset confusion).
- A **non-committed terminal attempt auto-resets** the working tree back to the attempt's **base ref**
  — HEAD recorded per-attempt at `propose_and_pr` entry — not just to `HEAD` (`git reset --hard
  <base_ref>` + `git clean -fd`), after saving the discarded attempt's diff vs the base ref as a
  `tool_author_failed_*.patch` under the run's `authoring/` dir. Resetting to the base ref (rather
  than `HEAD`) **drops any commit the coder lands itself** — a `reset --hard HEAD` would keep it —
  while preserving an earlier round's successful `approve_edit` commit in a multi-tool run. This keeps
  a rejected attempt's edits (and any illegitimate commit) from surfacing a phantom tool to re-running
  workers or tripping the next attempt's clean-tree precondition. The launch preflight still asserts a
  clean branch at startup (a dirty tree, or a survived commit, there means a run was killed before its
  reset ran — the §12 Ctrl-C hardening). The runtime resets only its own authoring branch; it never
  touches master.
- The runtime has **no concept of master** — it never merges, never asks to merge, holds no
  reference to the trusted branch. A **human reviews branches and merges to master entirely
  manually, out of band**; that human review is where **replicability** (rerun the human-half)
  and **golden-regression** (vs. known-good references) are performed
  ([§9.4](#94-the-mergeability-gate-deterministic--the-real-trust-boundary)). Rewind = a human
  `git revert`s the one commit. One tool = one commit = clean revert.
- The runtime **never** writes to `auto_research`; self-modification stays impossible by
  construction ([§2](#2-the-two-repository-trust-boundary)).

### 9.10 Build order (Phase 2)

Each step leaves a **working, documented increment** — update the relevant `DOCS.md` and
README before moving on. Stop and verify the checkpoint at each step.

Statuses: **Todo** = in Phase 2 scope, not yet built · **Done** = built & documented.

| # | Status | What is being built | Checkpoint (what works after) |
|---|---|---|---|
| 0 | Done | **Relocate the sandbox scratch** — worker `cwd` → per-worker scratch under the runtime data dir; `PYTHONPATH` stays limited to `research_toolkit`; neither repo is ever the cwd. | `bash_exec` cannot write into either repo; figures/scratch land under the data dir. |
| 1 | Done | **`ToolSpec` + two-halves contract** — schema a worker emits (science context, expected behavior, validation ObsID, proposed checks) + the human-half/ai-half + tolerance contract. | Schema validates; a sample `ToolSpec` round-trips; documented in `schemas/DOCS.md`. |
| 2 | Done | **merge_flow harness + rework-loop scaffold** — `merge_flow.propose_and_pr()` invokes the Claude Agent SDK to write human-half, ai-half (`TOOL_MANIFEST` entry), and tests in an isolated worktree, structure-context injected from runtime-safe sources (`xspec_utils` + `prompts/coder.md` + config). Built with the L2/L3/L4 rework loop around **injectable gate/review hooks** (stub-PASS now; steps 3/4 drop in), the `ask_lead`/`ask_human` escalation tools, and the `coder` model role. | From a `ToolSpec`, the agent produces a branch with both halves and tests; gate/review rejections re-drive the warm session; cap exhaustion returns a no-commit failure for the lead to raise to the human; nothing touches master. |
| 3 | Done | **The deterministic gate** — generic Layer-A checks + a Layer-B tool-local test runner + provenance wiring + the no-fabrication scan. | A good tool passes; one that fabricates, fails replication, or breaks a declared check is rejected with a specific machine-readable reason. |
| 4 | Done | **Provider-decorrelated review** — a **GPT analyst co-lead** (provider ≠ the Claude author, routed from config) reviews the diff + gate result and judges the Layer-B checks; code-review only. Its pass + the gate authorizes the commit. | Review runs on a different provider than the author; a planted subtle bug is caught or escalated, not rubber-stamped. |
| 5 | Done | **Raise-to-human (clarification)** — `HumanQuery`/`HumanResponse` schema; the injectable `human_handler` + a shared `ask_human` tool for every ReAct role and the author (blocking direct-handler; LangGraph `interrupt()`+checkpointer durable suspend/resume deferred to §12); handler-injection with CLI/`slack.py`/`whatsapp.py` connectors behind an auto-detecting resolver; `FAILED`/`NEEDS_HUMAN` routing + failure classification. | A blocked role raises and the run blocks for a human; an honest FAILED is accepted with no penalty; no fabrication on cap exhaustion. |
| 6 | Done | **`approve_edit()` atomic commit** — on gate-pass + co-lead review-pass, one atomic commit per tool to the branch; the runtime never merges; replicability + golden-regression happen at the human merge, not the gate. | An approved tool lands as one commit on a branch; a human reruns the human-half, checks regression, and merges/reverts cleanly. |
| 7 | Done | **End-to-end** — a sibling of `test_full_suite.py` poses a question requiring a missing capability; an analyst authors a tool through the whole flow; it lands on a branch; a human merges; the analysis re-runs on the new tool. | The missing-capability analysis completes using a freshly authored, gated tool. (Phase-2 end-to-end testing is observational/live — here the `notebooks/usage_example.ipynb` "test code authoring" run, IXPE P1 — not deterministic assertions.) |

---

## 10. Tech stack & conventions

- **Environment:** conda, latest stable Python (pinned in `environment.yml`; conda-forge).
- **Orchestration:** LangGraph. The reviewer→dispatch→review loop is a graph with
  conditional/cyclic edges and **hard iteration caps**.
- **Async-first:** write interfaces as `async`. I/O-bound work (LLM calls, source
  queries, DB) should be concurrency-friendly.
- **Schemas:** Pydantic v2 ([§7](#7-schemas--protocols)).
- **Storage:** SQLite via Python (papers / memory / query_log; embedding column reserved
  for vector search — [§12](#12-deferred-beyond-phase-2)).
- **Observability:** LangSmith tracing + per-call cost logging in the provider layer.
- **Secrets:** `.env` (gitignored) + `.env.example`. NASA ADS token and all API keys here.
- **Runtime data dir:** a configurable directory **outside both repos**, not committed,
  holding the SQLite file, the figures/artifacts store, run logs, and the **per-worker
  sandbox scratch** (`<DATA_DIR>/runs/<run_id>/workers/<worker_id>/`, persisted per-run,
  wiped between runs). This is the only place a runtime tool may write.
- **HITL connectors:** community libraries behind the `human_handler` interface —
  `slack_bolt`, `python-telegram-bot`, Twilio ([§9.8](#98-human-feedback-channels-cli-vs-library)).
- **Testing:** minimal `pytest` on deterministic parts only (DB ops, query parsing, schema
  validation, the Phase-2 gate). Do not assert subjective LLM behavior. End-to-end behavior
  is exercised live via `test_full_suite.py` and its Phase-2 siblings.
- **Live API tests:** every module wrapping an external API leaves an opt-in, env-gated
  real-API test stub. Default to running the relevant live test when working on that
  integration, unless the human says otherwise, credentials are unavailable, or spend is
  inappropriate. Live tests write logs under `logs/dev/`.
- **Development test logs:** any command that intentionally emits logs, live model outputs,
  usage, or cost data writes those artifacts under `logs/dev/`. Prefer JSONL plus a
  Markdown summary. These are local development artifacts, not runtime memory.

---

## 11. File linking

- This `CLAUDE.md` is the single source of truth, placed at the development workspace root.
- Other agent-instruction files are **symlinks** to it, e.g. `ln -s CLAUDE.md AGENTS.md`.
- If a tool requires the file inside a specific repo, symlink it there too rather than
  copying. Never fork the content — one file, many links.

---

## 12. Deferred beyond Phase 2 (do NOT build now — leave stubs)

- **Durable HITL suspend/resume:** a LangGraph checkpointer so a `HumanQuery` can be
  answered later without holding the process (Phase 2 ships the blocking handler only).
- **Human-steer channel — BUILT (the stop half, 2026-07-31); what remains is *redirect*.** The
  bundle's abort half is done and is no longer a flag anyone polls: `/stop` (and SIGINT/SIGTERM on a
  process we own) calls `RunControl.interrupt`, which **cancels the run task**, runs a deterministic
  shielded cleanup, and raises `UserInterruptionError` (contract in `graph/DOCS.md`). That closes graceful shutdown
  (the SIGINT handler, the consolidated cleanup, the `asyncio.shield` protecting the
  authoring-branch reset, the OS-level child kill) with **one** architecture across the three entry
  contexts, because cancellation is the one mechanism that works in all of them: the CLI and the
  detached backend install signal handlers (a library must not seize its host's signals, and off the
  main thread `signal.signal` raises), while a notebook — where ipykernel keeps the interrupt and
  often never cancels the awaited run (observed 2026-06-26) — reaches the same path through `/stop`.
  Still open: a *redirect* steer that binds the whole run rather than one role. `/steer <id>` reaches
  one role at its next ReAct turn, which is right for a message; what has no channel is a run-wide
  "change course" that every role picks up. Durable HITL suspend at `interrupt()` stays deferred
  (it re-runs the node on resume — infeasible mid-`_react`).
- **OS-level sandbox jailing:** read-only bind-mounts / seccomp / egress control beyond the
  relocated-scratch subprocess sandbox. *(Partly mitigated: every runtime agent's file access is
  now confined to the `config/orchestration.yaml: agent_fs_allow` allow-list via
  `sandbox.fs_guard.PathGuard` — a `PreToolUse` hook for the coder, provider checks for the
  reviewer/lead/workers — so `auto_research` and governance files are denied. The OS jail of that
  same allow-list is what remains: it would also gate a shell subprocess's **implicit** reads,
  which the command-string `Bash` check cannot see. See §13.)*
- **Multimodal guards are per-provider and enforce only what is documented.**
  `models/providers/_media.py` gates every request carrying figures/documents — mime, image count,
  PDF pages, per-item bytes, dimensions, whole-request payload — with each adapter's `_LIMITS`
  quoting its own provider docs. Anthropic publishes the sharpest rules (>20 image blocks ⇒ 2000 px;
  10 MB/image; 32 MB request; 100-page PDFs), so its guard is the strictest; OpenAI and Google
  publish counts and payload ceilings but **no >N-image dimension rule**, so none is invented for
  them — an undocumented limit stays unenforced until a doc or a live 400 says otherwise. Residual:
  the Files API (§12) would cut payload and latency further, and Google's 20 MB inline ceiling is
  the tightest of the three, so a Gemini member in a figure-heavy loop is the most likely next
  eviction case.
- **`bash_exec` figure capture:** snapshot the worker scratch `cwd` before each `bash_exec`,
  diff for new image files after, and synthesize `FigureRef`s into `ToolResult.figures`.
- **RAG retrieval (three targets):** keep `vector_search()` stubbed; intended backend **ChromaDB**
  (decided 2026-07-06, reversing the earlier sqlite-vec lean; `Maitreya_RAG` is the reference), for
  **literature**, a **persistent-memory** store (runtime LLMs lay down useful findings, RAG-retrieved),
  and a **tool** index mirroring `TOOL_MANIFEST` (synced after every commit; tools loaded lazily by
  relevance). Embedding model is config-driven.
- **`onboard_toolkit` mini-pipeline (install-script increment 5):** the lead drafts a `ToolSpec`
  chain from a start-fresh toolkit's staged raw code + a user description, then runs the existing
  `propose_and_pr` → gate → co-lead review → `approve_edit` flow (`human_channel=cli`) to turn
  undisciplined everyday analysis code into gated, tested tools. Deferred (decided 2026-07-14) — it
  needs cross-domain collaborators to supply the raw code + validation datasets. The installer's
  start-fresh option already scaffolds the package, `ai` branch, and staging dir and prints the
  kickoff; only the pipeline entry point is unbuilt.
- **Lead-scientist group:** an idea-generation team that mines the knowledge base to propose
  new research requests, routed through the same reviewer gate.
- **Librarian IO ingest consensus:** activate `IODebateToConverge` and replace the
  `evaluate_search_results()` stub (`accepted=True`) with a real IO-lead LLM call (cap 3).
- **Restrict runtime MCP to read-only file-tree exploration:** no network, shell, or writes
  via MCP — directory listing and path/metadata reads only, via prompt + a registration
  filter at role-init. *(Largely subsumed by the §13 `PathGuard` allow-list, which now scopes
  every agent's file reads; the no-network/no-write tightening for MCP exploration remains.)*

---

## 13. Loose ends (in-code stubs, deprecations, best-effort gaps)

A **living registry** of code that is intentionally incomplete, deprecated, or best-effort — so the
gaps stay visible rather than getting rediscovered the hard way. Distinct from §12 (planned-but-
unbuilt *features*): these exist in the tree **today**. Append as new ones land; delete when closed.

- **`retrieval.vector_search` / `vector_search_paths`** — `NotImplementedError` Phase-2 stub
  (ChromaDB RAG backend deferred — §12).
- **Librarian IO search-iteration / `evaluate_search_results()`** — Phase-1 stubs; the latter
  returns `accepted=True` (IO ingest consensus deferred — §12).
- **`sandbox.merge_flow._make_path_confiner`** — **DEPRECATED, unwired.** The SDK runs
  `can_use_tool` last and skips it under `acceptEdits`/allow-rules, so it never gated reads/Bash.
  Superseded by the `_make_fs_guard_hook` `PreToolUse` hook (`sandbox.fs_guard.PathGuard`). Kept
  for reference only; do not use `can_use_tool` for confinement.
- **Filesystem jail is best-effort for shell commands.** `PathGuard` enforces the `agent_fs_allow`
  allow-list **exactly** for tools whose path it sees (`Read`/`Write`/`Edit`/`Glob`/`Grep`, and the
  workers' `read_file`/`list_dir`), but for `Bash`/`bash_exec` it only checks the absolute paths
  **named in the command** — a subprocess's implicit reads are invisible. Complete confinement is
  the OS-level jail (§12). Also: the workers' **toolkit-tool path *arguments*** are not yet
  allow-listed (toolkit tools take obs/data paths and run in the sandbox).
- **`sandbox.merge_flow._reset_direct_tree`** — best-effort: a git failure during the post-failed-
  authoring reset is logged (`coder.tree_reset.error`) and swallowed so it never masks the result;
  a residual dirty tree is then caught at the next launch's preflight (the in-flow
  `check_toolkit_branch_clean` still *raises* mid-run — graceful-degrade is a tracked TODO). It now
  resets to the **per-attempt base ref** (HEAD at `propose_and_pr` entry), so a commit the coder
  lands itself is dropped, not kept (§9.9). **A human interrupt now reaches it** (2026-07-31): the
  session is published on `RunControl` while `drive_authoring` runs, and the shielded cleanup calls
  `aclose()` itself — because that method's own `finally` sits inside the cancelled scope, where its
  awaits die before the reset. **Residual:** a *hard* kill (SIGKILL, an OOM, a host reboot) still
  skips it, leaving the coder's commit on the branch with a *clean* tree — which the launch
  `check_toolkit_branch_clean` (dirty-tree only) does not catch; the human drops it with
  `git reset --hard <base>` out of band. The reset is also bounded (30 s) on the interrupt path, so a
  wedged git yields rather than hanging the stop; a dirty tree then falls to the next launch's
  preflight and the resume-time heal.
- **Coder git-ban is best-effort (deny-rule).** The coder's Bash deny-rule
  (`disallowed_tools=["Bash(git:*)", "Bash(git)"]`, §9.9) blocks the obvious `git …`, but prefix
  matching can be slipped (pipes, `$(…)`, env prefixes, `python -c subprocess`). The robust catches are
  the **gate-entry auto-revert** (`_gated` does `reset --mixed <base_ref>` on a moved HEAD before
  scanning, so a slipped commit is normalized to uncommitted work, not a confusing reject) and the
  **base-ref failure reset** above (drops any commit on a failed attempt) — both regardless of how the
  commit was made; the deny-rule is the early, legible block that also steers the coder to `ask_lead`.
- **WhatsApp/Twilio HITL connector is DEPRECATED.** `hitl/whatsapp.py` bills per message — a poor fit
  for an agent HITL channel that may raise many clarifications in a run. It still works (warns a
  `DeprecationWarning` on construction, sends the query's Markdown body raw) but prefer
  `human_channel='slack'` (free, renders Markdown via a `markdown` block) or `'cli'` (attended
  terminal, renders via `rich`). Its env-gated live test (`tests/test_live_whatsapp.py`) stays.
- **Worker `edit_report` formatting is model-dependent (V-4, observed live).** In the V-4 two-round
  live check (`tests/test_live_report_rounds.py`), a `claude_sonnet_5` worker composing its longer
  round-2 report crammed the whole report as pseudo-XML into the `summary` argument and left
  `cited_figures` empty, so *that worker's* report carried no figures (a syntactically-valid but
  semantically-wrong `edit_report` call — the worker safety net only fires when nothing is written).
  It did **not** break V-4: the peer GPT worker cited correctly and the lead's cross-iteration
  catalog accumulation preserved the round-1 figures, so the final adjudicated report still carried
  figures from both rounds. **Mitigated:** `execute_edit_report` now scans the composed prose for
  numbered figure references (`Figure N`/`Fig. N`, `_referenced_figure_numbers`) and, when the prose
  refers to more figures than were placed, appends a **soft note** to its tool output ("…seems to
  reference Figure(s) […] but attached only N. Did you mean that? …") inviting — not requiring — the
  model to `review_report` + re-cite (not an error; the report is still saved, and both report tools
  are budget-free so the model can fix it in-loop if it chooses). Best-effort (a
  determined mis-format that also omits figure references in prose slips through); it is a source-side
  nudge at the moment of the slip, not a downstream reviewer check (which cannot see the problem — a
  misfiled report reads as complete). `tests/test_report_authoring.py::test_edit_report_warns_*`.
- **Durable resume is built through Stage 1 only; the full killed-and-resumed run is NOT LIVE
  TESTED.** The §12 checkpointer is wired (`graph/checkpoint.py`; `run_research`/`run_research_nohup`
  gain `resume=`; `AsyncSqliteSaver` at `run_dir/checkpoint.sqlite`, `thread_id=run_id`,
  `durability="sync"`; `checkpointer_available` preflight — no silent non-durable run). Verified
  deterministically incl. the real `AsyncSqliteSaver` resuming across a fresh instance (process
  boundary), but a real backend killed mid-`run_research` and resumed is unproven. **Stage 2
  (dirty-`ai`-branch self-heal) is now also built** (`merge_flow.heal_toolkit_branch_if_dirty`;
  `run_research(resume=True)` heals before the preflight; `build_authoring_session` persists the
  per-attempt base-ref to `run_dir/authoring/current_base_ref`; the dirty-tree check raises the
  distinct `ToolkitBranchUnclean`, which the mid-run authoring path degrades to an honest terminal),
  so the resume→dirty-branch→die-again loop is closed — but end-to-end (a real killed-mid-authoring
  run resumed and healed) is still unproven. **Stage 3 (HITL slash-commands) built 2026-07-22**
  (`/help`, `/status`, `/steer`, `/stop` over Telegram/Slack — commands intercepted, not treated as
  LLM replies; CLI/WhatsApp unwired, see the loose-end below). **Stage 4 (graceful cleanup) built
  2026-07-31** — and it turned out to be the same build as making `/stop` real: shielded teardown, a
  CLI/backend signal handler, and the interrupt that drives both (§12). **NOT LIVE TESTED:** a real
  `/stop` mid-authoring, whose whole point is that the branch reset runs before the process ends.
  Durable-HITL-suspend-at-`interrupt()` stays deferred (re-runs the node on resume — infeasible
  mid-`_react`).
- **HITL slash-commands are Telegram/Slack only; CLI + WhatsApp are not command-wired.** The
  `/help`,`/status`,`/steer`,`/stop` channel (`hitl/commands.py`, §12 Stage 3) needs an
  *always-listening* connector so a command lands any time. Telegram (long-poll) and Slack (socket)
  qualify and are wired (`attach_commands` + a command receive-path + `wait_reply_or_stop`). **CLI**
  (blocks on `input()` only at a prompt) and **WhatsApp** (Twilio, polls only *during* a pending
  query) have no background listener, so commands would only work while a query happens to be open —
  they don't fit cleanly and are deliberately left unwired (decision 2026-07-22). Adding them would
  need a background receive loop per connector (a bigger change); WhatsApp is deprecated anyway (§13
  above). Command *dispatch* is deterministically tested; end-to-end command behavior over a live
  channel is observational (a human must type the command) — **NOT LIVE TESTED**.
- **Context editing covers Anthropic only — the other two members have no working valve.** The
  long-loop context-pressure valve (`models/providers/anthropic.py` routes `cache_prefix` calls through
  `client.beta.messages` with a `clear_tool_uses_20250919` `context_management` edit;
  `config: context_mgmt_*`) is **live-verified 2026-07-28** and ships enabled. What stays open is
  **coverage**: it protects the lead (pure Claude) + the Claude worker member, while the GPT worker
  member relies on its large window (OpenAI's own server-side compaction 500s whenever it fires, and
  per openai-python#3075 never emits on tool-call-only turns — our exact ReAct shape) and Google has no
  equivalent (its multi-turn tool use is broken anyway, [I8]). A Tier-2 hand-rolled watermark compactor
  for those two is proposed, not built. Figure interaction is a watch item — clearing old tool results
  drops their inline figures from the model's view, but the accumulated `figures` list still carries them
  to the report; `keep_tool_uses` + a high watermark keep the common case unaffected. Any Tier-2
  compactor must also **exclude tool-search discovery blocks** (see the next entry).
- **Tool search / lazy tool loading — live-verified and enabled; three watch items remain.** With
  `config: tool_search_enabled` every `research_toolkit` tool is sent `defer_loading: true` and its
  schema stays out of the model's context until discovered through the provider's server-side search
  tool (Anthropic `tool_search_tool_bm25_20251119`; OpenAI `tool_search`, gpt-5.4+; Google has none and
  keeps every tool resident). Harness tools stay resident. **Live-verified 2026-07-28 on both providers**
  (`tests/test_live_tool_search.py`): resident input **19,074 → 3,789 tokens on gpt-5.5** and **35,208 →
  9,003 on claude-sonnet-5** against the real manifest, the model searched and called the discovered
  tool, and a three-turn probe confirmed the expanded definition lands **inside** the conversation cache
  breakpoint (cache-read at 0.1× thereafter). Watch items: (a) **activation lives in the history** — a
  discovered tool stays callable only while its `server_tool_use`/`tool_search_tool_result` blocks
  survive replay, so any future context compactor must exclude them or a tool silently de-activates
  mid-run — and it is also **why a live conversation's catalog cannot shrink under it**, the property
  that made the tombstone guard unreachable and got it deleted (2026-07-30 changelog entry).
  The same blocks are why **every request that replays a trajectory declares its tools**
  (`tool_choice="none"` on a terminal compose, 2026-08-06): a block naming an undeclared tool is a
  400, and filtering it out is what Anthropic's echo rule forbids — re-confirmed live 2026-08-06;
  (b) **provider asymmetry** — Anthropic hides a deferred tool
  entirely (hence the tool catalogue injected into the task instructions is what tells the model the
  tool exists at all), while OpenAI keeps names+descriptions resident and withholds only the parameter
  schema; (c) **Anthropic BM25 returns up to 5 references per search and expands all of them**, so a
  broad natural-language query pulls five full schemas into the tail where the injected catalogue's
  exact name would have pulled one — the resident-token win is real either way, but a query-shape
  regression costs tokens silently.

---

## Changelog

Compacted 2026-08-05: entries through 2026-07-31 are folded into themed date ranges keeping tag ids,
rulings, rejected alternatives, and NOT-LIVE-TESTED markers; the narrative detail is in git history.

- 2026-05-27 -> 2026-06-16 — **Phase 1, then Phase 2 steps 0–7: the whole author→gate→review→commit
  flow.** Phase 1: the LangGraph team end-to-end (schemas, provider registry, observability, SQLite
  storage/retrieval, sources, `prompts/`, roles + ReAct loops, consensus, capped feedback edges,
  sandbox/registry, reports, CLI, Studio, notebook); a Cyg X-1 joint fit produced JSONL + LaTeX/PDF.
  Phase 2 then built what §9 specs: runtime writes confined under `runtime_dir`;
  `ToolSpec`/`ValidationDataset`/`ProposedCheck`/`AuthoringDecision` + `AnalystReport.tool_request`/
  `status`; `merge_flow.propose_and_pr` (Sonnet Claude-SDK `coder`, isolated worktree,
  `ask_lead`/`ask_human`, bounded rework) → `gate.run_gate` (Layer-A + the `code_gate.yaml`
  no-fabrication scan; Layer-B tool tests) → `review.make_review_hook` (provider-decorrelated GPT
  co-lead) → `approve_edit` (one atomic commit, explicit-path staging, never `git add -A`;
  **commit ⇔ gate ∧ review**; the runtime never merges to master). `preflight` runs every no-LLM check
  before any model; `hitl` adds the injectable `HumanHandler` + connectors + the shared `ask_human`.
  Lead-driven authoring/resume (`decide_authoring` → `propose_and_pr(direct_branch="ai")` → registry
  reload + sandbox restart + worker rerun, capped by `tool_author_cap`). Live: `usage_example.ipynb`
  IXPE P1.

- 2026-06-18 -> 2026-07-09 — **Live-run polish, the coding contract, and two gate corrections.**
  Dispatch mechanics moved into the `ReviewVerdict` schema + manifest-quality tool descriptions; §3
  gained **write only positive actions** and then **plan-and-debate before coding** (now its first hard
  rule); worker→lead `AnalystToolRequest` split from lead→coder `ToolSpec` (dropping the OpenAI-strict
  open map, §7); analysts-author-no-code prompt policy; authoring hardened (fs jail, coder
  git-discipline). **Gate determinism** reworked to per-test JUnit outcome maps rather than a byte
  compare (§9.4). **Reviewer is the sole I/O (§9.6):** `finalize_authoring_outcome` composes one honest
  terminal report instead of spinning. Clarification channels carry one Markdown `message` + derived
  `headline`; WhatsApp/Twilio deprecated. Robustness (additive): figure bytes reach models at the
  multimodal boundary; `\href` display escaping; gate Layer-B requires ≥1 PASSED; malformed figures
  dropped-with-note; bare no-fab descriptors → soft findings; **detached runs** (`run_research_nohup`)
  survive a killed kernel. **[A8]** the lead authors the whole ordered `tool_specs` chain and reruns
  workers once. **[A12]** the gate derives its target `*_utils` packages from the touched diff, not the
  lead's advisory `ToolSpec.target_domain` — removing a doomed reject-forever loop. `open_paper` refuses
  a PDF over 8 MB (an oversized paper rides by value in every later call and blew Anthropic's 413 cap).
  **RAG direction** set to ChromaDB for three targets — deferred (§12).

- 2026-07-13 -> 2026-07-16 — **[V-4] figure-cited reports, the domain-agnostic installer, and three
  robustness fixes.** **[V-4]:** every analyst report is a per-round JSONL the agent refines through
  `edit_report`/`review_report`; figures are **cited** by a `{source, index}` handle from an append-only
  per-source catalog and resolved in order to `report.figures` — **replacing figure-dedup** — so no
  figure is last-round-only; new `FigureCitation` + `FiguredReport`; `edit_report` emits a soft "did you
  mean that?" note when the prose references more figures than it placed (§13). **Installer increments
  0–4:** machine-local config gitignored with tracked `*.example`; new `repl_init_py` + `analyze_probe`
  keys (the REPL boot and the bash probe stop being hardcoded XSPEC); `install.sh` + `python -m
  auto_research.setup` (interactive `questionary`, headless `--answers`); typed domain-dependency
  buckets **A–E** (boot commands · package install · python preload · data roots · license pointer);
  toolkit scan → point/clone/start-fresh scaffolding a §9.2 skeleton + `ai` branch. Increment 5
  (`onboard_toolkit`) deferred (§12); build-from-source out of scope; **P9** made 8 cluster-assuming
  tests hermetic-or-skipping. **Transient-provider retry** (`models/retry.call_with_transient_retry` on
  every `ModelHandle.call`; "Internet stability" config keys) after two runs died on Anthropic 529s.
  **Gate scan:** an emptied `code_gate.yaml` section is legal, and all RNG patterns moved hard-fail →
  co-lead-judged finding (human policy: Monte-Carlo tools need randomness inside them). **Detached-run
  hardening:** a zombie counts as dead for liveness; **double-fork spawn** so the backend is reparented
  to init at spawn (a graceful VSCode/Jupyter logout walks `/proc` parent→child links, which
  `start_new_session` does not sever); httpx/httpcore pinned to WARNING so Telegram's URL-embedded bot
  token stops reaching backend logs.

- 2026-07-20 -> 2026-07-22 — **Parse robustness, [A11] recovery, cost visibility, and the context/durability
  bundle Stages 1–3.** A GPT co-lead reply with trailing text crashed a live run on a bare `json.loads`
  → new `providers/_json.loads_first_json_object` on the OpenAI + Google receive paths (send-side strict
  sanitizers stay: the SDK's own conversion emits keys OpenAI strict mode 400-rejects). Per §2, the
  **catalog read moved into `research_toolkit`** (`catalog_utils.data_catalog` over
  `obsdb_catalog.neutral.read`); `observations.py` shrank to obs-db location + health preflight.
  **[A11]:** the registry reload / sandbox restart / worker rerun now fires whenever **≥1 tool committed**
  (was whole-chain), and a non-commit loops back to recover, so the §9.6 honest terminal fires only at
  credit exhaustion with nothing committed (new `analyst.tool.chain_resume`). **[I5] cost:** litellm-first,
  config-fallback pricing; `ModelUsage` gains cache-read/cache-write/reasoning classes (heavily-cached
  ReAct input had been recorded as **zero**); LangSmith runs annotated with `usage_metadata` + `cost_usd`;
  a `python -m auto_research.observability.usage` rollup. **Context editing** built as Anthropic
  provider-native `clear_tool_uses_20250919` behind `cache_prefix` (§1 use-the-wheel; the field converged
  on token-watermark management and moved it server-side), shipped disabled. **Durable resume Stages 1–3:**
  `graph/checkpoint.py` (`AsyncSqliteSaver`, `thread_id=run_id`, `durability="sync"`) + `resume=` + a
  `checkpointer_available` preflight (no silent non-durable run); `heal_toolkit_branch_if_dirty` resets a
  hard-killed authoring branch to the recorded per-attempt base ref, with the launch check raising the
  distinct `ToolkitBranchUnclean` that the mid-run path degrades to an honest terminal; HITL
  slash-commands `/help /status /steer /stop` over `RunControl` + `hitl/commands.py`, intercepted rather
  than treated as LLM replies — **Telegram/Slack only**, since CLI/WhatsApp have no background listener.
  Durable-HITL-suspend-at-`interrupt()` stays deferred (it re-runs the node on resume — infeasible
  mid-`_react`).

- 2026-07-27 -> 2026-07-31 — **The tool-context diet (verified live), the observability rewiring, and
  `/stop` becoming Ctrl-C.** **Tool context:** every `TOOL_MANIFEST` description became a one-line
  keyword-dense brief with the prose in `args_schema`; `ToolkitRegistry.load()` stopped overwriting that
  description with the backing function's `__doc__` (a Phase-1 convenience Phase 2 inverted); and
  provider-native **tool search / `defer_loading`** landed — the provider runs the search (BM25, zero
  infra) while we own the archive (the manifest) and the menu (the injected catalogue). Resident tool
  context 18,999 → 2,382 tok. **Live-verified 2026-07-28 and enabled** together with context editing:
  the Anthropic server-tool declaration needs the **undated** `name` (a 400 no deterministic test could
  see), resident input fell 19,074 → 3,789 on `gpt-5.5` and 35,208 → 9,003 on `claude-sonnet-5`, and a
  three-turn probe proved an expanded definition lands **inside** the conversation cache breakpoint.
  Anthropic BM25 returns up to 5 references per query and expands all of them (§13). The flip also
  exposed a general hazard: two tests passed only while the flag was false — **re-run the full suite
  after any config-default flip**. **Observability:** preflight records into the run ledger; the co-lead's
  `monitor` kwarg reached `make_review_hook` (its actions had been invisible behind its verdict);
  accumulated cost is stamped on **every** monitor line with one `cost.call` event per call, the separate
  run-blind `cost_ledger.jsonl` is deleted, and the coder's Agent-SDK spend is folded in as per-turn
  deltas; `monitor/summary.py` derives `run.summary` from the event stream (fires on the failure path
  too); the ledger and `backend.log` are append-only, and **only `overwrite=True` is destructive**.
  **Human control:** `_react` polls between batched calls; `/steer` takes a numbered roster id; commands
  record as `hitl.command`/`.reply`; events actor by indexed `agent_key`; `/status`'s prompt moved to
  `prompts/status_monitor.md` (§4). **The discovery-replay run-killer:** both adapters took
  `include_discovery = tools is not None`, since a request declaring no tools has nothing for a reference
  to point at. **The tombstone guard was deleted** as a misdiagnosis — a live conversation's tool catalog
  cannot shrink under it (four independent topology grounds, kept in the module docstring). **`/stop` is
  Ctrl-C:** an acknowledged cooperative stop was still followed by ~14 model calls and *manufactured*
  revision rounds by truncating evidence, so `RunControl.interrupt()` now cancels the run task and a
  shielded three-phase cleanup runs — kill every REPL **and its descendants** (walked via
  `/proc/<pid>/task/*/children`, not `killpg`, which shares the backend's group), then the authoring-branch
  reset to the attempt's base ref, then close/record — raising `UserInterruptionError` with **no model
  call and no report** on that path; Ctrl-C/SIGTERM take the same path on the CLI and the detached backend
  (exit 130, `result.json` status `interrupted`); five cooperative-stop branches deleted; `/stop` +
  `resume=True` is pause-and-continue. Rulings: stop is **force by default with no flag vocabulary**
  (escalations became buttons on the steer ack, since a chat platform shows commands and buttons but not
  flags), and [F6]'s terminal-compose guard was **dropped rather than built** (a runtime-synthesized report
  sits closer to fabrication than the crash it prevents). **`/status` investigates** rather than reading a
  digest: `MonitorToolProvider` (`brief_history` / `ledger_event` / `ledger_grep`) driven by a
  `StatusMonitorAgent` ReAct role, read-only + bounded + re-read per call (an agent reading its own run log
  is the one place a harness can feed itself without limit — cf. openai/codex#27131); both compaction paths
  deleted with **no fallback**, `status_monitor` joining `REQUIRED_ROLES`. **NOT LIVE TESTED:** a real
  `/stop` mid-authoring, where the branch reset is the whole point.

- 2026-08-03 — **Two data-honesty fixes: one bad ADS record, and a GPG-encrypted download.** ADS
  occasionally serves a **relative** URL where `FullTextLink.url` is an `HttpUrl`, and the
  `ValidationError` escaped `search_literature` entirely — `nasa_ads` returned nothing for that query
  while arxiv and web answered. The record is now skipped and every well-formed link beside it kept
  (a bad link costs one link, never the source). **[H9]** (research_toolkit scope): HEASARC serves an
  observation still inside its proprietary period **GPG-encrypted**, and four layers independently
  defined "present" as *a filename matching a product regex* — so a complete download read as empty
  ground, the agent's catalog query returned **no line at all**, and re-acquisition was the correct
  inference from what it was told (live: SS 433 `04004001`, ~25 min re-fetched to no effect). Fixed with
  **one shared rule** (`neutral.split_lock`/`is_locked`/`LOCK_SUFFIX`) in the module every layer already
  imports, patterns matching wrapper-stripped, and a readable copy ranked above an encrypted twin.
  Encryption state is **derived from the recorded path, never stored** (it changes out of band, so a
  decryption surfaces as ordinary drift for `repair`). Human rulings: a locked product keeps its **honest**
  level and is marked `LOCKED, NOT USABLE` on its catalog line (the mark, not a demoted level, is what
  callers read); a repeat request consults the archive's `public_date` (`heasarc.availability`, one
  metadata row) instead of re-transferring; only `.gpg` is recognised, an unknown suffix stays unknown.
  Folded in **O4**: the `data_catalog` brief said "mission" five times against a schema with no such
  argument. The human runs `obsdb_catalog repair_all` once, out of band.

- 2026-08-03 -> 2026-08-05 — **The replay seam: three run-killers, then the unification that removes the
  class.** With tool search on, part of the transcript is authored by the *provider*, and for the first
  time we hand back items we did not build. Three live 400s followed in one week, each the same mistake
  wearing a new face — we re-type a provider's record into a smaller shape of our own, so anything we did
  not think to keep is destroyed at ingest: `Unknown parameter: 'input[2].created_by'` (the Responses API
  **accepts a smaller vocabulary than it emits**; openai-python **#3008**/**#2323**/**#2670** ask for an
  `.as_input()` helper the SDK does not ship), `Missing namespace for function_call` (a discovered tool is
  addressed by a namespace we had rebuilt away), and `tool_search_call … without its required 'reasoning'
  item`. Interim fixes carried the provider's record whole (`ToolCall.provider_fields`; an inverted
  `_UNCARRIED_ITEM_TYPES` default so an unanticipated item type rides along; `_as_input_item` filtering by
  **name and by value** once at capture, which is what protects prompt caching — the hazard is applying a
  different *rule* on different turns, not one that reads values). **Then the root cause, 2026-08-05:**
  `ModelHandle.call` treated `schema` and `tools` as mutually exclusive — **self-imposed**, asserted in
  four places, and a live probe showed all three providers accept both in one request. That exclusivity
  forced the terminal compose to declare no tools, which *is* the request shape behind the 2026-07-29
  `Tool reference … not found` killer and its `include_discovery` amputation. It is gone, and **the
  response type now IS the assistant turn:** `AssistantToolUse` deleted, `ConversationMessage =
  ModelMessage | ModelResponse | ToolResultMessage`, and `_react` appends the response it received instead
  of hand-copying three fields into a twin — the copy that had silently dropped tool-search blocks until
  2026-07-27 and `thinking` until now. `provider_blocks` → **`provider_parts`, allowlist deleted** (we were
  reading 3 of Anthropic's 16 block types; the set is never complete). **Replay follows each provider's own
  documentation** — Anthropic *"echo the assistant message exactly as received"*, Google stateless mode
  *"MUST always resend all `thought` blocks exactly as they were received"*, OpenAI's pass-back guidance —
  so Anthropic and Google echo the whole array while OpenAI keeps its capture-time input projection, whose
  field allowlist is now **derived from the SDK's own `ResponseInputItemParam` union** (27 types). Google
  also stops using the `.text`/`.function_calls` accessors (they flatten a mixed turn and hide
  `thought_signature`) and captures the signature onto `provider_fields` — the **[I8]** mechanism, not yet
  live-verified. **Two rules had been wrong because they were inferred from 400s rather than read from the
  docs** (a "thinking first" reassembly order and a "drop only tool-referencing blocks" filter — the docs
  forbid rebuilding or filtering at all); a request declaring no tools therefore carries no provider parts
  and rebuilds from the interpreted views. Live-verified 2026-08-05 on both providers: second-turn replay
  of a full Anthropic turn (`thinking → server_tool_use → tool_search_tool_result → thinking → tool_use`)
  and a tool-less structured compose over the same trajectory. The cheap live test that caught two of the
  three 400s for cents — `test_live_tool_search.py::test_live_second_turn_after_discovery`, after the first
  cost ~$10 of live analysis — now covers the terminal compose too. Suite 649 → 665. **Deferred, recorded:**
  `stop_reason` is still not modelled (`_react` infers "done" from the absence of tool calls, so
  `max_tokens`/`refusal`/`pause_turn` all read as a clean finish); tool `input_schema`s get no dialect
  normalization; the tool_use `caller` field is dropped on a synthesized-turn rebuild; letting the terminal
  compose declare its tools would put it back on the documented echo path. **Also observed live on those
  runs** (all first-time evidence): durable resume re-entered the analysis node with no literature repeat,
  the ledger stayed append-only across the kill, cost seeded from the ledger, locked-ObsID handling worked
  end to end on both providers, and **Anthropic context editing fired in a real analysis** (two edits, ~26k
  tokens cleared each) — though its collision with tool search (§13 watch item (a)) was not exercised, both
  edits landing while the worker was already writing up. Open from the same runs: **[F3]** the filesystem
  guard denies `2>/dev/null` and trains agents into `cd <dir> && <relative>` form the command check cannot
  inspect at all.

- 2026-08-06 — **The unification finished, after it let a fourth run-killer through.** The
  2026-08-05 pass deleted the `schema`/`tools` *assertions* but not the branching: both adapters
  still ran `if schema is not None: … return` **before** `if tools is not None`, so passing both
  silently dropped the tools (only Google merged them) — and the test named for that property
  asserted `parsed` came back rather than that the tools reached the request, so it passed against
  an adapter honouring neither. The terminal compose therefore still could not declare tools, the
  tool-less request still dropped every carried provider part, and the `function_call` items a few
  lines below still replayed unconditionally, orphaning the reasoning items they require. Live run
  `8c75990e` (Q21) died on exactly that at **$29.50**, one step before its first commit — gate
  passed, GPT co-lead composing its verdict — with `b-run-q21-0803c`'s 400 verbatim. **The fix is
  the one already recorded as deferred:** each adapter builds ONE request (schema and tools
  independent) and runs ONE extraction; `tool_choice` gains **`"none"`** (`"none"` / `{"type":
  "none"}` / `mode="NONE"`, each read from that provider's own docs), and every request that
  replays a trajectory declares its tools — so `include_provider_parts` is **deleted** rather than
  narrowed. The drop rule had been inherited from the 2026-07-29 discovery-trim, where only a
  *tool-referencing* item was ever the problem; applied to the whole part list it created the
  orphan. `StatusMonitorAgent`'s cap-exhaustion fallback had the same shape (a plausible cause of
  the open *"`/status` returns silence"* defect). **The probe ran before the code, for cents**
  (`tmp_scripts/probe_tool_choice_none.py`): Anthropic's `messages.parse` accepts
  `output_format` + `tools` + `tool_choice` + `thinking` in one call — the risk that could have
  forced a redesign — and OpenAI accepts `tools` + `tool_choice="none"` + `text.format`. The new
  live test covers what shipped the bug: an **ordinary function-call** trajectory, where every
  prior live test had exercised tool-search discovery instead; re-running the file also
  re-confirmed that a tool-less compose over a discovery trajectory still 400s, which is why no
  caller may use that shape. Suite 665 → 670, live 7/7 both providers. **Lesson recorded:** a
  removed assertion is not a removed behaviour, and a test that asserts the *result* of a call
  cannot see that a parameter was ignored — assert on the request. **Immediate sequel, same day:**
  the relaunch died 80 min / $5.36 in on `NameError: name 'tools' is not defined` — two of the
  seven compose sites were patched by pattern with `tools=tools` where the local is `self.tools`,
  in `decide_authoring` and `clarify_for_author`, branches no deterministic test exercises. A
  `NameError` is invisible until its branch runs, and the branches that only run deep inside a
  live run are the expensive ones to find that way. Fixed, and the class is now closed by
  `tests/test_no_undefined_names.py` — a scope-aware AST guard (module/function/class/closure/
  comprehension/lambda, annotations skipped under PEP 563) that fails on any name read out of
  scope, parametrized per module. A naive version is useless here (183 false positives from
  closures alone); pyflakes would do it but is not in the env, and a test may not depend on a
  linter that is not installed. Suite 670 → 760. **And the altitude mistake behind it:** the live
  tests call `provider.call` directly, so they never execute `roles.py` — two layers were changed
  and only one was live-exercised. Two guards close that: `tests/test_compose_declares_loop_tools.py`
  asserts, per call site, that the compose declares **the same** `tools` expression its own
  `_react` used (catching a correctly-spelled but wrong name, which the undefined-name guard
  cannot), and `tests/test_live_schema_matrix.py` sends **all nine** structured schemas we can
  emit — `ReviewVerdict`, `AnalystReport`, `AuthoringDecision`, `ToolReviewVerdict`,
  `_ClarifyReply`, `SearchRequest`, `MemoryRecord`, `ResearchReport`, `LatexReport` — to **both**
  providers with tools declared, validating each response against its model. Every earlier live
  check of the structured path had used a two-field toy schema, so "big real schema alongside a
  tools array" had never been sent to any provider; **18/18 pass** (~112 s). Suite 760 → 768.
  **Then the human asked the two questions the tests still dodged — attach real history, and check
  whether one session mixes schemas — and the first found a bug the same change had introduced.**
  The lead's `adjudicate` attaches a report workspace (adding `edit_report`/`review_report`) for
  its loop and detaches it in a `finally`; the safety-net compose ran *after* that, so it declared
  `self.tools` **minus** the two tools whose `tool_use` blocks the replayed trajectory can name —
  the 2026-07-29 `Tool reference … not found` shape, reintroduced in a new place. Source-text
  equality could not see it (both sites say `self.tools`; the *object* mutates between them), so
  the compose moved inside the `try` and a runtime guard joined the static one
  (`test_react_tools.py::test_compose_declares_the_tools_the_loop_ACTUALLY_had`, a provider whose
  surface shrinks). The worker's `analyze` was safe by construction — its provider is built with
  the workspace and never mutated. **On schema mixing: it does not happen** — every role method
  emits exactly one schema, its tool-less fallback included, and each action builds a fresh
  `[system, payload]` seed, so no trajectory is ever replayed under a different schema; pinned by
  `test_one_conversation_carries_exactly_one_schema`. New live test
  `test_live_compose_with_real_schema_after_a_real_trajectory` sends two real tool-calling turns
  then the `none` compose with `AnalystReport`/`ToolReviewVerdict` on both providers — and its log
  is the confirmation the original probe missed: the OpenAI turn carried a **`reasoning`** item,
  replayed beside its `function_call` with tools declared, and was accepted (the probe's repro leg
  had been inconclusive because that turn emitted none). Suite 768 → 770; live 4/4.

- 2026-08-07 — **The multimodal boundary had no guard at all; now each adapter gates its own.**
  Run `8c75990e` reached its **fifth commit** — five tools authored, every one gate-passed and
  co-lead-approved first try — then died at **$116.10** on the census re-run with `400 … image
  dimensions exceed max allowed size for many-image requests: 2000 pixels`. The rule is Anthropic's:
  past **20 image blocks** a request's per-image dimension cap tightens to 2000 px, and a worker's
  own newly-authored plotter returned 3 PNGs per call that replay inline on every later request, so
  the count crossed 20 with a 2677x1554 figure aboard. **The audit was the finding:** we base64'd
  whatever we were handed on all three providers, checking neither count, dimensions, bytes, total
  payload, nor mime — the one guard in the system was `_OPEN_PAPER_MAX_MB = 8` in the *tool* layer,
  which could see neither the provider nor the rest of the request, refused papers every provider
  would have taken, and never saw a figure. **The gate belongs in the adapter** because we are
  stateless: it holds the whole conversation plus this call's attachments before sending, which is
  exactly the set a volume limit is defined over. New `providers/_media.py` (walk → plan → resize →
  page-count); each adapter declares `_LIMITS` with every value quoted from its own docs, because
  they differ enough that a shared cap would be wrong for all three (Google's **20 MB inline** total
  is the tightest ceiling; Anthropic's >20-block rule the sharpest edge). An over-limit image is
  **resized**, a document **withheld**, and either way a model-visible note names the file and the
  reason — silent absence is what invites invention. A resize touches only the transmitted copy, so
  `FigureRef.sha256` still identifies the artifact a human reproduces. **Nothing downscales unless a
  limit would otherwise be breached** — the human caught the framing error in the first proposal:
  providers already resize server-side and bill the smaller version, so client-side downscaling
  saves **no tokens**, only payload. Also closes the mime half of **[V-3]** (open since July) and
  deletes `_OPEN_PAPER_MAX_MB`. **Files API deferred** (§12) as a cost/latency change that would not
  have prevented this: per Anthropic's docs a `file_id` image is still an image block, still counted
  against 20, still measured against 2000 px. New deps `pillow` (previously undeclared — transitive
  via matplotlib) and `pypdf`. Suite 770 → 780; live proof is
  `test_live_many_image_request_that_used_to_400`, which sends the exact 21-image shape that died
  and gets back *"I received 21 panels."*
