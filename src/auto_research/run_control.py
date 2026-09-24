"""Run-wide control shared by the graph, the agents, and the HITL command channel (§12).

One object, built at run setup and threaded everywhere. The HITL slash-commands
(`/steer`, `/stop` over Telegram/Slack — see :mod:`auto_research.hitl.commands`) **write** to it.

**Steer is cooperative; stop is not.** A steer is queued and delivered at the target role's next
ReAct turn — it is a message, and a message can wait. A **stop is an interrupt**: it is what
Ctrl-C would be if the run had a terminal to press it in, so it cancels the run task outright
(:meth:`RunControl.interrupt`) rather than asking the pipeline to wind itself down. The
cooperative wind-down this module used to drive was removed 2026-07-31: it did not stop a run,
it only stopped it *using tools* — every role still made its terminal write-up call and the
pipeline walked every remaining stage (measured: ~14 further model calls, several replaying a
100k-token trajectory), and worse, truncating the workers' evidence made the lead judge the
debate unconverged and order fresh revision rounds. Stopping manufactured work.

Everything runs in a single event loop (the connector callbacks are asyncio tasks in the same
loop as the graph, executed during the graph's ``await`` points — which is *why* commands are
processed "anytime"), so no locks are needed.

This is a leaf module (stdlib only) so agents, graph, and hitl can all import it without cycles.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def _fire(aborter: "Callable[[], Awaitable[bool]]") -> int:
    """Run one aborter, reporting whether it cancelled live work. Never raises: a stop must not
    be defeated by the failure of the thing it is trying to stop."""
    try:
        return 1 if await aborter() else 0
    except Exception:  # noqa: BLE001
        return 0


# The answer a blocked HITL wait returns when `/stop` fires, so a role awaiting `ask_human` wakes
# instead of sitting on a future the cancellation would have to tear down underneath it.
STOP_SENTINEL = "[run stopped by human]"


class UserInterruptionError(RuntimeError):
    """A human stopped the run: `/stop`, or SIGINT/SIGTERM on a process we own.

    Raised by ``run_research`` in place of the ``CancelledError`` that carried the interrupt, so a
    notebook cell reads "a human stopped this" rather than a bare cancellation an ipykernel may
    swallow. It is **not** an analysis outcome: there is no report, and none was composed — the
    run was cut off, and its record is whatever it had already written to disk (named in
    ``artifacts`` so the raising side does not have to go looking).

    ``resume=True`` continues from the last completed graph node, so an interruption is a pause
    rather than a loss (the interrupted node re-runs from its start).
    """

    def __init__(
        self,
        reason: str = "",
        *,
        by: str = "human",
        where: str = "",
        artifacts: dict[str, str] | None = None,
    ) -> None:
        self.reason = reason
        self.by = by
        self.where = where
        self.artifacts = dict(artifacts or {})
        detail = f" while {where}" if where else ""
        because = f": {reason}" if reason else ""
        listed = "".join(f"\n  {k}: {v}" for k, v in self.artifacts.items())
        super().__init__(
            f"run interrupted by {by}{detail}{because}. No report was composed. "
            f"Resume with resume=True to continue from the last completed node."
            f"{listed}"
        )


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
        # agent_key → set while that role has an undelivered steer, so a role parked in a
        # blocking wait (the lead's `wait_coder`) wakes the moment one arrives rather than at its
        # next timeout. Cleared by `drain_steer`, which is where the steer is actually delivered.
        self._steer_events: dict[str, asyncio.Event] = {}
        # agent_key → how to cancel that role's in-flight tool call (its sandbox's `abort`).
        self._aborters: dict[str, Callable[[], Awaitable[bool]]] = {}
        # The task running the pipeline, and the live authoring session (if any) — both set by
        # the runtime so an interrupt can cancel the one and reset the other's branch.
        self._run_task: asyncio.Task | None = None
        self.authoring_session: Any | None = None

    # ── stop ──────────────────────────────────────────────────────────────────
    def request_stop(self, reason: str = "", by: str = "human") -> None:
        """Record that a stop was requested (idempotent) and wake anything blocked on it.

        Sets the flag only. It is the flag that tells ``run_research``'s handler the incoming
        ``CancelledError`` is *ours* — an outer cancel (a kernel shutdown, a caller's timeout)
        arrives without it and is re-raised untouched rather than mislabelled as a human stop.
        """
        self.stop_requested = True
        self.stop_reason = reason or self.stop_reason
        self.stopped_by = by or self.stopped_by
        self.stop_event.set()

    def register_run_task(self, task: asyncio.Task | None) -> None:
        """Record the task running the pipeline, so :meth:`interrupt` can cancel it."""
        self._run_task = task

    def interrupt(self, reason: str = "", by: str = "human") -> bool:
        """Stop the run now: flag it, then cancel the run task. Returns whether a task was cancelled.

        The cancellation is **scheduled**, not immediate (``call_soon``), for one reason: the
        caller is usually a HITL command handler that still has to send its acknowledgement, and
        cancelling synchronously would race the interrupt's own cleanup — which closes the
        connector — against that reply. Yielding one loop turn lets the reply get in flight first.

        Idempotent: a second interrupt on an already-cancelling task is a no-op.
        """
        self.request_stop(reason, by=by)
        task = self._run_task
        if task is None or task.done():
            return False
        asyncio.get_running_loop().call_soon(task.cancel)
        return True

    # ── steer ─────────────────────────────────────────────────────────────────
    def add_steer(self, agent_key: str, message: str) -> None:
        """Queue a human steer message for ``agent_key`` (delivered at its next ReAct iteration).

        ``/steer all`` is expanded to per-key adds by the command handler (which knows the
        roster), so there is no shared "all" bucket to race — each role owns its own inbox.
        """
        self._steer.setdefault(agent_key, []).append(message)
        self.steer_event(agent_key).set()

    def steer_event(self, agent_key: str) -> asyncio.Event:
        """The event set while ``agent_key`` has a steer waiting — what a blocking wait watches.

        The wait only *wakes* on it; the steer itself still reaches the role through
        :meth:`drain_steer` at the loop's next safe boundary, so waking adds no injection site.
        """
        return self._steer_events.setdefault(agent_key, asyncio.Event())

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
        event = self._steer_events.get(agent_key)
        if event is not None:
            event.clear()
        if not msgs:
            return []
        self._steer[agent_key] = []
        return list(msgs)


__all__ = ["RunControl", "STOP_SENTINEL", "UserInterruptionError"]
