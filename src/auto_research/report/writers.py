"""Report serialization, LaTeX writing, validation, and compilation."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from auto_research.schemas.figures import FigureRef
from auto_research.schemas.report import ResearchReport


def write_report_jsonl(report: ResearchReport, path: Path) -> Path:
    """Write the machine report as a single JSONL line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json() + "\n", encoding="utf-8")
    return path


def write_report_latex(latex: str, path: Path) -> Path:
    """Write the LaTeX source."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(latex, encoding="utf-8")
    return path


def validate_latex_figures(
    latex: str,
    staged_figures: list[tuple[FigureRef, str]],
) -> list[str]:
    """Return errors for `\\includegraphics` paths that were not staged."""
    errors: list[str] = []
    staged_paths = {relpath for _, relpath in staged_figures}
    relpaths = ", ".join(sorted(staged_paths)) or "(none)"

    for m in re.finditer(r"\\includegraphics(?:\[.*?\])?\{([^}]+)\}", latex):
        p = m.group(1).strip()
        if p not in staged_paths:
            errors.append(f"\\includegraphics path not staged: {p!r}. Use one of: {relpaths}")

    return errors


def validate_report_links(
    latex: str,
    report_dir: Path,
    staged_papers: list[tuple[Path, str]],
) -> list[str]:
    """Hard reference-link gate: every `\\href` to a staged paper must resolve to a PDF.

    Parses `\\href{<target>}{...}` from the composed LaTeX and, for each target that points
    into ``papers/`` (the staged-paper area), resolves ``report_dir/target`` (following the
    symlink) and asserts it exists and is a real PDF. A miss is a hard reject — the message
    names the offending target and lists the valid staged relpaths so the reviewer can fix it.
    """
    errors: list[str] = []
    valid = ", ".join(sorted(relpath for _, relpath in staged_papers)) or "(none)"

    for m in re.finditer(r"\\href\{([^}]+)\}", latex):
        target = m.group(1).strip()
        # Only gate links into the staged-paper area; leave other hyperlinks alone.
        if not (target.startswith("papers/") or target.endswith(".pdf")):
            continue
        resolved = (report_dir / target).resolve()
        ok = resolved.is_file()
        if ok:
            try:
                ok = b"%PDF-" in resolved.read_bytes()[:1024]
            except OSError:
                ok = False
        if not ok:
            errors.append(
                f"reference link does not resolve to a PDF on disk: {target!r}. "
                f"Use one of these exact staged paper paths: {valid}"
            )

    return errors


def compile_latex(
    report_dir: Path,
    *,
    compiler: str = "pdflatex",
) -> tuple[bool, list[str]]:
    """Run pdflatex non-interactively in report_dir; return (success, error_lines).

    Parses !-prefixed error lines from pdflatex output. Returns (True, []) when
    the compiler is not on PATH so the feedback loop degrades to ref-checks only.
    """
    if not shutil.which(compiler):
        return True, []

    tex_files = list(report_dir.glob("*.tex"))
    if not tex_files:
        return True, []

    try:
        result = subprocess.run(
            [compiler, "-interaction=nonstopmode", "-halt-on-error", tex_files[0].name],
            cwd=report_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, ["pdflatex timed out after 60 s"]

    if result.returncode == 0:
        return True, []

    errors = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("!")
    ]
    if not errors:
        errors = [f"pdflatex exited with code {result.returncode}"]
    return False, errors
