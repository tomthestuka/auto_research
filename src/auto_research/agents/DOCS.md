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
  ReviewVerdict`. `literature` is now `Sequence[Path]` — local PDF paths returned
  by `text_search_paths()`. Each existing path is loaded as a `DocumentRef`
  attachment so the model reads the full paper.
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
- `AnalystWorkerAgent.analyze(task, *, peer_reports=()) -> AnalystReport`.
- `AnalystWorkerAgent.request_tools(task) -> list[ToolCallRequest]` captures
  requested shell/tool/database actions without executing them.
- `AnalystLeadAgent.adjudicate(task, reports) -> AnalystReport`.
- `AnalystLeadAgent.judge_convergence(task, reports, *, round_index,
  max_rounds) -> ReviewVerdict` judges whether worker reports have converged
  enough for final synthesis.
- `AnalystLeadAgent.review_tool_requests(requests) -> list[ToolCallReview]`
  reviews proposed tool actions without executing them.
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
  - `request_tools(task)` → `list[ToolCallRequest]` — structured tool requests, no execution.
  - `execute_tools(task, registry, *, tool_retry_cap=3)` → `list[ToolResult]` —
    calls `request_tools()`, runs each via `sandbox.call_tool()` /
    `sandbox.call_mcp_tool()`, normalizes the raw result via
    `_normalize_tool_raw()`, and appends a truncated terminal recording to
    `memory_path`. Returns `[]` if `sandbox` is `None`; if an attached sandbox
    is not alive it starts a replacement before requesting tools. Per attempt it
    (a) merges the `ToolCallRequest.command` field into the args, (b) recomputes
    routing from `current_req.kind` so a retry that changes tool routes correctly,
    (c) for toolkit tools, auto-fills `<mission>_path` args declared in the tool
    `args_schema` from `config/observations.yaml` (via `_observation_path_args()`),
    and (d) calls `_normalize_tool_raw()` so structured payloads without a
    `stdout` key (e.g. `list_dir` → `{entries}`, `load_data` → indices dict) are
    serialized into `stdout`, and any captured `console_tail` is folded in.
    when the model omitted them.
  - `is_alive` → `bool` — `True` when no sandbox attached; delegates to
    `sandbox.is_alive` otherwise.
- `AnalystLeadAgent` — analyst lead wrapper. Method: `adjudicate()`.
  Methods: `judge_convergence()`, `review_tool_requests()`.

## Changelog
- 2026-05-26/27 — Steps 7–8 built: prompt loading with governance-reference guard; async role wrappers for reviewer, librarian I/O, analyst worker, and analyst lead. `LibrarianSearcherAgent` replaced by pure-Python `text_search_paths()`. `LibrarianIOAgent` gains full literature pipeline: `compose_search_strategy()`, `search()`, `ingest_results()` (download + `IODebateToConverge` + storage), `evaluate_search_results()` Phase 1 stub. `ReviewerAgent.review()` loads PDFs as `DocumentRef` attachments. Opt-in live role test under `AUTO_RESEARCH_LIVE_AGENT_TESTS=1`.
- 2026-05-28 — Step 11–12: `AnalystWorkerAgent` gains `sandbox`, `memory_path`, `worker_index`, `is_alive`, and `execute_tools()`. `analyze()` reads `memory_path` and injects private memory. All role wrappers accept optional `RunMonitor` for high-chatter event recording. `execute_tools()`: `command` field merged into args; routing recomputed per retry attempt; instrument paths auto-filled from `observations.yaml`; `_normalize_tool_raw()` serializes non-protocol keys into `stdout` and folds `console_tail`; `_append_tool_results_to_memory()` caps stdout/stderr at 8 KB/2 KB.
- 2026-06-03 — `ReviewerAgent` gains `compose_machine_report()` (structured `ResearchReport`; overwrites figures/references/created_at deterministically after LLM prose) and `compose_latex_report()` (returns raw LaTeX string from `LatexReport` schema; accepts `feedback` errors for retry self-correction; passes `images=report.figures`).
- 2026-06-09 — `_call()` gains `overrides: dict | None` parameter; `_parse_json_response` merges overrides into the extracted dict before Pydantic validation. `compose_machine_report` uses this to inject `run_id`, `request_question`, `verdict_rationale`, and `created_at` before the schema is validated, fixing crashes where the model omits runtime-owned fields (`run_id` has no default; `request_question`/`verdict_rationale`/`created_at` fail `NonEmptyStr`/`datetime` constraints when left as placeholders).
- 2026-06-03 — `execute_tools()` auto-injects `cwd=memory_path.parent` into `mcp.builtin.bash_exec` when the model omits `cwd`, so `python -c "..."` output lands in the analyst's workspace folder. Fixed `FakeStorage` test stub missing `get_memory_for_paper`.
