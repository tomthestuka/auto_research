# CLAUDE.md

**Single source of truth for all coding agents** (Claude Code, Codex, and any other
agent harness) working on the `auto_research` project. All `AGENTS.md`-style files are
symlinks to this file (see [§11](#11-file-linking)).

> This file governs **build-time coding agents** — the assistants that construct and
> maintain the source code. It is **not** for the runtime LLM agents that the system is
> made of. See [§2](#2-the-two-repository-trust-boundary) and [§4](#4-claudemd-and-docsmd-are-build-time-only).
>
> **Archived specs.** Phase 1: [`CLAUDE_p1.md`](CLAUDE_p1.md). Phase 2 (tool authoring, the
> coding pipeline, receipts, the reviewer as sole I/O): [`CLAUDE_p2.md`](CLAUDE_p2.md) — its §9
> remains the authoritative description of the coding pipeline **as built** until Phase 3 moves
> that description into `sandbox/DOCS.md`. A reference of the form "P2 §9.x" below points there.

---

## 1. Project overview

`auto_research` is an automated research assistant: a team of LLM agents, orchestrated
with **LangGraph**, that takes a research request, searches the literature, runs
analyses, and reviews the results. The team has three roles:

- **Reviewer** — the sole I/O: scopes the request, routes work (`assign_to`), and reviews
  results against the literature; a report to the human is published and sent.
- **Librarian** — one research loop that reads papers and pages in full and files a
  `LiteratureReport`. It **uses** tools; it never authors them.
- **Analysts** — workers run toolkit tools and debate to convergence; a lead adjudicates and,
  when a capability is missing, briefs the **coding pipeline** that authors a new tool through
  a gated, branch-only flow.

**Phase 1 — DONE.** A minimal end-to-end skeleton with real calls, real search, real fitting.
**Phase 2 — DONE.** Tool authoring (author → deterministic gate → provider-decorrelated review →
one commit on a branch, merged to master only by a human), plus provenance at every role edge:
runtime-minted receipts, scoped grounding, and `turn_end` as the only exit. [§8](#8-what-phases-12-built-the-system-as-it-stands)
is the retrospective tour; `CLAUDE_p2.md` is the full spec.

**Phase 3 — THE ACTIVE PHASE: generalize the MTA unit.** By the end of Phase 2 every graph role
is the same unit, the **Minimal Trustable Agent (MTA)**: a toolkit, inputs, a quick channel, and
one report written in its loop that leaves only through `turn_end`'s validator, with every number
bound to a runtime-minted receipt. Today four role classes each re-implement that unit, and
several of its properties are global or reserved for one role: one toolkit root for the whole run,
one jail switch, a sandboxed REPL for the analyst workers only, the coding pipeline for the lead
only. Phase 3 makes the unit **one configurable thing**, so that per unit one can set:
- the toolkit root(s);
- the sandbox (bwrap on/off), the execution backend and its software environments;
- whether it may spawn subagents, either another unit or the coding pipeline.

The four roles become four **definitions**. **The motive is this repository's own needs; Phase 3
is not a public SDK or a framework for others** (ruling 2026-10-06, after the investigation
recorded in [§9.1](#91-scope-and-what-was-decided-against)).
[§9](#9-phase-3--generalize-the-mta-unit-the-active-phase) is the spec. Other deferred work is
parked in [§12](#12-deferred-beyond-phase-3-do-not-build-now--leave-stubs) — do **not** build it
now.

**What "minimal" means here:** prefer the fewest modules, functions, and lines that
satisfy the interface. Do not add configuration knobs, abstraction layers, retries,
caching, or features that were not requested. When in doubt, write less and document the
extension point in `DOCS.md`. **For Phase 3 specifically:** an option on the unit exists only
when two current roles need different values; generalizing is moving role-specific code behind
one interface, not adding capability nobody uses yet.

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
| **`research_toolkit`** | The analysts' domain tool library: uniform `<domain>_utils/` packages ([P2 §9.2](CLAUDE_p2.md#92-toolkit-architecture--the-uniform-module-structure)). Houses `xspec_utils` today; Phase 2 adds `heasarc_utils`, `lib_utils`, etc. | **The human** + (Phase 2) the runtime, which lands one atomic commit per tool **on a branch**. | **Read-only** for discovery/invocation via `ToolkitRegistry`. The **only** write path is the gated author→gate→review flow ([P2 §9](CLAUDE_p2.md#9-phase-2--tool-authoring-the-active-phase)), which commits to a **branch** — never to master. |

Consequences:

- The system's *own* code is `auto_research`, a repo the runtime can **never** touch.
- The runtime is **master-blind**: it commits authored tools to a working branch and has
  no concept of, reference to, or path to `research_toolkit`'s trusted branch. A human
  reviews branches and merges to master entirely **out of band** ([P2 §9.9](CLAUDE_p2.md#99-git-discipline--the-runtime-is-master-blind)).
  **Self-modification — and even un-reviewed toolkit changes — remain impossible by
  construction.**
- Recommended placement: a development workspace directory containing both repos side by
  side, with this `CLAUDE.md` at the workspace root.

**Phase 3 adds no package.** The generalized unit lives in `auto_research`. Software environments
(handoff [F18], [§9.6](#96-execution-sandbox-and-environments-per-unit)) are **defined** in
`research_toolkit` and changed only by the gated coding pipeline; their built prefixes, package
cache and locks are **runtime state** under the runtime data dir, never inside either repository.
The boundary extends to the human's own conda environments and package cache: units use them
read-only, and no agent ever writes them.

---

## 2b. `turn_end` is the only exit — provenance at every role edge

**Every graph role leaves its ReAct loop through `turn_end` and through nothing else, enforced
mechanically.** No fallback composes a role's artifact; no heuristic reads a turn as finished; no
cap silently returns an unfiled result. This binds the roles in the architecture figure — reviewer,
librarian, analyst lead, analyst workers — and not the coding pipeline, whose sanctioned exits are a
gated commit or an honest failure ([P2 §9](CLAUDE_p2.md#9-phase-2--tool-authoring-the-active-phase)).

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
- **An accepted `turn_end` is final for its batch** (2026-09-25). A call placed after it in the
  same model turn is answered and never run: an `edit_report` there replaced the validated report
  with an unvalidated one (found by an adversarial review, every role).
- **The reviewer is the sole I/O** (human ruling 2026-09-25). The run starts at the reviewer and
  every team's work returns to it; its report's `assign_to` routes the graph. The human is an edge
  endpoint, not a unit: a report to the human is published and sent, and the human's reply
  returns as a **nudge** — direction, never evidence, minted as no receipt.
- **A role that composes its artifact *after* its loop sits outside this boundary and is wrong.**
  Closed for every graph role on 2026-09-24: the reviewer writes its `ReviewerReport` inside its
  loop and `turn_end` renders and compiles it; the librarian files a `LiteratureReport` the same
  way. The analyst co-lead still composes its verdict after its loop, and stays so on purpose: it
  belongs to the coding pipeline, whose exits are a gated commit or an honest failure.
- **Every number a report states is bound to a receipt** (2026-09-24). Every artifact a role can
  rest a claim on — a tool's output, a figure, a data file, a paper, a web page, a file a human
  sent, the task it was handed — is a **receipt** the runtime mints (a human's text is a nudge, not
  a receipt, ruling 2026-09-25): bytes hashed by the runtime, producer and
  inputs stamped, parents linked, `failed`/`synthetic` flags propagated. Prose cites receipts by
  handle, and `turn_end` refuses a number that no cited receipt carries (*scoped grounding*, §7).
  The figure discipline was the first instance of this; it is now the rule for every artifact.
- **Evidence is what the analysis produced, never what a role read** (human ruling 2026-09-25:
  "citing in a report must be to sth it has produced, not just read"). Only a toolkit tool's
  return (and its files and figures), a `commit` the coding pipeline minted, a paper by attribution,
  and the request (cite-only, restating the question) can ground a number
  (`provenance.store.is_evidence`). Every harness return — `read_file`, `list_dir`, searches,
  `web_search`, `web_fetch`, the shell entirely — and every brief, human file and published report
  is recorded as where a role looked, and grounds nothing. The human gives verdicts and nudges,
  never materials.

The testable corollary: **every role edge is a validation point.** A live adversarial test drives
each role with a model instructed to escape — answering in prose, idling on free tools to the cap,
refusing, emitting malformed calls, claiming completion without filing, stating a number no
receipt carries, citing a handle it was never given, misquoting a span — and asserts that no path
yields an unfiled artifact, a silent exit, or an ungrounded claim.

**Phase 3 status of §2b.** The rule above is enforced today by four role classes that each
re-implement parts of it. In Phase 3 it becomes a property of the **one** unit: no role
definition can exit another way, file an unvalidated artifact, or ground a number in something it
only read ([§9.3](#93-invariants-the-unit-enforces-and-no-definition-turns-off)).


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
  application code** in [P2 §9.9](CLAUDE_p2.md#99-git-discipline--the-runtime-is-master-blind) — that is
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
- **A stateless adversarial review after every module change.** When a module's change is
  complete, spawn a fresh code-review agent with no memory of the session that wrote it. It reads
  the code and attacks it: it spots weaknesses, recognises shortcut logic (a check that cannot
  fail, a fallback that hides a failure, a test double standing in for the thing under test), and
  evaluates the tradeoffs taken. Verify each finding, fix every confirmed one inside the agreed
  scope, and re-run the tests before presenting; the human then sees what was fixed, what was
  rejected and why, and only the findings that need a ruling (a design, scope or tradeoff
  decision). Each of those is ruled on before the next module begins.
- **Every test drives the module API.** Deterministic and live tests alike exercise the real
  production logic through the module's own entry points and wiring. A hand-rolled equivalent of
  production code inside a test is a rubber stamp that catches no regression; fake only what lies
  outside the code under test (a provider's network, a clock), and build every role, handle or
  provider through the same functions the run path calls.

**Phase 3 additions to the contract:**

- **Generalize, don't extend.** Phase 3 moves role-specific code behind the one unit and its
  definition; it adds no capability a current role does not use. New options need two current
  callers that want different values.
- **Behaviour parity is the acceptance test.** Every step lands with the deterministic suite
  green and the invariant tests ([§9.10](#910-build-order-phase-3) step 0) unchanged, plus one live
  cell for each role whose wiring moved.
- **The execution layer is shared with the environments work.** Software environments
  (handoff [F18]; the gitignored `tmp_agent_envs_plan.md` is the authoritative record of its
  rulings, plan approved by the human 2026-10-06) are a separate work stream that
  touches the same sandbox/execution code; claim symbols on the coordination board before editing
  there, and keep its settled decisions ([§9.6](#96-execution-sandbox-and-environments-per-unit)).


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
author's "local structure context" ([P2 §9.3](CLAUDE_p2.md#93-actors-and-the-propose--author--review-flow))
is assembled from runtime-safe sources — real toolkit source, `prompts/coder.md`,
and config — never from a `DOCS.md`.**

**Phase 3:** the text every unit needs — how `turn_end`, receipts, citation syntax and the channel
work — may move into one shared file in `prompts/` that is loaded before each role's own prompt.
It is a runtime prompt, so this rule binds it: no `DOCS.md`, no `CLAUDE.md`.


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
not one model checking itself ([P2 §9.3](CLAUDE_p2.md#93-actors-and-the-propose--author--review-flow)).
Gemini may back cheaper, non-gating roles but stays off the critical author/review path.
The **coding/author agent is Sonnet-tier** (medium), spawned by the Opus analyst lead. The
`coder` member names **both a model and the author harness that drives it** (`harness:
claude_sdk | codex`), because a harness speaks one wire dialect and only some vendors publish an
endpoint in it; the legal pairings and their documented endpoints are a table checked at launch
(`sandbox/coder/harness.py`). The **decorrelation rule reads the author's actual vendor**, so a
non-Anthropic author simply requires a co-lead that is not that vendor.
Because the coder is the weaker model, *architectural* decisions — extend an existing
toolkit vs. author a new one ([P2 §9.3](CLAUDE_p2.md#93-actors-and-the-propose--author--review-flow)) —
stay with the more-capable proposer/lead; the Sonnet author executes them.

Default tier mapping (a *config default*, edit freely — not code):

| Role | Tier | Example defaults |
|---|---|---|
| Reviewer group | top | `claude-opus-4-8`, `gpt-5.5` (reasoning high), `gemini-3.5-flash` (thinking high) |
| Analyst lead | top | `claude-opus-4-8` |
| Analyst worker group | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning medium) |
| Librarian | medium | `claude-sonnet-4-6`, `gpt-5.5` (reasoning medium), `gemini-3.5-flash` (thinking high) |
| Embeddings | — | `text-embedding-3-small` *(deferred — see [§12](#12-deferred-beyond-phase-3-do-not-build-now--leave-stubs))* |

Implement a thin provider abstraction so a role can be backed by any provider via config.
Keep it minimal; do not build a general model-routing framework.

**Phase 3:** a unit's model still comes from this registry by its role key; a new unit definition
adds a role entry. The decorrelation rule travels with the coding pipeline: it is checked for
every unit granted the pipeline ([§9.7](#97-subagents-per-unit--onoff-two-kinds)), not only the
analyst lead.


---

## 7. Schemas & protocols

All inter-agent messages are **Pydantic v2** models in a shared `schemas` module. They
are the protocol and double as documentation. Bad data must fail loudly and early.

Representative messages: `ResearchRequest`, `SearchRequest` / `SearchResult`,
`LiteratureReport` (the librarian's), `AnalysisTask` / `AnalystReport`, `ReviewerReport` (the one
report the reviewer files per turn: the `assign_to` the graph routes on, plus title,
abstract, sections, key findings, figure receipt handles and tables), `ResearchReport` (the
published JSONL envelope: the filed report, its receipt closure and the artifact handles),
`Receipt` ([below](#receipts--every-artifact-by-reference-hashed-by-the-runtime)), and the
Phase-2 additions `ToolSpec`, `HumanQuery` / `HumanResponse`
([P2 §9](CLAUDE_p2.md#9-phase-2--tool-authoring-the-active-phase)). `ReviewVerdict`, `LatexReport`,
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
  a paper value is *attributed* with a locator, never text-matched, §13), every file a human sends,
  and every task a unit is handed (source `input`). A human's text is a nudge and mints nothing.
- **Evidence vs record** (human ruling 2026-09-25): a receipt is evidence only when
  `is_evidence` says so — its producer is a toolkit tool (`research_toolkit.<pkg>.<tool>`, also
  for a toolkit tool the lead runs in-process), it is a `commit` (minted by the runtime when a
  `submit_for_review` commits: tool, SHA, branch, submissions used), a paper, or the request. Every
  other receipt is the record of where a role looked: citing one is refused and names what it is
  ("a file you read", "a web page", "shell output", "a task brief"), and in scope it grounds
  nothing. Briefs (`follow_up`, `what_to_probe`, `tool_request`) are exempt from grounding and,
  dispatched, ground nothing downstream.
- **Inputs and ancestry.** The files a call's arguments name are hashed before it runs (once per
  run, cached by path, size and mtime); an input whose bytes an earlier receipt produced links it as
  a parent, so a result's ancestry is walkable and `failed` / `synthetic` (P2 §9.5) flags propagate to
  every descendant. A flagged receipt can be cited to describe what happened; it cannot ground a
  number.
- **One store per run** (`ReceiptStore.for_run`), append-only, replayed on resume, so a handle
  means the same artifact everywhere. It lives beside the run's ledger, outside every role's
  allow-list, so no role can read another's evidence and re-mint it as its own. Each unit cites through a scoped view: its own receipts, the
  run's inputs, and whatever the reports it received cite, with their ancestors.
- **Scoped grounding** (human ruling 2026-09-24, replacing "every number quoted"): a finding cites
  the receipts its numbers come from as `[handle]`, and every number in it must appear, on token
  boundaries, in one of them; other fields are grounded by all the evidence the report cites.
  `{{handle "…"}}` pins an exact span of a text receipt; for a paper the same form, with a
  locator (`{{lib:p3 "4.0 ± 0.2 %" Table 2}}`), attributes the value instead. A task's own numbers ground a text only where it cites
  `[input:…]`, for every unit including the reviewer, so a value handed in by the question cannot
  reappear as a finding unmarked. Measured before the ruling, over 231 real reports: a median
  worker report states ~240 numbers and 98% of its decimals already appear verbatim in that run's
  tool outputs, so quoting every number would have meant hundreds of quotes per report while
  scoped grounding refuses exactly the residue (mental arithmetic, rounding, sign drift). A derived
  number comes from a toolkit tool, requested through `tool_request` when none exists; the shell
  grounds nothing, so `echo`, write-then-read and `python -c` cannot mint evidence (2026-09-25,
  superseding the 2026-09-24 `python -c` route and its typed-number rule).
- **Normalisation and boundaries** are applied to both sides of a comparison and never to what is
  rendered: NFC, never NFKC (it maps `10²³` to `1023`); a U+2212 minus before a digit reads as `-`;
  paper/web text (kept at the function level; neither grounds since 2026-09-25) is compared with
  whitespace removed except between two digits (extracted text
  can split a number into `4 . 0 ± 0 . 2`), and math-italic letters read as plain ones (`𝜎` as
  `σ`). A failed quote is answered with the receipt that holds it, or the closest passage, never
  with text that lost a sign the quote carries. A needle may not sit inside a
  longer number or after a sign it lacks (`0.51` never matches `-0.51` or `10.51`), `2-8` is a
  range, `+/-` an error bar. Exempt: Figure/Table/Eq. numbers, years, source designations,
  ordinals that name a role or a step (`worker 0`, `DU 2`, `round 3`), list enumerators,
  persistent identifiers (DOIs, bibcodes, prefixed arXiv ids), and HTTP status codes with their
  reason phrase.

**Phase 3:** receipts, the store, the evidence predicate and scoped grounding keep their meaning.
Three things generalise:
- a receipt's `source` is the **unit's name** (today's role keys already are);
- "a toolkit tool" in the evidence predicate means **a tool of a toolkit registered for that unit
  at a recorded commit** (today a literal `research_toolkit.` producer prefix,
  [§9.5](#95-toolkits-per-unit));
- the producer stamp gains the **environment fingerprint** the call ran in (handoff [F18],
  [§9.6](#96-execution-sandbox-and-environments-per-unit)).


---

## 8. What Phases 1–2 built (the system as it stands)

The orientation tour; each module's `DOCS.md` is the authoritative interface, `CLAUDE_p2.md` the
full spec. Phase 3 **extracts** this — do not rebuild it.

**Foundation.** Pydantic v2 `schemas` (the protocol); a config-driven `models` layer over six chat
vendors (OpenAI, Anthropic, Google, DeepSeek, Z.AI, Alibaba; the last three over the Responses
dialect) with key failover, transient retry, a per-provider multimodal guard quoted from each
vendor's docs, provider-native tool search (`defer_loading`) and Anthropic context editing;
`observability` (LangSmith + a cost stamp on every monitor line); `storage` (SQLite);
`config_paths` (one config-directory seam, `AUTO_RESEARCH_CONFIG_DIR`); `preflight` (every no-LLM
check before any model call).

**The unit (every graph role).** One `_react` loop per turn over a tool provider, wrapped in the
quick channel (`message`, `view_receipt`, `wait_for_reply`, `list_peers`), writing one report in
its loop with `edit_report` and leaving only through `turn_end`, whose validator checks the schema,
the role's own conditions and the provenance bindings; grace rounds then `ReactBudgetExhausted` at
the cap; an accepted `turn_end` is final for its batch. Four roles instantiate it:
**reviewer** (sole I/O; `ReviewerReport.assign_to` routes the graph; renders and compiles the
report inside `turn_end`), **librarian** (search, read in full, paper/web receipts,
`LiteratureReport`), **analyst worker** (a persistent sandboxed REPL running toolkit tools;
`AnalystReport`, `tool_request` for a missing capability), **analyst lead** (one loop with no
stages; its report *is* its decision — `rework` + probe, or the result; `author_chain` briefs the
coder). The co-lead (tool-code reviewer) and the status monitor are not units.

**Provenance.** `schemas/artifacts.py` `Receipt` + `auto_research.provenance`: every tool call's
output, every file it created or changed, every page fetched, paper opened, human file and task
handed in is a runtime-hashed receipt with producer, inputs and parents; `failed`/`synthetic`
propagate; one append-only store per run beside the ledger, outside every role's reach; scoped
grounding at every unit's `turn_end`; evidence is what the analysis produced (§7).

**Consensus.** `DebateToConverge`: workers analyse, see peers' reports and figures each round, the
lead judges convergence, hard cap 3 rounds.

**Coding pipeline.** `ToolSpec` brief → an author harness (`claude_sdk` or `codex`, named on the
`coder` role) in an isolated worktree, beside the lead on the channel → the deterministic gate
(Layer A: per-test determinism maps, the no-fabrication scan, structural, provenance incl. the
static figure-shape check; the dynamic return check G15; Layer B: the tool's own tests) → the
provider-decorrelated co-lead review → `approve_edit`, one commit per tool on the branch.
**Commit ⇔ gate ∧ review.** Per-attempt base refs, gate-entry reset of a coder's own commit,
failure reset with a saved patch, submission snapshots for resume. A committed tool reloads the
registry and reruns the workers.

**Sandbox and reach.** One allow-list (`agent_fs_allow`, `agent_fs_readonly`) enforced by a path
guard and, with `agent_os_jail: true` (on), a `bwrap` jail for the analyst REPL, both author
harnesses and the gate's network-denied Layer-B run; `.git` bound read-only for the coder.
The worker REPL and the builtin shell server run under one analysis jail policy (`jail.analysis_policy`;
F20 closed 2026-10-06): toolkit read-only, a private `/tmp`, the init script sourced in a clean
environment without credentials.

**HITL and control.** Handler injection (`human_handler`), Telegram (rich messages, media both
ways) and CLI connectors, `/stop` as cancellation with a shielded cleanup, `/steer`, `/status`,
checkpointer resume, detached runs (`run_research_nohup`).

**Report and entry.** The reviewer writes plain text; `report/render.py` is the only LaTeX author;
one `v<k>/` folder per report to the human; `run_research()`, `python -m auto_research`, the
notebook. **Benchmarks:** `gate_bench` (seeded-defect catch rate), `author_bench` (authoring
reliability per coder), `science_bench` (seat trees). **Live record:** PI questions #2, #7, #12
accepted (handoff §1.1).

---

## 9. Phase 3 — generalize the MTA unit (the active phase)

### 9.1 Scope, and what was decided against

**In scope.** One unit, configured per instance; the reviewer, librarian, analyst worker and
analyst lead become definitions of it. What this buys the repository:
- the analyst workers stop being a special case — any unit may have an execution backend;
- each unit can carry its own toolkit (the librarian's literature toolkit, P2 §9.2's planned
  `lib_utils`, becomes expressible);
- the jail is a per-unit choice, applied by one launcher to every process a unit starts;
- any unit can be granted the coding pipeline, not only the lead;
- a new role costs a definition, not a class.

**Decided against (ruling 2026-10-06).** A public agent SDK, a third package, an API shaped after
another vendor's SDK, adapters into other frameworks, and a conformance suite for other
frameworks. The investigation behind the ruling (2026-09-27 → 10-06) is in the gitignored
`tmp_landscape_071526.md` §7b. In brief:
- the commodity part of an SDK — loop, providers, sandbox, subagents, hooks — ships in every
  vendor framework;
- small-team general frameworks rarely survive;
- every primitive here is individually anticipated in prior art:
  - receipts and exit gating: ECT, NabaOS, data-to-paper;
  - taint: CaMeL, FIDES, TaintDroid;
  - scoped citation: object capabilities, Agent Zero Memory;
  - every-edge validation: EnviSmart, Pydantic AI.

What survives is the **composition**, the **producer-class evidence rule** (what a unit *read* is
navigation; only what a registered tool *produced* grounds a number), and **self-laundering** as a
named failure class. Write about the system in those terms, never as "the first to bind claims to
receipts" or "the first single-exit agent". Re-open the SDK question only with a new reason.

### 9.2 The unit

```
                  task (input: receipt)   nudges   peer reports (+ receipt closure)
                            │               │               │
                            ▼               ▼               ▼
   ┌──────────────────────────────── MTA unit ────────────────────────────────┐
   │ definition: name · model (registry role) · prompt · report type · caps    │
   │                                                                           │
   │ tools:  toolkit tools ──► evidence receipts   (run in the unit's backend)  │
   │         harness tools ──► navigation receipts (read/list/web/shell)       │
   │         channel       ──► message · view_receipt · wait_for_reply · peers │
   │         spawn         ──► child units · coding pipeline   (only if given) │
   │         report        ──► edit_report · review_report · turn_end          │
   │                                                                           │
   │ execution (optional): REPL + shell, in an environment, under the jail     │
   │ turn_end validator: schema · role conditions · scoped grounding           │
   └─────────────────────────────────┬─────────────────────────────────────────┘
                                     │  the only exit
                    filed report + receipt closure  │  or honest failure (raised)
```

### 9.3 Invariants the unit enforces and no definition turns off

These are the trust boundary. A definition cannot relax one; a role's own validator may add a
refusal and never remove a built-in one.

1. **One exit.** A unit ends only through an accepted `turn_end`. A call after it in the same batch
   is answered and never run. Exhausting the cap (after grace) raises an honest failure and records
   it. No path composes an artifact on a model's behalf.
2. **Runtime-minted receipts.** Every tool call's output, and every file it created or changed, is
   hashed and stamped by the runtime, with the environment it ran in.
3. **Scoped grounding with a fixed evidence predicate.** A number stands only on:
   - a receipt of a toolkit registered for that unit, at a recorded commit;
   - a coding-pipeline `commit`;
   - a paper, by attribution with a locator;
   - the task (cite-only).

   Every harness return is navigation.
4. **Texts are not evidence.** Channel messages and a human's text mint no receipt and widen no
   citable scope.
5. **Reach is monotone down the tree.** A child unit's filesystem reach, network, environments,
   spawn rights and budgets are a subset of its parent's; a definition asking for more is refused
   at spawn.
6. **Commit ⇔ gate ∧ decorrelated review**, one tool per commit, on a branch; the runtime has no
   concept of master.
7. **No write outside declared roots.** None into `auto_research`, none into a toolkit except
   through the pipeline's worktree, none into the human's conda environments or package cache.
8. **Contained failure.** A tool or provider exception becomes an `is_error` result or that unit's
   honest failure, never a dead run (handoff F6/F15/E2). `ReactBudgetExhausted` is never caught.

### 9.4 What a unit definition holds

| Field | Today | Phase 3 |
|---|---|---|
| name | role key (`analyst_worker_0`) | the same; receipt source and channel address |
| model | registry role | unchanged |
| prompt | `prompts/<role>.md` | unchanged (+ an optional shared protocol file, §4) |
| report type + validator | per-role class, `*_turn_problems` functions | a report type and a list of validator functions |
| toolkits | one global `toolkit_root` | zero or more per unit (§9.5) |
| harness tools | fixed per provider class | chosen per unit |
| execution | analyst workers only | none, or a REPL + shell in an environment (§9.6) |
| sandbox | global `agent_os_jail` + allow-list | per unit (§9.6) |
| subagents | lead → coder only; graph routing elsewhere | none, units, and/or the coding pipeline (§9.7) |
| caps | per-role config keys | per unit |

Where definitions live — Python next to `graph/runtime.py` with values read from
`orchestration.yaml`, or a per-unit block in the YAML — is open ([§9.11](#911-open-questions)).

### 9.5 Toolkits per unit

- A unit declares zero or more toolkits (a root plus a package glob, default `*_utils`). Different
  units in one run may declare different toolkits; a tool-name collision refuses at load.
- **Evidence is earned by being in a registered, versioned toolkit.** The producer stamp names the
  toolkit and its commit, and the evidence predicate asks whether that toolkit is registered for
  the citing scope. A toolkit with uncommitted changes refuses at launch, because a receipt stamps
  a commit.
- The toolkit contract (P2 §9.2) gains **environment definitions**, committed beside the packages
  and named per package in its `tool_config.yaml` (§9.6).
- A toolkit is writable only through a coding pipeline granted to a unit that declares it, one
  pipeline per toolkit at a time.

### 9.6 Execution, sandbox and environments per unit

- **Sandbox per unit.** bwrap on or off, with that unit's reach (read-only, writable, network),
  replacing the global `agent_os_jail` / `agent_fs_allow` / `agent_fs_readonly`. Off is explicit
  and stamped on the unit's receipts.
- **Execution per unit.** Today's `PersistentSandbox` (a warm REPL, boot script, probe, per-call
  timeout, plus the shell) is available to any unit. A unit that declares a toolkit needs one.
- **One launcher.** Every process started for a unit — the REPL, the builtin server carrying the
  shell, an author harness, the gate's test runner — goes through one function that applies that
  unit's policy. F20 is closed (2026-10-06): the REPL (`sandbox/persistent.py`) and the shell
  server (`graph.runtime._builtin_server_config`) already share `jail.analysis_policy`
  (`sandbox/jail.py`), which the launcher step absorbs.
- **Environments** (handoff [F18], its own work stream; rulings 2026-10-05/06 recorded in
  `tmp_agent_envs_plan.md`). **Built and live-verified 2026-10-07** (plan B1–B5): definitions,
  the jailed builder, the coder's environment tools, the gate's Layer B in the package's
  environment, build on commit, launch reconcile and drift, replay, and routing of each toolkit
  call into its package's environment. The toolkit step (T1) landed 2026-10-07: `xspec_utils` runs
  in `ar-xspec`, and the toolkit's root `conftest.py` sorts tests into unmarked, `slow` and `live`
  (opt-in).
  - **Owned only by the coding pipeline.** Agent environments are named `ar-<name>` and are created
    or changed only by a gated, reviewed commit. Every unit lists and uses environments and never
    modifies them. A missing environment is a `tool_request` to the lead, then to the pipeline.
  - **Defined in the toolkit.** A definition lives at
    `research_toolkit/envs/<name>/{env.lock, env.pip, activate.sh, boot.py, env.yaml}`, and a
    package names its environment in `tool_config.yaml`. Activation changes only through the
    pipeline: the coder writes `activate.sh` / `boot.py`, gated and reviewed. A derived
    environment may layer activation over a human prefix, read-only.
  - **What an environment is:** a conda prefix (built with conda; pip only for what conda-forge
    lacks, pinned `name==version`), plus activation (init script, REPL boot file), plus
    human-declared externals (HEASoft, CALDB), which are stamped but cannot be rebuilt.
  - **Routing.** A toolkit call takes an optional environment; the default is the package's tested
    environment, named in the tool description. An override runs, and its receipt is flagged
    "untested env" (still evidence, fingerprint stamped). Every result names its environment.
    `use_env` switches only the unit's own REPL and shell (navigation).
  - **Human environments** (today `astropy`) are ordinary environments units use read-only. They
    serve a unit's own REPL and shell, and the packages that name no environment.
  - **Provenance.** Every receipt stamps an environment fingerprint, a lock store rebuilds any
    recorded version, and trajectories replay with their environment.
  - **No model-filled environment field in a brief.** The source environment is derived from the
    fingerprints of the receipts a brief cites.
  - **Drift** (an `ar-` environment no longer matching its definition) is rebuilt visibly:
    - the drifted prefix is kept as `ar-<name>.drift-<fp8>`;
    - the environment is rebuilt from its unchanged definition;
    - units are notified with the diff;
    - a change worth adopting goes through `tool_request` → pipeline, and a significant one (a
      changed science output) is raised, not absorbed;
    - a changed external produces a notice only.
  - **Storage.** Agent environments, packages and locks live under the runtime data dir, apart from
    the human's conda cache (human environments hardlink into `~/.conda/pkgs`, measured). No agent
    writes a human environment or that cache.
  - **Sequencing (T1, done 2026-10-07).** `xspec_utils` names `ar-xspec`, defined at
    `research_toolkit/envs/xspec/`: activation sourcing the HEASoft and CALDB externals, a boot file
    for the XSPEC session and relxill, and a lock ABI-matched to HEASoft's PyXspec (built against
    `/opt/anaconda/anaconda3/envs/astropy`, Python 3.13). Its tests give the same results there as in
    the human environment. The config-defined analysis environment (`analyze_env`,
    `analyze_ini_sh`, `repl_init_py`) stays for a unit's own REPL and shell and for packages naming
    no environment (`heasarc_utils`, `catalog_utils`); its keys retire when nothing needs it.
  - **Accepted residual risks:**
    - an upstream package removed;
    - in-place edits that escape the fingerprint, including a file a build's own package script
      writes outside the conda records (ruling 2026-10-06: `verify` checks records and pins, not
      the files a build may have dropped);
    - externals that cannot be rebuilt;
    - a user `.so` outside any environment, which is not hashed;
    - a human activating an `ar-` environment.
  - **Open:** F21, narrowed by T1 — a toolkit call routed to `ar-xspec` runs the toolkit's boot
    file in the jail (relxillCp loads, probed 2026-10-07), but `repl_init_py` lives in this
    repository, which the jail does not mount, so a unit's default REPL starts without it. Also open: a notice when a human environment changes between a
    unit's calls or runs.

### 9.7 Subagents per unit — on/off, two kinds

- **On/off by grant.** A unit with no grant declares no spawn tool. Declarations never shrink
  mid-conversation (P2 §9.3). Depth defaults to 1: a child spawns only if its own definition grants
  it.
- **Kind A — a child unit.** The child's filed report arrives in the parent's inbox with its
  receipt closure, which enters the parent's citable scope (today's peer-report rule).
  `wait_for_reply` lets time pass, and `message` resumes a finished child on its kept conversation
  (E12).
- **Kind B — the coding pipeline.** Author harness + gate + decorrelated reviewer + `approve_edit`,
  targeting a toolkit the parent declares. The brief is a `ToolSpec` (today's `author_chain`). It
  returns a `commit` receipt or an honest failure; a commit reloads the parent's registry and
  restarts its backend. Its members are not units: their exits are a gated commit or an honest
  failure (§2b).
- **Routing stays a graph for now.** The reviewer's `assign_to` routing and the lead↔workers
  debate remain LangGraph and `DebateToConverge`. Whether they become kind-A spawns is open
  (§9.11).

### 9.8 Reports and validators

- Every report type carries `status` (default `failed`, P2 §9.6). Its fields are marked by what
  they are:
  - claims, which are grounded — the default for text;
  - briefs, which are exempt and ground nothing downstream (`follow_up`, `what_to_probe`,
    `tool_request`);
  - routes, a `Literal` the graph reads (`assign_to`).

  `edit_report`, `review_report` and `turn_end` are generated from the type.
- Role conditions become **validator functions** attached to the definition: today's
  `lead_turn_problems`, `worker_report_problems`, and the reviewer's render-and-compile. Each
  returns problems, which go back into the loop as an `is_error`. A validator cannot accept what a
  built-in check refused.

### 9.9 What dissolves — special cases today, definitions tomorrow

| Today (special case) | Phase 3 |
|---|---|
| four role classes + `attach_{reviewer,lead,worker}_runtime` | one unit + four definitions |
| `SandboxToolProvider` / `ReadOnlyToolProvider` / `LibrarianToolProvider` | tool sets composed per definition: toolkit, harness, sources, report, channel, spawn |
| global `toolkit_root`, `*_utils` glob, `research_toolkit.` producer prefix | toolkits per unit; producer = toolkit + commit |
| global `agent_os_jail`, `agent_fs_allow`, `agent_fs_readonly` | sandbox per unit |
| `PersistentSandbox` for workers only (REPL and shell jailed by one policy since F20) | execution for any unit, through one launcher |
| one human analysis environment, human-configured only (F18) | environments defined in the toolkit, built only by the pipeline, chosen per toolkit call, fingerprinted on every receipt |
| `merge_flow` reading toolkit root, branch and caps from config for the lead | the coding pipeline granted per unit, targeting that unit's toolkit |
| `author_chain` on the lead only | kind-B spawn for any granted unit |
| per-role `*_turn_problems` functions | validators attached to a definition |

### 9.10 Build order (Phase 3)

Each step leaves a **working, documented increment** — update the relevant `DOCS.md` and README
before moving on, and stop to verify the checkpoint. Statuses: **Todo** · **Done**.

| # | Status | What is being built | Checkpoint (what works after) |
|---|---|---|---|
| 0 | Todo | **Invariant freeze** — §9.3 collected as one deterministic test module over today's code, from the existing per-role tests. | Green on the current tree; it is the parity oracle for every later step. |
| 1 | Todo | **One unit** — `_react` + channel + report tools + `turn_end` as one unit; the four roles become definitions with validators; exceptions contained at the unit boundary (§9.3.8). | Each role's tests pass through the unit; one live cell per role. |
| 2 | Todo | **Toolkits per unit** + the generalised evidence predicate. | Two units with different toolkits in one run; a tool of an unregistered toolkit grounds nothing. |
| 3 | Todo | **Sandbox + execution per unit; one launcher** (absorbs `jail.analysis_policy`, landed with F20). | Jail on/off per unit; every process a unit starts goes through the launcher; a unit with no backend has no shell. |
| 4 | Done (2026-10-07, on today's backend, ahead of 1–3) | **Environments** — the F18 work stream's build list, plugged into the execution backend and the coding pipeline. | Receipts carry environment fingerprints; a toolkit call runs in its package's tested environment; human environments untouched. |
| 5 | Todo | **Subagents, kind A** — grants, monotone reach, depth and count caps. | A parent spawns a child and cites its receipts; a child asking for more reach is refused. |
| 6 | Todo | **Coding pipeline as kind B** — `author_chain` → a grant any unit may hold. | A non-lead unit authors a tool into its toolkit's branch; a `commit` receipt; registry reload. |
| 7 | Todo | **Live parity run** — one PI question end to end on the generalized unit. | Accepted, with a ledger of the same shape as Phase 2's. |

### 9.11 Open questions

1. **Where definitions live:** Python next to `graph/runtime.py` with values from
   `orchestration.yaml`, or a per-unit YAML block.
2. **Routing:** keep the LangGraph graph and `DebateToConverge`, or express routing and debate as
   kind-A spawns.
3. **Jail default when `bwrap` is missing:** fail the launch, or warn and run (today: warn).
4. **The librarian's toolkit:** relocate literature search into a toolkit package now (P2 §9.2
   `lib_utils`), or keep `auto_research/sources` and give the librarian no toolkit.
5. **F21 (narrowed by T1):** whether a unit's default REPL needs a boot file at all now that
   `xspec_utils` calls run in `ar-xspec` with the toolkit's; if so, how `repl_init_py` reaches the
   jail.


---

## 10. Tech stack & conventions

- **Environment:** conda, latest stable Python (pinned in `environment.yml`; conda-forge).
- **Orchestration:** LangGraph. The reviewer→dispatch→review loop is a graph with
  conditional/cyclic edges and **hard iteration caps**.
- **Async-first:** write interfaces as `async`. I/O-bound work (LLM calls, source
  queries, DB) should be concurrency-friendly.
- **Schemas:** Pydantic v2 ([§7](#7-schemas--protocols)).
- **Storage:** SQLite via Python (papers / memory / query_log; embedding column reserved
  for vector search — [§12](#12-deferred-beyond-phase-3-do-not-build-now--leave-stubs)).
- **Observability:** LangSmith tracing + per-call cost logging in the provider layer.
- **Secrets:** `.env` (gitignored) + `.env.example`. NASA ADS token and all API keys here.
- **Runtime data dir:** a configurable directory **outside both repos**, not committed,
  holding the SQLite file, the figures/artifacts store, run logs, and the **per-worker
  sandbox scratch** (`<DATA_DIR>/runs/<run_id>/workers/<worker_id>/`, persisted per-run,
  wiped between runs). This is the only place a runtime tool may write.
- **HITL connectors:** community libraries behind the `human_handler` interface —
  `slack_bolt`, `python-telegram-bot`, Twilio ([P2 §9.8](CLAUDE_p2.md#98-human-feedback-channels-cli-vs-library)).
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

## 12. Deferred beyond Phase 3 (do NOT build now — leave stubs)

One line each; the open-work queue lives in the gitignored handoff, not here.

**Carried from Phase 2:**

- **Durable HITL suspend at `interrupt()`** — resume re-runs the node, infeasible mid-`_react`; the
  blocking handler ships instead. (Checkpointer resume and `/stop` are built.)
- **Run-wide redirect steer** — `/steer <id>` reaches one role; nothing binds every role at once.
- **OS jail by default** — `bwrap` jail built 2026-08-20 (`sandbox/jail.py`), off in the shipped
  `.example`; remaining: egress control for the non-gate processes, then default-on. (Phase 3
  makes the jail a per-unit option; its default is §9.11 question 3.)
- **Files API for media** — deferred; a referenced-PDF 413 at ~6.3 MB (anthropic-sdk-python#1098)
  suggests a tighter ceiling than the documented inline 32 MB.
- **`bash_exec` figure capture** — diff the scratch cwd for new images, synthesize `FigureRef`s.
- **RAG (ChromaDB, three targets)** — literature, persistent memory, tool index; `vector_search`
  stays stubbed. (A naive top-x literature injection for the librarian is open work, not deferred.)
- **`onboard_toolkit`** (installer increment 5) — staged raw code → gated tools; needs collaborators.
- **Lead-scientist group** — idea generation routed through the reviewer gate.
- **Provider-native web tools [I11]** — per-member routing, Tavily as fallback
  (`logs/dev/provider_matrix_20260901.md`).
- **Smaller deferrals:** concurrent HITL for parallel workers [E6]; figures routed to the coder [V-1];
  request-time data-feasibility check [H9]; Markdown/`backend.log` derived from the ledger [J1(b)];
  worker guard excluding toolkit source [F10]; trimming the ~35k-token authoring seed (whole
  `skills/__init__.py` inlined); names-only tool catalogue if it ever dominates; read-only MCP
  exploration.

**New with Phase 3:**

- **A public agent SDK / packaging the unit for other projects** — ruled out 2026-10-06
  ([§9.1](#91-scope-and-what-was-decided-against)); re-open only with a new reason.
- **A cross-framework conformance suite** — investigated 2026-10-05/06 (the anomaly classes,
  each framework's default escapes, and the precedents are in `tmp_landscape_071526.md` §7b);
  parked.
- **Non-Python tool runtimes** (a manifest entry naming a CLI) and **remote or container execution
  backends** — only the local `bwrap` launcher is in scope.


---

## 13. Loose ends (in-code stubs, deprecations, best-effort gaps)

Known gaps in the tree **today**, one entry each: the gap, its status or ruling, the site. Append as
they land; delete when closed. Distinct from §12 (unbuilt features).

**Provenance**
- **What receipts do not cover (2026-09-24):** (a) inputs a tool locates itself from an identifier
  are not hashed; (b) grounding checks a number exists in a cited receipt, not its row/column;
  (c) the static figure-shape scan is beaten by one indirection; the dynamic return check (G15)
  covers what the tests call; C-level writes and uncalled tools stay with the co-lead; (d) exemption shapes
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
- **Unattended runs:** `human` is a `message` address only with a handler. On the author side the unattended case is classified before MCP flattens it. A channel fault now arrives as a notice, and the ledger keeps `channel.reply.error`. Batches published before 2026-09-01 ran with prompts naming tools the roles lacked.
- **Worker `edit_report` misformatting** (whole report in `summary`) is mitigated by a soft note only.
- **Evidence rule, open edges (2026-09-25):** a paper readable only as a web page grounds no
  value (the librarian records it as a gap); an ObsID or band restated in a worker's text needs the
  request's `input:` handle or a toolkit result carrying it (a dispatched task no longer grounds);
  derived arithmetic waits on an authored toolkit tool. The `arg_numbers` receipt field is legacy.
- **`AnalystReport.tool_request` text is not grounding-checked** (a brief to the lead, not a
  claim); a live laundering attack put a peer's number there in an accepted report, by ruling not
  a finding (2026-09-25).
- **Reviewer as sole I/O (R1) — LIVE-TESTED 2026-09-25** (30 cells, contrast seat + one graph run on
  the configured seats): no unfiled exit, silent exit or uncited attacked number. Clause attacks
  not attempted by the model: `render_refused`, `ungrounded_number`; the edit-after-`turn_end`
  batch was declined (deterministic test only). Open: a role can still read back the librarian's fetched pages from the run dir (the
  report files and, 2026-10-06, `memory.md` moved out of reach); the reviewer admits only the latest analysis
  report's receipts; a pre-2026-09-25 checkpoint is refused on resume, not migrated.
- **Quick channel (E12) (2026-09-25):**
  - live: 25/25 ping-matrix pairs (every seat pair plus the human) and 2 attack cells, 2026-09-25;
  - the mirror to Telegram is a no-op until `TelegramHandler.notify` lands;
  - lead and worker read figures, not PDFs, over `view_receipt`;
  - a human reply after the graph node ended resumes a reply-only turn that nobody downstream reads;
  - `tests/test_live_no_hitl.py` still probes the retired `ask_human` provider surface.

**Coder and sandbox**
- **Codex coder blind spots** (on hold): a killed turn books $0; MCP startup status is not
  recorded.
- **Qwen coder pairing** (`dashscope`+`codex`) unprobed since account activation, flagged in
  `UNVERIFIED_PAIRINGS`. Only a pay-as-you-go `sk-ws-` key on `dashscope-intl` serves; Coding Plan
  keys speak Chat Completions only. Re-check: `tmp_scripts/check_qwen_entitlement.py`.
- **Shell commands are best-effort confined with `agent_os_jail` off** — `PathGuard` sees only paths
  named in a command, and its token extractor false-denies (`1/2`, URLs) [F3]; there is no kernel
  layer. With the jail on, the guard stands down only for a server whose launch recorded `jailed`
  (`ToolkitRegistry.mcp_jailed`); a configured `mcp_servers` entry keeps the scan. Toolkit-tool
  path arguments are not allow-listed.
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
- **WhatsApp/Twilio connector deprecated** — prefer `telegram` or `cli`.
- **Telegram rich messages render `||text||` as a spoiler** (hidden until tapped); agent prose is not
  escaped for it.
- **Telegram media reply leg not live-tested** — a human's photo/document/album reply is saved into `reply_dir` and minted by `mint_reply_attachments`; covered by deterministic tests only. Outbound embedding (Bot API 10.2 `rich_message.media`) is live-verified.
- **HITL slash-commands are Telegram/Slack only** — CLI and WhatsApp have no background listener.
- **Slack Socket Mode can stop delivering events after idle, silently** (bolt-python#1529, closed
  not-planned 2026-09-14); ruling 2026-09-25: Telegram is the channel, Slack not pursued.
- **`retrieval.vector_search`** — `NotImplementedError` stub (§12 RAG).
- **Environment pipeline residuals (2026-10-07):** after any unjailed build, the sticky taint makes
  every jailed build fresh until a human removes `built/unjailed.json`; each build downloads its
  packages afresh (one tool pays ~3 downloads, [F30]); input hashing resolves a worker's paths on
  the host, not in its jail ([F29]); the shell server keeps `-P`, so the analysis interpreter must
  be 3.11+; a derived environment's human base activation is copied for the jail (relative
  sourcing inside it fails loudly).
- **Provenance edges found 2026-10-07 ([R8]):** numbers inside a quote's locator are not scanned;
  a decimal tail after an exempt ordinal passes; `env_untested` is visible only to the calling
  worker; a shell-written file passed to a toolkit reader can yield evidence.

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
  `.pytest_cache` naming the broken test; confirmed no agent consulted it; ruling: numbers
  stand, builder fixed 2026-09-03.

---

## Changelog

Phases 1–2: see `CLAUDE_p1.md` and the compacted changelog at the end of `CLAUDE_p2.md` (its last
entry 2026-09-25, [R7] evidence rule, LIVE-VERIFIED 2026-09-26).

- 2026-09-27 — **Phase 3 opened: MTA as an agent SDK.** The Phase 2 file moved to `CLAUDE_p2.md`
  (spec archived, P2 §9 still authoritative for the coding pipeline as built); this file redrafted:
  three-package trust boundary (§2), SDK-neutrality rules (§3), the SDK protocol preamble (§4),
  model specs passed in rather than read (§6), receipt source and toolkit generalisation (§7),
  Phases 1–2 retrospective (§8), the Phase 3 spec (§9: the unit, the non-configurable invariants,
  the Claude-Agent-SDK-shaped API, toolkits and sandbox per unit, two subagent kinds, the
  inventory of special cases, build order 0–9, open questions), deferrals (§12). §2b, §3–§7, §10, §11, §13
  carried verbatim, with Phase 3 notes appended. **DRAFT — §9 awaits debate with the human before step 0.**
- 2026-10-06 — **§3 hard rules: two testing and review rules added** (human). A stateless
  adversarial code-review agent runs after every module change (weaknesses, shortcut logic,
  tradeoffs); every test, deterministic or live, drives the module API rather than a hand-rolled
  equivalent of production logic.
- 2026-10-06 — **Phase 3 rescoped (human ruling): generalize the MTA unit inside this repository;
  not an SDK.** The 2026-09-27 SDK draft is withdrawn after a landscape investigation (agent-SDK
  vendors, conformance-suite precedent, framework defaults read from source, prior art on five
  candidate contributions; findings in `tmp_landscape_071526.md` §7b).
  - §1 rewritten; §2 back to two repositories, plus the boundary around the human's conda
    environments.
  - §3 Phase 3 additions replaced: generalize, don't extend; behaviour parity; the shared
    execution layer with the F18 environments stream. The human's two 2026-10-06 hard rules kept.
  - §4/§6/§7 notes rewritten without SDK framing; §7 adds the environment fingerprint.
  - §9 rewritten: scope and what was decided against; the unit; invariants; what a definition
    holds; toolkits, sandbox, execution and environments per unit (F18's settled decisions
    summarised); two subagent kinds; validators; the dissolution table; build order 0–7; five open
    questions.
  - §10 SDK notes removed; §12 records the SDK and the conformance suite as parked.
- 2026-10-06 — **Environment rulings and F20 folded in** (from the F18 work stream's plan,
  approved by the human 2026-10-06; confirmed with that stream before editing).
  - §2 and §9.6: environments are defined in the toolkit, built only by the coding pipeline, and
    used read-only by every unit. Also: routing per toolkit call (tested default, "untested env"
    flag on an override), visible rebuild on drift, storage apart from the human conda cache, the
    sequencing of the toolkit step, accepted residual risks.
  - §8, §9.1, §9.6, §9.9, §9.10 step 3 and §13: F20 closed — the REPL and the shell server share
    `jail.analysis_policy`; the shell guard stands down only for a server recorded jailed.
  - §9.5: the toolkit contract gains environment definitions.
  - §9.11 q5 replaced by F21 (the REPL boot file is not mounted in the jail).
- 2026-10-06 — **§9.6 residual risks:** a file a build's package script writes outside the conda
  records is accepted as escaping the fingerprint (human ruling, after nine review rounds of the
  environment builder; `verify` checks records and pins only).
- 2026-10-07 — **Environment stream built and live-verified (plan B1–B5).** §9.6 status line; §13
  environment residuals and the provenance edges a review found ([R8]). B5 live cell
  (`tests/test_live_env_pipeline.py`): a worker's missing Boost.Python runtime became the
  pipeline-built `ar-sherpana` (Python 3.9, boost 1.87, hdf5 1.12), built on commit, the gate's tests
  and the workers' calls ran in it (`dataReader` imports), a second run reused it. Five harness bugs
  found and fixed on the way (symlinked build paths, a 4 MB record cap, `-P` on Python < 3.11, the
  REPL's 64 KB line limit, input hashing that could end a run).
- 2026-10-07 — **Toolkit step T1 and the B6 docs sweep; F18 closed.** `research_toolkit` gains a root
  `conftest.py` (unmarked / `slow` / `live`, live opt-in) and `envs/xspec/` (`ar-xspec`: production
  `solve`, 232 packages, `ixpeobssim` as a hashed wheel, HEASoft/CALDB activation, XSPEC + relxill
  boot file); `xspec_utils` names it. Parity: the `xspec_utils` suite gives 421/426 in `ar-xspec`
  and the same five failures in the human environment. Jailed probe: relxillCp loads in an
  `ar-xspec` REPL once `/data/fzhou/XSPEC/relxill` is on the read-only allow-list (config). §9.6
  status and sequencing, §9.10 step 4 Done, §9.11 q5 and F21 narrowed.
