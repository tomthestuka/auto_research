"""Run-wide control shared by the graph, the agents, and the HITL command channel (§12).

One object, built at run setup and threaded everywhere. The HITL slash-commands
(`/steer`, `/stop` over Telegram/Slack — see :mod:`auto_research.hitl.commands`) **write** to
it; the agent ReAct loops, the consensus authoring loop, and the graph review edges **read** it
at safe points. Delivery is **cooperative**: a steer/stop takes effect at the next poll (the top
of a ReAct iteration, a graph edge), never mid-model-call — there is no interruption of an
in-flight provider request.

Everything runs in a single event loop (the connector callbacks are asyncio tasks in the same
loop as the graph, executed during the graph's ``await`` points — which is *why* commands are
processed "anytime"), so no locks are needed.

This is a leaf module (stdlib only) so agents, graph, and hitl can all import it without cycles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def _fire(aborter: "Callable[[], Awaitable[bool]]") -> int:
    """Run one aborter, reporting whether it cancelled live work. Never raises: a stop must not
    be defeated by the failure of the thing it is trying to stop."""
    try:
        return 1 if await aborter() else 0
    except Exception:  # noqa: BLE001
        return 0


# The answer a blocked HITL wait returns when `/stop` fires (so a role awaiting `ask_human`
# unblocks immediately); the real stop is driven by ``stop_requested``, checked in the ReAct loop.
STOP_SENTINEL = "[run stopped by human]"


class RunControl:
    """Mutable run-wide flags: a human stop request and per-role steer inboxes."""

    def __init__(self) -> None:
        self.stop_requested: bool = False
        self.stop_reason: str = ""
        self.stopped_by: str = ""
        # Set alongside ``stop_requested`` so a blocked HITL wait (``wait_reply_or_stop``) wakes at
        # once. Constructed without a running loop is fine on Python ≥3.10 (Event binds lazily).
        self.stop_event: asyncio.Event = asyncio.Event()
        self._steer: dict[str, list[str]] = {}
        # agent_key → how to cancel that role's in-flight tool call (its sandbox's `abort`).
        self._aborters: dict[str, Callable[[], Awaitable[bool]]] = {}

    # ── stop ──────────────────────────────────────────────────────────────────
    def request_stop(self, reason: str = "", by: str = "human") -> None:
        """Request an honest, run-wide wind-down (idempotent). Sets the flag + wakes waits."""
        self.stop_requested = True
        self.stop_reason = reason or self.stop_reason
        self.stopped_by = by or self.stopped_by
        self.stop_event.set()

    # ── steer ─────────────────────────────────────────────────────────────────
    def add_steer(self, agent_key: str, message: str) -> None:
        """Queue a human steer message for ``agent_key`` (delivered at its next ReAct iteration).

        ``/steer all`` is expanded to per-key adds by the command handler (which knows the
        roster), so there is no shared "all" bucket to race — each role owns its own inbox.
        """
        self._steer.setdefault(agent_key, []).append(message)

    # ── cancelling work in flight ─────────────────────────────────────────────
    def register_aborter(self, agent_key: str, aborter: "Callable[[], Awaitable[bool]]") -> None:
        """Register how to cancel ``agent_key``'s in-flight work (its sandbox's ``abort``).

        Steer and stop are polled at safe points, which a role deep inside a long tool call does
        not reach — so a human's stop could be hours away behind a batch of downloads. An aborter
        is the escape hatch: it ends the current call *honestly* (the caller still gets a result),
        rather than trying to inject anything into work already running.
        """
        self._aborters[agent_key] = aborter

    async def abort(self, agent_key: str) -> int:
        """Cancel one role's in-flight work; return how many aborters reported it was busy."""
        aborter = self._aborters.get(agent_key)
        return await _fire(aborter) if aborter is not None else 0

    async def abort_all(self) -> int:
        """Cancel every registered role's in-flight work; return how many were busy."""
        busy = 0
        for aborter in list(self._aborters.values()):
            busy += await _fire(aborter)
        return busy

    def drain_steer(self, agent_key: str | None) -> list[str]:
        """Pop and return the pending steer messages for ``agent_key`` (empty if none/unkeyed)."""
        if not agent_key:
            return []
        msgs = self._steer.get(agent_key)
        if not msgs:
            return []
        self._steer[agent_key] = []
        return list(msgs)


__all__ = ["RunControl", "STOP_SENTINEL"]
