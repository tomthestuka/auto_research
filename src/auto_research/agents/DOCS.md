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
`decide_authoring`) bounds *total* iterations — the runaway-free-tool backstop; when unset it
defaults to `cap`. **Both caps are caps, not exits (§2b, ruling D4 2026-10-18):** a loop with a
terminal tool gets `react_grace_rounds` (default 20) at the budget cap, counted in budget-spending
calls, and the same number at the absolute ceiling, counted in model turns (calls there are
mostly free, such as a refused `turn_end`). It is told once, and a countdown on each result shows
whichever open window closes first. Filing inside either window is accepted; the end of either
raises `ReactBudgetExhausted` (`grace_exhausted`, `at: budget|ceiling`). With grace configured to
0 a terminal loop raises at the cap. Only a loop with no terminal tool ends quietly at a cap. `_react` returns
`(messages, figures, documents)`: a document a tool attaches (via `ToolOutcome.documents`,
e.g. a PDF from `open_paper`) **rides the tool result that opened it** (`ToolResultPart.documents`,
R2), once: sent at its place in the conversation it falls inside the cached prefix as the loop
grows, instead of being re-attached at the end of every call. One the conversation already
carries is not attached again (`PaperReader` returns the document every time, so a paper reopened
after a segment boundary comes back); the returned list holds every document the turn opened. A tool's **figures** are set on the `ToolResultPart` at their turn
(`ToolResultPart.figures = outcome.figures`), so the adapter renders them **inline in history**
at that turn — the model keeps seeing its own plots every subsequent round and in the terminal
report call, cache-stable — rather than only on iteration 0 via `images=`. (The returned
`figures` list still accumulates them so `analyze()` can staple real `FigureRef`s onto
`AnalystReport.figures`, which structured output cannot reproduce.) A terminal `_call_over_messages(...)`
then produces the role's structured output grounded in the trajectory (both reuse
`_structured_loop`, the shared off-schema-retry core). Tool surfaces live behind
the `ToolProvider` protocol in `react.py`: `SandboxToolProvider` (analyst science
tools in the `PersistentSandbox`; every call's arguments are the model's completed by the runtime —
the `bash_exec` folder and shell environment, a configured mission folder, figure outputs under
the worker's `figures/` — through `provenance.calls.resolve_call_args` over the call's context
(`_call_context`), returned as `ToolOutcome.call_context` and recorded on the `tool_result` ledger
line, so the ledger rebuilds the arguments the call ran with; when `orchestration.yaml: tool_search_enabled` is set it marks
every *registry* tool `defer_loading: true` so its schema loads on demand via the provider's
tool search, while the harness tools it appends stay resident — see `models/DOCS.md`.
Live-verified 2026-07-28 and enabled), `ReadOnlyToolProvider` (reviewer/lead/co-lead — in-process
`read_file`/`list_dir` confined to its `roots` (the permitted roots are named in each tool's
description and echoed in the `path not permitted` error so the model self-corrects instead of
guessing). `read_file` — the same one every role has, workers and the status monitor included —
reads **by line range** (`offset`/`limit`, default `_DEFAULT_LINE_LIMIT`=1000 lines and at most
`_MAX_WINDOW_CHARS`=100,000 characters, each line capped to `_MAX_LINE_CHARS`=2000 with a marked
cut) via `execute_read_file` / `_read_lines`, returning
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

**Inter-LLM report authoring — `write_report` / `edit_report` (V-4, A7, B18).** Every analyst
report (worker→lead, lead→reviewer) is a per-round JSONL the agent ReAct-refines **through tools**,
so analysts keep their read-only discipline (the runtime writes; the agent never touches disk).
Both providers grow the pair when a `ReportWorkspace` is attached (`report_path` + `report_model` +
an append-only per-source figure `catalog`): the draft document's `# figures` lines (`{source,
index}` handles) are resolved against the catalog → the ordered `figures`, and the JSONL is
persisted. **`review_report` was erased (human ruling B18, 2026-10-18)**: an edit needs no full
re-read — each write/edit reply shows the changed lines and an outline, and a refused call shows
the draft whole, text only (in B9 live a worker called it 17 times, each attaching the whole
24-figure catalog, ~43k image tokens, $4.40 in one turn). What it alone provided now appears where
the thing first does, text only: a worker's tool result names every figure it produced with its
citable label (`[self:k] caption`, the sink's index, stable across calls); a worker's task lists
the labels of earlier rounds' figures (`your_figures`); the lead's task carries every worker report
whole (`worker_reports`) and every placeable figure by label, caption and receipt handle
(`figure_catalog`, merged with the image budget's not-attached list so a figure appears once; the
attached images captioned by label; `_receipt_scope` admits those figure receipts); receipts are named on every tool result; the librarian's refusals list the documents it
opened in the pass. Figures are cited, never attached directly: the resolver
(`resolve_figure_citations`) refuses an out-of-range / unknown-source handle and keeps a
doubly-cited figure once — **citation is the de-duplication**.
The worker's catalog is one source `"self"` (its produced figures, appended live during the loop
via `figure_sink`); the lead's is one source **per worker** (its cited figures), both append-only
across debate rounds *and* rework iterations (the agent instances persist), which is what closes
the "last-round-only figures" leak. Round files are `<role>_round<n>_report.jsonl`
(`sandbox.workspace.round_report_path`; `n` = the monotonic debate round). Both tools are
budget-free in the loop (report authoring, not evidence gathering); a loop that ends without an
`edit_report` write files nothing (`turn_end` refuses; nothing is composed on the model's behalf).

**The report is one text document, written like a file (A5/A7, human rulings 2026-10-18).** "Make
edit_report really an edit", then "copy Claude Code / Codex's design": the draft is one text
document with a `# <block>` header line per report field (`agents/report_edit.py`: the block tables
`ANALYST_BLOCKS` / `REVIEWER_BLOCKS` / `LIBRARIAN_BLOCKS`, `render`, `parse`). Two tools, the same for
every role: **`write_report{content}`** replaces the whole draft (Write); **`edit_report{old_string,
new_string, replace_all}`** replaces an exact match (Edit: `old_string` occurs once unless
`replace_all`). A call whose `old_string` is missing or ambiguous, whose result does not parse
(a duplicate header, text before the first header, a list line before its first `- ` item, a
figure line that is not a handle, a two-line `# status`, an unknown `tool_request` key) or does not
validate, or (analysts) that places a figure the catalog does not hold, is refused with the reason
and nothing changes. The reply is the changed lines with three lines of context, numbered, plus a
one-line outline (`report_edit.snippet`, `outline`); a refused call shows the whole document
(`report_document`), text only (B18; A6: a round's draft starts empty). The stored report stays the Pydantic model, so `turn_end`,
provenance and the graph read it unchanged; a field left at its model default renders as an empty
block (`AnalystReport.summary` defaults to "" for that reason; `turn_end` refuses an empty summary).
Layout: a list item starts with `- ` and its further lines are indented two spaces; figures are
`- [source:index]` lines (the reviewer's `- handle`); the librarian's finding carries an indented
`support: lib:p1, lib:w2` line; the worker's `# tool_request` is `key: value` lines plus one
`validation_data: MISSION OBSID [synthetic]` line per dataset; the reviewer's sections and tables
start with `## `. Each tool's description lists the role's blocks with one line of meaning each
(`report_edit.layout`), then the role's own text (`_ANALYST_EVIDENCE` and the figure rule for the
analysts — `self` for a worker, worker keys for the lead — or the workspace's `edit_description`).

**The report form and its diagnostics (A1–A4).** One definition, stated in the role prompts, the
schema field descriptions and the block hints: `summary` a 3–5-sentence briefing; `findings` the
results, one item per result citing its receipts; `figures` the placed figures; `status` the
outcome. Notes on a saved draft: findings empty where `turn_end` will require them (a lead's
`rework` false, any other `succeeded`), and — ruling A2 — a **warning whenever the summary is
longer than the findings combined** (`report_shape_notes`), for a report that carries results.
`turn_end` shows the same warning (`report_warnings`, `execute_turn_end(..., warnings=)`) after a
refusal's fix line, or after "Turn ended." as "Your report is filed as it stands. Note for your
next report: …". Parameter markup in a text field is saved as text (ruling A1, "no special case"):
residual, a draft whose fields all sit in `summary` reads as an honest `failed` and is filed unless
another check refuses. `turn_end` refusals name the block and the fix: a worker's `succeeded`
report with no findings is refused (A4; a blank item is no finding, `_filled`); the lead's
no-findings refusal offers `rework` only while a round remains; the figure refusal names
`# figures` with an example line. The librarian's handle refusal says the document must be opened
in this pass (`captures` is per pass) with `read_paper` or `web_fetch`.

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
| Librarian I/O | `LibrarianToolProvider`: arXiv/ADS/web search, full-text reading of papers and pages (each minted as a receipt), local text search, `write_report`/`edit_report`/`turn_end`. | One research pass: a `LiteratureReport` whose findings cite, by handle, the documents it read. |
| Local retrieval | Pure-Python `text_search_paths()` over SQLite. | No runtime LLM role; returns local paper paths for reviewer attachments. |
| Analyst worker | `SandboxToolProvider` over `research_toolkit` manifest tools plus MCP tools in a persistent sandbox, plus `web_search` + `web_fetch`. | Runs the full analysis tool chain inside `analyze()`, then reports grounded results and figures. |
| Analyst lead | `ReadOnlyToolProvider` over run artifacts, papers, and `research_toolkit`; `web_search` + `web_fetch`; the five **cycle forms** + the three authoring tools when a `LeadCycle` is wired. | One cycle: investigate, triage the tool request, drive the chain, judge, and file the form that carries the decision. Also answers author questions. |
| Analyst co-lead | `ReadOnlyToolProvider` over the **toolkit worktree** (`include_text_search=False`) + `web_search` + `web_fetch`. | Provider-decorrelated **code review** of one authored tool: brief-match, Layer-B check adequacy, no-fabrication second pass, subtle bugs (§9.3 step 4). |
| Reviewer | `ReadOnlyToolProvider` over run artifacts, figures, papers, memory, and `research_toolkit` (to read tool source/`TOOL_MANIFEST` and confirm correct call parameters), plus `web_search` + `web_fetch`. | Dispatches, reviews, requests capped rework, and composes final report handles. |

**Session history lives in the run ledger (`agents/history.py`, B1, 2026-10-18).** Every ledger
event that adds to a conversation carries `data["history"]` (`HISTORY_KEY`), a part naming what
it adds: `turn_start` (a fresh turn id, an optional `base` — an earlier turn of the same actor and
the view applied to it, with the count of seed messages it supplies — the seed messages after the
base as `seed_json` (figures ride their messages), the task's receipt handle: worker, lead,
reviewer),
`user` (an appended user message: steer, inbox text, grace notice, the silent-turn nudge recorded
as `*.nudge`), `model` (one returned turn whole on `*.model_turn`, level 9, as `response_json`:
prose, thinking, tool calls with their provider fields — `bytes` tagged as base64 — plus
`in_history` and the declared tool set: `tools_sha256` on every call, `tools_json` whenever it
changed within the turn), `tool_result` (on the existing `*.tool_result`
event: the figures with their receipt handle and full reference, the documents, the call's
`result_receipt`), `results_end` (on `*.tools`: the batch closes into one tool turn; `suffix` is
the grace countdown) and `rewrite` (on `analyst.tool.compacted`: the lead's link cut, replayed
with `_compact_coding_span`; on `*.compaction`: a segment boundary, replayed with
`compaction.apply_compaction`). Messages and turns are JSON strings inside
the part because the ledger sorts each line's keys, and a resend must keep a tool call's argument
order. The turn id is fixed before the loop (`_react`, `resume`, and `run_cycle`, which hands it to
its `_ChainDriver`) and every part names it, so a background resume beside a new turn of the
same seat never interleaves; a resume that ends unfiled still becomes the base of the next.
`history.rebuild` / `turns_for` /
`conversation` read an actor's turns back from the JSONL file (or a chatter-10 monitor's memory)
and give, per turn, the conversation as it ended and every model call's input (messages,
documents); a part naming an unknown turn, a base the ledger
lacks, a base of the wrong length, a results batch that does not answer the model turn before it,
or an in-memory monitor below chatter 10 raises `HistoryError`. A resumed turn records its kept turn as
its `base` (verbatim) and nothing again. Tests: `tests/test_history_ledger.py` (the rebuilt input
of every call equals what a recording model was sent, for a worker turn with steer, nudge, grace,
figures and documents, a resume, an unfiled resume then another, two loops of one seat at once,
and the lead's compaction; the recording model keeps deep copies of the objects it was sent and
the key order of every argument and provider part).

**Every graph seat is one session across its turns (B2, human ruling 2026-10-18).** The worker's
`analyze`, the lead's `run_cycle`, the reviewer's `review` and the librarian's `research` open
with `AgentRole._history_seed()`, then that turn's own task. **Within a turn a conversation only
grows at its end** (ruling B6): no client-side edit of earlier content mid-turn, the system prompt
and tool list fixed. **At a turn boundary** the earlier turns become ONE user message (ruling
B6.2, the provider docs' "simple compaction" pattern): `history.transcript` of the seat's latest
turn rebuilt from the run ledger (this run's rows only) — what it was told, what it wrote and
called, each tool output as its index line (B3) — carrying the newest figure sets within the
image budget (B4). No earlier assistant turn is replayed, so no earlier thinking, reasoning item
or tool-use block is sent again on any provider, and the tool list may change between turns. A
transcript longer than half the compaction trigger is condensed by `session_compactor` and
followed by the pin of its receipts (`_condensed_transcript`). The seed is
`[system, transcript, task]`; the new turn records `base` (`view: "transcript"`, count 0), its
`seed_json` holding the whole seed. No ledger (no monitor, or a monitor without a JSONL below
chatter 10: `*.history.unavailable`) or a ledger that does not rebuild (`*.history.unreadable`,
level 1) means a fresh seed. A resume continues its kept turn verbatim (the same turn); a fresh
turn that ends unfiled drops the kept turn. The reviewer's continuing session is handed only this
turn's report (ruling B7). memory.md is retired: nothing writes or reads it, and a file planted in
an agent's folder never reaches a prompt. The co-lead, the status monitor and the benchmark's
`drive_authoring` stay single-turn.

**Earlier tool outputs are indexed (B3).** `history.index_outputs` / `_index_part` shrink an
earlier tool output to one line — `[earlier output] <tool> returned; arguments <first 200 chars>;
<N> characters. view_receipt("<handle>") shows it in full.` — naming its receipt
(`ToolResultPart.receipt`, set by the loop and by the rebuild from each `tool_result` part's
`result_receipt`; on the part because call ids repeat on some paths, the arguments taken from the
turn the results answer) and its figures by handle (the seat's own receipt first); an output with
no receipt (web pages, librarian and paper reads, commit results, report and channel tools) keeps
its first `INDEX_HEAD_CHARS` (1,500) characters; one of at most `INDEX_KEEP_CHARS` (300) is kept.
An output kept whole names its receipt (`… [handle]`), so a value in it stays citable; a result
that attached a document names it ("It attached …"), the document itself leaving the view.
`view_receipt` takes `offset`/`limit`: a text receipt longer than `_VIEW_TEXT_CAP` (20,000
characters), or any read passing either, comes back as numbered windows of at most 20,000
characters naming the next offset; a line over 2,000 characters is split into rows so every
character is reachable, and an offset past the end says so. Measured in
`tests/test_session_history.py`: a 90k-character output becomes one line, the transcript is more
than ten times smaller than the verbatim turn, and the output reads back whole by handle.

**The image budget (B4 at the turn boundary, `agents/images.py`).** Within a turn every figure
rides every later call, unchanged (ruling B6): the turn's first-call figures (`images=`) are
attached to its task message (`ModelMessage.figures`), so they stay at the same place on every call
instead of riding the first call only, and every image is fitted to the provider's strict edge
(2000 px on Anthropic) from its first send. At a boundary `images.keep_newest` picks the figure
sets the transcript carries: all while they fit `IMAGE_BUDGET_TOKENS` (40,000), else the newest
whole sets within `IMAGE_LOW_WATER_TOKENS` (20,000; rulings B3 — no set exempt — and B8), and the
same by count (`IMAGE_BUDGET_COUNT` 40, down to `IMAGE_LOW_WATER_COUNT` 20); each image
costed at `⌈w/28⌉·⌈h/28⌉` after the provider's downscale, at most `MAX_IMAGE_TOKENS` (4784). The
rest are named by handle in the index lines; a task message's figures are named by their receipts
too. Documents (PDFs) are outside this budget; the adapters' byte and page ceilings bound them.
**A turn's task attaches each figure once per session (R7).** The worker's peer figures and the
lead's accumulated figure catalog pass through `AgentRole._task_figures`: deduped by hash and, in a
continued session, stripped of every figure the seat's ledger turns already carried
(`_session_figures`, set by `_history_seed`); the set is then bounded like a boundary's (all within
40k image tokens, else the newest within 20k, one set per figure), and every figure left out is
named by receipt handle in the payload (`peer_figures_not_attached` / `figures_not_attached`).

**Bounds within a long turn (B6, B13, B14; `agents/compaction.py`).** Provider-native first,
ours as the fallback, chosen per member (ruling B13 as amended). An **Anthropic** member compacts on
the provider's side (`compact_20260112` at `compaction_trigger_fraction` 0.9 of its input limit,
`models/DOCS.md`): the loop replays the `compaction` block with its turn, records `*.compaction`
(its token cost; a null summary is `*.compaction_failed`), and queues the pin, which restates the
turn's task (the provider's summary replaced it). An **OpenAI** member compacts on its side too
(`context_management` with `compact_threshold`; probed live 5/5 on tool-call-only turns). Every
**other** provider (DeepSeek, Z.AI, Alibaba, Google) gets a **segment boundary**
(`_segment_boundary`, B13 (b)): after a complete tool round, once the last call's prompt reached
0.9 of the limit — or, on **every** provider, once the conversation's media near the provider's
documented ceilings (R3, `_media_trigger`: image blocks ≥ `MEDIA_IMAGES_FRACTION` 0.8 of
`max_images`, media bytes as base64 ≥ `MEDIA_BYTES_FRACTION` 0.7 of `max_request_bytes`, from
`models.providers.media_limits`), before the media guard would withhold earlier images — the
`session_compactor` role summarises the segment and the conversation becomes
`[system, task, summary, pin]` — no earlier assistant turn, so no earlier thinking — recorded as a
`rewrite` part (`op: "compact"`) the rebuild replays; a summariser that fails or returns nothing
leaves the turn whole (`*.compaction_failed`) and waits four calls. Never while the lead has a
coding link open. **The lead's coding links are segment bounds** (B14 (c)): a link closes inside
the tool call that ends it, and at the next complete tool round `_ChainDriver.apply_cut` keeps the
conversation up to the opening call word for word, rewrites its answer to the link's outcome (the
summary plus what the closing call answered) and removes the link — coder transcript and closing
turn — from the end; `close()` applies the last one after the loop. `pin_text` (code, not the
summariser) lists the task's receipt and the receipts the turn produced, each marked evidence or
a record of where the agent looked; a condensed turn-boundary seed pins every receipt the earlier
turns hold (R10, read from the rebuilt turns: each output's receipt, its figures', the papers its
tools opened, the figures handed in — never a handle merely quoted in a text). A paper a tool opens
is pinned by its receipt (`_paper_handle`). The latest round's figures and documents ride the
message that restates it (`ModelMessage.documents`), so a paper the model has not read yet stays;
documents of the condensed middle leave with it, the pin names them, and the agent reopens one to
read it again. The media trigger fires only when the cut can bring the media back under the line
(what it keeps — the task and the latest round — is below it), and skips the four-call wait unless
the last summary failed; the token estimate counts an opened PDF at 3,000 tokens a page. A summary is a message, never a receipt, so it grounds
nothing. Safety net (ruling B6.5): a provider 400 "bound to a different conversation" makes the
Anthropic adapter strip thinking from the named message to the end and retry once
(`*.thinking_dropped`).

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
  handle per paper across roles; `open_paper` is not declared for it), and `turn_end` runs `reviewer_turn_problems` — including render +
  compile of a report to the human through `render` (a `ReviewerRender`). The payload names what
  `arrived`, the budgets (`_review_instruction`), and — on its first turn, or with no ledger to continue
  from — its own earlier reports (`_earlier_report`), every literature report so far and the
  latest analysis report; a continuing session (B2) is handed only the report that arrived this
  turn (ruling B7), the history carrying the rest; the human's nudges (reply text,
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
- `AnalystWorkerAgent(*, sandbox=None, registry=None, ...)`.
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
- `ReviewerRender(report_dir, run_id, formats=("tex",), ledger=None)` — callable used as
  `render(report, lookup, scope)`: each listed format runs its own path (`tex`: render + compile;
  `html`: `report.html.render_html`, its numbers' sources from `check_report_fields(...,
  sources=True)` over the validator's own fields and scope, arguments rebuilt from `ledger`);
  returns model-fixable problems and keeps the last `render`/`compile`/`html` result,
  `renderer_errors` (all formats) and `html_errors` for the graph.
- `reviewer_scope(lookup) -> list[str]` — every readable handle except inputs: the request and
  dispatched tasks ground a text only when it cites `[input:hN]` (human 2026-09-24).
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
  worker_index=0, ...)` —
  analyst worker wrapper.
  - `analyze(task, *, peer_reports=())` → `AnalystReport`. Opens with its earlier turns
    rebuilt from the ledger (`AgentRole._history_seed`, indexed), then the task and peer
    reports; runs the inner ReAct loop over `SandboxToolProvider` when `sandbox` + `registry`
    are attached and returns the `AnalystReport` it filed. With no sandbox/registry it falls
    back to a plain structured call.
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

- `agents/history.py` (B1/B2): `HISTORY_KEY`; `HistoryError`; `new_turn_id(actor)`; `dumps`;
  `encode_message` / `decode_message`, `encode_response` / `decode_response`; `figure_entries(figures,
  store, prefer=)` / `figures_from`, `document_entries`; `ledger_events(source, actor, run_id=)`;
  `rebuild(events) -> {turn_id: Turn}`; `turns_for`, `latest_turn`, `conversation`; `apply_view`
  (`verbatim`, `indexed`), `index_outputs`, `transcript(messages, figure_handles)`,
  `turn_figure_sets`, `strip_thinking`; constants
  `INDEX_MARK`, `INDEX_KEEP_CHARS`, `INDEX_HEAD_CHARS`, `INDEX_ARGS_CHARS`, `TRANSCRIPT_MARK`,
  `CONDENSED_NOTE`; `Turn` (`id`, `actor`, `task_receipt`, `messages`, `calls`,
  `documents`, `handles`) and `ModelCall` (`messages`, `images`, `documents`, `tools`).
  `AgentRole._react_loop(..., history_base=None, task_receipt=None, turn_id=None)` records the
  parts; `_history_seed` (async), `_condensed_transcript`, `_segment_boundary`,
  `_task_figures(figures, *, continued) -> (attached, named)`,
  `_media_trigger(member, messages, kept)`, `_document_tokens(document)`, `_paper_handle(document)`,
  `_grounds`,
  `_history_turn_start`, `_history_part`, `_history_model`, `_history_store`, `_result_receipt`.
  `_mint_task_input` returns the task receipt's handle.
- `agents/images.py` (B4): `IMAGE_BUDGET_TOKENS`, `IMAGE_LOW_WATER_TOKENS`, `MAX_LONG_EDGE`,
  `MAX_IMAGE_TOKENS`, `UNREADABLE_IMAGE_TOKENS`, `image_tokens(path)`, `keep_newest(sets)`.
- `agents/compaction.py` (B6/B13/B14): `KEEP_RECENT_TURNS`, `MEDIA_IMAGES_FRACTION`,
  `MEDIA_BYTES_FRACTION`, `CompactionPlan` (`as_part`),
  `plan_compaction(messages, task_index, keep_recent=, keep_discovery=)`, `apply_compaction`,
  `summary_message`, `summary_text`, `render_middle`,
  `pin_text(task_receipt, produced, task=, earlier_turns=False)`,
  `is_discovery_turn`, `has_server_compaction`. `_ChainDriver.apply_cut(messages)`,
  `_ChainDriver.pending_cut`; `_compact_coding_span(messages, mark, summary)`.
- `ProviderTurnFailed(*, agent, action, cause)` (D1, CLAUDE.md §9.3.8) — raised by
  `AgentRole._react_loop` when `ModelHandle.call` raises a provider error
  (`models.retry.is_provider_error`: a 400 such as "prompt is too long", a 401, a transient
  condition whose retry budget is spent). Before raising, the loop records
  `<role>.<action>.provider_error` (level 1: `error`, `error_type`, `status_code`, `iteration`,
  `budget_used`, `filed: false`, `requeued_texts`). Texts the failed call carried are in that
  turn's ledger history and reach the worker's next turn through it (B2); texts still held for an
  unresolved server tool go back to the inbox (`_requeue_unread`). It clears the kept turn (E12)
  — on a failed `resume` only when the kept turn is still the one it resumed. Nothing is filed or
  composed on the unit's behalf. Attributes `agent`, `action`, `cause`, `status_code`
  (`status_code` or google-genai's `code`, `_provider_status`). Our own defects propagate
  unwrapped; `ReactBudgetExhausted` and `CancelledError` are never caught. Contained per worker by
  the analyst debate (`consensus/DOCS.md`); a lead, librarian or reviewer failure ends the run.
- `TurnOutcome` (D4/D5) — one ReAct turn's own record, passed as `_react(..., outcome=)` (created
  by `_react` when absent): `filed_form`, the terminal call its handler accepted, else `None`;
  `requeued_from`, `(sender, kind)` of each text put back after filing. **Only an accepted
  `turn_end` advances a draft to a report:** `analyze`, `run_cycle` and `research` read their draft
  only when `filed_form` is set. An unfiled worker gets the honest `failed` marker
  (`analyst_worker.analyze.no_report` with `unfiled_draft`), the lead's `outcome.report` is
  `None`, and the librarian raises. Per turn because `last_filed_form` is shared with a background
  resume of the same seat.
- **Texts never shown (D1, D5).** `_react_loop` keeps the inbox entries it drained but has not yet
  shown the model (`unshown`, arrival order; cleared when they are injected). When a provider error
  ends the turn (D1) or an accepted terminal ends it (D5), `_requeue_unread(entries)` puts them
  back in the inbox as they arrived (sender, kind, handles), so a later turn renders each once;
  grace countdowns, coder events and compaction pins are not inbox entries and stay with their
  turn. `*.filed` records `requeued_texts`.
- `_one_turn_at_a_time` — decorator on `analyze`, `run_cycle`, `research`, `review`, `resume` and
  `drive_authoring`: a per-seat `asyncio.Lock` (one per event loop) runs a seat's turns one at a
  time, so a resume never runs beside the caller's cleanup or the seat's next fresh turn (they
  share tools, sandbox and report state). When a turn returns normally it schedules
  `SeatRouter.wake` for the texts it put back (a turn that raised wakes nothing); the woken resume
  takes the lock after it. `resume()` returns at once unless the inbox holds a text that wakes a
  seat (`run_control.WAKING_KINDS`): a fresh turn that ran first may have read it, and a notice
  alone waits for the next turn.

## Changelog
Compacted 2026-09-01 and again 2026-09-24 (every entry), and 2026-10-07 (the 2026-10-06/07
entries); detail in git history.

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
- 2026-10-06 -> 2026-10-07 — **Reach.** [F20] The `bash_exec` command-path scan stands down only
  for a server whose launch recorded `jailed` (`_jail_supersedes_command_check` →
  `ToolkitRegistry.mcp_jailed`), not on the config's `agent_os_jail`. [F22] A worker's cwd is its
  own `report_dir`; memory.md moved beside the ledger; a human's files land in
  `workspace.human_reply_dir`. 2026-10-07: `read_paper(path=)` (librarian, reviewer) reads only the
  papers folder, where downloads land outside every role's write reach; a human's file stays a
  `human` receipt.
- 2026-10-06 — **Env plan B2a/B2b: workers see and choose environments.** `list_env`
  (`list_env_tool_def`, `execute_list_env`; navigation): the catalogue with fingerprints and
  `active`/`shell` flags, or one environment's packages, activation variable names and externals
  (`sandbox.envs.describe`). Every sandbox result carries `[ran in <env> @ <fp12>]`; the turn-start
  payload names the `environment` (`roles._environment_brief`). `use_env` (`use_env_tool_def`,
  `execute_use_env`) points the worker's own shell at a catalogue environment (no name returns it;
  a pipeline environment only when the shell server launched `jailed`); the result carries
  `[shell in <env>]`. `sandbox.envs.in_environment` (shared with the coder) runs the command under
  `/usr/bin/env -i` with the jail's `HOME`/`TMPDIR`/`PFILES`, sources the activation, puts the
  prefix first on `PATH` and the toolkit on `PYTHONPATH`, restores shell state after it, and ends
  the command with an error when activation fails. The environment is resolved by name every call;
  a model-supplied `shell_env` is dropped.
- 2026-10-06 — **Env plan B3b: routing.** `SandboxToolProvider._route_environment`: a toolkit call
  runs in its package's tested environment (`registry.tool_env` → `ar-<id>`) or in the caller's
  `env` choice (popped from the arguments; receipts `env_untested`; empty, `null` or `none` is no
  choice). An unbuilt tested environment, or a pipeline one with the jail off, is an error result.
  The header names `sandbox.call_environment` and flags an untested environment; a call the sandbox
  never ran is unstamped. `prompts/analyst_worker.md` says how to choose.
- 2026-10-06 — **Lead authoring (B3c-1, B3c-4).** `execute_respond_to_review` waits for a working
  coder before the re-review, and a commit through it mints a `commit` receipt as
  `submit_for_review` does. Both commit sites return `Not committed: <reason>` as an error, both
  review calls return an error result on an unreadable changeset, and both answers append the
  session's `env_note`. The in-process `data_catalog` runs committed toolkit code only
  (`react._toolkit_clean_head`: an error while the toolkit is dirty; memo keyed on the toolkit
  commit; re-import purges bytecode and imports under a private `sys.pycache_prefix`).
- 2026-10-17 — [provenance replay, step 1] `SandboxToolProvider._redirect_figure_args` and the inline path/shell rewrites replaced by `_call_context` + `provenance.calls.resolve_call_args` (one statement of the rewrite); `ToolOutcome.call_context`; `AgentRole._react_loop` records the model's args, the result text and steer messages whole (the `_event_arg_cap`/`_event_result_cap`/`_cap_text` helpers are gone) and the call context on `*.tool_result`. Behaviour unchanged (the path guard still runs first, on the model's arguments, which the rewrite never touches).
- 2026-10-17 — `ToolOutcome.receipt_call_id` (the minted receipts' `call_id`, from `SandboxToolProvider.execute` and `_receipted`), written as `call_id` on the `tool_result` ledger line; `_call_context` records `runtime_only` (`env` for a toolkit tool declaring none, `shell_env` for a wrapped shell command).
- 2026-10-17 — [provenance replay, step 2] `SandboxToolProvider._inputs_before` passes the call's mission folders, so each ObsID an argument names is an `obsdb` input; `_receipted(toolkit_root=)` — the lead's in-process `data_catalog` receipt now names the toolkit root and commit.
- 2026-10-17 — `_inputs_before` treats `obs_db_path` as a mission folder too; `_call_context` records the tool's `module` and `fn` (where a reader imports it from).
- 2026-10-17 — [provenance replay, step 3] `ReviewerRender` dispatches per format (`tex`, `html`; one format's fault never stops another's), takes the run ledger and the reviewer's scope (`reviewer_turn_problems` passes it), keeps `html` and `html_errors`.
- 2026-10-17 — `ReviewerRender(report, lookup, scope, numbers)` reuses the validator's own `check_report_fields(sources=True)` answer (`reviewer_turn_problems` computes it only when `html` is configured); each format path is isolated (an exception becomes that format's renderer error); the page is written only when no format has a problem that attempt; problems are deduplicated.
- 2026-10-17 — `_inputs_before(named=)` passes the mission folders the model named itself; `_call_context` records `workdir` (the REPL's folder) for toolkit calls.
- 2026-10-17 — [provenance replay, step 4b] One `read_file` for every role: `read_file_tool_def` + `execute_read_file` over `_page` (streamed, `offset`/`limit`, at most `_MAX_WINDOW_CHARS` = 100,000 characters per read in whole lines, a line over `_MAX_LINE_CHARS` cut with `… [+N chars]`, the footer naming the next offset). Reviewer/lead/co-lead as before; analyst workers now in-process through `SandboxToolProvider` (allow-list checked with links resolved, relative paths from the worker's folder, receipted as `read_file`, a record); the status monitor reads its own ledger with it (no path argument). `_page_text_lines` (web_fetch) shares `_page`.
- 2026-10-17 — Step-4b review fixes: `_bounded_lines` reads a file in bounded chunks (a 300 MB newline-free file had taken the backend to 705 MB) and `_page` stops counting 20 MB past the window ("more lines follow"); a non-regular file (FIFO) and a file with a NUL in its first 8 KB are answered, not read; every read runs off the event loop with a 60 s answer (`_bounded_read`); with the jail on, a worker's read allowed only through the host `/tmp` is sent to its shell (`_in_host_tmp`: the shell's /tmp is private); the status monitor's `read_file` keeps `ledger_event`'s 32,000-character bound; `_int_arg` answers `None`/`inf`/`"2, "` offsets for `read_file` and `web_fetch` (web_fetch had raised out of the loop). Residuals: a read past its timeout leaves its thread behind; a symlink swapped between the guard and `open` (sabotage only).
- 2026-10-17 — Reader re-review fixes (paging verified against a reference over 600 random files): the count-ahead cap counts newlines (a file of empty lines had been counted whole); the window bound counts each row's real width; a worker's path checks run off the event loop with the read, and its own folder is never taken for host /tmp; `ReadOnlyToolProvider._allowed` answers a NUL path (it had raised out of the turn); `_int_arg` treats only missing/None/"" as the default (`limit=0` is one line); the monitor's tool description names its own 32,000-character bound.
- 2026-10-17 — The librarian's `web_fetch` description: a page is listed in a finding's support for what it says in words; a value on it points to the paper to open and attribute. Reviewer refusals and `_PASS_NAMES` say "research" for the librarian; the reviewer is told a report to the human goes "as a document".
- 2026-10-18 — [A1–A4, run 32814459] Report form and diagnostics. `edit_report_tool_def`: the description and the `summary` / `findings` / `cited_figures` / `status` properties carry one definition of the form; `cited_figures[].source` is `self` on the worker surface and the worker key on the lead's. `execute_edit_report` refuses parameter markup naming a declared field inside any argument (`merged_parameter_problem`, also on the plain/reviewer path and the librarian's `_edit_report`), a non-array `cited_figures`/`findings` (`_list_field_problem`; `""`/`"null"` read as not sent) and a malformed entry (`_cited_entry_problem`, its example from `_example_source`) without saving; a saved draft notes dropped figures (`_dropped_figures_note`), a missing `status`, and results in `summary` (`report_shape_notes`, also the librarian's). `worker_report_problems` refuses a `succeeded` report with no findings and drops "or say why none apply"; refusals in it and in `lead_turn_problems` name the field and the fix (clause count unchanged for the lead; the no-findings refusal offers `rework` only while a round remains); the librarian's empty-summary refusal says what to write, and its `edit_report` reads "null" slots as not sent, names a non-list findings/gaps and notes an unsent status. The worker's unreachable "no status" refusal is removed (status defaults to `failed`; the save note covers it). Tests: `tests/test_report_diagnostics.py`. Live (opt-in `AUTO_RESEARCH_LIVE_REPORT_FORM=1`, `AUTO_RESEARCH_REPORT_FORM_SEATS` / `_LEAD_SEAT` / `_DRAWS`): `tests/test_live_report_form.py`. Blue workers ran 3 draws on each of claude-sonnet-5 and gpt-5.6-terra, and the blue lead on gpt-5.6-terra; all were first-try and filed the form. Red workers ran 2 draws per seat; each ended on a valid report, with a longest refusal run of 4. That run caught `</parameter name=…>`, which is now detected. Total $1.01.
- 2026-10-18 — [B1] The ledger is the session history: `_react_loop` records `turn_start`, every returned model turn whole (`*.model_turn`), the silent-turn nudge (`*.nudge`), figures/documents/result receipt on `*.tool_result`, the grace suffix on `*.tools`, and the lead's coding-span compaction, each as a `data["history"]` part; new `agents/history.py` rebuilds each actor's turns and every call's input from the ledger. `resume()` records its kept turn as the base. `_mint_task_input` returns the handle, named on `turn_start`. No behaviour change to any request.
- 2026-10-18 — [D1, run 32814459] Contained provider errors: `_react_loop` turns a provider error
  that escapes retry into `ProviderTurnFailed`, recorded as `<role>.<action>.provider_error`;
  texts held back from that call return to the inbox (`requeued`; delivered ones travel in the
  ledger history) and a stale kept turn is cleared.
  New `ProviderTurnFailed`, `_provider_status`. Tests:
  `tests/test_provider_containment.py`.
- 2026-10-18 — [B1 review] Fixes from the stateless review: the turn id is fixed before the loop and named by every part (a background resume beside a new turn had interleaved); a resume that ends unfiled becomes the next resume's base; messages, turns and tool sets ride as insertion-ordered JSON strings (`seed_json`, `response_json`, `tools_json` + `tools_sha256`), since the ledger sorts keys; `rebuild` refuses a results batch that does not answer the model turn before it and an in-memory monitor below chatter 10; the reviewer names its request receipt on `turn_start`; `_result_receipt` scans newest first.
- 2026-10-18 — [B2/B3] Every graph seat continues its session from the ledger (`AgentRole._history_seed`, ruling "all"): worker, lead, reviewer, librarian open with their latest turn rebuilt in the `indexed` view; memory.md retired (`_append_tool_results_to_memory`, `_STDOUT_MEM_CAP`/`_STDERR_MEM_CAP` and `AnalystWorkerAgent(memory_path=)` deleted). `history.index_outputs`, `INDEX_MARK`, `INDEX_KEEP_CHARS`, `INDEX_ARGS_CHARS`, `Turn.handles`; `apply_view(..., receipts=)`. `view_receipt` pages a long text receipt (`offset`/`limit`). Tests: `tests/test_session_history.py`.
- 2026-10-18 — [B4] Image budget: new `agents/images.py` (`IMAGE_BUDGET_TOKENS`, `IMAGE_LOW_WATER_TOKENS`, `image_tokens`, `figure_sets`, `plan_evictions`, `placeholder`, `apply_evictions`); `AgentRole._image_view` / `_figure_label`; `_react_loop` sends the budgeted view (D's containment wrap unchanged); `_history_model(evicted=)`; `history.rebuild` applies the recorded evictions. Tests: `tests/test_image_budget.py`.
- 2026-10-18 — [B7 ruling] `ReviewerAgent.review`: a continuing session is handed only this turn's report (the newest literature report when `arrived="librarian"`, the analysis report when `"analysts"`; no earlier reviewer reports); a fresh session gets them all as before. The lead's payload already carried only the current round's worker reports. The image budget exempts no set (ruling B3). Window from litellm first (`observability.costs.context_window_for`, ruling B5).
- 2026-10-18 — [B4 review] `plan_evictions` returns `(lasting, this_call_only)`: first-call figures evict conversation sets for that call only, and an over-budget first-call set is named in a user message (it had been dropped silently); image cost capped at the provider's processed size (an 8000x6000 image had cost 61k and could never be shown); set keys `"<id>#<n>"` (repeated ids had shared a key); `_figure_label` prefers this seat's own receipt; the media guard's count ceiling counts only images the provider takes.
- 2026-10-18 — [B2/B3 review] `ToolResultPart.receipt` carries each output's handle (repeated call ids had indexed the wrong receipt and arguments); outputs with no receipt keep a 1,500-character head; `view_receipt` splits long lines into rows and pages at 20,000 characters (a one-line 100k result had been readable to its first 2,000 only); `_history_seed` is async (ledger read off the event loop), filters by run id, and is contained (unreadable → level-1 event, fresh seed); `_CarriedTools` keeps earlier turns' tools declared; `_react` drops the kept turn when a fresh turn raises; `figure_entries(prefer=)`.
- 2026-10-18 — [B8 ruling] `IMAGE_LOW_WATER_TOKENS` = 20,000: eviction triggers at 40k and stops at 20k.
- 2026-10-18 — [A rulings] Human rulings on A1/A2: the merged-parameter refusal (`merged_parameter_problem` and its tag patterns) is removed from every role's `edit_report` — markup in a text field is saved as text and the other notes and `turn_end` carry it; the 3,000-character / sentence-count note is replaced by a warning when the summary is longer than the findings combined, at `edit_report` (`report_shape_notes`, `carries_results`) and at `turn_end` (`execute_turn_end(..., warnings=)`, `report_warnings`, `LibrarianToolProvider._warnings`), which still files the report. The librarian's unopened-handle refusal names the per-pass fix (open it in this pass with `read_paper` or `web_fetch`). Tests: `tests/test_report_diagnostics.py`; `tests/test_live_report_form.py` drops the retired `memory_path` and counts a summary warning as a blue failure. Review fixes: the warning reaches only reports that carry results (where `turn_end` requires findings); on a refusal it follows the fix line as "Also noted", on acceptance it reads "Your report is filed as it stands. Note for your next report"; the live recorder passes `warnings` through.
- 2026-10-18 — [B6/B7] Compaction: new `agents/compaction.py` (`plan_compaction`, `apply_compaction`, `CompactionPlan`, `summary_message`, `render_middle`, `pin_text`, `is_discovery_turn`, `has_server_compaction`, `KEEP_RECENT_TURNS`); `AgentRole._client_compaction`; `_react_loop` keeps a per-turn `session` (task index, produced receipts, prompt size), queues the pin after a server compaction and condenses before a call on the client path; `_record_context_edit` records `*.compaction`; `history.rebuild` replays `op: "compact"`. Prompt role `session_compactor`. Tests: `tests/test_compaction.py`.
- 2026-10-18 — [B10 ruling] `tests/test_live_report_rounds.py` deleted: it seeded rounds through the retired memory.md and called `lead.adjudicate`, deleted 2026-09-02; round-1 figures staying citable is covered by the append-only catalog tests and asserted in the B9 live run.
- 2026-10-18 — [rulings B6/B13/B14] Append-only turns: the B4 mid-turn eviction (`_image_view`, `plan_evictions`, `apply_evictions`) and `_CarriedTools` deleted; first-call figures ride the task message (`ModelMessage.figures`); the boundary seed is one transcript (`history.transcript`, `turn_figure_sets`, `TRANSCRIPT_MARK`; `images.keep_newest`; `_condensed_transcript`); `_client_compaction` became `_segment_boundary` (all earlier assistant turns removed); server compaction's pin restates the task; `_ChainDriver` cuts a finished link at the next complete tool round (`apply_cut`, `pending_cut`, the lead's `_segment_cut` hook); `_compact_coding_span` drops the closing turn. Tests updated: `test_session_history`, `test_image_budget`, `test_compaction`, `test_history_ledger`, `test_lead_forms`, `test_provider_containment`.
- 2026-10-18 — [B8] `AgentRole.resume` resumes its kept turn from the ledger rebuild (`_kept_turn_rebuild`, `history.conversation(..., run_id=)`), which becomes the kept list; a rebuild that differs from the kept turn in memory is recorded at level 1 (`*.history.unreadable`) and the kept list is used.
- 2026-10-18 — [D4 ruling] The absolute ceiling gets the budget's grace window (counted in model
  turns), then `ReactBudgetExhausted`. Grace 0 on a terminal loop raises at the budget cap. Only an
  accepted `turn_end` (`TurnOutcome.filed_form`, per turn) advances a draft to a report in
  `analyze` / `run_cycle` / `research`. Six tests that passed only by returning an unfiled draft
  after 600 turns now file for real (`test_lead_forms`, `test_report_authoring`, `test_sandbox`).
  [D5 ruling] Texts that arrive in the batch that files are re-queued as they arrived and wake the
  idle seat (`_requeue_unread`, `SeatRouter.wake`). Tests: `tests/test_react_ceiling.py`.
- 2026-10-18 — [A5/A7, human rulings] The report is one text document edited like a file: new `agents/report_edit.py` (block tables, `render`/`parse`, Edit semantics `apply_edit`, `snippet`, `outline`, `change`, `edit_tool_def`/`write_tool_def`, `WRITE_REPORT_TOOL`); `react.py` `edit_report_tool_def` (now Edit: `old_string`/`new_string`/`replace_all`), new `write_report_tool_def`, `report_blocks`, `report_document`, `execute_edit_report(workspace, arguments, tool=)` + `execute_write_report`, both providers declare and route `write_report`; `review_report` shows the document; refusals name blocks (`# findings`, `# figures`, `# summary`); librarian (`_edit_report(arguments, tool)`, `_report_tool_defs`, document in `_review_text`) and reviewer (`REVIEWER_EDIT_DESCRIPTION` is now the role text after the layout) share it; `roles.py` adds `WRITE_REPORT_TOOL` to the free-tool sets and `_RESUME_CLOSED`. The interim field-by-field version (add_/replace_/remove_ ops, built the same day) is replaced. A6: a round's draft starts empty. Tests: `tests/test_report_edit.py`, `tests/test_report_diagnostics.py`; every report-tool test fake moved to `write_report`/`edit_report` documents.

- 2026-10-18 — [D5 review fixes] Re-queued texts are the drained `InboxEntry` objects in arrival
  order (`unshown`; `_requeue_unread(entries)` returns `(sender, kind)`); a seat's turns run one at
  a time (`_one_turn_at_a_time` on analyze / run_cycle / research / review / resume) and the wake
  is scheduled as the turn ends; `resume()` skips an empty inbox. Tests in
  `tests/test_react_ceiling.py` (deadlock, live and idle senders, steer, notice, wake while
  working, one resume for two texts, next fresh turn reads the text once).
- 2026-10-18 — [A7 review fixes] `parse` refuses a header-like line that names no block of the role (`# Findings`, `# rework` on a worker), and a slip on a real header (`# findings:`, `#findings`), so nothing folds into the block above; a worker's `# status` is checked against the surface's `status_values` (`needs_human` stays the lead's); replies show the draft as stored (`report_edit.snippet(before, after)` diffs canonical renders; `reply_lines` echoes a write's stored draft when it differs from what was sent, blank lines included); outline notes say what an empty `# rework`/`# accepted`/`# assign_to` reads as; a librarian's support line is the item's last line, its handles are the tokens holding ':', brackets stripped; CRLF content is normalised. Live (`tests/test_live_report_form.py`, $0.42): blue 2 draws on each worker seat and the lead, all first-try with 0 refusals (one write of 1.3–2.9k characters, then edits of 0.5–1.5k); red 1 draw per seat, every attack refused with a reason, both ended on valid reports.
- 2026-10-18 — [D5 review 2] The turn lock is per event loop and wakes only on a normal return;
  `drive_authoring` is decorated too; `resume()` runs only for a waking text (message, reply,
  steer). Tests: a woken resume behind the next fresh turn; the two-idle-seats test now asserts B
  reads A's answer.
- 2026-10-18 — [R7/R3/R2/R10, B8 review] A turn's task attaches each figure once per session and within the image budget (`_task_figures`, `_session_figures`; `peer_figures_not_attached` / `figures_not_attached`); the transcript names a task message's figures by receipt. Segment boundary on media pressure for every provider (`_media_trigger`, `compaction.MEDIA_*_FRACTION`). Documents nest in the result that opened them (`ToolResultPart.documents`); the loop calls with no call-time documents and attaches a carried document once; `PaperReader` no longer dedupes ("already attached" removed). A condensed boundary seed pins every earlier turn's receipts (`history.transcript_receipts`, `CONDENSED_NOTE`, `pin_text(earlier_turns=)`); a nested condensed summary keeps its mark. Tests: `test_image_budget`, `test_compaction`, `test_react_tools`, `test_history_ledger`, `test_session_history` (resume identity), `test_consensus_peer_summary` (previous round for every position), `test_librarian`, `test_paper_tools`.
- 2026-10-18 — [R7/R3/R2/R10 review fixes] A segment boundary carries the latest round's documents on the message that restates it (`ModelMessage.documents`) and pins every opened paper's receipt (`_paper_handle`); the media trigger fires only when the kept media (task + latest round) are below the line, and bypasses the four-call wait unless the last summary failed (`session["failed"]`); the token estimate counts opened PDFs (`_document_tokens`, 3,000 tokens a page). R10 reads receipts from the rebuilt turns (`history.transcript_receipts` and its regexes removed: they pinned handles quoted in task text). An output kept whole in the indexed view names its receipt; `keep_newest` also caps by count (`IMAGE_BUDGET_COUNT`/`IMAGE_LOW_WATER_COUNT`). Tests in `test_compaction` (cut keeps the document and pins the paper, no refire on unremovable media, bytes trigger without back-off, naming, structural pin) and `test_media_guards` (nested/message documents budgeted, refusal for a `documents: false` member, Google nested parts).
- 2026-10-18 — [B18, human ruling] `review_report` erased for every role: `REVIEW_REPORT_TOOL`, `review_report_tool_def`, `execute_review_report`, `_catalog_menu`, `_report_view`, `_receipt_menu`, `ReportWorkspace.source_reports`/`draft_view`, the reviewer's `reviewer_draft_view`/`reviewer_text_view` (typeset preview; `turn_end` still renders, compiles and refuses failures), the librarian's `_review_text` and tool, and its name in every free-tool set (`roles.py`, `LIBRARIAN_FREE_TOOLS`). Its content now appears where it first does, text only: `SandboxToolProvider` names each figure a tool result produced as `[self:k] caption`; `AnalystWorkerAgent.analyze` adds `your_figures` (earlier rounds' labels); `AnalystLeadAgent.run_cycle` adds `figure_catalog` (every worker figure by `[analyst_worker_i:k]` label, the image budget's left-out figures marked with their receipt handle in the same line, replacing `figures_not_attached`; `_figure_labels`); refused `write_report`/`edit_report` calls show the draft as stored; the librarian names the documents opened in the pass (`_opened_text`). Prompts, refusals and descriptions point at those places.
- 2026-10-18 — [B18 review fixes] `_receipt_scope` admits the figure receipts of the reports a role received (`_receipt_handles_for_figures`), so `view_receipt` opens a placed or not-attached worker figure; every `figure_catalog` line carries its receipt handle, "(not attached)" past the image budget; the lead's attached images are captioned by their `[analyst_worker_i:k]` label (two workers' plots with one caption pair up); the unplaced-figures note carries captions; `report/DOCS.md` and `config/DOCS.md` lose their `review_report` mentions.

