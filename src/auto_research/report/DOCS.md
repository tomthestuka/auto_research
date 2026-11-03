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
  arguments). `check_page(path) -> list[str]`; `reproduce_code(receipt, call, lookup=None) -> (code,
  args_json)` (a `$ref` argument is resolved in the code from the parent's stored bytes; the
  toolkit runs at the receipt's stamped commit — the checkout when its HEAD is that commit, else
  that commit extracted with `git archive` into a temp folder; an in-process call with no recorded
  module imports through its package's `TOOL_MANIFEST`); `HTML_NAME` (`report.html`).
- A value reference `{{h#/pointer}}` renders at `provenance.bindings.display_value` (`2.48e7` as
  2.48×10⁷ in HTML, `$2.48\times10^{7}$` in TeX); in HTML it opens a value-first popover (the full
  value, the field, the result's chain one click further). A chain step shows its arguments as
  `key = value` (a `$ref` argument as the value it carried and its source), inputs by name, and
  hashes, commit and environment fingerprint in a collapsed **verify** section.
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
Compacted 2026-10-19 (every entry); detail in git history.

- 2026-06-03 -> 2026-08-10 — pdflatex report: references, figure staging, JSONL/LaTeX writers, compile
  loop, `\href` paper links, [D1] `_sanitize_href_display`, `_errors_with_context`, [D6] preamble.
- 2026-09-09 — F5e: `compose_latex_report` → `ReviewerAgent.author_latex_report` (superseded 09-24).
- 2026-09-24 — Reviewer as a unit (5c): `render.py` (`render_report`, `compile_report`, `parse_delimited`,
  `latex_escape`), `figures.stage_figure_receipts`, `publish.py`; lualatex (ruling); closes [D4], [D5],
  [C4]. Deleted `references.py`, `stage_figures`, `stage_papers`, `write_report_latex`,
  `_sanitize_href_display`, `validate_latex_figures`, `validate_report_links`, `compile_latex`,
  `report_retry_cap`. Ruling: paper values attributed (`paper_attributions`).
- 2026-10-06 — [F26] receipted files read through `provenance.store.read_receipted`.
- 2026-10-17 — Provenance replay step 3 (+ two review rounds): `html.py`, `report.html` with per-number
  popovers, `publish_report(html_path=)`, `check_page`, `check_table` (both formats), `_unbalanced`.
  User ruling: a table receipt must be evidence. Figures embedded as `data:image/…;base64`, SVG linked.
- 2026-10-26 — `reproduce_code(lookup=)`: arguments passed by `$ref` stay references in the JSON and
  a `_ref` resolver in the notebook lines reads each from the run's receipt store, so the rerun
  receives the values the call did (driven by `tests/test_refs.py`).
- 2026-10-26 — Review round 1: the chain's data column shows each result a step took by `$ref`
  (`_ref_html`, "passed by $ref, step n"); the reproduce resolver reads the returned-value blob
  and asserts its sha256 before use.
- 2026-10-26 — Review round 2: `check_page` reads the two reproduce shapes as exact alternatives, and a
  call with references must use the `_ref(...)` shape.
- 2026-10-27 — Value references render at their display value with a value-first popover (HTML)
  and typeset in TeX; chain steps show arguments as values and fold hashes into **verify**; the
  reproduce code runs the toolkit at the result's commit and imports an in-process call through
  its manifest (run faraday-refs-20261026: 218 of 249 blocks ran the live checkout; the lead's
  `data_catalog` block raised `NameError`). `_unbalanced` reads value references.
- 2026-10-27 — Stateless review (confirmed, fixed): the reproduce code evicts the toolkit package from
  `sys.modules` (a second block ran the first block's commit), extracts the commit when the checkout
  is dirty, names git's reason when a commit cannot be read, and falls back for a Python without
  extraction filters; only a float value is typeset as ×10ⁿ (`source1` read as `sourc×10¹`); a
  nested `$ref` argument shows its value; one `_Resolved` per page.
