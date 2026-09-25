# `auto_research.schemas` — inter-agent message protocol

## Purpose
Defines, in one place, every Pydantic v2 model that flows between agents. These
schemas double as the protocol: bad data fails loudly at the boundary, and the
schema files are the human-readable contract.

## Architecture
One module per logical message family:

- `base.py` — shared Pydantic configuration.
- `figures.py` — artifact references.
- `tools.py` — structured tool/shell/database action requests and reviews.
- `requests.py` — top-level human request.
- `search.py` — literature search messages.
- `analysis.py` — analyst task/report messages.
- `review.py` — reviewer verdicts.
- `toolspec.py` — the worker→lead `AnalystToolRequest`, the Phase-2 lead→coder
  tool-authoring brief (`ToolSpec`), and `AuthoringDecision` — which **no model fills any more**:
  the lead files `author_chain` or `hold_authoring` and the handler constructs it (its validator
  is kept as an assertion on the harness's own construction, where the state it rejects is
  unreachable).
- `merge.py` — Phase-2 tool-authoring flow results (gate report, co-lead review,
  `propose_and_pr` outcome).

**Every field a model fills carries `Field(description=...)`, and that is load-bearing rather than
tidy.** A `#` comment never reaches the JSON Schema (or tool `input_schema`) the model is given, so
a field explained only in a comment is a field filled blind. A live run died on exactly that
(2026-09-01, $19.43): `AuthoringDecision.tool_specs` was optional in the schema, no field carried a
description, and the rule making the chain mandatory lived in a validator the model could not see.
`tests/test_schema_descriptions.py` makes the rule permanent — it walks every model a model fills
and fails on an undescribed field. Two exemptions, both by name: models the *runtime* resolves
rather than the model (`FigureRef`), and auto-set bookkeeping (`id`, `created_at`, whose
descriptions say "set automatically; leave it alone").

All public models are re-exported from `auto_research.schemas`. Figures are
carried by **reference** (`FigureRef`), never embedded
([CLAUDE.md §7](../../../CLAUDE.md#7-schemas--protocols)).

## Interfaces / Contracts
- All schema classes inherit from `MessageModel`, which forbids unknown fields
  and validates assignment.
- All messages are JSON serializable with Pydantic v2 `model_dump(mode="json")`.
- Required free-text fields use non-empty string validation.
- `FigureRef` carries only metadata: `path`, `caption`, `mime_type`,
  `produced_by`, `created_at`, `sha256`, and optional `thumbnail_b64`.
  Figure bytes are never stored in messages.
- `FigureCitation` (V-4) is a figure *placement handle*: `source` (a catalog name — `"self"`
  for an agent's own figures, or a worker key like `"analyst_worker_0"` for one of the lead's
  per-worker catalogs) + `index` (1-based, `≥1`). An authoring agent emits these in
  `edit_report`; the runtime resolves each to a real `FigureRef`, in citation order (so the
  report-local number "Figure k" = the k-th cited). The two coordinate systems stay separate:
  `{source, index}` is the *selection* handle (the catalog menu), "Figure k" is the *reference*
  (the finished report).
- `FiguredReport` is the base for inter-LLM reports carrying figures by citation:
  `cited_figures: list[FigureCitation]` (what the model emits) + the runtime-resolved, ordered
  `figures: list[FigureRef]`. `AnalystReport` extends it; the reviewer's `ReviewerReport` does not
  (it places figures by receipt handle). Citation *is* the de-duplication — cited figures are placed and ordered, uncited catalog
  figures are dropped — so no separate figure-dedup pass exists.
- `Receipt` (`schemas/artifacts.py`, provenance P0) is the runtime's record of one artifact of
  any kind (`figure | result | data | paper | web | human | commit`). It is **minted by the
  runtime, never declared by a tool or a model**: `sha256` is computed over the stored bytes at
  `path`, and `produced_by` / `call_id` / `toolkit_commit` / `args_sha256` are stamped by the code
  that ran the work. `inputs` are the files the call read, hashed before it ran; `parents` are the
  receipts it derives from; `flags` (`failed`, `synthetic`) propagate to descendants. Its `handle`
  is `<source>:<letter><n>` (letters `f r d p w h c`, one per kind), so a handle names its kind;
  the validator refuses a handle whose letter disagrees with `kind`, and a figure receipt without
  `caption`/`mime_type`. `to_figure_ref()` is the multimodal-boundary view. Minting, lookup and
  the claim check live in `auto_research.provenance` (see its DOCS.md).
- `DocumentRef` carries only metadata: `path`, `title`, `mime_type`,
  `created_at`, and `sha256`. Document bytes are loaded only at provider-call
  boundaries for models configured with document support.
- `SearchSource` is currently one of `"arxiv"`, `"nasa_ads"`, `"web"`, or
  `"local"`.
- `SearchRequest.max_results` is bounded to `1..100`.
- `SearchResult.pdf_url` is optional and is used by source adapters that can
  offer a direct paper PDF download.
- `SearchResult.full_text_links` carries resolver/source-button candidates such
  as ADS `esource` links; `pdf_url` is the selected PDF-like download target.
- `SearchResult.local_path` (`Path | None`) is set only for records with a downloaded
  local file. Online/external search results usually leave it `None`; local retrieval
  returns stored PDF paths separately for reviewer `DocumentRef` attachments.
- `ReviewerReport.needs_more_literature` and
  `ReviewerReport.needs_analysis_rework` are the schema-level flags used by the
  Phase 1 feedback edges. Each controls **one** loop edge: `needs_more_literature`
  is the literature **re-loop** switch (the graph always runs an initial literature
  pass before the first verdict, so it is set true only to close a real literature
  gap; left false it advances to analysis), and `needs_analysis_rework` is the
  analysis **re-loop** switch. The dispatch text rides in `follow_up` regardless of
  flag — `graph._build_task` hands it to the analysis stage even on the first verdict —
  so dispatching the analysts means writing `follow_up` and keeping
  `needs_more_literature` false.
- `AnalystToolRequest` (Phase 2) is the **worker→lead** message an analyst worker raises on
  its `AnalystReport.tool_request` when the toolkit lacks a capability or an in-use tool
  misbehaves (CLAUDE.md §9.3). It carries only what the worker is qualified to state:
  `request_kind` (`"new_capability"`|`"fix_existing"`, default `new_capability`),
  `science_context`/`expected_behavior` (prose), a *suggested* `proposed_tool_name`,
  `observed_issue` (the symptom, for `fix_existing` defect reports — §9.3 L5),
  `validation_data: list[ValidationDataset]` (≥1 required — the real or marked-synthetic
  ObsID that exposed the gap), and free-text `notes` (where it may *suggest* check ideas).
  The architectural calls — `target_domain`, `disposition`, `proposed_checks` — are **not**
  here: §9.3 makes them the lead's, and excluding them keeps the worker's `AnalystReport`
  response schema free of any open map (so a GPT-backed worker can compose it under strict
  structured output, §7). `AnalystReport.tool_request: AnalystToolRequest | None`.
- `ToolSpec` (Phase 2) is the **lead→coder** tool-authoring brief the lead finalises from a
  worker's `AnalystToolRequest` and files on the `author_chain` form (CLAUDE.md §9.3). It is the
  **opening prompt of a coding session** — what an engineer reads before touching the toolkit —
  and a *brief, not a frozen contract*: `science_context`/`expected_behavior` (prose), `proposed_tool_name`,
  `target_domain` + `disposition` (`"extend_existing"`|`"new_package"`, the proposer/lead's
  call), `validation_data: list[ValidationDataset]` (≥1 required), advisory
  `proposed_checks: list[ProposedCheck]` (open-ended `kind` hint, not an enum), optional
  `replication_tolerance`, auto `id`/`created_at`, plus `request_kind` and `observed_issue`
  carried over from the request. The **committed tests, not the spec, are authoritative**;
  tolerances are lead-finalised, hence optional. `target_domain`/`disposition` are the lead's
  stated *intent*: the deterministic gate verifies where the tool actually landed from the diff
  (the touched `*_utils` packages), not from these fields, so a descriptive `target_domain`
  degrades only the coder brief — it cannot doom the gate ([A12], gate change 2026-07-09). `AnalystReport.status`
  (`"ok"`|`"failed"`|`"needs_human"`, default `"ok"`) is the §9.6 terminal state so an
  honest authoring failure is a correct outcome, never a trigger to fabricate.
- The Phase-2 **tool-authoring flow** types (`merge.py`): `GateReport` (the deterministic
  gate's verdict — `layer_a`/`layer_b: list[CheckResult]`, `passed`, `report_hash`),
  `ToolReviewVerdict` (the provider-decorrelated co-lead's code-review verdict —
  `approved`, `issues`, `checks_adequate`, `commit_summary`), and `MergeFlowResult` (what
  `merge_flow.propose_and_pr` returns — `status` ∈ `committed|failed|needs_human`,
  `branch`, `commit_sha`, `gate_report`, `review`). These make the rework loop legible
  end-to-end through the gate (step 3), provider-decorrelated review (step 4), and the
  `approve_edit` atomic commit (step 6).
- The **librarian's report** (`literature.py`): `LiteratureReport` (`summary`, `findings`, `gaps`,
  `status` ∈ `succeeded|failed`, default `failed`) and `LiteratureFinding` (`claim`, `support` —
  handles such as `lib:p3`/`lib:w1` of documents the librarian read). Permissive by design so a
  partial `edit_report` write is legal; completeness is the librarian's `turn_end`. There is no
  bibliography field: references derive from the cited receipts' `title` + `ids`.

## Dependencies
- External: `pydantic>=2`.
- Imported by: every other submodule (this is the protocol).

## Dictionary-level reference
- `MessageModel(BaseModel)` — shared base class; `extra="forbid"` and
  `validate_assignment=True`.
- `FigureRef(path: Path, caption: str, mime_type: str, produced_by: str,
  created_at: datetime, sha256: str, thumbnail_b64: str | None = None)` —
  serializable artifact image reference. `sha256` must be a 64-character
  lowercase hexadecimal digest.
- `Receipt(handle, kind, path, sha256, produced_by, created_at, call_id=None,
  toolkit_commit=None, args_sha256=None, inputs=[], parents=[], flags=[], caption=None,
  mime_type=None, title=None, ids={}, source_sha256=None, parser=None, commit_sha=None)` — the
  runtime's record of one artifact; `.source` is the handle's source; `.to_figure_ref()`.
- `InputFile(path, sha256, basis="content"|"listing", receipt=None)` — one input a call read.
- `KIND_LETTER` / `LETTER_KIND` / `HANDLE_PATTERN` / `ArtifactKind` / `ReceiptFlag` — the handle
  scheme.
- `DocumentRef(path: Path, title: str, mime_type: str, created_at: datetime,
  sha256: str)` — serializable document attachment reference. Bytes stay out of
  messages and logs.
- `document_from_file(path: str | Path, *, title: str | None = None) ->
  DocumentRef` — builds a document reference from a local file path.
- `ResearchRequest(question: str, context: str | None = None,
  constraints: list[str] = [])` — the human's top-level ask entering the
  reviewer loop.
- `SearchSource` — literal source label: `"arxiv"`, `"nasa_ads"`, `"web"`, or
  `"local"`.
- `FullTextLink(link_type: str, url: HttpUrl, title: str | None = None)` —
  candidate full-text or source link.
- `SearchRequest(query: str, sources: list[SearchSource] = [],
  max_results: int = 10, since: date | None = None)` — request to search
  literature or local memory.
- `SearchResult(source: SearchSource, title: str, url: HttpUrl | None = None,
  abstract: str | None = None, authors: list[str] = [],
  published: date | None = None, identifiers: dict[str, str] = {},
  keywords: list[str] = [], pdf_url: HttpUrl | None = None,
  full_text_links: list[FullTextLink] = [],
  local_path: Path | None = None)` — normalized metadata for one paper or
  authoritative web result. `local_path` is present only when a result has a stored local
  file.
- `AnalysisTask(question: str, instructions: str | None = None,
  literature: list[SearchResult] = [],
  previous_report: AnalystReport | None = None)` — work item sent to analyst
  workers. `previous_report` carries the prior round's result on rework iterations.
- `AnalystReport(FiguredReport)` — `summary: str`, `findings: list[str] = []`,
  `uncertainty: str | None = None`, `tool_request: AnalystToolRequest | None = None`,
  `status` — plus the inherited `cited_figures: list[FigureCitation]` and runtime-resolved
  `figures: list[FigureRef]` (V-4). Worker or lead output. `tool_request` (Phase 2) is set when
  the worker raises a toolkit gap/defect. Figures are placed by citation, not attached directly.
- `ReviewerReport` (`review.py`, 2026-09-24) — the one report the reviewer files per review
  through `edit_report`/`turn_end`: verdict fields (`accepted`, `rationale`,
  `needs_more_literature`, `needs_analysis_rework`, `follow_up`, the names the graph routes on)
  + `title`, `abstract`, `sections:
  list[ReviewerSection(heading, body)]`, `key_findings`, `figures: list[Handle]` (figure receipts,
  in 'Figure k' order), `tables: list[ReviewerTable(caption, receipt | columns+rows)]`. Permissive
  (every field defaulted); what each decision requires is checked at `turn_end`
  (`agents/reviewer_tools`). `rework` = either flag. `prose_fields() -> dict[label, str | list[str]]`
  is the single enumeration of its prose, in reading order, walked by both the quote check and the
  renderer; table rows are joined per row with `" | "`. No references field: the bibliography is
  built from the cited paper/web receipts.
- `ResearchReport(run_id, request_question, created_at, report: ReviewerReport, receipts:
  list[Receipt], artifacts: dict[str, Handle])` (`report.py`) — the machine handle
  (`report.jsonl`), written by the runtime after the final review: the filed report, every receipt
  it cites with their ancestors, the handles of the other published files by format, and
  `paper_attributions: list[PaperAttribution(handle, value, locator)]` — every value the report
  attributes to a paper, for a human to verify against the PDF.
- `ToolKind` — string tool request kind. Core categories include `"bash"`,
  `"python"`, `"research_toolkit"`, `"observation_db"`, and
  `"source_archive"`; registered tool names such as
  `"research_toolkit.xspec_utils"` are also valid.
- `ToolCallRequest(requested_by: str = "analyst_worker", kind: ToolKind,
  purpose: str = "", command: str | None = None, arguments: dict[str, Any] = {},
  working_dir: Path | None = None, timeout_s: int = 300,
  network_required: bool = False)` — structured request for sandboxed execution,
  built from a **provider-native tool call**: `kind` is the tool name and
  `arguments` is the per-tool-schema-constrained argument map (real typed values,
  e.g. a nested `indices` object — never a hand-serialized JSON string).
  `requested_by`/`purpose` are runtime metadata and default, because the model
  authors a native tool call, not this envelope. Separate from `AnalystReport`.
- `ToolCallReview(approved: bool, rationale: str, revised_request:
  ToolCallRequest | None = None)` — lead review of a proposed tool action.
- `ToolResult(tool_name: str, stdout: str, stderr: str, exit_code: int,
  figures: list[FigureRef] = [], elapsed_s: float)` — result of a sandboxed
  tool execution. Produced by the analyst worker's `SandboxToolProvider` loop, written to
  the worker's private `memory.md`, and summarized into subsequent analyst task
  instructions by the consensus layer. Figures are `FigureRef` objects returned by tools.
  `exit_code=127` indicates "tool unavailable".
- `ValidationDataset(mission: str, obsid: str, synthetic: bool = False)` — one real
  (or marked-synthetic, §9.5) input a `ToolSpec`'s checks run against.
- `ProposedCheck(kind: str, description: str, tolerance: float | None = None)` — one
  *advisory* verification check; `kind` is an open free-text hint (§9.4 open menu), not an
  enum. Every field is a closed scalar (no open map) so the enclosing `ToolSpec` stays valid
  under any provider's strict structured output.
- `AnalystToolRequest(request_kind: "new_capability"|"fix_existing" = "new_capability",
  science_context: str, expected_behavior: str, proposed_tool_name: str,
  observed_issue: str | None = None, validation_data: list[ValidationDataset] (≥1),
  notes: str | None = None)` — the worker→lead message (CLAUDE.md §9.3). Carries only what the
  worker can judge; the lead adds `target_domain`/`disposition`/`proposed_checks` when it
  finalises a `ToolSpec`. Excluding those open/architectural fields keeps `AnalystReport`'s
  response schema free of open maps for GPT workers (§7).
- `ToolSpec(id: str = auto, created_at: datetime = now, science_context: str,
  expected_behavior: str, proposed_tool_name: str, target_domain: str,
  disposition: "extend_existing"|"new_package", validation_data: list[ValidationDataset]
  (≥1), proposed_checks: list[ProposedCheck] = [], replication_tolerance: float | None = None,
  notes: str | None = None)` — Phase-2 tool-authoring brief (CLAUDE.md §9.3). `disposition`
  is the proposer/lead's call; `proposed_checks`/tolerances are advisory and lead-finalised
  (the committed tests are authoritative). `id` auto-generates (`toolspec-<hex>`) when omitted.
  Adds `request_kind: "new_capability"|"fix_existing"` (default `new_capability`) and
  `observed_issue: str | None` (the symptom, for `fix_existing` defect reports — §9.3 L5).
- `CheckResult(name: str, layer: "A"|"B", passed: bool, detail: str = "")` — one gate
  check; `detail` is the machine-readable reason fed back to the author on failure.
- `GateReport(passed: bool, layer_a: list[CheckResult] = [], layer_b: list[CheckResult] = [],
  findings: list[CheckResult] = [], reason: str = "", report_hash: str = "")` — the
  deterministic gate's verdict (§9.4); `report_hash` seeds the §9.9 atomic-commit message.
  `findings` are **non-failing** observations (the literal-array landmine) the mechanical
  gate records for the step-4 co-lead to judge — they never flip `passed`. Produced by
  `gate.run_gate` (step 3).
- `ToolReviewVerdict(approved: bool, rationale: str, issues: list[str] = [],
  checks_adequate: bool = True, commit_summary: str = "")` — the provider-decorrelated
  co-lead's code-review verdict (§9.3 step 4); distinct from the reviewer's `ReviewerReport`.
  `commit_summary` is the one-line imperative commit subject the co-lead drafts when
  approving; it becomes the §9.9 atomic-commit message (`approve_edit` falls back to the tool
  name if empty).
- `MergeFlowResult(tool_spec_id: str, status: "committed"|"failed"|"needs_human",
  branch: str | None = None, commit_sha: str | None = None, gate_report: GateReport | None = None,
  review: ToolReviewVerdict | None = None, reason: str = "")` — return type of
  `merge_flow.propose_and_pr`. `committed` ⇔ gate-pass ∧ co-lead review-pass.
- `AnalystReport.status: "ok"|"failed"|"needs_human"` (default `"ok"`) — §9.6 terminal
  state; an honest failure is a correct outcome, never a reason to fabricate.
- `HumanQuery(id, kind: "clarify_science"|"clarify_code"|"blocked", message: str,
  asked_by: str, model: str = "", created_at)` — a blocked role's raise-to-human
  clarification (§9.7); no `approve` kind (the runtime never asks a human to merge). `message`
  is the body in **Markdown** (the raising LLM composes it — the clarification channels carry
  free-form NL, so Markdown is the container, not a fixed question/context shape; connectors
  polish it per channel). `model` is the backing model id (e.g. `claude-sonnet-4-6`); the
  `sender_label` property renders `"<asked_by> (<model>)"` for connector message bodies, or just
  the role when `model` is blank. The `headline` property is a one-line summary of `message`
  (first meaningful line, capped) for notifications, the Slack thread label, the monitor, and the
  headless fail-safe.
- `HumanResponse(query_id: str, answer: str, created_at)` — the human's reply. Both are the
  typed contract behind the injectable `human_handler` and the `ask_human` tools.
- `LiteratureFinding(claim: str, support: list[str] = [])` — one claim and the handles of the
  documents behind it. Every number in `claim` sits inside a quote binding.
- `LiteratureReport(summary: str = "", findings: list[LiteratureFinding] = [], gaps: list[str] = [],
  attributed_values: list[PaperAttribution] = [], status: Literal["succeeded", "failed"] = "failed")`
  — the librarian's filed report. Field descriptions are the prose the librarian's `edit_report`
  tool shows the model; `attributed_values` is set by the runtime at filing, never by the model.
- `attributed_values` uses the shared `artifacts.PaperAttribution(handle, value, locator)` (0f's), the
  one verify-list shape across roles (human ruling); title and ids resolve from the receipt.

## Changelog
- 2026-05-26 -> 2026-06-12 — Full Pydantic v2 protocol: research/search/analysis/review messages,
  `FigureRef`/`DocumentRef`, function-call tool envelopes, sandbox results, report + LaTeX schemas.
- 2026-06-12 -> 2026-06-16 — Phase 2: `ToolSpec`/`ValidationDataset`/`ProposedCheck`,
  `AnalystReport.tool_request`/`status`, `CheckResult`, `GateReport`, `ToolReviewVerdict`,
  `MergeFlowResult`, `HumanQuery`/`HumanResponse`, `AuthoringDecision`.
- 2026-06-18 -> 2026-06-24 — `ReviewVerdict` fields described, plus `cited_paths`; worker→lead
  `AnalystToolRequest` split from lead→coder `ToolSpec`; `ProposedCheck.params` dropped (OpenAI strict).
- 2026-06-29 — `HumanQuery` carries one Markdown `message` (was `question`+`context`) + `headline`.
- 2026-07-06 -> 2026-07-09 — [A8] `AuthoringDecision.tool_specs: list[ToolSpec]` (validator:
  `should_author ⇒ ≥1`). [A12] `ToolSpec.target_domain`/`disposition` documented as advisory.
- 2026-07-14 — [V-4] `FigureCitation{source, index}` + `FiguredReport` base (`cited_figures`,
  runtime-resolved `figures`); `AnalystReport` extends it; citation replaces figure-dedup.
- 2026-09-02 — Every model-filled field carries `Field(description=...)` (a `#` comment never reaches the
  model); `AuthoringDecision` is harness-constructed from a filed form. Guard:
  `tests/test_schema_descriptions.py`.
- 2026-09-09 — F9: `AnalystReport.status` is agent-declared `Literal["succeeded", "failed"]`, default
  `failed` (was `ok`); `needs_human` retired. The lead's `submit_terminal_report` kept
  `failed`/`needs_human` (deleted 09-12).
- 2026-09-12 — A3: `AnalystReport` gained `rework`, `rationale`, `what_to_probe`; `status` regained
  `needs_human`, barred from workers by the `edit_report` surface, not the type. Conditional
  requirements checked at `turn_end`, not in the schema; a `LeadReport` subclass rejected.
- 2026-09-24 — Provenance: `Receipt` (`schemas/artifacts.py`), runtime-minted, with input hashes, parents
  and propagating `failed`/`synthetic` flags; `FigureRef` unchanged (`Receipt.to_figure_ref`).
- 2026-09-24 — `literature.py`: `LiteratureReport` + `LiteratureFinding`, filed by the librarian through
  `turn_end` into `state["literature"]`.
- 2026-09-24 — `review.py`: `ReviewerReport`, `ReviewerSection`, `ReviewerTable`. Deleted: `LatexReport`,
  `ReviewVerdict`, `ReportSection`, `Reference`; `ResearchReport` is the published envelope (filed
  report + receipt closure + artifact handles).
- 2026-09-24 — `PaperAttribution` (`handle`, `value`, `locator`), the one shape of every verify-against-
  the-PDF list (ruling: papers cited, never text-checked): `ResearchReport.paper_attributions`,
  `LiteratureReport.attributed_values`; the interim `AttributedValue` removed.
