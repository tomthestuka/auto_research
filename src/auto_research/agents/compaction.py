"""Bounds within a long turn (rulings B6, B13, B14 of 2026-10-18).

Within a turn a conversation only grows at its end; the bounds are segment boundaries that send no
earlier thinking:

* **Anthropic** compacts on its own server (``compact_20260112``, wired in the adapter at
  ``compaction_trigger_fraction`` of the member's input limit): the loop replays the ``compaction``
  block with its turn and appends the pin, which restates the turn's task — "all content blocks
  before it are ignored" (platform.claude.com/docs/en/build-with-claude/compaction-threshold).
* **Every other provider** (DeepSeek, Z.AI, Alibaba and Google have no server-side compaction;
  OpenAI's is unprobed for our tool-call-only turns, openai-python#3075) starts a new segment at
  the same trigger (``AgentRole._segment_boundary``): :func:`plan_compaction` with
  ``keep_recent=0, keep_discovery=False`` keeps the system prompt and the turn's task, and the
  ``session_compactor`` role's summary replaces everything else — no earlier assistant turn
  remains. Recorded as a ``rewrite`` part (``op: "compact"``), replayed by :func:`apply_compaction`.
* **The lead's coding links** are bounds of their own (``_ChainDriver.apply_cut``, B14).

Either way the summary is navigation, never evidence: it is a message, not a receipt, so nothing
in it can be cited, and :func:`pin_text` — written by code — lists the task's receipt and the
receipts the turn produced, each marked by whether it can back a number.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from auto_research.models import ModelMessage, ModelResponse, ToolResultMessage

#: Assistant turns kept word for word at the end of a compacted turn.
KEEP_RECENT_TURNS = 4
#: How much of one tool output the summariser reads (the receipt keeps the whole of it).
RENDER_RESULT_CHARS = 6_000
#: Provider part types that record a tool-search discovery (OpenAI items; Anthropic blocks).
_DISCOVERY_TYPES = frozenset({"tool_search_call", "tool_search_output", "tool_search_tool_result"})

#: A segment boundary also starts when the conversation's media nears the provider's request
#: ceilings (R3), on every provider: this share of its image-block limit ...
MEDIA_IMAGES_FRACTION = 0.8
#: ... or this share of its request-size limit (media bytes, base64).
MEDIA_BYTES_FRACTION = 0.7


def is_discovery_turn(message: Any) -> bool:
    """Does this assistant turn carry a tool-search discovery, which must survive replay?"""
    if not isinstance(message, ModelResponse):
        return False
    for part in message.provider_parts:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind in _DISCOVERY_TYPES:
            return True
        if kind == "server_tool_use" and str(part.get("name", "")).startswith("tool_search"):
            return True
    return False


def has_server_compaction(response: Any) -> bool:
    """Did the provider compact this call on its side? An Anthropic ``compaction`` block with a
    summary, or an OpenAI ``compaction`` item with its encrypted content. An Anthropic block whose
    content is null is a failed compaction, which the server treats as a no-op (anthropic SDK
    ``BetaCompactionBlock``)."""
    def done(part: Any) -> bool:
        if not isinstance(part, dict) or part.get("type") != "compaction":
            return False
        return any(isinstance(part.get(k), str) and part[k].strip()
                   for k in ("content", "encrypted_content"))

    return any(done(p) for p in getattr(response, "provider_parts", None) or ())


def summary_text(reply: str) -> str:
    """The summariser's text, without the ``<summary>`` tags its prompt asks for."""
    text = reply.strip()
    if "<summary>" in text and "</summary>" in text:
        text = text.split("<summary>", 1)[1].rsplit("</summary>", 1)[0]
    return text.strip()


@dataclass
class CompactionPlan:
    """Which messages stay, by index, around the one summary that replaces the rest."""

    head: list[int]
    kept: list[int]
    task: int | None
    tail_start: int
    middle: list[int]

    def as_part(self, summary: str) -> dict[str, Any]:
        return {"op": "compact", "head": self.head, "kept": self.kept, "task": self.task,
                "tail_start": self.tail_start, "summary": summary}


def plan_compaction(
    messages: Sequence[Any],
    task_index: int | None,
    *,
    keep_recent: int = KEEP_RECENT_TURNS,
    keep_discovery: bool = True,
) -> CompactionPlan | None:
    """Where to cut, or ``None`` when there is nothing worth condensing.

    Kept word for word: the leading system message(s); the current turn's task (``task_index``,
    the last message of the turn's seed); the last ``keep_recent`` assistant turns of this turn
    and everything after them (the tail starts at an assistant turn, so no result loses its call);
    and every turn that discovered a tool, with the tool turn that answers it. Everything else —
    earlier turns and this turn's older steps — is the middle the summary replaces. The result
    reads ``[system, discovery turns…, task, summary, tail…]``.
    """
    head = []
    for i, message in enumerate(messages):
        if isinstance(message, ModelMessage) and message.role == "system":
            head.append(i)
        else:
            break
    floor = task_index if task_index is not None else (head[-1] if head else -1)
    assistant = [i for i, m in enumerate(messages) if isinstance(m, ModelResponse) and i > floor]
    if keep_recent <= 0:
        tail_start = len(messages)
    else:
        tail_start = assistant[-keep_recent] if len(assistant) >= keep_recent else (
            assistant[0] if assistant else len(messages)
        )
    kept: list[int] = []
    middle: list[int] = []
    i = len(head)
    while i < tail_start:
        message = messages[i]
        if i == task_index:
            i += 1
            continue
        if keep_discovery and is_discovery_turn(message):
            kept.append(i)
            if i + 1 < tail_start and isinstance(messages[i + 1], ToolResultMessage):
                kept.append(i + 1)
                i += 1
        else:
            middle.append(i)
        i += 1
    if not middle:
        return None
    return CompactionPlan(head=head, kept=kept, task=task_index, tail_start=tail_start,
                          middle=middle)


def summary_message(summary: str) -> ModelMessage:
    return ModelMessage(
        role="user",
        content=(
            "[The middle of this turn, condensed by the runtime's summariser. It is a guide to "
            "what happened and where; the receipts it names are the record, and view_receipt "
            "opens each.]\n" + (summary.strip() or "(the summariser returned no text)")
        ),
    )


def apply_compaction(messages: Sequence[Any], part: Mapping[str, Any]) -> list:
    """The conversation after a recorded ``compact`` rewrite (the live edit and its replay)."""
    task = part.get("task")
    return [
        *(messages[i] for i in part.get("head") or ()),
        *(messages[i] for i in part.get("kept") or ()),
        *([messages[int(task)]] if task is not None else []),
        summary_message(str(part.get("summary") or "")),
        *messages[int(part["tail_start"]):],
    ]


def render_middle(messages: Sequence[Any], indices: Sequence[int]) -> str:
    """The span the summariser reads: who said what, each tool output with its receipt handle."""
    out: list[str] = []
    for i in indices:
        message = messages[i]
        if isinstance(message, ToolResultMessage):
            for part in message.results:
                handle = part.receipt
                label = f"result of {part.name}" + (" (error)" if part.is_error else "")
                label += f" [{handle}]" if handle else ""
                text = part.content or ""
                if len(text) > RENDER_RESULT_CHARS:
                    text = text[:RENDER_RESULT_CHARS] + f"\n… [{len(text):,} characters in all]"
                out.append(f"[{label}]\n{text}")
        elif isinstance(message, ModelResponse):
            if message.content:
                out.append(f"[agent]\n{message.content}")
            for call in message.tool_calls:
                args = json.dumps(dict(call.arguments or {}), default=str, ensure_ascii=False)
                if len(args) > RENDER_RESULT_CHARS:
                    args = args[:RENDER_RESULT_CHARS] + "…"
                out.append(f"[agent calls {call.name}]\n{args}")
        elif isinstance(message, ModelMessage):
            out.append(f"[{message.role}]\n{message.content}")
    return "\n\n".join(out)


def pin_text(
    task_receipt: str | None,
    produced: Sequence[tuple],
    *,
    task: str | None = None,
    earlier_turns: bool = False,
) -> str:
    """What code tells the agent after a compaction, outside any summary: its task (restated when
    the provider's summary replaced it) and where the record is, each receipt marked by whether it
    can back a number (``provenance.store.is_evidence``). ``earlier_turns``: the summary is of the
    session's earlier turns (a turn-boundary seed), not of this turn."""
    span = "Your earlier turns were" if earlier_turns else "The earlier part of this turn was"
    lines = [f"{span} condensed into a summary. The record stands in the receipts:"]
    if task:
        lines.append(f"- your task, as this turn began:\n{task}")
    if task_receipt:
        lines.append(f'- your task\'s receipt: view_receipt("{task_receipt}")')
    if produced:
        lines.append("- the outputs your tools produced "
                     f"{'in those turns' if earlier_turns else 'this turn'}, oldest first:")
        for entry in produced:
            tool, handle = entry[0], entry[1]
            grounds = entry[2] if len(entry) > 2 else False
            role = "evidence you can cite" if grounds else "a record of where you looked"
            lines.append(f"  {handle} ({tool}; {role})")
    lines.append("Open a receipt with view_receipt before you rely on a value in it; cite values "
                 "from the evidence receipts they came from.")
    return "\n".join(lines)


__all__ = [
    "CompactionPlan",
    "KEEP_RECENT_TURNS",
    "MEDIA_BYTES_FRACTION",
    "MEDIA_IMAGES_FRACTION",
    "apply_compaction",
    "has_server_compaction",
    "is_discovery_turn",
    "pin_text",
    "plan_compaction",
    "render_middle",
    "summary_message",
]
