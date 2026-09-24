"""LangGraph state for the Phase 1 research loop."""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from auto_research.schemas import (
    AnalystReport,
    ResearchRequest,
    ReviewerReport,
)
from auto_research.schemas.literature import LiteratureReport
from auto_research.schemas.report import ResearchReport
from auto_research.schemas.artifacts import Receipt


class ResearchState(TypedDict, total=False):
    """State carried through the orchestration graph."""

    request: ResearchRequest
    # The librarian's latest filed report, and the documents it read by handle (lib:pN / lib:wN).
    # Each literature pass replaces both: a re-loop researches the reviewer's follow-up afresh.
    literature: LiteratureReport | None
    literature_receipts: dict[str, Receipt]
    report: AnalystReport | None
    # The reviewer's filed reports, one per review; the latest routes the graph, and the final one
    # is the run's report (its references are the paper receipts it cites).
    verdict: ReviewerReport | None
    verdicts: Annotated[list[ReviewerReport], operator.add]
    lit_iterations: int
    analysis_iterations: int
    max_iterations: int
    # Report artifacts: .tex/PDF rendered inside the reviewer's turn_end, JSONL by compose_report
    research_report: ResearchReport | None
    report_jsonl: Path | None
    report_latex: Path | None
    report_pdf: Path | None
