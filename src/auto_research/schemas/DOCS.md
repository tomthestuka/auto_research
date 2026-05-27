# `auto_research.schemas` — inter-agent message protocol

## Purpose
Defines, in one place, every Pydantic v2 model that flows between agents. These
schemas double as the protocol: bad data fails loudly at the boundary, and the
schema files are the human-readable contract.

## Architecture
One module per logical message family (request, search, analysis, review). All
public models re-exported from `auto_research.schemas`. Figures are carried by
**reference** (`FigureRef`), never embedded ([CLAUDE.md §7](../../../CLAUDE.md#7-schemas--protocols)).

## Interfaces / Contracts
*To be populated in step 1.* Representative models per CLAUDE.md:
- `ResearchRequest` — the human's top-level ask.
- `SearchRequest` / `SearchResult` — librarian I/O.
- `AnalysisTask` / `AnalystReport` — analyst lead ↔ workers.
- `ReviewVerdict` — reviewer output.
- `FigureRef` — `path`, `caption`, `mime_type`, `produced_by`, `created_at`,
  `sha256`, optional `thumbnail_b64`.

## Dependencies
- External: `pydantic>=2`.
- Imported by: every other submodule (this is the protocol).

## Dictionary-level reference
*(none yet — populated in step 1.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.
