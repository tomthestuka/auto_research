"""Channel-agnostic HITL slash-command dispatch (§12 Stage 3).

Command-capable connectors (Telegram/Slack — always-listening) recognize a leading-``/``
message and route it here **instead of** treating it as a reply to a pending LLM query, so a
human can drive the run at any time:

- ``/help`` — list commands + the live team/role/model roster.
- ``/status [question]`` — report the run's state; the ``status_monitor`` reporter reads the run's
  own ledger through :class:`~auto_research.agents.react.MonitorToolProvider` and answers.
- ``/steer <role> <message>`` — queue a message for a role (delivered at its next ReAct step);
  ``/steer all`` broadcasts to every role.
- ``/stop`` — interrupt the run now (the Ctrl-C a run without a terminal cannot receive):
  cancel in-flight work, clean up deterministically, end with ``UserInterruptionError``.

All state lives on the shared :class:`~auto_research.run_control.RunControl`; this module only
parses + routes and returns Markdown to send back. ``wait_reply_or_stop`` is the shared
reply-wait every command-capable connector uses so ``/stop`` unblocks a role awaiting a reply.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from auto_research.run_control import STOP_SENTINEL, RunControl

# Roster entry: (team, agent_key, model_id). The human addresses a role by its **id** — its
# 1-based position in this list, printed by `/help` — so nothing has to be retyped from memory on
# a phone keyboard; `agent_key` remains the internal steer-inbox key.
RosterEntry = tuple[str, str, str]

# Every reply an invalid command produces starts with this, so a human sees at a glance that
# nothing was queued or acted on (a command either takes effect or says why, immediately).
_REJECTED = "⚠️ "

#: The immediate answer to the commands whose work takes a while (ruling D8): sent before the work
#: starts, so the human knows the command landed. `/stop`'s outcome follows as the landing
#: checklist; `/status`'s as the monitor agent's answer.
STOP_ACK = "/stop received, cleaning up…"
STATUS_ACK = "/status received, monitor agent investigating…"


class CommandReply(str):
    """A command's Markdown reply, optionally carrying buttons.

    A ``str`` subclass on purpose: every connector already sends the reply as text, so buttons are
    additive — a connector that cannot render them (CLI, WhatsApp, or Slack until its interactivity
    path is wired) keeps working untouched, and only Telegram reads ``.buttons``.
    """

    buttons: tuple[tuple[str, str], ...] = ()   # (label, callback_data)

    def __new__(cls, text: str, buttons: tuple[tuple[str, str], ...] = ()) -> "CommandReply":
        reply = super().__new__(cls, text)
        reply.buttons = tuple(buttons)
        return reply


# Per-process, so a tap on a button posted by an EARLIER segment of the same run is detectable:
# `run_id` cannot do that job (it is deliberately stable across a resume), and the in-process steer
# inbox that button referred to died with its process.
_BOOT = uuid4().hex[:6]


@dataclass
class CommandContext:
    """Run-scoped context the dispatcher needs, wired onto a connector by ``run_research``."""

    run_control: RunControl
    roster: Callable[[], list[RosterEntry]]
    monitor_jsonl_path: Path | None = None
    # (question) -> Markdown report from the `status_monitor` reporter. Always wired by
    # `run_research` (the role is required at preflight); left optional here only so a test or a
    # duck-typed harness can build a context without a model.
    status_answer: Callable[[str], Awaitable[str]] | None = None
    # The run's RunMonitor. Commands are recorded in the run ledger (§J1: the JSONL is the single
    # source of truth), because without it a rejected command, a queued steer and a delivered
    # steer are indistinguishable after the fact — exactly the ambiguity that made a lost steer
    # un-diagnosable on the 2026-07-29 run.
    monitor: Any | None = None
    # The run's id, carried in button payloads so a tap on ANOTHER run's history is refused.
    run_id: str = ""


def _known_commands() -> list[tuple[str, str]]:
    return [
        ("/help", "List commands and the live team/role/model roster."),
        ("/status [question]", "Report the run's state; add a question to ask about it."),
        ("/steer <id> <message>", "Send a message to one role's next step (id from `/help`; `all` = every role)."),
        ("/stop", "Interrupt the run now — no report; resume=True continues it."),
    ]


def menu_commands() -> list[tuple[str, str]]:
    """``(name, description)`` pairs for a client-side command menu (Telegram's ``setMyCommands``).

    Names carry no leading slash and descriptions are one short line, per the platform's format.
    Derived from the same list `/help` prints, so the menu can never drift from the help text.
    """
    return [
        (name.lstrip("/").split()[0], description)
        for name, description in _known_commands()
    ]


async def dispatch_command(
    text: str, ctx: CommandContext, *, ack: Callable[[str], Awaitable[Any]] | None = None
) -> str:
    """Parse and run a ``/command …`` line; return Markdown to send back to the human.

    Every outcome is immediate and deterministic: a command either takes effect or answers with
    what was wrong and what the valid options are. Nothing is queued on a guess.

    ``ack`` (a connector's send) answers ``/stop`` and ``/status`` at once, before their work
    (ruling D8); ``/stop`` then returns an empty reply, since its outcome is the landing
    checklist the run posts. Without ``ack`` the acknowledgement is ``/stop``'s reply.
    """
    text = (text or "").strip()
    parts = text.lstrip("/").split(maxsplit=1)
    name = parts[0].lower().split("@", 1)[0] if parts and parts[0] else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    await _record_command(ctx, "hitl.command", level=2, summary=f"human sent /{name or 'help'}",
                          data={"text": text, "command": name})

    if name in ("", "help"):
        reply, accepted = _help(ctx), True
    elif name == "stop":
        ending = _already_ending(ctx.run_control)
        if ending:
            reply, accepted = f"/stop received; {ending}", True
        elif not ctx.run_control.running:
            # Still flagged, so a reply-wait in progress unblocks; but nothing will clean up.
            _stop(ctx)
            reply, accepted = "/stop received; no run is in flight, so there is nothing to clean up.", True
        else:
            if ack is not None:
                await _ack(ack, STOP_ACK)
            _stop(ctx)
            reply, accepted = ("" if ack is not None else _STOP_REPLY), True
    elif name == "steer":
        reply, accepted = _steer(arg, ctx)
    elif name == "status":
        if ack is not None:
            await _ack(ack, STATUS_ACK)
        try:
            reply, accepted = await _status(arg, ctx), True
        except Exception as exc:  # noqa: BLE001 — the human was told to wait; tell them why not
            reply = f"{_REJECTED}/status could not answer: {type(exc).__name__}: {exc}"
            accepted = False
    else:
        reply, accepted = f"{_REJECTED}Unknown command `/{name}`.\n\n{_help(ctx)}", False

    await _record_command(
        ctx, "hitl.command.reply", level=3,
        summary=f"/{name or 'help'} {'accepted' if accepted else 'rejected'}",
        data={"command": name, "accepted": accepted, "reply": reply},
    )
    return reply


#: How long an acknowledgement may hold up the command it acknowledges (a `/stop` waits for it).
_ACK_TIMEOUT_S = 3.0


async def _ack(send: Callable[[str], Awaitable[Any]], text: str) -> None:
    """Send an acknowledgement, bounded; a slow or failed send never holds up or stops the
    command it acknowledges."""
    try:
        await asyncio.wait_for(send(text), timeout=_ACK_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — the command still runs
        pass


def _already_ending(run_control: RunControl) -> str:
    """Why a ``/stop`` has nothing to start, or ``""`` when it stops the run now."""
    if getattr(run_control, "fatal", None) is not None:
        return "the run is already ending on a failure and cleaning up."
    if run_control.stop_requested:
        return "the run is already stopping."
    return ""


async def _record_command(ctx: CommandContext, event: str, *, level: int, summary: str,
                          data: dict) -> None:
    """Record one command event in the run ledger (guarded — logging never breaks the channel)."""
    monitor = getattr(ctx, "monitor", None)
    if monitor is None:
        return
    try:
        await monitor.record("run", event, level=level, summary=summary, actor="human", data=data)
    except Exception:  # noqa: BLE001 — a monitor fault must not swallow a human's command
        pass


def _roster_rows(ctx: CommandContext) -> list[tuple[int, str, str, str]]:
    """The roster as ``(id, team, agent_key, model)``; the id is the 1-based display position."""
    return [(i, team, key, model) for i, (team, key, model) in enumerate(_safe_roster(ctx), start=1)]


def _roster_listing(ctx: CommandContext) -> str:
    rows = _roster_rows(ctx)
    if not rows:
        return "- (roster unavailable)"
    return "\n".join(f"- `{i}` · `{team}` · `{key}` · `{model}`" for i, team, key, model in rows)


def _help(ctx: CommandContext) -> str:
    lines = ["**Commands**"]
    lines += [f"- `{name}` — {desc}" for name, desc in _known_commands()]
    lines.append("")
    lines.append("**Roster** — id · team · role · model (address a role by its **id**):")
    lines.append(_roster_listing(ctx))
    return "\n".join(lines)


def _stop(ctx: CommandContext) -> None:
    """Stop the run — this is Ctrl-C, for a run that has no terminal to press it in.

    It interrupts: the run task is cancelled, whatever was in flight ends there, and cleanup is
    deterministic (kill the sandbox processes and their children, reset the authoring branch,
    close the channels, record the ledger's closing events). **No report is composed and no model
    is called** on the way out — the run ends with ``UserInterruptionError``.

    The earlier design asked the pipeline to wind itself down at each role's next safe point. It
    did not stop the run, only its tool use: every role still made its terminal write-up call and
    the pipeline walked every remaining stage — ~14 further model calls, several replaying a
    100k-token trajectory — and truncating the workers' evidence made the lead judge the debate
    unconverged and order fresh revision rounds. Stopping manufactured work, and the first
    write-up call after the stop is what crashed the 2026-07-29 run.
    """
    ctx.run_control.interrupt("human /stop", by="human")


#: `/stop`'s reply on a connector that sends no separate acknowledgement.
_STOP_REPLY = (
    f"{STOP_ACK} A checklist of every shutdown step follows when it ends. No report is composed; "
    "relaunch with `resume=True` to continue from the last completed step."
)


def _steer(arg: str, ctx: CommandContext) -> tuple[str, bool]:
    """Queue a steer for one roster id (or ``all``). Returns ``(reply, accepted)``.

    Targeting is by **id**, not role name: the id is what `/help` prints, so what is displayed is
    exactly what is accepted. A name that used to work answers with its id rather than silently
    queueing under a key nothing drains.
    """
    bits = arg.split(maxsplit=1)
    rows = _roster_rows(ctx)
    if len(bits) < 2 or not bits[1].strip():
        return (
            f"{_REJECTED}Usage: `/steer <id> <message>` — id from `/help`, or `all`.\n"
            f"{_roster_listing(ctx)}",
            False,
        )
    target, message = bits[0], bits[1].strip()

    if target.lower() == "all":
        for _, _, key, _ in rows:
            ctx.run_control.add_steer(key, message)
        return (
            f"📨 Steer **queued** for all {len(rows)} role(s) — each is delivered at that role's "
            "next step (a role inside a long tool call sees it when that call returns).",
            True,
        )

    if not target.isdigit():
        named = [row for row in rows if row[2] == target]
        hint = f" `{target}` is id `{named[0][0]}`." if named else ""
        return (
            f"{_REJECTED}`/steer` takes a roster **id**, not a name.{hint}\n"
            f"{_roster_listing(ctx)}",
            False,
        )

    match = [row for row in rows if row[0] == int(target)]
    if not match:
        return (
            f"{_REJECTED}No role with id `{target}`.\n{_roster_listing(ctx)}",
            False,
        )
    _, team, key, model = match[0]
    ctx.run_control.add_steer(key, message)
    return (
        CommandReply(
            f"📨 Steer **queued** for `{key}` (id `{target}`, {team} · {model}) — delivered at its "
            "next step (if it is inside a long tool call, when that call returns).",
            # The two escalations, one tap each, attached to the thing they modify: no syntax to
            # recall and nothing to mistype at the moment it matters most.
            buttons=(
                ("⚡ Deliver now", f"steer:now:{target}:{ctx.run_id}:{_BOOT}"),
                ("🗑 Clear", f"steer:clear:{target}:{ctx.run_id}:{_BOOT}"),
            ),
        ),
        True,
    )


async def dispatch_callback(data: str, ctx: CommandContext) -> str:
    """Handle a button tap (Telegram callback data); return Markdown to show the human.

    Payload is ``steer:<action>:<id>:<run_id>:<boot>``. Nothing but the action and the target
    travels in it — the message is already in the steer inbox — so there is no side store to keep
    and no token to expire. The two trailing fields answer "is this button still mine?": a
    different ``run_id`` means another run's history, and a different boot nonce means an earlier
    segment of THIS run (whose inbox died with its process).
    """
    parts = (data or "").split(":")
    if len(parts) != 5 or parts[0] != "steer":
        return f"{_REJECTED}Unrecognized button."
    _, action, target, run_id, boot = parts

    if ctx.run_id and run_id != ctx.run_id:
        return f"{_REJECTED}That button belongs to run `{run_id}`; this run is `{ctx.run_id}`."
    if boot != _BOOT:
        return (
            f"{_REJECTED}That button is from an earlier session of this run — its queue is gone. "
            "Send the command again."
        )

    rows = [row for row in _roster_rows(ctx) if str(row[0]) == target]
    if not rows:
        return f"{_REJECTED}No role with id `{target}` in this run.\n{_roster_listing(ctx)}"
    _, _, key, _ = rows[0]

    if action == "clear":
        dropped = len(ctx.run_control.drain_steer(key))
        reply = (
            f"🗑 Cleared {dropped} queued steer(s) for `{key}`."
            if dropped
            else f"🗑 Nothing was queued for `{key}`."
        )
    elif action == "now":
        cancelled = await ctx.run_control.abort(key)
        reply = (
            f"⚡ Forcing delivery to `{key}` — cancelled its in-flight tool call, so the steer "
            "lands on its next step."
            if cancelled
            else f"⚡ `{key}` was not mid-call; its queued steer lands at its next step."
        )
    else:
        return f"{_REJECTED}Unknown button action `{action}`."

    await _record_command(
        ctx, "hitl.command.button", level=3, summary=f"human tapped {action} for {key}",
        data={"action": action, "agent_key": key, "reply": reply},
    )
    return reply


async def _status(arg: str, ctx: CommandContext) -> str:
    """Answer `/status` from the run's ledger via the `status_monitor` reporter.

    There is no deterministic-digest fallback: `status_monitor` is a required role (so a
    misconfigured run fails at launch, not here), transient provider errors are already retried
    at the model layer, and a run whose provider is genuinely down cannot advance anyway — a
    hand-rolled digest would report on a run that is not moving. A raise here answers with the
    error rather than a second, lesser view of the same log.
    """
    return await ctx.status_answer(arg)


def _safe_roster(ctx: CommandContext) -> list[RosterEntry]:
    try:
        return ctx.roster()
    except Exception:  # noqa: BLE001
        return []


def make_status_answer(
    model: object, jsonl_path: Path | None, *, monitor: Any | None = None
) -> Callable[[str], "Awaitable[str]"]:
    """Build the ``/status`` answerer: a :class:`StatusMonitorAgent` investigating the run's ledger.

    It is handed tools over the log rather than a pre-made digest, because the digest was the
    limit: it dropped every event's ``data``, so the answers to the questions people actually ask
    ("what did the reviewer decide?", "why did the gate reject it?") were the part thrown away.
    The reporter now searches the index, narrows, and opens what it found.

    The system prompt is loaded from ``prompts/status_monitor.md`` — this is a runtime LLM like any
    other, so its instructions live in ``prompts/`` and not in this module (§4).
    """
    from auto_research.agents.roles import StatusMonitorAgent

    agent = StatusMonitorAgent(model=model, monitor=monitor)

    async def _answer(question: str) -> str:
        return await agent.report(question, jsonl_path=jsonl_path)

    return _answer


async def wait_reply_or_stop(
    fut: "asyncio.Future[str]", run_control: RunControl | None, timeout: float
) -> str:
    """Await a human-reply future, returning :data:`STOP_SENTINEL` at once if ``/stop`` fires.

    The shared reply-wait for every command-capable connector: without a ``run_control`` it is a
    plain ``asyncio.wait_for``; with one it also wakes on ``run_control.stop_event`` so a role
    blocked on ``ask_human`` unblocks the instant a human sends ``/stop``. Raises ``TimeoutError``
    past ``timeout`` (unchanged behavior).
    """
    if run_control is None:
        return await asyncio.wait_for(fut, timeout=timeout)
    stop_task = asyncio.ensure_future(run_control.stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {fut, stop_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise TimeoutError("no human reply within the response window")
        if fut in done:
            return fut.result()
        return STOP_SENTINEL  # /stop won the race
    finally:
        stop_task.cancel()


__all__ = [
    "CommandContext",
    "dispatch_command",
    "wait_reply_or_stop",
    "make_status_answer",
    "RosterEntry",
]
