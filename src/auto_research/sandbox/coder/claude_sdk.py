"""The Claude Agent SDK author harness (CLAUDE.md §9.3).

This is the harness Phase 2 was built on: the SDK spawns the `claude` CLI as a subprocess whose
cwd is the toolkit checkout being edited, seeded with `prompts/coder.md` and the brief, holding
its own filesystem gate and the two escalation tools.

Everything here is specific to that SDK. What the authoring flow needs from it is the three
methods of :class:`~auto_research.sandbox.coder.backend.CoderBackend`; the flow itself is defined
over the working tree, not over this process.

Four wires are worth naming because they are the ones a second harness has to reproduce:

* **Filesystem gate** — a ``PreToolUse`` hook, which runs first in the SDK's permission flow and
  fires for every tool call, consulting the shared ``PathGuard`` allow-list in-process.
* **Git ban** — a ``Bash`` deny-rule, best-effort by construction; the robust backstops are the
  gate-entry auto-revert and the per-attempt base-ref reset (§9.9).
* **Escalation** — an in-process SDK MCP server, so ``ask_lead`` and ``ask_human`` run against
  the live lead object and the real HITL connector rather than a copy.
* **Cost** — the SDK reports a session-cumulative dollar figure, so spend enters the run's meter
  as a per-turn delta keyed by session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from auto_research.sandbox.coder.events import CODER_TEXT_CAP, cap_text, emit


def _make_escalation_server(
    ask_lead: Callable[..., Awaitable[str]], ask_human: Callable[..., Awaitable[str]]
):
    """Build the in-process MCP server exposing ``ask_lead`` and ``ask_human`` (§9.7)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    _msg_schema = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Your question, in Markdown — the ask first, then any needed facts.",
            }
        },
        "required": ["message"],
    }

    @tool(
        "ask_lead",
        "Ask the analyst lead a code/convention/which-lib-function question (Markdown).",
        _msg_schema,
    )
    async def _ask_lead(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ask_lead(args["message"])
        return {"content": [{"type": "text", "text": answer}]}

    @tool(
        "ask_human",
        "Escalate a genuine expert-science unknown to the real human (Markdown).",
        _msg_schema,
    )
    async def _ask_human(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ask_human(args["message"])
        return {"content": [{"type": "text", "text": answer}]}

    return create_sdk_mcp_server("escalation", tools=[_ask_lead, _ask_human])


def _make_path_confiner(worktree: Path):
    """DEPRECATED — do not use for confinement (see CLAUDE.md "Loose ends").

    A ``can_use_tool`` callback that denied file writes outside the worktree. The official
    Claude Agent SDK permission flow runs ``can_use_tool`` **last** and **skips it** under
    ``permission_mode="acceptEdits"`` / when an ``allowed_tools`` rule matches — so it never
    fired here (it could not gate reads or Bash at all). Superseded by the ``PreToolUse`` hook
    :func:`_make_fs_guard_hook` (runs first, fires for every tool call) enforcing the shared
    :class:`~auto_research.sandbox.fs_guard.PathGuard` allow-list. Kept for reference; unwired.
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    write_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    root = worktree.resolve()

    async def can_use_tool(name: str, args: dict[str, Any], ctx: Any) -> Any:
        if name in write_tools:
            target = args.get("file_path") or args.get("path") or args.get("notebook_path")
            if target is not None:
                resolved = Path(str(target)).resolve()
                if resolved != root and root not in resolved.parents:
                    return PermissionResultDeny(message=f"path outside worktree: {target}")
        return PermissionResultAllow()

    return can_use_tool


_FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"}


def _make_fs_guard_hook(guard):
    """A Claude Agent SDK ``PreToolUse`` hook enforcing the shared filesystem allow-list
    (:class:`~auto_research.sandbox.fs_guard.PathGuard`) on the coder.

    The correct gate: ``PreToolUse`` runs **first** in the permission flow and fires for **every**
    tool call (unlike ``can_use_tool`` — see the deprecated :func:`_make_path_confiner`). File
    tools are checked by their path argument (exact); ``Bash`` by the absolute paths *named* in its
    command (best-effort — a subprocess's implicit reads are invisible to the hook; the complete
    fix is the OS-level jail, §12 / Loose ends). The system's ``auto_research`` repo and governance
    files are not on the allow-list, so they are denied (§2/§4). Fail-open on an unexpected internal
    error so a guard bug never bricks authoring.
    """

    async def hook(input_data, tool_use_id, context):  # noqa: ANN001 — SDK callback signature
        try:
            tool = input_data.get("tool_name", "")
            ti = input_data.get("tool_input") or {}
            denied: str | None = None
            if tool in _FILE_TOOLS:
                target = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
                if target is not None and not guard.is_allowed(target):
                    denied = str(target)
            elif tool == "Bash":
                denied = guard.forbidden_in_command(ti.get("command", ""))
            if denied is not None:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"{denied} is outside the coder's filesystem allow-list "
                            f"(research_toolkit / obsdb / analyst-env / HEASoft); the auto_research "
                            f"orchestration repo and governance files are off-limits (§2/§4)."
                        ),
                    }
                }
        except Exception:  # noqa: BLE001 — a guard bug must never brick authoring (fail-open)
            return {}
        return {}

    return hook


def _build_options(*, worktree: Path, coder_model: str, system_prompt: str, escalation_server, max_turns: int, env: dict[str, str] | None = None):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from auto_research.sandbox.fs_guard import PathGuard

    # Filesystem jail: a PreToolUse hook (runs first, fires for every tool call) enforcing the
    # shared config allow-list. Replaces the deprecated can_use_tool confiner, which the SDK
    # skips under acceptEdits/allow-rules. Inactive when agent_fs_allow is unset (allow-all).
    fs_guard_hook = _make_fs_guard_hook(PathGuard.from_config())
    kwargs: dict[str, Any] = dict(
        cwd=str(worktree),
        model=coder_model,
        system_prompt=system_prompt,
        setting_sources=[],  # SDK-only: ignore any filesystem .claude/settings
        permission_mode="acceptEdits",
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="Read|Write|Edit|MultiEdit|NotebookEdit|Glob|Grep|Bash",
                    hooks=[fs_guard_hook],
                )
            ]
        },
        mcp_servers={"escalation": escalation_server},
        allowed_tools=[
            "Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "mcp__escalation__ask_lead", "mcp__escalation__ask_human",
        ],
        # The coder never issues git (§9.9): staging/commit is the runtime's job
        # (``approve_edit`` only, after gate-pass ∧ review-pass). Deny-rules are evaluated
        # first and apply under acceptEdits; prefix-matching is best-effort (pipes/subshells
        # can slip), so the per-attempt base-ref reset (``_reset_direct_tree``) is the robust
        # backstop that drops any commit a coder still lands. Scoped to the coder by being on
        # the coder's own options, not global settings.
        disallowed_tools=["Bash(git:*)", "Bash(git)"],
        max_turns=max_turns,
    )
    env = _with_live_anthropic_key(env)
    if env:  # full sourced env (analyst conda + HEASoft); omit when unset so the SDK inherits.
        kwargs["env"] = env
    return ClaudeAgentOptions(**kwargs)


def _with_live_anthropic_key(env: dict[str, str] | None) -> dict[str, str] | None:
    """Stamp the currently-live Anthropic key onto the coder's env, when it has rotated.

    The coder is the Claude Agent SDK — a **separate process** that authenticates from its
    own environment, so a key rotation inside this process (see
    ``auto_research.models.keys``) does not reach it. A session started after a rotation
    would otherwise launch on the throttled or exhausted key.

    Untouched in the common case: while the live key still equals ``ANTHROPIC_API_KEY``
    there is nothing to override, so ``None`` stays ``None`` and the SDK keeps inheriting
    the parent environment exactly as before. Only a rotated key materializes a dict.
    A session already in flight keeps the key it launched with — the SDK owns that
    subprocess's environment for its lifetime.
    """

    import os

    from auto_research.models.keys import ANTHROPIC, active_key

    live = active_key(ANTHROPIC)
    if not live or live == os.environ.get("ANTHROPIC_API_KEY"):
        return env
    merged = dict(env) if env else dict(os.environ)
    merged["ANTHROPIC_API_KEY"] = live
    return merged


def _default_sdk_client_factory(options):
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


async def _record_coder_cost(result_message: Any, coder_model: str) -> float | None:
    """Feed one coder turn's spend to the run's cost meter; return the turn's delta in USD.

    ``ResultMessage.total_cost_usd`` is **session-cumulative** (verified against real runs: it grows
    monotonically across a session's turns), so the meter converts it to a per-turn delta keyed by
    ``session_id``. Returns ``None`` when the SDK reported no cost or no meter is installed.
    """

    from auto_research.observability import current_cost_meter

    cumulative = getattr(result_message, "total_cost_usd", None)
    meter = current_cost_meter()
    if cumulative is None or meter is None:
        return None
    try:
        return await meter.record_session_total(
            role="coder",
            member="claude_sdk",
            provider="anthropic",
            model=coder_model or "claude_sdk",
            session_id=str(getattr(result_message, "session_id", "") or "coder"),
            cumulative_usd=float(cumulative),
        )
    except Exception:  # noqa: BLE001 — cost accounting never breaks an authoring turn
        return None


async def _drive_author(
    client: Any, message: str, *, monitor: Any | None = None, coder_model: str = ""
) -> str:
    """Send one turn to the warm author session and drain its response (L0 runs inside).

    When a ``monitor`` is wired, each streamed block is mirrored to the run's JSONL so the
    otherwise-opaque SDK author session is legible: tool calls (file edits, bash, ask_*),
    tool results, narration, and the per-turn end summary. Logging never breaks the drain.

    The turn-end result also carries the SDK session's **cumulative** dollar cost, which is fed to
    the run's cost meter as a delta — the coder is a separate process making its own API calls, so
    this is the only path by which its spend (historically the largest unaccounted line) reaches
    the run's cost record.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    await client.query(message)
    chunks: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    if block.text.strip():
                        await emit(monitor, "coder.text", level=8,
                                    summary=cap_text(block.text.strip(), 80),
                                    data={"text": cap_text(block.text)})
                elif isinstance(block, ToolUseBlock):
                    await emit(
                        monitor, "coder.tool_use", level=7,
                        summary=f"coder → {block.name}",
                        data={"tool": block.name,
                              "input": cap_text(json.dumps(block.input, default=str, sort_keys=True))},
                    )
        elif isinstance(msg, UserMessage):
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, ToolResultBlock):
                    text = block.content if isinstance(block.content, str) else json.dumps(
                        block.content, default=str
                    )
                    await emit(
                        monitor, "coder.tool_result", level=7,
                        summary=f"coder tool result{' (error)' if block.is_error else ''}",
                        data={"is_error": bool(block.is_error), "text": cap_text(text)},
                    )
        elif isinstance(msg, ResultMessage):
            turn_usd = await _record_coder_cost(msg, coder_model)
            await emit(
                monitor, "coder.turn_end", level=5,
                summary=f"coder turn ended ({msg.num_turns} turns)",
                data={"num_turns": msg.num_turns, "stop_reason": getattr(msg, "stop_reason", None),
                      "cost_usd": getattr(msg, "total_cost_usd", None),
                      "cost_usd_turn": turn_usd,
                      "is_error": bool(getattr(msg, "is_error", False))},
            )
    return "\n".join(chunks)


class ClaudeSdkBackend:
    """:class:`~auto_research.sandbox.coder.backend.CoderBackend` over one SDK client.

    The client is injected rather than built here, which keeps the flow's existing
    ``sdk_client_factory`` seam intact: a test can hand in a fake with the SDK's four methods and
    exercise the whole authoring loop without a subprocess.
    """

    harness = "claude_sdk"

    def __init__(self, client: Any, *, monitor: Any | None = None, coder_model: str = "") -> None:
        self._client = client
        self._monitor = monitor
        self._coder_model = coder_model

    async def connect(self) -> None:
        await self._client.connect()

    async def drive(self, message: str) -> str:
        return await _drive_author(
            self._client, message, monitor=self._monitor, coder_model=self._coder_model
        )

    async def disconnect(self) -> None:
        await self._client.disconnect()
