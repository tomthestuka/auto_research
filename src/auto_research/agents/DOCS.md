# `auto_research.agents` — per-role wrappers

## Purpose
Thin wrappers around the provider layer that bind a role to its prompt
(`prompts/<role>.md`), its allowed tools, and its expected I/O schemas. One class
per role — reviewer, librarian I/O, analyst worker, analyst lead
([CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop)).
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
tools in the `PersistentSandbox`), `ReadOnlyToolProvider` (reviewer/lead — in-process
`read_file`/`list_dir`/`text_search`, confined to the run + papers dirs),
`SourceToolProvider` (librarian — arXiv/ADS/web + local `text_search`). A role with
`tools=None` falls back to a plain single structured call.

When a `RunMonitor` is injected, role wrappers record high-chatter input,
parsed output, raw model content, attachment counts, and errors. These records
are for human inspection only and are never fed back into runtime prompts.

Critical: **runtime LLMs never see `CLAUDE.md` or any `DOCS.md`**
([CLAUDE.md §4](../../../CLAUDE.md#4-claudemd-and-docsmd-are-build-time-only)).
Only the file in `prompts/` plus task data goes into the model.

Step 8 adds thin role wrappers and validates per-role plumbing: prompt loading,
config-driven model resolution, structured Pydantic input, structured Pydantic
output, and provider-layer trace/cost logging. It does **not** mean each role can
complete its full real-world job yet.

At step 8, role capability splits by whether that role's tools already exist:

| Role | Tools available at step 8? | What "one task" means |
|---|---|---|
| Librarian I/O | Yes — step 5 built arXiv / ADS / Tavily, and document attachments are wired through the model boundary. | Real source/document input can produce structured search results or memory abstractions. |
| Librarian searcher | Yes — step 6 built `text_search()` over SQLite. | Real query returns structured records from the DB. |
| Analyst worker | No — `research_toolkit` sandbox is not wired until step 11. | LLM-only reasoning or structured tool-request capture; no shell/tool execution yet. |
| Analyst lead | N/A — pure adjudication. | Given worker reports or tool requests, produce structured adjudication/review. |
| Reviewer | N/A — pure dispatch + review. | Given a request or stub results, produce a structured dispatch/review decision. |

## Interfaces / Contracts
Prompt loading:
- `load_prompt(role, root=None) -> str` loads `prompts/<role>.md`, strips leading
  and trailing whitespace, and raises `ValueError` if the prompt text references
  build-time governance filenames.
- `prompts_dir(root=None) -> Path` returns the prompt directory, allowing tests to
  inject a temporary prompt root.
- `PROMPT_FILENAMES` maps the four runtime LLM roles to their Markdown filenames.

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
  and stores each `SearchResult`. For PDF-bearing results: downloads via
  `download_and_ingest_result()`, runs `IODebateToConverge` (2 worker passes +
  lead), writes `mem_abbrev` to storage. For results without a PDF: stores
  metadata only with `result.abstract` as `mem_abbrev` (no LLM call).
- `LibrarianIOAgent.evaluate_search_results(request, results) -> ReviewVerdict`
  — Phase 1 stub returning `accepted=True`. See CLAUDE.md §13.
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
- All wrappers accept an injected `model`; otherwise they resolve their role
  from `config/models.yaml` and use the first configured group member unless
  `model_name` is supplied.
- All wrappers accept an optional `monitor: RunMonitor | None`. At
  `chatter=10`, each model-backed public method records its structured input and
  parsed output. Errors are recorded at low chatter before being re-raised.
- Live role verification is opt-in:
  `AUTO_RESEARCH_LIVE_AGENT_TESTS=1 pytest tests/test_live_agents.py -s`.
  It runs one real model call for each role and writes JSONL plus Markdown logs
  under `logs/dev/step-8/`.

## Dependencies
- Internal: `auto_research.models`, `auto_research.schemas`,
  `auto_research.sources`, `auto_research.retrieval`,
  `auto_research.observability`.

## Dictionary-level reference
- `PromptRole` — literal type for the four Phase 1 runtime roles:
  `reviewer`, `librarian_io`, `analyst_worker`, `analyst_lead`.
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
- `AnalystWorkerAgent(*, available_toolkits, sandbox=None, memory_path=None, worker_index=0, ...)` —
  analyst worker wrapper.
  - `analyze(task, *, peer_reports=())` → `AnalystReport`. Reads `memory_path`
    and injects its content into the payload before each LLM call.
  - `request_tools(task, registry)` → `list[ToolCallRequest]` — native
    function-calling elicitation (per-tool `args_schema` constrains arguments); no execution.
  - `execute_tools(task, registry, *, tool_retry_cap=3)` → `list[ToolResult]` —
    calls `request_tools(task, registry)`, runs each via `sandbox.call_tool()` /
    `sandbox.call_mcp_tool()`, normalizes the raw result via
    `_normalize_tool_raw()`, and appends a truncated terminal recording to
    `memory_path`. Returns `[]` if `sandbox` is `None`; if an attached sandbox
    is not alive it starts a replacement before requesting tools. Per attempt it
    (a) merges the legacy `ToolCallRequest.command` field into the args if present
    (back-compat — function calling normally delivers `command` as an argument),
    (b) recomputes routing from `current_req.kind` so a retry that changes tool
    routes correctly, (c) for toolkit tools, auto-fills `<mission>_path` args
    declared in the tool `args_schema` from `config/observations.yaml` (via
    `_observation_path_args()`) when the model omitted them, and (d) calls
    `_normalize_tool_raw()` so structured payloads without a `stdout` key (e.g.
    `list_dir` → `{entries}`, `load_data` → indices dict) are serialized into
    `stdout`, with any captured `console_tail` folded in. On tool-execution
    failure it re-issues `request_tools` with the error fed back, up to
    `tool_retry_cap`.
  - `is_alive` → `bool` — `True` when no sandbox attached; delegates to
    `sandbox.is_alive` otherwise.
- `AnalystLeadAgent` — analyst lead wrapper.
  Methods: `adjudicate()`, `judge_convergence()`.

## Changelog
- 2026-05-26/27 — Steps 7–8 built: prompt loading with governance-reference guard; async role wrappers for reviewer, librarian I/O, analyst worker, and analyst lead. `LibrarianSearcherAgent` replaced by pure-Python `text_search_paths()`. `LibrarianIOAgent` gains full literature pipeline: `compose_search_strategy()`, `search()`, `ingest_results()` (download + `IODebateToConverge` + storage), `evaluate_search_results()` Phase 1 stub. `ReviewerAgent.review()` loads PDFs as `DocumentRef` attachments. Opt-in live role test under `AUTO_RESEARCH_LIVE_AGENT_TESTS=1`.
- 2026-05-28 — Step 11–12: `AnalystWorkerAgent` gains `sandbox`, `memory_path`, `worker_index`, `is_alive`, and `execute_tools()`. `analyze()` reads `memory_path` and injects private memory. All role wrappers accept optional `RunMonitor` for high-chatter event recording. `execute_tools()`: `command` field merged into args; routing recomputed per retry attempt; instrument paths auto-filled from `observations.yaml`; `_normalize_tool_raw()` serializes non-protocol keys into `stdout` and folds `console_tail`; `_append_tool_results_to_memory()` caps stdout/stderr at 8 KB/2 KB.
- 2026-06-03 — `ReviewerAgent` gains `compose_machine_report()` (structured `ResearchReport`; overwrites figures/references/created_at deterministically after LLM prose) and `compose_latex_report()` (returns raw LaTeX string from `LatexReport` schema; accepts `feedback` errors for retry self-correction; passes `images=report.figures`).
- 2026-06-09 — `_call()` gains `overrides: dict | None` parameter; `_parse_json_response` merges overrides into the extracted dict before Pydantic validation. `compose_machine_report` uses this to inject `run_id`, `request_question`, `verdict_rationale`, and `created_at` before the schema is validated, fixing crashes where the model omits runtime-owned fields (`run_id` has no default; `request_question`/`verdict_rationale`/`created_at` fail `NonEmptyStr`/`datetime` constraints when left as placeholders).
- 2026-06-03 — `execute_tools()` auto-injects `cwd=memory_path.parent` into `mcp.builtin.bash_exec` when the model omits `cwd`, so `python -c "..."` output lands in the analyst's workspace folder. Fixed `FakeStorage` test stub missing `get_memory_for_paper`.
- 2026-06-09 — Native structured output + function calling (P0). `_call()` rewritten: it passes the output Pydantic model to `model.call(schema=...)`, takes `ModelResponse.parsed`, applies `overrides`, and validates strictly with `model_validate` — **no salvage parsing**; a missing/off-schema result raises loudly. Deleted `_payload_text`'s schema-in-prompt body (now just task data), `_parse_json_response`, `_strip_json_fence`, `_extract_json_value`. Call sites pass a `type[BaseModel]` (no more `TypeAdapter`). `request_tools(task, registry)` rewritten to native function calling over `registry.tool_definitions()`; its arguments arrive as real per-tool-validated values, fixing the hand-serialized-`indices` truncation crash. Deleted the unused `AnalystLeadAgent.review_tool_requests` (the only bare-`list[...]` structured output). Prompts no longer carry tool-specific instructions — tool names/args/usage flow through tool loading (function definitions + manifest descriptions); `analyst_worker.md` trimmed accordingly.
- 2026-06-11 — `_call()` gains a **bounded off-schema retry** (`_structured_retry_cap()` ← `orchestration.yaml::structured_retry_cap`, default 2). On a `ValidationError`/`ValueError` (off-schema or missing structured output) it re-issues the structured call, injecting a `_retry_feedback` note into the payload, mirroring the tool-call error-feedback loop; cap exhaustion still raises (fail-loud preserved). Other exceptions (auth/network — already retried by the SDK) propagate immediately. Motivated by the `analyst_lead.judge_convergence` run-killer where the Anthropic adapter's unenforced forced-tool returned a nested `{"parameter": {...}}` `ReviewVerdict`; the real fix is the adapter's switch to native `messages.parse` (see `models/DOCS.md`), with this retry as defense in depth across all providers.
- 2026-06-11 — **Inner ReAct loop for every agent + `react.py`.** Added `AgentRole._react` (message-accumulating tool loop), `_call_over_messages` (terminal structured output over a trajectory), and `_structured_loop` (shared retry core for both `_call` and `_call_over_messages`); `AgentRole` gained an optional `tools` ToolProvider. New `agents/react.py`: `ToolProvider` protocol, `ToolOutcome`, and three providers — `SandboxToolProvider` (wraps `ToolkitRegistry` + `PersistentSandbox`; moved `_normalize_tool_raw`/`_observation_path_args` here), `ReadOnlyToolProvider` (in-process read_file/list_dir/text_search, path-confined), `SourceToolProvider` (QuerySources + local text_search). **`AnalystWorkerAgent` rewritten**: `analyze()` now runs the ReAct loop (load→fit→run→plot chained in one call, seeing each result in-context) then reports grounded in real results, with the loop's figures attached; `request_tools`/`execute_tools` removed; `memory_path` is now the durable cross-round store and `last_tool_results` feeds the consensus peer summary; falls back to a plain call with no sandbox/registry. `ReviewerAgent.review` and `AnalystLeadAgent.adjudicate` use a `ReadOnlyToolProvider` loop (wired in `graph/runtime`) to inspect artifacts/figures/memory before deciding; `judge_convergence` stays a plain call. `LibrarianIOAgent.gather_literature` runs a `SourceToolProvider` loop (iterative search/refine) and returns de-duplicated results; the graph `literature_node` prefers it, falling back to `compose_search_strategy`→`search`. Fixes the run where analysts only ever explored/loaded and never fit (the tool-selection call was blind to its own prior results and one debate round gated each dependent step).
