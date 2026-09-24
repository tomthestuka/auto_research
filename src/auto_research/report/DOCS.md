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
- 2026-06-03 -> 2026-06-12 — Report composition module built: references, figure staging,
  JSONL/LaTeX writers, ref validation, pdflatex compile loop.
- 2026-06-22 — References became verifiable local-PDF hyperlinks: the `.tex` References section is
  `\href{papers/<name>.pdf}{…}` links over cited PDFs (no BibTeX/`\cite`). Added `stage_papers`
  and `validate_report_links` (hard gate resolving every reference `\href` to a real on-disk PDF,
  a dead link re-entering the compose loop like a compile error); `validate_latex_refs` narrowed
  to `validate_latex_figures`; `compose_latex_report` takes `staged_papers`. The JSONL handle
  keeps `build_references`/`Reference`.
- 2026-07-06 — Deterministic `\href` display escaping ([D1]): `write_report_latex` runs
  `_sanitize_href_display`, wrapping each `\href` display argument in `\nolinkurl{}`, fixing the
  `_`-as-math-subscript `! Missing $ inserted.` that produced no PDF on every paper-citing run.
  No prompt change.

- 2026-08-10 — **A one-symbol LaTeX failure cost a whole run's PDF; the retry could not see which
  symbol.** Q21 (`8c75990e`) finished accepted, with `report.tex` and no `report.pdf`: both compile
  attempts died on `! Undefined control sequence.` at `\lesssim`, `report_retry_cap: 2` was exhausted,
  and the loop accepted the residual errors. Two independent causes, both fixed. (1) `compile_latex`
  extracted only `!`-prefixed lines, discarding the `l.<n>` source line that is the *only* place TeX
  names the offending macro — so the retry was told the error class and not the error, and re-emitted
  the same document. `_errors_with_context` now pairs each `!` message with its context line, stopping
  the lookahead at the next `!` so one error's context cannot be attributed to another. (2) The preamble
  is written by the reviewer model, not a code template, and the only instruction was "load hyperref";
  `graphicx`/`amsmath`/`amssymb`/`hyperref` are now mandated in both places that carry it
  (`prompts/reviewer.md` and the `output_format_instructions` in `agents/roles.py`), with the reason
  stated so the model extends the list when a symbol needs more. `amssymb.sty` was installed the whole
  time — this was never an environment gap. Tests: `tests/test_report.py::test_compile_errors_carry_the_offending_source_line`
  plus the no-context and no-leak cases.

- 2026-09-09 — **F5e: the reviewer authors `report.tex` by editing it, not by recomposing it.**
  `compose_latex_report` asked for a whole `LatexReport` through structured output on every
  attempt and fed the previous attempt's compile errors back as prose, so each attempt produced a
  fresh document — which is how [D7] happened (the retry saw `! Undefined control sequence.` with
  no macro named and re-emitted the same document). It is replaced by
  `ReviewerAgent.author_latex_report`, a real ReAct loop with `file_edit` on that one file plus a
  validating `turn_end`; the validator is the door (figures resolve, links resolve, pdflatex
  compiles) and a rejection names the errors and keeps the loop going. Two motives: an edit keeps
  every fix that already landed, and nothing here declares an output schema —
  `ResearchReport`/`LatexReport` are structured output, and the contrast arm's reviewer
  (`qwen3.8-max`) is recorded as failing that path at every reasoning effort (2026-08-24:
  unresolved `$ref` in `list[Model]` fields, so it invented a shape and returned HTTP 200).
  `compose_machine_report` (the JSONL handle) is **untouched** — a machine-readable artifact is
  where a schema belongs, and replacing it was not part of the ruling.
  **Two fixes were nearly lost in the move and are carried explicitly, both of them past
  run-killers no test covers:** [D1]'s `\href`-display sanitization lived in
  `write_report_latex`, which a direct file write bypasses, so the validator re-applies it as
  mechanical formatting before validating; and [D6]'s mandated preamble
  (`graphicx, amsmath, amssymb, hyperref`) plus the references no-fabrication clause lived in the
  retired method's `output_format_instructions`, and are restored on the new payload as
  `format_requirements`. `\lesssim` without `amssymb` exhausted `report_retry_cap` on Q21 and the
  run finished with a `report.tex` and no PDF; the changelog records that the prompt-side mandate
  in `prompts/reviewer.md` was **not** sufficient on its own.
- 2026-09-24 — **Reviewer-as-unit renderer (5c; steps 1–3 of the reviewer refactor).** New
  `render.py` (`render_report`, `compile_report`, `parse_delimited`, `latex_escape`) and
  `figures.stage_figure_receipts`. The human's design: the reviewer writes one plain-text report
  like every other unit and the harness renders the `.tex` from it once verified, so quotes are
  checked on the text the model wrote rather than on escaped LaTeX. lualatex replaces pdflatex on
  this path (human ruling) with a fallback font chain; measured, DejaVu Serif alone lacks ≲
  (U+2272). Closes [D4] (sha-named staging) and [D5] (re-hash before staging) and [C4] (references
  from receipt ids) on the new path. The old pdflatex path is unchanged until the graph switches.
  Tests: `tests/test_reviewer_report.py`.
- 2026-09-24 (second) — **The pdflatex path is deleted and the report is published as receipts
  (5c; steps 4–5).** Removed: `references.py` (`build_references`), `stage_figures`,
  `stage_papers`, `write_report_latex` + `_sanitize_href_display`, `validate_latex_figures`,
  `validate_report_links`, `compile_latex`. The reviewer no longer writes LaTeX, so the href
  sanitizer, the figure-path and link validators and the separate compile loop have nothing left
  to guard; their jobs moved into `render.py`, where the harness writes every `\includegraphics`
  and `\href` itself. New `publish.py`. `config: report_retry_cap` removed with the loop it bounded.
- 2026-09-24 (third) — Paper values are attributed, not quoted (human ruling, option A): the
  renderer typesets an attribution with its reference and adds the verify-against-the-PDFs list;
  `publish_report` writes the same list as `ResearchReport.paper_attributions`. `render_bindings` is
  no longer used here (an attribution needs its handle beside the value).
