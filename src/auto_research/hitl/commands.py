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

from auto_research.run_control import STOP_SENTINEL, RunControl

# Roster entry: (team, agent_key, model_id). agent_key is the token `/steer <role>` accepts.
RosterEntry = tuple[str, str, str]


@dataclass
class CommandContext:
    """Run-scoped context the dispatcher needs, wired onto a connector by ``run_research``."""

    run_control: RunControl
    roster: Callable[[], list[RosterEntry]]
    monitor_jsonl_path: Path | None = None
    # Set in Stage 3.3: (question) -> Markdown report via a light status model. When None,
    # /status falls back to a deterministic digest so it never dead-ends.
    status_answer: Callable[[str], Awaitable[str]] | None = None


def _known_commands() -> list[tuple[str, str]]:
    return [
        ("/help", "List commands and the live team/role/model roster."),
        ("/status [question]", "Report the run's state; add a question to ask about it."),
        ("/steer <role> <message>", "Send a message to a role's next step (role from /help; `all` = every role)."),
        ("/stop", "Wind the whole run down to an honest terminal."),
    ]


async def dispatch_command(text: str, ctx: CommandContext) -> str:
    """Parse and run a ``/command …`` line; return Markdown to send back to the human."""
    text = (text or "").strip()
    parts = text.lstrip("/").split(maxsplit=1)
    if not parts or not parts[0]:
        return _help(ctx)
    name = parts[0].lower().split("@", 1)[0]  # tolerate a Telegram /cmd@botname suffix
    arg = parts[1].strip() if len(parts) > 1 else ""
    if name == "help":
        return _help(ctx)
    if name == "stop":
        return _stop(ctx)
    if name == "steer":
        return _steer(arg, ctx)
    if name == "status":
        return await _status(arg, ctx)
    return f"Unknown command `/{name}`.\n\n{_help(ctx)}"


def _help(ctx: CommandContext) -> str:
    lines = ["**Commands**"]
    lines += [f"- `{name}` — {desc}" for name, desc in _known_commands()]
    lines.append("")
    lines.append("**Roster** — team · role · model (use the role with `/steer`):")
    try:
        roster = ctx.roster()
    except Exception:  # noqa: BLE001 — a roster failure must not break /help
        roster = []
    if roster:
        lines += [f"- `{team}` · `{key}` · `{model}`" for team, key, model in roster]
    else:
        lines.append("- (roster unavailable)")
    return "\n".join(lines)


def _stop(ctx: CommandContext) -> str:
    ctx.run_control.request_stop("human /stop", by="human")
    return (
        "🛑 **Stop requested.** The run will wind down to an honest terminal at the next safe "
        "point, and any role waiting on your reply is being released."
    )


def _steer(arg: str, ctx: CommandContext) -> str:
    bits = arg.split(maxsplit=1)
    if len(bits) < 2 or not bits[1].strip():
        return "Usage: `/steer <role> <message>` — role from `/help`, or `all`."
    role, message = bits[0], bits[1].strip()
    keys = [key for _, key, _ in _safe_roster(ctx)]
    if role == "all":
        for key in keys:
            ctx.run_control.add_steer(key, message)
        return f"📨 Steer queued for all {len(keys)} role(s); each sees it at its next step."
    if role not in keys:
        listing = "\n".join(f"- `{k}`" for k in keys) or "- (none)"
        return f"Unknown role `{role}`. Known roles:\n{listing}"
    ctx.run_control.add_steer(role, message)
    return f"📨 Steer queued for `{role}`; it will see it at its next step."


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
    """
    from auto_research.models import ModelMessage

    system = (
        "You monitor a live multi-agent research run. Given a digest of its event log (most "
        "recent last), give a brief, concrete status report; if a question is asked, answer it "
        "from the log. Report only what the log shows — say so if it does not contain the answer. "
        "Be concise: a few sentences."
    )

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
