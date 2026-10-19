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


#: Inbox kinds a seat is resumed to read (D5): a seat's message, the human's reply, a human steer.
#: A notice waits for the seat's next turn; a coder's text belongs to the authoring turn.
WAKING_KINDS = frozenset({"message", "reply", "steer"})


class InboxEntry:
    """One text waiting for a seat: who sent it, what it says, and the receipt handles it names.

    The same queue carries a human ``/steer``, a human's reply to a seat's ``message``, and a text
    from another seat (E12). A text is informal: it is never a receipt, and the handles it names
    only become *viewable* for the recipient, never citable.
    """

    __slots__ = ("sender", "text", "handles", "kind")

    def __init__(
        self, sender: str, text: str, handles: tuple[str, ...] = (), kind: str = "message"
    ) -> None:
        self.sender = sender
        self.text = text
        self.handles = tuple(handles)
        # "steer" (a human /steer), "reply" (the human answering a seat's message), "message"
        # (a seat's text), "notice" (the runtime: a resumed seat ended its turn, a reply timed out).
        self.kind = kind

    def render(self) -> str:
        """The user turn the recipient reads."""
        if self.kind == "steer":
            return f"[Human steer] {self.text}"
        if self.kind == "reply":
            return f"[Reply from the human] {self.text}"
        if self.kind == "notice":
            return f"[Notice] {self.text}"
        if self.sender == "coder":
            return (
                f"[Message from the coder] {self.text}\n\nAnswer it with message(to=\"coder\") — that "
                "reaches the coder inside the turn it is still running, so it does not have to stop "
                "and wait."
            )
        return (
            f"[Message from {self.sender}] {self.text}\n\n"
            f"(Reply with message(to=\"{self.sender}\") when an answer helps; texts are informal, so "
            "rest every claim on your own evidence.)"
        )


class RunControl:
    """Mutable run-wide flags: a human stop request and per-seat inboxes."""

    def __init__(self) -> None:
        self.stop_requested: bool = False
        self.stop_reason: str = ""
        self.stopped_by: str = ""
        # Set alongside ``stop_requested`` so a blocked HITL wait (``wait_reply_or_stop``) wakes at
        # once. Constructed without a running loop is fine on Python ≥3.10 (Event binds lazily).
        self.stop_event: asyncio.Event = asyncio.Event()
        self._steer: dict[str, list[InboxEntry]] = {}
        # E12: seats whose loop is running right now, and the background tasks that resumed a
        # dormant seat, keyed by the seat that sent the message (so its turn_end can wait for them).
        self._live: set[str] = set()
        self._resumed: dict[str, dict[str, asyncio.Task]] = {}
        self._background: set[asyncio.Task] = set()
        # agent_key → set while that role has an undelivered steer, so a role parked in a
        # blocking wait (the lead's `wait_coder`) wakes the moment one arrives rather than at its
        # next timeout. Cleared by `drain_steer`, which is where the steer is actually delivered.
        self._steer_events: dict[str, asyncio.Event] = {}
        # agent_key → how to cancel that role's in-flight tool call (its sandbox's `abort`).
        self._aborters: dict[str, Callable[[], Awaitable[bool]]] = {}
        # The task running the pipeline, and the live authoring session (if any) — both set by
        # the runtime so an interrupt can cancel the one and reset the other's branch.
        self._run_task: asyncio.Task | None = None
        # A failure that must end the run although it arose in a channel task nobody awaits —
        # a resumed seat's `ReactBudgetExhausted` (ruling D6). Set by `fail`; `run_research`
        # raises it.
        self.fatal: BaseException | None = None
        self._cancel_sent = False  # the run task's stop/failure cancel goes out once
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

        Idempotent: a second interrupt on an already-cancelling task is a no-op. A run that a
        channel failure is already ending (``fatal``, D6) ends with that failure; the stop is not
        recorded over it.
        """
        if self.fatal is not None or self.stop_requested:
            # Already ending: a second /stop must not cancel the cleanup the first one started.
            return False
        self.request_stop(reason, by=by)
        task = self._run_task
        if task is None or task.done():
            return False
        asyncio.get_running_loop().call_soon(self._cancel_run)
        return True

    def _cancel_run(self) -> None:
        """Cancel the run task as registered *now*: one that already finished its graph has
        unregistered itself, and the stop's (or failure's) cancel is sent once."""
        task = self._run_task
        if task is not None and not task.done() and not self._cancel_sent:
            self._cancel_sent = True
            task.cancel()

    @property
    def running(self) -> bool:
        """Whether a run task is registered and still running (a /stop has something to stop)."""
        return self._run_task is not None and not self._run_task.done()

    def fail(self, exc: BaseException) -> bool:
        """End the run with ``exc``, raised from a background task (ruling D6).

        A channel task's exception reaches no one unless awaited, so it is recorded here (the
        first one wins) and the run task is cancelled the way an interrupt is; ``run_research``
        sees ``fatal`` and raises it instead of treating the cancel as a stop. Only the first
        failure counts, and none after a human stop. Returns whether a run task was cancelled.
        """
        if self.fatal is not None or self.stop_requested:
            # The run is already ending: by an earlier failure, or by a human stop that came
            # first (the stop wins; this failure is in the ledger).
            return False
        self.fatal = exc
        task = self._run_task
        if task is None or task.done():
            return False
        asyncio.get_running_loop().call_soon(self._cancel_run)
        return True

    # ── steer ─────────────────────────────────────────────────────────────────
    def add_steer(
        self,
        agent_key: str,
        message: str,
        *,
        sender: str = "human",
        kind: str = "steer",
        handles: tuple[str, ...] = (),
    ) -> None:
        """Queue a text for ``agent_key``'s inbox (delivered at its next ReAct boundary).

        With the defaults this is a human ``/steer``. The seat router (E12) posts a seat's
        ``message``, a human reply and runtime notices through the same call, so every text a seat
        receives reaches it at the one drain that knows when an injected turn is legal.

        ``/steer all`` is expanded to per-key adds by the command handler (which knows the
        roster), so there is no shared "all" bucket to race — each role owns its own inbox.
        """
        self._steer.setdefault(agent_key, []).append(
            InboxEntry(sender, message, handles=handles, kind=kind)
        )
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

    def drain_inbox(self, agent_key: str | None) -> list[InboxEntry]:
        """Pop and return every entry waiting for ``agent_key`` (empty if none/unkeyed)."""
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

    def drain_steer(self, agent_key: str | None) -> list[str]:
        """Pop the pending entries for ``agent_key`` and return their texts (``/steer``'s view)."""
        return [entry.text for entry in self.drain_inbox(agent_key)]

    def peek_inbox(self, agent_key: str | None) -> list[InboxEntry]:
        """The entries waiting for ``agent_key``, left in place (D5: whether to resume a seat)."""
        return list(self._steer.get(agent_key or "", ()))

    def has_inbox(self, agent_key: str | None) -> bool:
        """Whether ``agent_key`` has an undelivered entry."""
        return bool(agent_key) and bool(self._steer.get(agent_key))

    # ── seats (E12) ───────────────────────────────────────────────────────────
    def set_live(self, agent_key: str | None, live: bool) -> None:
        """Record whether ``agent_key``'s loop is running (set by the loop itself)."""
        if not agent_key:
            return
        if live:
            self._live.add(agent_key)
        else:
            self._live.discard(agent_key)

    def is_live(self, agent_key: str) -> bool:
        return agent_key in self._live

    def register_resumed(self, sender: str, seat: str, task: asyncio.Task) -> None:
        """Record that ``sender``'s message resumed ``seat`` in ``task``; forgotten when it ends."""
        tasks = self._resumed.setdefault(sender, {})
        tasks[seat] = task

        def _forget(done: asyncio.Task) -> None:
            if tasks.get(seat) is done:
                del tasks[seat]

        task.add_done_callback(_forget)

    def resumed_by(self, sender: str | None) -> list[str]:
        """The seats ``sender`` resumed that are still in their turn."""
        if not sender:
            return []
        return [seat for seat, task in self._resumed.get(sender, {}).items() if not task.done()]

    def register_background(self, task: asyncio.Task) -> None:
        """Record a channel task that no turn waits on (a human reply being awaited), so an
        interrupt cancels it; forgotten when it ends."""
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def cancel_resumed(self, timeout: float | None = None) -> int:
        """Cancel and await every channel task — resumed seats and awaited human replies — for
        the interrupt cleanup; return how many were running.

        ``timeout`` bounds the wait (D8): tasks still running past it raise ``TimeoutError`` naming
        how many, so a seat stuck in its own cleanup is reported rather than waited on forever.
        A cancel of the caller itself is never swallowed.
        """
        tasks = [t for group in self._resumed.values() for t in group.values() if not t.done()]
        tasks += [t for t in self._background if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in done:
                if not task.cancelled():
                    task.exception()  # read: a stop is not defeated by what it stops
            if pending:
                raise TimeoutError(f"{len(pending)} channel task(s) still running")
        return len(tasks)


__all__ = ["InboxEntry", "RunControl", "STOP_SENTINEL", "UserInterruptionError"]
