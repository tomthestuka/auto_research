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

Providers: **OpenAI, Anthropic, Google, DeepSeek, Z.AI, Alibaba Model Studio.** The last
three joined the chat set on 2026-08-24 over the OpenAI **Responses** dialect they already
publish, so a capability-compatible role may be bound to any of the six. Reaching a vendor is not
the same as being eligible for a role: a member's adapter declares what that vendor's
documentation states and a live probe confirms. Z.AI and Alibaba reach schemas through
function-tool arguments because their native strict-output path is unsuitable here.

**Vision binds the roles that are actually sent figures** (figures, [§7](#7-schemas--protocols)) —
the **reviewer**, the **analyst lead** and the **analyst worker**, and those only. Audited
2026-08-25 against the call sites rather than the config: the reviewer is passed the report's
figures, the lead the accumulated per-worker figure catalog, the worker its peers' figures and its
own tools'. The **analyst co-lead is not**, and by construction — `review_tool` passes no images
and its tool set is read-file/list-dir/web-search with the knowledge-base tools switched off, so a
code reviewer never receives a plot. The librarian needs documents, not vision; the status monitor
neither; the `coder`'s `vision` flag is dead config, since an author is a harness subprocess and
never a `ModelHandle`. The flag is a **declaration, not a guard**: it gates the call-time `images=`
channel only, so figures arriving on a tool result are unchecked ([V-2], §13).

**DeepSeek is treated as text-only by policy, not by vendor limitation** (ruling 2026-08-25). The
vendor documents `deepseek-v4-flash-vision-exp` with a full limit set and names the Responses API
among the endpoints accepting it; the project declines to depend on a multimodal path its vendor
labels experimental, and revisits when a non-experimental one ships. `deepseek-v4-pro` **is**
accepted by the Responses endpoint and needs no new wiring. Alibaba's current Qwen Max page lists
image/text/video input, so its vision eligibility follows that documented model capability.

**Phase 2 — authoring/review providers.** Only **Claude and GPT** are reliable enough to
author and review tools. The tool **author and reviewer must be different providers**
(e.g. author Claude / reviewer GPT) so a merge candidate gets two *independent* checks,
not one model checking itself ([§9.3](#93-actors-and-the-propose--author--review-flow)).
Gemini may back cheaper, non-gating roles but stays off the critical author/review path.
The **coding/author agent is Sonnet-tier** (medium), spawned by the Opus analyst lead. The
`coder` member names **both a model and the author harness that drives it** (`harness:
claude_sdk | codex`), because a harness speaks one wire dialect and only some vendors publish an
endpoint in it; the legal pairings and their documented endpoints are a table checked at launch
(`sandbox/coder/harness.py`). The **decorrelation rule reads the author's actual vendor**, so a
non-Anthropic author simply requires a co-lead that is not that vendor.
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
`models` provider abstraction (OpenAI/Anthropic/Google/DeepSeek/Z.AI/Alibaba) resolving role→model groups from
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
   is the **harness named on the `coder` role** — the Claude Agent SDK, or the Codex app-server —
   seeded with the `ToolSpec`. What the flow asks of either is only *bring the author up, send it
   one instruction, take it down*, so the gate, the review and the commit are defined over the
   working tree rather than over the process that edited it. The
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
- The **coder never issues git** — it is the runtime's job, so `prompts/coder.md` tells it to leave
  git to the runtime and `ask_lead` instead of working around it, and each harness adds what it can:
  the Claude SDK author carries a Bash deny-rule (`disallowed_tools=["Bash(git:*)", "Bash(git)"]` on
  its own options), while the Codex author runs under a sandbox in which `.git` is read-only even
  inside a writable root, so committing is refused by the OS rather than by a rule. A deny-rule is
  best-effort (prefix-matched; pipes/subshells can slip), so the gate-entry check and reset below
  remain the robust backstops on either harness.
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
- **OS-level sandbox jailing — BUILT 2026-08-20, shipped OFF** (`config: agent_os_jail`;
  `sandbox/jail.py`). `bwrap` unprivileged turns `agent_fs_allow` into a mount plan for all four
  process kinds that touch a disk (both author harnesses via their binary-path options, the analyst
  REPL, the gate's Layer-B pytest), with `agent_fs_readonly` naming the read-only roots and the
  gate's tests running **network-denied** (§9.4: a mergeability verdict must depend on the data on
  disk). What remains of this item is **egress control for the other three** and turning the flag
  on by default once a live authoring attempt has run under it. Residual detail: *(Partly mitigated: every runtime agent's file access is
  now confined to the `config/orchestration.yaml: agent_fs_allow` allow-list via
  `sandbox.fs_guard.PathGuard` — a `PreToolUse` hook for the coder, provider checks for the
  reviewer/lead/workers — so `auto_research` and governance files are denied. The OS jail of that
  same allow-list is what remains: it would also gate a shell subprocess's **implicit** reads,
  which the command-string `Bash` check cannot see. See §13.)* **Half of it arrived with the Codex
  harness (2026-08-19):** that harness enforces the same allow-list as its sandbox's writable
  roots, i.e. by the OS, so **writes** are jailed there — while **reads** get worse, since Codex
  grants full-disk read access by design and only records a crossing. Feasibility of the full jail
  was measured the same day on this host: Landlock is active, unprivileged user namespaces are
  enabled, `bwrap` is present, and PyXspec plus the toolkit import inside a jail that binds only
  the allow-list while denying `auto_research` and `~/.claude` — which also names the one conflict,
  since the Claude author authenticates from `~/.claude/.credentials.json`.
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
  allow-listed (toolkit tools take obs/data paths and run in the sandbox). **Per-harness, for the
  coder:** the Claude author is gated by that hook; the **Codex author's reads are not gated at
  all** (full-disk read access is that sandbox's documented design, and a command the sandbox
  permits is not even offered to the client's approval handler — measured 2026-08-19), so a
  crossing is only *recorded*, as `coder.read_outside_allowlist`. Its **writes**, conversely, are
  jailed by the OS against the same allow-list. **Measured, not hypothetical** (2026-08-19): a
  DeepSeek coder on that harness read `sandbox/coder/codex_sdk.py` (the backend driving it),
  `sandbox/DOCS.md`, `tests/test_live_coder_matrix.py` (the test harnessing it), its fixture, the
  installed Codex SDK sources and `~/.codex/config.toml` — 36 recorded crossings in one session,
  none of them asked for. The §12 `bwrap` jail measured feasible the same day denies exactly these
  while the science stack still imports.
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
- **The Qwen coder pairing is NOT LIVE TESTED.** `dashscope` + `codex` is configured against the
  endpoint Alibaba Model Studio documents for Codex, and nothing has ever run through it: on
  2026-08-19 every attempt returned HTTP 400 `AccessDenied.Unpurchased` across three models and all
  three documented hosts (the pay-as-you-go host recognised the key; the CN and token-plan hosts
  answered `InvalidApiKey`), i.e. right wiring, unentitled account — QwenCloud gates pay-as-you-go
  on a complete billing profile, not on buying credits. Recorded in
  `sandbox/coder/harness.py: UNVERIFIED_PAIRINGS`, warned at launch by
  `check_coder_harness_supported` (a `PreflightWarning`, which never blocks a launch), and skipped
  with the vendor's refusal by `tests/test_live_coder_matrix.py`. The other four pairings are live
  probed. Two traps when finishing it: the Coding Plan's `sk-sp-…` keys speak Chat Completions,
  which current Codex refuses, and the Token Plan changes both key and host.
  **Re-probed 2026-08-24 and the diagnosis holds, with two facts added.** The key authenticates:
  `GET /models` on the international host returns 200 with a 162-model list. Every inference call is
  still refused — HTTP 400 `AccessDenied.Unpurchased` ("Access to app denied") on the Responses path,
  HTTP 403 with the same code on Chat Completions — and that held for a **current** model id
  (`qwen3.8-max`) as well as the stale `qwen3-max`/`qwen-plus` tried before, so it is not a
  model-name error. **Second, the endpoint in `HARNESS_ENDPOINTS` is probably wrong:**
  `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` appears nowhere in the vendor's own
  Responses host list, which is workspace-scoped (`{WorkspaceId}.<region>.maas.aliyuncs.com`) plus a
  bare `dashscope-us` host that answered `InvalidApiKey` for this `sk-ws-` key. No host can be
  confirmed while the account is unentitled, so the row is left naming the host that at least
  authenticates and the discrepancy is recorded in `models/providers/dashscope.py`. The same gate
  makes the **chat** adapter added 2026-08-24 unverified, so `qwen` is unusable as a lead or co-lead
  until the account is activated.
  **The diagnosis was corrected the same day, twice, and both corrections matter.** First,
  `AccessDenied.Unpurchased` is not a billing gate: Alibaba's error reference gives it exactly one
  meaning, *"Alibaba Cloud Model Studio service has not been activated on your account"*, with a
  four-step remedy ending in agreeing to the service terms in the console. New accounts then get 1M
  free tokens per eligible model on the International endpoint for 90 days, so the row may cost
  nothing. Second, **there is more than one key class and each has its own host**, which is why no
  amount of probing one host settled it. Measured against every host `.../model-studio/base-url`
  documents: an `sk-ws-` pay-as-you-go key is valid on `dashscope-intl` alone (and there reports the
  unactivated service), while an `sk-mr-` **Coding Plan** key is valid on
  `coding-intl.dashscope.aliyuncs.com/v1` alone, where it lists ten models across four vendors and
  then refuses inference with *"invalid access token or token expired"*.
  **The architectural finding outranks the credential one:** `/responses` **404s** on the Coding
  Plan host, so that plan speaks Chat Completions only and can drive **neither** the Codex coder
  harness **nor** the chat adapter, both of which are Responses. A Coding Plan key is therefore a
  dead end for this system regardless of whether its token is renewed, which generalises the
  already-recorded `sk-sp-` trap to `sk-mr-`. The usable path is pay-as-you-go plus activation.
  `tmp_scripts/check_qwen_entitlement.py` sweeps every documented host and names both the accepting
  host and the remedy, so this is a seconds-long re-check rather than a re-investigation.
- **An unattended run's action space is narrower than its prompts once said, and MCP still
  flattens what the author is told.** `ask_human`/`plan_and_debate` reach a role's tool list only
  when a `human_handler` is wired, so headless the worker, lead and reviewer **cannot ask** —
  which also makes `author_bench/trial.py`'s `needs_human` terminal unreachable from them (never
  once fired in ~180 trials; its docstring says otherwise and is wrong). The three prompts that
  named those tools unconditionally were made conditional 2026-09-01, but **both already-published
  benchmark batches ran with the old text**, so a lead in those batches was instructed to settle a
  design through a channel it did not have — state that whenever those numbers are compared with
  later ones. Residual on the author side: `_make_ask_human_handler` now classifies *unattended*
  from *channel fault* before the boundary, yet MCP's `except Exception` still converts both into
  an `isError` carrying only `str(exc)`, so the distinction survives in the **ledger** events
  (`coder.ask_human.unattended` / `.error`) and in the message text, never in a type the author
  can branch on. A harness that let a tool return a typed failure would close that; none does.

- **Coder git-ban is best-effort (deny-rule) on the Claude harness.** Its Bash deny-rule
  (`disallowed_tools=["Bash(git:*)", "Bash(git)"]`, §9.9) blocks the obvious `git …`, but prefix
  matching can be slipped (pipes, `$(…)`, env prefixes, `python -c subprocess`). The Codex harness
  does not have this gap: `.git` is read-only inside a writable root under its sandbox. The robust catches are
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
- **API-key failover is per-key, and two of its edges stay best-effort.** `ANTHROPIC_API_KEY_FALLBACK`
  and `OPENAI_API_KEY_FALLBACK` rotate on a rate limit or a spent balance (`models/keys.py`), but
  (a) **rate limits are per-organization**, so a fallback issued by the *same* org rotates onto the same exhausted bucket —
  the preflight check cannot see the org and rejects only what it can prove useless (blank, duplicate,
  or primary-less); (b) **a coder session already in flight keeps the key it launched with** — the
  author harness owns that subprocess's environment for its lifetime, so only the *next* session picks
  up a rotation (**it does now start from the live key**: `coder/codex_sdk.resolve_vendor_key` reads
  the pool rather than the raw variable, fixed 2026-08-20 after an OpenAI author was unrunnable on a
  spent primary while its own co-lead answered on the fallback); and (c) 401 `authentication_error` is deliberately **not** rotated, so a revoked primary
  key still fails the run rather than silently running on the fallback; (d) key **retirement** is
  process-scoped, so a key retired for a spent balance is eligible again on the next run — right
  for a topped-up key, but it means a still-dead key costs one wasted call at each run's start.
  `keys.ROTATING_PROVIDERS` is what makes wiring safe rather than incidental: a provider whose
  adapter does not send `active_key` must never rotate, or the client would keep the SDK-resolved
  key while the caller skipped its cooldown. Wiring a provider takes **three** things — a cited
  per-provider error taxonomy, the adapter line, and the tuple entry — because the vocabulary is
  not shared: Anthropic reports a spent balance as a 400/403 `billing_error` while **OpenAI reports
  every key quota as a 429**, so a global "a 429 refills" rule leaves an OpenAI dead key in
  rotation. (e) **Google is deliberately unwired**: its troubleshooting guide and error reference
  disagree on the vocabulary, neither publishes the response body, and its SDK's `APIError.code` is
  the HTTP status integer where OpenAI's is a string code — so no rule is cited for it and it never
  rotates.

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

- 2026-08-10 — **A second Anthropic key, because the two failures that stall or kill a run are
  quotas of the *key*, not of Anthropic.** One retry policy answered everything by waiting: a 429
  cost up to ~30 min of cooldown per call across 8 attempts, and a spent credit balance — a
  non-transient 400 — killed the run outright (2026-07-22). Neither is provider capacity, and a key
  from another organization clears both at once, which no cooldown can do. `models/keys.py` holds a
  process-global per-provider key pool (`ANTHROPIC_API_KEY` + optional `ANTHROPIC_API_KEY_FALLBACK`);
  `retry.is_key_scoped_provider_error` splits that class off — 429 / `rate_limit_error`,
  `billing_error`, and a 400 naming the credit balance — and `call_with_transient_retry` consults an
  injected `rotate_key` **before** its cooldown, retrying immediately at no cooldown and no retry
  budget. **The triggers were read from Anthropic's error taxonomy, not inferred from the failures we
  happened to see** (the 2026-08-05 lesson): `.type` is what separates `billing_error` from the
  `permission_error` sharing its 403, and 529/5xx are excluded because provider capacity is identical
  on every key. 401 is excluded too, deliberately — a revoked key is a configuration fault a human
  must see, not one to route around. Rotation offers each key once per call and then falls through to
  the existing path, so a 429 on every key still waits and a balance spent on every key still raises
  honestly; because the live key is process state, rotation is bidirectional *across* calls and
  neither key is stranded. **Bidirectional rotation is only safe for a quota that refills, which
  the first draft missed and a pre-launch read of the actual Q21 terminal caught:** that run died
  on the credit-balance 400 with `$231.65` spent, so its primary is *permanently* dead — and
  rotating back onto a dead key the first time the fallback hit a routine 429 would have made that
  key's non-transient 400 the exception that raises, converting a one-minute wait into a dead run,
  strictly worse than having no fallback at all. So `is_exhausted_key_error` splits the
  non-refilling subset (spent balance) from the refilling one (429), and a balance failure
  **retires** that key for the process (`KeyPool.retire_active`); a 429 never does. Every key
  retired ⇒ rotation returns `None` and the run fails honestly instead of looping. Three seams
  follow from "the SDK resolves the env var once per client":
  the Anthropic adapter now passes `api_key=` explicitly, the coder subprocess gets the live key
  stamped on **only after a rotation** (`merge_flow._with_live_anthropic_key`, leaving the common path
  byte-identical), and each switch records a `provider.key_rotation` ledger event naming the slot,
  never the key. New preflight check `api_key_fallbacks` rejects the three fallbacks the pool silently
  deduplicates away — blank, identical to the primary, set without a primary — because each would
  leave a human believing a run is protected when rotation has nowhere to go. **Stated limit, not
  papered over:** rate limits are per-organization, so the fallback must be a different org for the
  429 half to help (§13). Anthropic-only by choice (§1 minimal); the pool is provider-keyed.
  `tests/test_provider_key_failover.py` (40); suite 781 → 821. **LIVE-VERIFIED the same day on the Q21
  resume**, against the very failure that had killed that run: the first Anthropic call of the resumed
  run (`analyst_worker/claude_sonnet_5`, seq 3647) took the credit-balance 400, rotated to `fallback1`,
  retired the dead key, and the run continued — spend unchanged at $231.65, because a request refused
  before any tokens is never billed. Preflight went 10/10 → **11/11** with `api_key_fallbacks`. What the
  resume does *not* yet exercise is a 429 on both keys and a rotation preceding a coder session
  (`tmp_remaining_livetest.md` K1).

- 2026-08-18 — **Authoring-reliability instrument (paper item 4), and key failover generalized to
  OpenAI.** `benchmarks/author_bench` runs one fixed brief K times through the **production**
  lead-driven loop (`AnalystLeadAgent.drive_authoring`), deliberately not the legacy
  `propose_and_pr` auto-loop whose rework caps §9.10 already records as unused on that path. The
  brief is not written for the benchmark: it is the Opus lead's own `ToolSpec` from run `46ed6671`
  (analysis #2), recovered verbatim from that run's ledger and frozen, so §3's walkthrough becomes
  trial 0 of the same distribution and the brief keeps its real ambiguities — its proposed checks
  name two synthetic fixture ObsIDs that the original authoring resolved by writing controlled CSVs
  into the test's own `tmp_path` instead. Trials clone the toolkit at `fa99ef8`, the parent of the
  commit that landed the tool, each on its own branch, so the live checkout is untouched and trials
  cannot see each other. Concurrency rests on the cost meter being a `ContextVar`: each trial
  installs its own **inside its own task**, or K trials would each report the batch total. No human
  handler is wired, so a raise records as the `needs_human` terminal (§9.6) rather than being
  answered for. The report separates **terminal states** (which partition K) from **rounds**
  (events inside a trial), a correction the paper's Table 6 needs too, and reports fabrication as a
  signal for hand-reading rather than a count.
  **The first batch (K=15) died at 48 minutes and $122.26 with 0 trials finished**, on
  `429 insufficient_quota` / `credit_balance_exhausted` from the GPT co-lead, and that exposed the
  second item. The key pool was already provider-generic; the **error vocabulary** was not.
  Anthropic reports a spent balance as a 400/403 `billing_error`, so `is_exhausted_key_error`
  short-circuited "any 429 refills" — true there, false at OpenAI, where every key quota is a 429
  and only `code` separates the refilling one from the four that do not. Adding OpenAI to
  `ROTATING_PROVIDERS` alone would have rotated, never retired the dead primary, and bounced back
  onto it at the fallback's next rate limit: the failure retirement exists to prevent. Both
  predicates now take a **required** `provider` and read a per-provider table whose every entry
  cites that provider's own documentation, with shapes taken version-exact from the SDKs
  (openai 2.38.0 parses `.type`/`.code` from `body["error"]`) and confirmed against the live error.
  An unrefillable quota now **raises once rotation is exhausted** instead of taking the transient
  path — the 48 minutes were spent backing off against a balance no wait could restore. `AsyncOpenAI`
  sends `api_key=` explicitly. **Google stays unwired and the reason is recorded**: its own two
  documentation pages disagree on the vocabulary, neither publishes the response body, and
  google-genai's `APIError.code` is the HTTP status integer where OpenAI's is a string code, so a
  shared field read would misclassify silently. Measured on the way: a full authoring trial costs
  **$27–45** through the whole loop against the $13–30 projected from the Q21 windows, which had
  priced tools authored onto a branch already carrying their chain-mates; `papers/FANGZ/DOCS.md`
  §8/§8.4 corrected, K set to 10. Suite 821 → 957 (`tests/test_author_bench.py` 14,
  `test_provider_key_failover.py` 40 → 49).

- 2026-08-19 — **The author became swappable: a second harness, and the `coder` role now names
  which one.** Paper item 7 (the model-tier sweep) needs authors other than Claude Sonnet, and the
  research answered the shape of that work: **alt models are not a harness problem** — DeepSeek,
  Z.AI GLM and Alibaba Model Studio each publish an Anthropic-dialect endpoint documented for Claude
  Code *and* a Responses endpoint documented for Codex — while **a second harness is real work**.
  Codex-first was chosen because all three vendors are natively Codex-capable, so one backend opens
  every leg with no gateway. New `sandbox/coder/`: `CoderBackend` is three methods
  (`connect`/`drive`/`disconnect`), which is all the authoring flow ever needed from an author, so
  the gate, the co-lead review, `approve_edit`, the per-attempt base ref and the failure reset are
  untouched; `claude_sdk.py` holds today's wires verbatim and re-exports them from `merge_flow` so
  callers and tests are unchanged; `codex_sdk.py` is the new one. `RoleModelConfig` gained
  `harness`/`base_url`/`key_env`, `ProviderName` gained the three harness-only vendors (whose
  members resolve to a `HarnessOnlyProvider` that raises if anything tries to chat with them), and
  two preflight checks refuse an impossible pairing or an unlaunchable harness before any model is
  built. **Decorrelation needed no new rule** — it reads the author's vendor, so a DeepSeek author
  and the GPT co-lead separate on their own.
  **The harness swap is not neutral, and the differences were measured rather than assumed** (live
  probe, cents): Codex's `workspace-write` + `writableRoots` turns `agent_fs_allow` into an
  **OS-enforced write jail** (half of §12, finally), `.git` is read-only inside it so the coder's
  git ban stops being a prefix rule, and its per-call token counts let coder spend be priced by the
  same code as every chat role instead of the Claude SDK's opaque session dollars. Against that:
  **Codex reads are ungated** (full-disk read access is documented design; a command the sandbox
  permits is never offered to the client's approval handler), so a crossing is recorded as
  `coder.read_outside_allowlist` and nothing more — a §4 boundary the Claude legs enforce and the
  Codex legs do not, to be stated wherever a Codex leg is reported. Escalation moved to an
  in-process Streamable-HTTP MCP endpoint on loopback with a per-session bearer token, because
  `ask_lead` and `ask_human` must keep reaching the live lead object and the run's HITL connector.
  The role prompt rides as `developerInstructions` (layering over Codex's own tool instructions)
  rather than replacing them.
  **The full OS jail was priced the same day and is light, not heavy:** Landlock active,
  unprivileged user namespaces enabled, `bwrap` present, and PyXspec + the toolkit importing inside
  a jail that binds only the allow-list while denying `auto_research` and `~/.claude` — the last of
  which is also the one conflict, since the Claude author authenticates from the credentials there.
  New deps: `openai-codex==0.147.0` (pip; it pulls its own version-matched CLI binary, which is why
  no conda `codex` is pinned — SDK and app-server share a protocol). Suite 967 → 995
  (`tests/test_coder_backends.py`). **NOT LIVE TESTED:** a real authoring attempt on the Codex
  harness (the probe exercised the harness, not the flow), and no alt-vendor credential exists yet.

- 2026-08-19 (same day, follow-up) — **The coder's live coverage became a matrix, and the harness
  table was narrowed to what the code can do.** The three `claude_sdk` alt-vendor rows are gone:
  those vendors do publish an Anthropic-dialect endpoint, but the Claude backend sets no
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` and no model-name mapping, so a member naming one
  would have silently authored on Claude — a table that promises more than the backend delivers is
  worse than a short table. Five pairs remain, and `sandbox/coder/harness.py` records why the other
  three are absent. **`tests/test_live_coder_matrix.py`** is the coder analogue of the schema
  matrix: one warm session per leg, walked through nine stimuli in the order a real attempt produces
  them (seed brief, gate rejection, co-lead rejection, lead instruction, governance-file read, git
  attempt, `ask_lead`, lengthening trajectory), with every stimulus **frozen out of real ledgers**
  rather than written for the test (`tests/fixtures/coder_history.json`;
  `tmp_scripts/extract_coder_history.py` mines it). Assertions stay on our side of the boundary and
  a leg skips on a missing credential, so two legs run today and three wait on vendor keys.
  **It caught two defects before it caught anything about a harness.** Its own first invocation died
  on a missing import, which is the `NameError` class the 2026-08-06 static guard exists to prevent
  — in a file that guard did not scan. `tests/test_no_undefined_names.py` now scans `tests/` and
  `benchmarks/` too, and that widening immediately found a real latent one: `test_live_agents.py`
  still constructed a `LibrarianSearcherAgent` for a role deleted from config, so that live test
  would have died the moment anyone ran it. **Lesson, matching the original:** a test that only runs
  behind an env flag has fewer chances to be caught than a rare source branch, so the guard belongs
  over the tests as much as over the package.
  **Then the matrix found the two Codex defects it was built to find, neither visible to any
  deterministic test.** The escalation tools never reached their handler: MCP servers are read when
  the app-server **process** starts, not per thread, so a server passed at thread start never
  connected — and once registered correctly, an MCP tool call is offered to the client as an
  **elicitation** whose reply vocabulary is the MCP spec's `{"action": "accept"}`, so answering with
  the `{"decision": …}` used for patch approvals reads as a rejection and the author is told *"user
  rejected MCP tool call"* while our handler is never called. This is the
  read-the-contract-don't-infer-it lesson again, at a new boundary: two shapes arrive on one
  callback and they do not share a vocabulary. Second defect: the read audit resolved Codex's
  **relative** command paths against this process's cwd, recording 47 crossings for ordinary
  in-worktree reads — and a second path bug behind it, since `cwd` is a `RootModel` whose `str()`
  is `root='/tmp/…'`, cost 40 more. An audit that cries wolf hides the crossing it exists to show.
  All fixed and pinned; the escalation round-trip is now live-verified end to end on Codex (model →
  localhost MCP → in-process handler → answer). **One finding was about the test:** demanding an
  *edit* in response to a recorded rejection asks a throwaway toolkit to contain the files that
  rejection names, so the cells assert **delivery** and record the rest. Suite 995 → 1101.

- 2026-08-19 (third, same day) — **Launch checks learned two things: what the config actually
  needs, and how to say "unproven".** `check_required_env` had a hand-written key list that drifted
  in both directions — it demanded `GOOGLE_API_KEY` from a config with no Google member, and knew
  nothing of the vendor variables the coder harnesses authenticate with, so a missing one surfaced
  inside an author subprocess instead of at launch. Requirements are now **derived from
  `config/models.yaml`**: a provider a role names implies its own SDK's variable, a member's
  `key_env` requires exactly that variable and beats the provider default (a plan-specific endpoint
  may use another name), and the failure names the role/member that wanted each missing key. The
  alias table carries all six vendors, so configuring DeepSeek, Z.AI or Qwen as *chat* members in
  the model sweep needs no `key_env` at all.
  **New warning tier.** Preflight was pass-or-raise, which cannot express a precondition that is
  satisfiable and unproven at once. `PreflightWarning` fills that: the launch continues, the line
  reads `WARNING`, the caution records at level 2, and the summary counts warnings apart from
  failures. Its first user is the Qwen coder pairing (§13): configured against the endpoint the
  vendor documents, never run, because the account is not entitled. Blocking that launch would be
  wrong and passing silently would make a first-time failure look like a defect in the flow.
  The live matrix classifies it the same way — a leg whose vendor refuses the account on the seed
  turn **skips carrying that refusal** rather than failing, since nine downstream cells reporting
  nine consequences of one cause would bury the cause. Four of five legs are live-probed.
  Suite 1101 → 1112.

- 2026-08-20 — **The OS-level jail is built and shipped off.** The Codex measurement made the case
  concrete: a coder read this repository's own backend, docs, test and the harness's config file
  unprompted, 36 crossings in one session, because that harness grants full-disk reads by design and
  our guard only inspects the paths a model names. `sandbox/jail.py` turns `agent_fs_allow` into a
  `bwrap` mount plan for all four process kinds that touch a disk — both author harnesses (through
  their own binary-path options, so a vendor's argv changes cannot break the wrapping), the analyst
  REPL, and the gate's Layer-B pytest, that last one **network-denied** because a mergeability
  verdict must depend on the data on disk (§9.4), verified against all 30 existing suites first.
  `agent_fs_readonly` splits the list, which makes the jail *tighter* than the guard rather than a
  copy of it: nothing writes to the conda environment, HEASoft or CALDB.
  **Four rules it learned by failing, each now pinned.** bwrap creates a mount point's parents
  itself, so only the **outermost** symlinked path may get a link — recreating symlinked ancestors,
  then a nested root, killed all 30 suites twice with "Can't mkdir parents". A later bind lands on
  top of an earlier one, so roots are applied **outermost first** or declaration order silently
  decides which of a nested read-only/writable pair wins. The jailed process is pid 1 of its own
  namespace, so `--die-with-parent` is what keeps the interrupt path's kill total. And the finding
  worth carrying: **the allow-list was never exercised as a mount plan**, so nobody had to know it
  was incomplete — every file in PyXspec's package directory is a symlink four levels up into a
  sibling of `$HEADAS`, which produced an importable-looking namespace package and an `ImportError`
  on the first real symbol, thirty suites deep instead of at launch. The HEASoft release root joined
  the list, and `check_os_jail_probe` now runs the configured `analyze_probe` **inside** the jail at
  launch. Two failure modes on purpose: a missing `bwrap` or denied user namespaces **warns** (that
  is the configuration every earlier run used), while a jail that breaks the science stack **fails**
  the launch rather than failing every tool call separately. Feasibility was measured before any of
  this was written — Landlock active, unprivileged user namespaces enabled, `bwrap` present, PyXspec
  importing inside a jail that denies `auto_research` and `~/.claude` — and the Claude author's
  credential file is bound as a single read-only file so the subscription path and every measured
  cost figure survive a hidden home. Shipped **off** (`agent_os_jail: false`); what remained was a
  live authoring attempt under it and egress control for the three processes that keep the network.
  **The authoring attempt happened the same day and the jail did not survive it intact** — see the
  third 2026-08-20 entry: four startup defects, then the structural one (this harness's own sandbox
  is bubblewrap, so jailing it nests the same mechanism). Now fixed and live-verified, with the
  harness sandbox standing down when the jail is on. **Egress control remains the open item.**
  Tests: `tests/test_jail.py` (19 → 28). Suite 1115 → 1136.

- 2026-08-20 (second, same day) — **The author became a parameter, so the model sweep is a benchmark
  rather than a config edit — and building it found a key the coder could never reach.** Paper item 7
  (the coder model sweep, §6.2 Table 8) needs one fixed brief authored by several vendors, and the
  authoring flow could not express that: `build_authoring_session` resolved the model id one way, the
  author provider a second way for review routing, and `_build_backend` resolved the harness member a
  third way, each reading the `coder` role independently. One `coder: RoleModelConfig` parameter now
  carries all three facts (`_resolve_coder_provider` deleted, `_resolve_coder_model` reduced to an
  accessor), which is a simplification rather than an addition: the three could previously disagree.
  Harness dispatch is now proven by *passing* members instead of monkeypatching ambient config, which
  is what production does. Second seam: `make_review_hook(..., allow_correlated=False)`, the only way
  past the decorrelation boundary, opt-in and set by no production caller, because the same-provider
  arm is otherwise inexpressible — an injected co-lead is asserted decorrelated too. Every review
  under it emits `review.correlated`, since a verdict reached that way does not carry production's
  guarantee, and the launch preflight still guards the configured roles.
  **The sweep lives in `benchmarks/author_bench`** (the human's call): a *leg* is one (author member,
  reviewer arrangement) pair with its own K, concurrency ceiling and vendor brand colour; legs run
  sequentially with trials concurrent inside; the report grows a per-leg table only when a batch has
  more than one leg, and the single-leg report is **byte-identical** to the one already in the paper.
  Two structural rulings came out of the roster. **A tier row cannot be an OpenAI author**: the tier
  axis needs the reviewer held fixed, and the configured co-lead is OpenAI, so such a row would vary
  two things at once — it appears instead as a deliberately correlated row, with decorrelation
  elaborated in the text. **The decorrelation panel holds the author at the campaign default** and
  moves only the reviewer, which makes the existing K=10 batch its cross-provider arm for free;
  `claude-opus-5` is the pinned same-provider reviewer because the reviewer swap found it the
  strongest, so a cost there is a strong result and no cost an honest negative one. The **Qwen row is
  absent, not substituted**, with the reason recorded in code. The subscription session cap that
  contaminated six of ten trials in the first batch is now a **concurrency ceiling on the roster row**
  rather than something the operator must remember.
  **The finding: the Codex author never consulted the key pool.** It resolved its vendor key from the
  raw environment variable at two places, so it always launched on the *primary* even after a chat
  role had rotated off a spent one — worse than the §13 note that a session in flight keeps its key,
  because it never *started* from the live key. Surfaced by the new per-leg vendor probe: the OpenAI
  primary was out of credits (`429 credit_balance_exhausted`) while the configured fallback answered,
  so an OpenAI author was unrunnable at the same moment its own co-lead was answering normally. One
  shared `resolve_vendor_key` now reads the pool for a rotating provider whose member names that
  provider's standard primary variable, and the raw variable otherwise — a plan-specific variable must
  not be shadowed, and a harness-only vendor has no cited error taxonomy so it never rotates. It
  returns a **log-safe slot name**, never the value. The probe uses the same resolver *and fails over*,
  because the pool only advances reactively: at launch it still holds the spent primary, so the probe
  is the run's first real call and retires a spent key for the process, leaving the batch to launch
  its authors on the key that answered instead of discovering the dead one inside an author
  subprocess after each trial's lead spend is gone. **Human ruling on cost reporting:** the paper
  states one list-price USD figure with no billed-versus-notional split ("the science won't care
  whether those were billed from a subscription or an API key"), and the Claude coder is *not* moved
  onto an API key to make its spend real, because it is too expensive to bill that way. Suite
  1136 → 1160.

- 2026-08-20 (third, same day) — **The OS jail met its first author, and needed five fixes to
  survive it.** §12 recorded "a live authoring attempt under it" as the jail's remaining work; the
  coder sweep supplied one, and every layer failed in turn — all at startup, all for $0, which is
  the only reason this was cheap. (1) The launcher **never bound the binary it execs**: both
  harnesses ship their CLI inside the orchestration environment, a tree `agent_fs_allow` deliberately
  does not name, so `bwrap` failed `execvp` on all 40 trials. (2) **Bind and exec disagreed on the
  path spelling** — the bind resolved symlinks, the exec did not, so the Claude author still failed
  after its own directory was bound, while Codex survived because for it the two coincide. The launch
  check I had just written resolved as well, so it reported "reachable" while the launcher failed:
  *a check that normalises differently from the thing it checks is not a check.* (3) Codex was
  pointed at the **operator's own settings directory**, absent from the jail. (4) Both CLIs keep
  state in **SQLite** and the runtime dir is an **NFS** mount, where it cannot take locks, so author
  homes moved to local disk. (5) A label derived from the clone's *directory name* collided across
  every concurrent author, because every trial clones into a directory called `repo`.
  **The structural one was last: this harness's own sandbox IS bubblewrap.** Jailing it nests the
  same mechanism — Codex cannot spawn `codex-linux-sandbox`, every shell command is rejected, and the
  author reaches the gate having read and written nothing, which reads as a weak model rather than as
  broken plumbing. The coder diagnosed it itself and escalated to the lead, the §9.6 path behaving as
  designed. **Human ruling: one enforcement layer.** The harness sandbox stands down when the kernel
  jail is on, and `coder_policy` tightens the author to two writable places (its working tree and its
  own settings home), which is *stricter* than the `workspace-write` surrendered: the observation
  database is read-only, so no author can write a data file where a fabricated result would have to
  live, and the temp root is absent entirely so one author cannot read another's solution — a
  contamination that would have invalidated a repeated-trial measurement rather than broken anything
  visibly. **Live-verified end to end**: two concurrent authors per harness starting jailed, the
  Claude pair authenticating from the subscription and replying, and a full production trial
  authoring both toolkit halves plus tests and passing the deterministic gate on its second
  submission. Suite 1136 → 1176. **Two process lessons.** A hand-built probe is not the production
  path: mine failed with the jail *off* too, which is the only reason I did not read its failure as a
  code defect. And the config-flip lesson recurred twice in one session — two tests asserted against
  whatever `agent_os_jail` happened to be on this host, and both now sweep it.

- 2026-08-24 — **The three harness-only vendors became chat members, so the lead and co-lead are a
  provider axis rather than a constant.** §8.5 of the paper DOCS found that the coder sweep varied
  the reviewer's provider and the lead's together, because the lead was Anthropic in all five arms;
  separating them needs leads and co-leads from more than two vendors. DeepSeek, Z.AI and Alibaba
  Model Studio each already publish an OpenAI **Responses** endpoint — the dialect our OpenAI
  adapter speaks — so this was a factoring, not three new adapters: `providers/_responses.py` holds
  the wire logic (input projection, capture-time field filtering, replay ordering, one
  request/one extraction) and each vendor module holds a `ResponsesVendor` of documented facts
  beside its citations, mirroring how `_media.py` already splits mechanics from per-provider policy.
  Copying 540 lines written against three run-killers in one week would have forked those fixes
  four ways. `CHAT_PROVIDERS` is now all six; `HarnessOnlyProvider` remains for any future unwired
  vendor.
  **Every flag was read from the vendor's docs and then probed, because a compatibility table says
  what is *accepted*, not what is *honoured*** — and that distinction decided who may hold a role.
  **DeepSeek** honours a strict schema exactly (bare JSON, declared keys, both its models), calls
  functions, and accepts the `tools`+`schema`+`tool_choice="none"` compose shape, so it is a full
  member: **11/11 live** on all nine real schemas plus both trajectory composes. **Z.AI accepts
  `text.format` and ignores it**, answering once with prose carrying no JSON and once with a fenced
  object whose keys the schema never declared. A 200 with invented keys cannot be caught downstream,
  so `supports_strict_schema=False` refuses the request with a reason instead; its documented
  workaround (schema in the prompt) did return exact keys on a nested schema, but once, so nothing
  is wired on one draw. DeepSeek also rejects `prompt_cache_key`, which the lead role sends on every
  cached call.
  **Two defects only a live probe could find, both now per-vendor flags.** Its strict mode accepts a
  **narrower `format` set** and enumerates it in the refusal ("expected one of `email`, `hostname`,
  `ipv4`, `ipv6`, `uuid`"), which refused three of the nine schemas for carrying a date field. And
  its validator **does not follow a `$ref` inside an `anyOf` branch**, so every `X | None` field
  broke: the cheap fix was probed first and merely moved the error to "An object with no properties
  is not allowed", which is what proves the reference is never followed, while inlining the
  definition is accepted and validates. The inliner is deliberately narrow (only a branch whose sole
  key is `$ref`, never through a definition already open on the path) and its **first version
  recursed without bound** — caught by its own guard test, not by any schema we send.
  **Live tests now route through `ModelHandle` rather than `provider.call`.** Both frontier primaries
  were out of credit that day (Anthropic 400 credit balance, OpenAI 429 `credit_balance_exhausted`)
  and 22 legs failed against working fallback keys, because key rotation lives in the layer the live
  tests skipped. Routing through the handle fixes that and also closes the altitude gap recorded on
  2026-08-06, where a live test could not see a defect introduced above the adapter. The same move
  exposed a stale monkeypatch target that resolved for Anthropic and raised for OpenAI, now derived
  from the vendor registry instead of spelled per leg.
  **Qwen ships unverified**: its key authenticates and lists 162 models, but every inference call is
  refused `AccessDenied.Unpurchased` on both dialects and with a current model id, so it is a
  billing gate rather than our wiring; its documented Responses hosts are workspace-scoped and do
  not include the one `HARNESS_ENDPOINTS` names, recorded rather than silently changed. New deps:
  none. Suite 1008 -> 1058; live 36/1-skipped on the schema matrix and 7/7 on tool search.
  **Open, awaiting a ruling:** whether GLM gets a row in the lead x co-lead matrix given that its
  structured path is unmeasured (handoff [I14]).

- 2026-08-24 (second, same day) — **A structured answer now has two routes, because a vendor can
  accept the parameter that requests one and ignore it.** Z.AI answers `text.format` with
  `strict: true` at HTTP 200 and then writes prose, or an object whose keys the schema never
  declared. A 200 with invented keys cannot be caught downstream, which is worse than a rejection.
  The human's insight found the way through: **a coding harness never uses structured output** —
  Claude Code's and Codex's own editing tools are JSON-Schema functions, so any vendor that works
  inside those harnesses must handle schema-shaped tool arguments. `ResponsesVendor.schema_via_tool`
  therefore declares one synthetic tool whose parameters *are* the schema and pins `tool_choice` to
  it. Live-verified with the real `ToolReviewVerdict` on **both** dialects Z.AI publishes, and then
  across the whole nine-schema matrix. The loop's own tools stay declared beside the emit tool, so a
  replayed trajectory still resolves every item that names one — forcing the emit tool is what
  replaces `tool_choice="none"` there — and the emit call is consumed into `parsed`, never surfaced
  as a `tool_call` the ReAct loop would try to execute. A vendor that declines the forced call
  raises rather than returning a quiet `parsed=None`.
  **Two of my own defensive choices were wrong and were corrected by measurement.** DeepSeek's
  narrow `format` allowlist had been applied to Z.AI and Qwen as well; that narrowing is imposed by
  *DeepSeek's strict-mode validator*, which these vendors never reach, and Z.AI demonstrably accepts
  `format: date` in tool parameters — so it is now DeepSeek-only, per the standing rule to enforce
  what a vendor refuses rather than what another one does.
  **And the forced-tool route has one measured quirk, which the fix must not overstate.** GLM
  intermittently fills an optional field with the *string* `"null"`: schema-valid against a
  `string` branch and a type error for anything else (live, `SearchRequest.since`). The same call
  emitted correct JSON `null` for an optional model field and real floats for both tolerances, so it
  is intermittent, which is the worse shape for a batch — and it reaches the authoring path, whose
  `AuthoringDecision` carries two optional floats. `_normalise_null_strings` coerces a literal
  `"null"` to `None` **only** where the schema permits null and no branch accepts a plain string, so
  `float | None` and `date | None` are unambiguous while `str | None` is left exactly as written. It
  normalises spelling, never content. Writing its tests caught two bugs in the first implementation:
  a date field is `{"type": "string", "format": "date"}`, so an "any string branch" exclusion skipped
  the very case it was written for, and `tool_specs.items` is a `$ref` the walk could not follow.
  **Live tests were also moved onto `ModelHandle`** rather than calling `provider.call` directly:
  both frontier primaries were out of credit and 22 legs failed against working fallback keys,
  because key rotation lives in the layer those tests skipped. That also closes the altitude gap
  recorded on 2026-08-06. The move exposed a monkeypatch target that resolved for Anthropic and
  raised for OpenAI once the wire logic was shared, now derived from the vendor registry.
  Suite 1058 -> 1073; Z.AI 12/12 live on the real schemas (one transient vendor auth blip, passing on
  retry), Qwen skipping with the vendor's own refusal quoted.

- 2026-08-24 (third, same day) — **Qwen came alive, and the last defect turned out to be ours: a
  compatibility layer does not resolve `$ref`.** Activating Model Studio cleared the entitlement
  gate, and with all five vendors reachable the remaining failures were schema-shaped rather than
  vendor-shaped. `ResearchReport` and `AuthoringDecision` failed on Qwen at *every* reasoning
  effort (10/11/8 errors, always at `figures.0`, `references.0`, `sections.0` — the `list[Model]`
  fields), which is what showed the cause was structural: our inliner rewrote `$ref` only inside
  `anyOf`, so list-item schemas stayed unresolved and the model **invented** a shape for them (a
  made-up `figure_id`, none of the required fields). Dereferencing the whole schema and dropping
  `$defs` fixes it; the narrow version is gone.
  **The two compat vendors fail the same way in opposite shapes, and that is the durable lesson.**
  DeepSeek *rejects* an unresolved reference (HTTP 400, found in minutes); Qwen *accepts* and
  fabricates a plausible object (HTTP 200, found only because Pydantic rejected it downstream). A
  rejection announces itself; an invention looks like an answer. It is the argument for keeping
  strict validation at the role layer rather than trusting a 200, and for treating "the vendor
  answered" as no evidence at all.
  **Two corrections the human's questions forced, both of which had been wrong in my picture.**
  `structured_retry_cap: 2` already retries off-schema output at the role layer, so no retry
  needed building — and the live tests sit *below* that loop, calling `ModelHandle.call` directly,
  so they measure raw single-shot behaviour and made Qwen look worse than a real run will.
  Thinking was never disabled: it runs at each vendor's default, and `reasoning.effort: none` is
  strictly worse (it breaks `AuthoringDecision` while default and high both validate), so the
  right trade is keeping thinking and *offering* the emit tool rather than disabling thinking to
  force it. Qwen refuses a named `tool_choice` while thinking — **the same rule Anthropic
  documents** for its own thinking models — so the emit tool is declared under `auto` with a final
  instruction that the call IS the answer. That instruction exists because a prompt written for
  native structured output may end "do not call any tool", which on this route forbids the answer
  itself; measured, it produced no emit call at all.
  **Qwen's credentials also had to be understood before any of this could run:** the key class
  decides the host, an `sk-mr-` Coding Plan key is valid only on `coding-intl` where `/responses`
  404s (so that plan can drive neither harness nor adapter), and only a pay-as-you-go key on
  `dashscope-intl` serves us. `tmp_scripts/check_qwen_entitlement.py` sweeps every documented host
  and names the remedy. `harness.py`'s endpoint was correct all along and its doubt is retracted.
  **Live: 55 passed / 2 skipped / 0 failed** across all five vendors on the nine real schemas plus
  the trajectory compose. Suite 1075 -> 1078. The lead x co-lead grid is now genuinely 5x5.
  **Corrected 2026-08-25: it is 4x5.** The lead is sent figures and DeepSeek is text-only under the
  ruling recorded that day, so DeepSeek reviews but does not lead; Google is excluded from both by
  [I8], its second tool call in any loop being a hard 400.

- 2026-08-24 (fourth, same day) — **"Cannot resolve a `$ref`" was never one property, and the
  boolean that encoded it is deleted.** Prompted by the human asking whether we had actually
  *tried* sending references to each vendor: we had not. Z.AI had inlining enabled purely by
  analogy with DeepSeek and was never tested; the general claim rested on two data points from
  different vendors. Measured properly at 5 draws per cell, every cell 0/5 or 5/5, no variance:

  | vendor | `anyOf: [{$ref}, null]` | `items: {$ref}` |
  |---|---|---|
  | DeepSeek | refused 0/5 | fine 5/5 |
  | Z.AI | fine 5/5 | fine 5/5 |
  | Qwen | fine 5/5 | refused 0/5 |

  **Qwen is what forced the redesign:** it needs opposite treatment in the two positions, so a
  per-vendor boolean is wrong whichever way it is set — and blanket dereferencing was *causing*
  its `anyOf` failures (a working 5/5 shape driven to 0/5, answering with a JSON-encoded string in
  an object field). `inline_anyof_refs` and the blanket inliner are gone; `resolve_refs_in` names
  the positions a vendor is measured to need, defaulting to none. Z.AI's shim was deleted rather
  than switched off, per the human's instruction.
  **DeepSeek's rule, probed six ways** (default endpoint, `/beta`, `/beta` strict function-calling,
  and the `$defs`/`$def`/`definitions` spellings): every `anyOf` branch must carry a literal
  `type`, checked *before* any reference is followed — adding the type moves the error to "An
  object with no properties is not allowed", which is what proves the pointer is never resolved.
  Its docs list `anyOf` and `$ref` as supported and are silent on the combination, so this is
  measured rather than read. The community search that opened the question also **independently
  corroborated the narrow `format` allowlist**, which had been derived only from an error message.
  **No dependency was added.** `jsonref` does blanket dereferencing, which the data says is wrong
  for Qwen, so the library solves a problem we no longer have; the ~60 lines kept do something no
  library does, resolve selectively by position.
  **Two process notes.** A closed third-party issue ("not planned") was cited as support when a
  live measurement of our own request already existed — the probe was always the authority, and
  the human was right to force the re-check. And writing the position-aware resolver
  **reintroduced the same unbounded-recursion bug fixed an hour earlier** (expand with the path
  recorded, then re-walk the expansion with the path forgotten); the guard test caught it, not
  review. Suite 1081.

- 2026-08-25 — **DeepSeek and Qwen live-probe trouble was separated into documented capability
  limits, endpoint behavior, and local transformations.** DeepSeek's Responses API
  accepts only `deepseek-v4-flash` and substitutes placeholder text for image/file inputs; the
  prior vision-experimental model claim had no official source, so its descriptor now withholds
  figures with a visible note. Alibaba's newer `qwen3.8-max` page lists image/text/video input and
  native visual understanding, so Qwen figures remain enabled. Alibaba's current Responses docs
  confirm custom functions, `allowed_tools`, and workspace-specific regional endpoints; three fresh Qwen
  terminal-tool variants stalled past 130 seconds (`allowed_tools` past six minutes). Its legacy
  endpoint remains functional, while Alibaba recommends workspace-specific domains for higher
  stability; member `base_url` is the deployment-time path once the workspace ID is known. The
  last live-verified automatic choice remains in production pending a stable response.

  Two local schema bugs were fixed: exact constrained strings such as enum/const/pattern `"null"`
  retain their data value, while an invalid optional spelling still normalizes to JSON null; and a
  root `$ref` expands only in Pydantic's bare `$ref` + `$defs` form, preserving JSON Schema 2020-12
  sibling constraints. A Qwen terminal response that both emits structured data and requests a
  working tool is now rejected as premature for bounded retry. Responses clients also disable the
  SDK's two hidden retries and cap one HTTP attempt at 300 seconds; `retry.py` remains the sole,
  visible retry owner. Qwen now sends Alibaba's documented session-cache header whenever the
  caller marks a stable prefix; the vendor defaults that cache off. Focused provider/media suite:
  100.

- 2026-08-25 — **Qwen caching is now the adapter default and explicit in both schema probes.**
  Alibaba's `x-dashscope-session-cache: enable` header rides every Qwen Responses request, while
  both the direct nine-schema live matrix and the two real-trajectory compose paths pass
  `cache_prefix=True`. This removes caller omission as a source of Qwen latency while preserving
  the common provider interface; the live matrix remains the measurement of endpoint tail latency.

- 2026-08-25 — **Qwen latency became a controlled live-test axis instead of an inherited
  default.** The successful 11-leg `qwen3.8-max` probe took 517.12 s against Sonnet's 78.14 s;
  across the nine direct calls, output-token count explained 98.6% of Qwen latency and Alibaba's
  Responses reference identified the omitted setting: `reasoning.effort` defaults to `medium`.
  `tests/test_live_schema_matrix.py` now runs the full nine-schema + two-trajectory matrix at
  explicit `minimal` reasoning for both `qwen3.8-max` and the balanced `qwen3.7-plus`, preserving
  the existing `(provider_name, provider, model)` fixture. A focused, identical `MemoryRecord`
  request sweeps `none`/`minimal`/`medium` on both models; logs are model-specific and retain
  elapsed time, cache reads, output tokens, and reasoning tokens per call. `none` stays a measured
  mechanical-call option rather than the full-matrix default because it previously broke the
  complex `AuthoringDecision` shape.

- 2026-08-25 (second, same day) — **Role eligibility was decided per role instead of per tier, and
  the lead x co-lead grid was sized from it.** §6 had one blanket clause, "reviewer-tier and analyst
  models must be vision-capable", which is a tier statement about a per-role fact. Audited against
  the call sites: three roles are sent figures (reviewer, analyst lead, analyst worker) and the
  **analyst co-lead is not**, by construction rather than by accident — `review_tool` passes no
  images and its tool set is read-file/list-dir/web-search with the knowledge-base tools off, so a
  code reviewer never receives a plot. The clause now names those three. Two smaller findings from
  the same audit: the `coder`'s `vision: true` is dead config, an author being a harness subprocess
  and never a `ModelHandle`, and the flag is a declaration rather than a guard, since it gates the
  call-time `images=` channel only and not figures arriving on a tool result ([V-2]).
  **The DeepSeek exclusion was right and its stated reason was wrong.** The previous day's entry
  recorded that DeepSeek's Responses API accepts only `deepseek-v4-flash` and that the removed
  `deepseek-v4-flash-vision-exp` descriptor "had no vendor source". The vendor's Responses guide
  lists three models including `deepseek-v4-pro`, and its vision guide documents the vision model
  with exactly the limits that had been deleted. Human ruling: the code stands
  (`images_accepted=False`), because the project declines a multimodal path its vendor labels
  experimental and will revisit on a non-experimental one — but the *justification* is replaced,
  since "the vendor has no vision model" is a fact a later reader would act on and would be wrong,
  where "we decline the experimental one" is a decision they can reopen. The recurring rule
  (2026-08-05) applies at a new address: read **every** page the vendor publishes for the
  capability, not the one page already open. The Responses reference was read; the vision guide,
  which is where that vendor documents image support, was not.
  **The grid.** §8.5 of `papers/FANGZ/DOCS.md` records a confound: the analyst lead was Anthropic in
  all five arms of the coder sweep, so the reviewer's provider moved with the lead's and neither
  could be credited. The measurement agreed this session is a **4 x 5 lead x co-lead grid at a
  pinned author**, `deepseek-v4-flash` on the Codex harness, K=5, 100 trials. Leads are Anthropic,
  OpenAI, Z.AI and Alibaba; co-leads are those four plus DeepSeek, which reviews without vision on
  `deepseek-v4-pro`. Google is out of both axes on [I8]. Because DeepSeek authors but cannot lead,
  the author-correlated and lead-correlated cells are fully crossed and never coincide — the four
  arms §8.5 says do not exist — at the cost of the doubly-correlated cell being structurally
  unreachable, so no interaction is estimable. The four author-correlated cells run only through
  `make_review_hook(allow_correlated=True)` and each records `review.correlated`.
  **What the grid decides is §9.3 itself:** that rule binds the *author's* vendor and says nothing
  about the lead's, and the coder sweep's own data does not support the author as the operative
  axis. If the lead-correlated diagonal moves and the author-correlated column does not, the rule
  binds the wrong actor. Docs-only this session; no code changed.

- 2026-08-31 — **Submission snapshots: an authoring attempt is durable from the moment it meets
  the gate.** Real-run ledgers show coding is the chunk that eats most time and money, and the
  checkpoint map had a hole exactly there: LangGraph resumes at node granularity, `approve_edit`
  commits are durable, the per-attempt base ref heals the branch — but everything between "coder
  starts" and "commit" was volatile, so a review-provider failure on a gate-passed tree cost a
  full re-authoring. `AuthoringSession.run_gate_step` now persists every submission (after the
  §9.9 coder-commit backstop, before the scan) as a patch + verdict record under
  `run_dir/authoring/submissions/<tool>/`, failed submissions included; on resume,
  `build_authoring_session` restores the latest snapshot only onto its exact recorded base ref,
  seeds the coder with the restored tree and its outstanding feedback, and the gate re-runs on
  the restored tree — no stored verdict is trusted, `commit ⇔ gate ∧ review` stays defined over
  the live tree, `approve_edit` stays the sole commit path, and the placement follows the
  existing discipline (durable state under the run dir; tree, never process; resume re-enters at
  a boundary by re-running the step). `commit()` discards the tool's record; a snapshot that
  fails to apply is discarded — an economy, never a dependency. Same session: the swapfix
  batch's 8 review-stage errors were investigated (none internet/throttle: 2 were our
  `ReadOnlyToolProvider` bare-`int()` crash on a model-sent `offset='150, '`, fixed to an
  `is_error` tool result per the [F11] rule and re-run clean; 6 are known vendor
  structured-output shapes, now ruled to count as failing the item — `swap._review_errored`
  keeps them in the denominators). **LIVE-VERIFIED 2026-09-01**
  (`tests/test_live_snapshot_resume.py`): real coder + gate + co-lead on the frozen run-46ed6671
  brief; killed mid-review with no cleanup; heal → restore → the new session continued on the
  restored tree through two more gate-passed submissions and ended in an honest no-commit within
  the review cap. The full-run (`run_research(resume=True)`) leg of the §13 durable-resume list
  stays open; the authoring-attempt leg is closed.

- 2026-09-01 (second, same day) — **Turning the human channel off was already done; what was
  broken was what the system says when it is off.** A benchmark plan was choosing between three
  ways to close HITL for an unattended batch, and reading the code retired two of the three
  options as already-decided or dead. **Withholding the tool was the existing behaviour** for the
  worker, lead and reviewer (the providers append `ask_human` only when a handler is wired;
  `include_plan_and_debate=human_handler is not None`), so `HumanInputRequired` is unreachable
  from them and the "record it as a `needs_human` terminal" arm has **never fired in ~180
  `author_bench` trials**. The author was the only live exposure — and its failure was not a lost
  trial but a **mirror**: every MCP stack turns a handler exception into an `isError` carrying
  only `str(exc)`, and `resolve_human` raises `HumanInputRequired(query.headline)`, so a coder
  asking *"Which calibration epoch applies?"* was answered with *"Which calibration epoch
  applies?"*. Measured by probe, not inferred. The same `except Exception` also made a **broken
  connector** and a **wrong-typed handler** indistinguishable from an absent human, at the author
  *and* in the ledger. Fixed by classifying before the boundary: `coder.ask_human.unattended` +
  a re-raised `HumanInputRequired` carrying the new domain-neutral `UNATTENDED_GUIDANCE` (search
  the web first and hard, then `ask_lead`, then reason and state the assumption in the code and
  its test), versus `coder.ask_human.error` + a `RuntimeError` naming the fault. **Both stay a
  tool error** — the rejected alternative was a canned *answer*, which would write content no
  human wrote into the ledger as a reply and, on the budget-free `plan_and_debate`, open a free
  retry loop bounded only by `react_loop_absolute_cap` (up to ~100 extra full model calls on a
  growing context). The typed raise still reaches every catching caller, so §9.8 is unchanged.
  **The genuine behaviour change was the prompts:** `analyst_lead.md` told the lead to settle a
  non-trivial design *via* `plan_and_debate` and to close it with a final such turn, and
  `reviewer.md`/`coder.md` named `ask_human` unconditionally — headless, instructions to use
  tools the role was never given, and **true of both published batches**, now a stated cross-batch
  difference. All three take the conditional positive form `analyst_worker.md` already used.
  **Second item, measured before it was decided:** the run ledger's per-field caps (2,000 args /
  4,000 results / 4,000 coder text) were raised to 100,000 and moved into `orchestration.yaml`.
  Scanning the `[+N chars]` markers of real ledgers showed the heaviest run (`8c75990e`, five
  tools authored, $116) hiding **6.50 M characters behind 1,027 trimmed fields** — 7.92 → 14.42 MB
  uncapped, **1.8×** — while a non-authoring run is 1.0×, i.e. trimming is almost entirely an
  *authoring* phenomenon. Largest single field 68 kB, so 100k keeps everything observed and still
  bounds a pathological blob; 14 trials cost order 0.2–0.35 GB. Config rather than constants
  because the ledger is the **durable** record an offline scoring pass reads long after, so the
  value a batch used must travel with that batch — and read at call time, since a constant
  captured at import makes a config key look wired while doing nothing. The read side needed
  nothing: `MonitorToolProvider` already windows a payload at 8,000/32,000 chars with a `bias`
  offset and `ledger_grep` returns index rows only, so write cap and agent context are
  independent. **Process note:** the caps were the only irreversible choice in the batch's design
  — every other scoring decision can be revisited offline from the archive, but text trimmed at
  write time is gone — which is why they were measured rather than argued. Suite 1447 → 1462
  (`test_coder_backends.py` 46 → 49, `test_env.py` 8 → 15, `test_prompts.py` 15 → 20).
  **Unrelated pre-existing failures observed and left alone** (§3: consult before fixing): nine
  `benchmarks/gate_bench/tests/test_gate_bench.py` cases fail on `.pytest_cache` files leaking
  into the synthetic-repo comparison.

