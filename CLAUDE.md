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

- **Librarian** — one research loop that searches, reads papers and pages in full, and files a
  `LiteratureReport` whose findings cite what it read. The librarian **uses** tools; it never
  authors them.
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

## 2b. `turn_end` is the only exit — provenance at every role edge

**Every graph role leaves its ReAct loop through `turn_end` and through nothing else, enforced
mechanically.** No fallback composes a role's artifact; no heuristic reads a turn as finished; no
cap silently returns an unfiled result. This binds the roles in the architecture figure — reviewer,
librarian, analyst lead, analyst workers — and not the coding pipeline, whose sanctioned exits are a
gated commit or an honest failure ([§9](#9-phase-2--tool-authoring-the-active-phase)).

**This is a trust boundary at the same altitude as the two-repository separation in
[§2](#2-the-two-repository-trust-boundary), and it is written here because it was not written at the
beginning and should have been.** §2 binds **what code may run**. This binds **what may be claimed**.
`turn_end`'s validator is where a role's output is bound to the work that produced it, so an exit
skipping the validator yields an artifact nothing vouched for. Neither boundary suffices alone: an
isolated toolkit with unvalidated role edges gives trustworthy tools and untraceable conclusions,
while validated edges over a toolkit the runtime can rewrite give careful accounting of unreliable
work. **Only both together make provenance complete**, and that completeness is what the repository
rests on.

Consequences, each with a measured failure behind it:

- **A role's artifact is written by the role, inside its loop, and submitted by `turn_end`.** A
  harness that composes an artifact on a model's behalf fabricates provenance — which is why the
  worker's fallback compose was deleted (F5), and why a loop that ends unfiled records an honest
  `failed` rather than a substitute.
- **No heuristic may decide that a turn was a finish.** `_continuation_for`'s `len(content) > 40`
  test did exactly that and lost a completed 14-quantity reproduction to a 58-character turn
  (2026-09-15); over 192 recorded silent turns it had never once discriminated a value between 1
  and 40.
- **Exhausting a cap is not an exit.** A loop that runs out of budget without filing must fail
  loudly and record that it did. Returning an unfiled result silently is the same defect wearing a
  limit's clothing.
- **A role that composes its artifact *after* its loop sits outside this boundary and is wrong.**
  Closed for every graph role on 2026-09-24: the reviewer writes its `ReviewerReport` inside its
  loop and `turn_end` renders and compiles it; the librarian files a `LiteratureReport` the same
  way. The analyst co-lead still composes its verdict after its loop, and stays so on purpose: it
  belongs to the coding pipeline, whose exits are a gated commit or an honest failure.
- **Every number a report states is bound to a receipt** (2026-09-24). Every artifact a role can
  rest a claim on — a tool's output, a figure, a data file, a paper, a web page, a human answer, the
  task it was handed — is a **receipt** the runtime mints: bytes hashed by the runtime, producer and
  inputs stamped, parents linked, `failed`/`synthetic` flags propagated. Prose cites receipts by
  handle, and `turn_end` refuses a number that no cited receipt carries (*scoped grounding*, §7).
  The figure discipline was the first instance of this; it is now the rule for every artifact.

The testable corollary: **every role edge is a validation point.** A live adversarial test drives
each role with a model instructed to escape — answering in prose, idling on free tools to the cap,
refusing, emitting malformed calls, claiming completion without filing, stating a number no
receipt carries, citing a handle it was never given, misquoting a span — and asserts that no path
yields an unfiled artifact, a silent exit, or an ungrounded claim.

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
| Librarian | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning medium), `gemini-3.5-flash` (thinking high) |
| Embeddings | — | `text-embedding-3-small` *(deferred — see [§12](#12-deferred-beyond-phase-2))* |

Implement a thin provider abstraction so a role can be backed by any provider via config.
Keep it minimal; do not build a general model-routing framework.

---

## 7. Schemas & protocols

All inter-agent messages are **Pydantic v2** models in a shared `schemas` module. They
are the protocol and double as documentation. Bad data must fail loudly and early.

Representative messages: `ResearchRequest`, `SearchRequest` / `SearchResult`,
`LiteratureReport` (the librarian's), `AnalysisTask` / `AnalystReport`, `ReviewerReport` (the one
report the reviewer files per review: the verdict fields the graph routes on, plus title,
abstract, sections, key findings, figure receipt handles and tables), `ResearchReport` (the
published JSONL envelope: the filed report, its receipt closure and the artifact handles),
`Receipt` ([below](#receipts--every-artifact-by-reference-hashed-by-the-runtime)), and the
Phase-2 additions `ToolSpec`, `HumanQuery` / `HumanResponse`
([§9](#9-phase-2--tool-authoring-the-active-phase)). `ReviewVerdict`, `LatexReport`,
`ReportSection` and `Reference` were retired on 2026-09-24. Keep the set minimal; refine
signatures in the module's `DOCS.md`.

**One report class serves both analyst roles, and the TOOL SURFACE keeps them apart** (2026-09-12).
`AnalystReport` carries the lead's decision fields (`rework`, `rationale`, `what_to_probe`)
alongside the worker's `tool_request`, and `edit_report` declares only what a given role may write
— the worker's surface omits the decision fields and the `needs_human` status, the lead's omits
`tool_request`. A subclass per role was rejected because it would widen a `Literal` in a subtype,
and the analysis result returned all the way to the reviewer *is* that type. The conditional
requirements (`rework=true` needs a probe; `rework=false` needs findings) live in the `turn_end`
validator, not in the schema: the schema stays permissive so a partial write mid-turn is legal,
and conditional required-fields is not portable across the six vendors.

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

The `FigureRef` `sha256` + `produced_by` pair was the seed of the **provenance** model below; the
tool-declared values it carried are no longer trusted (the runtime recomputes them).

### Receipts — every artifact by reference, hashed by the runtime

Built 2026-09-24 (`schemas/artifacts.py`, `auto_research.provenance`); the figure pattern above,
generalised and corrected.

- **One shape for every kind** — `figure | result | data | paper | web | human | commit` — and one
  handle scheme, `<source>:<letter><n>` (`analyst_worker_0:r12`, `lib:p4`, `rev:d1`,
  `input:h1`). The source is the producing agent's key; the letter names the kind.
- **Minted by the runtime, never declared by a tool or a model.** The hash is computed over the
  bytes the runtime stored; the producer (tool kind, toolkit commit, call id, hash of the call's
  arguments) is stamped by the code that ran the work. A figure a tool returns is re-hashed from
  the file, a declared hash that disagrees is overridden with a note, and a figure whose file is
  absent is dropped. Every tool call's output text is itself a receipt, and so is every file the
  call created or changed, every page fetched, every paper opened (the receipt is the PDF itself;
  a paper value is *attributed* with a locator, never text-matched, §13), every human answer, and every task a unit is handed (source `input`).
- **Inputs and ancestry.** The files a call's arguments name are hashed before it runs (once per
  run, cached by path, size and mtime); an input whose bytes an earlier receipt produced links it as
  a parent, so a result's ancestry is walkable and `failed` / `synthetic` (§9.5) flags propagate to
  every descendant. A flagged receipt can be cited to describe what happened; it cannot ground a
  number.
- **One store per run** (`ReceiptStore.for_run`), append-only, replayed on resume, so a handle
  means the same artifact everywhere. It lives beside the run's ledger, outside every role's
  allow-list, so no role can read another's evidence and re-mint it as its own. Each unit cites through a scoped view: its own receipts, the
  run's inputs, and whatever the reports it received cite, with their ancestors.
- **Scoped grounding** (human ruling 2026-09-24, replacing "every number quoted"): a finding cites
  the receipts its numbers come from as `[handle]`, and every number in it must appear, on token
  boundaries, in one of them; other fields are grounded by everything the report cites.
  `{{handle "…"}}` pins an exact span of a text receipt; for a paper the same form, with a
  locator (`{{lib:p3 "4.0 ± 0.2 %" Table 2}}`), attributes the value instead. A task's own numbers ground a text only where it cites
  `[input:…]`, for every unit including the reviewer, so a value handed in by the question cannot
  reappear as a finding unmarked. Measured before the ruling, over 231 real reports: a median
  worker report states ~240 numbers and 98% of its decimals already appear verbatim in that run's
  tool outputs, so quoting every number would have meant hundreds of quotes per report while
  scoped grounding refuses exactly the residue (mental arithmetic, rounding, sign drift). Plain
  arithmetic on receipted numbers runs as a `python -c` call, which is receipted like any other;
  a number the role typed into any shell command cannot be grounded by shell output, so `echo` or
  write-then-read cannot mint a receipt for a number nothing computed.
- **Normalisation and boundaries** are applied to both sides of a comparison and never to what is
  rendered: NFC, never NFKC (it maps `10²³` to `1023`); a U+2212 minus before a digit reads as `-`;
  paper/web text is compared with whitespace removed except between two digits (extracted text
  can split a number into `4 . 0 ± 0 . 2`), and math-italic letters read as plain ones (`𝜎` as
  `σ`). A failed quote is answered with the receipt that holds it, or the closest passage, never
  with text that lost a sign the quote carries. A needle may not sit inside a
  longer number or after a sign it lacks (`0.51` never matches `-0.51` or `10.51`), `2-8` is a
  range, `+/-` an error bar. Exempt: Figure/Table/Eq. numbers, years, source designations,
  ordinals that name a role or a step (`worker 0`, `DU 2`, `round 3`), list enumerators,
  persistent identifiers (DOIs, bibcodes, prefixed arXiv ids), and HTTP status codes with their
  reason phrase.

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
librarian is one research loop that exits through `turn_end` (2026-09-24): it searches, reads
papers and pages in full, mints each document it opens as a paper/web receipt, and files a
`LiteratureReport` whose findings cite what it read by handle.
*(Phase 2 relocates literature search into a `research_toolkit/lib_utils` package —
[§9.2](#92-toolkit-architecture--the-uniform-module-structure).)*

**Agents.** Per-role wrappers (`agents`, `roles.py`) each load their `prompts/<role>.md`,
resolve models via config, and exchange structured Pydantic messages: reviewer, librarian,
analyst worker, analyst lead. Every graph role is the same unit — tools, inputs, a quick channel
(`/steer`, `ask_human`), and one report written in its loop that leaves only through `turn_end`
(§2b); the graph is built from that unit.

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

**Report & entry.** The reviewer writes plain text only; `report/render.py` is the only LaTeX
author (lualatex, DejaVu Serif with luaotfload fallbacks). Figures are staged by hash and
re-hashed, tool-computed tables are typeset from their receipt's bytes, references come from
paper/web receipt identifiers, a provenance appendix lists what the report cites, and a
"values to verify against the PDFs" appendix lists every paper value the report attributes. Render and
compile run **inside the reviewer's `turn_end`**; the `compose_report` node runs no model — it
mints `rev:dN` receipts for `report.tex`/`.pdf`/`.jsonl` (parents: the cited receipts) and writes
the JSONL when configured. Output formats come from config and `turn_end` validates every
configured one. The `monitor`
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
2. The **analyst lead runs one ReAct loop per analysis cycle, with NO stages, and its REPORT is
   its decision** (`run_cycle`, ending on `turn_end`) — the same contract the analysts already
   write. `rework=true` sends the analysts back with a probe (`rationale` + `what_to_probe`);
   `rework=false` makes the report the analysis result. One form survives, `author_chain`, and it
   is a **brief handed to a subagent, not a decision**: filing it opens the first coding session
   and the turn continues. A finished link — committed, or its review submissions spent —
   **advances the chain on a tool result**, so the next session opens underneath the lead and a
   dead link is a sentence in the conversation rather than the end of the turn. Every question
   carries its own description, the channel a Pydantic `#` comment never reached.
   `author_chain` asks for its first link as a **required property** and its handler refuses a
   chain without one — the schema is the belt, the handler the suspenders (a run died on that
   state going *unrejected*, 2026-09-01; measured 2026-09-02, a provider does not enforce
   `minItems` on a tool `input_schema` the way it constrains structured output, which is why the
   rule is a required property rather than a cardinality keyword).
   Completeness is checked once, at `turn_end` (`lead_turn_problems`): no report written, rework
   with no probe or no rounds left (the cap named), a conclusion with no findings, **a cited
   figure whose file is not on disk**, or a coder still running — each an `is_error` that puts the
   lead back in its loop, never an ending.
   **What this replaced, and why (2026-09-12).** A filed form was a `terminal_tool`, and while
   authoring the only two the lead could file were both whole-cycle terminals — so "this link is
   dead, try the next" was inexpressible. Slot 1 of the 2026-09-11 batch ended a whole analysis
   with **11 of 12 author credits unspent**; slot 2, from the identical position, spent a debate
   round and a full worker rerun to say the same thing. One missing action, two different scars.
   Two of the four retired handlers validated nothing anyway: they copied an already-validated
   report onto the cycle and differed by one field the report itself carries.
   The declared tool set never shrinks — it is gated on the cycle, not on a live session, because
   a set that shrinks mid-conversation is a documented run-killer — and the caps stay in Python
   (`debate_max_rounds`, `tool_author_cap`), so the loop chooses *what* and the harness decides
   *how many times*. Briefing the chain **spawns a Sonnet-tier coding sub-agent** (the **author**)
   per link, the lead staying in an **interactive channel** with it during authoring — to clarify science/code as it goes (this
   lead↔author loop is built in step 2 of [§9.10](#910-build-order-phase-2)). The coder works
   *beside* the lead: a message to a working coder is queued for its next turn unless the lead
   asks for mid-turn delivery, and the lead lets time pass with a **blocking `wait_coder`** that
   wakes on the coder's turn end, its question, or a human steer — a short timeout is raised to a
   floor rather than refused, because a refused poll is still a paid turn. Between links the
   coding transcript is dropped at its own boundary — for a **committed** link only, since a
   rejection or a spent budget is what the next brief must be written against — keeping the lead's
   brief and its own submit call with the real commit SHA on the result: both turns it produced or
   received, nothing synthesized in their place.
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
- **Provenance** — outputs carry input **sha256** hashes + the tool-call chain, making "computed a
  downstream result from a failed/synthetic upstream step" mechanically detectable. At run time
  this is now the runtime's job, not the tool's: every call is receipted with its inputs' hashes
  and parents ([§7](#receipts--every-artifact-by-reference-hashed-by-the-runtime)). The gate's own
  static provenance check still recognises one constructor (§13).

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

One line each; the open-work queue lives in the gitignored handoff, not here.

- **Durable HITL suspend at `interrupt()`** — resume re-runs the node, infeasible mid-`_react`; the
  blocking handler ships instead. (Checkpointer resume and `/stop` are built.)
- **Run-wide redirect steer** — `/steer <id>` reaches one role; nothing binds every role at once.
- **OS jail by default** — `bwrap` jail built 2026-08-20 (`sandbox/jail.py`), off in the shipped
  `.example`; remaining: egress control for the non-gate processes, then default-on.
- **Files API for media** — deferred; a referenced-PDF 413 at ~6.3 MB (anthropic-sdk-python#1098)
  suggests a tighter ceiling than the documented inline 32 MB.
- **`bash_exec` figure capture** — diff the scratch cwd for new images, synthesize `FigureRef`s.
- **RAG (ChromaDB, three targets)** — literature, persistent memory, tool index; `vector_search`
  stays stubbed.
- **`onboard_toolkit`** (installer increment 5) — staged raw code → gated tools; needs collaborators.
- **Lead-scientist group** — idea generation routed through the reviewer gate.
- **Provider-native web tools [I11]** — per-member routing, Tavily as fallback
  (`logs/dev/provider_matrix_20260901.md`).
- **Smaller deferrals:** concurrent HITL for parallel workers [E6]; figures routed to the coder [V-1];
  request-time data-feasibility check [H9]; Markdown/`backend.log` derived from the ledger [J1(b)];
  worker guard excluding toolkit source [F10]; trimming the ~35k-token authoring seed (whole
  `skills/__init__.py` inlined); names-only tool catalogue if it ever dominates; read-only MCP
  exploration.

---

## 13. Loose ends (in-code stubs, deprecations, best-effort gaps)

Known gaps in the tree **today**, one entry each: the gap, its status or ruling, the site. Append as
they land; delete when closed. Distinct from §12 (unbuilt features).

**Provenance**
- **What receipts do not cover (2026-09-24):** (a) inputs a tool locates itself from an identifier
  are not hashed; (b) grounding checks a number exists in a cited receipt, not its row/column;
  (c) the gate's static provenance check recognises only `FigureRef(...)`; (d) exemption shapes
  (years, Figure/Table numbers) are unchecked; (e) a toolkit tool writing model-chosen content could
  launder it; (f) `FigureRef`'s declared fields remain the multimodal carrier, the receipt is
  authoritative. The flagged-quote shape is pinned deterministically only.
- **Paper values are attributed, not verified** (ruling 2026-09-24) — a model can still attribute a
  wrong number; the `paper_attributions` list makes it cheap to check.
- **Report fields are checked for emptiness, not content** — accepted limit, every role (ruling
  2026-09-24); numbers stay grounded.

**Roles**
- **Reviewer live-tested on the contrast seat only** (`qwen3.8-max`; configured seat ruled
  unnecessary). Residuals: a non-glyph compile failure leaves `.tex` and no PDF
  (`renderer_error`, L1); tables typeset to 60 rows; the draft preview needs `pdftoppm`.
- **`review_report` returns the whole citable catalogue by value every call** (18 image blocks
  measured) — ruling 2026-09-10: leave it.
- **Paused-turn continuation guard has never run** (0 `paused` in 184 silent turns) — kept for the
  day a hosted vendor tool is declared.
- **Unattended runs:** `ask_human`/`plan_and_debate` exist only with a handler; on the author side MCP
  flattens unattended vs channel-fault into `str(exc)` (the ledger events keep the distinction).
  Batches published before 2026-09-01 ran with prompts naming tools the roles lacked.
- **Worker `edit_report` misformatting** (whole report in `summary`) is mitigated by a soft note only.

**Coder and sandbox**
- **Codex coder blind spots** (awaiting ruling): a killed turn books $0; MCP startup status is not
  recorded.
- **Qwen coder pairing** (`dashscope`+`codex`) unprobed since account activation, flagged in
  `UNVERIFIED_PAIRINGS`. Only a pay-as-you-go `sk-ws-` key on `dashscope-intl` serves; Coding Plan
  keys speak Chat Completions only. Re-check: `tmp_scripts/check_qwen_entitlement.py`.
- **Shell commands are best-effort confined** — `PathGuard` sees only paths named in a command, and
  its token extractor false-denies (`1/2`, URLs) [F3]; with `agent_os_jail` off there is no kernel
  layer. Toolkit-tool path arguments are not allow-listed.
- **Coder git ban** is kernel-enforced when jailed (`.git` read-only); otherwise the Claude
  deny-rule is prefix-matched. Backstops either way: gate-entry `reset --mixed`, failure reset to
  the base ref.
- **Claude SDK built-ins** (`ToolSearch`, `ScheduleWakeup`, `TaskOutput`) are not gated by
  `--allowedTools` [F12].
- **`_reset_direct_tree` is best-effort** — a git failure is logged and swallowed; a hard kill can
  leave the coder's commit on a clean tree, which preflight (dirty-tree only) misses; bounded at 30 s
  on interrupt.
- **`_make_path_confiner`** — deprecated, unwired; use the `PreToolUse` hook.
- **`PathGuard` denies governance basenames everywhere**, incl. research_toolkit's own contract —
  ruling: leave it.
- **WhatsApp/Twilio connector deprecated** — prefer `telegram`/`slack`/`cli`.
- **HITL slash-commands are Telegram/Slack only** — CLI and WhatsApp have no background listener.
- **`retrieval.vector_search`** — `NotImplementedError` stub (§12 RAG).

**Providers**
- **Gemini parts captured twice** (`google.py`, 7 → 12) and replayed; `thought_signature` capture
  [I8] not live-verified — ruling 2026-09-12: Google is not an active seat, record only.
- **OpenAI assistant `phase` is not replayed** — ruling: "we don't change openai".
- **Context editing covers Anthropic only**; a Tier-2 compactor is unbuilt and must exclude
  tool-search discovery blocks.
- **Tool search:** activation lives in the history (never clear discovery blocks; every replaying
  request declares its tools); Anthropic hides deferred tools, OpenAI keeps names; Anthropic BM25
  expands up to 5 schemas per query.
- **Key-failover edges:** rate limits are per-org; an in-flight coder keeps its launch key; 401 is
  not rotated; retirement is process-scoped; Google is unwired (no cited taxonomy).
- **Media:** eviction is oldest-first, so one near-ceiling document survives while small ones drop
  (size-aware needs a ruling) [V-6]; the `vision` flag gates only the call-time `images=` channel,
  not figures on a tool result [V-2].
- **Vendor schema quirks beyond the probed positions:** GLM writes `"null"` into `str | None` fields
  [I14]; `$ref` under `allOf`/`oneOf`/`additionalProperties` unmeasured [I20].
- **DeepSeek:** `images_accepted=False` stands on policy; the docstring's text-only reason is stale.
  `deepseek-v4-flash` is a retired alias now served by V4.1-Flash.

**Benchmarks**
- **gate_bench pruning cache leak** — 20 pruned entries in swap batches from 2026-08-26 carried
  `.pytest_cache` naming the broken test; ruling: benchmark valid, no re-run.

---

## Changelog

Compacted 2026-08-05, 2026-09-01 and again **2026-09-24** (all entries, to terse items keeping
dates, tag ids, rulings and LIVE-VERIFIED / NOT-LIVE-TESTED markers); detail is in git history.

- 2026-05-27 -> 2026-06-16 — **Phase 1 + Phase 2 steps 0–7.** Phase 1: the LangGraph team end to end
  (schemas, provider registry, observability, SQLite, sources, prompts, roles, consensus, sandbox,
  reports, CLI, Studio); a Cyg X-1 joint fit produced JSONL + LaTeX/PDF. Phase 2: `ToolSpec` →
  `propose_and_pr` (coder in an isolated worktree) → `run_gate` → decorrelated co-lead review →
  `approve_edit` (**commit ⇔ gate ∧ review**); `preflight`; `hitl` + `ask_human`. Live: IXPE P1.
- 2026-06-18 -> 2026-07-09 — §3 gained **write only positive actions** and **plan-and-debate before
  coding**; `AnalystToolRequest` split from `ToolSpec`; gate determinism via per-test JUnit maps;
  the reviewer is the sole I/O (§9.6); detached runs (`run_research_nohup`); **[A8]** the lead authors
  the whole `tool_specs` chain; **[A12]** gate targets derived from the diff; `open_paper` refuses a PDF
  over 8 MB; WhatsApp deprecated; RAG direction set to ChromaDB (deferred, §12).
- 2026-07-13 -> 2026-07-16 — **[V-4]** per-round report JSONL refined via `edit_report`/`review_report`,
  figures cited by `{source, index}` handle (replacing dedup); installer increments 0–4 (setup kinds
  A–E, toolkit point/clone/start-fresh; increment 5 deferred); transient-provider retry on every
  `ModelHandle.call`; RNG patterns → co-lead-judged finding (ruling: Monte-Carlo tools need
  randomness); double-fork detached spawn.
- 2026-07-20 -> 2026-07-22 — `loads_first_json_object` on OpenAI/Google receive paths; catalog read moved
  into `research_toolkit`; **[A11]** the rerun fires on ≥1 commit; **[I5]** litellm-first pricing with
  cache/reasoning token classes; Anthropic context editing built (shipped off); durable resume Stages
  1–3 (`AsyncSqliteSaver` checkpointer, `heal_toolkit_branch_if_dirty`, `/help /status /steer /stop`
  over Telegram/Slack only). Durable HITL suspend at `interrupt()` stays deferred.
- 2026-07-27 -> 2026-07-31 — Tool search / `defer_loading` + one-line manifest briefs, **LIVE-VERIFIED
  2026-07-28** and enabled with context editing (resident input 19,074 → 3,789 tok on `gpt-5.5`);
  cost stamped on every monitor line (`cost_ledger.jsonl` deleted); tombstone guard deleted. **`/stop`
  is Ctrl-C:** `RunControl.interrupt()` cancels the run task, then a shielded three-phase cleanup, no
  model call, no report (rulings: force by default, no flags; [F6] terminal-compose guard dropped).
  `/status` investigates via `StatusMonitorAgent`. **NOT LIVE TESTED:** `/stop` mid-authoring.
- 2026-08-03 -> 2026-08-07 — A malformed ADS link costs one link, not the source; **[H9]** GPG-locked
  HEASARC products share one lock rule (rulings: honest level + `LOCKED, NOT USABLE` mark; state
  derived from the path). **Replay seam:** `schema` and `tools` are no longer exclusive, the response
  IS the assistant turn, `provider_parts` replayed per each provider's docs, `tool_choice="none"`;
  **[I8]** Gemini signature capture not live-verified. **Multimodal guard:** `providers/_media.py`
  with per-adapter `_LIMITS` quoted from docs (resize or withhold, always with a model-visible note;
  nothing downscales unless a limit binds); closes the mime half of [V-3]; Files API deferred.
- 2026-08-10 -> 2026-08-18 — API-key failover (`models/keys.py`, `…_FALLBACK`): rotate on a rate limit,
  retire a key on a spent balance, never rotate on 401; per-provider error taxonomies (OpenAI reports
  every quota as a 429; Google unwired). **LIVE-VERIFIED** on the Q21 resume. `benchmarks/author_bench`
  runs one frozen real brief K times through the production lead loop (a trial costs $27–45; K=10).
- 2026-08-19 -> 2026-08-20 — The author is a parameter: `sandbox/coder/` `CoderBackend`
  (connect/drive/disconnect) + a Codex backend; members gain `harness`/`base_url`/`key_env`;
  decorrelation reads the author's vendor; Codex reads are ungated (recorded as
  `coder.read_outside_allowlist`). Env requirements derived from `models.yaml`; `PreflightWarning`
  tier (Qwen coder UNVERIFIED). OS jail (`sandbox/jail.py`, `bwrap`) built, shipped OFF (ruling: one
  enforcement layer); `author_bench` legs + `allow_correlated`. Ruling: the paper states one list-price
  USD figure. **LIVE-VERIFIED** jailed on both harnesses; a Codex authoring attempt NOT LIVE TESTED.
- 2026-08-24 -> 2026-08-25 — Six chat vendors: DeepSeek, Z.AI and Alibaba through a shared
  `providers/_responses.py` + per-vendor `ResponsesVendor` facts; `schema_via_tool` as a second
  structured route; `$ref` resolved only in the positions each vendor is measured to need; GLM
  `"null"` strings normalised. Vision eligibility per role (reviewer, lead, worker; not the co-lead);
  ruling: DeepSeek stays text-only by policy. 4 x 5 lead x co-lead grid agreed; Qwen live tests at
  explicit `minimal` effort; live tests moved onto `ModelHandle`. Open: GLM's grid row ([I14]).
- 2026-08-31 — Submission snapshots: every gate submission persisted as patch + verdict; on resume
  restored only onto its exact base ref and re-gated (no stored verdict trusted). [F11] bad tool args
  → `is_error`; ruling: vendor structured-output review errors count as failing the item.
  **LIVE-VERIFIED 2026-09-01**; the full-run resume leg stays open.
- 2026-09-01 — Headless HITL: the author's `ask_human` classifies unattended (`UNATTENDED_GUIDANCE`)
  vs channel fault instead of mirroring the question back; prompts name `ask_human`/`plan_and_debate`
  only when wired (both published batches ran the old text). Ledger field caps raised to 100k, in
  config. **LIVE-VERIFIED** (`test_live_no_hitl.py`; ruling: no further testing). Ruling: the pipeline
  always reaches a verdict, and a plainly stated "not verifiable" is a correct row ([I7] downgraded).
- 2026-09-02 — The lead became one ReAct loop (`run_cycle`, ruling) ending on a filed form tool
  (`author_chain`, `request_another_round`, …); `adjudicate`/`decide_authoring`/… deleted; form fields
  carry Pydantic descriptions; declarations never shrink; caps stay in Python; a committed link's
  coding transcript is dropped at its boundary; `drive_authoring` kept for `author_bench`.
  **LIVE-VERIFIED** (`test_live_lead_cycle.py`).
- 2026-09-02 — Six fixes from block 0 attempt 5: the rerun follows the commit count, not the filed
  form; an approved commit advances the base ref (a post-commit submit had reverted it), and a no-op
  submit is budget-free; the media budget reserves non-media request weight (a 413); a clause-scoped
  negated banned word is a judged finding (0 `gate_bench` verdicts moved); one link = one capability
  (lead prompt); `author_bench` checks every recorded commit is reachable. §9.3's `minItems` claim
  corrected (the handler, not the schema, holds).
- 2026-09-03 — Ledger audit of attempt 6: the coder read peer seats' models from run logs; rulings:
  only trivial changes outside core logic, `runtime_dir` stays readable (channel measured, not
  closed). `_author_env` drops `CLAUDE*`/`COPILOT*`/`VSCODE*`; report filenames and lead tools stop
  naming vendors. `gate_bench` pruning leaked `.pytest_cache` into defect worktrees only; fixed, plus
  an unfiltered pair test (§13).
- 2026-09-08 — Science sweep reframed (ruling) as a demonstration: incumbent + contrast seat trees x 3
  trials, not a factorial; seats chosen per seat's ability, never price; reasoning effort rejected as
  the lever. Any model in any seat via `tests/seats.py` + `--seats`. Scoring built (`answer_key.json`,
  extract/score/report; PA wrapped mod 180°; `useful` = ≥10 of 14); [A20] relabel reads `lead_cycles`.
  Role wiring single-sourced in `graph/runtime.py` (`readonly_tools_for`, `attach_worker_runtime`).
- 2026-09-09 — Demonstration batch re-diagnosed: the gate and registry read the tree, the commit
  reads the index. **F1** reset keyed on `pending_changes()`; **F2** structural check an allow-list of
  `*_utils` (ruling); **F5** the loop exits on a validated `turn_end` tool, worker fallback compose
  deleted, figures in their own turn; **F9** a failed link no longer ends the chain, `status` defaults
  to `failed`; **F10** narrow write set; **F7** per-vendor transient overrides; **F8** sectioned
  prompts; N1/N4/N6/N8/N11. **F3 withdrawn** (ruling: answer-key values stay in the question).
- 2026-09-09 — The coding live test rebuilt on `run_cycle`; its failure was the Claude-SDK coder's
  credit running out, returned to the lead as a successful reply (recorded, deferred; ruling: fix the
  measurement, not production). An inert Layer-B skip recorded. Ruling: every live test resolves models
  through the config pointer. Shipped coder → `deepseek-v4-flash` on Codex; `.git` bound read-only
  inside the jail, so the coder's git ban is kernel-enforced on both harnesses.
- 2026-09-10 — **A1** (Anthropic): a tool's figures nest inside their `tool_result`, fixing an unpaired
  server-tool 400; OpenAI deliberately unchanged, Google excluded (ruling). **LIVE-VERIFIED.** **A5**:
  the answer key's four May band PD values corrected to Krawczynski+2022 Table S2 in the CSV, key and
  `main.tex` (ruling); no recovery count moves (mean |z| 1.00 → 1.178).
- 2026-09-12 — The lead's turn is one loop with no stages, ending on `turn_end`; its report is its
  decision (`AnalystReport` gains `rework`/`rationale`/`what_to_probe`); the four cycle forms and the
  stages deleted; `_ChainDriver` advances links on a tool result. Ruling 18: commit and rework branches
  stay separate. **A1** `has_unresolved_server_tool` per vendor; **A2** a steer is held a turn after an
  unresolved server-tool call, and a `paused` silent turn appends nothing (ruling: guard dead code).
  Gemini duplicate parts and OpenAI `phase` recorded, not fixed (rulings, §13).
- 2026-09-12 — `/stop` during a Codex coder turn hung at exit; fixed (rulings, both remedies): `drive`
  keeps its turn queue registered when cancelled, and polls on a daemon thread it owns. `hitl/cli.py`
  reads stdin on a daemon thread (ruling). The shipped coder launched unauthenticated (no `key_env`);
  `CodexBackend` now derives the key variable as the chat path does (ruling). `check_coder` routed and
  the seed drive backgrounded (ruling); submit refused while the coder works. **LIVE-VERIFIED:** the
  B8 stop drill repairs a dirty tree and exits clean; a mid-write child cancel is still unproven.
- 2026-09-24 — **The librarian became a unit**: one `_react` loop over `LibrarianToolProvider` filing a
  `LiteratureReport` through `turn_end`; papers read as native PDF attachments and cited, never quoted
  or number-checked (rulings); `IODebateToConverge` deleted. Live: shape matrix 19 of 21 cells (§13).
- 2026-09-24 — **The reviewer became a unit**: files one `ReviewerReport` in its loop, and `turn_end`
  renders and compiles every configured format (ruling); `report/render.py` is the only LaTeX author;
  `ReviewVerdict`/`LatexReport`/`ReportSection`/`Reference` deleted. Rulings: request numbers ground
  only via `[input:…]`; paper values attributed and listed for human checking. Live: 10 cells on
  `qwen3.8-max`; the configured seat not live-tested (ruled unnecessary).
- 2026-09-24 — `check_coder` replaced by a blocking `wait_coder` (floor/default/max 60/300/1800 s) that
  wakes on turn end, `ask_lead` or `/steer`; `drive_coder` queues for a working coder unless
  `mid_turn`; submit waits for the coder (rulings). **LIVE-VERIFIED** (mechanism cells, Opus and
  `gpt-5.6-terra` leads, `/stop` while parked). Two findings recorded in §13, not fixed.
- 2026-09-24 — **Provenance for every artifact**: `schemas/artifacts.py` `Receipt` +
  `auto_research.provenance`; every tool call mints runtime-hashed receipts with parents and flags.
  Ruling: scoped grounding over "quote every number" (measured on 231 reports). Checked at every
  unit's `turn_end`, closing §2b for every graph role (co-lead excluded). Live runs: agent ordinals
  exempted; three checker defects fixed.
- 2026-09-25 — Docs compaction: every module changelog and this one condensed to the early terse
  style; §12/§13 rewritten one line per item, closed entries deleted (B8 stop drill, `/stop` hang,
  durable resume, author-env leak, `/steer` injection, coder reading the run record).
