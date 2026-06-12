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
- `SearchResult.local_path` (`Path | None`) is populated by
  `LibrarianSearcherAgent` for results that have a downloaded PDF in the local
  papers directory. It stays `None` for online/external results returned by
  `LibrarianIOAgent`. The reviewer loads these PDFs as `DocumentRef` attachments
  at the model-call boundary.
- `ReviewVerdict.needs_more_literature` and
  `ReviewVerdict.needs_analysis_rework` are the schema-level flags used by the
  Phase 1 feedback edges.
- `ToolSpec` (Phase 2) is the **tool-authoring brief** an analyst worker emits when the
  toolkit lacks a capability (CLAUDE.md §9.3). It is a *brief, not a frozen contract*:
  `science_context`/`expected_behavior` (prose), `proposed_tool_name`, `target_domain`
  + `disposition` (`"extend_existing"`|`"new_package"`, the proposer/lead's call),
  `validation_data: list[ValidationDataset]` (≥1 required — a real or marked-synthetic
  ObsID), advisory `proposed_checks: list[ProposedCheck]` (open-ended `kind` hint, not an
  enum), optional `replication_tolerance`, auto `id`/`created_at`. The **committed tests,
  not the spec, are authoritative**; tolerances are lead-finalised, hence optional.
  `AnalystReport.tool_request: ToolSpec | None` is how a worker raises it.

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
  authoritative web result. `local_path` is set by `LibrarianSearcherAgent`
  for locally stored papers.
- `AnalysisTask(question: str, instructions: str | None = None,
  literature: list[SearchResult] = [],
  previous_report: AnalystReport | None = None)` — work item sent to analyst
  workers. `previous_report` carries the prior round's result on rework iterations.
- `AnalystReport(summary: str, findings: list[str] = [],
  figures: list[FigureRef] = [], uncertainty: str | None = None,
  tool_request: ToolSpec | None = None)` — worker or lead output, including figure
  references. `tool_request` (Phase 2) is set when the worker proposes a new/extended tool.
- `ReviewVerdict(accepted: bool, rationale: str,
  needs_more_literature: bool = False, needs_analysis_rework: bool = False,
  follow_up: str | None = None)` — reviewer decision and optional feedback
  request.
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
  tool execution. Produced by `AnalystWorkerAgent.execute_tools()`, written to
  the worker's private `memory.md`, and summarized into subsequent analyst task
  instructions by the consensus layer. Figures are `FigureRef` objects parsed
  from tool stdout. `exit_code=127` indicates "tool unavailable" (Phase 2 stub).
- `ValidationDataset(mission: str, obsid: str, synthetic: bool = False)` — one real
  (or marked-synthetic, §9.5) input a `ToolSpec`'s checks run against.
- `ProposedCheck(kind: str, description: str, params: dict[str, Any] = {},
  tolerance: float | None = None)` — one *advisory* verification check; `kind` is an
  open free-text hint (§9.4 open menu), not an enum.
- `ToolSpec(id: str = auto, created_at: datetime = now, science_context: str,
  expected_behavior: str, proposed_tool_name: str, target_domain: str,
  disposition: "extend_existing"|"new_package", validation_data: list[ValidationDataset]
  (≥1), proposed_checks: list[ProposedCheck] = [], replication_tolerance: float | None = None,
  notes: str | None = None)` — Phase-2 tool-authoring brief (CLAUDE.md §9.3). `disposition`
  is the proposer/lead's call; `proposed_checks`/tolerances are advisory and lead-finalised
  (the committed tests are authoritative). `id` auto-generates (`toolspec-<hex>`) when omitted.

## Changelog
- 2026-05-26 → 2026-06-09 — **Full Pydantic v2 protocol built.** `ResearchRequest`,
  `SearchResult` (`pdf_url`/`full_text_links`/`local_path`), `AnalysisTask`/`AnalystReport`,
  `ReviewVerdict`, `FigureRef`/`DocumentRef`, `ToolCallRequest` (`arguments: dict[str,Any]`
  for native function-call values; `requested_by`/`purpose` defaulted), `ToolResult`
  (exit_code 127 = unknown tool), report schemas (`ReportSection`, `Reference`,
  `ResearchReport`, `LatexReport`).
- 2026-06-12 — Clarified `LatexReport` docstring: `latex` is a full document in one
  structured call; `max_tokens` must hold it (raised to 16384 in `config/models.yaml`).
  `\\` in truncation errors is correct single JSON escaping. Phase 2 step 1: `ToolSpec`
  brief (`schemas/toolspec.py`) — `ToolSpec` + `ValidationDataset` + `ProposedCheck`;
  `AnalystReport.tool_request: ToolSpec | None`; round-trip + validation tests added.
