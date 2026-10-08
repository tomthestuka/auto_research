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
iteration (e.g. `wait_for_reply`) does not count, so waiting on the coder or the human never
starves the evidence-gathering iterations. `absolute_cap` (`react_loop_absolute_cap`, default 60; passed only by the lead's
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
`LibrarianToolProvider` (librarian, `agents/librarian_tools.py` — search, read in full, report,
`turn_end`; see below). A role with `tools=None` falls back to a plain single structured call.

**The librarian is one research loop that exits only through `turn_end` (CLAUDE.md §2b).**
`LibrarianIOAgent.research` runs `_react` over a `LibrarianToolProvider`: `search_literature`
returns numbered hits (leads, not evidence); `read_paper(hit|path)` downloads a hit's PDF (its
`pdf_url`, then its full-text links), mints a **paper receipt** in the run's `ReceiptStore`
(`sources.receipts.mint_paper_receipt`: the receipt IS the PDF — a paper is cited, never
text-matched, human ruling 2026-09-24) and
**attaches the PDF itself** as a document, which `_react` keeps attached for the rest of the loop
so every vendor reads it natively — text, tables, equations, figures; a paper with no text layer is
refused as uncitable; `web_fetch(url|handle, offset)` mints a web receipt and pages the extract in
24k-character windows that end at a line break;
`text_search` searches the local knowledge base. The model writes a `LiteratureReport` with
`edit_report` and submits it with `turn_end`, whose validator refuses: no report; success with no
findings; a finding with no `support` or citing a handle this loop never opened; and every problem
the shared binding check (`provenance.check_bindings`) finds. A paper value must be an
**attribution** with its place, `{{lib:p3 "12.5 ± 3.1 days" Table 2}}` — unverified, and collected at
filing into `LiteratureReport.attributed_values` (`LibrarianToolProvider.attributed_values`, the
shared `PaperAttribution` shape), the list a person checks against the PDFs; every number outside a binding is checked against the web
extracts cited, so a paper citation never exempts the numbers beside it; gaps have their citations
checked but no number scan. Reopening a document
returns its existing receipt, so a handle is stable for the run (and across a resume). Report
authoring and `turn_end` are budget-free; the budget is `librarian_react_loop_cap`. After filing,
each finding is appended as memory on the knowledge-base rows of the papers it cites. Nothing is
composed on the librarian's behalf: an unfiled loop raises `ReactBudgetExhausted`.

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

**The quick channel (`message`, E12, 2026-09-25).** Every graph seat — reviewer, librarian, lead,
workers — is on one `hitl.router.SeatRouter` (`graph.runtime.attach_channel`), and `AgentRole._react`
wraps its provider in `ChannelTools`, which adds three tools: `message(to, text, attachments, kind)`
(`message_tool_def`; the `to` enum is the directory — every other seat, then `human` only when a
handler is wired), `view_receipt(handle)` (`execute_view_receipt`) and `wait_for_reply(timeout_s)`
(`execute_wait_for_reply`, budget-free). **`message` never blocks**: to a live seat the text lands
in its inbox (`RunControl`, drained at the loop's one boundary under A2's hold rule, rendered by
`InboxEntry.render`); to a seat that finished its turn it lands there and **resumes that seat's normal
loop** in the background (`AgentRole.resume` on the turn `_react` kept); to a seat that has not
started it waits; to the human the router awaits the handler in the background and posts the reply
to the sender's inbox. **Texts are informal**: none mints a receipt or widens a citable scope; a
handle a text names becomes *viewable* by the recipient only. The human's text is a nudge (never a
receipt); the files the human sends are the sender's receipts (`hitl.attachments`). `ChannelTools`
also holds `turn_end` while a seat this one resumed is still in its turn, and in a resumed turn
declares exactly the finished turn's tool set, refuses the tools that would re-file
(`edit_report`, `file_edit`, the lead's cycle tools — the filed report is final) and accepts
`turn_end` without touching the report. The co-lead and the status monitor get no router, so
their loops are exactly `_react_loop`. The providers themselves carry no human tool.

**Lead authoring tools (`message` to `coder` / `submit_for_review`, §9.3 B-topology — A1 slice
2).** When an `authoring_session` (an `auto_research.sandbox.merge_flow.AuthoringSession`) is wired
or the cycle enables authoring, the lead's channel layer adds the address `coder` to its `message`
tool (with an optional coder-only `mid_turn`), and `ReadOnlyToolProvider` declares
`submit_for_review` / `respond_to_review` — lead-only, because a session is attached only on its
provider. A text to `coder` (`ChannelTools._to_coder` → `execute_coder_message`, labelled
`[Message from analyst_lead]`) steers the live coder (the lead is the plan owner, the coder
executes; `drive_coder` until E12 folded it in); `submit_for_review` (`execute_submit`) runs the deterministic gate, then (on gate-pass)
the provider-decorrelated GPT co-lead review, and **commits on gate-pass ∧ review-pass** — the lead
never self-approves (`commit ⇔ gate ∧ review`, §9.9). The gate is a mechanical wall the lead can only
*satisfy* by messaging the coder; the review is *debatable* (the slice-4 `respond_to_review`). Both will be
listed in the lead's authoring-loop `free_tools` (slice 3), bounded by `submit_cap` +
`react_loop_absolute_cap`. They drive the **same** `AuthoringSession` primitives `propose_and_pr`
drives in its auto-loop — the difference is *who* diagnoses each verdict (the lead, vs. the coder).

**The lead waits on its coder; it does not poll (`wait_for_reply`, 2026-09-24; renamed from
`wait_coder` by E12).** The coder runs beside the lead: a text to `coder` returns at once, **queueing** a
message for a working coder as its next turn (`mid_turn=true`, or any reply to an open coder
question, steers the running turn instead). While a coder is working, `wait_for_reply(timeout_s)`
(`execute_wait_coder`) parks inside the tool call on `AuthoringSession.wait_for_event` and returns
when the coder's turn ends (with its narration), a message reaches the lead's inbox (the coder's own
question arrives there as `[Message from the coder]`, as does a human steer — delivered by `_react`'s
drain), or the timeout passes. A timeout under `coder_wait_floor_s` is **raised** to it, never
refused; every return starts `[waited X of Y; coder working, Z into its current turn]`, the lead's
clock. `wait_for_reply` is free in every seat's loop. `submit_for_review` on a working coder waits
for it to stop (up to `coder_wait_max_s`) before the gate runs, and hands back a message instead of
gating. `_ChainDriver._open` wires the lead's `RunControl.steer_event(agent_key)` into each link's
session.

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
| Librarian I/O | `LibrarianToolProvider`: arXiv/ADS/web search, full-text reading of papers and pages (each minted as a receipt), local text search, `edit_report`/`review_report`/`turn_end`. | One research pass: a `LiteratureReport` whose findings cite, by handle, the documents it read. |
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
- `ReviewerAgent.review(request, *, arrived="request", literature=(), literature_receipts=None,
  report=None, previous=(), human_nudges=(), human_files=(), budgets=None, human_attended=False,
  render=None) -> ReviewerReport` — one reviewer turn as ONE ReAct loop
  whose only exit is `turn_end` (`require_terminal=True`, §2b). The reviewer writes its report with
  `edit_report` (surface `REVIEWER_REPORT_FIELDS` over `ReviewerReport`, description
  `REVIEWER_EDIT_DESCRIPTION`), reads papers with the shared `paper_tools.PaperReader` under source
  `rev` (via `extra_tools`: the PDF itself attached, quotes checked against its text layer, one
  handle per paper across roles; `open_paper` is not declared for it), sees its draft typeset through
  `review_report` (`draft_view`, always the typeset preview when `tex` is configured, since any
  turn may report to the human), and `turn_end` runs `reviewer_turn_problems` — including render +
  compile of a report to the human through `render` (a `ReviewerRender`). The payload names what
  `arrived`, the budgets (`_review_instruction`), its own earlier reports (`_earlier_report`),
  every literature report so far, the latest analysis report, the human's nudges (reply text,
  guidance only) and files (`_human_file`). Its scope is a `ScopedLookup` over the run's store: its
  own `rev:` receipts, the request (`input:hN`, minted via `store.mint_input`), the human's files
  (`input:`), the librarian's receipts across passes, every handle the lead's report cites and the
  lead's figures (matched to receipts by hash), with their ancestors. Requires `tools`, `receipts`
  and `report_dir` (the runtime sets all three, `graph/runtime.attach_reviewer_runtime`); raises
  if the loop ends without an accepted `turn_end`. One JSONL per turn, `review{n}.jsonl`, beside
  the ledger (`sandbox.workspace.report_record_path`), out of every role's reach.
- `LibrarianIOAgent(*, sources=None, storage=None, model=None, ...)`. `sources` defaults to
  arXiv, NASA ADS and Tavily web; `storage` to `Storage()`. Attributes the runtime sets:
  `report_dir` (its working folder; per-pass `literature_pass{n}.jsonl` beside the ledger via
  `report_record_path`), `agent_key`, and
  `receipts` (the run's one store, `ReceiptStore.for_run(run_dir)`; unwired, it takes
  `for_run(report_dir)`, so no second instance ever opens one directory).
  `last_receipts: dict[str, Receipt]` — the documents the last pass opened, by handle.
- `LibrarianIOAgent.research(request, *, follow_up=None) -> LiteratureReport` — one research
  loop (above). `follow_up` is the reviewer's literature re-loop instruction. Returns the report
  the librarian filed through `turn_end`, or raises `ReactBudgetExhausted`.
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
  `AuthoringSession`, seed the coder, run the lead's loop over `message` to `coder` /
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

Reviewer report surface and check (`reviewer_tools.py`, 2026-09-24; wired into
`ReviewerAgent.review`):
- `REVIEWER_REPORT_FIELDS`, `REVIEWER_STATUS_VALUES` (empty: `assign_to` is the decision),
  `REVIEWER_SOURCE = "rev"` (the reviewer's own receipts: papers it opens, reports it renders).
- `reviewer_turn_problems(report, *, lookup, scope, budgets, render=None) -> list[str]` — the
  reviewer's `turn_end` door. Refuses: no report; empty `rationale`; no `assign_to`; a report to
  the librarian or analysts with an empty `follow_up` or no passes left (`budgets`, cap named);
  a report to the human missing title/abstract/sections/key_findings; a citation of an earlier
  published report (`PUBLISHED_REPORT_PRODUCER`); any number no receipt grounds, anywhere,
  `follow_up` included (`check_report_fields(itemised=(), scope=…)`); then, for a report to the
  human only, whatever `render` returns.
- `reviewer_scope(lookup)` — the handles that ground uncited: every readable receipt except
  `input:` ones (the request, the human's files) and published reports (`report_renderer`), which
  record a decision and carry the request, nudges and unlocated paper values.
- `ReviewerRender(report_dir, run_id, formats=("tex",))` — callable used as `render`: renders and
  compiles when `tex` is configured, returns model-fixable problems, and keeps the last
  `render`/`compile` result and `renderer_errors` for the graph.
- `reviewer_scope(lookup) -> list[str]` — every readable handle except inputs: the request and
  dispatched tasks ground a text only when it cites `[input:hN]` (human 2026-09-24).
- `reviewer_draft_view(report_dir, run_id, lookup) -> (report -> (text, [FigureRef]))` — renders
  the draft into `report_dir/draft/`, compiles, and rasterizes up to `MAX_PREVIEW_PAGES` (12) pages
  at `PREVIEW_DPI` (80) with `pdftoppm`; a render problem is shown instead of pages.
- `reviewer_text_view(lookup) -> (report -> (text, [FigureRef]))` — `review_report`'s draft view
  when no `.tex` is configured: the prose fields plus each placed figure from its receipt.
- `READ_PAPER_TOOL`, `REVIEWER_EDIT_DESCRIPTION`.

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
  Quick channel (E12): `channel` (the run's `SeatRouter`, set on graph seats only);
  `_react(*, tools, resumed=False, declared=None, **loop_kwargs)` wraps `_react_loop` in
  `ChannelTools`, marks the seat live for the loop and keeps a turn that filed its terminal;
  `has_kept_turn()`; `resume()` reruns the kept turn's normal loop (report final);
  `ensure_run_control()` makes a `RunControl` for a role run outside a run (a benchmark lead).
- `ChannelTools(base, *, role, resumed=False, declared=None)` — a seat's provider plus the channel:
  declares `message`/`list_peers`/`view_receipt`/`wait_for_reply`, adds the lead's `coder` address
  while authoring is declared (`_coder_declared`, `_to_coder`), holds `turn_end` while a resumed
  seat runs, and in a resumed turn replays the kept declaration and refuses `_RESUME_CLOSED` and
  the coder address.
- `react.py` channel symbols: `MESSAGE_TOOL`, `LIST_PEERS_TOOL`, `VIEW_RECEIPT_TOOL`,
  `WAIT_FOR_REPLY_TOOL`, `CHANNEL_TOOLS`; `message_tool_def(directory)` (a `coder` address adds the
  instruction note and `mid_turn`), `list_peers_tool_def()`, `execute_list_peers(router, sender,
  label)`, `execute_coder_message(session, text, *, mid_turn=False)`, `view_receipt_tool_def()`,
  `wait_for_reply_tool_def()`; `execute_view_receipt(lookup, viewable, store, arguments, *,
  images_ok)` (mints and admits nothing); `execute_wait_for_reply(run_control, agent_key,
  arguments, *, outstanding)` (a seat with no coder; `execute_wait_coder` serves the lead's coder).
- `ReviewerAgent` — the reviewer as a unit. Method: `review(...)` (see Interfaces); attributes
  `receipts`, `report_dir`.
- `LibrarianIOAgent(sources, storage, ...)` — the librarian. Method: `research()`.
- `agents/paper_tools.py` — **the shared paper reader** (human ruling 2026-09-24: every role opens a
  paper the librarian's way). `PaperReader(store, lookup, *, source, storage=None, allowed=None,
  minter=None, unreadable_action=...)`: `execute({"path"|"handle"})`, `open_pdf(pdf, *, title=None,
  ids=None) -> (ToolOutcome, Receipt | None)`, `open_handle(handle)`, `tool() -> ExtraTool`,
  `definition(extra_properties=, lead=)`. Opening attaches the PDF natively; a PDF whose bytes
  already have a paper receipt from ANY source keeps that handle and is admitted into `lookup`
  (one paper, one handle per run); otherwise it is minted under `source`. `lookup` may be a
  zero-argument callable (per-round scopes). Title/ids from the knowledge base, else the file stem.
  `paper_reader_tool(store, lookup, *, source, storage=None, allowed=None) -> ExtraTool`.
- `agents/librarian_tools.py`: `LibrarianToolProvider(sources, *, storage, store, report_path,
  papers_dir, web_source=None, retrieval=None, source_key="lib", record=None, downloader=None,
  paper_minter=None)` — the librarian's tool surface; `captures` (handle → `Receipt` opened by
  this loop), `hits`, `paper_ids`, `written_report`, `turn_problems()`. Constants
  `SEARCH_LITERATURE_TOOL`, `READ_PAPER_TOOL`, `WEB_FETCH_TOOL`, `TEXT_SEARCH_TOOL`,
  `LIBRARIAN_FREE_TOOLS`, `READ_WINDOW_CHARS = 24_000`. `SourceToolProvider` (the old
  librarian's `react.py` provider) is deleted.
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
Compacted 2026-09-01 and again 2026-09-24 (every entry); detail in git history.

- 2026-05-26 -> 2026-06-18 — Reviewer, librarian, worker and lead wrappers (native structured
  output, bounded schema retries, `Sandbox`/`ReadOnly`/`SourceToolProvider`); Phase 2 added
  `clarify_for_author`, `AnalystColeadAgent.review_tool`, `ask_human`, `decide_authoring`,
  `data_catalog`.
- 2026-06-22 -> 2026-06-30 — `_react` returns `(messages, figures, documents)` with split call/result
  events; `open_paper`; FS jail (`PathGuard`); `free_tools`/`absolute_cap`, `plan_and_debate`;
  `finalize_authoring_outcome`; `drive_coder`/`submit_for_review`/`respond_to_review`,
  `drive_authoring`.
- 2026-07-06 -> 2026-07-14 — [F4] `_parse_tool_figures` drops a bad figure with a note; [A8] ordered
  `tool_specs` chain; [I1]/[I10]/[F8] `web_search` for every role, paged `web_fetch`, line-range
  `read_file`; V-4 `edit_report`/`review_report` with cited figures replace figure-dedup.
- 2026-07-20 -> 2026-07-28 — `data_catalog` served from `research_toolkit.catalog_utils`;
  `_record_context_edit`; `AgentRole.run_control`/`agent_key` + steer inbox; deferred tool loading
  under `tool_search_enabled` (LIVE-VERIFIED 2026-07-28, enabled).
- 2026-07-29 -> 2026-07-31 — Events actored by indexed `agent_key`; stop-flag reading deleted (`/stop`
  cancels); the lead publishes its `AuthoringSession` on `RunControl`; new `MonitorToolProvider`
  (`brief_history`/`ledger_event`/`ledger_grep`) + `StatusMonitorAgent` (`status_react_loop_cap`).
- 2026-08-05 -> 2026-08-31 — `_react` appends the received `ModelResponse` whole (`AssistantToolUse`
  deleted); terminal composes declare the loop's tools with `tool_choice="none"`; `_OPEN_PAPER_MAX_MB`
  deleted (limits live in the adapters); [F11] a malformed `offset`/`limit` returns `is_error`.

- 2026-09-01 — `_EVENT_ARG_CAP`/`_EVENT_RESULT_CAP` replaced by `_event_arg_cap()`/
  `_event_result_cap()` reading `env.ledger_caps()`. New `tests/test_live_no_hitl.py`: headless roles
  lose exactly `ask_human`/`plan_and_debate`; LIVE-VERIFIED, each role saw the closed channel.
- 2026-09-02 — The lead became one ReAct loop filing forms (`run_cycle`: `author_chain`,
  `hold_authoring`, `request_another_round`, `submit_adjudication`, `submit_terminal_report`);
  `adjudicate`/`judge_convergence`/`decide_authoring`/`finalize_authoring_outcome` deleted.
- 2026-09-02 — `_react` gained `terminal_tools`; drive tools declared on `cycle.authoring_enabled`
  (`_authoring_declared()`); a committed link's coding span dropped; `drive_authoring` kept for the
  benchmark. LIVE-VERIFIED (`tests/test_live_lead_cycle.py`); new `tests/test_live_form_shapes.py`.
- 2026-09-02 — Forms topology LIVE-VERIFIED in production: an empty `author_chain` was refused and
  refiled, a link committed, and the dropped span returned the lead to its pre-coding context.

- 2026-09-12 — A1–A11: every form but `author_chain` deleted (with the stage constants,
  `_STAGE_FORMS`, `LEAD_CYCLE_TERMINALS`, `_author_chain`, `_drive_one_link`); `run_cycle` ends on
  `turn_end` (`lead_turn_problems`); `_ChainDriver.advance` opens the next link on a tool result.
- 2026-09-12 — `_react` gained `share_messages`; `AnalystReport` gained `rework`/`rationale`/
  `what_to_probe`, `status` regained `needs_human`; `edit_report_tool_def(prose_fields,
  status_values)` gives each role its surface; the reviewer loop gained `terminal_tools={turn_end}`.
- 2026-09-12 — A2: one steer drain per iteration, held while the last turn has an unresolved
  server-tool call (`_last_turn_has_unresolved_server_tool`); nothing appended to a paused turn.
- 2026-09-12 — B3–B6: `drive_coder` returns at once (`start_drive`); new `check_coder`; a stopped
  coder reaches the lead through A2's queue; B6 drop on committed links only (reversed 09-13).
- 2026-09-12 — `tests/test_live_form_shapes.py` renamed `tests/test_live_lead_providers.py`: schema,
  whole `run_cycle` and own-history replay cells per vendor.
- 2026-09-12 — `check_coder` actually routed; `_ChainDriver._open` uses `start_drive`;
  `execute_submit` refuses (budget-free) while the coder is working.
- 2026-09-12 — `clarify_for_author` and `_ClarifyReply` deleted: the coder's question enters the
  lead's loop via `_coder_questions` as a `[coder asks]` turn under A2's hold rule.

- 2026-09-13 — `_drop_coding_middle` (it stranded an unpaired tool call) replaced by
  `_compact_coding_span`: the opening call's answer becomes the link outcome, facts written in
  Python; both arms compact (reverses B6). New `compactor` role; new `_unanswered_call_ids`.
- 2026-09-13 — `_build_session` is the only method a test double replaces (`tests/chain_stub.py`
  `StubSession`); new `AUTO_RESEARCH_LEAD_SEAT` for the chain live test.
- 2026-09-15 — `check_coder` rate-limited (`check_coder_min_interval_s`, default 900 s; replaced
  09-24) and the four strings urging polling corrected.
- 2026-09-15 — Ruling: `turn_end` is the only exit. `_react` gained `require_terminal` (lead, then
  worker): a silent turn gets `_TERMINAL_REQUIRED_NUDGE`, stating the channel rule (human wording);
  the `len(content) > 40` finish test removed; `refusal`/`transient` still end the loop.
- 2026-09-15 — Grace window (`react_grace_rounds`, default 20) once a budget is spent; closing it
  unfiled raises `ReactBudgetExhausted`; `turn_end` joins `_CYCLE_FREE_TOOLS`. Escape shapes
  (`escape_by_prose`/`_claiming_done`/`_idling`) in `test_live_turn_end_shapes.py`, all pass live.

- 2026-09-24 — (95) Librarian: `research()` is one `_react` loop over new `LibrarianToolProvider`
  filing a `LiteratureReport` ([I2], [I9], §2b); `compose_search_strategy` … `_dedup_results` (ten
  pipeline methods) deleted; failures recorded as `librarian.source.error`.
- 2026-09-24 — (85) `check_coder` replaced by blocking `wait_coder` (`env.coder_wait_limits_s`: floor
  60 / default 300 / max 1800 s); `drive_coder` queues, `mid_turn` steers; `execute_submit` waits
  (`_wait_before_submit`, `_submit_now`); `check_coder_min_interval_s` removed.
- 2026-09-24 — (5c) `reviewer_tools.py`: the reviewer's report surface and `turn_end` check, which
  renders and compiles the report; ruling: a number in `follow_up` must be grounded.
- 2026-09-24 — (0f) Provenance P1/P2: both providers mint receipts per call (inputs hashed first,
  figures re-hashed from the file, written files as `data`, `web_fetch` as `web`);
  `report_provenance_problems` checks each report in `edit_report` (as notes) and at `turn_end`.
- 2026-09-24 — `ReportWorkspace` gained `receipts`/`receipt_source`/`itemised`/`provenance_check`;
  `edit_report_tool_def(..., report_model=)` derives properties from the model; `extra_tools`;
  per-round `ScopedLookup` (`roles._receipt_scope`).
- 2026-09-24 — Report tools serve a plain model (`_edit_plain_report`, `ReportWorkspace.draft_view`/
  `edit_description`); `ReadOnlyToolProvider(include_open_paper=False)`;
  `tests/test_provenance_wiring.py`.
- 2026-09-24 — P7 live shapes (`ungrounded_number`, `unknown_handle`, `misquoted_span`,
  `quote_without_text`, `flagged_quote`): four refused and recovered LIVE, `flagged_quote` never
  reached; `LeadCycleOutcome.verdict` deleted; `coder_still_running` seed closes on wait/submit.
- 2026-09-24 — `roles._mint_task_input` receipts the task on entry to `analyze`/`run_cycle`
  (`[input:…]`).
- 2026-09-24 — (5c) `ReviewerAgent` is one loop over one report on the generic `edit_report`;
  `compose_machine_report`/`author_latex_report` deleted; §2b closed for the reviewer.
- 2026-09-24 — (95) `search_literature` uses `search_with_match` with a fallback note; prompt tuned;
  `read_paper` attaches the PDF natively (ruling: no Docling, no vendor citation feature).
- 2026-09-24 — Shared `agents/paper_tools.py` (`read_paper`, `PaperReader`, `format_ids`,
  `ids_from_stem`) serves librarian and reviewer (ruling); `ReviewerPaperReader` deleted.
- 2026-09-24 — Librarian shape matrix (`tests/test_live_librarian_shapes.py`,
  `test_librarian_shape_contract.py`); citations scoped via `ScopedLookup`. Rulings: papers cited,
  never quoted (option A), then per-number attribution with a locator (`attributed_values`).
- 2026-09-24 — Reviewer `review_report` without `tex` uses `reviewer_text_view` (crash fix); prompt
  asks for located paper attributions. Shape `attribution_without_locator` LIVE-VERIFIED.
- 2026-09-25 — [I4] `_react` answers a call carrying `arguments_error` with an `is_error` result asking
  for valid JSON, and never executes the tool on empty arguments.
- 2026-09-25 — [G14] `_parse_tool_figures` moved to `sandbox.runner.parse_tool_figures`;
  `SandboxToolProvider` imports it (behaviour unchanged).
- 2026-09-25 — [G15] `_normalize_tool_raw`, `_PROTOCOL_KEYS` and the result-text rule moved to
  `sandbox.runner` (`normalize_tool_raw`, `PROTOCOL_KEYS`, `tool_text`); behaviour unchanged.
- 2026-09-25 — [R1] `ReviewerAgent.review` is one turn of the sole-I/O reviewer: `stage` and
  `iterations_left` replaced by `arrived`, `previous`, `human_nudges`, `human_files`, `budgets`,
  `human_attended`; `reviewer_turn_problems` routes on `assign_to` (cap per team), refuses citing
  an earlier published report, and `reviewer_scope` drops published reports. Role report JSONLs
  (reviewer, librarian, workers, lead) move beside the ledger (human ruling: out of reach, since a
  read-back minted a role's own typed numbers). `_react`: a call after an accepted terminal in the
  same batch is answered and not run (found by an adversarial review: an `edit_report` after
  `turn_end` replaced the filed report; human ruling, every role).
- 2026-09-25 — [E12] Quick channel between any two seats: `ChannelTools` (in `_react`, which now
  wraps `_react_loop`) adds `message` / `view_receipt` / `wait_for_reply` for every graph seat on a
  `SeatRouter`; `message` never blocks, a finished seat is resumed on its kept turn
  (`AgentRole.resume`, report final), texts mint no receipt, the human's reply is a nudge. Deleted
  `ask_human`/`plan_and_debate` (defs, executors, provider branches, `_RECEIPTED` entries) and the
  providers' `human_handler`/`include_plan_and_debate`/`asked_by`/`model` params; `wait_coder` →
  `wait_for_reply`; `_coder_questions` deleted (the coder's question reaches the inbox).
- 2026-09-25 — [E12] `list_peers` (free): each address with its role, model and state (working /
  finished, a text resumes it / not started), the human when attended, and for the lead its coder.
  `drive_coder` folded into `message(to="coder")` (user ruling): the lead's `coder` address is
  declared for the whole authoring cycle, `mid_turn` kept as a coder-only field, the coder reads
  `[Message from analyst_lead]`; `drive_coder_tool_def`/`DRIVE_CODER_TOOL` deleted,
  `execute_drive_coder` → `execute_coder_message`; every model-facing mention rewritten.
- 2026-09-25 — [E12] A seat whose loop can author (`_has_authoring_surface`: a live session or a
  cycle enabling authoring) and has no router gets a one-seat `SeatRouter` from `_react` itself, so
  the lead reaches its coder with `message(to="coder")` however it was built (only `run_research`
  attached the team channel; `attach_lead_runtime` alone left the coder unreachable).
- 2026-09-25 — [R7] `ReadOnlyToolProvider`: a committing `submit_for_review` mints a `commit`
  receipt (`_commit_receipted`: tool, SHA, branch, gate/review passed, submissions used/cap), and the
  in-process `data_catalog` is receipted under `research_toolkit.catalog_utils.data_catalog`.
  `reviewer_tools.reviewer_claims` / `BRIEF_FIELDS`: the reviewer's `follow_up` is exempt from
  grounding (a brief); `reviewer_scope` excludes only `input:` (published reports are not evidence).
- 2026-10-06 — **[F20] The `bash_exec` command-path scan stands down only for a server launched inside the OS jail**: `_jail_supersedes_command_check(registry, kind)` reads `ToolkitRegistry.mcp_jailed(kind)` (recorded at launch) instead of the config's `agent_os_jail`, so a configured `mcp_servers` entry, a host without `bwrap` or a config edited mid-run keeps the scan.
- 2026-10-06 — **[F22] A worker's cwd is its own folder (`report_dir`), not memory.md's**, which
  moved beside the ledger; files a human sends land in `workspace.human_reply_dir` (beside the
  ledger), not in the agent's writable folder.
- 2026-10-06 — **Env plan B2a.** Workers get `list_env` (`react.list_env_tool_def`,
  `execute_list_env`): with no argument, each environment's name, owner, Python version, fingerprint
  and which is active; with `env`, its conda/pip packages, the variable names its activation sets
  and its externals (`sandbox.envs.describe`). Receipted as navigation ("an environment listing").
  Every sandbox tool result carries `[ran in <env> @ <fp12>]` after its receipt header, and the
  worker's turn-start payload names its `environment` (`roles._environment_brief`). Until pipeline
  environments exist (B3) the only environment listed is the sandbox's own.
- 2026-10-06 — **Env plan B2b: `use_env`.** `react.use_env_tool_def` / `execute_use_env`: a worker points its own shell (`bash_exec`) at another environment from the catalogue (`sandbox.environments()`: its own first, then every pipeline-built one); no name returns it. The provider rewrites the command (`_in_environment`): `env -i` with that environment's clean activation, the toolkit on `PYTHONPATH`, the jail's `HOME`/`TMPDIR`/`PFILES`; the call records `shell_env` and its result carries `[shell in <env>]`. Toolkit tools are untouched (routing is B3b, after T1). `list_env` lists the catalogue with `active` (the REPL's) and `shell` flags.
- 2026-10-06 — **B2b review fixes:** `_in_environment` sources the environment's activation inside the jailed shell (`env -i` with the jail's `HOME`/`TMPDIR`/`PFILES` and locale, then `source`, then `<prefix>/bin` first on `PATH`, the toolkit on `PYTHONPATH`; a failed activation ends the command with an error); a model-supplied `shell_env` argument is dropped; the chosen environment is looked up by name on every call (a rebuilt one is the current build; a vanished one is an error naming `use_env`).
- 2026-10-06 — **B2b review 3 (clean) lows fixed:** `use_env` takes a pipeline environment only when the shell server's launch recorded `jailed`; `_in_environment` restores what an activation could change (functions, aliases, `set -e/-f`, `IFS`, the working folder), replaces only the writable half of `PFILES` (the whole of it when the activation sets none), and reports an activation that ends the shell before the command (a marker written just before it); `list_env`'s description says activation variables are listed for human environments.
- 2026-10-06 — **Authoring (B3c-1 review 8):** `execute_respond_to_review` waits for a working coder before the re-review (as `execute_submit` does); both commit sites return `Not committed: <reason>` as an error when the session refuses (tree or HEAD changed after the gate or the review).
- 2026-10-06 — A commit landed through `respond_to_review` mints a `commit` receipt like `submit_for_review`'s; the commit sites also catch `OSError`/`CalledProcessError` as `Not committed` errors.
- 2026-10-06 — Both review calls (`submit_for_review`, `respond_to_review`) return an error result when the review cannot read the changeset (`OSError`/`ValueError`/`CalledProcessError`), never an exception that ends the lead's loop.
- 2026-10-06 — **The lead's in-process `data_catalog` runs committed toolkit code only** (`react._toolkit_clean_head`): while the toolkit holds uncommitted changes (an authoring session's work) it returns an error asking to call again later, and its memo is keyed on the toolkit commit — a new commit re-imports `catalog_utils`/`obsdb_catalog` (B3c-1 review 31: an edited, later rejected module ran unjailed in the orchestrator, its output minted as evidence, and kept running from memory after the reset).
- 2026-10-06 — `data_catalog`'s re-import purges the toolkit's bytecode first and imports with `sys.pycache_prefix` set to a private folder (git ignores bytecode, so the clean check could not see a forged `.pyc` matching the committed source's header; B3c-1 review 32).
- 2026-10-06 — **B3c-3:** `_in_environment` now lives in `sandbox/envs.py` as `in_environment` (shared with the coder's candidate wrappers; react imports it under the old name). It calls `/usr/bin/env` by path and keeps `USER`, `LOGNAME`, `LC_*`, `TZ` and proxy variables when set.
- 2026-10-06 — **B3c-4:** the lead's commit answers (`submit_for_review`, `respond_to_review`) append the session's `env_note`: which environments the commit built, or why one did not (the commit stands).
- 2026-10-06 — **Env plan B3b: routing.** `SandboxToolProvider._route_environment`: a toolkit call runs in its package's tested environment (`registry.tool_env` → `ar-<id>`, resolved by name from the catalogue each call), or in the caller's `env` choice (popped, never sent to the tool; receipts marked `env_untested`); an unbuilt tested environment, or a pipeline environment with the jail off, is an error result. The result header names `sandbox.call_environment` and says "untested environment for this tool". `prompts/analyst_worker.md` says how to choose.
- 2026-10-06 — **B3b review fixes:** an empty, `null` or `none` `env` is no choice; `_sandbox_env_fingerprint` falls back to the call's routed environment.
- 2026-10-06 — **B3b review 2:** a call the sandbox never ran (an unregistered tool) is not stamped with an environment.
- 2026-10-07 — **`read_paper(path=)` reads only the papers folder** (librarian and reviewer): a paper receipt is evidence, and a PDF a run wrote (a worker's scratch) had been mintable as one. Downloads land in the papers folder, outside every role's write reach; a human's file stays a `human` receipt. Test: a shell-written PDF in a worker folder is refused, a PDF in the papers folder opens.
