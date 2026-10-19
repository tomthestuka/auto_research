"""The librarian's report: what the research beyond the data says about a request (the literature
first, then documentation, code and community practice), bound to what was read.

The librarian writes this inside its own loop with ``edit_report`` and files it with ``turn_end``,
the same lifecycle as an analyst's report. Every claim names the captured documents that support
it by handle (``lib:p4`` a paper, ``lib:w2`` a web page), and every number sits inside a quote of
one of them, so the reviewer receives claims it can check against the very text the librarian read.

The schema is **permissive** so a partial write mid-loop is legal; completeness (a finding with no
support, a handle that resolves to nothing, an unfiled status) is ``turn_end``'s job. There is no
bibliography field: the runtime derives it from the cited captures, which carry each document's
title and persistent identifiers, so a reference is never typed by the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from auto_research.schemas.artifacts import PaperAttribution
from auto_research.schemas.base import MessageModel


class LiteratureFinding(MessageModel):
    """One claim from the research and the captured documents that support it."""

    claim: str = Field(
        description=(
            "One self-contained statement of what the documents you read report — papers, "
            "documentation, code, discussions — in prose. Attribute every number to the paper it "
            "comes from, with its place: {{lib:p3 \"value\" Table 2}}. A web page supports a statement "
            "that carries no value."
        )
    )
    support: list[str] = Field(
        default_factory=list,
        description=(
            "Handles of the documents this claim rests on, e.g. 'lib:p3' or 'lib:w1', each a "
            "document you read in this loop. At least one."
        ),
    )


class LiteratureReport(MessageModel):
    """The librarian's filed answer to a research request's research questions."""

    summary: str = Field(
        default="",
        description=(
            "A briefing of 3–5 sentences: what was searched, what was read, and the headline of "
            "what the research establishes for this request. The claims themselves go in "
            "findings."
        ),
    )
    findings: list[LiteratureFinding] = Field(
        default_factory=list,
        description=(
            "The results: one item per claim the research supports, each bound to the documents "
            "behind it."
        ),
    )
    gaps: list[str] = Field(
        default_factory=list,
        description=(
            "What this search could not settle: sub-questions no document answered, and sources "
            "that were locked, paywalled or failed to fetch, each stated plainly."
        ),
    )
    attributed_values: list[PaperAttribution] = Field(
        default_factory=list,
        description=(
            "Set by the runtime when the report is filed: every value the report attributes to a "
            "paper, with the place in it, for a person to check against the PDF. One shape across "
            "roles (PaperAttribution); title and identifiers resolve from the handle's receipt."
        ),
    )
    status: Literal["succeeded", "failed"] = Field(
        default="failed",
        description=(
            "Declare the outcome of your work: 'succeeded' when you completed the search and the "
            "report states what was found, 'failed' when you could not. An honest 'failed' is a "
            "correct, penalty-free answer."
        ),
    )
