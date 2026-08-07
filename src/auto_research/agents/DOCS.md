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
the "last-round-only figures" leak. Round files are `<role>_<model>_round<n>_report.jsonl`
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
| Analyst lead | `ReadOnlyToolProvider` over run artifacts, papers, and `research_toolkit`; `web_search` + `web_fetch`. | Judges convergence, adjudicates, triages `ToolSpec`, and answers author questions. |
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
- `AnalystLeadAgent.adjudicate(task, reports, *, sources=None) -> AnalystReport` — accumulates
  each worker's cited figures into a per-worker catalog source (keyed by `sources[i]`, append-only
  across rounds/iterations), then **writes the adjudicated report via `edit_report`** citing
  figures by `{worker, index}` when a `ReadOnlyToolProvider` is attached (the workspace is wired
  onto it only for this call, cleared in `finally`); plain structured call with catalog figures
  attached otherwise. The lead — not the consensus — now owns the report's figures (V-4).
- `AnalystLeadAgent.judge_convergence(task, reports, *, round_index,
  max_rounds) -> ReviewVerdict` judges whether worker reports have converged
  enough for final synthesis.
- `AnalystLeadAgent.clarify_for_author(message, *, kind) -> str` — answers a
  tool author's `ask_lead` clarification (the lead↔author channel, §9.3), sent as a single
  Markdown `message`; consults
  `web_search` (when a `ReadOnlyToolProvider` with a `web_source` is attached) before
  answering rather than guessing. Used by `merge_flow.propose_and_pr`.
- `AnalystLeadAgent.finalize_authoring_outcome(task, reports, tool_spec, result) ->
  AnalystReport` — composes the terminal report after a **non-committed** authoring attempt
  (§9.6): documents the unavailable capability + partial findings and sets `status`
  (`needs_human`/`failed`/`ok`); never fabricates, never re-requests the tool. Called by
  `DebateToConverge` instead of re-authoring; the report flows to the reviewer (sole I/O).
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
  iterations). Methods: `adjudicate(task, reports, *, sources=None)` (V-4: accumulate per-worker
  catalog, write via `edit_report`), `judge_convergence()`, `clarify_for_author()`,
  `finalize_authoring_outcome()`.
- `AnalystColeadAgent` — Phase-2 provider-decorrelated co-lead reviewer wrapper.
  Method: `review_tool(tool_spec, *, diff, gate, tools=None) -> ToolReviewVerdict`.

## Changelog
Compacted 2026-08-05: entries through 2026-07-28 folded into themed ranges (detail in git history).

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
  ledger.** Mid-batch control landed first (a `/stop` skipping remaining calls with honest results, a
  steer delivered right after the batch), together with `_record` actoring events by the **indexed
  `agent_key`** (`analyst_worker_1`) — two workers had been indistinguishable in the log, so neither a
  reader nor a human choosing a `/steer` target could tell them apart. Two days later `_react` stopped
  reading the stop flag altogether: a `/stop` now cancels the run task, so the loop has nothing to
  notice, and the top-of-iteration break, the mid-batch skip and their events were deleted. **Steering is
  untouched** — a steer is a message and a message can wait. The lead publishes its live
  `AuthoringSession` on `RunControl` while `drive_authoring` runs, so an interrupt can reset the toolkit
  branch (that method's own `finally` sits inside the cancelled scope, where its awaits die first);
  whichever path gets there first clears the handle, and `aclose()` is idempotent. **`MonitorToolProvider`
  + `StatusMonitorAgent`:** a fourth provider exposing three tools over the run's own `monitor.jsonl` —
  `brief_history` (the index, filterable by level/sequence/prefix/actor/team/substring),
  `ledger_event(sequence, max_chars, bias)` (one event **with its payload**, windowed, negative `bias`
  for the tail where a long dump keeps its errors) and `ledger_grep` (find by payload content, returning
  index rows only). Three properties are structural, not prompted: **read-only** (no tool takes a path),
  **bounded** (400 rows, 32k payload chars, clamped in code — an agent reading its own run log is the one
  place a harness can feed itself without limit, cf. openai/codex#27131) and **fresh** (re-read per call).
  Driven under `status_react_loop_cap` (default 6 — a human is waiting on a phone), asking once more with
  no tools rather than returning silence on cap. `prompts/status_monitor.md` teaches the level convention
  (1 = most important … 10 = chattiest, so "important only" is a *low* `max_lvl`). Tests:
  `tests/test_run_control.py`, `tests/test_monitor_tools.py`.
- 2026-08-05 — **`_react` replays the turn it received.** The loop used to build an
  `AssistantToolUse` by copying `content`/`tool_calls`/`provider_blocks` off the response one field
  at a time; `ModelResponse` is now the neutral assistant turn itself, so the loop appends it whole
  (`messages.append(response)`) and `AssistantToolUse` is gone. This deletes a class of bug rather
  than an instance of it: that copy silently dropped tool-search discovery blocks until 2026-07-27
  and Anthropic `thinking` blocks until today, and it would have dropped the next part type too.
  Adapters now own their provider's replay rule, each grounded in that provider's documentation
  (see `models/DOCS.md`). Behaviour of the loop is otherwise unchanged — it still ends when the
  model emits no tool call. Test: `tests/test_react_tools.py::test_react_replays_the_received_turn_whole`.
- 2026-08-06 — **The terminal compose declares the loop's tools.** `_call_over_messages` and
  `_structured_loop` take the same `ToolProvider` `_react` just used and pass
  `tools=…, tool_choice="none"`: the compose replays a trajectory whose provider items reference
  those tools, and a request that omits the declarations orphans them — the 400 that killed run
  `8c75990e` on the co-lead's `review_tool` (see `models/DOCS.md`). `"none"` rather than `"auto"`
  because the compose must answer; a model that calls again returns `parsed=None` and burns a
  `structured_retry_cap` attempt. All seven compose sites pass their provider (reviewer `review`,
  worker `analyze`, lead `adjudicate`/`clarify_for_author`/`decide_authoring`/
  `finalize_authoring_outcome`, co-lead `review_tool`), and so does `StatusMonitorAgent`'s
  cap-exhaustion fallback, which had the identical tool-less shape on the GPT reporter — a
  plausible cause of the open *"`/status` returns silence on cap exhaustion"* defect. Payload-only
  `_call` is unchanged (no trajectory, so nothing to declare). Tests:
  `tests/test_react_tools.py::test_terminal_compose_declares_the_loops_tools`,
  `::test_compose_without_tools_declares_none`, `tests/test_monitor_tools.py` updated.
- 2026-08-06 — **`open_paper` no longer caps PDF size.** `_OPEN_PAPER_MAX_MB` (8 MB) is deleted
  along with its stat-check and the sentence advertising it. It was the wrong layer: it could see
  neither the provider in play nor the rest of the request, so it refused papers every provider
  would have accepted while missing the limits that actually fail a call (page count, image count,
  total payload). Those now live per adapter in `models/providers/_media.py`. The distinct-paper cap
  (`_OPEN_PAPER_CAP`=6) stays — that one is about attention, not provider limits. Test:
  `tests/test_react_tools.py::test_open_paper_no_longer_caps_size_itself`.
