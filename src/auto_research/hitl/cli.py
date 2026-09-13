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
import threading

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


async def _read_stdin_line() -> str:
    """Read one line from the terminal on a thread the interpreter will abandon at exit.

    ``asyncio.to_thread`` would put this on the **default executor**, whose workers are non-daemon
    and which ``concurrent.futures.thread._python_exit`` joins with no timeout and no daemon check
    (bpo-39812). The timeout below then fired correctly and the process still hung forever on the
    orphaned ``input()`` — measured 2026-09-12, the same class of defect as the Codex coder's
    notification poll (CLAUDE.md §13).

    Nothing can interrupt a thread parked in a read on stdin: closing the descriptor from another
    thread is not guaranteed to wake the reader and risks the number being reused underneath it,
    ``dup2`` does not touch a reader already inside ``read()``, and PEP 475 retries a signalled
    syscall. So this does not try to reach the thread — it stops depending on it. A daemon thread
    is abandoned at exit rather than joined, so an unanswered prompt costs nothing after the
    timeout returns.
    """
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[str] = loop.create_future()

    def _settle(value: str | BaseException) -> None:
        # The timeout cancels this future while the thread is still blocked, so a human who types
        # an answer afterwards must land on a no-op rather than an InvalidStateError.
        if answered.done():
            return
        if isinstance(value, BaseException):
            answered.set_exception(value)
        else:
            answered.set_result(value)

    def _read() -> None:
        try:
            value: str | BaseException = input()
        except BaseException as exc:  # noqa: BLE001 — re-raised in the awaiting task
            value = exc
        try:
            loop.call_soon_threadsafe(_settle, value)
        except RuntimeError:
            # The loop is gone (the run ended while this prompt was unanswered). Nothing to tell.
            return

    threading.Thread(target=_read, name="hitl-stdin", daemon=True).start()
    return await answered


async def cli_stdin_handler(query: HumanQuery) -> HumanResponse:
    """Prompt the attended terminal for an answer and return it (blocking, off-thread).

    Waits at most ``response_timeout_s()`` (shared across all HITL channels) before raising
    ``TimeoutError`` — so even an attended terminal no longer hangs the run forever, and since
    2026-09-12 a timed-out prompt no longer hangs the **process** either (see
    :func:`_read_stdin_line`).
    """
    _render(query)
    answer = await asyncio.wait_for(_read_stdin_line(), timeout=response_timeout_s())
    return HumanResponse(query_id=query.id, answer=answer.strip())
