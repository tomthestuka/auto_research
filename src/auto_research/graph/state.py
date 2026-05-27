"""LangGraph state for the Phase 1 research loop."""

from __future__ import annotations

from typing import TypedDict

from auto_research.schemas import (
    AnalystReport,
    ResearchRequest,
    ReviewVerdict,
    SearchResult,
)


class ResearchState(TypedDict, total=False):
    """State carried through the orchestration graph."""

    request: ResearchRequest
    literature: list[SearchResult]
    report: AnalystReport | None
    verdict: ReviewVerdict | None
    lit_iterations: int
    analysis_iterations: int
    max_iterations: int
