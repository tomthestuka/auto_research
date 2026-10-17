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


def _merge(left: dict[str, Receipt] | None, right: dict[str, Receipt] | None) -> dict[str, Receipt]:
    """Union of two handle maps (a handle names the same receipt everywhere in a run)."""
    return {**(left or {}), **(right or {})}


class ResearchState(TypedDict, total=False):
    """State carried through the orchestration graph."""

    request: ResearchRequest
    # Every literature report of the run and the documents they read by handle (lib:pN / lib:wN).
    # Accumulated, not replaced: a second search round builds on the first (human 2026-09-25).
    literature: Annotated[list[LiteratureReport], operator.add]
    literature_receipts: Annotated[dict[str, Receipt], _merge]
    report: AnalystReport | None
    # The reviewer's filed reports, one per turn; the latest routes the graph on its `assign_to`,
    # and the one sent to the human is the run's report.
    verdict: ReviewerReport | None
    verdicts: Annotated[list[ReviewerReport], operator.add]
    # What came back since the reviewer's last turn: request | librarian | analysts | human.
    arrived: str
    # The human's replies: their text as nudges (guidance, never receipts, human 2026-09-25) and
    # their files as input: receipt handles; and how many reports went to the human.
    human_nudges: Annotated[list[str], operator.add]
    human_thread: Annotated[list[str], operator.add]
    human_rounds: int
    lit_iterations: int
    analysis_iterations: int
    max_iterations: int
    # Report artifacts of the latest report to the human: .tex/PDF rendered inside the reviewer's
    # turn_end, JSONL and the rev:dN handles by compose_report
    research_report: ResearchReport | None
    report_artifacts: dict[str, str]
    report_jsonl: Path | None
    report_latex: Path | None
    report_pdf: Path | None
    report_html: Path | None
