"""Report composition utilities for reviewer-authored research artifacts."""

from auto_research.report.figures import stage_figures, stage_papers
from auto_research.report.references import build_references
from auto_research.report.writers import (
    compile_latex,
    validate_latex_figures,
    validate_report_links,
    write_report_jsonl,
    write_report_latex,
)

__all__ = [
    "build_references",
    "compile_latex",
    "stage_figures",
    "stage_papers",
    "validate_latex_figures",
    "validate_report_links",
    "write_report_jsonl",
    "write_report_latex",
]
