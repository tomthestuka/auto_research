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
`tools.tool_definitions()`; if it emits tool calls, execute each via
`tools.execute(name, args)`, append the assistant `tool_use` turn + the
`tool_result` turn, and re-call so the model sees its own results in-context and
chains dependent calls; stop when it emits no calls or `react_loop_cap`
(`orchestration.yaml`, default 12) is hit. A terminal `_call_over_messages(...)`
then produces the role's structured output grounded in the trajectory (both reuse
`_structured_loop`, the shared off-schema-retry core). Tool surfaces live behind
the `ToolProvider` protocol in `react.py`: `SandboxToolProvider` (analyst science
tools in the `PersistentSandbox`), `ReadOnlyToolProvider` (reviewer/lead/co-lead — in-process
`read_file`/`list_dir` confined to its `roots` (the permitted roots are named in each tool's
description and echoed in the `path not permitted` error so the model self-corrects instead of
guessing), an optional knowledge-base `text_search`
(`include_text_search`, default on; the §9.3 co-lead omits it and points `roots` at the
toolkit worktree instead), plus an optional `web_search` tool when a `web_source` is supplied
— the analyst lead uses it to consult authoritative docs while triaging tool requests, the
co-lead to check conventions while reviewing, §9.3),
`SourceToolProvider` (librarian — arXiv/ADS/web + local `text_search`). A role with
`tools=None` falls back to a plain single structured call.

**Raise-to-human (`ask_human`, §9.7/§9.8).** When a `human_handler` is wired, both
`SandboxToolProvider` and `ReadOnlyToolProvider` expose one shared `ask_human` tool
(`ask_human_tool_def`/`execute_ask_human` in `react.py`): the role builds a `HumanQuery` and
**blocks** on the injected handler (via `hitl.resolve_human`) until the human replies — the
same model the SDK author uses. With no handler the tool is not offered, and a direct call
raises `HumanInputRequired` (the headless fail-safe). `run_research` threads the resolved
handler to the workers (`SandboxToolProvider`) and to the reviewer/lead (`ReadOnlyToolProvider`,
`asked_by` = the role). Each provider also carries the backing `model` id (resolved via
`_model_label`), set on the `HumanQuery` so connector message bodies identify the sender as
`"<role> (<model>)"`.

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
| Analyst worker | `SandboxToolProvider` over `research_toolkit` manifest tools plus MCP tools in a persistent sandbox. | Runs the full analysis tool chain inside `analyze()`, then reports grounded results and figures. |
| Analyst lead | `ReadOnlyToolProvider` over run artifacts, papers, and `research_toolkit`; optional `web_search`. | Judges convergence, adjudicates, triages `ToolSpec`, and answers author questions. |
| Analyst co-lead | `ReadOnlyToolProvider` over the **toolkit worktree** (`include_text_search=False`) + optional `web_search`. | Provider-decorrelated **code review** of one authored tool: brief-match, Layer-B check adequacy, no-fabrication second pass, subtle bugs (§9.3 step 4). |
| Reviewer | `ReadOnlyToolProvider` over run artifacts, figures, papers, memory, and `research_toolkit` (to read tool source/`TOOL_MANIFEST` and confirm correct call parameters). | Dispatches, reviews, requests capped rework, and composes final report handles. |

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
- `ReviewerAgent.review(request, *, literature=(), report=None) ->
  ReviewVerdict`. `literature` is `Sequence[Path]` — local PDF paths loaded as
  `DocumentRef` attachments. When a `ReadOnlyToolProvider` is attached (`tools`),
  the reviewer first investigates run artifacts/figures/memory via a ReAct loop,
  then issues the verdict grounded in that investigation plus the papers; with no
  `tools` it makes a single structured call.
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
  For results without a PDF: stores metadata only with `result.abstract` as
  `mem_abbrev` (no LLM call). **Each paper's ingest is isolated**: any failure
  (a document the model API rejects, a provider fault, an oversized PDF) is caught,
  logged as `librarian_io.ingest_document.error`, and degraded to metadata-only
  ingest — one bad paper can never propagate out of the batch and abort the
  `literature` node (the shared `_store_metadata_only` helper is the fallback).
- `LibrarianIOAgent.evaluate_search_results(request, results) -> ReviewVerdict`
  — Phase 1 stub returning `accepted=True`. See CLAUDE.md §12.
- `AnalystWorkerAgent(*, sandbox=None, registry=None, memory_path=None, ...)`.
  `AnalystWorkerAgent.analyze(task, *, peer_reports=()) -> AnalystReport` runs the
  inner ReAct loop over a `SandboxToolProvider` (when `sandbox`+`registry` are
  attached) — chaining toolkit/MCP calls to completion — then composes the report
  grounded in the tool results, with the loop's figures attached. `last_tool_results`
  exposes the loop's `ToolResult`s for the consensus peer summary. With no
  sandbox/registry it falls back to a plain structured call. (`request_tools`/
  `execute_tools` were removed; the loop subsumes them.)
- `AnalystLeadAgent.adjudicate(task, reports) -> AnalystReport` — uses a
  `ReadOnlyToolProvider` loop to inspect run artifacts/figures/memory before
  synthesizing when `tools` is attached; plain structured call otherwise.
- `AnalystLeadAgent.judge_convergence(task, reports, *, round_index,
  max_rounds) -> ReviewVerdict` judges whether worker reports have converged
  enough for final synthesis.
- `AnalystLeadAgent.clarify_for_author(question, *, kind, context) -> str` — answers a
  tool author's `ask_lead` clarification (the lead↔author channel, §9.3); consults
  `web_search` (when a `ReadOnlyToolProvider` with a `web_source` is attached) before
  answering rather than guessing. Used by `merge_flow.propose_and_pr`.
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
- `AnalystLeadAgent` — analyst lead wrapper.
  Methods: `adjudicate()`, `judge_convergence()`, `clarify_for_author()`.
- `AnalystColeadAgent` — Phase-2 provider-decorrelated co-lead reviewer wrapper.
  Method: `review_tool(tool_spec, *, diff, gate, tools=None) -> ToolReviewVerdict`.

## Changelog
- 2026-05-26 -> 2026-06-12 — Agent wrappers built and ReAct-wired: reviewer,
  librarian I/O, analyst worker, analyst lead, native structured output/function calling,
  bounded schema retries, `SandboxToolProvider`/`ReadOnlyToolProvider`/`SourceToolProvider`,
  report composition, monitor hooks, librarian cap split, reviewer LaTeX token fix, figure
  redirection, and `coder` prompt loading.
- 2026-06-12 -> 2026-06-15 — Phase 2 agent support landed (steps 4-5). Lead read-only toolkit
  inspection + optional `web_search`, `clarify_for_author()` for `ask_lead`, and
  `AnalystWorkerAgent.analyze()` as the sole owner of tool execution (stale
  `request_tools`/`execute_tools` removed). Added `AnalystColeadAgent.review_tool()`
  (provider-decorrelated code review → `ToolReviewVerdict`) + the `analyst_colead` prompt role,
  with `ReadOnlyToolProvider.include_text_search` so the co-lead reviews the toolkit worktree
  without the papers KB. Both tool providers gained a shared `ask_human` tool + `human_handler`.
  Monitor hooks attach configured model strings and emit per-tool ReAct result events.
- 2026-06-16 -> 2026-06-18 — Phase 2 step 7 + live-run polish. `AnalystLeadAgent` gained
  `decide_authoring(reports) -> AuthoringDecision` (read-only `toolkit_root` browse sets
  disposition and returns one finalised `ToolSpec` or declines; consensus, not the LLM, then
  issues `merge_flow.propose_and_pr`, §9.9). Added a shared read-only `data_catalog` tool
  (reading `observations.read_catalog`) on `SandboxToolProvider` (worker) and on
  `ReadOnlyToolProvider` when `include_data_catalog=True` (lead, not reviewer), so a role checks
  local data before requesting an acquire tool. `HumanQuery` carries a `model` id (sender shown
  as `"<role> (<model>)"`). `ReadOnlyToolProvider.read_file`/`list_dir` self-describe permitted
  `roots` in descriptions + errors (the reviewer's `roots` now include `research_toolkit`), so a
  role recovers in one step instead of thrashing — the failure mode that stalled a reviewer
  triage loop. Literature/web provider faults in `SourceToolProvider`/`ReadOnlyToolProvider`
  now degrade to recoverable `is_error` outcomes instead of aborting the run. Every `react.py`
  harness tool gained manifest-quality tool-level + per-argument descriptions (notably
  `ask_human` `question`/`context`/`kind`), so the model reads each tool's contract from the
  tool, not the prompt.
- 2026-06-22 — `LibrarianIOAgent.ingest_results` now **isolates each paper's ingest**: the
  per-paper body is wrapped so any failure (a document the model API rejects, a provider fault,
  an oversized PDF) is caught, logged as `librarian_io.ingest_document.error`, and degraded to
  metadata-only ingest via the new shared `_store_metadata_only` helper — a single bad paper can
  no longer propagate out of the `asyncio.gather` and abort the `literature` node. A live run had
  crashed when an ADS `esource` link resolved to an HTML page saved as `.pdf` and the Anthropic
  document API rejected it (`400 "The PDF specified was not valid."`); the download-side
  `%PDF-` validation (see `sources/DOCS.md`) keeps such pages out, and this isolation is the
  defense-in-depth so no future bad document can kill the whole run.
- 2026-06-22 — `_react` `*.analyze.tool_result` events now record the tool-call **`args`** and the
  returned **`text`** (capped at `_EVENT_ARG_CAP`/`_EVENT_RESULT_CAP` via `_cap_text`), for every
  ReAct role (worker/lead/reviewer/librarian) — so a run is diagnosable from the JSONL alone rather
  than only via per-worker `memory.md`/LangSmith. Heavy text lands in the JSONL only (the terminal
  renderer ignores these keys). Part of the fix for the diagnostic-logging gap (see `monitor/DOCS.md`).
