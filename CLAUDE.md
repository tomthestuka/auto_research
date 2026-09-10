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
2. The **analyst lead runs one ReAct loop per analysis cycle and states every decision by
   filing a form** (`run_cycle`; `author_chain` / `hold_authoring`, then
   `request_another_round` / `submit_adjudication` / `submit_terminal_report`). The branch it
   picks *is* the decision, so the branch and the fields it must fill arrive together, each
   question carrying its own description — the channel a Pydantic `#` comment never reached.
   `author_chain` asks for at least one link **in its own schema** (`minItems: 1`) **and its handler
   refuses a chain without one**, which is the layer that actually holds. Measured 2026-09-02: the
   Opus lead filed `author_chain` carrying only `rationale`, `tool_specs` absent — a provider does
   not enforce `minItems` on a tool `input_schema` the way it constrains structured output, so
   "author, with nothing to author" is **expressible and rejected**, not inexpressible. The
   rejection is an `is_error` result naming the missing field, which `_react` treats as a
   correction; that lead recovered on its next turn and filed a real 4-link chain. (A run died on
   this state going *unrejected*, 2026-09-01 — the schema is the belt, the handler the suspenders.) Every form is declared for the whole cycle and the *handler*
   enforces the stage, because a declared tool set that shrinks mid-conversation is a documented
   run-killer; the caps stay in Python (`debate_max_rounds`, `tool_author_cap`), so the loop
   chooses *what* and the harness decides *how many times*. Filing the chain then **spawns a
   Sonnet-tier coding sub-agent** (the **author**) per link, the lead staying in an
   **interactive channel** with it during authoring — to clarify science/code as it goes (this
   lead↔author loop is built in step 2 of [§9.10](#910-build-order-phase-2)). Between links the
   coding transcript is dropped at its own boundary, keeping only the lead's filed brief and its
   own submit call with the real commit SHA on the result: both turns it produced or received,
   nothing synthesized in their place.
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
   (`reviewer.provider ≠ author.provider`, [§6](#6-config-driven-model-registry)) — the rule
   reads whichever vendor the configured author names, so a DeepSeek author on the Codex
   harness is reviewed by the GPT co-lead exactly as a Claude one is. It judges: does the code match the
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

`AnalystReport.status` is a first-class, **agent-declared** terminal state — `succeeded` or
`failed`, defaulting to `failed` — that the lead and reviewer treat as a *correct* outcome with
**no quality penalty**; otherwise the retry cap pressures the agent to invent something on the
last attempt. "Real results or no results" is absolute. The harness never infers the state: a
report nobody wrote has not succeeded at anything, which is why the default is the honest one
(F9, 2026-09-09; it previously defaulted to `ok`). `NEEDS_HUMAN` was retired from this schema —
headless, `ask_human` is not in a graph role's tool list, so the state was unreachable by the
route it names, and [A20] shows the label being filed for something that was not a human block.
A role that needs a person says so in `summary`. (The lead's own `submit_terminal_report` keeps
`failed`/`needs_human`: a cycle decision, not a worker's outcome.)

**The reviewer is the sole I/O — an honest failure is adjudicated, not bypassed.** When an
authoring attempt does not commit, the analyst lead files **one terminal report**
(`submit_terminal_report`, one of its cycle forms — §9.3) documenting the unavailable capability
and any partial findings, rather than re-authoring or re-entering debate (closing the
cap-exhausted authoring *spin*). That
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
  git to the runtime and `ask_lead` instead of working around it, and **the kernel enforces it on
  both harnesses**: `jail.coder_policy` binds `.git` read-only inside every writable root, so a
  write to it fails however it is spelled (a pipe, a subshell, `python -c subprocess`) while
  `status`/`diff`/`log`/`show` keep working. Above that sit the harnesses' own best-effort rules —
  the Claude SDK author's Bash deny-rule (`disallowed_tools=["Bash(git:*)", "Bash(git)"]`) and the
  Codex sandbox's own read-only `.git` — which are prefix-matched and can slip, which is why the
  gate-entry check and reset below remain the backstops on either harness. The runtime's own git is
  outside the jail (`approve_edit`, the gate-entry reset, `collect_all_changes` all run in the
  parent process), so making `.git` read-only for the author costs the runtime nothing.
- As the **first action at the gate**, `_gated` compares HEAD to the per-attempt base ref; if the
  coder committed (slipped the deny-rule), the runtime does `git reset --mixed <base_ref>` — undoing
  the commit but **keeping the work in the working tree** — and emits a `coder.committed` event, so the
  gate then scans the recovered changes normally. The coder never touches git, its work is preserved,
  and the only real commit remains `approve_edit`'s (post gate+review), so `commit ⇔ gate ∧ review`
  holds. This is what makes the gate robust to commit state (vs. the old empty-changeset confusion).
- **A terminal attempt auto-resets whatever work the tree still holds unbanked**, back to the attempt's
  **base ref** — HEAD recorded per-attempt at `propose_and_pr` entry, and advanced by each
  `approve_edit` commit — not just to `HEAD` (`git reset --hard <base_ref>` + `git clean -fd`), after
  saving the discarded attempt's diff vs the base ref as a `tool_author_failed_*.patch` under the run's
  `authoring/` dir. The guard is a **tree query** (`pending_changes()`), not the session's commit latch:
  asking *"did this session ever commit?"* let a session that committed one tool and then kept working on
  a rejected second one exit without resetting, leaving the rejected tool callable through
  `registry.reload()`, which reads the tree (measured live 2026-09-08; corrected 2026-09-09). Resetting to the base ref (rather
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

- **Coder git-ban: kernel-enforced since 2026-09-09; the deny-rule above it is still best-effort.**
  `jail.coder_policy` binds `.git` read-only inside every writable root, so `add`/`commit`/`checkout`
  fail with *"Unable to create '.git/index.lock': Read-only file system"* on both harnesses however
  the command is spelled, while `status`/`diff`/`log`/`show` succeed. Measured, not assumed
  (`tests/test_jail.py::test_a_jailed_process_can_read_git_history_and_cannot_write_it` runs a real
  repository through a real `bwrap`). Residual: with `agent_os_jail` off there is no kernel layer,
  and the Claude harness's Bash deny-rule (`disallowed_tools=["Bash(git:*)", "Bash(git)"]`, §9.9) is
  prefix-matched and slippable (pipes, `$(…)`, env prefixes, `python -c subprocess`) — and it also
  denies read-only inspection, which cost a live coder one turn reporting its own change set
  (2026-09-09, seq 128). The robust catches are
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
- **Which media survives an eviction is now load-bearing, and the Files API is not the way out.**
  The media budget reserves the request's non-media weight (2026-09-02), so eviction actually
  happens where it previously did not — and eviction is **oldest-first** by design (the current
  turn's evidence is the most relevant). A single near-ceiling document therefore survives while
  several small ones are dropped one at a time to make room for it: a reviewer holding one 24 MB
  paper and five small ones keeps the 24 MB one. Strictly better than the prior behaviour, where
  nothing was evicted and the request 413'd, but the *choice* is a policy nobody has measured. A
  size-aware eviction (drop the largest that would free enough) is the obvious alternative and is
  **not** built — it inverts the relevance ordering the module states, so it needs a ruling rather
  than an edit. Related: the §12 Files API item would **not** fix this and looks worse than the
  inline path on the evidence — Anthropic recommends `file_id` references "to keep request payloads
  small", but anthropic-sdk-python#1098 reports a 413 at ~6.49 MB of *referenced* PDFs with an
  ~18 KB HTTP body, i.e. an undocumented ~6.3 MB combined ceiling enforced during server-side
  extraction, unanswered by maintainers. It would trade a documented 32 MB ceiling for a tighter
  undocumented one.
- **`web_fetch` has no size bound, and it is the one by-value payload that still does not.**
  Observed live 2026-09-02 in a science-sweep trial: a single `web_fetch` returned **159,668
  characters** — the largest tool result of the run by 14x, the next being `review_report` at 11.6k
  and the toolkit's own `ixpe_pcube_timeseg` at 8.2k. Nothing broke, because Anthropic context
  editing absorbed it, but the page then dominated that worker's context and was re-cleared on
  **every** subsequent turn (~111k tokens per edit, repeatedly), crowding out real evidence and
  paying for it each time. This is the shape the `open_paper` 8 MB refusal was written for (§13
  history: an oversized paper rides by value in every later call and blew a 413 cap) and the shape
  `read_file` avoids by paging with a stated resume offset. The fix is a character cap **at entry**
  in `execute_web_fetch` with a model-visible truncation marker naming what was cut — downstream is
  too late, since the payload is already in the trajectory. Not built: found mid-run, and §3 gates
  a fix on a human ruling.

---

- **The coder can read the run's own record — accepted and measured, not closed (ruling
  2026-09-03).** Re-audited over every event of run `fbb93f26` (attempt 6) by eleven independent
  chunk audits: the coder's `grep -rl` over the run directory returned `backend.log`,
  `monitor.jsonl` and the lead's report files (seq 4007), the ledger content it then parsed carried
  `model` fields for the lead and worker 0, and two `backend.log` lines named **both workers'**
  models (seq 4045) — three of the four varied seats; the reviewer's and co-lead's models never
  reached it. The coder also hunted for the orchestration repo, `orchestration.yaml` and
  `TOOL_MANIFEST` in site-packages (seq 4031–4042) and **the jail held**: only the conda env path
  surfaced, whose `auto_research` install is editable and points at unmounted source. Human ruling:
  `runtime_dir` **stays** on `agent_fs_allow` — agents may read their own logs — so the channel is
  measured per trial by `science_bench --scan`'s actor-read check rather than closed; report
  filenames no longer carry the model (`workspace.round_report_path`). The lead listed the run root
  five times in that run and read nothing from it; no worker or reviewer touched it.
- **The path guard denies `research_toolkit`'s own contract.** `PathGuard` denies
  `CLAUDE.md`/`AGENTS.md`/`DOCS.md` by basename everywhere; the toolkit README's first line points
  agents at its own `CLAUDE.md`, and in attempt 6 the co-lead followed it **once** (`AGENTS.md`,
  the symlink; seq 4264–4265) and was refused. Human ruling: that file is safe and intended for
  coding agents; scoping the denial by `toolkit_root` was written and then dropped as a guard-logic
  change. Two wasted tool calls per run; the toolkit README is left as written.
- **The author's environment inherited the operator's editor variables — FIXED 2026-09-03.**
  `CLAUDE_CODE_EXECPATH` and a Copilot variable reached the coder's shell (seq 922, via
  `_source_ini_env` on top of the launching terminal's environment); `_author_env` now drops
  `CLAUDE*`/`COPILOT*`/`VSCODE*`. The `None` (inherit) branch, taken only when no analyst env is
  configured, is unfiltered.
- **`request_another_round` is not open at the author stage ([A20]).** A link's loop ends only on
  a filed form, and after a `COMMITTED` submit the only form open is `submit_terminal_report`, so
  the lead filed it — `status: needs_human` — after each of attempt 6's three commits; the rerun
  followed the commit count regardless, so the outcome was right and the recorded terminal wrong.
  Contaminates the Tier-0 terminal-state signal the science sweep scores. Not fixed (loop logic);
  the trivial remedies are a sentence in the author-stage instruction and a scoring rule that a
  terminal filed with `committed ≥ 1` is not a failure row.
- **The reviewer-swap batches ran with a defect-only artifact in the reviewer's view.** Fixed in
  the corpus builder 2026-09-03 (see changelog): `keep_only_passing_tests` left
  `.pytest_cache/v/cache/lastfailed`, naming the honest test the plant breaks, in every pruned
  defect worktree and in no control. Pruning entered on 2026-08-25, so the published 37-defect
  table (08-17 batches) is unaffected; the paper's Table 3 / Fig. 4 source (`20260831T172738Z-
  swapfix-*`) and every swap batch from 08-26 on carried it for the 20 pruned entries, and the
  `qwen3_8_max` co-lead cited the file verbatim in two catches (08-27 swapclean). Records hold no
  tool trajectories, so silent use by other legs cannot be excluded. **Open: re-run the swapfix
  legs, or exclude the 20 entries with the reason stated.**

## Changelog

Compacted 2026-08-05 (entries through 2026-07-31) and again **2026-09-01 (entries through
2026-08-25)**: folded into themed date ranges keeping tag ids, rulings, rejected alternatives,
measured numbers, and NOT-LIVE-TESTED / LIVE-VERIFIED markers; the narrative detail is in git
history. Token-level preservation of the second pass was checked mechanically, not by eye.

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

- 2026-08-03 -> 2026-08-07 — **The replay seam, the self-imposed exclusivity behind it, and the
  first multimodal guard.** **Data honesty first:** one malformed ADS `esource` record (a relative
  URL where `FullTextLink.url` is an `HttpUrl`) had zeroed the whole `nasa_ads` source — now the
  record is skipped and every well-formed link beside it kept, *a bad link costs one link, never
  the source*. **[H9]**: HEASARC serves a still-proprietary observation **GPG-encrypted**, and four
  layers independently defined "present" as a filename matching a product regex, so a complete
  download read as empty ground and re-acquisition was the correct inference from what the agent
  was told (live: SS 433 `04004001`, ~25 min to no effect). Fixed with **one shared rule**
  (`neutral.split_lock`/`is_locked`/`LOCK_SUFFIX`) in the module every layer already imports.
  Human rulings: a locked product keeps its **honest** level and is marked `LOCKED, NOT USABLE` on
  its catalog line (the mark, not a demoted level, is what callers read); a repeat request consults
  the archive's `public_date` (`heasarc.availability`) instead of re-transferring; encryption state
  is **derived from the recorded path, never stored**; only `.gpg` is recognised. Folded in **O4**
  (the `data_catalog` brief said "mission" five times against a schema with no such argument).
  **Then the replay seam.** With tool search on, part of the transcript is authored by the
  *provider*, and three live 400s in one week were the same mistake wearing new faces — we re-typed
  a provider's record into a smaller shape of our own, destroying at ingest anything we had not
  thought to keep: `Unknown parameter: 'input[2].created_by'` (the Responses API **accepts a
  smaller vocabulary than it emits**; openai-python **#3008**/**#2323**/**#2670** ask for the
  `.as_input()` helper the SDK does not ship), `Missing namespace for function_call`, and
  `tool_search_call … without its required 'reasoning' item`. Interim fixes carried the provider's
  record whole (`ToolCall.provider_fields`; an inverted `_UNCARRIED_ITEM_TYPES` default;
  `_as_input_item` filtering **by name and by value** once at capture, which is what protects
  prompt caching — the hazard is a different *rule* on different turns, not one that reads values).
  **Root cause, 2026-08-05:** `ModelHandle.call` treated `schema` and `tools` as mutually
  exclusive — **self-imposed**, asserted in four places, and a live probe showed all three
  providers accept both. That exclusivity forced the terminal compose to declare no tools, which
  *is* the request shape behind the 2026-07-29 `Tool reference … not found` killer and its
  `include_discovery` amputation. Gone, and **the response type now IS the assistant turn**:
  `AssistantToolUse` deleted, `ConversationMessage = ModelMessage | ModelResponse |
  ToolResultMessage`, `_react` appends the response it received instead of hand-copying three
  fields into a twin (the copy had silently dropped tool-search blocks until 2026-07-27 and
  `thinking` until now). `provider_blocks` → **`provider_parts`, allowlist deleted** (we read 3 of
  Anthropic's 16 block types; the set is never complete). **Replay follows each provider's own
  documentation** — Anthropic *"echo the assistant message exactly as received"*, Google stateless
  *"MUST always resend all `thought` blocks exactly as they were received"*, OpenAI's pass-back
  guidance — so Anthropic and Google echo the whole array while OpenAI keeps its capture-time
  projection, whose field allowlist is derived from the SDK's own `ResponseInputItemParam` union
  (27 types). Google stopped using `.text`/`.function_calls` (they flatten a mixed turn and hide
  `thought_signature`) and captures the signature onto `provider_fields` — the **[I8]** mechanism,
  **not yet live-verified**. Two rules had been wrong because inferred from 400s rather than read
  from docs (a "thinking first" reassembly order; a "drop only tool-referencing blocks" filter —
  the docs forbid rebuilding or filtering at all). **The unification then let a fourth run-killer
  through (2026-08-06):** the *assertions* were deleted but not the branching — both adapters still
  ran `if schema is not None: … return` **before** `if tools is not None`, so passing both silently
  dropped the tools, and the test named for that property asserted `parsed` came back rather than
  that the tools reached the request. Live run `8c75990e` (Q21) died on exactly that at **$29.50**,
  one step before its first commit. Fix: each adapter builds ONE request and runs ONE extraction;
  `tool_choice` gains **`"none"`** (`"none"` / `{"type": "none"}` / `mode="NONE"`, each read from
  that provider's docs), every request that replays a trajectory declares its tools, and
  `include_provider_parts` is **deleted rather than narrowed**. The probe ran before the code, for
  cents (`tmp_scripts/probe_tool_choice_none.py`) — as did the cheap live test that caught two of
  the three 400s, after the first had cost ~$10 of live analysis. **Immediate sequel:** the relaunch died 80 min /
  $5.36 in on `NameError: name 'tools' is not defined` — two of seven compose sites patched by
  pattern with `tools=tools` where the local is `self.tools` — in `decide_authoring` and
  `clarify_for_author`, branches no deterministic test exercises. Closed by `tests/test_no_undefined_names.py`, a scope-aware AST guard (a naive version
  gives 183 false positives from closures alone; pyflakes would do it but is not in the env, and a
  test may not depend on an uninstalled linter). **And the altitude mistake behind it:** live tests
  called `provider.call` directly and never executed `roles.py`. Two guards close that —
  `test_compose_declares_loop_tools.py` (per call site, the compose declares **the same** `tools`
  expression its own `_react` used) and `test_live_schema_matrix.py` (all **nine** structured
  schemas to **both** providers with tools declared; 18/18, ~112 s). Suite 665 → 770; live 7/7 both
  providers on tool search and 4/4 on the new real-schema compose. Then the human's two questions
  found one more: the lead's `adjudicate` attaches a report workspace and detaches it in a
  `finally`, and the safety-net compose ran *after* that, declaring `self.tools` **minus** the two
  tools the replayed trajectory can name — source-text equality could not see it (the *object*
  mutates), so the compose moved inside the `try` and a runtime guard joined the static one.
  **Schema mixing does not happen**: every role method emits exactly one schema, its tool-less
  fallback included, pinned by `test_one_conversation_carries_exactly_one_schema`.
  **Deferred, recorded:** `stop_reason` is still not modelled (`_react` infers "done" from the
  absence of tool calls, so `max_tokens`/`refusal`/`pause_turn` all read as a clean finish); tool
  `input_schema`s get no dialect normalization; the tool_use `caller` field is dropped on a
  synthesized-turn rebuild. **Also observed live on those runs** (first-time evidence): durable
  resume re-entered the analysis node with no literature repeat, the ledger stayed append-only
  across the kill, cost seeded from the ledger, locked-ObsID handling worked end to end on both
  providers, and **Anthropic context editing fired in a real analysis** (two edits, ~26k tokens
  each) — though its collision with tool search (§13 watch item (a)) was not exercised. Open from
  the same runs: **[F3]** the filesystem guard denies `2>/dev/null` and trains agents into
  `cd <dir> && <relative>` form the command check cannot inspect.
  **2026-08-07, the multimodal boundary had no guard at all.** Run `8c75990e` reached its **fifth
  commit** — five tools authored, every one gate-passed and co-lead-approved first try — then died
  at **$116.10** on `400 … image dimensions exceed max allowed size for many-image requests: 2000
  pixels`. Anthropic's rule: past **20 image blocks** the per-image dimension cap tightens to
  2000 px, and a worker's own newly-authored plotter returned 3 PNGs per call that replay inline on
  every later request. **The audit was the finding:** we base64'd whatever we were handed on all
  three providers, checking neither count, dimensions, bytes, total payload, nor mime; the one
  guard in the system was `_OPEN_PAPER_MAX_MB = 8` in the *tool* layer, which could see neither the
  provider nor the rest of the request. **The gate belongs in the adapter** because we are
  stateless: it holds the whole conversation plus this call's attachments before sending. New
  `providers/_media.py` (walk → plan → resize → page-count); each adapter declares `_LIMITS` quoted
  from its own docs, because a shared cap would be wrong for all three (Google's **20 MB inline**
  is the tightest ceiling; Anthropic's >20-block rule the sharpest edge). An over-limit image is
  **resized**, a document **withheld**, and either way a model-visible note names the file and the
  reason — silent absence is what invites invention. A resize touches only the transmitted copy, so
  `FigureRef.sha256` still identifies the artifact a human reproduces. **Nothing downscales unless
  a limit would otherwise be breached** — the human caught the framing error in the first proposal:
  providers already resize server-side and bill the smaller version, so client-side downscaling
  saves **no tokens**, only payload. Closes the mime half of **[V-3]**; deletes
  `_OPEN_PAPER_MAX_MB`. **Files API deferred** (§12) as a cost/latency change that would not have
  prevented this (a `file_id` image is still an image block, still counted against 20, still
  measured against 2000 px). New deps `pillow`, `pypdf`. Suite 770 → 780; live proof
  `test_live_many_image_request_that_used_to_400` sends the exact 21-image shape that died.

- 2026-08-10 -> 2026-08-18 — **Key failover, and the per-provider error taxonomies it needed.**
  The two failures that stall or kill a run are quotas of the *key*, not of the provider: a 429
  cost up to ~30 min of cooldown per call across 8 attempts, and a spent credit balance — a
  non-transient 400 — killed a run outright. `models/keys.py` holds a process-global per-provider
  key pool (`ANTHROPIC_API_KEY` + optional `…_FALLBACK`); `retry.is_key_scoped_provider_error`
  splits that class off — 429 / `rate_limit_error`, `billing_error`, and a 400 naming the credit
  balance — and `call_with_transient_retry` consults an injected `rotate_key` **before**
  its cooldown, retrying at no cooldown and no retry budget. **Triggers were read from the vendor's
  error taxonomy, not inferred from the failures we happened to see:** `.type` separates
  `billing_error` from the `permission_error` sharing its 403; 529/5xx are excluded because
  provider capacity is identical on every key; **401 is excluded deliberately** — a revoked key is
  a configuration fault a human must see. **Bidirectional rotation is only safe for a quota that
  refills**, which a pre-launch read of the Q21 terminal caught: that run died on the credit-balance
  400 with **$231.65** spent, so rotating back onto a dead key at the fallback's next routine 429
  would convert a one-minute wait into a dead run — strictly worse than no fallback. So
  `is_exhausted_key_error` splits the non-refilling subset and a balance failure **retires** that
  key for the process (`KeyPool.retire_active`); a 429 never does; every key retired ⇒ the run fails
  honestly. Three seams follow from "the SDK resolves the env var once per client": the Anthropic
  adapter passes `api_key=` explicitly, the coder subprocess gets the live key stamped on **only
  after a rotation** (leaving the common path byte-identical), and each switch records
  `provider.key_rotation` naming the slot, never the key. New preflight `api_key_fallbacks` rejects
  the three fallbacks the pool silently deduplicates away. **Stated limit:** rate limits are
  per-organization, so the fallback must be a different org for the 429 half to help (§13).
  `tests/test_provider_key_failover.py` (40); suite 781 → 821. **LIVE-VERIFIED the same day on the
  Q21 resume**, against the very failure that killed it: the first Anthropic call rotated, retired
  the dead key, and continued — spend unchanged at $231.65, because a request refused before any
  tokens is never billed. Preflight 10/10 → **11/11**. **NOT covered:** a 429 on both keys, and a
  rotation preceding a coder session (`tmp_remaining_livetest.md` K1).
  **2026-08-18, the authoring-reliability instrument (paper item 4).** `benchmarks/author_bench`
  runs one fixed brief K times through the **production** lead-driven loop
  (`AnalystLeadAgent.drive_authoring`), deliberately not the legacy `propose_and_pr` auto-loop whose
  rework caps §9.10 records as unused on that path. The brief is **not written for the benchmark**:
  it is the Opus lead's own `ToolSpec` from run `46ed6671`, recovered verbatim and frozen, so §3's
  walkthrough becomes trial 0 of the same distribution and the brief keeps its real ambiguities.
  Trials clone the toolkit at `fa99ef8` (parent of the commit that landed the tool), each on its own
  branch. Concurrency rests on the cost meter being a `ContextVar`: each trial installs its own
  **inside its own task**, or K trials each report the batch total. No human handler is wired, so a
  raise records as the `needs_human` terminal (§9.6) rather than being answered for. The report
  separates **terminal states** (which partition K) from **rounds** (events inside a trial) — a
  correction the paper's Table 6 needs too — and reports fabrication as a signal for hand-reading
  rather than a count. **The first batch (K=15) died at 48 minutes and $122.26 with 0 trials
  finished**, on `429 insufficient_quota` / `credit_balance_exhausted` from the GPT co-lead, which
  exposed the second item: the key pool was provider-generic but the **error vocabulary** was not.
  Anthropic reports a spent balance as a 400/403 `billing_error` while **OpenAI reports every key
  quota as a 429** and only `code` separates the refilling one from the four that do not. Adding
  OpenAI to `ROTATING_PROVIDERS` alone would have rotated, never retired, and bounced back onto the
  dead primary. Both predicates now take a **required** `provider` and read a per-provider table
  citing that provider's own documentation, shapes taken version-exact from the SDKs (openai 2.38.0
  parses `.type`/`.code` from `body["error"]`). An unrefillable quota now **raises once rotation is
  exhausted** instead of taking the transient path. `AsyncOpenAI` sends `api_key=` explicitly.
  **Google stays unwired and the reason is recorded**: its two documentation pages disagree on the
  vocabulary, neither publishes the response body, and google-genai's `APIError.code` is the HTTP
  status integer where OpenAI's is a string code, so a shared field read would misclassify
  silently. Measured on the way: a full authoring trial costs **$27–45** against the $13–30
  projected from the Q21 windows (which had priced tools authored onto a branch already carrying
  their chain-mates); `papers/FANGZ/DOCS.md` §8/§8.4 corrected, K set to 10. Suite 821 → 957.

- 2026-08-19 -> 2026-08-20 — **The author became a swappable parameter, launch checks learned to
  warn, and the OS jail met its first real author.** Paper item 7 needs authors other than Claude
  Sonnet, and the research answered the shape: **alt models are not a harness problem** (DeepSeek,
  Z.AI GLM and Alibaba each publish an Anthropic-dialect endpoint documented for Claude Code *and*
  a Responses endpoint documented for Codex) while **a second harness is real work**. Codex-first
  was chosen because all three vendors are natively Codex-capable. New `sandbox/coder/`:
  `CoderBackend` is three methods (`connect`/`drive`/`disconnect`), which is all the flow ever
  needed from an author, so the gate, the co-lead review, `approve_edit`, the per-attempt base ref
  and the failure reset are untouched. `RoleModelConfig` gained `harness`/`base_url`/`key_env`;
  `ProviderName` gained the three harness-only vendors (resolving to a `HarnessOnlyProvider` that
  raises if anything tries to chat with them); two preflight checks refuse an impossible pairing
  before any model is built. **Decorrelation needed no new rule** — it reads the author's vendor.
  **The harness swap is not neutral, and was measured rather than assumed:** Codex's
  `workspace-write` + `writableRoots` turns `agent_fs_allow` into an **OS-enforced write jail**,
  `.git` is read-only inside it so the coder's git ban stops being a prefix rule, and its per-call
  token counts let coder spend be priced by the same code as every chat role. Against that,
  **Codex reads are ungated** (full-disk read access is documented design; a command the sandbox
  permits is never offered to the client's approval handler), so a crossing is only recorded as
  `coder.read_outside_allowlist` — a §4 boundary the Claude legs enforce and the Codex legs do not,
  **to be stated wherever a Codex leg is reported**. Escalation moved to an in-process
  Streamable-HTTP MCP endpoint on loopback with a per-session bearer token. New dep
  `openai-codex==0.147.0`. **The three `claude_sdk` alt-vendor rows were then removed**: those
  vendors do publish an Anthropic-dialect endpoint, but the Claude backend sets no
  `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` and no model-name mapping, so a member naming one
  would have silently authored on Claude — *a table that promises more than the backend delivers is
  worse than a short table*. Five pairs remain; `harness.py` records why the other three are absent.
  **`tests/test_live_coder_matrix.py`** is the coder analogue of the schema matrix: one warm session
  per leg through nine stimuli in the order a real attempt produces them, every stimulus **frozen
  out of real ledgers** rather than written for the test. **It caught two defects before it caught
  anything about a harness:** its own first invocation died on a missing import — the `NameError`
  class the static guard exists to prevent, in a file that guard did not scan — so the guard now
  scans `tests/` and `benchmarks/` too, which immediately found a real latent one
  (`test_live_agents.py` constructing a role deleted from config). **Lesson: a test behind an env
  flag has fewer chances to be caught than a rare source branch.** Then it found the two Codex
  defects it was built for, neither visible to a deterministic test: MCP servers are read when the
  app-server **process** starts, not per thread, so a server passed at thread start never connected;
  and once registered, an MCP tool call arrives as an **elicitation** whose reply vocabulary is the
  MCP spec's `{"action": "accept"}`, so answering with the `{"decision": …}` used for patch
  approvals reads as a rejection and the author is told *"user rejected MCP tool call"* while our
  handler is never called — **read-the-contract-don't-infer-it at a new boundary: two shapes arrive
  on one callback and they do not share a vocabulary.** Second: the read audit resolved Codex's
  **relative** command paths against this process's cwd (47 false crossings), and behind it `cwd` is
  a `RootModel` whose `str()` is `root='/tmp/…'` (40 more) — *an audit that cries wolf hides the
  crossing it exists to show*. Escalation is now live-verified end to end on Codex. One finding was
  about the test: demanding an *edit* in response to a recorded rejection asks a throwaway toolkit
  to contain the files that rejection names, so those cells assert **delivery** and record the rest.
  **Launch checks, same day:** `check_required_env` had a hand-written key list that drifted both
  ways; requirements are now **derived from `config/models.yaml`** (a provider a role names implies
  its SDK's variable; a member's `key_env` beats the provider default), and the failure names the
  role/member that wanted each missing key. New **`PreflightWarning`** tier — preflight was
  pass-or-raise, which cannot express a precondition that is satisfiable and unproven at once; the
  launch continues, the line reads `WARNING`, and the summary counts warnings apart from failures.
  Its first user is the **Qwen coder pairing, UNVERIFIED** (§13): configured against the documented
  endpoint, never run, because the account is not entitled — blocking would be wrong and passing
  silently would make a first-time failure look like a defect in the flow. A leg whose vendor
  refuses the account on the seed turn **skips carrying that refusal** rather than failing, since
  nine downstream cells reporting nine consequences of one cause would bury the cause.
  **The OS-level jail was built and shipped OFF** (`agent_os_jail: false`; `sandbox/jail.py`): the
  Codex measurement made the case concrete — a coder read this repository's own backend, docs, test
  and the harness's config file unprompted, **36 crossings in one session**. `bwrap` turns
  `agent_fs_allow` into a mount plan for all four process kinds that touch a disk (both author
  harnesses through their own binary-path options, the analyst REPL, and the gate's Layer-B pytest
  **network-denied**, because a mergeability verdict must depend on the data on disk, §9.4);
  `agent_fs_readonly` splits the list, which makes the jail *tighter* than the guard rather than a
  copy of it. **Four rules learned by failing, each now pinned:** bwrap creates a mount point's
  parents itself, so only the **outermost** symlinked path may get a link; a later bind lands on top
  of an earlier one, so roots are applied **outermost first**; the jailed process is pid 1 of its own
  namespace, so `--die-with-parent` keeps the interrupt path's kill total. And the finding worth
  carrying: **the allow-list was never exercised as a mount plan**, so nobody had to know it was
  incomplete — every file in PyXspec's package directory is a symlink four levels up into a sibling
  of `$HEADAS`, producing an importable-looking namespace package and an `ImportError` on the first
  real symbol, thirty suites deep instead of at launch. The HEASoft release root joined the list and
  `check_os_jail_probe` now runs the configured `analyze_probe` **inside** the jail at launch. Two
  failure modes on purpose: a missing `bwrap` or denied user namespaces **warns**; a jail that
  breaks the science stack **fails** the launch.
  **The author became a parameter, so the model sweep is a benchmark rather than a config edit.**
  `build_authoring_session` resolved the model id one way, the author provider a second way for
  review routing, and `_build_backend` the harness member a third — one `coder: RoleModelConfig`
  parameter now carries all three facts (`_resolve_coder_provider` deleted, `_resolve_coder_model`
  reduced to an accessor), which is a simplification: the three could previously disagree. Harness dispatch is proven by *passing* members instead of monkeypatching ambient
  config. Second seam: `make_review_hook(..., allow_correlated=False)`, opt-in and set by no
  production caller, because the same-provider arm is otherwise inexpressible; every review under it
  emits `review.correlated`. The sweep lives in `benchmarks/author_bench` (the human's call): a *leg* is one (author member,
  reviewer arrangement) pair with its own K, concurrency ceiling and vendor brand colour; legs run
  sequentially with trials concurrent inside, and the single-leg report stays **byte-identical** to
  the one already in the paper. **Two structural rulings from the roster:** a tier row **cannot** be an
  OpenAI author (the tier axis needs the reviewer held fixed and the configured co-lead is OpenAI,
  so such a row would vary two things at once — it appears instead as a deliberately correlated
  row); and the decorrelation panel **holds the author at the campaign default** and moves only the
  reviewer, which makes the existing K=10 batch its cross-provider arm for free, with
  `claude-opus-5` the pinned same-provider reviewer because the reviewer swap found it strongest.
  The **Qwen row is absent, not substituted**, with the reason recorded in code. **The finding: the
  Codex author never consulted the key pool** — it resolved its vendor key from the raw environment
  at two places, so it always launched on the *primary* even after a chat role had rotated off a
  spent one, worse than the §13 note that a session in flight keeps its key. One shared
  `resolve_vendor_key` now reads the pool for a rotating provider naming that provider's standard
  primary variable, and the raw variable otherwise (a plan-specific variable must not be shadowed;
  a harness-only vendor has no cited taxonomy so it never rotates), returning a **log-safe slot
  name**. The per-leg probe uses the same resolver *and fails over*, because the pool only advances
  reactively. **Human ruling on cost reporting:** the paper states one list-price USD figure with no
  billed-versus-notional split, and the Claude coder is *not* moved onto an API key to make its
  spend real, because it is too expensive to bill that way.
  **The jail then met its first author and needed five fixes**, all at startup, all for $0 — the
  only reason it was cheap. (1) The launcher **never bound the binary it execs** (both harnesses
  ship their CLI inside the orchestration environment, a tree `agent_fs_allow` deliberately does not
  name), so `bwrap` failed `execvp` on all 40 trials. (2) **Bind and exec disagreed on the path
  spelling** — the bind resolved symlinks, the exec did not — and the launch check resolved as well,
  so it reported "reachable" while the launcher failed: *a check that normalises differently from
  the thing it checks is not a check.* (3) Codex was pointed at the operator's own settings
  directory, absent from the jail. (4) Both CLIs keep state in **SQLite** and the runtime dir is an
  **NFS** mount where it cannot take locks, so author homes moved to local disk. (5) A label derived
  from the clone's *directory name* collided across every concurrent author. **The structural one
  was last: this harness's own sandbox IS bubblewrap**, so jailing it nests the same mechanism —
  Codex cannot spawn `codex-linux-sandbox`, every shell command is rejected, and the author reaches
  the gate having read and written nothing, which reads as a weak model rather than as broken
  plumbing. The coder diagnosed it itself and escalated to the lead, the §9.6 path behaving as
  designed. **Human ruling: one enforcement layer.** The harness sandbox stands down when the kernel
  jail is on, and `coder_policy` tightens the author to two writable places, which is *stricter*
  than the `workspace-write` surrendered: the observation database is read-only, so no author can
  write a data file where a fabricated result would have to live, and the temp root is absent
  entirely so one author cannot read another's solution — a contamination that would have
  invalidated a repeated-trial measurement rather than broken anything visibly. **Live-verified end
  to end**: two concurrent authors per harness starting jailed, the Claude pair authenticating from
  the subscription, and a full production trial authoring both toolkit halves plus tests and passing
  the deterministic gate on its second submission. **Two process lessons.** A hand-built probe is
  not the production path (mine failed with the jail *off* too, the only reason I did not read its
  failure as a code defect). And the config-flip lesson recurred twice in one session — two tests
  asserted against whatever `agent_os_jail` happened to be on this host, and both now sweep it.
  Suite 967 → 1176. **Open:** egress control for the three processes that keep the network; turning
  the flag on by default. **NOT LIVE TESTED at the time:** a real authoring attempt on the Codex
  harness (the probe exercised the harness, not the flow).

- 2026-08-24 -> 2026-08-25 — **Six chat vendors, two routes to a structured answer, and role
  eligibility decided per role.** §8.5 of the paper DOCS found the coder sweep varied the reviewer's
  provider and the lead's together, because the lead was Anthropic in all five arms; separating them
  needs leads and co-leads from more than two vendors. DeepSeek, Z.AI and Alibaba each already
  publish an OpenAI **Responses** endpoint, so this was a factoring, not three new adapters:
  `providers/_responses.py` holds the wire logic and each vendor module holds a `ResponsesVendor` of
  documented facts beside its citations, mirroring how `_media.py` splits mechanics from
  per-provider policy. Copying 540 lines written against three run-killers in one week would have
  forked those fixes four ways. `CHAT_PROVIDERS` is now all six.
  **Every flag was read from the vendor's docs and then probed, because a compatibility table says
  what is *accepted*, not what is *honoured*** — and that decided who may hold a role. **DeepSeek**
  honours a strict schema exactly (**11/11 live** on all nine real schemas plus both trajectory
  composes). **Z.AI accepts `text.format` and ignores it**, answering once with prose carrying no
  JSON and once with a fenced object whose keys the schema never declared; a 200 with invented keys
  cannot be caught downstream, so `supports_strict_schema=False` refuses the request with a reason
  instead. Its documented workaround (schema in the prompt) returned exact keys once, so nothing is
  wired on one draw (its forced-tool route later measured **12/12 live** on the real schemas, one
  transient vendor auth blip passing on retry). DeepSeek also rejects `prompt_cache_key`. **Two defects only a live probe could
  find:** DeepSeek's strict mode accepts a **narrower `format` set** and enumerates it in the
  refusal (`email`, `hostname`, `ipv4`, `ipv6`, `uuid`), which refused three of nine schemas for a
  date field; and its validator **does not follow a `$ref` inside an `anyOf` branch**, so every
  `X | None` broke — the cheap fix merely moved the error to "An object with no properties is not
  allowed", which is what proves the pointer is never resolved.
  **A structured answer now has two routes.** The human's insight found the way through: **a coding
  harness never uses structured output** — Claude Code's and Codex's own editing tools are
  JSON-Schema functions, so any vendor that works inside those harnesses must handle schema-shaped
  tool arguments. `ResponsesVendor.schema_via_tool` declares one synthetic tool whose parameters
  *are* the schema and pins `tool_choice` to it; the loop's own tools stay declared beside it, and
  the emit call is consumed into `parsed`, never surfaced as a `tool_call` the ReAct loop would
  execute. A vendor that declines the forced call raises rather than returning a quiet
  `parsed=None`. **Two of my own defensive choices were wrong and were corrected by measurement:**
  DeepSeek's narrow `format` allowlist had been applied to Z.AI and Qwen as well, but that narrowing
  is imposed by *DeepSeek's* validator, which they never reach — *enforce what a vendor refuses,
  never what another one does*. **One measured quirk:** GLM intermittently fills an optional field
  with the *string* `"null"`, schema-valid against a `string` branch and a type error for anything
  else, and it reaches the authoring path (`AuthoringDecision` carries two optional floats).
  `_normalise_null_strings` coerces a literal `"null"` to `None` **only** where the schema permits
  null and no branch accepts a plain string — it normalises spelling, never content — and exact
  constrained strings (enum/const/pattern `"null"`) keep their data value.
  **"Cannot resolve a `$ref`" was never one property, and the boolean encoding it is deleted.**
  Prompted by the human asking whether we had actually *tried* sending references to each vendor —
  we had not; Z.AI had inlining enabled purely by analogy and was never tested. Measured at 5 draws
  per cell, every cell 0/5 or 5/5, no variance: **DeepSeek** refuses `anyOf:[{$ref},null]` 0/5 but
  takes `items:{$ref}` 5/5; **Z.AI** takes both 5/5; **Qwen** takes `anyOf` 5/5 and refuses `items`
  0/5. **Qwen forced the redesign:** it needs opposite treatment in the two positions, so a
  per-vendor boolean is wrong whichever way it is set — and blanket dereferencing was *causing* its
  `anyOf` failures (a working 5/5 shape driven to 0/5). `inline_anyof_refs` and the blanket inliner
  are gone; `resolve_refs_in` names the positions a vendor is *measured* to need, defaulting to
  none; Z.AI's shim was **deleted rather than switched off**, per the human's instruction.
  DeepSeek's rule, probed six ways: every `anyOf` branch must carry a literal `type`, checked
  *before* any reference is followed. **No dependency was added** — `jsonref` does blanket
  dereferencing, which the data says is wrong for Qwen. **Two process notes:** a closed third-party
  issue was cited as support when a live measurement of our own request already existed — *the probe
  was always the authority, and the human was right to force the re-check*; and writing the
  position-aware resolver **reintroduced the same unbounded-recursion bug fixed an hour earlier**,
  caught by its own guard test, not by review.
  **Qwen came alive, and the last defect was ours.** Activating Model Studio cleared the entitlement
  gate. `ResearchReport` and `AuthoringDecision` failed at *every* reasoning effort (10/11/8 errors),
  always at `figures.0`/`references.0`/`sections.0` — the `list[Model]` fields — which showed the cause was
  structural: our inliner rewrote `$ref` only inside `anyOf`, so list-item schemas stayed unresolved
  and the model **invented** a shape (a made-up `figure_id`, none of the required fields).
  Dereferencing the whole schema and dropping `$defs` fixes it. **The durable lesson: the two compat
  vendors fail the same way in opposite shapes** — DeepSeek *rejects* an unresolved reference (HTTP
  400, found in minutes); Qwen *accepts* and fabricates a plausible object (HTTP 200, found only
  because Pydantic rejected it downstream). *A rejection announces itself; an invention looks like
  an answer.* It is the argument for keeping strict validation at the role layer rather than
  trusting a 200. **Two corrections the human's questions forced:** `structured_retry_cap: 2`
  already retries off-schema output at the role layer, and the live tests sit *below* that loop, so
  they measure raw single-shot behaviour and made Qwen look worse than a real run will; and thinking
  was never disabled — `reasoning.effort: none` is strictly worse (it breaks `AuthoringDecision`),
  so the right trade is keeping thinking and *offering* the emit tool. Qwen refuses a named
  `tool_choice` while thinking — **the same rule Anthropic documents** — so the emit tool is
  declared under `auto` with a final instruction that the call IS the answer; a prompt ending "do
  not call any tool" would forbid the answer itself. **Qwen's credentials had to be understood
  first:** the key class decides the host, an `sk-mr-` Coding Plan key is valid only on
  `coding-intl` where `/responses` **404s** (so that plan can drive neither harness nor adapter),
  and only a pay-as-you-go key on `dashscope-intl` serves us; `tmp_scripts/check_qwen_entitlement.py`
  sweeps every documented host. `harness.py`'s endpoint was correct all along and its doubt is
  retracted. **Live: 55 passed / 2 skipped / 0 failed** across all five vendors; the schema matrix ran 36 with
  1 skipped and tool search 7/7 on the day the compat vendors were wired.
  **Live tests moved onto `ModelHandle`** rather than `provider.call`: both frontier primaries were
  out of credit that day and 22 legs failed against working fallback keys, because key rotation
  lives in the layer those tests skipped — which also closes the 2026-08-06 altitude gap.
  **2026-08-25, role eligibility decided per role instead of per tier.** §6 had one blanket clause,
  "reviewer-tier and analyst models must be vision-capable", which is a tier statement about a
  per-role fact. Audited against the call sites: three roles are sent figures (reviewer, analyst
  lead, analyst worker) and the **analyst co-lead is not, by construction** — `review_tool` passes
  no images and its tool set is read-file/list-dir/web-search with the knowledge-base tools off. Two
  smaller findings: the `coder`'s `vision: true` is dead config (an author is a harness subprocess,
  never a `ModelHandle`), and the flag is a **declaration, not a guard** — it gates the call-time
  `images=` channel only, not figures arriving on a tool result ([V-2]). **The DeepSeek exclusion
  was right and its stated reason was wrong:** the vendor's Responses guide lists `deepseek-v4-pro`
  and its vision guide documents the vision model with exactly the limits that had been deleted.
  **Human ruling: the code stands** (`images_accepted=False`), because the project declines a
  multimodal path its vendor labels experimental and will revisit on a non-experimental one — but
  the *justification* is replaced, since "the vendor has no vision model" is a fact a later reader
  would act on and would be wrong. **The recurring rule at a new address: read *every* page the
  vendor publishes for the capability, not the one already open.** **The grid** agreed this session
  is a **4 x 5 lead x co-lead grid at a pinned author** (`deepseek-v4-flash` on Codex, K=5, 100
  trials): leads Anthropic/OpenAI/Z.AI/Alibaba, co-leads those four plus DeepSeek (which reviews
  without vision on `deepseek-v4-pro`); Google out of both axes on **[I8]**. Because DeepSeek
  authors but cannot lead, the author-correlated and lead-correlated cells are fully crossed and
  never coincide — the four arms §8.5 says do not exist — at the cost of the doubly-correlated cell
  being structurally unreachable, so **no interaction is estimable**. The four author-correlated
  cells run only through `make_review_hook(allow_correlated=True)` and each records
  `review.correlated`. **What the grid decides is §9.3 itself:** that rule binds the *author's*
  vendor and says nothing about the lead's, and the coder sweep's own data does not support the
  author as the operative axis — if the lead-correlated diagonal moves and the author-correlated
  column does not, the rule binds the wrong actor. **Qwen latency became a controlled live-test
  axis** rather than an inherited default: an 11-leg `qwen3.8-max` probe took 517.12 s against
  Sonnet's 78.14 s, output-token count explained 98.6% of it, and Alibaba's reference identified the
  omitted setting (`reasoning.effort` defaults to `medium`); the matrix now runs at explicit
  `minimal` for `qwen3.8-max` and `qwen3.7-plus`, with a focused sweep of `none`/`minimal`/`medium`.
  `none` stays a measured mechanical-call option rather than the full-matrix default because it
  previously broke `AuthoringDecision`. Qwen sends Alibaba's documented session-cache header
  (`x-dashscope-session-cache: enable`) on every Responses request, the vendor defaulting it off,
  and both schema probes pass `cache_prefix=True`. Responses clients disable the SDK's two hidden
  retries and cap one HTTP attempt at 300 s; `retry.py` remains the sole, visible retry owner. A
  Qwen terminal response that both emits structured data and requests a working tool is rejected as
  premature for bounded retry; a root `$ref` expands only in Pydantic's bare `$ref` + `$defs` form,
  preserving JSON Schema 2020-12 sibling constraints. Alibaba recommends workspace-specific domains
  for stability; member `base_url` is the deployment-time path once the workspace ID is known.
  **Open, awaiting a ruling:** whether GLM gets a row in the lead x co-lead matrix given that its
  structured path is unmeasured (handoff [I14]). Suite 1008 → 1081.

- 2026-08-31 — **Submission snapshots: an authoring attempt is durable from the moment it meets the
  gate.** Real-run ledgers show coding is the chunk that eats most time and money, and the
  checkpoint map had a hole exactly there: LangGraph resumes at node granularity, `approve_edit`
  commits are durable, the per-attempt base ref heals the branch — but everything between "coder
  starts" and "commit" was volatile, so a review-provider failure on a gate-passed tree cost a full
  re-authoring. `AuthoringSession.run_gate_step` now persists every submission (after the §9.9
  coder-commit backstop, before the scan) as a patch + verdict record under
  `run_dir/authoring/submissions/<tool>/`, failed submissions included; on resume,
  `build_authoring_session` restores the latest snapshot **only onto its exact recorded base ref**,
  seeds the coder with the restored tree and its outstanding feedback, and the gate re-runs on the
  restored tree — **no stored verdict is trusted**, `commit ⇔ gate ∧ review` stays defined over the
  live tree, `approve_edit` stays the sole commit path, and the placement follows the existing
  discipline (durable state under the run dir; tree, never process; resume re-enters at a boundary
  by re-running the step). `commit()` discards the tool's record; a snapshot that fails to apply is
  discarded — **an economy, never a dependency**. Same session: the swapfix batch's 8 review-stage
  errors were investigated (none internet/throttle: 2 were our `ReadOnlyToolProvider` bare-`int()`
  crash on a model-sent `offset='150, '`, fixed to an `is_error` tool result per the [F11] rule and
  re-run clean; 6 are known vendor structured-output shapes, now ruled to count as failing the item
  — `swap._review_errored` keeps them in the denominators). **LIVE-VERIFIED 2026-09-01**
  (`tests/test_live_snapshot_resume.py`): real coder + gate + co-lead on the frozen run-46ed6671
  brief; killed mid-review with no cleanup; heal → restore → the new session continued on the
  restored tree through two more gate-passed submissions and ended in an honest no-commit within the
  review cap. The full-run (`run_research(resume=True)`) leg of the §13 durable-resume list stays
  open; the authoring-attempt leg is closed.

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
  write time is gone — which is why they were measured rather than argued.
  **Third item: `tests/test_live_no_hitl.py`, the first live look at the unattended condition
  itself.** Every benchmark batch has run headless and nobody had watched a model meet that
  condition. Two halves matching where the asymmetry is: the graph roles have the tool
  **withheld**, so each is driven through its **real** ReAct loop with its real prompt and a
  stimulus built to make it want to ask, and what it does instead is recorded; the author has the
  tool **offered**, so it runs as three cells — unattended, channel fault, attended control —
  through the real `_make_ask_human_handler` and the real MCP boundary. Assertions stay on our
  side (tool inventory, which ledger events fired, that no cell records an answer with no human,
  and the **mirror regression** asserted against *what the author says it received*, so it covers
  the whole round trip rather than the handler). Model quality is never asserted — responses land
  under `logs/dev/no-hitl/` with derived signals beside them. The inventory half needs no tokens:
  headless, the three roles lose **exactly** `ask_human`/`plan_and_debate` and nothing else, which
  is what makes the two arms comparable. **LIVE-VERIFIED the same day, $12.52 over four runs, and
  the unattended path is concluded working** (human ruling: no further testing). Every role
  detected the closed channel unaided and said so in its own output — worker
  `AnalystReport.status=needs_human`, lead `should_author=false` with *"NO HUMAN CHANNEL … I
  explicitly do NOT claim any human debate, steer, or sign-off"*, reviewer `accepted=false` with
  *"no route to a person exists in this run"*, author routing to `ask_lead` — and **none
  fabricated**, the worker having surfaced the published `PD = 4.0 ± 0.2 %` by web search and
  labelled it *"literature context only, not a measurement made in this run."* The author leg
  passed first try with no rework and distinguished a `ConnectionError` outage from an absent
  human **unprompted**, which is the whole point of the classification. **Three probe defects cost
  three paid runs**, all in the code that *reads* results rather than the code under test: a
  4-step cap that captured intent instead of outcome; an extractor that walked past the assistant
  turns into the **system prompt** and recorded 17,520 characters of `analyst_lead.md` as the
  model's answer; and the design error behind both — hunting for a prose conclusion the pipeline
  never produces, when production ends a role with a **structured compose** into its own schema.
  Fixed by selecting turns by authorship, composing the real schema, and persisting the
  trajectory shape with every record; pinned by the new ungated
  `tests/test_no_hitl_probe_helpers.py` (12), whose first two cases are those live defects
  verbatim. **Lesson: a results *reader* needs its own tests, because one pointed at the wrong
  field looks exactly like a boring finding.** Stated limits: the stimuli are prose rather than
  real payloads so decision *quality* is unmeasured, and the lead ran with
  `authoring_session=None` so `should_author=false` is a triage verdict and **no authoring was
  attempted or possible** on that leg.
  **Fourth: the human ruling that governs the sweep's scoring** — *the pipeline always reaches a
  verdict; the point is what the verdict is.* It must never stall, above all as one step inside a
  larger orchestration: it always composes a report, and a verdict of "this result is not
  verifiable", cleanly stated, is a **correct** outcome. So a terminal `needs_human` /
  `status=failed` / `accepted=false` is a correct row rather than a failure row, and the question
  worth scoring is whether the report *says so plainly*. This also downgrades **[I7]** for this
  purpose: the probe's reviewer produced exactly its shape (`accepted=false` with no rework flags,
  `follow_up=null`), and under the ruling finalizing rather than looping is the desired behaviour —
  what remains is the narrower compose/prompt question of what that report states.
  Suite 1576 → 1615 (`test_coder_backends.py` 46 → 49, `test_env.py` 8 → 15, `test_prompts.py`
  15 → 23, new `test_live_no_hitl.py` 7, new `test_no_hitl_probe_helpers.py` 12,
  `test_no_undefined_names.py` 214 → 216).
  **Unrelated pre-existing failures observed and left alone** (§3: consult before fixing): nine
  `benchmarks/gate_bench/tests/test_gate_bench.py` cases fail on `.pytest_cache` files leaking
  into the synthetic-repo comparison.


- 2026-09-02 — **The analyst lead became one ReAct loop that fills in forms, because the old shape
  was what killed a run.** Block 0's third launch died at $19.43 on the lead answering the authoring
  question with `should_author=true` and an **empty** `tool_specs`, twice, and a Pydantic validator
  killing the run. The post-mortem found four channels, two silent and one actively misleading, and
  none of them a model defect: the lead was **six separate conversations** (each re-seeded, each
  ending in a structured compose, nothing carried between them); its harness tools at that moment
  were four read-only ones that said nothing about authoring; the schema it received marked the
  chain **optional** and carried **no field descriptions at all**, because every explanation in
  `schemas/toolspec.py` was a `#` comment and *a comment never reaches JSON Schema*; the payload was
  **27,208 characters of single-line JSON** in which the specification was 1,016 (3.7%) and the one
  prose rule was stated **only in the direction that did not bind** ("`should_author=false` when the
  worker mis-called an existing tool" — the lead's case was the opposite: the worker was right and
  the capability existed in `lib/` unexposed); and the generic compose tail said *"do not describe
  intentions or plans"* while the requested output **is** a plan. `target_domain` was required and
  appeared in no channel at all. **The human's ruling was architectural**: one large ReAct loop whose
  termination depends on the lead's own decision, where each turn files a form whose shape depends on
  the previous entries and every question carries a description. Built as `AnalystLeadAgent.run_cycle`
  — one loop, one conversation, **no output schema** — ending when the lead files `author_chain`,
  `hold_authoring`, `request_another_round`, `submit_adjudication` or `submit_terminal_report`;
  `adjudicate`/`judge_convergence`/`decide_authoring`/`finalize_authoring_outcome` are deleted with
  their composes (7 compose sites → 4), and `LeadLike` shrank to that one method. **Forms are tools,
  not a branching output schema**, for four reasons in order of weight: a tool `input_schema`'s
  properties carry `description` and providers render them (this is the channel that fixes the bug);
  conditional required-fields in JSON Schema is not portable across the six vendors; the project had
  already settled that tools are the portable route (`ResponsesVendor.schema_via_tool`, on the
  insight that a coding harness never uses structured output because its editing tools *are*
  JSON-Schema functions); and it deletes the `tool_choice="none"` terminal compose, the request shape
  behind two historical run-killers. Four properties carry the design: **the impossible state is
  inexpressible** (`minItems: 1` — the [A12] move of removing a dependency on a free-filled field
  rather than validating it); **every question carries its own description**, read from the Pydantic
  `Field(description=...)` of the model it validates back into, so the worker's and the lead's view
  of one object cannot drift; **declarations never shrink** (the drive tools are gated on
  `cycle.authoring_enabled`, never on a live session, so swapping the session per chain link is
  invisible to the declared set — the 2026-07-29 `Tool reference … not found` killer), with the
  stage enforced by the **handler** returning `is_error`, which `_react` treats as a correction
  rather than a finish; and **caps stay in Python** (`rounds_left` passed in, `request_another_round`
  refused at zero with the cap named). **The coding middle is dropped at its own boundary**: after
  each chain link the `drive_coder` transcript between the filed brief and the `submit_for_review`
  result is removed — whole turns only (Anthropic documents *echo the assistant message exactly as
  received*), no orphaned `tool_use`, and **nothing synthesized**, since both survivors are turns
  the lead produced or received; a synthesized assistant turn would write content its apparent
  author never wrote, the shape rejected on 2026-09-01. Safe for this role specifically because
  `ReadOnlyToolProvider` sets `defer_loading` on **nothing**, so no dropped turn can de-activate a
  discovered tool (§13 watch item (a)); the measurement that motivated it is the lead clearing
  **~86,000 tokens every turn, 18 times** in one authoring phase. An authoring cycle **ends before
  judging**, because a committed tool is evidence the current reports predate — the workers rerun
  once and the lead judges a fresh set in the next cycle (A8), and credits spent with nothing
  committed **skips** that rerun (nothing to load; re-running only re-surfaces the same gap), so the
  §9.6 honest terminal is the lead filing `submit_terminal_report` rather than a separate
  conversation. **`drive_authoring` deliberately stays** — `benchmarks/author_bench` measures
  exactly that call and its published K=10 batches priced it, so deleting it would silently change
  a published instrument. **The one design risk was probed before any form was written and was not
  there** (`tmp_scripts/probe_nested_form.py`, cents, routed through `ModelHandle`): all five
  vendors accept a tool whose `input_schema` nests an object array and fill ≥2 links with every
  required field — the 2026-08-24 Qwen `items: {$ref}` refusal applies to **output** schemas only,
  and there is no `"null"`-string quirk on this path — and **all five classified `disposition`
  correctly unprompted**, which is direct evidence the launch-3 failure was *informational, not a
  model limitation*. **LIVE-VERIFIED 2026-09-02** (`tests/test_live_lead_cycle.py`, stimulus frozen
  verbatim out of that launch's own ledger rather than written for the test): the Opus lead ran a
  real 9-call cycle on the real `prompts/analyst_lead.md` through `ModelHandle`, its catalog figures
  (2683 px — above Anthropic's many-image dimension cap, so the media guard was genuinely exercised)
  rode the first request, and it filed `request_another_round` with a dispatch text naming the tools
  that **already exist** — the launch-3 diagnosis, reached by the lead itself. Also: the two
  remaining `.pytest_cache` `gate_bench` failures are unchanged and still untouched (§3). Suite
  1399 → 1663; new `tests/test_lead_forms.py` (41), `tests/test_schema_descriptions.py` (19),
  `tests/test_live_lead_cycle.py` (3 cells), and the eleven behavioural tests in
  `tests/test_consensus_authoring.py` pass **unchanged in intent**.

- 2026-09-02 (third, same day) — **Six plumbing fixes from block 0 attempt 5, and two of them were
  limits measured in the wrong unit.** The attempt authored, gated, co-lead-approved and committed
  two tools in 6h50m/$61.01 and still ended `status: failed`, so the whole batch of defects came
  from one run's evidence rather than from speculation. **[1] The rerun follows the commit count,
  not the filed form** — my own regression from the forms refactor. `submit_terminal_report` is
  reachable from `AUTHOR_STAGE` on §9.6 grounds, and filing it there returned `authored_chain=False`,
  so `_resume_after_authoring` never ran: no `registry.reload()`, no `sandbox.restart()`, no worker
  rerun. Two committed tools then returned `[exit 127] Tool not registered` to the workers the
  reviewer sent back to use them. §9.6 and [A11] met at a stage §9.6 was opened into, and **§9.6 won
  by control flow rather than by decision**; `strategy.run` now branches on `authored_chain or
  committed >= 1`, so an honest terminal from the authoring stage ends the **chain**, never the
  analysis. Bounded because each pass needs a commit and every commit spends an author credit.
  **[2] An approved commit becomes the attempt's base ref** — pre-existing since `c2253d5`
  (2026-06-30), not from the refactor, which merely multiplies it N-ways over one conversation.
  `commit()` never advanced `_base_ref`, and three readers took the stale value: the §9.9
  gate-entry backstop reverts on a moved HEAD without consulting `self.committed`, so **a
  `submit_for_review` after a successful commit reverted that commit** (live: `1523349` → reverted →
  `49bbc53` → reverted → `da507d7`, three full gate+review revolutions at ~$0.71 each, self-limiting
  only because the next link re-recorded the base ref); `aclose`'s failure reset would reset *behind*
  a legitimate commit; and `heal_toolkit_branch_if_dirty` does a `reset --hard` to the **persisted**
  value, so a hard kill after a commit would have healed the tool off the branch. Advancing both the
  in-memory and the on-disk value fixes all three at once — guarding the revert with
  `if not self.committed` fixes one and leaves two wrong. **And advancing it alone was worse than
  the bug**, which the tests as first written did not see: with the revert gone, a post-commit
  submit scans an *empty* changeset and then asks `approve_edit` to `git commit` with nothing
  staged — a non-zero exit raising straight out of the tool call, and `_react` does not catch tool
  exceptions ([F6]). Found by replaying the ledger's real sequence through `execute_submit`
  instead of calling `run_gate_step` directly, on the user's question of whether the tests
  simulated the actual trajectory. New `AuthoringSession.pending_changes()`; `execute_submit`
  answers a no-op submit with "already committed, nothing has changed", **budget-free** (the live
  run spent two of five submissions on this), keyed on *nothing changed* rather than *already
  committed* so a genuine follow-up still lands. **[3] The media budget reserves the
  non-media request weight.** The run died at its last node on `413 request_too_large` with **zero
  media-guard events**, and the measurement was the finding: two PDFs came to **33,512,296 encoded
  bytes against the 33,554,432 ceiling**, i.e. the guard stopped with **42 KB** of headroom for a
  conversation orders of magnitude larger. Anthropic states the rule for both document limits —
  *"Both limits are on the entire request payload, including any other content sent alongside PDFs"*
  (.../build-with-claude/pdf-support) — so the ceiling was being applied to a subset of what counts
  toward it. New `_media.nonmedia_weight` estimates message text, tool-call arguments, tool-result
  text, every verbatim provider part and the declared tool schemas plus a flat per-part JSON
  allowance; all three adapters pass it as `reserved_bytes`. Deliberately an estimate over the
  *neutral* conversation, because the payload cannot be built until the plan exists; it rounds up,
  since under-reserving costs the request and over-reserving withholds one figure with a note. **No
  per-document byte cap was added**, and that is a reading of the docs rather than a preference: the
  PDF page publishes only the whole-request ceiling and the page count, so a per-file limit would be
  a number we invented — which `MediaLimits` forbids by construction. Claude Code has the same
  defect, filed as claude-code#56691 (*"token context budgets and serialized request-body bytes are
  decoupled"*) and **closed as not planned**; several sibling reports show a 413 driving an infinite
  loop, where we at least die loudly. **[4] A negated banned word is a judged finding, and the
  scoping rule was measured rather than argued.** Across every production ledger on disk, **4 of 27
  gate runs failed and all 4 were vocabulary failures** — every sampled instance a negation, e.g.
  *"The module contains no table of published values … the tool can never back-fill or fabricate a
  benchmark"*. Nothing else has ever hard-failed Layer A in production, so the only thing the
  deterministic vocabulary tier had ever caught was code stating the discipline it enforces, at
  roughly one submission per authoring run. The user's question — *does this void previous
  `gate_bench` results?* — was the right one and the answer was yes for the rule first proposed:
  a **token-window** negation cue demotes `fab_shell_script` (`# where the reduction has not run
  yet, emit fake data for now`), one of the six mutants the published catch-rate table attributes
  to the scan, because the negation governs a different clause than the banned phrase. A
  **clause-scoped** cue separates them; `tmp_scripts/gate_bench_negation_rescan.py` re-scans the
  whole 134-entry corpus offline (LLM-free, $0) and reports **0 moved** under the shipped predicate
  and **1 moved under `--naive`** — the second half being what proves the check can see a move at
  all, rather than being a probe that cries nothing. `prompts/coder.md` already warned against
  writing such a disclaimer, so this is the suspenders half catching what the belt did not.
  **Run over the six patches the attempt actually left on disk, the rule fires three times and the
  honest reading is mixed:** two `placeholder` occurrences in README prose are demoted and one in a
  manifest description string stays hard — so that submission still hard-fails and no real verdict
  moves — but **both demotions are coincidental**, the negation governing the *condition* (*"only
  when NEITHER … exist does the annotated placeholder panel appear"*) rather than the banned word.
  That is the `fab_shell_script` shape with no comma to separate the clauses, i.e. the rule is crude
  in both directions, which is what makes the safe-direction argument load-bearing rather than
  decorative. A word-distance cut would separate all six observed cases at ≤5 words and was
  **rejected as fitting a threshold to six points**. Pinned verbatim in `tests/test_gate.py`. None
  of the three occurrences was ever gated — they entered the tree after that link's fifth
  submission, consistent with all five of its gate runs passing.
  **[5] One link is one capability, one commit** (prompt-level) — **and the first version of this
  fix was aimed at the wrong actor.** It was written from the diff and the review issues; reading
  the `author_chain` arguments the lead actually filed reversed both attributions. The cache was
  **in link 1's brief** (*"Region selection (cached)"*, sidecar design spelled out), not bolted on
  by a mid-authoring steer; and `benchmark_compare` was **in link 2's brief** as *"### Function 2"*
  with a full signature, so the coder built exactly what it was asked rather than smuggling a tool
  in. The costs re-verified and stand: two of link 1's three rejections are that cache's
  invalidation bugs (stale across regions, then validity never bound to the Level-2 inputs — the
  same feature one level deeper each time), link 2's single rejection is
  `test_repro_benchmark_compare` missing because §9.4's obligation attaches to a tool deriving
  significance values, and one commit (`da507d7`) landed two tools, weakening §9.9's
  one-tool-one-commit-one-clean-revert. So the remedy moved from authoring time to **planning**
  time: `analyst_lead.md` states the rule in the chain-planning paragraph, where a helper, a cache
  or a comparison step is simply the next link. The coder-side edit was **rewritten rather than
  kept** — "ship one capability" would have told it to refuse a two-function brief, inverting the
  standing rule that the brief is a settled plan to execute; it now says everything in the diff
  traces to the brief or a relayed review, and reserves `ask_lead` for a capability nobody asked
  for. The gate's structural check verifies a manifest entry *exists*, never that the submission is
  only the brief, and the co-lead notices only by *inheriting* the extra capability's obligations —
  so a multi-capability link is priced as review cycles and never named. Also folded in: for
  any tool that reuses, caches or overlays an artifact, `proposed_checks` must require that a
  **foreign but plausible** artifact is rejected (same group, same binning, different observation)
  — precisely the case both of that run's too-weak bindings passed on their first fix. No gate-side
  scope check is proposed: a legitimate review fix also changes the diff, so one cannot be specified
  honestly. **[6] Every recorded commit is checked against the repository.** `trial.
  commit_reachability` cross-checks each ledger `commit` SHA against `git rev-list --all` in the
  trial's private checkout before teardown. [2] reverted three approved commits and the ledger
  recorded a `commit` event for each, including SHAs no longer on the branch; it was invisible until
  the git log was read by hand, which is exactly the class a record-versus-repository check catches.
  It returns `(checked, unreachable)` rather than one list because the first draft failed in the
  silent direction, and pointing it at attempt 5's real ledger is what showed it: the private clone
  is swept at teardown, so a missing repository answered `[]` — a clean bill of health for the run
  that reverted two approved commits.
  **The theme, stated because it recurred four times in one run:** a limit expressed in a unit that
  does not bound what actually fails — a moved HEAD standing in for provenance, a PDF *count*
  standing in for bytes, an output cap consumed entirely by reasoning, a 1000-*line* window
  returning 159,668 characters. Every fix here except [1] and [6] is a unit correction.
  **Process note, and it is the reusable part.** Every fix here was tested and three of the tests
  were shaped by *my* model of the trajectory rather than by the run's own record. Asked whether
  they simulated the real one, reading the ledger back answered it three ways: [1]'s production
  event is `analyst.lead.cycle | filed=submit_terminal_report committed=2` — the fake's shape was
  right but its chain was one link where the real one was three with the third failing, now
  matched; [2]'s test called `run_gate_step` directly and so never met `execute_submit`, which is
  where the new crash lived; and [6] run against the real ledger returned a clean `[]` because the
  checkout it needed had been swept. **Two of the three defects were in the code that *reads* a
  result, not in the code under test** — the same shape as the 2026-09-01 probe-helper lesson, and
  the reason a ledger replay is worth more than a hand-built fake even when the fake is faithful.
  **Extending the same check to [3], [4] and [5] found three more things, and the last is the one
  that matters.** [3]: the verbatim prompt dump holds the exact request that 413'd, and against it
  the reserve is 81,787 bytes where the guard had left 42,136 of headroom, so the fix does prevent
  that 413 — and the reserve is **1.13x** the true 72,124-byte wire payload, i.e. it rounds up as
  documented. (A first comparison said 0.92x; it measured against the *dump's* serialization, which
  carries `raw`/`usage`/`parsed` fields no request ever sends. Recorded because the wrong number
  nearly produced a correction to code that was right.) [4]: the rule's two firings on real diffs
  are both coincidental, above. [5]: **reading the filed briefs reversed both of its attributions**
  — see [5]. And a fourth, unrelated to the six: the lead filed `author_chain` with `tool_specs`
  **absent**, so §9.3's "inexpressible" claim about `minItems: 1` is false; a provider does not
  enforce it on a tool `input_schema`, the handler's `is_error` is what held, and the lead recovered
  on its next turn. §9.3 and the wording around it are corrected.
  **Residual, recorded in §13:** with the reserve in place eviction actually fires, and it is
  oldest-first, so a single near-ceiling document now survives while several small ones are dropped
  to make room for it — better than a dead request, but a policy nobody has measured. **18 new
  deterministic tests**, each of the five code fixes verified to fail against the unfixed code
  before being kept: `tests/test_consensus_authoring.py` 11 → 13, `tests/test_merge_flow.py`
  41 → 45, `tests/test_media_guards.py` 8 → 12, `tests/test_gate.py` 52 → 56,
  `benchmarks/science_bench/tests/test_science_bench.py` 108 → 110. Full suite **1703 passed /
  159 skipped / 9 failed**, the nine being the pre-existing `.pytest_cache` leak into
  `gate_bench`'s synthetic-repo comparison, unchanged and still untouched (§3).

- 2026-09-03 — **Two audits re-derived, and both prior conclusions were undersized.** Working tree
  reset to `a82cec8` first; every change here is derived from that commit (human ruling: only
  trivial changes outside core logic; the prior day's jail/guard/allow-list narrowing was
  discarded untested). **Ledger audit** of attempt 6 (`fbb93f26`, 5437 events) by eleven
  independent chunk auditors, decisive claims re-verified against the raw ledger: (1) "the coder
  read a model string" understated it — seq 4007–4045 gave the coder the lead's *and both workers'*
  models from `monitor.jsonl`/`backend.log` content; (2) "the co-lead tried `CLAUDE.md` twice" was
  one refused `AGENTS.md` read (seq 4264–4265); (3) new: the coder's active hunt for the
  orchestration repo and config (seq 4031–4042) was stopped by the jail — the one §4 success in
  the run — and its shell carried `CLAUDE_CODE_EXECPATH`/`COPILOT_*` inherited from the launching
  terminal (seq 922). Ruling: `runtime_dir` stays on every allow-list (agents may read their own
  logs); the coder channel is measured, not closed. Applied: `merge_flow._author_env` drops
  `CLAUDE*`/`COPILOT*`/`VSCODE*`; `workspace.round_report_path` drops the model slug; the three
  lead tool descriptions and `analyst_lead.md` stop naming vendors (`test_prompts` guards every
  `*_tool_def` and every runtime prompt); `science_bench`'s scanner walks the parsed record and
  skips opaque keys and base64 blobs (11 → 1 reported leaks on the real dump, the real one kept).
  **gate_bench test review** (Opus auditor, mechanism reproduced first-hand): the pruning step ran
  pytest with its cache on, so every pruned defect worktree carried `.pytest_cache/v/cache/
  lastfailed` naming the honest check the plant breaks, and no control did — a sixth prompt
  channel the five-channel audit never enumerated, and the earlier `_TOOLING_ARTIFACT` filter in
  the pair tests had made the guards blind to exactly it. **The guard hid the leak.** Used live by
  `qwen3_8_max` in two catches; scope and the open re-run are in §13. Fixed: `-p no:cacheprovider`
  + `PYTHONDONTWRITEBYTECODE=1` in `keep_only_passing_tests`, and a new pair test comparing the
  full defect/control listings with **no** artifact filter (what `list_dir` returns) across all 25
  matched pairs. Test corrections: the one-anchor pair test deleted (false by construction for
  pruning families, verified against `keep_only_passing_tests`); `_NOT_BEHAVIOURAL` gains
  `led_weighted_residuals_as_sample_variance` (fixture residuals `[-9,-3,3,9]` sum to zero, so the
  arithmetic error cancels — verified) and loses `hole_g4_provenance_dict` (it **does** fail the
  honest suite; the auditor's same claim about `hole_g6_manifest_import` was wrong — the control's
  suite passes against it, 15/15, so it stays declared); the two hand-rolled artifact filters
  unified. gate_bench 227 → 274 passed. The live 413 probe (`test_live_oversized_pdf_request_that_used_to_413`) is
  re-added verbatim. Full suite 1709 → **1752 passed / 160 skipped / 0 failed**. **Process:** two inherited diagnoses
  went wrong the same way — a filter or a summary standing in for the artifact — and both were
  settled by reading the record itself (the ledger; a freshly built worktree). §13 gains five
  entries.

- 2026-09-08 — **The science sweep became a harness-capability demonstration, any model can now be
  put in any seat for any live test, and the scoring pass found three reader defects by being
  pointed at a real artifact.**
  **The reframe (user ruling).** The 11-slot screening array is superseded by six runs: the
  incumbent seat assignment three times (attempt 6 is its first, by hash rather than bookkeeping)
  and one contrast assignment three times. Two reasons, and the second is the load-bearing one.
  Nine arms at n=1 with 2 df of pure error cannot carry a seat effect at any price — the published
  floor for a stable per-cell estimate on agentic tasks is **8-16 trials** (arXiv 2512.06710,
  ICC convergence; n>=32 for complex reasoning) — and the claim was never a per-seat effect: it is
  that **a tree of visibly weaker models still reaches a verifiable result or declines cleanly**,
  because the gate, the cross-provider tool review and the honest-failure floor carry the load
  rather than the models' judgment. An existence claim is demonstrated, not estimated, and it is
  §9.1's own thesis rather than a new one. Cost falls from order $800-2,000 to order $300-600. The
  array is **kept** (`--block1`, balance still asserted) as the record of what was designed.
  **Grounded against the field first, and the grounding changed the wording.** "Harness
  engineering" is a live 2026 framing (OpenAI Feb-2026, Anthropic Nov-2025, LangChain Mar-2026
  "Agent = Model + Harness"), and the pro-harness evidence norm is *model fixed, harness varied,
  repeats stated* (arXiv 2606.08529: pre-registered, 5 models x 3 scaffolds, 3 attempts per
  question, a 28-point within-model swing). But two large-N papers cut the other way and a referee
  will cite them: **2604.18805** (>25,000 runs over 8 domains: base model 41.4% of explained
  variance, scaffold **1.5%**, "scaffold engineering alone cannot repair them") and **2608.14905**,
  our own anchor (failures recur across all 8 harness x model combinations). So the claim to make is
  the one neither can measure: the harness does not repair the deficit, it **converts** it into a
  rejected commit or a plainly stated non-verifiable verdict instead of a plausible wrong number.
  A survey claim that Confucius Code Agent (2512.10398) shows a weaker model beating a stronger one
  at 52.7 vs 52.0 was **not** verifiable in that paper's abstract and is not cited.
  **Seats chosen per seat, on independent boards, never on price** (user correction: "each seat
  values different abilities"). Lead `gpt-5.6-terra` (AA 42 vs the frontier band's 47-53,
  Terminal-Bench 4.0 21.5% vs 37-58%) because the lead's loop is the longest in the pipeline;
  reviewer `qwen3.8-max` (AA 40, CritPt 20 against Fable's 30, yet arena-vision #3 and tau3-banking
  #1) because that seat needs critique and figure reading and never sustains a long loop; workers
  `claude-sonnet-5` + `qwen3.7-plus` (38, 26). **Lowering reasoning effort was rejected as the
  lever**: effort is the campaign's pinned constant across both published batches, and a low-effort
  model fails by *quitting early* (`gpt-5.6-luna` low: 1.5% pass@1 at 12.5 steps) where a weaker
  model fails by thrashing — a different failure shape than the one under study. `claude-haiku-4-5`
  excluded by the user as infrequently updated; it also scores 0% on Terminal-Bench 4.0.
  **Any model in any seat, for any live test, through the configuration pointer.** Live tests that
  drive a real role resolve their model the way production does, so they measured whatever
  `models.yaml` named, and the alternative each reached for was a hand-built `ModelHandle` — which
  is the 2026-08-06 altitude mistake. New `tests/seats.py` writes a whole configuration directory
  with `models.yaml` overridden and the other three files copied **byte for byte**, and
  `tests/conftest.py` applies it session-wide from `--seats` / `AUTO_RESEARCH_SEATS`
  (`role=provider:model`, `|` between a role's members, `!novision`/`!nodocs`). Code under test
  then resolves the overridden seat through its ordinary path with the media guard, key rotation,
  retry and prompt loading all in front of it, knowing nothing about the mechanism. Three
  properties make pointing a *running* process elsewhere safe, and the third is a constraint on the
  helper rather than a fact about it: `load_registry()` holds no cache and resolves `models_yaml()`
  per call; the four file-reading caches key on the **resolved path**; and the caches that hold a
  *value* rather than a path (`_react_loop_cap`, `_structured_retry_cap`, the jail flags) are
  unaffected **only because orchestration is copied unchanged** — a helper that also edited a cap
  would be silently ineffective for anything already read once. Absent a seat spec the conftest is
  a no-op, which a test asserts by requesting the session fixture rather than reimplementing it.
  New `tests/test_seat_overrides.py` (27) and `tests/test_live_seat_probe.py` (3 cells, unrun):
  reviewer reads a paper (a number only the text carries **and** figure colours only a rendered
  crop can supply), lead sustains a real `run_cycle` and files a form, worker drives a real tool
  loop and sees a peer figure — per-actor capability, not stage coverage (the 2026-08-20 lesson).
  **Scoring built (steps 6-8), frozen before spend, no model in the loop.** `answer_key.json` (the
  fourteen paper values from the pipeline's own provenance-headed comparison CSV, which is what
  `main.tex` publishes, hashed), `extract.py`, `score.py`, `report.py`, and `--score DIR` for a $0
  offline pass. Four rules settled before a number was seen: signed z is the pipeline's own
  arithmetic (and agrees in magnitude with `compare_to_benchmark`'s fourteen published values on
  every row, a free oracle a test uses); **a polarization angle is periodic modulo 180 degrees** so
  its difference is wrapped, because otherwise a tree reporting the same angle as `+159.3` instead
  of `-20.7` scores as a hundred-sigma discrepancy for a convention choice; within tolerance is the
  key's own 3-sigma flag; and a **unit mismatch never counts as recovered**, being a different
  failure with a different cause. `useful` = >=10 of 14 (user). A tree that reports the published
  values back scores 14/14 and **fails** the trap, which a test pins.
  **Three defects found by pointing the code at the real artifact instead of a fixture, and all
  three were in the code that *reads* results.** (i) The extractor's window ran a fixed 420
  characters past a label and so spilled into the next list item: eight of fourteen quantities
  inherited a **neighbour's** error as a dual error, both genuine dual errors were missed, and every
  `(same artifact)` back-reference inherited the wrong file. Bounded now at the earliest of the next
  item, the next prescribed label, and the character cap. (ii) The unit alternation listed a bare
  `percent` ahead of `percent/keV`, so the PD(E) slope's unit was silently truncated — first-match
  wins, and the truncation hides exactly the unit error the field exists to catch. (iii)
  `summarise_ledger` read a worker's status at `data.status` where it lives at `data.report.status`,
  so attempt 6's nine worker reports came back as nine `None`s while the ledger held `ok` x4,
  `failed` x3, `needs_human` x2.
  **[A20] was implemented and its first version was inert on the very run it was written for.**
  The frozen rule says a terminal report filed by a cycle that committed is not a failure row.
  Keyed on the trial record's `measured.needs_human` — which is derived from *worker* statuses — it
  would never have fired, so `summarise_ledger` now records `lead_cycles` (the form filed, that
  cycle's commit count, the status filed) and the rule reads those: attempt 6 filed **four**
  terminal reports, three of them after a commit. The want-a-human count is left exactly as the
  ledger derived it and the correction is recorded as `terminals_relabelled`, because that count
  comes from `coder.ask_human*`/`hitl.*` events which the mislabelled terminal never emits (that
  ledger holds none at all) — subtracting would have erased a genuine question asked elsewhere in
  the same run. Same shape as the 2026-09-02 unit-correction theme: a signal standing in for
  something it does not bound.
  **Verified end to end against attempt 6:** all fourteen labels found and measured, values
  hand-checked against the report, 14/14 within 3 sigma, 10/10 on the non-approximate rows, mean
  |z| 1.00, PD mean signed z -1.39, PA max |z| 0.63, trap reproduced, `useful` true. New roster
  levels (`gpt-5.6-terra` lead, `w4` worker pair), `arms.pinned_parameters` made public, and
  `qwen3.7-plus` priced off the vendor's own page because litellm knows the model and prices it at
  **zero**, so without the entry that member bills nothing and its arm's cost is understated.
  Three `science_bench` tests were **corrected rather than loosened**: block 1's balance is a
  property of the three levels *it* uses, not of a roster that now also carries demonstration-only
  levels. Suite 1752 -> 1868 (`test_seat_overrides.py` 27, `test_scoring.py` 41,
  `test_no_undefined_names.py` 242, science_bench 112 -> 154).

- 2026-09-08 (second, same day) — **A live probe was measuring its own scaffolding, so role wiring
  now has one definition in the run path.** Found by the user's instruction to use the real
  pipeline's injectable seams rather than a hand-rolled version, and it was right.
  **The defect, and why it was worse than a wrong result.** The seat probe's worker cell built an
  `AnalystWorkerAgent` by hand and attached **no sandbox and no registry**. `analyze()` runs its
  inner ReAct tool loop only when both are present and otherwise falls back to a plain structured
  call, so that cell drove a worker that could not call a tool. Its live failure —
  `qwen3.7-plus` emitting a structured answer alongside `bash` calls, which `_responses.py`
  rejects as premature — is therefore **unattributable**: a model that will not drive a loop and a
  model that was never given one are indistinguishable from outside. The lead cell was wrong more
  quietly, building a `ReadOnlyToolProvider` that differed from production's by five arguments.
  Same class as the 2026-08-06 altitude lesson, one layer up: a test below the layer the defects
  live in measures the test.
  **The fix is one definition, called by the run path.** `graph/runtime.py` gains
  `readonly_tools_for(role, ...)` (the reviewer's and the lead's loops, which differ by three
  flags on purpose and refuse any other role rather than handing it a default),
  `readonly_roots_for(run_id, toolkit_root)`, and `attach_worker_runtime(workers, ...)` (identity,
  memory, sandbox, registry, stop-aborter). `run_research` calls all three instead of restating
  them; behaviour-preserving, with `include_plan_and_debate` now derived from whether a human
  handler exists rather than passed `True` and re-decided one layer down. Two incidental
  confirmations while wiring the probe to them: both roles load their own prompt, so the
  hand-passed `prompt=load_prompt(...)` was redundant, and sandbox teardown is `shutdown`, not
  `close`. New `tests/test_role_wiring_seams.py` (8), a **static** guard because correctness here
  cannot be asserted at runtime: the two constructors appear in the run path only inside their
  factory, `run_research` actually calls both (else the factories would keep passing while
  describing a role production no longer builds — the worst version, since a probe would then
  measure a fiction with a factory's authority), the probe hand-rolls neither, and the two loops
  still differ as documented.
  **Two claims I made about the blast radius were wrong, and both were checked rather than
  repeated.** "Six older live tests" came from a `head`-truncated grep: **nineteen** files
  construct that wiring, of which six are live tests (`test_live_agents`,
  `test_live_honest_failure`, `test_live_lead_cycle`, `test_live_loader_authoring` x3,
  `test_live_no_hitl` x2, `test_live_report_rounds`), one more live test builds sandboxes directly
  as its own subject (`test_live_sandbox_policy`), and the heavily-counted deterministic files
  (`test_react_tools` 22, `test_lead_forms` 21) are unit tests *of* the provider, where
  constructing it directly is the point. And "two of them back published measurements" is
  **false**: no number in the manuscript is backed by any live test — grepping `main.tex` and its
  evidence ledger for `test_live` returns nothing. The file that *does* back published numbers is
  not a test: `benchmarks/author_bench/trial.py`, behind Table 6 and Fig. 5, hand-rolls the lead's
  loop. Its comment claims it is wired as `runtime.py` wires it, and measuring both builds
  confirms it — identical five tools (`list_dir`, `read_file`, the data catalog, `web_fetch`,
  `web_search`) — so the published authoring numbers stand. It is left untouched: routing a
  published instrument through the new factory is behaviour-preserving on this evidence but is the
  human's call, not a side effect of a test fix.

- 2026-09-09 — **The 2026-09-08 demonstration batch, re-diagnosed from the ledgers and fixed.**
  Five independent forensic reads of the five slot ledgers, each blind to the prior notes, then
  every decisive claim re-derived against the source and the vendors' own docs. **Three of the
  prior file's five attributions were wrong**, and the correction mattered: the batch's defining
  finding is not plumbing in the contrast arm but one defect class that fired hardest in the
  **anchor** arm with an Opus lead — **the gate, the registry and the commit disagree about what
  "the code" is.** The gate and `ToolkitRegistry.reload` read the **working tree**; the commit
  reads the **index**. `commit ⇔ gate ∧ review` held in git in all five runs; the invariant that
  failed was never written down — *a tool a worker can call has been committed*. Slot 2's
  reviewer-**accepted** report took two of its four figures from a plotter that was never
  committed and was review-rejected three times; slot 3's equivalent produced a **scored**
  quantity; slot 4 committed a tool importing a symbol defined only in an uncommitted file, so
  that commit does not run on a clean checkout.
  **F1** — `aclose`'s reset guard moves from the `committed` latch (set once, never cleared) to
  `pending_changes()`, so a session that commits and keeps working resets the later, unbanked
  work. `committed` was then a boolean shadow of `last_commit_sha` and was deleted; that deletion
  **nearly reintroduced the [F6] run-killer**, because `execute_submit`'s [A17] guard read it as
  `getattr(session, "committed", False)` — a string name no attribute grep sees, whose `False`
  default silently meant "not committed" and led to `git commit` with nothing staged. Caught by
  the existing test. **Lesson: grep the string literal as well as the attribute.**
  **F2** — Layer A now names three jobs, and the third was previously **unfalsifiable**:
  `structural.allowed_paths` applied `_TOOLKIT_PKG_RE` to a list `collect_targets` had already
  filtered by it. New `collect_all_changes` (wide, unfiltered) backs `structural.changes_allowed`;
  human ruling made it an explicit **allow-list** of `*_utils`, which also closes the hole a
  protected deny-list would leave. `check_structural`'s new parameter **defaults to deriving the
  changeset**, so no caller can reintroduce the defect. `gate_bench` 274 passed, no verdict moved
  — but its `hole_structural_unfalsifiable` mutant was planted against exactly this hole and its
  recorded verdict names the co-lead as the only catcher, so that row is annotated as pre-F2.
  **F5** (absorbing the planned F4 and F6) — the terminal-compose architecture is gone.
  `ModelResponse.stop` normalises each vendor's own documented field, because **Anthropic's
  convention does not generalise**: DeepSeek reports an overloaded cluster as
  `insufficient_system_resource` and Z.AI a dropped inference as `network_error`, both *in the
  finish-reason field*, and the ecosystem maps the latter to `stop`. So the loop terminates on a
  **tool** (`turn_end`, validated, rejecting with the reason) and uses `stop` only to choose what
  to say to a silent turn. `_react`'s silent `break` becomes a bounded continuation — applied only
  where a terminal tool exists, after a looser first rule caused four regressions. The worker's
  fallback compose is **deleted**: it was the only path that sent a model the runtime-owned
  `figures`/`FigureRef` tree, which `_sanitize_strict` promoted to *required*, which is how four
  `placeholder_*` digests killed slot 5 one line before `model_copy` would have discarded them —
  not fabrication under pressure (3 errors in 54 tool results, zero rejections, the real PNGs on
  disk), the harness asked for it. Figures move out of tool-result messages into their own
  following turn, the arrangement Anthropic names as the cause of the 2-3-token empty response
  that killed slot 3. The reviewer authors `report.tex` by **editing** it, which also removes the
  structured-output path `qwen3.8-max` — the contrast arm's reviewer — is recorded as failing.
  **F9** — a failed link no longer ends the chain (it told the lead why instead, since burning
  all 12 credits is the ruled-intended behaviour); `AnalystReport.status` is agent-declared
  `succeeded`/`failed` defaulting to **failed**, because a report nobody wrote has not succeeded
  and the harness must not infer success from silence. `needs_human` retired from that schema.
  **F10** — `PathGuard` gains a separate writable set: reads stay broad (a role reads its own
  logs), writes narrow to the role's own work folder. The jail half is deliberately **not** done
  blind and is recorded as residual.
  **F7** — Alibaba returns HTTP **400** for a condition its own reference documents at 500/429;
  `is_transient_provider_error` read `status_code` first, so the string-`code` fallback was
  unreachable and slot 6 died in one attempt. Per-vendor body overrides, consulted only on the
  statuses a vendor is documented to mislabel, matched as vocabulary and never as a bare
  three-digit substring. Anthropic's spend-cap 429 added — to `exhausted_codes`, because the first
  attempt put it in the message markers where it was **inert**.
  **F8** — the lead's prompt was 20k characters with **zero** sections and a longest line of
  **2,144**, and the one-capability rule sat as that line's third sentence; two live runs violated
  it. Lead 13 `##` + 2 `###`, worker 8 `##`, every prompt's longest line ≤159, no clause lost
  (verified by a whitespace-normalised sentence diff against HEAD). The new guard's two halves are
  held to different bars on purpose: the line bound applies to every prompt because that is the
  defect with an incident behind it; sections only above 12k, because a lower threshold would be a
  style rule invented to catch a well-wrapped 7.7k prompt.
  Also **N1** (the Layer-B failure window kept the *tail*, which is the warnings summary — now
  head+tail and config-driven; slot 2 lost 3h28m and $16.26 to a traceback it was never shown),
  **N6** (an adapter raise after a billable response is now billed, via `billed_usage` on the
  exception so the retry contract's exception *type* is preserved), **N11** (author credits are
  per-analysis in a mutable holder, since the strategy is a frozen dataclass), **N4** and **N8**.
  **F3 was WITHDRAWN** by human ruling after review of the actual text: the ten answer-key values
  stay in the dispatched question, so the paper must state that "recovered N of 14" cannot alone
  distinguish measurement from transcription — the coherent-PD-low trap, `paper_value_claimed` and
  the four withheld quantities are what discriminate.
  **Process incident, recorded because the remedy is a habit.** Removing the superseded
  `compose_latex_report` I computed the span as "up to the next `async def`"; it was the last
  method in its class, so the span also deleted `class LibrarianIOAgent`, its docstring, its
  `role` and its **entire `__init__`**. My first repair, written from `DOCS.md`, restored the
  class and its methods but invented a docstring and missed `role` and `__init__` — the tree
  imported, so nothing told me, and I reported the damage as smaller than it was. Read-only git
  access then restored it verbatim **and** caught a second loss no test would have: the retired
  method's body carried [D6]'s mandated `amssymb` preamble (a missing package is a fatal compile
  that once cost a whole PDF) and the references no-fabrication clause, both now carried on the
  new payload. **A structural heuristic is not a boundary check, and reading my own diff is the
  audit** — `git diff -U0 | grep -E "^-\s*(async def|def|class) "` found the deletion set in one
  command. Plan and per-item detail in `tmp_fix_plan_20260909.md`.

- 2026-09-09 (second, same day) — **The coding live test was measuring the wrong driver, and its
  failure had the wrong cause written on it.** Two independent forensic reads of the run's ledger,
  every decisive claim re-derived by hand.
  **Root cause: the coder's credential ran out of credit and the harness delivered that to the lead
  as a successful reply.** At 21:50:41 the Claude-SDK session went fatal (`is_error: true`), cost
  frozen at `$5.088389` and never moving again; every later `drive_coder` returned the Claude Code
  CLI's own hardcoded billing string, `"Credit balance is too low"`, with **`is_error: false`**.
  `stop_reason: "stop_sequence"` and `num_turns: 1` on those turns are *constants of the CLI's
  synthetic API-error message builder*, so the discarded `is_error` was the only detector.
  `_drive_author` reads it into the monitor event and returns `"\n".join(chunks)` one line later
  (`claude_sdk.py:338-372`); `CoderBackend.drive` is typed `-> str`, so no layer below has an error
  channel, and `execute_drive_coder` takes the default `is_error=False`. The lead spent three drives
  and 3 of the run's 9.5 minutes steering a corpse at **$0**, then gave up, and `_authoring_result`
  recorded `"co-lead review unresolved: …"` — §9.6's honest failure with a **confidently
  mis-attributed cause**. The co-lead's PA-periodicity rejection was itself real and good, and the
  gate and review ran on a complete, current tree (reviewed snapshot and discarded patch
  byte-identical, md5 `8623872f…`); it just was not what decided the outcome. Two asymmetries worth
  keeping: `"credit balance"` is a first-class, key-retiring condition on the *chat* path
  (`retry.py:76`) and had none on the author path; and the **Codex backend does not have this gap**
  (`codex_sdk.py:519-533` emits `status`/`error` and injects `[harness error] the {provider} turn
  failed: {error}`, with a comment naming exactly this mode), so `author_bench/trial.py`'s
  `turns_failed`/`turns_rate_limited` counters — which read those fields — are structurally blind to
  a Claude-harness failed turn.
  **Second defect, same log: a Layer-B check can be inert and no layer says so.** At submit time the
  suite was `19 passed, 1 skipped`, and the skipped one was the single check comparing the new code
  against *independent existing toolkit code* — the one the lead had commissioned to replace a
  self-referential test. It skipped because the brief's own validation ObsID has one
  `pcube_timeseg` segment row and the coder's own guard needs ≥3. Layer B passes on
  `rc == 0 ∧ ≥1 PASSED` (`gate.py:689-701`), so the gate recorded `passed: true`, `detail: ""`,
  `failing_checks: []`. **Nothing bypassed the gate** — it truthfully reported that everything which
  *ran* passed. What no layer reports is *which tests ran*, and that set is chosen at runtime by the
  file under review. Recorded, not yet fixed.
  **Human ruling: fix the measurement, not the production code.** A test exists to judge whether the
  new production code is valid, so editing that code to make the test pass introduces unknowns
  exactly where the batch needs certainty. Accordingly the dead-session channel, the reason
  attribution, the `silent_turn` text and the discarded seed-drive summary are **recorded and
  deferred**, and the gap is confined: block 0's coder is the Codex harness, which already has the
  error channel. The one production change taken is the jail's, below.
  **The test was rebuilt on the production seam.** `test_sequential_chain_authoring_end_to_end`
  looped the chain itself and called `drive_authoring` per spec — a driver production no longer
  uses — so three behaviours were unobservable: F9's chain continuation past a failed link, [A20]'s
  stage machinery, and F5b's silent-turn classification (which short-circuits on
  `if not terminal_tools`, and only `_drive_one_link` passes them). It is now
  `test_lead_cycle_authors_a_chain_end_to_end`, driving `run_cycle` from two worker reports one of
  which carries the `tool_request` — the lead files `author_chain` itself and `_author_chain` drives
  the links. **This resolves the `_continuation_for` gap without touching production**: the gap only
  exists on the path production abandoned. Its assertions are harness properties (a form is always
  filed; every claimed commit is reachable in the repository with a manifest entry and a test file;
  every planned link is attempted while credits remain; a failed coder turn never reaches the lead
  as a success; a non-committed attempt leaves a clean tree), with model judgment recorded rather
  than asserted — a co-lead rejection on a real science point is a correct outcome, and the old
  "both tools commit" assertion made it a test failure. Layer-B skip counts are extracted from the
  ledger and printed, so the exposure above is measured while it is unfixed.
  **Every live test resolves its models through the configuration pointer** (human ruling; *"fake a
  config if we want to test sth else"*). `tests/conftest.py` already applies `--seats` session-wide
  to `AUTO_RESEARCH_CONFIG_DIR`, so a live test inherits it unless it builds a handle itself; four
  still do (`test_live_tool_search`, `test_live_schema_matrix`, `test_live_form_shapes`,
  `test_live_figure_vision`) and are named here rather than converted. The rebuilt test writes its
  coder seat through `seats.seated(...)` and logs the seat it read back out of the written file, so
  the run states what it measured; `AUTO_RESEARCH_CODER_HARNESS=codex|claude_sdk` selects the
  backend, `codex` carrying `science_bench`'s own `CODER` seat whole so the test cannot drift from
  the batch.
  **Config: the shipped coder moved to `deepseek-v4-flash` on the Codex harness** (human edit,
  polished here). It **would have failed at launch**: that pairing needs an explicit
  `base_url: https://api.deepseek.com/` (`harness.py:133`), which is now present. Checked rather
  than assumed: the pairing is live-probed and not in `UNVERIFIED_PAIRINGS`; litellm prices the
  model at $0.44/$1.32 per M, so dropping `priceMTk` does not silently zero the coder's spend;
  `DEEPSEEK_API_KEY` is the derived default so `key_env` is redundant. `parameters` is now absent,
  so a non-batch run authors at the vendor's default reasoning effort — the batch pins effort in
  `arms.py`. The stale "the coder is Claude" claims in §9.3 and `README.md` are corrected.
  **Two tests hardcoded facts `config/models.yaml` owns**, and the config edit made them false:
  `test_models.py`'s `_VALID_PROVIDERS = {"anthropic","google","openai"}` (a literal predating the
  six-vendor expansion — now `set(get_args(ProviderName))`, the annotation the field itself carries)
  and `test_env.py`'s fixed five-key `.env` fixture (the requirement is *derived* from
  `models.yaml`, so a new vendor in any role failed a valid config — the fixture now writes the
  three deliberate alias lines by hand and derives everything else). Same class as the config-flip
  lesson: a valid configuration failing the check that exists to catch an invalid one.
  **The jail change (the one production edit):** see `sandbox/DOCS.md` — `.git` is bound read-only
  inside every writable root, so the coder's git ban is enforced by the kernel on both harnesses
  while read-only inspection finally works. Suite **1909 passed / 187 skipped / 0 failed**. The
  skip count moved 169 → 187 against the morning's baseline and the delta is **not** the config
  change: every skip is an env-gated live test, and that baseline was measured in a shell where the
  live flags were set, so ~18 live tests ran there instead of skipping. Checked rather than assumed,
  because a config edit that silently quiets tests is the config-flip lesson's exact shape.
