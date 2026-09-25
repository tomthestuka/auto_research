# `auto_research.report` — rendering and publishing the reviewer's report

## Purpose
Turns the reviewer's filed `ReviewerReport` into the run's durable artifacts: a LaTeX document
compiled to PDF (the human handle) and, when configured, a JSONL machine handle — and makes each
published file a receipt, so the report is an artifact like any other (human 2026-09-24).

## Architecture
The reviewer writes plain text with receipt bindings and never writes LaTeX; this module is the
only author of LaTeX (human 2026-09-24). That split is what lets the quote check run on the very
text the model wrote.
- `render.py` — `render_report` substitutes each quote through `provenance.render_bindings`,
  escapes LaTeX's specials with one fixed map, and writes `report.tex` for **lualatex** (DejaVu
  Serif plus luaotfload fallbacks, so χ², ± and ≲ typeset as written). Figures are copied into
  `figures/<sha12><ext>` after re-hashing against their receipt ([D4]+[D5]); a tool-computed table
  is typeset from its receipt's stored bytes; references come from the cited paper/web receipts'
  titles and ids ([C4]); a provenance appendix lists every cited handle. A value attributed to a
  paper (`{{lib:p4 "value" Table 2}}` — papers are cited, never text-checked, human 2026-09-24) is
  typeset with its reference, and every such value is listed in a "Values to verify against the
  PDFs" appendix (`report_attributions`). `compile_report` runs
  lualatex twice and separates missing glyphs (the model's to fix) from errors (the renderer's).
  Both run **inside the reviewer's `turn_end`** (`agents/reviewer_tools.ReviewerRender`), so the
  final report is validated at the role's edge rather than composed after it.
- `figures.py` — `stage_figure_receipts`.
- `publish.py` — after the reviewer's final report is accepted, with no model: mints `rev:dN`
  receipts for `report.tex`/`report.pdf`/`report.jsonl` whose parents are the receipts the report
  cites (so a failed/synthetic flag propagates to the report), and writes the JSONL
  (`ResearchReport`: the filed report + the receipt closure) when `jsonl` is configured.
- `writers.py` — `write_report_jsonl` and the TeX log reader `_errors_with_context`.

The graph's `compose_report` node calls `publish_report`; the `.tex`/PDF already exist by then.

## Interfaces / Contracts
- `render_report(report, lookup, report_dir, *, run_id, own_source="rev") -> RenderResult` —
  writes `report_dir/report.tex` only when no model-fixable problem stands; `RenderResult(tex_path,
  problems, cited, references, figures)`. Problems (sentences for `turn_end`): a figure handle that is
  missing, not a figure, or whose bytes no longer match; a table filling both forms or neither; a
  table receipt that does not parse as delimited text; a built table with a ragged row.
- `compile_report(report_dir, *, compiler="lualatex", run=subprocess.run, timeout_s=120) ->
  CompileResult(pdf_path, glyph_problems, errors)` — a missing compiler is an error, never a pass.
- `stage_figure_receipts(handles, lookup, report_dir) -> (list[(Receipt, relpath)], problems)` —
  sha-named copies, verified first, deduplicated by handle.
- `parse_delimited(text) -> list[list[str]] | None` — CSV/TSV/`;`/Markdown pipe rows; `#` lines
  dropped; `None` unless header + ≥1 row, ≥2 columns, every row as wide as the header.
- `latex_escape(text) -> str` — the ten LaTeX specials; Unicode passes through.
- `report_attributions(report, lookup, *, own_source) -> list[Attribution]` — every value the report
  attributes to a paper, in reading order, each once (over `provenance.paper_attributions`).
- `publish_report(report, *, store, run_id, request_question, report_dir, formats, tex_path=None,
  pdf_path=None, own_source="rev") -> PublishResult(research_report, jsonl_path, artifacts)` —
  `artifacts` maps format → minted handle.
- `cited_handles(report, *, own_source) -> list[str]` — prose bindings, figures, table receipts, in
  reading order. `receipt_closure(store, handles) -> list[Receipt]` — cited first, then ancestors.
- `write_report_jsonl(report, path) -> Path` — one JSONL line.

## Dependencies
- External: stdlib (`re`, `csv`, `shutil`, `subprocess`, `pathlib`, `urllib.parse`); lualatex with
  fontspec and the DejaVu/FreeSerif fonts; `pdftoppm` for the reviewer's draft preview (optional).
- Internal: `auto_research.provenance`, `auto_research.schemas.{review,report,artifacts}`.
- Used by: `auto_research.agents.reviewer_tools` (render, compile), `auto_research.graph.builder`
  (`compose_report` → `publish_report`).

## Dictionary-level reference
- The functions above — see Interfaces. `RenderResult`, `CompileResult`, `PublishResult` —
  dataclasses as named there.
- `MAIN_FONT` (`DejaVu Serif`), `FALLBACK_FONTS`, `LATEX_COMPILER` (`lualatex`), `TEX_NAME`
  (`report.tex`), `MAX_TABLE_ROWS` (60; further rows are counted in a note naming the receipt),
  `JSONL_NAME` (`report.jsonl`).

## Changelog
- 2026-06-03 -> 2026-06-12 — Report composition built: references, figure staging, JSONL/LaTeX writers,
  ref validation, pdflatex compile loop.
- 2026-06-22 -> 2026-07-06 — References became `\href{papers/<name>.pdf}{…}` links (`stage_papers`,
  `validate_report_links`; `validate_latex_figures`); [D1] `_sanitize_href_display` wraps displays in
  `\nolinkurl{}`.
- 2026-08-10 — `_errors_with_context` pairs each `!` compile error with its `l.<n>` source line; the
  reviewer's preamble mandates `graphicx`/`amsmath`/`amssymb`/`hyperref` ([D6]).
- 2026-09-09 — F5e: `compose_latex_report` replaced by `ReviewerAgent.author_latex_report`, a ReAct loop
  editing `report.tex` behind a validating `turn_end` ([D1] sanitizer and [D6] preamble carried over).
  Superseded 09-24.
- 2026-09-24 — Reviewer as a unit (5c): new `render.py` (`render_report`, `compile_report`,
  `parse_delimited`, `latex_escape`), `figures.stage_figure_receipts` and `publish.py`; the harness
  renders the reviewer's plain-text report. lualatex (ruling) with a fallback font chain; closes [D4], [D5], [C4].
- 2026-09-24 — pdflatex path deleted: `references.py`, `stage_figures`, `stage_papers`,
  `write_report_latex`, `_sanitize_href_display`, `validate_latex_figures`, `validate_report_links`,
  `compile_latex`, and `config: report_retry_cap`.
- 2026-09-24 — Ruling: paper values are attributed, not quoted; the renderer adds the verify-against-
  the-PDFs list and `publish_report` writes it as `ResearchReport.paper_attributions`.
