"""Publish the reviewer's accepted report: the JSONL handle, and receipts for every file written.

Runs after the reviewer's ``turn_end`` accepted a final report, with no model in it. The ``.tex``
and PDF already exist (rendered and compiled inside that ``turn_end``); what is left is the
machine handle, when configured, and the receipts that make the report itself an artifact: each
published file is minted ``rev:dN`` with the receipts the report cites as its parents, so a flag
on any of them (failed, synthetic) propagates to the report (human 2026-09-24: a PDF on disk is
one form of artifact).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from auto_research.provenance import ReceiptStore, parse_bindings
from auto_research.report.writers import write_report_jsonl
from auto_research.schemas.artifacts import Receipt
from auto_research.report.render import report_attributions
from auto_research.schemas.report import ResearchReport
from auto_research.schemas.review import ReviewerReport

JSONL_NAME = "report.jsonl"


def cited_handles(report: ReviewerReport, *, own_source: str) -> list[str]:
    """Every receipt the report cites, in reading order: prose bindings, figures, table receipts."""
    handles: list[str] = []
    for value in report.prose_fields().values():
        for text in [value] if isinstance(value, str) else value:
            handles.extend(b.handle for b in parse_bindings(text, own_source=own_source))
    handles.extend(report.figures)
    handles.extend(t.receipt for t in report.tables if t.receipt)
    return list(dict.fromkeys(handles))


def receipt_closure(store: ReceiptStore, handles: Sequence[str]) -> list[Receipt]:
    """The cited receipts and all their ancestors, each once, cited ones first."""
    seen: dict[str, Receipt] = {}
    for handle in handles:
        receipt = store.get(handle)
        if receipt is not None:
            seen.setdefault(handle, receipt)
    for handle in list(seen):
        for ancestor in store.ancestors(handle):
            seen.setdefault(ancestor.handle, ancestor)
    return list(seen.values())


@dataclass
class PublishResult:
    research_report: ResearchReport | None = None
    jsonl_path: Path | None = None
    artifacts: dict[str, str] = field(default_factory=dict)


def publish_report(
    report: ReviewerReport,
    *,
    store: ReceiptStore,
    run_id: str,
    request_question: str,
    report_dir: Path,
    formats: Sequence[str],
    tex_path: Path | None = None,
    pdf_path: Path | None = None,
    own_source: str = "rev",
) -> PublishResult:
    """Mint receipts for the rendered files and write the JSONL handle when ``jsonl`` is configured."""
    cited = cited_handles(report, own_source=own_source)
    result = PublishResult()
    for fmt, path in (("tex", tex_path), ("pdf", pdf_path)):
        if path is not None and Path(path).is_file():
            receipt = store.mint(source=own_source, kind="data", produced_by="report_renderer",
                                 path=Path(path), parents=cited)
            result.artifacts[fmt] = receipt.handle
    if "jsonl" in formats:
        machine = ResearchReport(
            run_id=run_id,
            request_question=request_question,
            created_at=datetime.now(UTC),
            report=report,
            receipts=receipt_closure(store, cited),
            artifacts=dict(result.artifacts),
            paper_attributions=report_attributions(report, store, own_source=own_source),
        )
        result.jsonl_path = write_report_jsonl(machine, report_dir / JSONL_NAME)
        result.research_report = machine
        receipt = store.mint(source=own_source, kind="data", produced_by="report_renderer",
                             path=result.jsonl_path, parents=cited)
        result.artifacts["jsonl"] = receipt.handle
    return result
