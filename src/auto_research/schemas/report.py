"""Report schemas for reviewer-authored research artifacts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel
from auto_research.schemas.figures import FigureRef


NonEmptyStr = Annotated[str, Field(min_length=1)]


class ReportSection(MessageModel):
    """One section of a research report."""

    heading: NonEmptyStr
    body: NonEmptyStr


class Reference(MessageModel):
    """Bibliographic reference for a research report."""

    key: NonEmptyStr
    title: NonEmptyStr
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    source: str | None = None
    local_path: Path | None = None


class ResearchReport(MessageModel):
    """Machine-handle research report: structured JSONL with figures/refs by reference."""

    run_id: str
    request_question: NonEmptyStr
    title: NonEmptyStr
    abstract: NonEmptyStr
    sections: list[ReportSection] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    figures: list[FigureRef] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    verdict_rationale: NonEmptyStr
    created_at: datetime


class LatexReport(MessageModel):
    """Human-handle research report: LLM-authored LaTeX source.

    The model emits {"latex": "..."} with backslashes escaped as required by
    JSON (each LaTeX backslash becomes \\\\ in the JSON string literal).
    """

    latex: NonEmptyStr
