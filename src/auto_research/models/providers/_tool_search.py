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

**NOT LIVE TESTED as of 2026-07-27** — exercised by deterministic tests only; the live checks run
once provider usage is available.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from auto_research.models.types import AssistantToolUse, ConversationMessage

TOMBSTONE_DESCRIPTION = (
    "This tool is no longer available in the toolkit — it was discovered earlier in this "
    "conversation but has since been withdrawn (for example, an authoring attempt that did not "
    "land). Report the missing capability and continue with the tools that remain."
)


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
