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
`read_file`/`list_dir`/`text_search`, confined to the run + papers dirs, plus an optional
`web_search` tool when a `web_source` is supplied — the analyst lead uses it to consult
authoritative docs while triaging tool requests, §9.3),
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
- `AnalystLeadAgent.clarify_for_author(question, *, kind, context) -> str` — answers a
  tool author's `ask_lead` clarification (the lead↔author channel, §9.3); consults
  `web_search` (when a `ReadOnlyToolProvider` with a `web_source` is attached) before
  answering rather than guessing. Used by `merge_flow.propose_and_pr`.
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
- `PromptRole` — literal type for the runtime roles, mirroring `config/models.yaml` role
  keys: `reviewer`, `librarian_io`, `analyst_worker`, `analyst_lead`, and the Phase-2
  `coder` (the tool author).
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
  Methods: `adjudicate()`, `judge_convergence()`, `clarify_for_author()`.

## Changelog
- 2026-05-26 → 2026-06-11 — **Phase 1 built and ReAct-wired.** Four agent roles (reviewer,
  librarian I/O, analyst worker, analyst lead). Native structured output (`_call` via
  `model.call(schema=...)`), overrides injection, bounded off-schema retry
  (`structured_retry_cap`). Inner ReAct loop (`_react`, `_call_over_messages`,
  `_structured_loop`; `AgentRole.tools`). New `agents/react.py`: `SandboxToolProvider`,
  `ReadOnlyToolProvider`, `SourceToolProvider`. `AnalystWorkerAgent.analyze()` runs the
  full load→fit→plot loop in one call; `LibrarianIOAgent.gather_literature` runs an
  iterative source loop. Report composition (`compose_machine_report`,
  `compose_latex_report`). `execute_tools`/`request_tools` removed (tools live inside
  `analyze` now).
- 2026-06-12 — **Three run-killers fixed.** (1) `compose_latex_report` truncation: reviewer
  `max_tokens` 4096→16384 (`config/models.yaml`); the `\\` in the error was correct single
  JSON escaping, not a bug — root cause was the output ceiling. (2) Librarian loop cap split:
  `gather_literature` now reads `orchestration.yaml::librarian_react_loop_cap` (default 3)
  instead of the 12-cap general `react_loop_cap`; each search re-bills a growing 20-paper
  context so ~$20 was observed before the cap. (3) Figure placement: `SandboxToolProvider`
  gains `figures_dir` (`<worker cwd>/figures`) and `_redirect_figure_args()` keyed on
  `_FIGURE_PATH_ARGS = {save_path, save_path_prefix}` arg-name convention; workers now omit
  those args.
- 2026-06-12 — Phase 2 step 2: `AnalystLeadAgent.clarify_for_author()` added (the lead↔author
  `ask_lead` channel, used by `merge_flow`). `ReadOnlyToolProvider` gains an optional
  `web_source` → a `web_search` tool so the lead consults authoritative docs while triaging.
  `PromptRole` gains `coder` (the tool author's prompt), mirroring `config/models.yaml` keys.
