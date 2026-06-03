"""Report composition utilities for reviewer-authored research artifacts."""

from auto_research.report.figures import stage_figures
from auto_research.report.references import build_references
from auto_research.report.writers import (
    compile_latex,
    validate_latex_refs,
    write_report_jsonl,
    write_report_latex,
)

__all__ = [
    "build_references",
    "compile_latex",
    "stage_figures",
    "validate_latex_refs",
    "write_report_jsonl",
    "write_report_latex",
]
