"""The run's published report: the reviewer's filed report plus the receipts it rests on."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field

from auto_research.schemas.artifacts import Handle, PaperAttribution, Receipt
from auto_research.schemas.base import MessageModel
from auto_research.schemas.review import ReviewerReport


NonEmptyStr = Annotated[str, Field(min_length=1)]


class ResearchReport(MessageModel):
    """Machine handle of the run's report (``report.jsonl``), written by the runtime, never a model.

    ``report`` is exactly what the reviewer filed through ``turn_end``; ``receipts`` is every
    receipt it cites together with their ancestors, so a reader can follow each number back to the
    artifact and the tool call that produced it without the run directory's index. ``artifacts``
    names the receipts of the other published files (``tex``, ``pdf``) by format.
    ``paper_attributions`` lists every value attributed to a paper: those were cited, never
    text-checked (human 2026-09-24), so this is the list a human verifies against the PDFs.
    """

    run_id: str
    request_question: NonEmptyStr
    created_at: datetime
    report: ReviewerReport
    receipts: list[Receipt] = Field(default_factory=list)
    artifacts: dict[str, Handle] = Field(default_factory=dict)
    paper_attributions: list[PaperAttribution] = Field(default_factory=list)
