# `auto_research.agents` — per-role wrappers

## Purpose
Thin wrappers around the provider layer that bind a role to its prompt
(`prompts/<role>.md`), its allowed tools, and its expected I/O schemas. One class
per role — reviewer, librarian I/O, librarian searcher, analyst worker, analyst
lead ([CLAUDE.md §10](../../../CLAUDE.md#10-agent-architecture--process-loop)).

## Architecture
Each role is a small async class:
1. Construction loads its prompt file and resolves its model via
   `auto_research.models.registry`.
2. Each public method accepts a task schema, returns a result schema.
3. Tool access (sources, retrieval, sandboxed toolkit calls) is injected — not
   imported globally — so swap-outs in tests are trivial.

Critical: **runtime LLMs never see `CLAUDE.md` or any `DOCS.md`**
([CLAUDE.md §4](../../../CLAUDE.md#4-claudemd-and-docsmd-are-build-time-only)).
Only the file in `prompts/` plus task data goes into the model.

Step 8 adds thin role wrappers. They call the selected model member and validate
the returned JSON against existing Pydantic schemas. Librarian tool access is
injected through `QuerySource` and `RetrievalBackend`. Analyst wrappers expose
available toolkit names, including `research_toolkit.xspec_utils`, as task
context only; toolkit execution remains deferred to the sandbox step.

## Interfaces / Contracts
Prompt loading:
- `load_prompt(role, root=None) -> str` loads `prompts/<role>.md`, strips leading
  and trailing whitespace, and raises `ValueError` if the prompt text references
  build-time governance filenames.
- `prompts_dir(root=None) -> Path` returns the prompt directory, allowing tests to
  inject a temporary prompt root.
- `PROMPT_FILENAMES` maps the five Phase 1 roles to their Markdown filenames.

Role wrappers:
- `ReviewerAgent.review(request, *, literature=(), report=None) ->
  ReviewVerdict`.
- `LibrarianIOAgent.search(request) -> list[SearchResult]`.
- `LibrarianIOAgent.ingest_document(document, *, paper_id=None,
  instructions=None) -> MemoryRecord` passes a `DocumentRef` attachment to the
  configured I/O model and returns one memory abstraction. It does not convert
  PDFs to text in Python; bytes are loaded only at the provider-call boundary.
- `LibrarianSearcherAgent.search(request) -> list[SearchResult]`.
- `AnalystWorkerAgent.analyze(task, *, peer_reports=()) -> AnalystReport`.
- `AnalystWorkerAgent.request_tools(task) -> list[ToolCallRequest]` captures
  requested shell/tool/database actions without executing them.
- `AnalystLeadAgent.adjudicate(task, reports) -> AnalystReport`.
- `AnalystLeadAgent.review_tool_requests(requests) -> list[ToolCallReview]`
  reviews proposed tool actions without executing them.
- All wrappers accept an injected `model`; otherwise they resolve their role
  from `config/models.yaml` and use the first configured group member unless
  `model_name` is supplied.
- Live role verification is opt-in:
  `AUTO_RESEARCH_LIVE_AGENT_TESTS=1 pytest tests/test_live_agents.py -s`.
  It runs one real model call for each role and writes JSONL plus Markdown logs
  under `logs/dev/step-8/`.

## Dependencies
- Internal: `auto_research.models`, `auto_research.schemas`,
  `auto_research.sources`, `auto_research.retrieval`,
  `auto_research.observability`.

## Dictionary-level reference
- `PromptRole` — literal type for the five Phase 1 runtime roles:
  `reviewer`, `librarian_io`, `librarian_searcher`, `analyst_worker`,
  `analyst_lead`.
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
  `ModelResponse` on `last_response` for logging.
- `ReviewerAgent` — reviewer wrapper. Method: `review()`.
- `LibrarianIOAgent` — external-source librarian wrapper. Method: `search()`.
  Method: `ingest_document()` for direct document attachment ingestion.
- `LibrarianSearcherAgent` — local-memory librarian wrapper. Method:
  `search()`.
- `AnalystWorkerAgent` — analyst worker wrapper. Method: `analyze()`.
  Method: `request_tools()`.
- `AnalystLeadAgent` — analyst lead wrapper. Method: `adjudicate()`.
  Method: `review_tool_requests()`.

## Changelog
- 2026-05-27 — Step 8 completed: added async role wrappers for reviewer,
  librarian I/O, librarian searcher, analyst worker, and analyst lead. The
  wrappers load runtime prompts, call configured model groups, validate schema
  JSON, inject source/retrieval dependencies, and expose toolkit availability
  without prescribing toolkit use.
- 2026-05-27 — Added the step-8 opt-in live role test and development log path.
- 2026-05-27 — Added direct document attachment ingestion for librarian I/O and
  report-independent tool request/review methods for analysts.
- 2026-05-27 — Step 7 completed: added runtime prompt loading utilities and
  governance-reference guards for prompt files.
- 2026-05-26 — Folder seeded during step 0.
