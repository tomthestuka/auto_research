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
- 2026-06-03 -> 2026-06-12 — Report composition module created and kept stable:
  references, figure staging, JSONL/LaTeX writers, ref validation, pdflatex compile loop,
  and deterministic tests.
- 2026-06-12 — Compacted changelog; no report behavior changes.
- 2026-06-22 — **References became verifiable local-PDF hyperlinks.** The `.tex` References
  section is now a list of `\href{papers/<name>.pdf}{…}` links the reviewer authors over the
  cited PDFs (no BibTeX / `\cite` / `thebibliography`). Added `stage_papers` (symlinks cited
  PDFs into `report_dir/papers/`) and `validate_report_links` (a hard gate that resolves every
  reference `\href` to a real on-disk PDF; a dead link re-enters the reviewer's compose loop like
  a compile error). `validate_latex_refs` narrowed to `validate_latex_figures` (figures only);
  `compose_latex_report` takes `staged_papers` instead of `references` and emits `\href` links.
  The JSONL handle keeps `build_references`/`Reference` and is correct by construction (built from
  already-validated `cited_paths`). New `stage_papers`/`validate_report_links` tests; the
  error-feedback-loop test now exercises a dead link.
- 2026-07-06 — **Deterministic `\href` display escaping — fixes the systematic no-PDF ([D1]).**
  `write_report_latex` now runs `_sanitize_href_display`, wrapping every `\href` display argument in
  `\nolinkurl{}`. The compose prompt emits `\href{<relpath>}{<relpath>}` verbatim, and staged relpaths
  are always `papers/arxiv_<id>.pdf`; the `_` in the *display* arg was typeset as a math subscript →
  `! Missing $ inserted.` → no PDF on **every** paper-citing run, and the compile-repair loop could not
  converge (its feedback contradicted the standing "verbatim" instruction). hyperref reads the URL arg
  verbatim and `\includegraphics` is fine, so only the display text needed fixing; `\nolinkurl` (hyperref,
  already loaded) typesets it verbatim while `\href` keeps the link. No prompt change. pdflatex-verified
  (raw fails, wrapped compiles). New `test_report.py` sanitizer + idempotency tests.
