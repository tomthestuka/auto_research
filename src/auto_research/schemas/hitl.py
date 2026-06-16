"""Raise-to-human clarification protocol (CLAUDE.md §9.7/§9.8).

A :class:`HumanQuery` is what *any* role emits when it is blocked and needs a real human —
the cross-cutting clarification channel, decoupled from toolkit merging. A
:class:`HumanResponse` is the human's reply. They are the typed contract behind the
injectable ``human_handler`` (and the connectors in :mod:`auto_research.hitl`); the SDK
author's ``ask_human`` and every in-graph role's ``ask_human`` tool route through them.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]

# Why a role is raising (CLAUDE.md §9.7): a science clarification, a code/convention
# clarification, or a hard block (missing data, broken dependency, contradictory request).
# There is no ``approve`` kind — the runtime never asks a human to merge.
HumanQueryKind = Literal["clarify_science", "clarify_code", "blocked"]


class HumanQuery(MessageModel):
    """A blocked role's clarification request to a real human."""

    id: NonEmptyStr = Field(default_factory=lambda: f"hq-{uuid4().hex[:12]}")
    kind: HumanQueryKind = "blocked"
    question: NonEmptyStr
    context: str = ""
    asked_by: NonEmptyStr  # the role raising it: "analyst_worker", "analyst_lead", "coder", …
    model: str = ""  # the model backing that role, e.g. "claude-sonnet-4-6" (blank if unknown)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def sender_label(self) -> str:
        """Human-readable sender: ``"analyst_worker (claude-sonnet-4-6)"`` when the model
        is known, else just the role. Used by every connector's message body."""
        return f"{self.asked_by} ({self.model})" if self.model else self.asked_by


class HumanResponse(MessageModel):
    """The human's reply to a :class:`HumanQuery`."""

    query_id: NonEmptyStr
    answer: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
