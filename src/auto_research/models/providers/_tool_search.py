"""Shared tool-search helpers: keeping a discovered-but-vanished tool from killing a run.

Under provider-native tool search a discovered tool is recorded in the conversation as a
``tool_reference`` (Anthropic) / a loaded-tool entry (OpenAI), and the API re-expands it from
the catalog on **every** later request. If the referenced tool is no longer in the catalog the
request fails outright — Anthropic returns 400 *"Tool reference '<name>' not found in available
tools"*.

That is a real state in this system, not a hypothetical: a failed authoring attempt resets the
toolkit working tree and ``registry.reload()`` drops the tool again, while the live worker
conversation has already discovered it. Without a guard the next model call of that run dies
([F6] run-killer class).

The guard re-adds a **tombstone**: a stub definition under the same name whose description says
the tool is gone. The request then succeeds, and the model learns the capability withdrew — an
honest tool-level failure instead of a dead run.

Tool search itself is live-verified (2026-07-28); this **tombstone path specifically is exercised
by deterministic tests only** — forcing it live needs a real failed-authoring reset mid-run.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from auto_research.models.types import AssistantToolUse, ConversationMessage

_DATED_SUFFIX = re.compile(r"_\d{8}$")

TOMBSTONE_DESCRIPTION = (
    "This tool is no longer available in the toolkit — it was discovered earlier in this "
    "conversation but has since been withdrawn (for example, an authoring attempt that did not "
    "land). Report the missing capability and continue with the tools that remain."
)


def anthropic_search_tool_name(tool_type: str) -> str:
    """The ``name`` Anthropic requires beside a dated server-tool ``type``.

    The API pins the name to the **undated** identifier: a ``type`` of
    ``tool_search_tool_bm25_20251119`` must carry the ``name`` ``tool_search_tool_bm25``, and a
    mismatch is a 400 (*"Input should be 'tool_search_tool_bm25'"*, observed live 2026-07-28).
    Deriving the name keeps the dated type the single configured value and covers the ``regex``
    variant on the same rule; a type carrying no dated suffix is already the name.
    """
    return _DATED_SUFFIX.sub("", tool_type)


def referenced_tool_names(messages: Sequence[ConversationMessage]) -> set[str]:
    """Every tool name referenced by a tool-search discovery block in the history.

    Scans recursively for ``tool_name`` keys, which is how both providers' discovery payloads
    identify a loaded tool. A shape this misses simply yields no tombstone — the pre-guard
    behaviour — so an unrecognised payload can never make things worse than before.
    """
    found: set[str] = set()
    for message in messages:
        if isinstance(message, AssistantToolUse):
            for block in message.provider_blocks:
                _collect(block, found)
    return found


def _collect(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        name = node.get("tool_name")
        if isinstance(name, str) and name:
            found.add(name)
        for value in node.values():
            _collect(value, found)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect(value, found)


def missing_tool_names(
    messages: Sequence[ConversationMessage], present: Iterable[str]
) -> list[str]:
    """Referenced-but-absent tool names, sorted for a deterministic request payload."""
    return sorted(referenced_tool_names(messages) - set(present))
