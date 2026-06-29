"""Core types for the raise-to-human channel (CLAUDE.md §9.7/§9.8).

A ``HumanHandler`` is the single injectable interface every role's ``ask_human`` routes
through; the connectors in this package (cli / slack / whatsapp) are pluggable backends
behind it. ``resolve_human`` is the one chokepoint that calls a handler, normalizes its
result, and enforces the headless fail-safe.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from auto_research.schemas import HumanQuery, HumanResponse

# The injectable interface (§9.8): (HumanQuery) -> HumanResponse, sync or async. A bare
# ``str`` return is accepted as a convenience and wrapped into a HumanResponse.
HumanHandler = Callable[[HumanQuery], "HumanResponse | str | Awaitable[HumanResponse | str]"]


class HumanInputRequired(RuntimeError):
    """Raised when a role needs a human but no handler is wired (§9.8 headless fail-safe).

    The guarantee that a run never silently hangs or fabricates when the only remaining
    resource — a real human — cannot be reached.
    """


async def resolve_human(handler: HumanHandler | None, query: HumanQuery) -> HumanResponse:
    """Call ``handler`` with ``query`` and normalize the reply to a :class:`HumanResponse`.

    Accepts sync or async handlers and a bare-``str`` answer. Raises
    :class:`HumanInputRequired` when ``handler is None`` (the headless fail-safe) — never
    hangs, never fabricates.
    """
    if handler is None:
        raise HumanInputRequired(query.headline)
    result = handler(query)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, HumanResponse):
        return result
    if isinstance(result, str):
        return HumanResponse(query_id=query.id, answer=result)
    raise TypeError(
        f"human_handler returned {type(result).__name__}; expected HumanResponse or str"
    )
