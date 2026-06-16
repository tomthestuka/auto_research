"""Attended-terminal human handler (CLAUDE.md §9.8).

The stdin connector for a CLI/interactive run. ``run_research`` and the CLI never call
``input()`` themselves — only this handler does, behind the ``HumanHandler`` interface.
"""

from __future__ import annotations

import asyncio
import sys

from auto_research.hitl.resolve import response_timeout_s
from auto_research.schemas import HumanQuery, HumanResponse


def _format(query: HumanQuery) -> str:
    lines = [
        "",
        "──────────────────────────────────────────────",
        f"  HUMAN INPUT NEEDED  ({query.kind} · from {query.sender_label})",
        f"  {query.question}",
    ]
    if query.context:
        lines.append(f"  context: {query.context}")
    lines.append("──────────────────────────────────────────────")
    lines.append("> ")
    return "\n".join(lines)


async def cli_stdin_handler(query: HumanQuery) -> HumanResponse:
    """Prompt the attended terminal for an answer and return it (blocking, off-thread).

    Waits at most ``response_timeout_s()`` (shared across all HITL channels) before raising
    ``TimeoutError`` — so even an attended terminal no longer hangs the run forever.
    """
    print(_format(query), end="", file=sys.stderr, flush=True)
    answer = await asyncio.wait_for(asyncio.to_thread(input), timeout=response_timeout_s())
    return HumanResponse(query_id=query.id, answer=answer.strip())
