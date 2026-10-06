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
* **Escalation.** The coder's ``message`` tool (to the lead or the human) is served over a Streamable-HTTP MCP endpoint
  running in this process, so their handlers still reach the live lead object and the real HITL
  connector. See :mod:`auto_research.sandbox.coder.escalation_server`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from auto_research.models.keys import env_var_names, resolve_key
from auto_research.sandbox.coder.events import cap_text, emit


# Item types whose completion is worth one ledger line, mapped to how they are summarised.
_TOOL_ITEM_TYPES = {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}

# The notification methods that end a turn. Read in two places on purpose: the poller thread stops
# itself on one (so no thread outlives the turn it was started for) and `_record_notification`
# reports the same fact to the drive loop. One set, so the two can never disagree about what
# "the turn is over" means.
_TERMINAL_TURN_METHODS = frozenset({"turn/completed", "turn/failed", "turn/aborted"})


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


#: The key a Codex author launches with, and a log-safe name for its slot.
#:
#: Re-exported rather than defined here: the chat adapters need the identical resolution, and a
#: run must not authenticate two of its own actors from different keys. The rule and its history
#: live with the key pool (``models.keys.resolve_key``); this name is kept because the harness,
#: its launch probe and their tests all address it.
resolve_vendor_key = resolve_key


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
        # A member that names no variable authenticates from its provider's standard one. The
        # launch check has always derived it that way (`env.provider_key_aliases`), and this
        # class did not: `key_env` gates the child's environment overlay, the API-key login and
        # the `env_key` we hand Codex, so an absent field silently launched an author with no
        # credential at all while preflight passed on the very variable it could not see. Measured
        # 2026-09-12 — every Codex turn returned `401 Unauthorized: Authentication Fails
        # (governor)` against a key that answers `/responses` with 200 when sent by hand. The chat
        # path already reads this way (`_responses.py` falls back to its vendor's `key_env`), so
        # this makes the two agree rather than inventing a rule.
        self._key_env = member.key_env or env_var_names(member.provider)[0]
        self._env = env
        self._writable_roots = writable_roots or [str(worktree)]
        self._monitor = monitor
        self._codex_bin = codex_bin
        self._client_factory = client_factory
        self._client: Any | None = None
        self._thread_id: str | None = None
        # The turn currently in flight, or None between turns. `turn_steer` is pinned to it, so
        # this is the one piece of per-turn state the lead's side needs to reach into a running
        # turn; kept here rather than as a local in `drive` for exactly that reason.
        self._turn_id: str | None = None

    # ---------------------------------------------------------------- vendor key
    def _live_vendor_key(self) -> tuple[str, str]:
        """This author's launch key and its slot; see :func:`resolve_vendor_key`."""
        return resolve_vendor_key(self._provider, self._key_env)

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
            value, slot = self._live_vendor_key()
            if value:
                if slot == "env":
                    # Nothing more authoritative than the environment for this vendor, so an
                    # inherited value is left alone.
                    env.setdefault(self._key_env, value)
                else:
                    # A pooled key is what the rest of the run is using, so it wins over whatever
                    # the inherited environment carries: the point is not to launch on a key the
                    # run has already rotated away from.
                    env[self._key_env] = value
        # Jailed, the CLI's settings directory has to be one that exists *inside* the jail. It
        # defaults to the operator's own, which is not bound there, and the CLI refuses to start
        # rather than degrade — every Codex author died this way on the first jailed sweep. Set only
        # when jailed, so the unjailed path keeps whatever the operator has configured.
        from auto_research.sandbox.jail import codex_settings_home, jail_enabled

        if jail_enabled():
            env["CODEX_HOME"] = str(codex_settings_home(self._jail_label()))
        # Its own HEASoft parameter folder (F23), in its own home, which its jail lets it write.
        from auto_research.sandbox.jail import coder_home
        from auto_research.sandbox.runner import with_private_param_dir

        env = with_private_param_dir(env, coder_home(self._jail_label()) / "pfiles")
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
        key = env.get(self._key_env) or self._live_vendor_key()[0]
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
        from auto_research.sandbox.jail import jail_enabled

        # One enforcement layer, not two. This harness's own sandbox **is** bubblewrap, so running
        # it inside our bubblewrap jail nests the same mechanism: Codex cannot spawn its
        # ``codex-linux-sandbox`` helper, every shell command it issues is rejected, and the author
        # reads nothing and writes nothing — which reaches the gate as an empty changeset and reads
        # like a weak model rather than like broken plumbing (measured 2026-08-20, the coder
        # diagnosed it itself and escalated).
        #
        # So when the kernel jail is on, this harness's own sandbox stands down and the jail is the
        # wall. That is only safe because ``jail.coder_policy`` tightens the author's writable roots
        # to the working tree and its own settings home, which is **stricter** than the
        # ``workspace-write`` mode being given up: the observation database, the runtime directory
        # and the environments are all read-only, so an author cannot write data files anywhere.
        # The approval policy stays ``untrusted`` either way, so the path guard still sees the
        # commands it can see — belt, with the kernel as the wall.
        params: dict[str, Any] = {
            "cwd": str(self._worktree),
            "model": self._model,
            # ``never`` under the jail, and this is not a relaxation of anything that worked.
            # With the harness sandbox on, a command it already permitted was never offered to the
            # client (so this was never the command gate); standing that sandbox down routed
            # command approvals to a handler for the first time, and that path has never worked —
            # `python`, `pytest` and every compound shell command came back rejected, so the author
            # could not run the tests it had just written. Measured 2026-08-20 across three trials,
            # with the lead telling the coder those binaries were "blocked by design". The kernel
            # jail already makes anything outside the allow-list *absent*, which is stronger than a
            # command-string check, so the approval round-trip buys nothing here and costs the
            # author its own test run. What is genuinely given up is the ``PathGuard`` command
            # audit for this harness while jailed; it was documented best-effort (§13) and the
            # jail supersedes it.
            "approvalPolicy": "never" if jail_enabled() else "untrusted",
            "approvalsReviewer": "user",
            "sandbox": "danger-full-access" if jail_enabled() else "workspace-write",
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
            coder_home,
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
        target = self._worktree if self._worktree.is_dir() else Path(".")
        # A label of this author's own, so concurrent authors do not share one settings directory:
        # a sweep runs several at once, and the CLI writes login state and history into it.
        label = self._jail_label()
        policy = coder_policy(label=label, extra_rw=(str(target),))
        # The launcher lives in this author's own home, NOT in the working tree.
        #
        # Writing it into the tree left an untracked `.codex-jailed` behind after every session, so
        # the *next* tool in an authoring chain found a dirty branch, `check_toolkit_branch_clean`
        # raised, and the chain took the §9.6 honest terminal. A codex author under the jail could
        # therefore land exactly ONE tool. Measured 2026-09-01: a five-tool chain committed
        # `load_data` and died on link 2 with one untracked file in `git status`. Invisible until
        # then because `author_bench` authors a single tool per trial and never reaches link 2.
        #
        # The home is bound writable by the policy and is unique per author, so the launcher is
        # reachable inside the jail from there; `extra_rw` grants the working tree separately, so
        # nothing about the sandbox depends on the script sitting inside the repository. Keeping
        # runtime artifacts out of `research_toolkit` is also what §9.9 asks for: the only thing
        # the runtime puts in that repository is an `approve_edit` commit.
        return str(write_launcher(coder_home(label) / ".codex-jailed", real, policy))

    def _jail_label(self) -> str:
        """A settings-home label unique to this author.

        Derived from the working tree's **full** path, not its name: every trial clones its toolkit
        into a directory called ``repo``, so a name-derived label collided across every concurrent
        author and they raced to initialise one state database (2026-08-20).
        """
        import hashlib

        digest = hashlib.sha1(str(self._worktree.resolve()).encode()).hexdigest()[:10]
        return f"codex-{digest}"

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
        """Send one instruction, stream its items to the ledger, and return the final answer.

        A ``/stop`` lands here as a cancellation, and two properties keep it from wedging the
        process (both measured 2026-09-12; see ``sandbox/DOCS.md``). The notification poll runs on
        a **daemon thread this method owns** rather than on the default executor, because a
        cancel cannot reach a thread blocked in ``queue.Queue.get()`` by any mechanism and the
        executor's non-daemon workers are joined at interpreter exit with no timeout
        (bpo-39812) — so a stranded poll used to hang shutdown forever. And the turn is left
        **registered** on the cancelled path: the vendor wakes blocked waiters by walking its own
        ``turn_id -> queue`` dict when the client closes, so deregistering first removes us from
        the very lookup that would have told us to stop.
        """
        import asyncio

        if self._client is None or self._thread_id is None:
            raise RuntimeError("CodexBackend.drive called before connect()")
        started = await asyncio.to_thread(self._client.turn_start, self._thread_id, message)
        turn_id = started.turn.id
        await asyncio.to_thread(self._client.register_turn_notifications, turn_id)
        self._turn_id = turn_id
        texts: list[str] = []
        cancelled = False
        try:
            await self._drain_turn(turn_id, texts)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._turn_id = None
            if not cancelled:
                await asyncio.to_thread(self._client.unregister_turn_notifications, turn_id)
        return "\n".join(texts)

    async def steer(self, message: str) -> bool:
        """Append one message to the turn already running; False when there is none.

        `turn_steer` carries the turn id it expects, so a steer racing the end of a turn is
        refused by the server rather than landing in whatever comes next — the atomic-checkpoint
        rule the harness ecosystem converged on. A refusal is reported as "no turn in flight",
        which is what the caller needs to know: say it with `drive_coder` instead.
        """
        import asyncio

        client, thread_id, turn_id = self._client, self._thread_id, self._turn_id
        if client is None or thread_id is None or turn_id is None:
            return False
        try:
            await asyncio.to_thread(client.turn_steer, thread_id, turn_id, message)
        except Exception as exc:  # noqa: BLE001 — a lost race is a normal outcome, not a fault
            await emit(
                self._monitor, "coder.steer.refused", level=4,
                summary="the coder turn ended before the steer landed",
                data={"error": cap_text(str(exc), 300), "harness": self.harness},
            )
            return False
        await emit(
            self._monitor, "coder.steer", level=5,
            summary=f"steered the coder mid-turn: {cap_text(message, 60)}",
            data={"text": cap_text(message), "harness": self.harness},
        )
        return True

    async def _drain_turn(self, turn_id: str, texts: list[str]) -> None:
        """Record this turn's notifications, polling them on a daemon thread we own.

        The thread stops itself on a terminal notification, so nothing outlives a turn that ends
        normally. On a cancelled turn it stays blocked — which is safe precisely because it is
        daemonised: the interpreter abandons it at exit instead of joining it, and the client's
        own close-time wake reaches it anyway while ``drive`` leaves the turn registered.
        """
        import asyncio
        import threading

        loop = asyncio.get_running_loop()
        notes: asyncio.Queue[Any] = asyncio.Queue()

        def _hand_over(value: Any) -> bool:
            """Pass one item to the awaiting task; False once the loop is gone.

            A cancelled turn leaves this thread blocked until the client's close-time wake
            arrives, by which point the loop it was serving may already be closed. That wake is
            the thread's exit signal, not an error, so a closed loop ends the poll quietly rather
            than printing a teardown traceback nobody can act on.
            """
            try:
                loop.call_soon_threadsafe(notes.put_nowait, value)
            except RuntimeError:
                return False
            return True

        def _poll() -> None:
            client = self._client
            while True:
                try:
                    note = client.next_turn_notification(turn_id)
                except BaseException as exc:  # noqa: BLE001 — re-raised in the awaiting task
                    _hand_over(exc)
                    return
                if not _hand_over(note):
                    return
                if getattr(note, "method", "") in _TERMINAL_TURN_METHODS:
                    return

        threading.Thread(
            target=_poll, name=f"codex-poll-{turn_id}", daemon=True,
        ).start()

        while True:
            note = await notes.get()
            if isinstance(note, BaseException):
                raise note
            if await self._record_notification(note, texts):
                return

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
        return method in _TERMINAL_TURN_METHODS

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
