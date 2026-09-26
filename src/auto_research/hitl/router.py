"""The quick channel between any two seats, the human one endpoint (E12).

One ``message(to, text, attachments)`` tool on every graph seat routes through :class:`SeatRouter`,
and the call **never blocks**: it returns at once, and whatever comes back — the human's reply, the
other seat's answer, a notice that a resumed seat ended its turn — arrives later in the sender's
inbox (``RunControl``), read at its next loop boundary or by ``wait_for_reply``.

::

    message(to=X) ─▶ SeatRouter ─┬─ human  → background: resolve_human(HumanQuery) → sender's inbox
                                 ├─ X live → X's inbox
                                 ├─ X kept → X's inbox + resume X's normal loop in the background
                                 ├─ X unrun→ X's inbox (read when its turn starts)
                                 └─ mirror → human_handler.notify(text)   (seat-to-seat texts only)

LLM↔LLM reuses the human transport: the inbox that carries a human ``/steer`` carries a seat's text,
and the human's reply travels the same :class:`~auto_research.hitl.base.HumanHandler` every role
already used. **Texts are informal**: nothing here mints a receipt for a text, and a handle a text
names becomes *viewable* for the recipient (``view_receipt``), never citable — every claim a seat
makes rests on its own evidence. The files a human sends are the one exception: they are minted as
the sender's own receipts by ``hitl.attachments.mint_reply_attachments``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from auto_research.hitl.base import HumanHandler, HumanInputRequired, resolve_human

#: The human's address on the channel.
HUMAN = "human"

#: What a seat must offer the router to be resumed: its key, whether a finished turn was kept,
#: and the coroutine that runs that turn's loop again.
Seat = Any


class SeatRouter:
    """Routes one seat's text to another, and the human's replies back.

    ``seats`` are registered by the runtime (the graph roles: reviewer, librarian, lead, workers).
    The co-lead and the status monitor are never registered, so no seat can address them and they
    receive nothing; the coder is reached only by its lead, through the lead's own
    ``message(to="coder")`` (``agents.roles.ChannelTools``).
    """

    def __init__(
        self,
        run_control: Any,
        human_handler: HumanHandler | None = None,
        *,
        monitor: Any | None = None,
        mirror: bool = True,
        store: Any | None = None,
    ) -> None:
        self.run_control = run_control
        self.human_handler = human_handler
        self.monitor = monitor
        self.mirror = mirror
        # The run's ReceiptStore: where the human's files are minted, and what view_receipt reads.
        self.store = store
        self._seats: dict[str, Seat] = {}
        # seat → handles a delivered text named: shown by view_receipt, never admitted to a scope.
        self._viewable: dict[str, set[str]] = {}
        # seat → questions to the human still awaiting an answer.
        self._open_human: dict[str, int] = {}

    # ── the directory ────────────────────────────────────────────────────────
    def register(self, key: str, seat: Seat) -> None:
        self._seats[key] = seat

    @property
    def attended(self) -> bool:
        return self.human_handler is not None

    def directory(self, sender: str | None) -> list[str]:
        """Who ``sender`` may address: every registered seat but itself, then the human."""
        seats = [key for key in self._seats if key != sender]
        return seats + ([HUMAN] if self.attended else [])

    def seat(self, key: str) -> Seat:
        return self._seats[key]

    def state(self, key: str) -> str:
        """What ``key`` is doing now, as a seat choosing whom to text needs to know it."""
        if self.run_control.is_live(key):
            return "working now, reads a text at its next step"
        if getattr(self._seats.get(key), "has_kept_turn", lambda: False)():
            return "finished its turn, a text resumes it"
        return "not started, reads a text when its turn begins"

    def viewable(self, key: str | None) -> set[str]:
        return set(self._viewable.get(key or "", ()))

    def outstanding(self, key: str | None) -> list[str]:
        """What ``key`` could still hear back from: the human, and seats it resumed."""
        pending = ["the human"] if self._open_human.get(key or "", 0) > 0 else []
        return pending + list(self.run_control.resumed_by(key))

    # ── sending ──────────────────────────────────────────────────────────────
    async def send(
        self,
        sender: str,
        to: str,
        text: str,
        *,
        handles: Iterable[str] = (),
        scope: Any | None = None,
        receipt_source: str | None = None,
        reply_dir: Path | None = None,
        kind: str = "clarify_science",
        model: str = "",
    ) -> tuple[str, bool]:
        """Deliver ``text`` from ``sender`` to ``to``; return ``(tool text, is_error)`` at once."""
        text = (text or "").strip()
        if not text:
            return "message was not sent: write the text you want delivered.", True
        handles = [h.strip() for h in handles if h and h.strip()]
        if to == HUMAN:
            if not self.attended:
                return (
                    "message was not sent: no human is attending this run. "
                    f"Seats you can reach: {', '.join(self.directory(sender)) or 'none'}.",
                    True,
                )
            return await self._to_human(
                sender, text, handles=handles, scope=scope, receipt_source=receipt_source,
                reply_dir=reply_dir, kind=kind, model=model,
            )
        if to == sender or to not in self._seats:
            return (
                f"message was not sent: '{to}' is not a seat you can reach. "
                f"Seats you can reach: {', '.join(self.directory(sender)) or 'none'}.",
                True,
            )
        return await self._to_seat(sender, to, text, handles=handles, scope=scope)

    async def _to_seat(
        self, sender: str, to: str, text: str, *, handles: list[str], scope: Any | None
    ) -> tuple[str, bool]:
        shown, problems = self._shareable(handles, scope)
        self._viewable.setdefault(to, set()).update(shown)
        self.run_control.add_steer(to, text, sender=sender, kind="message", handles=tuple(shown))
        seat = self._seats[to]
        if self.run_control.is_live(to):
            route, reply = "live", (
                f"Delivered to {to}, which is working now and reads it at its next step."
            )
        elif getattr(seat, "has_kept_turn", lambda: False)():
            route = "resumed"
            task = asyncio.create_task(self._resume(sender, to, seat))
            self.run_control.register_resumed(sender, to, task)
            reply = (
                f"Delivered to {to}, which had finished its turn and has been resumed to read it. "
                "Its answer, if it sends one, arrives as a message; call wait_for_reply when you "
                "have nothing else to do."
            )
        else:
            route, reply = "queued", (
                f"Delivered to {to}, which has not started its turn yet and reads it when it does."
            )
        await self._record(
            "channel.message", level=4, actor=sender,
            summary=f"{sender} → {to} ({route}): {_cap(text, 80)}",
            data={"from": sender, "to": to, "route": route, "text": _cap(text, 4000),
                  "handles": shown},
        )
        await self._mirror(f"💬 {sender} → {to}\n\n{text}")
        return reply + "".join(f"\n{p}" for p in problems), False

    def _shareable(self, handles: list[str], scope: Any | None) -> tuple[list[str], list[str]]:
        """The handles the sender can itself see (only those may be shown to the recipient)."""
        shown: list[str] = []
        problems: list[str] = []
        for handle in dict.fromkeys(handles):
            visible = scope is not None and getattr(scope, "visible", lambda _h: False)(handle)
            if visible:
                shown.append(handle)
            else:
                problems.append(f"{handle} is not a receipt you can see, so it was not shared.")
        return shown, problems

    async def _to_human(
        self,
        sender: str,
        text: str,
        *,
        handles: list[str],
        scope: Any | None,
        receipt_source: str | None,
        reply_dir: Path | None,
        kind: str,
        model: str,
    ) -> tuple[str, bool]:
        from auto_research.schemas import HumanQuery

        attachments, problems = [], []
        if handles:
            from auto_research.hitl.attachments import resolve_attachments

            if scope is None:
                problems = [f"{h} was not attached: this seat has no receipts." for h in handles]
            else:
                attachments, problems = resolve_attachments(scope, handles)
        query = HumanQuery(
            kind=kind if kind in ("clarify_science", "clarify_code", "blocked") else "clarify_science",
            message=text, asked_by=sender, model=model, attachments=attachments,
            reply_dir=reply_dir,
        )

        async def deliver(reply: str, reply_handles: tuple[str, ...], entry_kind: str) -> None:
            self._open_human[sender] = max(0, self._open_human.get(sender, 0) - 1)
            self.run_control.add_steer(
                sender, reply, sender=HUMAN, kind=entry_kind, handles=reply_handles
            )
            seat = self._seats.get(sender)
            if (
                entry_kind == "reply"
                and seat is not None
                and not self.run_control.is_live(sender)
                and getattr(seat, "has_kept_turn", lambda: False)()
            ):
                # The seat filed while the human was answering: the reply resumes it, reply-only.
                task = asyncio.create_task(self._resume(HUMAN, sender, seat))
                self.run_control.register_background(task)

        self.ask_human(query, deliver=deliver, receipt_source=receipt_source)
        self._open_human[sender] = self._open_human.get(sender, 0) + 1
        await self._record(
            "channel.message", level=3, actor=sender,
            summary=f"{sender} → human: {query.headline}",
            data={"from": sender, "to": HUMAN, "route": "human", "kind": query.kind,
                  "query_id": query.id, "text": _cap(text, 4000),
                  "attachments": [a.handle for a in attachments]},
        )
        sent = (
            "Sent to the human. Their reply arrives as a message; keep working, or call "
            "wait_for_reply when you need it before going on."
        )
        return sent + "".join(f"\n{p}" for p in problems), False

    def ask_human(
        self,
        query: Any,
        *,
        deliver: Callable[[str, tuple[str, ...], str], Awaitable[None]],
        receipt_source: str | None = None,
    ) -> None:
        """Await the human's answer to ``query`` in the background and hand it to ``deliver``.

        ``deliver(text, file_handles, kind)`` receives ``kind="reply"`` for an answer and
        ``kind="notice"`` when none came (timeout, channel fault). Raises
        :class:`HumanInputRequired` at once when no human is attending, so a caller that must
        classify an unattended run (the coder's MCP tool) still can.
        """
        if self.human_handler is None:
            raise HumanInputRequired(query.headline)
        task = asyncio.create_task(self._await_human(query, deliver, receipt_source))
        self.run_control.register_background(task)

    async def _await_human(
        self,
        query: Any,
        deliver: Callable[[str, tuple[str, ...], str], Awaitable[None]],
        receipt_source: str | None,
    ) -> None:
        try:
            response = await resolve_human(self.human_handler, query)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._record(
                "channel.reply.timeout", level=3, actor=query.asked_by,
                summary=f"no human reply to {query.asked_by} within the response window",
                data={"query_id": query.id},
            )
            await deliver(
                "The human did not reply within the response window. Carry on with what you "
                "have and say plainly what stayed open.", (), "notice",
            )
            return
        except Exception as exc:  # noqa: BLE001 — a broken channel is a fault the sender hears
            await self._record(
                "channel.reply.error", level=2, actor=query.asked_by,
                summary=f"the human channel failed for {query.asked_by}: {exc!r}",
                data={"query_id": query.id, "error": repr(exc)},
            )
            await deliver(f"The human channel failed: {exc}. No answer arrived.", (), "notice")
            return
        minted: list[str] = []
        if response.attachments and self.store is not None and receipt_source:
            from auto_research.hitl.attachments import mint_reply_attachments

            # The human's TEXT is a nudge and never a receipt; the files are the sender's own.
            receipts = mint_reply_attachments(
                self.store, source=receipt_source, response=response, parent=None
            )
            minted = [r.handle for r in receipts]
        parts = [response.answer or "(the human sent no text)"]
        if minted:
            parts.append("Files the human sent (your receipts; see them with view_receipt): "
                         + ", ".join(minted))
        parts.extend(f"({note})" for note in getattr(response, "notes", ()) or ())
        await self._record(
            "channel.reply", level=3, actor=query.asked_by,
            summary=f"human → {query.asked_by}: {_cap(response.answer or '', 80)}",
            data={"query_id": query.id, "answer": _cap(response.answer or "", 4000),
                  "files": minted, "notes": list(getattr(response, "notes", ()) or ())},
        )
        await deliver("\n\n".join(parts), tuple(minted), "reply")

    # ── resuming a dormant seat ──────────────────────────────────────────────
    async def _resume(self, sender: str, key: str, seat: Seat) -> None:
        await self._record(
            "channel.resume", level=3, actor=key,
            summary=f"{key} resumed to read a message from {sender}",
            data={"seat": key, "from": sender},
        )
        try:
            await seat.resume()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the resumed turn's failure is the sender's news
            await self._record(
                "channel.resume.error", level=1, actor=key,
                summary=f"{key}'s resumed turn failed: {exc!r}",
                data={"seat": key, "from": sender, "error": repr(exc)},
            )
            outcome = f"{key}'s resumed turn failed ({type(exc).__name__}: {exc})."
        else:
            await self._record(
                "channel.resume.end", level=3, actor=key,
                summary=f"{key} ended its resumed turn", data={"seat": key, "from": sender},
            )
            outcome = f"{key} ended its resumed turn."
        if sender != HUMAN:
            self.run_control.add_steer(sender, outcome, sender=key, kind="notice")

    async def record_message(
        self, sender: str, to: str, text: str, *, route: str, is_error: bool = False
    ) -> None:
        """Ledger a text the router did not route itself (the lead's to its coder)."""
        await self._record(
            "channel.message", level=4, actor=sender,
            summary=f"{sender} → {to} ({route}): {_cap(text, 80)}",
            data={"from": sender, "to": to, "route": route, "text": _cap(text, 4000),
                  "is_error": is_error},
        )

    # ── plumbing ─────────────────────────────────────────────────────────────
    async def _mirror(self, text: str) -> None:
        notify = getattr(self.human_handler, "notify", None) if self.mirror else None
        if notify is None:
            return

        async def _post() -> None:
            try:
                result = notify(text)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 — a mirror is a courtesy, never a failure
                await self._record(
                    "channel.mirror.error", level=4, actor=HUMAN,
                    summary=f"mirror post failed: {exc!r}", data={"error": repr(exc)},
                )

        self.run_control.register_background(asyncio.create_task(_post()))

    async def _record(
        self, event: str, *, level: int, actor: str, summary: str, data: dict | None = None
    ) -> None:
        if self.monitor is None:
            return
        try:
            await self.monitor.record(
                "channel", event, level=level, summary=summary, actor=actor, data=data or {}
            )
        except Exception:  # noqa: BLE001 — the ledger never breaks the channel
            pass


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


__all__ = ["HUMAN", "SeatRouter"]
