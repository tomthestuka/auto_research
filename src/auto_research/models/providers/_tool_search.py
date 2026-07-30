"""Shared tool-search helpers for the provider adapters.

Under provider-native tool search a discovered tool is recorded in the conversation as a
``tool_reference`` (Anthropic) / a loaded-tool entry (OpenAI), and the API re-expands it from
the catalog on every later request — so a reference to a name absent from the catalog is a 400
(*"Tool reference '<name>' not found in available tools"*).

A run cannot reach that state: the catalog a live conversation sees is fixed for that
conversation's lifetime. The only runtime mutation point is ``ToolkitRegistry.reload()``, whose
single caller (``consensus/strategy.py``, guarded by ``committed >= 1``) runs *between* actions
while every worker is idle, and every ``_react`` action builds a fresh seed — so no conversation
spans a catalog change. Only ``SandboxToolProvider`` defers tools at all, so the lead, reviewer
and co-lead never produce a discovery block in the first place. A tool that fails to commit was
never in the catalog to be discovered, since the registry reads ``TOOL_MANIFEST`` at load time.
"""

from __future__ import annotations

import re

_DATED_SUFFIX = re.compile(r"_\d{8}$")


def anthropic_search_tool_name(tool_type: str) -> str:
    """The ``name`` Anthropic requires beside a dated server-tool ``type``.

    The API pins the name to the **undated** identifier: a ``type`` of
    ``tool_search_tool_bm25_20251119`` must carry the ``name`` ``tool_search_tool_bm25``, and a
    mismatch is a 400 (*"Input should be 'tool_search_tool_bm25'"*, observed live 2026-07-28).
    Deriving the name keeps the dated type the single configured value and covers the ``regex``
    variant on the same rule; a type carrying no dated suffix is already the name.
    """
    return _DATED_SUFFIX.sub("", tool_type)
