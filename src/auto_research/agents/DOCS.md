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
`config.documents` guard). A terminal `_call_over_messages(...)`
then produces the role's structured output grounded in the trajectory (both reuse
`_structured_loop`, the shared off-schema-retry core). Tool surfaces live behind
the `ToolProvider` protocol in `react.py`: `SandboxToolProvider` (analyst science
tools in the `PersistentSandbox`), `ReadOnlyToolProvider` (reviewer/lead/co-lead — in-process
`read_file`/`list_dir` confined to its `roots` (the permitted roots are named in each tool's
description and echoed in the `path not permitted` error so the model self-corrects instead of
guessing), an optional **knowledge-base toolset** — `text_search` (returns candidate paper
paths) **and `open_paper`** (attaches a chosen local PDF as a document, capped at
`_OPEN_PAPER_CAP`=6 distinct papers, recording `opened_paths`) — both gated by
`include_text_search` (default on; the §9.3 co-lead omits it and points `roots` at the
toolkit worktree instead), plus an optional `web_search` tool when a `web_source` is supplied
— the analyst lead uses it to consult authoritative docs while triaging tool requests, the
co-lead to check conventions while reviewing, §9.3),
`SourceToolProvider` (librarian — arXiv/ADS/web + local `text_search`). A role with
`tools=None` falls back to a plain single structured call.

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
- `AnalystLeadAgent` — analyst lead wrapper.
  Methods: `adjudicate()`, `judge_convergence()`, `clarify_for_author()`.
- `AnalystColeadAgent` — Phase-2 provider-decorrelated co-lead reviewer wrapper.
  Method: `review_tool(tool_spec, *, diff, gate, tools=None) -> ToolReviewVerdict`.

## Changelog
- 2026-05-26 -> 2026-06-12 — Agent wrappers built and ReAct-wired: reviewer, librarian I/O,
  analyst worker, analyst lead; native structured output/function calling, bounded schema retries,
  `Sandbox`/`ReadOnly`/`SourceToolProvider`, report composition, monitor hooks, `coder` prompt.
- 2026-06-12 -> 2026-06-18 — **Phase 2 agent support (steps 4–5, 7) + polish.** Lead read-only
  toolkit inspection + `web_search` + `clarify_for_author` (`ask_lead`); `AnalystWorkerAgent.analyze`
  the sole tool-execution owner; `AnalystColeadAgent.review_tool → ToolReviewVerdict` +
  `analyst_colead` role; both providers gained `ask_human`/`human_handler`. `decide_authoring(reports)
  → AuthoringDecision` (lead sets disposition + finalises one `ToolSpec`; consensus issues
  `propose_and_pr`, §9.9). Shared read-only `data_catalog` tool; `HumanQuery.model` sender label;
  `read_file`/`list_dir` self-describe permitted `roots`; literature/web faults degrade to
  recoverable `is_error`; every `react.py` tool gained manifest-quality descriptions.
- 2026-06-22 -> 2026-06-23 — **Diagnosability + retrieval + FS jail.** `_react` returns
  `(messages, figures, documents)` and splits each call into `*.tool_call` (input, pre-exec) +
  `*.tool_result` (output) events with capped `args`/`text` — a hung tool is visible in the JSONL;
  `ReadOnlyToolProvider.open_paper` (reviewer reads a candidate before citing; `cited_paths`).
  `ingest_results` isolates each paper (bad doc → metadata/orphan-memory, never aborts the node).
  FS jail (todo #3): `ReadOnlyToolProvider` denies governance basenames inside a root,
  `SandboxToolProvider` holds a `PathGuard.from_config()` gating `bash_exec`/`read_file`/`list_dir`.
  `analyst_worker.md`: all data ops are toolkit calls, `bash` read-only.
- 2026-06-24 — **§5.5 lead-drives-design + budget-free `plan_and_debate`.** `_react` gained
  `free_tools`/`absolute_cap` (`react_loop_cap`=30 bounds budget-consuming iterations only,
  `react_loop_absolute_cap`=60 backstop); the lead-only `plan_and_debate` design channel
  (`decide_authoring` opts in). Prompts: settle the design with the human before finalising the brief.
- 2026-06-26 — **`finalize_authoring_outcome` (handoff #4).** After a non-committed authoring
  attempt the lead composes ONE honest terminal `AnalystReport` (§9.6) instead of the consensus loop
  spinning; it flows to the reviewer (sole I/O). `reviewer.md` gained honest-failure adjudication.
  Coverage `tests/test_{consensus_authoring,graph}.py` + live `tests/test_live_honest_failure.py`.
- 2026-06-29 — **A1 slice 2: lead-only authoring tools `drive_coder`/`submit_for_review`.**
  `ReadOnlyToolProvider` gains an `authoring_session` param; when set it exposes the two tools
  (`drive_coder_tool_def`/`execute_drive_coder`, `submit_tool_def`/`execute_submit`) and dispatches
  them, else they are hidden **and** denied (the mask — a session is attached only on the lead's
  provider). `submit_for_review` runs gate → (gate-pass) GPT review → commit on both-pass; the lead
  never self-approves. They drive the same `AuthoringSession` primitives as `propose_and_pr`. Tests
  in `tests/test_react_tools.py` (masking, drive routing, the three submit branches). Suite 372 pass.
- 2026-06-29 — **A1 slice 3: `AnalystLeadAgent.drive_authoring` (the lead drives authoring).**
  `drive_authoring(tool_spec, *, toolkit_root, worktree_base, direct_branch, colead, human_handler)`
  builds + connects an `AuthoringSession`, attaches it to a lead-scoped `ReadOnlyToolProvider`
  (`include_text_search=False`, `authoring_session=…`), seeds the coder with the brief, then runs
  `_react` over `drive_coder`/`submit_for_review` (free: `submit_for_review` + `plan_and_debate`;
  ceiling `react_loop_absolute_cap`; submits bounded by `session.submit_cap`), and returns the
  terminal `MergeFlowResult` read off the session (`committed`/`failed`). Owns the session lifecycle
  (`aclose` in `finally`). Consensus calls this instead of `propose_and_pr`; `execute_submit` enforces
  `submit_cap`. Suite 373 pass.
- 2026-06-29 — **A1 slice 4: the lead↔co-lead debate (`respond_to_review`) + anti-sycophancy.**
  `AnalystColeadAgent.review_tool` gains `prior_review`/`lead_rebuttal`: when a rebuttal is supplied
  it **reconsiders** (concede only when genuinely persuaded; hold firm against pressure — the
  `reconsider_semantics` payload + `prompts/analyst_colead.md`). New lead-only tool
  `respond_to_review` (`respond_to_review_tool_def`/`execute_respond_to_review`): re-runs the review
  with the rebuttal; on a concede it **re-validates the gate** then commits (commit ⇔ gate ∧ review
  holds — the lead persuades, never overrides), bounded by `session.debate_cap`
  (`tool_author_debate_cap`). Added to the lead's authoring provider + `free_tools`. Tests in
  `tests/test_react_tools.py` (before-submit error, holds, persuade→regate→commit, debate cap,
  exposure). Suite 378 pass.
- 2026-06-29 — **Clarification channels take one Markdown `message`** (was `question`/`context`/
  `plan`): `ask_human`, `plan_and_debate`, `clarify_for_author`; `execute_plan_and_debate` no longer
  folds a plan into context. Typed protocol unchanged. Tests `tests/test_{hitl,react_tools}.py`.
- 2026-06-30 — **Authoring finalization is prompt-borne, not a code nudge loop.** Dropped the
  `_drive_authoring_loop` nudge-on-uncommitted-stop loop (+ `_drive_authoring_nudge_cap` and config
  `tool_author_drive_nudge_cap`) per CLAUDE.md §1: `drive_authoring` now runs a single `_react`, and
  `submit_for_review`'s tool description (`submit_tool_def`) states it is the **sole** finalization
  path — stopping without it discards the work — so a bare no-tool stop is a deliberate honest
  give-up. Decided after the sonnet-5 live loader run (`logs/dev/P2/loader-test/…140117`) showed the
  lead called `submit_for_review` 18× on its own and the nudge fired 0× (the run-1 narrate-and-stop
  did not recur). Tests `tests/test_{react_tools,merge_flow}.py`.
