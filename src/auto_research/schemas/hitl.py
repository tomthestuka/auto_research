"""Raise-to-human clarification protocol (CLAUDE.md §9.7/§9.8).

A :class:`HumanQuery` is what *any* role emits when it is blocked and needs a real human —
the cross-cutting clarification channel, decoupled from toolkit merging. A
:class:`HumanResponse` is the human's reply. They are the typed contract behind the
injectable ``human_handler`` (and the connectors in :mod:`auto_research.hitl`); the SDK
author's ``ask_human`` and the roles' human-bound messages route through them.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field

from auto_research.schemas.artifacts import Handle
from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]

# Why a role is raising (CLAUDE.md §9.7): a science clarification, a code/convention
# clarification, or a hard block (missing data, broken dependency, contradictory request).
# ``report`` is a filed report sent to the human for discussion. There is no ``approve`` kind —
# the runtime never asks a human to merge.
HumanQueryKind = Literal["clarify_science", "clarify_code", "blocked", "report"]


class HumanAttachment(MessageModel):
    """One file travelling with a query or a reply, by reference (never the bytes).

    Outbound, the runtime builds it from a receipt (``hitl.attachments.resolve_attachments``), so
    ``handle`` names the receipt and ``path`` is the runtime's own copy of the bytes; a model
    supplies only the handle. Inbound, a connector saves the human's file under the query's
    ``reply_dir`` and leaves ``handle`` unset until the runtime mints it.
    """

    handle: Handle | None = None
    path: Path
    filename: NonEmptyStr  # what the human sees as the file name
    mime_type: NonEmptyStr
    caption: str = ""


class HumanQuery(MessageModel):
    """A blocked role's clarification request to a real human.

    The body is a single **Markdown** ``message`` the raising LLM composes — the clarification
    channels carry free-form natural language, so Markdown (which every LLM writes natively and
    every surface can render) is the container, not a fixed question/context shape. Connectors
    polish it per channel (CLI via ``rich``, Slack via a ``markdown`` block); ``kind`` stays as a
    small routing/label enum the code uses, and ``headline`` is the one-line summary for
    notifications, thread labels, and the headless fail-safe.
    """

    id: NonEmptyStr = Field(default_factory=lambda: f"hq-{uuid4().hex[:12]}")
    kind: HumanQueryKind = "blocked"
    message: NonEmptyStr  # the body, in Markdown — what the role wants to tell/ask the human
    asked_by: NonEmptyStr  # the role raising it: "analyst_worker", "analyst_lead", "coder", …
    model: str = ""  # the model backing that role, e.g. "claude-sonnet-4-6" (blank if unknown)
    attachments: list[HumanAttachment] = Field(default_factory=list)
    # Where a connector saves the files the human replies with; None = it saves none.
    reply_dir: Path | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def sender_label(self) -> str:
        """Human-readable sender: ``"analyst_worker (claude-sonnet-4-6)"`` when the model
        is known, else just the role. Used by every connector's message body."""
        return f"{self.asked_by} ({self.model})" if self.model else self.asked_by

    @property
    def headline(self) -> str:
        """A one-line summary of ``message`` for notifications, the Slack thread label, the
        monitor, and the headless fail-safe — the first non-empty line, stripped of Markdown
        heading/quote/list markers and capped so it stays a label, not a paragraph."""
        for raw in self.message.splitlines():
            line = raw.strip().lstrip("#>-*").strip()
            if line:
                return line if len(line) <= 120 else line[:119].rstrip() + "…"
        return self.message.strip()[:120]


class HumanResponse(MessageModel):
    """The human's reply to a :class:`HumanQuery`."""

    query_id: NonEmptyStr
    answer: str  # the human's own words only
    attachments: list[HumanAttachment] = Field(default_factory=list)
    # The connector's notes about what did not travel (an attachment too large to send or to
    # download), kept apart from ``answer`` so the human's receipt holds only the human's words.
    notes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
