"""LangGraph state for the Phase 1 research loop."""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from auto_research.schemas import (
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
)
from auto_research.schemas.report import ResearchReport


class ResearchState(TypedDict, total=False):
    """State carried through the orchestration graph."""

    request: ResearchRequest
    # PDFs the reviewer opened and cited during the final analysis review (∩ actually
    # opened). The report's References link to exactly these files.
    cited_paths: list[Path]
    report: AnalystReport | None
    verdict: ReviewVerdict | None
    verdicts: Annotated[list[ReviewVerdict], operator.add]
    lit_iterations: int
    analysis_iterations: int
    max_iterations: int
    # Report artifacts (populated by compose_report node on accepted verdicts)
    research_report: ResearchReport | None
    report_jsonl: Path | None
    report_latex: Path | None
    report_pdf: Path | None
