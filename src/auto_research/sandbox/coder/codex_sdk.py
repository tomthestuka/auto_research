"""The Codex author harness (CLAUDE.md §9.3, §6 provider-swappable author).

The Codex SDK talks JSON-RPC to a ``codex app-server`` subprocess. This backend maps that
harness onto the same three-method contract the authoring flow drives, and onto the same
``coder.*`` ledger events, so a run authored by Codex reads like a run authored by the Claude
Agent SDK and the two are comparable in one campaign.

Where the two harnesses genuinely differ, and how each difference is handled here:

* **Write jail.** ``workspace-write`` plus ``writableRoots`` is the primary gate here, and it is
  stronger than the Claude harness's: the allow-list is enforced by the OS (Landlock on Linux),
  not by a hook we can be routed around. Reads are the opposite: Codex grants full-disk read
  access by design, so a read outside the allow-list is *recorded* as
  ``coder.read_outside_allowlist`` rather than prevented. The preventive form is the OS-level jail
  of §12; the event is what makes the gap countable meanwhile.
* **Permission gate.** Codex has no per-tool allow-list. The app-server asks the client to approve
  a *patch* and to approve an action the sandbox would otherwise refuse, and this backend answers
  from the shared ``PathGuard`` allow-list. Measured 2026-08-19: a shell command the sandbox
  already permits arrives with **no** approval request, so the handler is a second line over
  patches and escalations rather than a gate on every command. Commands are confined by the
  sandbox above and audited below, which is why both exist.
* **Git.** ``.git`` is read-only inside a writable root under this sandbox, so the coder cannot
  commit even before our own rules apply. The §9.9 backstops (gate-entry auto-revert, per-attempt
  base-ref reset) are unchanged and remain the guarantee.
* **Cost.** Codex reports token counts per turn (``ThreadTokenUsage.last``), which map onto our
  own usage classes, so its spend is priced through the same path as every chat role rather than
  arriving as an opaque session dollar figure.
* **Escalation.** ``ask_lead`` and ``ask_human`` are served over a Streamable-HTTP MCP endpoint
  running in this process, so their handlers still reach the live lead object and the real HITL
  connector. See :mod:`auto_research.sandbox.coder.escalation_server`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from auto_research.sandbox.coder.events import cap_text, emit


# Item types whose completion is worth one ledger line, mapped to how they are summarised.
_TOOL_ITEM_TYPES = {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}


def _plain_path(value: Any) -> str:
    """Read a path out of a Codex field, whose types are often wrapped models.

    ``CommandExecutionThreadItem.cwd`` is a ``RootModel``, so ``str()`` on it yields
    ``root='/tmp/…'`` rather than the path — which silently turned every relative read into a
    path outside the allow-list (measured 2026-08-19).
    """
    if value is None:
        return ""
    inner = getattr(value, "root", value)
    return "" if inner is None else str(inner)


def _toml_literal(value: Any) -> str:
    """Render one config value the way the Codex CLI's ``--config`` parser reads it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    return json.dumps(str(value))  # JSON string escaping matches TOML's basic strings


def _flatten_overrides(config: dict[str, Any], prefix: str = "") -> list[str]:
    """Flatten a config tree into the ``key.path=value`` vector the CLI takes.

    Codex reads MCP servers and provider blocks when the **process** starts, not per thread
    (measured 2026-08-19: an escalation server passed in ``thread_start(config=…)`` never
    connected and its tools were absent from the author's surface). So the whole config is
    handed to the client as ``--config`` overrides instead.
    """
    flat: list[str] = []
    for key, value in config.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.extend(_flatten_overrides(value, f"{path}."))
        else:
            flat.append(f"{path}={_toml_literal(value)}")
    return flat


def _thread_config(*, writable_roots: list[str]) -> dict[str, Any]:
    """The config a coder session runs under.

    ``sandbox_workspace_write.writable_roots`` is the allow-list; network access stays off for
    spawned commands, since the author's own model traffic goes through the app-server rather
    than through anything it runs, and the tests it writes read local observation products.
    """
    return {
        "sandbox_workspace_write": {
            "writable_roots": writable_roots,
            "network_access": False,
            "exclude_slash_tmp": False,
        },
        "tools": {"web_search": True},
    }


class CodexBackend:
    """:class:`~auto_research.sandbox.coder.backend.CoderBackend` over one Codex thread."""

    harness = "codex"

    def __init__(
        self,
        *,
        member: Any,
        worktree: Path,
        system_prompt: str,
        guard: Any,
        escalation: Any | None = None,
        escalation_url: str | None = None,
        escalation_token_env: str | None = None,
        env: dict[str, str] | None = None,
        writable_roots: list[str] | None = None,
        monitor: Any | None = None,
        codex_bin: str | None = None,
        client_factory: Any | None = None,
    ) -> None:
        # The coder member (a RoleModelConfig) carries the model, the vendor, its endpoint and
        # its key variable, and is also what prices this harness's tokens (``record_call``).
        self._member = member
        self._worktree = Path(worktree)
        self._model = member.model
        self._provider = member.provider
        self._system_prompt = system_prompt
        self._guard = guard
        # An EscalationServer, owned by this backend: started before the thread (Codex connects
        # to the MCP endpoint at thread start) and closed with it.
        self._escalation = escalation
        self._escalation_url = escalation_url
        self._escalation_token_env = escalation_token_env
        self._base_url = member.base_url
        self._key_env = member.key_env
        self._env = env
        self._writable_roots = writable_roots or [str(worktree)]
        self._monitor = monitor
        self._codex_bin = codex_bin
        self._client_factory = client_factory
        self._client: Any | None = None
        self._thread_id: str | None = None

    # ---------------------------------------------------------------- permission gate

    ESCALATION_SERVER = "escalation"

    def approve(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """Answer one request the app-server routes to the client.

        Three shapes arrive here, and they do not share a reply vocabulary:

        * a **patch** approval and a sandbox **escalation** answer with ``{"decision": …}``. A
          command the sandbox already permits is never offered (measured 2026-08-19), so this is
          not the command gate; writable roots are.
        * an **MCP tool call** arrives as an *elicitation* (``mcpServer/elicitation/request``,
          ``_meta.codex_approval_kind = "mcp_tool_call"``) and takes the MCP spec's
          ``{"action": "accept" | "decline"}``. Answering it with a ``decision`` key reads as a
          rejection: the author sees "user rejected MCP tool call" and loses its escalation tools
          entirely (measured 2026-08-19 — `ask_lead` reached the model, was called, and never
          reached our handler).

        Our own escalation server is this process, so its tools are accepted; another server
        asking to run a tool is not something the runtime configured, so it is declined.
        """
        params = params or {}
        if method.endswith("elicitation/request"):
            server = str(params.get("serverName") or "")
            if server == self.ESCALATION_SERVER:
                return {"action": "accept", "content": {}}
            return {"action": "decline"}
        denied: str | None = None
        try:
            if method.endswith("commandExecution/requestApproval"):
                command = params.get("command") or params.get("item", {}).get("command") or ""
                if isinstance(command, list):
                    command = " ".join(str(part) for part in command)
                denied = self._guard.forbidden_in_command(str(command))
            elif method.endswith("fileChange/requestApproval"):
                for change in self._patch_paths(params):
                    if not self._guard.is_allowed(change):
                        denied = change
                        break
        except Exception:  # noqa: BLE001 — a guard bug must never brick authoring (fail-open)
            denied = None
        if denied is None:
            return {"decision": "accept"}
        return {
            "decision": "deny",
            "reason": (
                f"{denied} is outside the coder's filesystem allow-list "
                "(research_toolkit / obsdb / analyst-env / HEASoft); the auto_research "
                "orchestration repo and governance files are off-limits (§2/§4)."
            ),
        }

    @staticmethod
    def _patch_paths(params: dict[str, Any]) -> list[str]:
        """Every path a file-change approval request names."""
        changes = params.get("changes")
        if changes is None:
            changes = (params.get("item") or {}).get("changes") or []
        paths: list[str] = []
        for change in changes or []:
            path = change.get("path") if isinstance(change, dict) else None
            if path:
                paths.append(str(path))
        return paths

    # ---------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        import asyncio
        import os as _os

        if self._escalation is not None:
            await self._escalation.start()
            self._escalation_url = self._escalation.url
            self._escalation_token_env = self._escalation.token_env
            # The author process reads the bearer token from its own environment, so it has to
            # be stamped on before the client is built.
            self._env = {**(dict(self._env) if self._env else dict(_os.environ)),
                         **self._escalation.env()}
        self._client = await asyncio.to_thread(self._build_client)
        self._thread_id = await asyncio.to_thread(self._start_thread)

    def _build_client(self) -> Any:
        from openai_codex.client import CodexClient, CodexConfig

        from auto_research.sandbox.coder.codex_bin import configured_codex_bin

        # The SDK merges this into the inherited environment rather than replacing it (unlike the
        # Claude harness, which starts from the analyst init script's rebuilt env), so what is set
        # here is an overlay: the analyst science stack, the vendor key, the escalation token.
        env = dict(self._env) if self._env else dict(os.environ)
        # The harness authenticates from the vendor key named on the coder member; the SDK
        # resolves it inside the child, so it has to be present in the child's environment.
        if self._key_env:
            value = os.environ.get(self._key_env, "")
            if value:
                env.setdefault(self._key_env, value)
        overrides = [
            "preferred_auth_method=apikey",
            "forced_login_method=api",
            *_flatten_overrides(self._config_payload()),
        ]
        codex_bin = self._codex_bin or configured_codex_bin()
        jailed = self._jail_launcher(codex_bin)
        config = CodexConfig(
            # None lets the SDK resolve the binary it ships with, which is the version-matched
            # one; an explicit path is the escape hatch (see coder.codex_bin). With the OS jail on,
            # this is a wrapper that execs that same binary inside the jail.
            codex_bin=jailed or codex_bin,
            env=env,
            cwd=str(self._worktree),
            config_overrides=tuple(overrides),
        )
        factory = self._client_factory
        client = (
            factory(config, self.approve)
            if factory is not None
            else CodexClient(config=config, approval_handler=self.approve)
        )
        client.start()
        client.initialize()
        self._login(client, env)
        return client

    def _login(self, client: Any, env: dict[str, str]) -> None:
        """Authenticate with the vendor key, when one is configured."""
        if not self._key_env:
            return
        key = env.get(self._key_env) or os.environ.get(self._key_env, "")
        if not key:
            return
        from openai_codex.generated.v2_all import (
            ApiKeyLoginAccountParams,
            LoginAccountParams,
        )

        client.account_login_start(
            LoginAccountParams(root=ApiKeyLoginAccountParams(api_key=key, type="apiKey"))
        )

    def _start_thread(self) -> str:
        params: dict[str, Any] = {
            "cwd": str(self._worktree),
            "model": self._model,
            "approvalPolicy": "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "workspace-write",
            # Layered on top of Codex's own instructions rather than replacing them
            # (``baseInstructions``): the harness's account of its tools is harness-specific and
            # worth keeping, while `prompts/coder.md` is policy. Both were live-checked to be
            # honoured (2026-08-19).
            "developerInstructions": self._system_prompt,
            "ephemeral": True,
        }
        if self._provider != "openai":
            params["modelProvider"] = self._provider
        started = self._client.thread_start(params)
        return started.thread.id

    def _config_payload(self) -> dict[str, Any]:
        config = _thread_config(writable_roots=self._writable_roots)
        if self._base_url:
            # A non-native vendor is reached by declaring its provider block; the endpoint and
            # the key variable both come from the coder member (see coder.harness).
            config["model_providers"] = {
                self._provider: {
                    "name": self._provider,
                    "base_url": self._base_url,
                    "wire_api": "responses",
                    **({"env_key": self._key_env} if self._key_env else {}),
                }
            }
            config["model_provider"] = self._provider
        if self._escalation_url:
            server: dict[str, Any] = {"url": self._escalation_url}
            if self._escalation_token_env:
                server["bearer_token_env_var"] = self._escalation_token_env
            config["mcp_servers"] = {"escalation": server}
        # The member's own Codex settings, last so they win. This is how a model's requirements
        # reach the harness without being hardcoded here: `glm-5.3` refuses a turn outright unless
        # a reasoning effort is named ("This model always engages in thinking and cannot be
        # disabled; please use low, high, or max" — every turn failed with it, 2026-08-19), and
        # DeepSeek's own Codex page sets the same key.
        config.update(dict(getattr(self._member, "parameters", None) or {}))
        return config


    def _jail_launcher(self, codex_bin: str | None) -> str | None:
        """A wrapper running the app-server inside the OS jail, or ``None`` when the jail is off.

        This harness is the reason the jail exists: its reads are ungated by design, and a coder on
        it read this repository's own source unprompted (§13). The write jail it already applies
        (``writable_roots``) stays — belt and kernel — while the mount namespace is what finally
        makes a read outside the allow-list impossible rather than merely recorded.
        """
        from auto_research.sandbox.jail import (
            coder_policy,
            jail_available,
            jail_enabled,
            write_launcher,
        )

        if not jail_enabled():
            return None
        ok, _ = jail_available()
        if not ok:
            return None  # reported by the launch check; run as before rather than not at all
        real = codex_bin
        if not real:
            from auto_research.sandbox.coder.codex_bin import resolve_codex_bin

            try:
                real = resolve_codex_bin()
            except Exception:  # noqa: BLE001 — let the SDK report a missing binary as it always did
                return None
        # CODEX_HOME lives under the worktree's authoring dir, so the wrapper goes beside it.
        target = self._worktree if self._worktree.is_dir() else Path(".")
        policy = coder_policy(label="codex", extra_rw=(str(target),))
        return str(write_launcher(target / ".codex-jailed", real, policy))

    async def disconnect(self) -> None:
        import asyncio

        client, self._client = self._client, None
        try:
            if client is not None:
                await asyncio.to_thread(client.close)
        finally:
            escalation, self._escalation = self._escalation, None
            if escalation is not None:
                await escalation.aclose()

    # ---------------------------------------------------------------- one turn

    async def drive(self, message: str) -> str:
        """Send one instruction, stream its items to the ledger, and return the final answer."""
        import asyncio

        if self._client is None or self._thread_id is None:
            raise RuntimeError("CodexBackend.drive called before connect()")
        started = await asyncio.to_thread(self._client.turn_start, self._thread_id, message)
        turn_id = started.turn.id
        await asyncio.to_thread(self._client.register_turn_notifications, turn_id)
        texts: list[str] = []
        try:
            while True:
                note = await asyncio.to_thread(self._client.next_turn_notification, turn_id)
                done = await self._record_notification(note, texts)
                if done:
                    break
        finally:
            await asyncio.to_thread(self._client.unregister_turn_notifications, turn_id)
        return "\n".join(texts)

    async def _record_notification(self, note: Any, texts: list[str]) -> bool:
        """Mirror one notification into the run ledger; return True on turn completion."""
        payload = getattr(note, "payload", None)
        method = getattr(note, "method", "")

        item = getattr(payload, "item", None)
        if item is not None:
            root = getattr(item, "root", item)
            kind = getattr(root, "type", "")
            if kind == "agentMessage":
                text = getattr(root, "text", "") or ""
                if text.strip():
                    texts.append(text)
                    await emit(
                        self._monitor, "coder.text", level=8,
                        summary=cap_text(text.strip(), 80), data={"text": cap_text(text)},
                    )
            elif kind in _TOOL_ITEM_TYPES:
                await self._record_tool_item(root, kind)

        usage = getattr(payload, "token_usage", None)
        if usage is not None:
            await self._record_usage(usage)

        if method == "turn/completed":
            turn = getattr(payload, "turn", None)
            status = str(getattr(turn, "status", "") or "")
            error = cap_text(str(getattr(turn, "error", "") or ""), 600) or None
            failed = "fail" in status.lower() or "abort" in status.lower()
            await emit(
                self._monitor, "coder.turn_end", level=2 if failed else 5,
                summary=f"coder turn ended ({status or 'completed'})",
                data={"status": status, "error": error, "harness": self.harness},
            )
            if failed:
                # A failed turn must not read as an empty reply. The lead receives this text as
                # the coder's answer, and "" is indistinguishable from a no-op: the vendor's own
                # message is what makes an unprovisioned account or a bad model id legible
                # instead of looking like a silent author.
                texts.append(f"[harness error] the {self._provider} turn failed: {error}")
            return True
        return method in {"turn/failed", "turn/aborted"}

    async def _record_tool_item(self, root: Any, kind: str) -> None:
        data: dict[str, Any] = {"tool": kind}
        summary = f"coder → {kind}"
        if kind == "commandExecution":
            command = str(getattr(root, "command", "") or "")
            data["input"] = cap_text(command)
            data["exit_code"] = getattr(root, "exit_code", None)
            output = getattr(root, "aggregated_output", None)
            if output:
                data["text"] = cap_text(str(output))
            summary = f"coder → shell: {cap_text(command, 60)}"
            await self._audit_reads(root)
        elif kind == "fileChange":
            paths = [getattr(c, "path", "") for c in getattr(root, "changes", []) or []]
            data["input"] = json.dumps(paths)
            summary = f"coder → edit {cap_text(', '.join(paths), 60)}"
        elif kind == "mcpToolCall":
            tool = getattr(root, "tool", "")
            data["tool"] = f"{getattr(root, 'server', '')}.{tool}"
            data["input"] = cap_text(json.dumps(getattr(root, "arguments", None), default=str))
            summary = f"coder → {data['tool']}"
        elif kind == "webSearch":
            data["input"] = cap_text(str(getattr(root, "query", "") or ""))
            summary = f"coder → web_search: {cap_text(data['input'], 60)}"
        await emit(self._monitor, "coder.tool_use", level=7, summary=summary, data=data)

    async def _audit_reads(self, root: Any) -> None:
        """Record a read of a path outside the allow-list.

        Codex grants full-disk read access by design and asks for no approval to read, so this
        is a detective control, not a preventive one: it makes a boundary crossing countable
        while the preventive OS-level jail (§12) is unbuilt.

        Paths come from Codex's own parse of the command and are usually **relative** to the
        command's working directory. Resolving them against this process's cwd instead put every
        ordinary in-worktree read outside the allow-list (47 false events in one session,
        measured 2026-08-19), which would have made the audit useless by drowning a real crossing.
        """
        actions = getattr(root, "command_actions", None) or []
        base = Path(_plain_path(getattr(root, "cwd", None)) or self._worktree)
        outside: list[str] = []
        for action in actions:
            inner = getattr(action, "root", action)
            for attr in ("path", "paths", "file", "files"):
                value = getattr(inner, attr, None)
                if value is None:
                    continue
                candidates = value if isinstance(value, (list, tuple)) else [value]
                for candidate in candidates:
                    try:
                        resolved = Path(_plain_path(candidate) or "")
                        if not str(resolved) or str(resolved) == ".":
                            continue
                        if not resolved.is_absolute():
                            resolved = base / resolved
                        if not self._guard.is_allowed(str(resolved)):
                            outside.append(str(resolved))
                    except Exception:  # noqa: BLE001 — auditing never breaks a turn
                        continue
        if outside:
            await emit(
                self._monitor, "coder.read_outside_allowlist", level=3,
                summary=f"coder read {len(outside)} path(s) outside the allow-list",
                data={"paths": outside[:20], "harness": self.harness},
            )

    async def _record_usage(self, usage: Any) -> Any:
        """Book this turn's tokens on the run's cost meter.

        Codex reports real token counts, so unlike the Claude harness's session dollar figure this
        spend is priced by the same code that prices every chat role, from the coder member's own
        entry in ``config/models.yaml``.
        """
        from auto_research.models.types import ModelUsage
        from auto_research.observability import current_cost_meter

        meter = current_cost_meter()
        last = getattr(usage, "last", None)
        if meter is None or last is None:
            return None
        cached = int(getattr(last, "cached_input_tokens", 0) or 0)
        total_input = int(getattr(last, "input_tokens", 0) or 0)
        try:
            return await meter.record_call(
                "coder",
                self._member,
                ModelUsage(
                    # Codex reports total input with the cached part called out, so uncached
                    # input is the difference (the same normalization the OpenAI chat adapter does).
                    input_tokens=max(total_input - cached, 0),
                    cache_read_tokens=cached,
                    cache_write_tokens=int(getattr(last, "cache_write_input_tokens", 0) or 0),
                    output_tokens=int(getattr(last, "output_tokens", 0) or 0),
                    reasoning_tokens=int(getattr(last, "reasoning_output_tokens", 0) or 0),
                ),
            )
        except Exception:  # noqa: BLE001 — cost accounting never breaks an authoring turn
            return None
