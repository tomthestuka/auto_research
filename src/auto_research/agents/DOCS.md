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

**The lead waits on its coder; it does not poll (`wait_coder`, 2026-09-24).** The coder runs beside
the lead: `drive_coder` returns at once, **queueing** a message for a working coder as its next
turn (`mid_turn=true`, or any reply to an open coder question, steers the running turn instead).
`wait_coder(timeout_s)` (`execute_wait_coder`) parks inside the tool call on
`AuthoringSession.wait_for_event` and returns when the coder's turn ends (with its narration), the
coder asks (`[coder asks] …`), a human steer for the lead arrives (the steer itself is still
delivered by `_react`'s own drain), or the timeout passes. A timeout under
`coder_wait_floor_s` is **raised** to it, never refused, so no calling pattern gets an instant
empty answer; every return starts `[waited X of Y; coder working, Z into its current turn]`,
which is the lead's clock. `wait_coder` is in `_CYCLE_FREE_TOOLS`. `submit_for_review` on a
working coder waits for it to stop (up to `coder_wait_max_s`) before the gate runs, and hands back
a question or steer instead of gating. `_ChainDriver._open` wires the lead's
`RunControl.steer_event(agent_key)` into each link's session.

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
- `ReviewerAgent.review(request, *, stage, literature=None, literature_receipts=None,
  report=None, iterations_left=0, render=None) -> ReviewerReport` — one review as ONE ReAct loop
  whose only exit is `turn_end` (`require_terminal=True`, §2b). The reviewer writes its report with
  `edit_report` (surface `REVIEWER_REPORT_FIELDS` over `ReviewerReport`, description
  `REVIEWER_EDIT_DESCRIPTION`), reads papers with the shared `paper_tools.PaperReader` under source
  `rev` (via `extra_tools`: the PDF itself attached, quotes checked against its text layer, one
  handle per paper across roles; `open_paper` is not declared for it), sees its draft typeset through
  `review_report` (`draft_view`), and `turn_end` runs `reviewer_turn_problems` — including render
  + compile of a final analysis review through `render` (a `ReviewerRender`). Its scope is a
  `ScopedLookup` over the run's store: its own `rev:` receipts, the request (`input:hN`, minted via
  `store.mint_input`), the librarian's receipts, every handle the lead's report cites and the
  lead's figures (matched to receipts by hash), with their ancestors. Requires `tools`, `receipts`
  and `report_dir` (the runtime sets all three, `graph/runtime.attach_reviewer_runtime`); raises
  if the loop ends without an accepted `turn_end`. One JSONL per review under `report_dir`
  (`review{n}_{stage}.jsonl`).
- `LibrarianIOAgent(*, sources=None, storage=None, model=None, ...)`. `sources` defaults to
  arXiv, NASA ADS and Tavily web; `storage` to `Storage()`. Attributes the runtime sets:
  `report_dir` (its working folder; per-pass `literature_pass{n}.jsonl`), `agent_key`, and
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

Reviewer report surface and check (`reviewer_tools.py`, 2026-09-24; wired into
`ReviewerAgent.review`):
- `REVIEWER_REPORT_FIELDS`, `REVIEWER_STATUS_VALUES` (empty: the verdict fields are the decision),
  `REVIEWER_SOURCE = "rev"` (the reviewer's own receipts: papers it opens, reports it renders).
- `reviewer_turn_problems(report, *, lookup, scope, stage, iterations_left, render=None) ->
  list[str]` — the reviewer's `turn_end` door. Refuses: no report; empty `rationale`; empty
  `follow_up` on a dispatch or rework; a switch set at the stage that ignores it; a rework with no
  iterations left (cap named); a finalizing report missing title/abstract/sections/key_findings;
  any number no receipt grounds, anywhere, `follow_up` included (`check_report_fields(itemised=(),
  scope=…)`, scope = every readable receipt); then, for a final report only,
  whatever `render` returns.
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

- 2026-09-15 — **`check_coder` is rate-limited, and the four strings that made the lead poll it
  are corrected.** Measured across three runs of the same `gpt-5.6-terra` seat: **116 / 138 / 122**
  polls per authoring window at a **3.6-5.6 s median cadence**, of which **96.6 / 98.6 / 99.2 per
  cent** returned a byte-identical 159-character "still working" string. In run `328347c4` that
  was **$3.73 — 59% of the lead's entire spend and 13% of the whole run** — to learn four things,
  one per coder turn-end. Each poll re-read a growing trajectory to emit one tool call, so the
  per-poll price rose **41%** as cache-read grew 96k → 180k tokens. A 60 s cadence would have
  covered the same window in 17 polls for about $0.44.
  **It was compliance, not pathology**, and that is why the fix is four strings plus a limit
  rather than a prompt scolding. `check_coder`'s description claimed *"Free: calling it costs no
  tool budget"* — **false**: the tool is absent from `_CYCLE_FREE_TOOLS`, so the model was told an
  action was free and charged for it 116 times. Its "working" return said *"do something else and
  check again"*; `drive_coder`'s acknowledgement ended *"Do not wait idly."*; and
  `submit_for_review`'s refusal said *"Call check_coder to see where it got to."* Every string in
  the channel pointed one way. The lead obeyed all of them and acted on **every** one of the four
  real narrations within 7 seconds — it was never ignoring information, it had no way to know when
  information would arrive.
  **And there is no wait primitive.** The lead's tool set has no sleep and no timeout, and
  `submit_for_review` is *refused* while the coder works rather than blocking — so a model without
  a clock can only let time pass by emitting a turn, and the only turn that advances what it is
  waiting on is a poll. `_ChainDriver.advance`'s own text had also promised that
  `submit_for_review` *"waits for the coder to stop"*, which it does not; corrected with the rest.
  `check_coder_min_interval_s` (config, default **900 s**) is enforced in `execute_check_coder`.
  A refusal carries the remaining time and is the answer rather than an obstacle. Two states are
  deliberately never throttled: a **finished** drive, because `take_drive_result` consumes the
  narration exactly once and refusing it would strand what the lead is owed; and the **first**
  check of a new session, which is the one poll that reliably carries information.
  Six tests in `tests/test_lead_forms.py`, mutation-verified, including a set-assertion over the
  model-visible strings so a newly added "check again" is caught, and one that pins the
  description against `_CYCLE_FREE_TOOLS` so the free-budget claim cannot drift back.
  `prompts/analyst_lead.md` gains a paragraph stating that a coder works in minutes, that a
  throttle refusal is the answer, and that having nothing to do until it stops is the expected
  state rather than a problem to solve. Suite 1519 → 1525.

- 2026-09-15 (second) — **No escape path for the lead: `turn_end` is the only allowed exit.**
  Human ruling, after a worker lost a completed 14-quantity reproduction — all fourteen
  answer-key values with signed sigmas, plus the comparison CSV the task asked for — to a
  **58-character** turn eighteen seconds later. `_react` gains `require_terminal`, passed only by
  `AnalystLeadAgent.run_cycle`; when a turn carries no tool calls the loop re-injects
  `_TERMINAL_REQUIRED_NUDGE` instead of ending, and `_SILENT_TURN_CAP` no longer bounds that
  loop.
  **Why the old rule was wrong, measured.** `_continuation_for`'s `len(content) > 40` test read
  a turn as a genuine finish. Over 192 recorded silent turns it **never once discriminated a
  value between 1 and 40** — the short side is exactly the 8 turns with `content_chars == 0`, so
  `== 0` would have produced identical behaviour on every run ever recorded. It was picked, not
  derived: born with `40` in `0a8beb0`, the sole occurrence in the repository, pinned by no test
  (the existing ones use 73- and 75-character strings and pass at any threshold from 1 to 72).
  And it reads `response.content`, which for the Responses vendors excludes reasoning items —
  the lost turn was 58 characters of text over **799 output tokens**, so the docstring's premise
  ("no tool call and nothing produced") was false in both directions.
  **Scope is the lead only, deliberately.** Of eight `_react` callers, four declare a terminal
  tool and only two write their artifact *inside* the loop: the lead and the worker. The
  reviewer and co-lead compose theirs in a **separate call after** the loop — 163 of the 192
  silent turns are theirs and cost nothing — so a blanket rule would nudge them to the cap on
  every one. The worker has the lead's shape and all three measured losses; it is left for a
  separate ruling rather than extended silently.
  **Two deliberate exceptions.** A `refusal` needs a different member and a `transient` needs the
  retry layer, so both still end the loop: re-injecting into either spins to the ceiling
  achieving nothing.
  **The first version of this rule was an infinite loop, and its own test caught it.** The nudge
  path never advanced `iteration`, which was harmless while `_SILENT_TURN_CAP` bounded it at two
  and unbounded the moment that bound was removed — a silent turn consumes no tool budget
  (`consumed_budget` is `any([])`), so nothing else was counting. `iteration += 1` on that path
  is what bounds `require_terminal`, at `react_loop_absolute_cap`.
  Nine tests in `tests/test_lead_forms.py`, three of them **behavioural** — driving `_react`
  with a model that answers in prose — after the first three passed against a mutation that
  disabled the whole override, which is the same test-around-the-thing failure this week has
  produced twice elsewhere. Both halves mutation-verified. Suite 1525 → 1534.

- 2026-09-15 (third) — **The nudge states the channel rule, and the escape cells joined the
  shapes contract.** Human wording: the message a silent turn now receives leads with *why* the
  turn failed rather than *what* was missing — "your actions and conclusions only reach the
  harness as tool calls; a turn without one arrives as nothing, whatever text it carried" — then
  names the three routes out (investigate with a tool, record with `edit_report`/`review_report`,
  finish with `turn_end`). Leading with the channel rule matters because the model's mistake is
  not forgetting a step, it is believing prose counts as an action.
  The adversarial cells were folded into `tests/test_live_turn_end_shapes.py` rather than given
  their own file: that file already drives one attacking agent per shape against the lead's real
  loop, and a second file would have duplicated its harness and its findings reporter. Three new
  entries in `SHAPES` — `escape_by_prose`, `escape_by_claiming_done`, `escape_by_idling` — which
  differ from every prior shape in having **no `problem_key`**, because nothing is filed to
  refuse; what is asserted is that the loop did not end. `tests/test_turn_end_shape_contract.py`
  exempts them from the "state what the refusal must say" rule and still requires a `why` and a
  cell, and it caught the missing cells the moment the shapes landed.
  `escape_by_idling` is **expected to fail** until [A24] is closed: a loop that exhausts its
  budget without filing returns silently, so the cell asserts the ending is *recorded* rather
  than that it never happens (a cap must exist; "no escape" cannot mean "loop forever").
  `prompts/analyst_worker.md` and `prompts/analyst_lead.md` both open their report section with
  the rule now: composing the report and submitting it with `turn_end` is the only way the work
  concludes, nothing is composed on their behalf, and a loop that stops another way is recorded
  as having produced nothing. Suite 1534 passed.

- 2026-09-15 (fourth) — **The grace window, `turn_end` free everywhere, and the no-escape rule
  extended to the worker.** CLAUDE.md §2b says exhausting a cap is not an exit; this is that,
  implemented.
  **Grace** (`react_grace_rounds`, config, default 20). When a loop with a terminal tool spends
  its budget it is not ended — it is told *"You have run out of rounds. Summarise your work
  within N rounds, using edit_report and then turn_end"*, and every tool result inside the window
  carries `[N grace round(s) left …]`. A **refused** `turn_end` costs nothing and leaves the model
  in the loop: `filed_terminal` is set only on `not outcome.is_error`, so one validated call is
  enough and failing inside the window is expected rather than fatal (human clarification). The
  window closing with no validated `turn_end` raises `ReactBudgetExhausted`, which kills the run —
  the alternative being the silent unfiled return that discarded a completed 14-quantity
  reproduction. A loop with no terminal tool has no exit to force, so grace never opens for it.
  **`turn_end` is now in `_CYCLE_FREE_TOOLS`**, i.e. free on every graph loop. It already was for
  the worker and reviewer; the lead was the odd one out, which made its own tool description
  ("Free: calling it costs no tool budget") false for one caller of four — the same false-claim
  shape as `check_coder`'s — and made a refused `turn_end` burn a grace round, charging a role for
  trying to file inside the window it was given to file in.
  **`require_terminal` now covers the worker as well as the lead.** It was scoped to the lead
  only because the ruling named the lead; the shape is the same and **all three measured losses
  were the worker's**. A parametrised guard asserts both call sites, since the parameter defaults
  to False and a loop that needs it but omits it looks identical to one that does not.
  **The live adversarial cell found a defect in the fix, which the deterministic suite had
  passed.** `escape_by_idling` drove with `absolute_cap = cap + 2`: grace opened at the budget
  cap and the ceiling ended the loop two rounds later — unfiled, silent, never reaching the fatal
  raise. A window a bound can retract is not a window, so the ceiling is raised to accommodate
  grace. Pinned with the cell's exact shape and mutation-verified.
  **One over-reach, reverted.** The first attempt put a blanket "no unfiled exit ever" raise after
  the loop, which made `refusal` and `transient` fatal too and broke seven tests — beyond what was
  specified, since re-asking cannot fix either. The narrow ceiling fix replaced it.
  Live: all three escape cells pass (`escape_by_idling` had been failing). Suite 1534 → 1541.

- 2026-09-24 — **The librarian became one deep-research loop filing a `LiteratureReport`** (95's
  session, human-approved design). Before: a 1-turn source loop, raw hits ingested uncurated
  ([I2]), each PDF "debated" by one model against itself (`IODebateToConverge([self, self])`),
  per-source failures swallowed ([I9]), no report and no `turn_end` (a §2b violation). Now:
  `research()` = one `_react` loop over the new `LibrarianToolProvider` — search, read papers and
  pages in full (paged), write the report, `turn_end`; every opened document is a P0 receipt
  minted through `sources.receipts`, and the validator runs the shared binding check. Deleted:
  `compose_search_strategy`, `search`, `gather_literature`, `ingest_document`,
  `abstract_document`, `judge_abstract_convergence`, `adjudicate_abstract`, `ingest_results`,
  `evaluate_search_results`, `_dedup_results`. Source failures are now recorded as
  `librarian.source.error`. Tests: new `tests/test_librarian.py` (26), env-gated
  `tests/test_live_librarian.py`; the old librarian cases in `test_agents.py` and the retired
  pipeline in `test_live_agents.py` removed.
- 2026-09-24 — **`check_coder` is replaced by a blocking `wait_coder`, and `drive_coder` queues
  by default (85).** The 2026-09-15 rate limit did not stop the polling; it moved it. Slot 6
  (`gpt-5.6-terra` lead, $20.43, the costliest role) made 134 `check_coder` calls (114 refused),
  47 mid-turn steers, 31 refused `submit_for_review` calls and 9 refused `turn_end` calls at a
  3-10 s cadence, because a loop with no way to let time pass emits a tool call to do it, and a
  refusal is a paid turn. Researched against the harnesses that solved it: Codex's `wait_agent`
  blocks in the tool call, wakes on child activity or new input, and **clamps a short timeout up**
  to a minimum (`multi_agents_v2/wait.rs`, "Prefer longer waits (minutes) to avoid busy
  polling"); its queued `send_message` vs explicit interrupt split is the model for `mid_turn`.
  Deleted: `CHECK_CODER_TOOL`, `check_coder_tool_def`, `execute_check_coder`,
  `env.check_coder_min_interval_s` and its config key. New: `WAIT_CODER_TOOL`,
  `wait_coder_tool_def`, `execute_wait_coder`, `_wait_before_submit`, `_submit_now` (the old
  gate+review+commit body), `env.coder_wait_limits_s` (floor 60 / default 300 / max 1800 s);
  `drive_coder` gains `mid_turn`; `execute_submit` waits rather than refuses. `_coder_event` now
  points at `wait_coder` and says what to do with a dead session instead of what not to do.
  `analyst_lead.md`'s coder paragraph rewritten to match. Tests: `tests/test_lead_forms.py` (the
  rate-limit block replaced by the wait surface, floor clamp and config ordering),
  `tests/test_run_control.py` (+1, the per-key steer event), session-level cells in
  `tests/test_merge_flow.py`; new env-gated `tests/test_live_coder_wait.py` (five mechanism cells
  on a real coder, two real-lead cells). Out-of-region touches, agreed with 0f: the `react.py`
  env import, the `roles.py` import block and `_CYCLE_FREE_TOOLS`.
- 2026-09-24 — `reviewer_tools.py` added (5c): the reviewer's report surface and `turn_end` check,
  including render + compile of the final report inside the check (§2b: the artifact is validated
  at the role's edge). A number in `follow_up` must be grounded too (human ruling: a request for
  more work is a report). Wiring into `ReviewerAgent.review` waits on 0f's generic `edit_report`.

- 2026-09-24 — **Provenance P1/P2: every tool call mints receipts, and every unit's report is
  checked against them (session 0f).** Generalises the figure discipline to every artifact
  (`auto_research.provenance`, `schemas/artifacts.py`). **Minting:** `SandboxToolProvider.execute`
  hashes the files a call's arguments name *before* it runs (data roots and figure-output args
  skipped), then `provenance.capture.receipt_call` mints a `result` receipt over the tool's own
  output text, a `figure` receipt per returned figure **with the hash recomputed from the file**
  (a tool-declared hash that disagrees is overridden and noted under `[provenance]`; a figure whose
  file is absent is dropped), and a `data` receipt per file the call created or changed. The text
  the model sees opens with `[receipt <handle>] · [<handle>] <product>`. `ReadOnlyToolProvider`
  receipts its reads (`read_file`, `list_dir`, `text_search`, `data_catalog`, `web_search`,
  `ask_human`, `plan_and_debate`; errors are not receipted); `web_fetch` on both providers mints a
  `web` receipt through the librarian's `sources.receipts.mint_web_receipt`, so every role's pages
  carry the same fields. **Checking:** `report_provenance_problems(workspace, report)` is the
  shared half of `worker_report_problems` and `lead_turn_problems`: it stats every cited figure
  (the worker now gets the stat the lead had) and runs `check_report_fields` over the claim fields
  (`summary`, `findings`, `uncertainty`, `rationale`) through the workspace's `receipts` lookup.
  `execute_edit_report` runs the same check and reports its problems as notes while the draft is
  written; `turn_end` refuses on them. **Surface:** `ReportWorkspace` gains `receipts`,
  `receipt_source` (what `self:` means), `itemised`, and `provenance_check` (a role with nested
  prose supplies its own check, which replaces the default field walk in both places);
  `edit_report_tool_def(..., report_model=)` derives any property it does not hand-write from the
  model's own `Field(description=…)`, so the reviewer and librarian reuse it without a fork; both
  providers take `extra_tools: [(definition, executor)]`, dispatched first; `review_report` lists
  the citable receipts. `edit_report`'s description and the `summary`/`findings` properties tell
  the model how to cite. **Roles:** `AnalystWorkerAgent.receipts` / `AnalystLeadAgent.receipts`
  (set by runtime); each round's workspace gets a `ScopedLookup` — own receipts plus those the
  received reports cite (peer reports for a worker, worker reports for the lead) and their
  ancestors (`roles._receipt_scope`). `test_turn_end.py`'s complete-worker fixture now uses a real
  figure file; `test_turn_end_shape_contract.py` counts `report_provenance_problems`' clauses with
  the lead's (the number-grounding family is owed live shapes in P7). Also: the open-session
  refusal in `lead_turn_problems` now points the lead at `wait_coder` (85's request).
- 2026-09-24 (second) — **The report tools serve a report that is not the analysts' (for the
  reviewer session).** `execute_edit_report` branches on `issubclass(report_model, FiguredReport)`;
  a plain model (`_edit_plain_report`) takes exactly its surface fields, no figure placement.
  `edit_report_tool_def(..., description=)` and, for a plain model, every property and the
  `required` list come from the model itself. `ReportWorkspace.draft_view` replaces the own-draft
  view in `review_report`; `ReportWorkspace.edit_description` overrides the description.
  `ReadOnlyToolProvider(include_open_paper=False)` drops the native-PDF `open_paper` while keeping
  `text_search` (the reviewer reads receipted Docling text instead, human ruling). The shared figure
  stat skips figure *handles* (no `path`). `tests/test_provenance_wiring.py` (6) drives the real
  `SandboxToolProvider` → receipts → `edit_report` → `turn_end` path, including a tool-declared
  figure hash overridden by the file's, a missing figure dropped, a written file becoming a data
  receipt that the next call's input links as its parent, and the scoped views of worker and lead.
- 2026-09-24 (third) — **Provenance P7: the live turn_end shapes attack the binding (written, NOT
  YET RUN).** `tests/test_live_turn_end_shapes.py` wires a receipt store the way
  `attach_lead_runtime` does and gives the workers' reports real receipts; five new refusing
  shapes, one per clause of `provenance.check_bindings` — `ungrounded_number`, `unknown_handle`,
  `misquoted_span`, `quote_without_text`, `flagged_quote` — each seeded deterministically and told
  to attack. `test_turn_end_shape_contract.py` now counts `check_bindings`' clauses, so the
  exhaustiveness claim covers them (13 = 13). `LeadCycleOutcome.verdict` deleted (read nowhere;
  the reviewer's session retires `ReviewVerdict`).
- 2026-09-24 (fourth) — **Workers and the lead receipt the task they were handed**
  (`roles._mint_task_input`, on entry to `analyze` / `run_cycle`), so a number their task states is
  citable as `[input:…]` (see `provenance/DOCS.md`).
- 2026-09-24 (second) — **`ReviewerAgent` rewritten as one loop over one report (5c; step 4).**
  `compose_machine_report` and `author_latex_report` deleted; `review` no longer composes a verdict
  after its loop (that loop's `turn_end` validated nothing, `lambda: []`). It now runs on 0f's
  generic `edit_report` (plain `report_model`, `edit_description`, `draft_view`,
  `provenance_check`) and `ReadOnlyToolProvider(include_open_paper=False)`; `reviewer_tools` gains
  `ReviewerPaperReader`, `reviewer_draft_view` and `reviewer_scope`. §2b's recorded violation for
  the reviewer is closed; the co-lead keeps its compose (coding pipeline, out of scope).
- 2026-09-24 — Librarian tool + prompt tuned from the first live run (95): `search_literature`
  uses `search_with_match` and prefixes a fallback note when no paper matched all terms; its query
  description names 3–6 terms and each source's field syntax; the prompt tells the librarian to
  find and read the original paper behind a cited result, and to page past the first window into
  results, tables and discussion (the live run read only page 1 of all 7 papers). The live test now
  attaches its cost meter to the monitor, so every ledger line carries the running cost.
- 2026-09-24 — **The librarian reads PDFs natively** (95, human ruling: provider-agnostic, no
  Docling, no vendor citation feature). `read_paper` attaches the downloaded PDF instead of paging a
  Docling text export; the paper receipt cites the PDF (`source_path`, re-hashed by `verify()`) and
  checks quotes against its pypdf text layer, and a refused quote now shows the closest passage
  (0f's `check_bindings`). Paging and the `offset`/`handle` arguments are gone from `read_paper`.
- 2026-09-24 (fifth) — **P7 live results** (`tests/test_live_turn_end_shapes.py`). Four provenance
  shapes refused with the right reason and the lead recovered; `flagged_quote` was never reached
  (the lead declined the bait twice), so that cell now judges the three outcomes the way
  `rework_without_probe` does: refused-and-recovered, declined, or the failed value in an accepted
  report (the finding). `coder_still_running`'s seed closes the session when the lead waits or
  submits (or after three refusals), since a session it could never close had hung the cell on
  2026-09-15 and again today. Not metered: the shapes harness installs no cost meter.
- 2026-09-24 — `agents/paper_tools.py`: the shared `read_paper` (95, from 0f's human's ruling that
  the reviewer uses the librarian's paper path), generalised from 5c's interim reviewer reader. The
  librarian's `read_paper` now adds only `hit=N` on top of it. Tests: `tests/test_paper_tools.py` (5),
  including one paper opened by two sources yielding one receipt.
- 2026-09-24 (third) — The reviewer reads papers through 95's shared `paper_tools.PaperReader`
  (human ruling: the librarian's path, native PDF). `ReviewerPaperReader` (it paged a Docling, then
  pypdf, text copy) deleted.
- 2026-09-24 — Live shape matrix for the librarian (95, human: "the live test asserts every possible
  path"): `tests/test_live_librarian_shapes.py` (18 cells: every reachable `turn_end` refusal, the
  three escapes, both legal outcomes, and the no-PDF / no-text-layer / failing-source paths) on local
  real PDFs, plus the free `tests/test_librarian_shape_contract.py`, which binds every validator
  clause to a cell or a stated unreachability. First run: 17/18, the one failure a wording
  expectation in the test (fixed); $1.78. The librarian's citations are now scoped to what it opened
  (`ScopedLookup`); identifiers render with their prefix (`paper_tools.format_ids`: `arXiv:…`,
  `doi:…`) so the number check reads them as names; a PDF outside the knowledge base takes its
  identifier from the downloader's `<source>_<id>` file name (`ids_from_stem`, from 5c's live run).
- 2026-09-24 — Option A on the librarian (95, human ruling): papers cited by handle, never
  quoted; the librarian's check uses 0f's sentence-scoped `cited_only_kinds` and `support=`; the
  prompt says to cite papers beside each claim (value exactly as printed, and where) and to quote
  web pages. Live shapes: `quoted_paper` added (the clause is now reachable), `misquoted_span` /
  `ungrounded_number` moved onto a web page, `scanned_pdf` replaces the no-text-layer cell.
- 2026-09-24 — Per-number attribution replaces the paper exemption (95, human ruling: "one paper
  citation must never exempt other numbers"). Found by the human: the sentence/claim-scoped
  exemption let any number sit unchecked beside a paper citation. Checker side is 0f's
  (`bindings.py`: required locator, `paper_attributions`, `cited_only_kinds` removed); librarian side
  here: `attributed_values` collected at filing and persisted in the pass JSONL; live shapes
  `attribution_without_place` and `unattributed_paper_value` replace `quoted_paper`.
- 2026-09-24 (sixth) — **Live shape `attribution_without_locator`** (not yet run): a worker report
  cites a real `%PDF-` paper receipt and the lead is told to attribute a value to it with no
  locator; binds the attribution clause `provenance.check_bindings` gained under the "papers are
  attributed, never text-checked" ruling, so the shape contract stays exhaustive (14 = 14).
- 2026-09-24 (fourth) — Fixed, found by the live path matrix: with `tex` not configured the reviewer's
  `review_report` fell back to the analysts' figure-record view, which read `.caption` off a handle
  string and raised out of the loop. `reviewer_text_view` is now its draft view there. The prompt
  now asks for paper values as attributions with a place (option A).
- 2026-09-24 (seventh) — **`attribution_without_locator` LIVE-VERIFIED**: the lead's unlocated
  attribution was refused with "without saying where", and it recovered and filed.
