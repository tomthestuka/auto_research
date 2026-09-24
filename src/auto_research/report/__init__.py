"""Report rendering and publishing for the reviewer's filed report."""

from auto_research.report.figures import stage_figure_receipts
from auto_research.report.publish import PublishResult, cited_handles, publish_report, receipt_closure
from auto_research.report.render import (
    CompileResult,
    RenderResult,
    compile_report,
    latex_escape,
    parse_delimited,
    render_report,
)
from auto_research.report.writers import write_report_jsonl

__all__ = [
    "CompileResult",
    "PublishResult",
    "RenderResult",
    "cited_handles",
    "compile_report",
    "latex_escape",
    "parse_delimited",
    "publish_report",
    "receipt_closure",
    "render_report",
    "stage_figure_receipts",
    "write_report_jsonl",
]
