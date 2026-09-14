# `auto_research.agents` — per-role wrappers

## Purpose
Thin wrappers around the provider layer that bind a role to its prompt
(`prompts/<role>.md`), its allowed tools, and its expected I/O schemas. One class
per role — reviewer, librarian I/O, analyst worker, analyst lead
([CLAUDE.md §8](../../../CLAUDE.md#8-what-phase-1-built-the-system-as-it-stands)).
The librarian searcher role has been replaced by the pure-Python `text_search_paths()`
and `vector_search_paths()` tools in `auto_research.retrieval` — no LLM is involved
in local retrieval.

## Architecture
Each role is a small async class:
1. Construction loads its prompt file and resolves its model via
   `auto_research.models.registry`.
2. Each public method accepts a task schema, returns a result schema.
3. Tool access (sources, retrieval, sandboxed toolkit calls) is injected — not
   imported globally — so swap-outs in tests are trivial.

**Inner ReAct loop (every role).** `AgentRole._react(seed_messages, tools, action,
cap, images)` drives a provider-agnostic loop: call the model with
`tools.tool_definitions()`; if it emits tool calls, for each it emits a
`*.{action}.tool_call` monitor event (the input — args + `tool_call_id`) **before**
dispatching, then executes via `tools.execute(name, args)` and emits the paired
`*.{action}.tool_result` (the output — returned text, same `tool_call_id`); it appends
the assistant `tool_use` turn + the `tool_result` turn, and re-calls so the model sees its own results in-context and
chains dependent calls; stop when it emits no calls or the budget is spent. **Budget
(`free_tools`, §5.5):** `cap` (`react_loop_cap`, default 30) bounds the *budget-consuming*
iterations — those calling ≥1 tool **not** in `free_tools` (default empty); a free-only
iteration (e.g. the lead's `plan_and_debate`) does not count, so the lead can debate a toolkit
design with the human across as many rounds as it needs without starving its evidence-gathering
iterations. `absolute_cap` (`react_loop_absolute_cap`, default 60; passed only by the lead's
`decide_authoring`) is a hard ceiling on *total* iterations — the runaway-free-tool backstop;
when unset it defaults to `cap`, so every other caller is unchanged (`free_tools` empty ⇒ the
loop is identical to `range(cap)`). `_react` returns
`(messages, figures, documents)`: documents a tool attaches (via `ToolOutcome.documents`,
e.g. a PDF from `open_paper`) are accumulated, deduped by sha256, and **re-attached to every
subsequent model call** so the model reads them multimodally mid-loop (`cache_prefix=True`
keeps the stable prefix cached; empty list otherwise, so non-document roles never trip the
`config.documents` guard). A tool's **figures** are set on the `ToolResultPart` at their turn
(`ToolResultPart.figures = outcome.figures`), so the adapter renders them **inline in history**
at that turn — the model keeps seeing its own plots every subsequent round and in the terminal
report call, cache-stable — rather than only on iteration 0 via `images=`. (The returned
`figures` list still accumulates them so `analyze()` can staple real `FigureRef`s onto
`AnalystReport.figures`, which structured output cannot reproduce.) A terminal `_call_over_messages(...)`
then produces the role's structured output grounded in the trajectory (both reuse
`_structured_loop`, the shared off-schema-retry core). Tool surfaces live behind
the `ToolProvider` protocol in `react.py`: `SandboxToolProvider` (analyst science
tools in the `PersistentSandbox`; when `orchestration.yaml: tool_search_enabled` is set it marks
every *registry* tool `defer_loading: true` so its schema loads on demand via the provider's
tool search, while the harness tools it appends stay resident — see `models/DOCS.md`.
Live-verified 2026-07-28 and enabled), `ReadOnlyToolProvider` (reviewer/lead/co-lead — in-process
`read_file`/`list_dir` confined to its `roots` (the permitted roots are named in each tool's
description and echoed in the `path not permitted` error so the model self-corrects instead of
guessing). `read_file` reads **by line range** (`offset`/`limit`, default `_DEFAULT_LINE_LIMIT`=1000
lines, each capped to `_MAX_LINE_CHARS`=2000) via the module helper `_read_lines`, returning
`cat -n`-numbered lines; when the file is longer than the window the output ends with a note naming
the next `offset`, so the lead can page through long toolkit source instead of reading blind (F8) —
truncation is signalled, never silent. It is text-only (local PDFs go through `open_paper`). Also an
optional **knowledge-base toolset** — `text_search` (returns candidate paper
paths) **and `open_paper`** (attaches a chosen local PDF as a document, capped at
`_OPEN_PAPER_CAP`=6 distinct papers, recording `opened_paths`). It applies **no size limit of its
own**: whether a PDF fits is a provider question — bytes, page count, and how much other media the
request already carries — answered per request by `models/providers/_media.py`, which is the only
layer that sees all of it. A document the provider cannot take comes back as a model-visible note
naming the file and the reason, so the caller cites it from `text_search` metadata instead. Both
tools are gated by
`include_text_search` (default on; the §9.3 co-lead omits it and points `roots` at the
toolkit worktree instead), plus the shared **web pair** `web_search` + `web_fetch`, present
whenever a `web_source` (Tavily) is attached — standard on the reviewer, analyst lead, and
co-lead (and, via `SandboxToolProvider`, the analyst worker). `web_search` returns the top-k
results (title/URL + the provider's query-aware teaser, uncapped by us — the `limit` is the only
cap); `web_fetch(url, offset, limit, depth)` reads one page in full as Markdown (Tavily Extract,
`depth="advanced"` default) and pages it exactly like `read_file` (shared `_page_text_lines` +
resume footer). The `WebSource` client is lazy so attaching it never reads the key at construction
(`TAVILY_API_KEY` is a launch preflight requirement)),
`SourceToolProvider` (librarian — arXiv/ADS/web + local `text_search`). A role with
`tools=None` falls back to a plain single structured call.

**Inter-LLM report authoring — `edit_report` / `review_report` (V-4).** Every analyst report
(worker→lead, lead→reviewer) is a per-round JSONL the agent ReAct-refines **through tools**, so
analysts keep their read-only discipline (the runtime writes; the agent never touches disk). Both
providers grow the pair when a `ReportWorkspace` is attached (`report_path` + `report_model` +
an append-only per-source figure `catalog` + subordinate `source_reports`): `edit_report`
validates the report args into the model, resolves `cited_figures` (`{source, index}` handles)
against the catalog → the ordered `figures`, and persists the JSONL; `review_report(source=None)`
reads the draft back plus the whole citable catalog (every figure attached, `[source:index]`-
labelled), or a named subordinate's finished report (numbered `Figure N` images). Figures are
cited, never attached directly: the resolver (`resolve_figure_citations`) drops out-of-range /
unknown-source citations with a note and keeps a doubly-cited figure once — **citation is the
de-duplication**. `resolve → figures` runs at the tool boundary; the provider renders each figure
caption verbatim (the `Figure N —` / `[source:index]` prefix comes from the presentation layer,
`_report_view` / `_catalog_menu`), so the prose "Figure k" lines up with the k-th attached image.
The worker's catalog is one source `"self"` (its produced figures, appended live during the loop
via `figure_sink`); the lead's is one source **per worker** (its cited figures), both append-only
across debate rounds *and* rework iterations (the agent instances persist), which is what closes
the "last-round-only figures" leak. Round files are `<role>_round<n>_report.jsonl`
(`sandbox.workspace.round_report_path`; `n` = the monotonic debate round). Both tools are
budget-free in the loop (report authoring, not evidence gathering); a loop that ends without an
`edit_report` write falls back to one terminal structured compose with the catalog figures attached.

**Raise-to-human (`ask_human`, §9.7/§9.8).** When a `human_handler` is wired, both
`SandboxToolProvider` and `ReadOnlyToolProvider` expose one shared `ask_human` tool
(`ask_human_tool_def`/`execute_ask_human` in `react.py`): the role writes a Markdown `message`,
which builds a `HumanQuery`, and **blocks** on the injected handler (via `hitl.resolve_human`)
until the human replies — the same model the SDK author uses. With no handler the tool is not offered, and a direct call
raises `HumanInputRequired` (the headless fail-safe). `run_research` threads the resolved
handler to the workers (`SandboxToolProvider`) and to the reviewer/lead (`ReadOnlyToolProvider`,
`asked_by` = the role). Each provider also carries the backing `model` id (resolved via
`_model_label`), set on the `HumanQuery` so connector message bodies identify the sender as
`"<role> (<model>)"`.

**Lead design channel (`plan_and_debate`, §5.5).** When `include_plan_and_debate=True` **and** a
`human_handler` is wired, `ReadOnlyToolProvider` also exposes `plan_and_debate`
(`plan_and_debate_tool_def`/`execute_plan_and_debate`) — set only for the **analyst lead** (in
`graph/runtime.py`). It routes the lead's Markdown design report (proposal, options/trade-offs,
interface sketch, the ask) to the human via the same `resolve_human` path (reusing the
`clarify_science` `HumanQuery` kind), but is listed in the lead's `_react` `free_tools`, so the lead settles a toolkit design
with the human across unbounded rounds **without consuming `react_loop_cap`** — only the
`react_loop_absolute_cap` backstop bounds it; the human ending the discussion is the normal stop.
The coder then follows the settled brief rather than re-opening the architecture.

**Lead authoring tools (`drive_coder`/`submit_for_review`, §9.3 B-topology — A1 slice 2).** When an
`authoring_session` (an `auto_research.sandbox.merge_flow.AuthoringSession`) is wired,
`ReadOnlyToolProvider` exposes two **lead-only** tools — masked to the lead alone, because a session
is attached only on its provider (no other role sees them; this is the mask). `drive_coder`
(`execute_drive_coder`) steers the live Sonnet coder one turn (the lead is the plan owner, the coder
executes); `submit_for_review` (`execute_submit`) runs the deterministic gate, then (on gate-pass)
the provider-decorrelated GPT co-lead review, and **commits on gate-pass ∧ review-pass** — the lead
never self-approves (`commit ⇔ gate ∧ review`, §9.9). The gate is a mechanical wall the lead can only
*satisfy* via `drive_coder`; the review is *debatable* (the slice-4 `respond_to_review`). Both will be
listed in the lead's authoring-loop `free_tools` (slice 3), bounded by `submit_cap` +
`react_loop_absolute_cap`. They drive the **same** `AuthoringSession` primitives `propose_and_pr`
drives in its auto-loop — the difference is *who* diagnoses each verdict (the lead, vs. the coder).

**The lead's cycle (`LeadCycle`, §9.3).** The lead's turn is **one ReAct loop with no stages**,
ending on `turn_end`, and **its report is its decision** — the same contract an analyst already
writes. It has exactly one form left: `author_chain` (`LEAD_FORM_TOOLS`), which is a *brief* handed
to a coding subagent, not a decision, and which does **not** end the turn.

*What this replaced, and why.* The lead used to state each decision by filing one of five forms,
and a filed form was a `terminal_tool`. While authoring, the only two forms it could file were both
whole-cycle terminals — so "this link is dead, try the next" was inexpressible. Slot 1 of the
2026-09-11 batch ended a whole analysis with **11 of 12 author credits unspent**; slot 2, from the
identical position, spent a debate round and a full worker rerun to say the same thing. Two of the
four retired handlers validated nothing anyway: they copied an already-validated report onto the
cycle and differed from each other by one field the report itself carries.

Four properties carry the design:

- **The impossible state is inexpressible.** `author_chain` takes the first link as a **required
  property** (`first_link` + an optional `additional_links` tail). Cardinality keywords are
  enforced on no path — a live lead filed an empty `minItems: 1` array straight through
  (2026-09-02) — while `required` is enforced under strict mode and degrades correctly elsewhere.
  The handler still checks: belt and suspenders.
- **Every question carries its own description.** Each field's prose is read from the Pydantic
  `Field(description=...)` of the model it validates back into, so the worker's view and the
  lead's view of one object cannot drift, and no field is filled blind — a `#` comment never
  reaches JSON Schema, which is the defect behind the $19.43 run.
- **Declarations never shrink.** `_authoring_declared()` gates the authoring tools on
  `cycle.authoring_enabled` rather than on a live `authoring_session`, so swapping the session per
  chain link never changes the declared set (the 2026-07-29 `Tool reference … not found`
  run-killer).
- **The chain advances on a tool result.** `LeadCycle.advance_chain` — supplied by `run_cycle`,
  which owns every session and every git operation (§9.9) — closes the finished link and opens the
  next, returning the sentence the lead reads. That is what lets ONE loop span a whole chain: a
  dead link is a sentence in the conversation rather than the end of the turn.

**`turn_end` is the door (`lead_turn_problems`).** `edit_report` stays permissive so a partial
write mid-turn is legal; completeness is checked once, here, and a rejection is an `is_error` that
puts the model back in its loop with its tools. It refuses: no report written; `rework` without a
probe or a rationale, or with no rounds left (the cap named); a conclusion with no findings; a
cited figure whose file is not on disk; and a coding session still open. Conditional
required-fields is deliberately *not* expressed in the schema — it is not portable across the six
vendors, and a schema that refuses a legal intermediate state fights the write-freely contract.

**One report class, two surfaces.** `AnalystReport` carries the lead's `rework` / `rationale` /
`what_to_probe` alongside the worker's `tool_request`, and `ReportWorkspace.prose_fields` +
`status_values` decide which a role is offered — the worker's surface omits the decision fields and
`needs_human`, the lead's omits `tool_request`. The alternative, a subclass per role, would have
widened `status` in a subtype so anything typed as the worker's report could receive a value the
worker may not file.

`LeadCycleOutcome` is what `run_cycle` returns: the report (the decision), plus the authoring
bookkeeping (`decision` = the brief written, `authored`, `committed`, remaining `author_budget`)
the caller needs to reload discovery and rerun the workers. `report=None` means the lead never
wrote one, which the caller treats as the failure it is — nothing is composed on its behalf ([F6]).

When a `RunMonitor` is injected, role wrappers record high-chatter input,
parsed output, raw model content, attachment counts, and errors. These records
are for human inspection only and are never fed back into runtime prompts. Model-backed
role events include the configured model string in monitor payloads, and each
ReAct tool result gets a compact monitor event (tool name, ok/error status,
result character count, and figure count) before the iteration summary.

Critical: **runtime LLMs never see `CLAUDE.md` or any `DOCS.md`**
([CLAUDE.md §4](../../../CLAUDE.md#4-claudemd-and-docsmd-are-build-time-only)).
Only the file in `prompts/` plus task data goes into the model.

Current role capability surfaces:

| Role | Tool surface | What "one task" means |
|---|---|---|
| Librarian I/O | `SourceToolProvider` over arXiv, ADS, Tavily web, and local text search. | Real source/document input can produce structured search results or memory abstractions. |
| Local retrieval | Pure-Python `text_search_paths()` over SQLite. | No runtime LLM role; returns local paper paths for reviewer attachments. |
| Analyst worker | `SandboxToolProvider` over `research_toolkit` manifest tools plus MCP tools in a persistent sandbox, plus `web_search` + `web_fetch`. | Runs the full analysis tool chain inside `analyze()`, then reports grounded results and figures. |
| Analyst lead | `ReadOnlyToolProvider` over run artifacts, papers, and `research_toolkit`; `web_search` + `web_fetch`; the five **cycle forms** + the three authoring tools when a `LeadCycle` is wired. | One cycle: investigate, triage the tool request, drive the chain, judge, and file the form that carries the decision. Also answers author questions. |
| Analyst co-lead | `ReadOnlyToolProvider` over the **toolkit worktree** (`include_text_search=False`) + `web_search` + `web_fetch`. | Provider-decorrelated **code review** of one authored tool: brief-match, Layer-B check adequacy, no-fabrication second pass, subtle bugs (§9.3 step 4). |
| Reviewer | `ReadOnlyToolProvider` over run artifacts, figures, papers, memory, and `research_toolkit` (to read tool source/`TOOL_MANIFEST` and confirm correct call parameters), plus `web_search` + `web_fetch`. | Dispatches, reviews, requests capped rework, and composes final report handles. |

## Interfaces / Contracts
Prompt loading:
- `load_prompt(role, root=None) -> str` loads `prompts/<role>.md`, strips leading
  and trailing whitespace, and raises `ValueError` if the prompt text references
  build-time governance filenames.
- `prompts_dir(root=None) -> Path` returns the prompt directory, allowing tests to
  inject a temporary prompt root.
- `PROMPT_FILENAMES` maps runtime prompt roles to their Markdown filenames, including
  the Phase-2 `coder` prompt used by `merge_flow`.

Role wrappers:
- `ReviewerAgent.review(request, *, report=None) -> ReviewVerdict`. When a
  `ReadOnlyToolProvider` is attached (`tools`), the reviewer investigates run
  artifacts/figures/memory and **pulls literature itself** (`text_search` →
  `open_paper`) via a ReAct loop; the opened PDFs arrive as `DocumentRef`
  attachments (threaded through the loop) and are forwarded into the terminal
  verdict call. With no `tools` it makes a single structured call (no attachments).
- `LibrarianIOAgent(sources, storage, ...) -> LibrarianIOAgent`. Constructor
  now accepts a `storage: Storage | None` (defaults to `Storage()`). The IO
  agent satisfies both `IOAbstractionWorkerLike` and `IOAbstractionLeadLike`
  protocols for use in `IODebateToConverge`.
- `LibrarianIOAgent.compose_search_strategy(request, *, follow_up=None) ->
  SearchRequest` — LLM call. Translates a research request (and optional
  reviewer follow-up) into a focused `SearchRequest` (query, sources, limit).
  Called by the graph before each online search pass.
- `LibrarianIOAgent.search(request) -> list[SearchResult]` — no LLM call.
  Queries external sources and returns raw results. Intent-blind: does not
  filter or rank.
- `LibrarianIOAgent.gather_literature(request, *, follow_up=None) ->
  list[SearchResult]` — ReAct loop over a `SourceToolProvider` (arXiv/ADS/web +
  local `text_search`): the model searches, reads results, refines its query, and
  searches again until it has a focused set; returns de-duplicated results. Falls
  back to `compose_search_strategy`→`search` when no sources are configured. The
  graph `literature_node` prefers this over the single-shot path.
- `LibrarianIOAgent.ingest_document(document, *, peer_abstracts=(), paper_id=None,
  instructions=None) -> MemoryRecord` — LLM call. Passes a `DocumentRef`
  attachment to the configured I/O model and returns one memory abstraction.
  `peer_abstracts` are serialized into the payload for IO debate rounds.
- `LibrarianIOAgent.abstract_document(document, *, peer_abstracts=()) ->
  MemoryRecord` — worker shim over `ingest_document()`. Used by
  `IODebateToConverge` as the worker-role method.
- `LibrarianIOAgent.judge_abstract_convergence(document, abstracts, *,
  round_index, max_rounds) -> ReviewVerdict` — lead-role convergence check for
  IO debate.
- `LibrarianIOAgent.adjudicate_abstract(document, abstracts) -> MemoryRecord`
  — lead-role synthesis of the final memory record from worker abstractions.
- `LibrarianIOAgent.ingest_results(results) -> None` — downloads, abstracts,
  and stores each `SearchResult` concurrently (`asyncio.gather`, capped at 4). For
  PDF-bearing results: downloads via `download_and_ingest_result()`, runs
  `IODebateToConverge` (2 worker passes + lead), writes `mem_abbrev` to storage.
  For results without a PDF: records an **orphan memory** note (`paper_id = NULL`,
  `mem_abbrev = result.abstract`, no LLM call, no paper row) — a paper row requires a
  downloaded PDF (its sha256 is the identity). **Each paper's ingest is isolated**: any
  failure (a document the model API rejects, a provider fault, an oversized PDF) is caught,
  logged as `librarian_io.ingest_document.error`, and degraded to the orphan-note path —
  one bad paper can never propagate out of the batch and abort the `literature` node (the
  shared `_store_metadata_only` helper is the fallback).
- `LibrarianIOAgent.evaluate_search_results(request, results) -> ReviewVerdict`
  — Phase 1 stub returning `accepted=True`. See CLAUDE.md §12.
- `AnalystWorkerAgent(*, sandbox=None, registry=None, memory_path=None, ...)`.
  `AnalystWorkerAgent.analyze(task, *, peer_reports=()) -> AnalystReport` runs the
  inner ReAct loop over a `SandboxToolProvider` (when `sandbox`+`registry` are
  attached) — chaining toolkit/MCP calls to completion — then **writes the report via
  `edit_report`** (V-4), citing figures from its append-only `self` catalog by index; the
  persisted report is read back. `last_tool_results` exposes the loop's `ToolResult`s for the
  consensus peer summary. With no sandbox/registry it falls back to a plain structured call; a
  loop that never calls `edit_report` falls back to one terminal compose with catalog figures
  attached. `agent_key`/`report_dir` (set by runtime) name its catalog source + report folder.
- `AnalystLeadAgent.run_cycle(task, reports, *, sources=None, rounds_left=0, author_budget=0,
  toolkit_root=None, worktree_base=None, direct_branch=None, colead=None, human_handler=None,
  coder=None, allow_correlated_review=False) -> LeadCycleOutcome` — **the lead's whole turn**, one
  ReAct loop over one conversation, **with no stages**, ending on `turn_end`. It investigates,
  optionally briefs a coding chain, drives and submits each link, writes its report, and ends.
  **The report is the decision** (A3): `rework=true` sends the analysts back, `rework=false` makes
  it the analysis result. This conversation declares no output schema at all.
  - **`author_chain` is a brief, not a terminal.** Filing it writes `cycle.brief` and opens the
    first link's session; the turn continues. Before A7 every action open while authoring ended
    the whole cycle, which is how slot 1 finished an analysis with 11 of 12 author credits
    unspent.
  - **The chain advances on a tool result.** `_ChainDriver.advance` (wired to
    `LeadCycle.advance_chain`) closes the finished link — committed, or its review submissions
    spent — records its `MergeFlowResult`, and opens the next, returning the sentence the lead
    reads. When the chain runs out it says so and points at `edit_report` + `turn_end`. Every
    session lifecycle and every git operation stays in Python (§9.9); the model asks for the next
    link, it never builds one.
  - **`turn_end` validates** through `lead_turn_problems` (above): no report, `rework` without a
    probe/rationale or with no rounds left, a conclusion with no findings, a cited figure whose
    file is missing, a coder still running. A refusal is a correction, not a finish.
  - **The coding span is compacted** per link (`_compact_coding_span`): the call that opened the
    session keeps its answer, and that answer is rewritten to the link's outcome — code-owned
    facts (status, commit sha, submissions spent, credits left) around a `compactor`-written
    paragraph on the span. Both arms compact; the failure wording ends by handing the lead the
    choice to proceed, re-plan or retry, which is how a rejection stays actionable without
    keeping its transcript. Rewriting rather than cutting is what keeps the opening call paired.
  - **Figures.** Accumulates each worker's cited figures into a per-worker catalog source (keyed by
    `sources[i]`, append-only across rounds/iterations) and attaches them to the cycle's first
    call; the report is written through `edit_report` citing `{worker, index}`. The lead — not the
    consensus — owns the report's figures (V-4).
  - **No tool surface ⇒ no report.** `self.tools is None` returns `report=None` rather than
    composing a stand-in ([F6]). A loop that wrote a report but ran out of budget before
    `turn_end` has that report used — real evidence it produced — while one that wrote nothing
    returns nothing.
- `AnalystLeadAgent.clarify_for_author(message, *, kind) -> str` — answers a
  tool author's `ask_lead` clarification (the lead↔author channel, §9.3), sent as a single
  Markdown `message`; consults
  `web_search` (when a `ReadOnlyToolProvider` with a `web_source` is attached) before
  answering rather than guessing. Used by `merge_flow.propose_and_pr`.
- `AnalystLeadAgent.drive_authoring(tool_spec, *, toolkit_root, worktree_base, ...) ->
  MergeFlowResult` — **one** tool through authoring on its own conversation: build + connect an
  `AuthoringSession`, seed the coder, run the lead's loop over `drive_coder` /
  `submit_for_review` / `respond_to_review`, read the terminal result, `aclose`. `run_cycle`'s
  `_ChainDriver` is the same lifecycle over the *cycle's* conversation; this standalone entry
  remains because `benchmarks/author_bench` measures exactly it (its published K=10 batches
  priced this call), so the instrument is unchanged by the cycle refactor.
- The honest terminal after a non-committed attempt is no longer a separate method, and since A7
  it is no longer a separate *form* either: the lead reads the failure in its own conversation and
  writes it into its report (`rework=false`, `status=failed` or `needs_human`) — documenting the
  unavailable capability and any partial findings that hold, never fabricating, never re-requesting
  the tool. The report flows to the reviewer (sole I/O), §9.6.
- `AnalystColeadAgent.review_tool(tool_spec, *, diff, gate, tools=None) -> ToolReviewVerdict`
  — the Phase-2 step-4 provider-decorrelated **code review** (§9.3). Payload carries the
  `ToolSpec` brief, the authored `diff` bundle, and the (already-passed) `GateReport`
  (including its `findings`, the literal-array intent calls the co-lead adjudicates). With a
  read-only worktree/web `ToolProvider` it investigates the surrounding code first, then
  issues the verdict; `tools=None` makes a plain structured call. Driven by
  `sandbox.review.make_review_hook`, routed to a provider ≠ the Claude author's.
- All wrappers accept an injected `model`; otherwise they resolve their role
  from `config/models.yaml` and use the first configured group member unless
  `model_name` is supplied.
- All wrappers accept an optional `monitor: RunMonitor | None`. At
  `chatter=10`, each model-backed public method records its structured input and
  parsed output. Errors are recorded at low chatter before being re-raised.
  Role-originated monitor events include the configured model string when the
  backing model exposes it; ReAct loops additionally emit visible per-tool
  result summaries at `chatter=7`.
- Live role verification is opt-in:
  `AUTO_RESEARCH_LIVE_AGENT_TESTS=1 pytest tests/test_live_agents.py -s`.
  It runs one real model call for each role and writes JSONL plus Markdown logs
  under `logs/dev/step-8/`.

## Dependencies
- Internal: `auto_research.models`, `auto_research.schemas`,
  `auto_research.sources`, `auto_research.retrieval`,
  `auto_research.observability`.

## Dictionary-level reference
- `PromptRole` — literal type for the runtime roles, mirroring `config/models.yaml` role
  keys: `reviewer`, `librarian_io`, `analyst_worker`, `analyst_lead`, the Phase-2
  `analyst_colead` (the co-lead reviewer), and `coder` (the tool author).
- `PROMPT_FILENAMES` — role-to-filename mapping used by the loader.
- `FORBIDDEN_RUNTIME_REFERENCES` — tuple of build-time governance filenames that
  runtime prompts must not reference.
- `prompts_dir(root=None)` — returns an injected prompt root or the repository's
  top-level `prompts/` directory.
- `load_prompt(role, root=None)` — loads and validates one runtime prompt.
- `CallableModel` — protocol for injected model handles in role tests and
  runtime wrappers.
- `AgentRole` — base class that loads the role prompt, selects the model, sends
  task JSON, validates JSON responses, and stores the most recent
  `ModelResponse` on `last_response` for logging. Optional `monitor` records
  model-call inputs, outputs, and errors.
- `ReviewerAgent` — reviewer wrapper. Method: `review(request, *, literature: Sequence[Path], report)`.
  Loads each path as a `DocumentRef` attachment for full-text access.
- `LibrarianIOAgent(sources, storage, ...)` — IO librarian. Constructor accepts
  injected `sources` and `storage`. Methods: `compose_search_strategy()`,
  `search()`, `ingest_document()`, `abstract_document()`,
  `judge_abstract_convergence()`, `adjudicate_abstract()`, `ingest_results()`,
  `evaluate_search_results()`.
- `AnalystWorkerAgent(*, available_toolkits, sandbox=None, registry=None,
  memory_path=None, worker_index=0, ...)` —
  analyst worker wrapper.
  - `analyze(task, *, peer_reports=())` → `AnalystReport`. Reads `memory_path`
    and peer reports, runs the inner ReAct loop over `SandboxToolProvider` when
    `sandbox` + `registry` are attached, appends real `ToolResult`s to durable
    memory, and returns an `AnalystReport` with real loop figures attached. With no
    sandbox/registry it falls back to a plain structured call.
  - `last_tool_results` → `list[ToolResult]` — populated by the latest ReAct loop for
    consensus peer summaries.
  - `is_alive` → `bool` — `True` when no sandbox attached; delegates to
    `sandbox.is_alive` otherwise.
- `AnalystLeadAgent` — analyst lead wrapper. `agent_key`/`report_dir` (set by runtime) name its
  report folder; `_figure_catalog` is the per-worker figure catalog (append-only across rounds +
  iterations). Methods: `run_cycle(...)` (the whole turn — one loop, one conversation, ends on a
  filed form), `drive_authoring(...)` (one standalone authoring attempt, `author_bench`'s
  instrument), `clarify_for_author()`. Internals: `_accumulate_figures()`,
  `_make_report_workspace()`; the chain's sessions are owned by the module-level `_ChainDriver`.
- `AnalystColeadAgent` — Phase-2 provider-decorrelated co-lead reviewer wrapper.
  Method: `review_tool(tool_spec, *, diff, gate, tools=None) -> ToolReviewVerdict`.

## Changelog
Compacted 2026-09-01: entries through 2026-08-31 folded into themed ranges (detail in git history).

- 2026-05-26 -> 2026-06-18 — **Agent wrappers built, ReAct-wired, then extended for Phase 2.** Reviewer,
  librarian I/O, analyst worker and analyst lead with native structured output / function calling,
  bounded schema retries, `Sandbox`/`ReadOnly`/`SourceToolProvider`, report composition, monitor hooks,
  the `coder` prompt. Phase 2 added: lead read-only toolkit inspection + `web_search` +
  `clarify_for_author` (`ask_lead`); `AnalystWorkerAgent.analyze` as the sole tool-execution owner;
  `AnalystColeadAgent.review_tool → ToolReviewVerdict` + the `analyst_colead` role; `ask_human`/
  `human_handler` on both providers; `decide_authoring → AuthoringDecision`; shared `data_catalog`;
  self-describing `roots`; source faults degrading to a recoverable `is_error`; manifest-quality
  `react.py` tool descriptions.
- 2026-06-22 -> 2026-06-30 — **Diagnosability, the FS jail, and lead-driven authoring.** `_react` returns
  `(messages, figures, documents)` and splits each call into `*.tool_call`/`*.tool_result` events;
  `open_paper` (`cited_paths`); `ingest_results` isolates each paper; the FS jail (`ReadOnlyToolProvider`
  denies governance basenames, `SandboxToolProvider` uses `PathGuard`); `_react` gained
  `free_tools`/`absolute_cap` plus the lead-only budget-free `plan_and_debate` channel.
  `finalize_authoring_outcome` composes ONE honest terminal `AnalystReport` (§9.6) instead of the
  consensus loop spinning; lead-only `drive_coder`/`submit_for_review`/`respond_to_review` +
  `AnalystLeadAgent.drive_authoring` drive an `AuthoringSession` directly (gate → GPT review → commit,
  with an anti-sycophancy lead↔co-lead debate; commit ⇔ gate ∧ review); clarification channels take one
  Markdown `message`.
- 2026-07-06 -> 2026-07-14 — **Robustness, web tools, and V-4 citation reports.** [F4] returned figures
  route through `_parse_tool_figures` (a bad figure is dropped with a note, never fatal); **[A8]**
  `decide_authoring` returns the ordered `tool_specs` chain planned up front. `open_paper` refuses a PDF
  over 8 MB (stat-only, before read) — an opened PDF rides by value in every later call and one huge
  paper hit a live Anthropic 413. [I1]/[I10]/[F8]: `web_search` wired to reviewer + lead + worker (was
  co-lead only) and de-capped, new paged `web_fetch`, and `read_file` reads by line range with a resume
  footer instead of a silent char head-cut. **V-4:** `edit_report`/`review_report` appear whenever a
  `ReportWorkspace` is attached; `edit_report` resolves `cited_figures` ({source,index}) → ordered
  `figures` and persists the per-round JSONL; the worker writes via its `self` catalog and the lead cites
  by `{worker, index}` — citation replaces figure-dedup, closing the last-round-only leak.
- 2026-07-20 -> 2026-07-28 — **Catalog relocation, context-edit visibility, steer/stop, deferred tools.**
  `data_catalog` is no longer a built-in: its definition and read come from
  `research_toolkit.catalog_utils`, so workers get it through `ToolkitRegistry` and run it in the sandbox
  like any toolkit tool, while the sandbox-less lead runs the same pure-stdlib read in-process
  (`_import_from_toolkit` + `execute_data_catalog`, taking `obs_db_path`, not `mission`).
  `_record_context_edit` emits a `<role>.<action>.context_edit` event when a call reports provider-native
  context editing (Anthropic only; live-verified 2026-07-28). §12 Stage 3: `AgentRole` gained
  `run_control` + `agent_key`, draining its steer inbox into a `[Human steer]` user turn at the top of
  each `_react` iteration. **Deferred tool loading:** under `tool_search_enabled` every registry tool is
  emitted `defer_loading: true` while harness tools stay resident (satisfying both providers' "≥1
  non-deferred tool" rule, no per-tool exceptions), with discovery blocks carried through replay so a
  discovered tool stays callable. Live-verified on both providers 2026-07-28 and enabled: resident input
  19,074 → 3,789 (`gpt-5.5`) / 35,208 → 9,003 (`claude-sonnet-5`), the expanded definition sitting inside
  the conversation cache breakpoint.
- 2026-07-29 -> 2026-07-31 — **Human control, then its replacement by the interrupt; `/status` reads the
  ledger.** `_record` actors events by the **indexed `agent_key`** (`analyst_worker_1`), without which two
  workers are indistinguishable and no `/steer` target can be named. Mid-batch stop-flag reading, the
  top-of-iteration break, the mid-batch skip and their events were then **deleted**: a `/stop` cancels the
  run task. **Steering is untouched** — a message can wait. The lead publishes its live `AuthoringSession`
  on `RunControl` while `drive_authoring` runs so an interrupt can reset the toolkit branch (that method's
  own `finally` dies inside the cancelled scope); first path there clears the handle, `aclose()` is
  idempotent. **`MonitorToolProvider` + `StatusMonitorAgent`:** a fourth provider over the run's own
  `monitor.jsonl` — `brief_history` (index, filterable by level/sequence/prefix/actor/team/substring),
  `ledger_event(sequence, max_chars, bias)` (one event **with its payload**, windowed, negative `bias` for
  the tail) and `ledger_grep` (by payload content, index rows only). Structural, not prompted:
  **read-only** (no tool takes a path), **bounded** (400 rows, 32k payload chars, clamped in code — cf.
  openai/codex#27131), **fresh** (re-read per call). Driven under `status_react_loop_cap` (default 6 — a
  human is waiting on a phone), asking once more with no tools rather than returning silence on cap;
  `prompts/status_monitor.md` teaches the level convention (1 = most important … 10 = chattiest, so
  "important only" is a *low* `max_lvl`). Tests: `tests/test_run_control.py`,
  `tests/test_monitor_tools.py`.
- 2026-08-05 -> 2026-08-31 — **The replay seam, the terminal compose, and two layering corrections.**
  `_react` appends the turn it received whole (`messages.append(response)`); `AssistantToolUse` is gone,
  `ModelResponse` being the neutral assistant turn itself. The old field-by-field copy of
  `content`/`tool_calls`/`provider_blocks` had silently dropped tool-search discovery blocks until
  2026-07-27 and Anthropic `thinking` blocks until 2026-08-05; adapters now own their provider's
  documented replay rule (`models/DOCS.md`), and the loop still ends when the model emits no tool call.
  **The terminal compose declares the loop's tools:** `_call_over_messages`/`_structured_loop` take the
  same `ToolProvider` `_react` just used and pass `tools=…, tool_choice="none"`, since a compose replaying
  a trajectory that references those tools orphans them if the declarations are omitted — the 400 that
  killed run `8c75990e` on the co-lead's `review_tool`. `"none"` rather than `"auto"` because the compose
  must answer; a model that calls again returns `parsed=None` and burns a `structured_retry_cap` attempt.
  All seven compose sites pass their provider (reviewer `review`, worker `analyze`, lead
  `adjudicate`/`clarify_for_author`/`decide_authoring`/`finalize_authoring_outcome`, co-lead
  `review_tool`), as does `StatusMonitorAgent`'s cap-exhaustion fallback, which had the identical
  tool-less shape on the GPT reporter — a plausible cause of the open *"`/status` returns silence on cap
  exhaustion"* defect. Payload-only `_call` is unchanged (no trajectory, nothing to declare).
  **`open_paper` no longer caps PDF size:** `_OPEN_PAPER_MAX_MB` (8 MB) and its stat-check are deleted as
  the wrong layer, blind to the provider and to the rest of the request; the limits that actually fail a
  call (page count, image count, total payload) live per adapter in `models/providers/_media.py`. The
  distinct-paper cap (`_OPEN_PAPER_CAP`=6) stays — attention, not provider limits. **[F11] applies to a malformed argument:**
  `ReadOnlyToolProvider.execute` coerced `offset`/`limit` with bare `int()` inside a `try` catching only
  `OSError`, so a model-sent `offset='150, '` raised `ValueError` out of the provider — live, twice, in the
  gate-bench swapfix batch (claude_opus_5 on two real-base changesets, $0.05 each, whole review lost); it
  is the model's slip to correct, so `ValueError`/`TypeError` now return an `is_error` tool result naming
  the expectation, never a crash. Tests: `tests/test_react_tools.py`
  `::test_react_replays_the_received_turn_whole`, `::test_terminal_compose_declares_the_loops_tools`,
  `::test_compose_without_tools_declares_none`, `::test_open_paper_no_longer_caps_size_itself`,
  `::test_read_file_malformed_offset_is_a_tool_error_not_a_crash`; `tests/test_monitor_tools.py` updated.

- 2026-09-01 — **Ledger field caps read from config at call time.** `_EVENT_ARG_CAP` (2,000) and
  `_EVENT_RESULT_CAP` (4,000) are replaced by `_event_arg_cap()` / `_event_result_cap()`, which
  read `env.ledger_caps()` per record; the values move to `orchestration.yaml` and default to
  100,000. Rationale and the measurement behind the number are in the root `DOCS.md` entry of the
  same date. Nothing about what an agent can *read* changes — `MonitorToolProvider` windows an
  event payload independently.

- 2026-09-01 (same day) — **The withheld-tool half of the unattended path now has a live probe**
  (`tests/test_live_no_hitl.py`, `AUTO_RESEARCH_LIVE_NO_HITL=1`). Its token-free half asserts what
  `SandboxToolProvider` and `ReadOnlyToolProvider` actually build from
  `resolve_handler(None, "none")`: headless, the worker, lead and reviewer lose **exactly**
  `ask_human` and `plan_and_debate` and nothing else (`off == on - {ask_human, plan_and_debate}`,
  with a wired handler as the control) — which is what makes an unattended arm comparable to an
  attended one rather than merely different. Its live half drives each role's **real** `_react`
  loop, with its real prompt, against a stimulus built to make it want a human, and records what
  it does instead. **LIVE-VERIFIED 2026-09-01**, and the recording had to change to make it
  meaningful: a role never ends in prose, so the probe now does what production does and
  composes each role's **real terminal schema** over the trajectory. That is also what makes
  the result machine-readable — `AnalystReport.status` is literally `ok | failed |
  needs_human`. Unattended, the worker returned **`needs_human`**, the lead
  **`should_author=false`**, the reviewer **`accepted=false`**; each detected the closed
  channel unaided and said so in its own output (the lead: *"NO HUMAN CHANNEL … I explicitly
  do NOT claim any human debate, steer, or sign-off"*), and none fabricated — the worker had
  the published PD in hand from a web search and labelled it *"literature context only, not a
  measurement made in this run."* Two limits recorded rather than papered over: the stimuli
  are prose rather than real payloads, so **decision quality is unmeasured** (every role said
  so — *"no worker report, no `tool_request`, no validation ObsID on disk"*), and the lead's
  provider carries `authoring_session=None`, so `should_author=false` is a **triage** verdict
  and no authoring was attempted or possible.
- 2026-09-02 — **The analyst lead became one ReAct loop that files forms.** Block 0's third launch
  died at $19.43 because the lead answered the authoring question with `should_author=true` and an
  empty `tool_specs` — schema-VALID output a Pydantic validator then killed the run over. The cause
  was the interface, not the model: the lead was **six separate conversations** (each re-seeded,
  each ending in a compose), the `decide_authoring` payload was 27,208 characters of single-line
  JSON in which the specification was 1,016 (3.7%), the schema marked the chain **optional** and
  carried **no field descriptions at all** (every explanation was a `#` comment, which never
  reaches JSON Schema), the one prose rule was stated only in the direction that did not bind
  (*"should_author=false when the worker mis-called an existing tool"* — the lead's case was the
  opposite), and the generic compose tail said *"do not describe intentions or plans"* while the
  requested output **is** a plan. Now: `run_cycle` is the lead's whole turn — one loop, one
  conversation, no output schema — ending when it **files a form** (`author_chain`,
  `hold_authoring`, `request_another_round`, `submit_adjudication`, `submit_terminal_report`).
  `adjudicate`/`judge_convergence`/`decide_authoring`/`finalize_authoring_outcome` are gone with
  their composes; `drive_authoring` **stays**, because `benchmarks/author_bench` measures exactly
  that call and its published K=10 batches priced it. `_react` gained `terminal_tools`, breaking
  **after** the batch is appended so no `tool_use` is orphaned, and *not* breaking on an
  `is_error` — which is what lets the stage check live in the handler. Four properties carry the
  design: **the impossible state is inexpressible** (`author_chain` requires `minItems: 1`, so
  "author with nothing to author" cannot be expressed — the [A12] move of removing a dependency on
  a free-filled field); **every question carries its own description**, read from the Pydantic
  `Field(description=...)` of the model it validates back into, so the worker's and the lead's view
  of one object cannot drift; **declarations are constant** (`_authoring_declared()` gates the drive
  tools on `cycle.authoring_enabled`, never on a live session, so swapping the session per chain
  link cannot shrink the declared set — the 2026-07-29 run-killer); and **caps stay in Python**
  (`rounds_left` is passed in and `request_another_round` is refused at zero with the cap named).
  The **coding span is compacted** at its own boundary (`_compact_coding_span`): the call that
  opened the session keeps its answer and that answer becomes the link's outcome, whole turns
  only — safe here because `ReadOnlyToolProvider` sets `defer_loading` on nothing, so no dropped
  turn can de-activate a discovered tool. A tool result is the harness's own to write, so this
  synthesizes nobody's words; the facts in it are read off the session in Python and only the
  narrative comes from a model. A cycle with no
  tool surface returns the failure rather than composing a stand-in ([F6]); a cycle that wrote a
  report but never filed it has that report used, because it is real evidence the lead produced.
  **The landmine was probed before the forms were written and was not there**
  (`tmp_scripts/probe_nested_form.py`, cents): all five vendors accept a tool whose `input_schema`
  nests an object array and fill ≥2 links with every required field — the 2026-08-24 Qwen
  `items: {$ref}` refusal applies to **output** schemas only — with no `"null"`-string quirk, and
  **all five classified `disposition` correctly unprompted**, which is direct evidence the failure
  was informational rather than a model limitation. **LIVE-VERIFIED 2026-09-02**
  (`tests/test_live_lead_cycle.py`, stimulus frozen out of that launch's own ledger): the Opus lead
  ran a real 9-call cycle with the real prompt through `ModelHandle`, its catalog figures (2683 px,
  above Anthropic's many-image dimension cap) rode the first request through the media guard, and it
  filed `request_another_round` with a dispatch text naming the tools that **already exist** — the
  launch-3 diagnosis, reached by the lead itself. Suite 1399 → 1663; new `tests/test_lead_forms.py`
  (41) and `tests/test_live_lead_cycle.py`.
- 2026-09-02 (second) — The nested-form measurement moved out of scratch and into the suite as
  `tests/test_live_form_shapes.py` (env-gated, one call per vendor). It sends the **real**
  `author_chain_tool_def()` to all five vendors that could ever hold a seat handed it, and
  validates what comes back through the **real** handler into real `ToolSpec`s — so the property
  the whole refactor turns on (a tool whose `input_schema` nests an object array is fillable
  everywhere, the position the 2026-08-24 sweep found Qwen refusing on *output* schemas) is
  re-runnable rather than a changelog note. `disposition` choice is recorded, never asserted:
  which one a model picks is quality. Two live tests were also corrected for the new topology —
  `test_live_no_hitl`'s lead terminal is `AnalystReport` (its turn ends on a filed form, not a
  composed `AuthoringDecision`), and `test_live_schema_matrix` keeps its `AuthoringDecision` row
  relabelled **shape-only**: no role composes it any more, but it is still the strictest shape
  this project sends and the row keeps the output-schema side from regressing silently.
- 2026-09-02 (third) — **The context drop is LIVE-VERIFIED, and so is the rest of the cycle.** Block
  0's fifth attempt ran the forms topology in production end to end. What the ledger shows, in
  order: the lead read `lib/` for 11 iterations, filed `author_chain` with an **empty** chain at
  iteration 12 — the exact state that killed the previous attempt at $19.43 — had it **rejected by
  the handler** with a message naming the fix, refiled a real 4-link chain one turn later, and
  opened a coding session. So the bad state is still *expressible* (no provider was enforcing
  `minItems`, because `strict` had never been set) but is now *recoverable in one turn* rather than
  fatal. That gap is what the same-day `strict: true` work closes.
  **Link 1 (`ixpe_pcube_polarimetry`) committed as `4424dd0`** after 4 gate passes, 4 reviews, 3
  rejections and 662 coder turns — `commit ⇔ gate ∧ review` held throughout, and the model never
  issued git. Every rejection was a real defect; **two were in a `reuse_products` cache the lead
  added itself mid-authoring**, which is a scope-creep finding recorded separately.
  **The drop, measured at the session boundary:** link 1's conversation ended at **27 tool results /
  ~142,739 tokens** per context edit; link 2's first edit came back at **12 results / ~78,682** —
  a 64k reduction, and *identical* to what link 1 itself opened at, i.e. the conversation returned
  to its pre-coding baseline instead of accumulating. Without it, link 4 would have opened carrying
  four coder transcripts; the pre-refactor lead cleared ~86k on **every** turn with no boundary to
  cut at. Also observed working: the budget split (`react_loop_cap` per cycle stage, the absolute
  cap per link, `submit_for_review` exempt via `_CYCLE_FREE_TOOLS` — recorded as `free=True` with
  the budget not advancing) and the pre-gate submission snapshot firing on every submit.

- 2026-09-12 — **A1–A11: the lead's turn became one loop with no stages, and its report became its
  decision.** Deleted: `hold_authoring`, `request_another_round`, `submit_adjudication`,
  `submit_terminal_report` and their handlers; the three stage constants, `_STAGE_FORMS`,
  `_out_of_stage`, `LEAD_CYCLE_TERMINALS`, `LeadCycle.stage`/`.filed`, `_stage_note`,
  `_author_chain` and `_drive_one_link`. `LEAD_FORM_TOOLS` is now `{author_chain}` alone, and a
  brief no longer ends anything. **Why:** a filed form was a `terminal_tool`, and while authoring
  the only two the lead could file were both whole-cycle terminals — so "this link is dead, try
  the next" was inexpressible. Slot 1 of the 2026-09-11 batch ended a whole analysis with 11 of 12
  author credits unspent; slot 2, from the identical position, paid a debate round and a full
  worker rerun for the same thing. Two of the four retired handlers validated nothing anyway.
  **Now:** `run_cycle` is one `_react` with `terminal_tools={turn_end}`; `_ChainDriver.advance`
  (wired to `LeadCycle.advance_chain`) closes a finished link and opens the next **on a tool
  result**, so the chain advances inside the turn; `execute_submit`'s cap refusal became the
  CI-budget ticket that triggers it; `lead_turn_problems` validates the report at `turn_end` (no
  report · rework without probe/rationale · rework with no rounds left, cap named · a conclusion
  with no findings · **a cited figure whose file is not on disk** · a coder still running).
  `_react` gained `share_messages`, which is what lets the driver drop a committed link's coding
  transcript from the live trajectory instead of running a nested per-link loop.
  **One report class, two surfaces:** `AnalystReport` gained `rework`/`rationale`/`what_to_probe`
  and `status` regained `needs_human`; `ReportWorkspace` gained `status_values`, and
  `edit_report_tool_def(prose_fields, status_values)` filters what each role is offered — the
  worker's surface omits the decision fields and `needs_human`, the lead's omits `tool_request`.
  The schema stays permissive and `turn_end` is strict, deliberately: conditional required-fields
  is not portable across the six vendors, and a schema refusing a legal partial write fights the
  write-freely contract. The subclass alternative was rejected because it widens `status` in a
  subtype. **Reviewer:** `review`'s loop gained `terminal_tools={turn_end}` — without one,
  `_continuation_for` returns `None` immediately, so an empty or truncated reviewer turn read as
  "finished investigating" (17 of the 184 silent turns measured across 221 runs are this loop).
  **A2:** one injection drain at the top of the iteration, **held** for a turn when the preceding
  provider turn has an unresolved server-tool call (`_last_turn_has_unresolved_server_tool`); the
  mid-batch drain is deleted; and the silent-turn nudge appends **nothing** on a paused turn that
  is mid-server-tool — the vendor documents re-sending as-is, and a message appended there strands
  the call. Measured dead first: 184 silent turns across 221 runs, `stop == "end_turn"` on every
  one, zero `paused` ever. Suite: `test_lead_forms.py` largely rewritten (29), new A2 cells in
  `test_run_control.py` (17), `test_react_tools.py` 46 → 47.
  **Process note:** the first attempt at the `run_cycle` replacement matched `self._round += 1` by
  content and deleted `class AnalystLeadAgent` along with it — the 2026-09-09 incident verbatim, a
  structural heuristic standing in for a boundary check. Restored from a scratch copy and redone
  with the span asserted to contain exactly three method definitions and no `class` statement
  before any line was removed.

- 2026-09-12 (second) — **B3-B6: the second screen.** `drive_coder` hands the instruction over and
  returns at once (falling back to the blocking call for a session without `start_drive`, which is
  `drive_authoring`'s path and every test double's); new `check_coder` reports `working` /
  narration / a typed failure, consuming a finished result exactly once so it is reported once.
  **B4 — a stopped coder reaches the lead as its own user turn**, through **A2's queue and A2's
  hold rule**. That is the dependency the build order records: a pushed event is precisely what
  makes the injection race reachable, so it goes through the same gate rather than a second,
  unguarded path. The notice names the link and points at `check_coder`; the narration itself
  stays out of the injected turn, because a notice is a nudge to look, not the looking.
  **B6 — `_drop_coding_middle` fires on a COMMITTED link only.** A rejection or a spent CI budget
  is what the lead's next brief has to be written against, so dropping it would delete the reason
  the link died. Before this the drop ran for committed and failed links alike.
  *(Superseded 2026-09-13: both arms compact, and the summary carries the reason — see that
  day's changelog entry.)*

- 2026-09-12 (third) — **`tests/test_live_form_shapes.py` is now `tests/test_live_lead_providers.py`**
  and covers three cells per vendor rather than one. The name went with the forms: it began as a
  single-call check that each vendor could fill `author_chain`'s nested argument, and the question
  worth asking per vendor grew with the refactor. Cell 1 is that original schema check (fixed for
  `cycle.brief`); cell 2 drives a **whole real `run_cycle`** per vendor — seeded worker reports
  with a `tool_request` and figures, the real read-only tool loop, a briefed chain with stubbed
  coding sessions, ending on a validated `turn_end`, and re-checking the conditional requirements
  from OUTSIDE the validator (the only way to catch a validator that passed wrongly); cell 3 hands
  each vendor back its **own** history and asks for one more turn, the class that produced three
  run-killers in one week — with the history driven live rather than hand-built, because an
  invented `provider_parts` array tests my idea of the vendor's shape rather than the vendor's.
  **Tool search is not applicable to the lead and the file says so**: `ReadOnlyToolProvider` defers
  nothing, so the provider-side search step never runs for this role. Measured the expensive way —
  the injection-race probe was written against the lead first and recorded six turns with no
  server-tool block at all. The role that defers is the worker.

- 2026-09-12 (second) — **The lead keeps its turn when it briefs a chain, and `check_coder`
  reaches a handler.** Two defects from the A-series refactor, both found by reading one live
  trace rather than by a test. **(1)** `check_coder` was declared, defined, given an executor and
  given a dispatch branch — and its name was missing from the outer membership test guarding the
  authoring block, so that branch was unreachable and the call returned `unknown tool:
  check_coder`. Measured live: the lead filed `author_chain`, called `check_coder` to poll its
  coder, and was told the tool does not exist. Guarded now over the *declared* set
  (`test_every_declared_authoring_tool_is_actually_ROUTED`), so a tool added to the lead's surface
  without wiring fails a test rather than a model call. **(2)** `_ChainDriver._open` sent the brief
  with the **blocking** `drive`, holding the lead still for the coder's entire first turn — 263 s
  measured — against a contract (§9.3) that says the turn continues. Two consequences observed in
  the same run: no background task existed, so `check_coder` (once routable) would answer *"you
  have not sent the coder anything yet"* to a lead whose coder had just worked for four minutes,
  advice that would have had it re-send the whole brief; and the first turn's narration was
  discarded here, so the lead re-read five files by hand to learn what its own coder had already
  told it. `_open` now uses `start_drive` when the session has it, keeping the blocking call for a
  session that predates the seam (a benchmark's, a test double's) exactly as `execute_drive_coder`
  does. The link-opened message stops claiming a first attempt exists and points at `check_coder`.
  **`execute_submit` gained the guard that blocking call had been providing by accident**: the
  gate reads the working tree, so a submit while the coder is mid-write would run pytest twice and
  a paid co-lead review over a half-written tool and reject it for a defect nobody wrote. That race
  was unreachable while the lead could not act during the coder's turn; the guard lands with the
  change that makes it reachable. Budget-free, since a refusal is not a submission.
  Human ruling on the blocking itself: not alarming in isolation — the lead has little to do —
  *"except it wanted to do some read-only 'good measure'"*, which is precisely the pass both
  defects were blocking. `tests/test_lead_forms.py` 32 -> 36, the three new behavioural cells each
  verified against the unfixed code.

- 2026-09-12 (third) — **`clarify_for_author` is gone; the coder's question enters the lead's own
  loop.** It opened a second ReAct conversation concurrently with the lead's turn, on the shared
  provider, with the full coder-control surface — measured live spending 85 `check_coder` calls
  polling the coder that was blocked waiting for it, and answering 326 s late into a conversation
  the coder had abandoned. Replaced by `_coder_questions(tools)`, a pull at the same boundary as
  `_coder_event` under the same A2 hold rule, injecting a `[coder asks]` user turn; the lead
  answers with `drive_coder`, which now steers a turn already in flight. `_ClarifyReply` deleted
  with it. See `sandbox/DOCS.md` for the channel as a whole.

- 2026-09-13 — **The coding span is compacted into its opening call's answer, and the test seam
  moved under the session.** A live run died at $13.26, after committing a tool, on
  `400 No tool output found for function call call_AACT…`. `_ChainDriver._open` records the cut
  position while the opening tool's handler is still running, so it named the index that tool's
  *answer* was about to take; the old `_drop_coding_middle` cut **from** that index, keeping the
  `author_chain` call and discarding its result. Every committed link stranded one call — the
  first link's `author_chain`, thereafter the previous `submit_for_review` — and the run died on
  the lead's next model call. Not OpenAI-specific: Anthropic rejects an unpaired `tool_use` too;
  OpenAI was simply the seat the contrast arm's lead held. Introduced by the 2026-09-12 refactor,
  which deleted the harness-voiced *"Coding session N of M is open"* user turn (the boundary the
  mark used to be taken after) and folded its text into the tool result.
  **The fix is the shape the human chose, and it subsumes the defect rather than patching it.**
  `_compact_coding_span` rewrites the answer at the mark instead of cutting from it: the opening
  call keeps an answer, and that answer becomes the link's outcome. Only parts belonging to a
  chain-driving tool (`author_chain`/`submit_for_review`/`respond_to_review`) are rewritten, so a
  turn that batched the brief with a file read keeps the read's own result; the trailing turn
  whose result the loop has not appended yet survives the cut, because dropping it would strand
  that result one message later — the mirror of the same defect.
  New `compactor` role (`gpt_5_6_luna`, `prompts/compactor.md`, in `REQUIRED_ROLES`) writes the
  narrative; the **facts are written in Python** — status, commit sha, submissions spent, credits
  left — because that text is the lead's only view of the link and its next brief is written
  against it, so a thin or wrong summary must not be able to put a false commit in front of it.
  A compactor failure degrades to the facts alone (`analyst.tool.compactor_failed`) rather than
  leaving the call unanswered. **Both arms compact now**, reversing B6: the failure wording ends
  by handing the lead the three-way choice (proceed without the tool, re-investigate and re-plan,
  or retry with caution), which is how a rejection stays actionable without its transcript; the
  raw span stays in the ledger (`analyst.tool.compacted`, with `turns_dropped`).
  **Why no test caught it, which is the reusable half.** `_coding_conversation()` hand-built a
  `mark` production never produces — it placed the opening call's answer *before* the mark where
  production places it *at* the mark — so `test_the_drop_leaves_no_tool_use_without_its_result`
  passed against a retired shape. Worse, `_driver_with` replaced `_ChainDriver._open` outright,
  and the fake never assigned `driver.mark`: it stayed `0` from `__init__`, so the buggy line was
  executed by **no test at all**, and the fake `_close` re-implemented the cut it was meant to be
  testing. The seam was one layer too high. `_build_session` is now the only method a double
  replaces — building, connecting and driving a real coder is the only expensive part — so
  `_open`, `advance`, the mark and the compaction all run as production runs them, and the
  fixture is whatever the driver produced. New `_unanswered_call_ids` states the pairing rule as
  a query so a conversation edit is checked where it happens rather than discovered as a 400.
  Both new tests were mutation-verified against the shipped defect: restoring the old cut fails
  with orphaned ids, and moving the mark one message fails four cells. `tests/chain_stub.py`
  rewritten around a deliberately narrow `StubSession` — which immediately earned itself by
  naming three session attributes a `Mock` would have auto-answered.
  **Live-test coverage closed on the axis that mattered:** `test_lead_cycle_authors_a_chain_end_to_end`
  is the only test that drives a real chain and it seated only the coder, so its lead was always
  the configured Anthropic one while the defect lived on the Responses dialect. New
  `AUTO_RESEARCH_LEAD_SEAT=provider:model` seats the lead through the same configuration pointer,
  and the run records the lead it resolved out of the written file, as it already did the coder.
  `tests/test_config_paths.py` now derives its role list from `REQUIRED_ROLES` instead of writing
  it out — a hand-written list made a valid config fail the check that exists to catch an invalid
  one, the same shape as the 2026-09-09 `_VALID_PROVIDERS` literal.
  Suite 1499 → 1500 passed, 209 skipped, 0 failed.

