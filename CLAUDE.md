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
- **Human-steer channel:** a way for a human end/abort/redirect steer to **bind the whole run**,
  not just the role that happened to ask. Today the HITL handler is *blocking/reply-only*
  (`resolve_human` only listens while an agent is awaiting a query), so a human "end with failure"
  reply to one role (e.g. a worker) does **not** stop another (e.g. the analyst lead independently
  re-deciding to author). A *reply-borne* steer is buildable now (capture an abort from any reply
  at the single `resolve_human` chokepoint → a shared `RunControl` flag → the lead's authoring loop
  and graph nodes consult it → honest §9.6 terminal); a true *anytime* interjection (steer while no
  query is pending) depends on the durable suspend/resume above. **Graceful-shutdown is deferred
  as one piece of this bundle (decided 2026-06-29):** rather than bolt a Ctrl-C/cleanup mechanism
  onto each entry context separately, the SIGINT handler, the consolidated cleanup routine, the
  `asyncio.shield`/reordering that protects the authoring-branch reset, and the OS-level
  child-process kill are **co-designed together** with durable suspend/resume + this human-steer
  channel, so **one** architecture serves all three entry contexts. The contexts differ enough that
  a single naive handler does not fit: a global `KeyboardInterrupt`→cleanup handler works on the
  **CLI** (it owns the process) but is wrong as a library (a runtime must not seize the host's
  signal handling; off the main thread `signal.signal` raises and Ctrl-C never reaches the run) and
  does **not** fire in a **notebook** (ipykernel keeps the interrupt and often never cancels the
  awaited run — the observed 2026-06-26 failure). The portable spine is *idempotent cleanup run from
  a shielded `finally` on cancellation* (CLI/library), plus a *cooperative "stop please" flag* the
  run polls at safe points (the notebook story, = the reply-borne steer above) — built once, later.
  **Interim stop = hard kill of the kernel's whole process tree/group**, not just its PID: sandbox
  subprocesses share the kernel's process group (no `start_new_session`) so a terminal Ctrl-C or a
  process-group kill reaches them, but `kill <kernel_pid>` alone orphans them (no `PR_SET_PDEATHSIG`
  auto-reap). A hard kill skips the `finally` reset, so a dirty authoring branch may survive — caught
  next launch by `check_toolkit_branch_clean`. (§5.5's `plan_and_debate` already gives the human a
  veto checkpoint before *non-trivial* authoring.)
- **OS-level sandbox jailing:** read-only bind-mounts / seccomp / egress control beyond the
  relocated-scratch subprocess sandbox. *(Partly mitigated: every runtime agent's file access is
  now confined to the `config/orchestration.yaml: agent_fs_allow` allow-list via
  `sandbox.fs_guard.PathGuard` — a `PreToolUse` hook for the coder, provider checks for the
  reviewer/lead/workers — so `auto_research` and governance files are denied. The OS jail of that
  same allow-list is what remains: it would also gate a shell subprocess's **implicit** reads,
  which the command-string `Bash` check cannot see. See §13.)*
- **`bash_exec` figure capture:** snapshot the worker scratch `cwd` before each `bash_exec`,
  diff for new image files after, and synthesize `FigureRef`s into `ToolResult.figures`.
- **Vector retrieval:** keep `vector_search()` stubbed; the embedding column/table is
  reserved; intended backend **sqlite-vec** (no new infra); embedding model is config-driven.
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
  (sqlite-vec backend deferred — §12).
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
  lands itself is dropped, not kept (§9.9). **Residual (deferred — §12 graceful-shutdown bundle):** a
  run killed mid-attempt can skip this `finally`, leaving the coder's commit on the branch with a
  *clean* tree — which the launch `check_toolkit_branch_clean` (dirty-tree only) does not catch; the
  human drops it with `git reset --hard <base>` out of band. The accepted interim is a hard kill of
  the kernel's whole process tree/group (a graceful SIGINT/cleanup path is co-designed later with
  durable suspend/resume + human-steer, §12 — not bolted on per entry context now).
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

---

## Changelog

- 2026-05-27 -> 2026-06-12 — **Phase 1 built, hardened, verified live.** Full LangGraph team
  end-to-end (schemas, provider registry, observability, SQLite storage/retrieval, sources,
  prompts, agent roles, ReAct loops, consensus, feedback edges, sandbox/registry, reports, CLI,
  Studio, notebook); a Cyg X-1 joint-fit run produced JSONL + LaTeX/PDF. Phase 2's trust-boundary
  spec written here (uniform `research_toolkit` layout, no-fabrication, deterministic gate, actor
  flow, runtime git discipline, build order).
- 2026-06-12 -> 2026-06-16 — **Phase 2 steps 0–7: the full author→gate→review→commit flow.**
  Runtime writes confined under `runtime_dir`. Protocol: `ToolSpec`/`ValidationDataset`/
  `ProposedCheck`/`AuthoringDecision` + `AnalystReport.tool_request`/`status`. Flow:
  `merge_flow.propose_and_pr` (Sonnet Claude-SDK `coder`, isolated worktree, `ask_lead`/`ask_human`,
  bounded rework) → `gate.run_gate` (Layer-A structural/provenance/determinism + `code_gate.yaml`
  no-fabrication scan; Layer-B tool tests) → `review.make_review_hook` (provider-decorrelated GPT
  co-lead) → `approve_edit` (one atomic commit, explicit-path staging, never `git add -A`; commit ⇔
  gate ∧ review; runtime never merges to master). `preflight` runs every no-LLM check before any
  model; the `hitl` package adds the injectable `HumanHandler`/`resolve_human` + connectors + the
  shared `ask_human`. Lead-driven authoring/resume (`decide_authoring` → consensus issues
  `propose_and_pr(direct_branch="ai")` → registry reload + sandbox restart + workers rerun, capped
  by `tool_author_cap`). Live: `notebooks/usage_example.ipynb` IXPE P1.
- 2026-06-18 -> 2026-06-26 — **Live-run polish + hardening.** Dispatch mechanics moved into the
  `ReviewVerdict` schema + manifest-quality `react.py` tool descriptions (reviewer stops pausing for
  routing/paths); §3 **write only positive actions** (no-fabrication ban excepted); worker→lead
  `AnalystToolRequest` split from lead→coder `ToolSpec` + `ProposedCheck.params` dropped (OpenAI
  strict open-map 400, §7); **analysts-author-no-code** prompt policy (analysis via tools, `bash`
  read-only, missing capability → `tool_request`). Authoring hardened across fix-lists (timeouts,
  monitor legibility, `fs_guard.PathGuard` `PreToolUse` jail, coder path-hints, coder git-discipline:
  deny-rule + per-attempt base-ref reset + gate-entry auto-revert). **Gate determinism reworked
  (§9.4):** per-test JUnit outcome maps, not a byte-compare (`_strip_volatile` removed) — the
  false-positive that exhausted the rework cap on a correct tool; reproducibility is now a
  science-tool-only LLM-planned `test_repro…` Layer-B check. **Reviewer is the sole I/O (§9.6):**
  `analysis → review_analysis` unconditional, the lead's `finalize_authoring_outcome` composes one
  honest terminal report instead of spinning, and a finalize always documents the outcome. Non-live
  suite green.
- 2026-06-29 — **Clarification channels carry one Markdown `message`; rendered per channel
  ("use the wheel", §1; handoff #1).** `ask_human`, the lead's `plan_and_debate`, and the coder↔lead
  `ask_lead`/`clarify_for_author` carry free-form NL, so `HumanQuery` drops `question`+`context`
  (and `plan`) for one Markdown `message` + a derived `headline`; connectors *render* it — **CLI**
  via `rich`, **Slack** via the dedicated `markdown` block (12000-char, chunked, replacing the
  2900-char section `_truncate`). **WhatsApp/Twilio deprecated** (bills per message). The boundary
  held: only the free-text carrier moved to Markdown; the **typed protocol** (`ReviewVerdict`,
  `AnalystReport`, `ToolSpec`, `FigureRef`, … — code branches on fields, §7) stays Pydantic.
  Graceful SIGINT/cleanup shutdown (handoff #5) **deferred** into the §12 human-steer + durable
  suspend/resume bundle (one design for CLI/notebook/library; interim = hard-kill the kernel's
  process tree). Coverage: `tests/test_{hitl,react_tools,merge_flow}.py`; suite green (365).
- 2026-07-06 — **§3 coding contract gains "plan and debate before coding."** The build-time
  agent presents a plan and debates it to agreement before landing edits, except where the
  human has instructed/permitted a direct proceed; investigating/reading to form the plan is
  always allowed. Added as the first hard rule (it gates the whole workflow).
