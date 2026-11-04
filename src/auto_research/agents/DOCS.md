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
(`_call_context`, which also records the manifest's parameter `defaults`), returned as
`ToolOutcome.call_context` and recorded on the `tool_result` ledger line, so the ledger rebuilds
the arguments the call ran with. Before a toolkit call runs, `_validated` is the unit's input
validator (`provenance.inputs`): the model's arguments are checked against their declared kinds,
then references resolve and typed parameters lose their reason on the way to the tool; the lead's
in-process `execute_data_catalog` runs the same two functions against the committed manifest; when `orchestration.yaml: tool_search_enabled` is set it marks
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
to the sender's inbox. **Texts are informal**: none mints a receipt or widens a citable scope. Each
text renders as `[Message from X]` / `[Human steer]` / `[Reply from the human]` / `[Notice]`, the
receipts it names, and the address that answers its sender (`InboxEntry.render`). `view_receipt`
opens any receipt of the run (view is global, user ruling 2026-10-27), a paper as its PDF. The human's text is a nudge (never a
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
- `load_prompt(role, root=None) -> str` loads `prompts/<role>.md` — for a unit role
  (`UNIT_ROLES`: reviewer, librarian, worker, lead) `prompts/conventions.md` first, then the
  role's file (`CONVENTIONS_FILENAME`) — strips each, and raises `ValueError` if either references
  a build-time governance filename.
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
  `wait_for_reply_tool_def()`; `execute_view_receipt(store, arguments, *, images_ok)` (any receipt
  of the run: a figure as its image, a paper as its PDF document, text paged; mints and admits
  nothing); `execute_wait_for_reply(run_control, agent_key,
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
- `AnalystWorkerAgent(*, sandbox=None, registry=None,
  worker_index=0, ...)` —
  analyst worker wrapper.
  - `analyze(task, *, peer_reports=())` → `AnalystReport`. Opens with its earlier turns
    rebuilt from the ledger (`AgentRole._history_seed`, indexed), then the task and peer
    reports; runs the inner ReAct loop over `SandboxToolProvider` when `sandbox` + `registry`
    are attached and returns the `AnalystReport` it filed. With no sandbox/registry it falls
    back to a plain structured call. Its `$ref`s resolve its own results only.
  - The task and peer reports reach the model through `_for_reader`: each value reference
    followed by its value (`provenance.bindings.render_values`); the lead's `worker_reports` and
    the reviewer's `analysis_report` / `literature_reports` the same way.
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
Compacted 2026-09-01 and again 2026-09-24 (every entry), 2026-10-07 (the 2026-10-06/07
entries) and 2026-10-19 (every entry); detail in git history.

- 2026-05-26 -> 2026-06-30 — Reviewer, librarian, worker and lead wrappers (native structured
  output, bounded schema retries, `Sandbox`/`ReadOnly`/`SourceToolProvider`); Phase 2 added
  `clarify_for_author`, `AnalystColeadAgent.review_tool`, `ask_human`, `decide_authoring`,
  `data_catalog`. `_react` returns `(messages, figures, documents)`; `open_paper`; FS jail
  (`PathGuard`); `free_tools`/`absolute_cap`, `plan_and_debate`; `finalize_authoring_outcome`;
  `drive_coder`/`submit_for_review`/`respond_to_review`, `drive_authoring`.
- 2026-07-06 -> 2026-07-31 — [F4] `_parse_tool_figures` drops a bad figure with a note; [A8] ordered
  `tool_specs` chain; [I1]/[I10]/[F8] `web_search` for every role, paged `web_fetch`, line-range
  `read_file`; V-4 `edit_report`/`review_report`; `data_catalog` from
  `research_toolkit.catalog_utils`; `_record_context_edit`; `AgentRole.run_control`/`agent_key` +
  steer inbox; deferred tool loading (`tool_search_enabled`, LIVE-VERIFIED 2026-07-28); stop-flag
  reading deleted (`/stop` cancels); the lead's `AuthoringSession` on `RunControl`;
  `MonitorToolProvider` (`brief_history`/`ledger_event`/`ledger_grep`) + `StatusMonitorAgent`
  (`status_react_loop_cap`).
- 2026-08-05 -> 2026-08-31 — `_react` appends the received `ModelResponse` whole (`AssistantToolUse`
  deleted); terminal composes use `tool_choice="none"`; `_OPEN_PAPER_MAX_MB` deleted; [F11] a
  malformed `offset`/`limit` returns `is_error`.
- 2026-09-01 -> 2026-09-02 — `_event_arg_cap()`/`_event_result_cap()` read `env.ledger_caps()`;
  `tests/test_live_no_hitl.py` LIVE-VERIFIED. The lead became one ReAct loop filing forms
  (`run_cycle`: `author_chain`, `hold_authoring`, `request_another_round`, `submit_adjudication`,
  `submit_terminal_report`;
  `adjudicate`/`judge_convergence`/`decide_authoring`/`finalize_authoring_outcome` deleted);
  `_react` `terminal_tools`; `_authoring_declared()`; LIVE-VERIFIED
  (`tests/test_live_lead_cycle.py`, new `tests/test_live_form_shapes.py`) and in production.
- 2026-09-12 -> 2026-09-13 — A1–A11: every lead form but `author_chain` deleted (stage constants,
  `_STAGE_FORMS`, `LEAD_CYCLE_TERMINALS`, `_author_chain`, `_drive_one_link`); `run_cycle` ends on
  `turn_end` (`lead_turn_problems`); `_ChainDriver.advance`; `_react` `share_messages`;
  `AnalystReport` gained `rework`/`rationale`/`what_to_probe`, `status` `needs_human`;
  `edit_report_tool_def(prose_fields, status_values)`; reviewer `terminal_tools={turn_end}`. A2: one
  steer drain per iteration (`_last_turn_has_unresolved_server_tool`). B3–B6: `start_drive`,
  `check_coder`; `execute_submit` refuses while the coder works;
  `clarify_for_author`/`_ClarifyReply` deleted (`_coder_questions`);
  `tests/test_live_form_shapes.py` → `tests/test_live_lead_providers.py`. 09-13:
  `_drop_coding_middle` → `_compact_coding_span` (reverses B6), `compactor` role,
  `_unanswered_call_ids`; `_build_session` the only test seam (`tests/chain_stub.py` `StubSession`);
  `AUTO_RESEARCH_LEAD_SEAT`.
- 2026-09-15 — `check_coder` rate-limited (`check_coder_min_interval_s`, replaced 09-24). Ruling:
  `turn_end` is the only exit: `_react` `require_terminal` (`_TERMINAL_REQUIRED_NUDGE`), the
  `len(content) > 40` finish test removed; grace window `react_grace_rounds` (default 20), then
  `ReactBudgetExhausted`; `turn_end` joins `_CYCLE_FREE_TOOLS`; escape shapes in
  `test_live_turn_end_shapes.py`, all pass live.
- 2026-09-24 — (95) Librarian: `research()` is one `_react` loop over `LibrarianToolProvider` filing
  a `LiteratureReport` ([I2], [I9], §2b; ten pipeline methods deleted; `librarian.source.error`);
  `search_with_match`; `read_paper` attaches the PDF natively (ruling: no Docling, no vendor
  citation feature); shared `agents/paper_tools.py` (`read_paper`, `PaperReader`, `format_ids`,
  `ids_from_stem`; `ReviewerPaperReader` deleted); rulings: papers cited, never quoted, then
  per-number attribution with a locator (`attributed_values`);
  `tests/test_live_librarian_shapes.py`.
- 2026-09-24 — (85) Blocking `wait_coder` (`env.coder_wait_limits_s`) replaces `check_coder`
  (`_wait_before_submit`, `_submit_now`). (5c) `reviewer_tools.py`; `ReviewerAgent` is one loop over
  one report on `edit_report` (`compose_machine_report`/`author_latex_report` deleted; §2b closed);
  ruling: a number in `follow_up` must be grounded.
- 2026-09-24 — (0f) Provenance P1/P2: both providers mint receipts per call;
  `report_provenance_problems` at `edit_report` (notes) and `turn_end`; `ReportWorkspace`
  `receipts`/`receipt_source`/`itemised`/`provenance_check`/`draft_view`/`edit_description`;
  `edit_report_tool_def(report_model=)`, `extra_tools`, `_edit_plain_report`; `ScopedLookup`
  (`roles._receipt_scope`); `roles._mint_task_input` (`[input:…]`);
  `ReadOnlyToolProvider(include_open_paper=False)`; `LeadCycleOutcome.verdict` deleted. P7 shapes:
  four refused and recovered LIVE, `flagged_quote` never reached; `attribution_without_locator`
  LIVE-VERIFIED.
- 2026-09-25 — [I4] a call with `arguments_error` gets an `is_error` asking for valid JSON, never
  run. [G14]/[G15] `parse_tool_figures`, `normalize_tool_raw`, `PROTOCOL_KEYS`, `tool_text` moved to
  `sandbox.runner`. [R1] `ReviewerAgent.review` is one turn of the sole-I/O reviewer (`arrived`,
  `previous`, `human_nudges`, `human_files`, `budgets`, `human_attended` replace
  `stage`/`iterations_left`); `reviewer_turn_problems` routes on `assign_to`; role report JSONLs
  beside the ledger (human ruling); a call after an accepted terminal in its batch is answered, not
  run (human ruling, every role). [R7] a committing `submit_for_review` mints a `commit` receipt
  (`_commit_receipted`); in-process `data_catalog` receipted; `reviewer_claims`/`BRIEF_FIELDS`
  (`follow_up` exempt); `reviewer_scope` excludes only `input:`.
- 2026-09-25 — [E12] Quick channel: `ChannelTools` (`_react` wraps `_react_loop`) with
  `message`/`view_receipt`/`wait_for_reply`/`list_peers` on a `SeatRouter`; `AgentRole.resume`;
  `ask_human`/`plan_and_debate` and the providers'
  `human_handler`/`include_plan_and_debate`/`asked_by`/`model` params deleted; `wait_coder` →
  `wait_for_reply`; `drive_coder` folded into `message(to="coder")` (user ruling;
  `drive_coder_tool_def`/`DRIVE_CODER_TOOL` deleted, `execute_drive_coder` →
  `execute_coder_message`); `_coder_questions` deleted; an authoring seat gets a one-seat
  `SeatRouter` (`_has_authoring_surface`).
- 2026-10-06 -> 2026-10-07 — [F20] the `bash_exec` path scan stands down only for a server recorded
  `jailed` (`_jail_supersedes_command_check` → `ToolkitRegistry.mcp_jailed`). [F22] a worker's cwd
  is its `report_dir`; memory.md beside the ledger; human files in `workspace.human_reply_dir`;
  `read_paper(path=)` reads only the papers folder. Env B2a/B2b: `list_env` (`list_env_tool_def`,
  `execute_list_env`), `use_env` (`use_env_tool_def`, `execute_use_env`), `[ran in <env> @
  <fp12>]`/`[shell in <env>]`, `roles._environment_brief`, `sandbox.envs.in_environment` (model
  `shell_env` dropped). B3b: `SandboxToolProvider._route_environment` (tested `ar-<id>` or the
  caller's `env`, receipted `env_untested`). B3c-1/B3c-4: `execute_respond_to_review` waits for the
  coder and mints a `commit` receipt; `Not committed: <reason>` as an error; `env_note`; in-process
  `data_catalog` on committed code only (`react._toolkit_clean_head`).
- 2026-10-17 — [provenance replay 1–4b] `_call_context` + `provenance.calls.resolve_call_args`
  replace `_redirect_figure_args` (`ToolOutcome.call_context`, `runtime_only`, `module`/`fn`,
  `workdir`); args, results and steers recorded whole
  (`_event_arg_cap`/`_event_result_cap`/`_cap_text` removed); `ToolOutcome.receipt_call_id`;
  `_inputs_before` mission folders (`obsdb` inputs, `obs_db_path`, `named=`);
  `_receipted(toolkit_root=)`. `ReviewerRender(report, lookup, scope, numbers)` per format (`tex`,
  `html`, `html_errors`). One `read_file` for every role (`read_file_tool_def`, `execute_read_file`,
  `_page`, `_MAX_WINDOW_CHARS`, `_MAX_LINE_CHARS`, `_bounded_lines`, `_bounded_read`,
  `_in_host_tmp`, `_int_arg`), workers through `SandboxToolProvider`, the monitor on its ledger.
  Residuals: a timed-out read leaves its thread; a symlink swap (sabotage only). Librarian
  `web_fetch` description and reviewer wording follow its research scope.
- 2026-10-18 — [A1–A4, A rulings, run 32814459] Report-form diagnostics (`_list_field_problem`,
  `_cited_entry_problem`, `_example_source`, `_dropped_figures_note`, `report_shape_notes`);
  `merged_parameter_problem` removed (ruling); a summary longer than the findings warns and still
  files (`carries_results`, `execute_turn_end(..., warnings=)`, `report_warnings`,
  `LibrarianToolProvider._warnings`). LIVE `tests/test_live_report_form.py` ($1.01).
- 2026-10-18 — [A5/A6/A7, human rulings] The report is one text document edited like a file:
  `agents/report_edit.py` (`render`/`parse`, `apply_edit`, `snippet`, `outline`, `change`,
  `edit_tool_def`/`write_tool_def`, `WRITE_REPORT_TOOL`); `edit_report` takes
  `old_string`/`new_string`/`replace_all`; `write_report_tool_def`, `execute_write_report`,
  `report_blocks`, `report_document`; a round's draft starts empty. LIVE ($0.42).
- 2026-10-18 — [B1–B3, B8, B10] The ledger is the session history (`agents/history.py`;
  `*.model_turn`, `*.nudge`; `seed_json`/`response_json`/`tools_json` + `tools_sha256`); every seat
  continues from it (`AgentRole._history_seed`, ruling "all"; resume via `_kept_turn_rebuild`);
  memory.md retired (`_append_tool_results_to_memory`, `_STDOUT_MEM_CAP`/`_STDERR_MEM_CAP`,
  `AnalystWorkerAgent(memory_path=)` deleted); `history.index_outputs`,
  `INDEX_MARK`/`INDEX_KEEP_CHARS`/`INDEX_ARGS_CHARS`, `Turn.handles`, `ToolResultPart.receipt`;
  `view_receipt` pages; `tests/test_live_report_rounds.py` deleted.
- 2026-10-18 — [B4–B8, B13/B14, rulings] `agents/images.py` (`IMAGE_BUDGET_TOKENS`,
  `IMAGE_LOW_WATER_TOKENS` = 20,000, `image_tokens`, `figure_sets`, `keep_newest`,
  `IMAGE_BUDGET_COUNT`/`IMAGE_LOW_WATER_COUNT`); `agents/compaction.py` (`plan_compaction`,
  `apply_compaction`, `CompactionPlan`, `summary_message`, `render_middle`, `pin_text`,
  `is_discovery_turn`, `has_server_compaction`, `KEEP_RECENT_TURNS`), prompt role
  `session_compactor`; append-only turns: `_image_view`, `plan_evictions`, `apply_evictions`,
  `_CarriedTools` deleted; one transcript seed (`history.transcript`, `turn_figure_sets`,
  `TRANSCRIPT_MARK`, `_condensed_transcript`); `_client_compaction` → `_segment_boundary`;
  `_ChainDriver` cuts (`apply_cut`, `pending_cut`, `_segment_cut`); a continuing reviewer gets only
  this turn's report (B7); no image set exempt (B3); window from litellm (B5).
- 2026-10-18 — [R2/R3/R7/R10] Task figures once per session (`_task_figures`, `_session_figures`);
  media-pressure boundary on every provider (`_media_trigger`, `MEDIA_*_FRACTION`); documents ride
  their result (`ToolResultPart.documents`, `ModelMessage.documents`); a condensed seed pins earlier
  receipts (`CONDENSED_NOTE`, `pin_text(earlier_turns=)`, `_paper_handle`, `_document_tokens`;
  `history.transcript_receipts` removed).
- 2026-10-18 — [D1, run 32814459] Provider errors contained (`ProviderTurnFailed`,
  `_provider_status`, `*.provider_error`). [D4/D5 rulings] the absolute ceiling gets the grace
  window, then `ReactBudgetExhausted`; only an accepted `turn_end` files (`TurnOutcome.filed_form`);
  texts in the filing batch are re-queued and wake the seat (`_requeue_unread`, `SeatRouter.wake`,
  `_one_turn_at_a_time`).
- 2026-10-18 — [B18, human ruling] `review_report` erased for every role (`REVIEW_REPORT_TOOL`,
  `review_report_tool_def`, `execute_review_report`, `_catalog_menu`, `_report_view`,
  `_receipt_menu`, `ReportWorkspace.source_reports`/`draft_view`,
  `reviewer_draft_view`/`reviewer_text_view`, `_review_text`); figure labels where the figure
  appears (`[self:k]`, `your_figures`, `figure_catalog`, `_figure_labels`, `_opened_text`;
  `figures_not_attached` replaced); `_receipt_handles_for_figures`.
- 2026-10-26 — Receipt references (`provenance.refs`; audit of run 32814459): `SandboxToolProvider`
  resolves `{"$ref": …}` toolkit arguments before a call runs (`_resolve_refs`; a refusal is the
  call's `is_error` and nothing runs), passes the parents to `receipt_call`, notes typed numbers a
  usable result holds (`_retype_note`, since replaced by the refusal `_retyped`); `ref_handles=` (default: its own receipts); `analyze` wires
  it and shows the task's named results (`_brief_handles`).
- 2026-10-26 — Review round 1: `lead_turn_problems` refuses a `what_to_probe` naming a handle the
  lead cannot open (`provenance.refs.brief_handle_problems`; `reviewer_turn_problems` does the same
  for `follow_up`); `SandboxToolProvider` passes the tool's returned keys to `receipt_call`
  (`returned=`) and resolves references and the retype note in a worker thread.
- 2026-10-26 — `SandboxToolProvider._retyped`: a toolkit call that types a float a usable result
  holds is refused before it runs, with the reference to write (`provenance.refs.RetypeCheck`).
- 2026-10-26 — Review round 2: `lead_turn_problems(viewable=)` and `reviewer_turn_problems(viewable=)`
  accept a brief handle a message showed the author (`AgentRole._viewable_by_message`,
  `_seat_viewable`).
- 2026-10-27 — Batch after run faraday-refs-20261026 (user rulings 2026-10-27): view is global
  (`execute_view_receipt(store, arguments)` opens any receipt, a paper as its PDF; `$ref` resolves
  any toolkit result; `_brief_handles`/`ref_handles` removed); `available_toolkits` deleted (a
  hard-coded field that controlled nothing and misled a lead); reports and tasks handed to a model
  render value references with their values (`_for_reader`); the message tool description says the
  recipient sees every handle a text names; `load_prompt` prepends `conventions.md` for unit roles.
- 2026-11-03 — A worker's `$ref`s resolve its own results only (`_usable_handles`); prompts and
  `REF_NOTE` say a peer's value enters a call once the worker's own tool run reproduces it.
- 2026-11-03 — `view_receipt` names what a result was made from (`_made_from`: the results passed to
  its call by reference and the files it read, or "typed arguments only"), so a reader holding a
  handle can tell a passed value from a typed one (user ruling).
- 2026-11-03 — `_made_from` states what the runtime recorded, for toolkit results only: "its call
  took … by reference and read …", or "took no earlier result by reference and read no recorded
  file".
- 2026-11-03 — The input validator on both toolkit call paths (piece A of
  `tmp_plan_handles_only.md`): `SandboxToolProvider._validated` (`inputs.check_arguments`, `env` a
  runtime argument, then `inputs.tool_values`) replaces `_resolve_refs` and `_retyped`;
  `_call_context` records manifest `defaults`; `execute_data_catalog` reads the committed manifest
  with the committed code (cached `(head, fn, spec)`), checks its arguments and fills its
  defaults; `data_catalog_tool_def` sends `sandbox.registry.provider_schema`.
- 2026-11-03 — `LEAD_KEY = "analyst_lead"` (used by `graph.runtime.attach_lead_runtime`);
  `_cited_handles(report, own_source=LEAD_KEY)` resolves the lead's `self:` citations instead of
  dropping them; `librarian_tools.LIBRARIAN_SOURCE = "lib"`; `LibrarianIOAgent.last_receipts` is
  every document a pass opened (the graph hands on only those its filed report cites).
- 2026-11-03 — One citing syntax (build item 9): `_ANALYST_EVIDENCE`, the report-document hints,
  `REVIEWER_EDIT_DESCRIPTION` and `read_paper`'s description and reply teach `{{handle}}`,
  `{{handle#/key}}`, `{{handle#/$args/<name>}}` and `{{handle "…"}}`; no text teaches `[handle]`.
