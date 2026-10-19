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
- `html.py` — with `html` in `report_formats`, `render_html` writes one self-contained
  `report.html` (also inside the reviewer's `turn_end`). Every number in the prose is a button (tap,
  or hover with a mouse) opening the chain behind the evidence receipt that holds it — the
  grounding check's own answer (`provenance.bindings.number_sources`). The chain is a two-column
  z-chain, data | tool: each step's inputs (an in-run file named by the step that made it, an
  `obsdb` observation, a raw file with its hash) → the tool as `<toolkit root>/<package>.<fn>`
  with its commit, the arguments it was sent (rebuilt from the run ledger,
  `provenance.calls.ledger_calls`, and proven by the receipt's hash) and a "copy reproduce code"
  button → its outputs, `◄─(environment)`. Replay is the reader's (user ruling 2026-10-16): the
  runtime re-runs nothing. Model, tool and receipt text reach the page only through
  `html.escape`; the page loads nothing external and carries one script of its own. Paragraph line
  breaks are kept (a line-start list enumerator stays exempt, as in the grounding scan); a quote's
  locator is shown only for a paper attribution (as in the .tex). A number the
  field's own citations hold opens that receipt's chain; one only the report's wider scope holds
  (a title citing nothing) opens a list of every receipt holding it, never one presented as its
  source. Tables go through `render.check_table`, the check the `.tex` uses (now also refusing a
  quote split across cells). The copy-reproduce code names the call's working folder and says that
  output arguments point into the run's folders and that a tool needing an earlier call's session
  needs that call first. Enforcement: a scanned number with no evidence receipt (a guard: the
  grounding check already refuses one), and a quote spanning two table cells, are model-fixable problems
  (nothing is written; nor while another format has one); the written page is parsed back by
  `check_page` and removed when refused — a popover target that does not exist, a duplicate id, a
  source outside the report folder, a link scheme other than http(s), a forbidden tag, an inline
  handler or style, other than one script, copy-block arguments that do not parse back, or a number
  in the reviewer's prose outside a button. A renderer fault (refused page, exception) is recorded,
  never the model's, and never stops the other format.
- `figures.py` — `stage_figure_receipts`.
- `publish.py` — after the reviewer's final report is accepted, with no model: mints `rev:dN`
  receipts for `report.tex`/`report.pdf`/`report.html`/`report.jsonl` whose parents are the receipts the report
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
- `check_table(table, number, lookup) -> (rows, note, problems)` (`render.py`) — the one table check
  both formats use (one form filled, a receipt that parses as a table, rows as wide as the columns;
  `note` = the receipt's F26 drift note).
- `render_html(report, lookup, report_dir, *, run_id, numbers, ledger, own_source="rev", write=True) ->
  HtmlResult(html_path, problems, errors, chains)` — `numbers` is `check_report_fields(...,
  sources=True).numbers`; `ledger` the run's `monitor.jsonl` (absent: the chain shows no
  arguments). `check_page(path) -> list[str]`; `reproduce_code(receipt, call) -> (code,
  args_json)`; `HTML_NAME` (`report.html`).
- `publish_report(..., html_path=None)` mints `report.html` as a `rev:` receipt like the others.

## Dependencies
- External: stdlib (`re`, `csv`, `shutil`, `subprocess`, `pathlib`, `urllib.parse`, `html`,
  `html.parser`, `ast`, `hashlib`); lualatex with
  fontspec and the DejaVu/FreeSerif fonts.
- Internal: `auto_research.provenance` (incl. `provenance.calls` for the ledger rebuild),
  `auto_research.schemas.{review,report,artifacts}`, `auto_research.env` (`env_lock_dir`, read for an
  environment's name).
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
- 2026-10-06 — [F26] receipted files read through `provenance.store.read_receipted` (read once, no symlink, hash-checked; a changed file is sent with a note and a `receipt.drift` alert). See `provenance/DOCS.md`.
- 2026-10-17 — [provenance replay, step 3] `html.py`: `report.html` with per-number provenance popovers (z-chain, rebuilt arguments, copy reproduce code), its own parse-back check; `publish_report(html_path=)`.
- 2026-10-17 — Step-3 review fixes: numbers keyed by the exact field text (whitespace around a field had unlinked them); `check_table` shared with tex (html-only had skipped the table checks); a receipt table shows its drift note; table cells rendered from their own spans (a cell holding " | " had unlinked its row); scope-only numbers list their holders; figures that are not images link instead of `<img>`; ids hashed unique; `check_page` adds duplicate ids, link schemes, forbidden tags, styles and the loose-number scan, and a refused page is removed; reproduce code checks identifiers and keeps comments one line.
- 2026-10-17 — Step-3 re-review fixes: paragraph line breaks kept (a numbered list had removed the page); a locator shown only on paper attributions; the scoped popover names the reviewer's citable set; `check_table` refuses a quote split across cells (both formats); reproduce code notes the working folder, output arguments and session state; `check_page` refuses absolute and protocol-relative links.
- 2026-10-17 — `_unbalanced` takes whole bindings out first (a quote holding braces, a dict a tool printed, had been refused).
- 2026-10-17 — `check_table` refuses a table receipt that is not evidence (user ruling: a CSV a read or the shell produced is not "a table a tool computed"), in both formats.
- 2026-10-17 — Figures are embedded in `report.html` as `data:image/…;base64` (the one file sent to a phone carries them; SVG is linked, never embedded); `check_page` accepts only those data URIs. A table receipt must be a toolkit producer's (not a read, the shell, a paper, a commit or the request).
