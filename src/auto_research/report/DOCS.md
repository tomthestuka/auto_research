# `auto_research.report` — research report composition

## Purpose
Produces durable research-report artifacts after an accepted verdict: a structured
machine-readable JSONL report (for upstream planning teams) and a human-readable
LaTeX document (compiled to PDF by pdflatex). Invoked from the in-graph
`compose_report` node in `auto_research.graph.builder`.

## Architecture
Three sub-modules, each stateless and deterministic except for filesystem I/O:
- `references.py` — maps run literature paths to `Reference` objects via SQLite
  storage metadata; derives stable BibTeX-style cite keys.
- `figures.py` — symlinks figure files into the reviewer's report directory so
  the LaTeX project can reference them by relative path without copying bytes.
- `writers.py` — serializes reports (JSONL, `.tex`), validates
  `\includegraphics`/`\cite` references, and drives pdflatex compilation with
  parsed error output.

The `compose_report` graph node (in `builder.py`) orchestrates these utilities:
1. Builds references from storage + run literature.
2. For the **JSONL handle**: calls `reviewer.compose_machine_report()` then
   overwrites figures/references/metadata deterministically before serializing.
3. For the **LaTeX handle**: symlinks figures, calls
   `reviewer.compose_latex_report()` (with pydantic `LatexReport` schema),
   validates refs, compiles, and feeds errors back in a capped retry loop.

## Interfaces / Contracts
- `build_references(storage, literature_paths) -> list[Reference]` — async; calls
  `storage.initialize()` then `storage.list_papers()`. Papers matched by resolved
  `local_path`; unmatched paths fall back to file stem. Cite keys are
  first-author-surname + year, letter-disambiguated for duplicates.
- `stage_figures(figures, report_dir) -> list[tuple[FigureRef, str]]` — symlinks
  each figure into `report_dir/figures/`; returns `(figref, relpath)` pairs.
  Replaces stale symlinks in-place.
- `write_report_jsonl(report, path) -> Path` — single JSONL line via
  `model_dump_json()`.
- `write_report_latex(latex, path) -> Path` — writes `.tex` source.
- `validate_latex_refs(latex, staged_figures, references) -> list[str]` —
  returns error strings for unknown `\includegraphics` relpaths or `\cite` keys.
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
- `stage_figures` — see Interfaces.
- `write_report_jsonl`, `write_report_latex` — see Interfaces.
- `validate_latex_refs` — checks `\includegraphics{...}` relpaths against
  staged figure relpaths, and `\cite{key[,key]}` keys against reference keys.
- `compile_latex` — 60 s timeout; `capture_output=True`; looks for `*.tex` in
  `report_dir`. Sets `report.pdf` on success.

## Changelog
- 2026-06-03 -> 2026-06-12 — Report composition module created and kept stable:
  references, figure staging, JSONL/LaTeX writers, ref validation, pdflatex compile loop,
  and deterministic tests.
- 2026-06-12 — Compacted changelog; no report behavior changes.
