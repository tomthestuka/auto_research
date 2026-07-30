"""Channel-agnostic HITL slash-command dispatch (§12 Stage 3).

Command-capable connectors (Telegram/Slack — always-listening) recognize a leading-``/``
message and route it here **instead of** treating it as a reply to a pending LLM query, so a
human can drive the run at any time:

- ``/help`` — list commands + the live team/role/model roster.
- ``/status [question]`` — report the run's state (LLM-answered when a status model is wired,
  else a deterministic digest of the monitor JSONL).
- ``/steer <role> <message>`` — queue a message for a role (delivered at its next ReAct step);
  ``/steer all`` broadcasts to every role.
- ``/stop`` — request an honest, run-wide wind-down.

All state lives on the shared :class:`~auto_research.run_control.RunControl`; this module only
parses + routes and returns Markdown to send back. ``wait_reply_or_stop`` is the shared
reply-wait every command-capable connector uses so ``/stop`` unblocks a role awaiting a reply.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from auto_research.run_control import STOP_SENTINEL, RunControl

# Roster entry: (team, agent_key, model_id). The human addresses a role by its **id** — its
# 1-based position in this list, printed by `/help` — so nothing has to be retyped from memory on
# a phone keyboard; `agent_key` remains the internal steer-inbox key.
RosterEntry = tuple[str, str, str]

# Every reply an invalid command produces starts with this, so a human sees at a glance that
# nothing was queued or acted on (a command either takes effect or says why, immediately).
_REJECTED = "⚠️ "


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
    # Set in Stage 3.3: (question) -> Markdown report via a light status model. When None,
    # /status falls back to a deterministic digest so it never dead-ends.
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
        ("/stop", "Wind the whole run down to an honest terminal."),
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


async def dispatch_command(text: str, ctx: CommandContext) -> str:
    """Parse and run a ``/command …`` line; return Markdown to send back to the human.

    Every outcome is immediate and deterministic: a command either takes effect or answers with
    what was wrong and what the valid options are. Nothing is queued on a guess.
    """
    text = (text or "").strip()
    parts = text.lstrip("/").split(maxsplit=1)
    name = parts[0].lower().split("@", 1)[0] if parts and parts[0] else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    await _record_command(ctx, "hitl.command", level=2, summary=f"human sent /{name or 'help'}",
                          data={"text": text[:400], "command": name})

    if name in ("", "help"):
        reply, accepted = _help(ctx), True
    elif name == "stop":
        reply, accepted = await _stop(ctx), True
    elif name == "steer":
        reply, accepted = _steer(arg, ctx)
    elif name == "status":
        reply, accepted = await _status(arg, ctx), True
    else:
        reply, accepted = f"{_REJECTED}Unknown command `/{name}`.\n\n{_help(ctx)}", False

    await _record_command(
        ctx, "hitl.command.reply", level=3,
        summary=f"/{name or 'help'} {'accepted' if accepted else 'rejected'}",
        data={"command": name, "accepted": accepted, "reply": reply[:400]},
    )
    return reply


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


async def _stop(ctx: CommandContext) -> str:
    """Stop the run — forcefully, with no modifier to remember.

    A stop that politely waits for a "safe point" is not a stop: on 2026-07-29 the next safe point
    was behind a batch of 25-minute downloads, so the acknowledged stop could not take effect for
    hours. So this also **cancels in-flight tool calls** (each returns an honest failure to its
    caller, keeping the transcript valid), which brings every loop to its next poll immediately.
    """
    ctx.run_control.request_stop("human /stop", by="human")
    cancelled = await ctx.run_control.abort_all()
    detail = (
        f" Cancelled {cancelled} in-flight tool call(s)."
        if cancelled
        else " Nothing was mid-call."
    )
    return (
        "🛑 **Stop requested.**" + detail + " The run winds down to an honest terminal at each "
        "role's next step, and any role waiting on your reply is being released."
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
        data={"action": action, "agent_key": key, "reply": reply[:200]},
    )
    return reply


async def _status(arg: str, ctx: CommandContext) -> str:
    if ctx.status_answer is not None:
        try:
            return await ctx.status_answer(arg)
        except Exception as exc:  # noqa: BLE001 — fall back to the deterministic digest, never dead-end
            return f"_(status model unavailable: {exc}; showing a plain digest)_\n\n{_deterministic_status(ctx)}"
    return _deterministic_status(ctx)


def _safe_roster(ctx: CommandContext) -> list[RosterEntry]:
    try:
        return ctx.roster()
    except Exception:  # noqa: BLE001
        return []


def _read_events(path: Path | None) -> list[dict]:
    """Read the run's monitor JSONL into a list of event dicts (best-effort, tolerant)."""
    if path is None:
        return []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _deterministic_status(ctx: CommandContext, *, tail: int = 10) -> str:
    """A no-LLM run digest from the monitor JSONL: totals + the last few significant events."""
    events = _read_events(ctx.monitor_jsonl_path)
    if not events:
        return "No run activity recorded yet."
    if ctx.run_control.stop_requested:
        head = "🛑 Stop requested — the run is winding down.\n\n"
    else:
        head = ""
    significant = [e for e in events if int(e.get("level", 10)) <= 4]
    recent = significant[-tail:] or events[-tail:]
    lines = [f"{head}**Run status** — {len(events)} events recorded."]
    lines.append("Recent activity:")
    for e in recent:
        lines.append(f"- `{e.get('team', '?')}` · {e.get('event', '?')} — {e.get('summary', '')}")
    return "\n".join(lines)


def _status_digest(events: list[dict], *, max_events: int = 80, max_chars: int = 8000) -> str:
    """Compact the recent monitor events into a digest string for the status model."""
    lines = [
        f"[L{e.get('level', '?')}] {e.get('team', '?')} · {e.get('event', '?')} — {e.get('summary', '')}"
        for e in events[-max_events:]
    ]
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


def make_status_answer(
    model: object, jsonl_path: Path | None
) -> Callable[[str], "Awaitable[str]"]:
    """Build the ``/status`` answerer: a light model summarizes the run's event-log digest on
    demand (a general report, or an answer to the trailing question). Report-only: the prompt
    keeps it to what the log shows. Wired into :class:`CommandContext` by ``run_research``.

    The system prompt is loaded from ``prompts/status_monitor.md`` — this is a runtime LLM like any
    other, so its instructions live in ``prompts/`` and not in this module (§4).
    """
    from auto_research.agents.prompts import load_prompt
    from auto_research.models import ModelMessage

    system = load_prompt("status_monitor")

    async def _answer(question: str) -> str:
        events = _read_events(jsonl_path)
        if not events:
            return "No run activity recorded yet."
        user = f"Event log digest:\n\n{_status_digest(events)}\n\n"
        user += f"Question: {question}" if question.strip() else "Give a general status report."
        resp = await model.call(  # type: ignore[attr-defined]
            [ModelMessage(role="system", content=system), ModelMessage(role="user", content=user)]
        )
        return (getattr(resp, "content", "") or "").strip() or "(status model returned nothing)"

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
