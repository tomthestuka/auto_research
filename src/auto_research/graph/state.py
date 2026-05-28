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


class ResearchState(TypedDict, total=False):
    """State carried through the orchestration graph."""

    request: ResearchRequest
    literature: list[Path]
    report: AnalystReport | None
    verdict: ReviewVerdict | None
    verdicts: Annotated[list[ReviewVerdict], operator.add]
    lit_iterations: int
    analysis_iterations: int
    max_iterations: int
