"""Attended-terminal human handler (CLAUDE.md §9.8).

The stdin connector for a CLI/interactive run. ``run_research`` and the CLI never call
``input()`` themselves — only this handler does, behind the ``HumanHandler`` interface.

The query body is Markdown (the raising LLM composes it); ``rich`` renders it to the
terminal so headings, bullet lists, and fenced code read cleanly. ``rich`` degrades to
plain text on a non-tty (captured streams, tests), so it never garbles a redirected log.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule

from auto_research.hitl.resolve import response_timeout_s
from auto_research.schemas import HumanQuery, HumanResponse


def _render(query: HumanQuery) -> None:
    """Render the query to stderr as a titled, Markdown-formatted block."""
    console = Console(stderr=True)
    console.print(Rule(f"HUMAN INPUT NEEDED · {query.kind} · from {query.sender_label}"))
    console.print(Markdown(query.message))
    console.print(Rule())
    print("> ", end="", file=sys.stderr, flush=True)


async def cli_stdin_handler(query: HumanQuery) -> HumanResponse:
    """Prompt the attended terminal for an answer and return it (blocking, off-thread).

    Waits at most ``response_timeout_s()`` (shared across all HITL channels) before raising
    ``TimeoutError`` — so even an attended terminal no longer hangs the run forever.
    """
    _render(query)
    answer = await asyncio.wait_for(asyncio.to_thread(input), timeout=response_timeout_s())
    return HumanResponse(query_id=query.id, answer=answer.strip())
