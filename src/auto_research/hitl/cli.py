"""Attended-terminal human handler (CLAUDE.md §9.8).

The stdin connector for a CLI/interactive run. ``run_research`` and the CLI never call
``input()`` themselves — only this handler does, behind the ``HumanHandler`` interface.
"""

from __future__ import annotations

import asyncio
import sys

from auto_research.schemas import HumanQuery, HumanResponse


def _format(query: HumanQuery) -> str:
    lines = [
        "",
        "──────────────────────────────────────────────",
        f"  HUMAN INPUT NEEDED  ({query.kind} · from {query.asked_by})",
        f"  {query.question}",
    ]
    if query.context:
        lines.append(f"  context: {query.context}")
    lines.append("──────────────────────────────────────────────")
    lines.append("> ")
    return "\n".join(lines)


async def cli_stdin_handler(query: HumanQuery) -> HumanResponse:
    """Prompt the attended terminal for an answer and return it (blocking, off-thread)."""
    print(_format(query), end="", file=sys.stderr, flush=True)
    answer = await asyncio.to_thread(input)
    return HumanResponse(query_id=query.id, answer=answer.strip())
