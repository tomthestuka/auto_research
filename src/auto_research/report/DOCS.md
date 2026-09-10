# `auto_research.report` — research report composition

## Purpose
Produces durable research-report artifacts after an accepted verdict: a structured
machine-readable JSONL report (for upstream planning teams) and a human-readable
LaTeX document (compiled to PDF by pdflatex). Invoked from the in-graph
`compose_report` node in `auto_research.graph.builder`.

## Architecture
Three sub-modules, each stateless and deterministic except for filesystem I/O:
- `references.py` — maps the reviewer's cited paths to `Reference` objects via SQLite
  storage metadata (the JSONL handle; the `.tex` handle uses hyperlinks, below).
- `figures.py` — symlinks figure files into the reviewer's report directory so
  the LaTeX project can reference them by relative path without copying bytes;
  `stage_papers` does the same for cited PDFs so the References section can link them.
- `writers.py` — serializes reports (JSONL, `.tex`), validates `\includegraphics`
  paths and **resolves reference `\href` links against on-disk PDFs**, and drives
  pdflatex compilation with parsed error output.

The `compose_report` graph node (in `builder.py`) orchestrates these utilities:
1. Builds references from storage + the reviewer's `cited_paths`.
2. For the **JSONL handle**: calls `reviewer.compose_machine_report()` then
   overwrites figures/references/metadata deterministically before serializing.
   References are correct by construction (built from already-validated cited PDFs),
   so the JSONL handle needs no separate link gate.
3. For the **LaTeX handle**: symlinks figures + cited papers, calls
   `reviewer.compose_latex_report()` (with pydantic `LatexReport` schema), validates
   figure paths + **resolves every reference `\href` link to a real PDF (hard reject)**,
   compiles, and feeds errors back in a capped retry loop — a dead link is fed back
   exactly like a compile error.

## Interfaces / Contracts
- `build_references(storage, cited_paths) -> list[Reference]` — async; calls
  `storage.initialize()` then `storage.list_papers()`. Papers matched by resolved
  `local_path`; unmatched paths fall back to file stem. Used for the JSONL handle.
- `stage_figures(figures, report_dir) -> list[tuple[FigureRef, str]]` — symlinks
  each figure into `report_dir/figures/`; returns `(figref, relpath)` pairs.
  Replaces stale symlinks in-place.
- `stage_papers(cited_paths, report_dir) -> list[tuple[Path, str]]` — symlinks each
  existing cited PDF into `report_dir/papers/`; returns `(path, relpath)` pairs. The
  relpath (`papers/<name>.pdf`) is what the reviewer hyperlinks in References; missing
  sources are skipped so a dangling link is never offered as valid.
- `write_report_jsonl(report, path) -> Path` — single JSONL line via
  `model_dump_json()`.
- `write_report_latex(latex, path) -> Path` — writes `.tex` source, first wrapping every
  `\href` display argument in `\nolinkurl{}` (`_sanitize_href_display`) so a staged relpath's
  underscore in the display text does not trip `! Missing $ inserted.`. URL arg and
  `\includegraphics` are left untouched; idempotent.
- `validate_latex_figures(latex, staged_figures) -> list[str]` — returns error
  strings for `\includegraphics` relpaths that were not staged (names the valid relpaths).
- `validate_report_links(latex, report_dir, staged_papers) -> list[str]` — the **hard
  reference-link gate**: every `\href` target into `papers/` (or ending `.pdf`) must
  resolve under `report_dir` to a real PDF; a miss returns an actionable error naming the
  target and the valid staged relpaths.
- `compile_latex(report_dir, *, compiler="pdflatex") -> tuple[bool, list[str]]`
  — runs pdflatex non-interactively; parses `!`-prefixed error lines. Returns
  `(True, [])` when compiler absent so the loop degrades to ref-checks only.

## Dependencies
- External: stdlib (`re`, `shutil`, `subprocess`, `pathlib`).
- Internal: `auto_research.schemas.figures.FigureRef`,
  `auto_research.schemas.report.{Reference, ResearchReport}`,
  `auto_research.storage.Storage`.
- Used by: `auto_research.graph.builder` (`compose_report` node).

## Dictionary-level reference
- `build_references` — see Interfaces.
- `stage_figures`, `stage_papers` — see Interfaces.
- `write_report_jsonl`, `write_report_latex` — see Interfaces.
- `validate_latex_figures` — checks `\includegraphics{...}` relpaths against staged
  figure relpaths.
- `validate_report_links` — checks reference `\href{...}` targets resolve to staged
  on-disk PDFs (hard reject on a miss).
- `compile_latex` — 60 s timeout; `capture_output=True`; looks for `*.tex` in
  `report_dir`. Sets `report.pdf` on success.

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
