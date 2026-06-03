"""Report serialization, LaTeX writing, validation, and compilation."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from auto_research.schemas.figures import FigureRef
from auto_research.schemas.report import Reference, ResearchReport


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


def validate_latex_refs(
    latex: str,
    staged_figures: list[tuple[FigureRef, str]],
    references: list[Reference],
) -> list[str]:
    """Return warnings for unresolved \\includegraphics paths and \\cite keys."""
    errors: list[str] = []
    staged_paths = {relpath for _, relpath in staged_figures}
    ref_keys = {r.key for r in references}

    for m in re.finditer(r"\\includegraphics(?:\[.*?\])?\{([^}]+)\}", latex):
        p = m.group(1).strip()
        if p not in staged_paths:
            errors.append(f"\\includegraphics path not staged: {p!r}")

    for m in re.finditer(r"\\cite\{([^}]+)\}", latex):
        for key in m.group(1).split(","):
            key = key.strip()
            if key and key not in ref_keys:
                errors.append(f"\\cite key not in references: {key!r}")

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
