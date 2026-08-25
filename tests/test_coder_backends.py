"""Author-harness plumbing: the harness handle, its legality table, and the two backends.

Deterministic only. No harness subprocess is launched and no model is called: what is asserted is
the config contract, the launch checks, the permission gate's decisions, the ledger mapping, and
the in-process escalation endpoint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_research.models.providers import provider_for
from auto_research.models.providers._harness_only import HarnessOnlyProvider
from auto_research.models.types import CHAT_PROVIDERS, RoleModelConfig
from auto_research.sandbox.coder.backend import CoderBackend
from auto_research.sandbox.coder.claude_sdk import ClaudeSdkBackend
from auto_research.sandbox.coder.codex_sdk import CodexBackend
from auto_research.sandbox.coder.escalation_server import EscalationServer
from auto_research.sandbox.coder.harness import (
    HARNESS_ENDPOINTS,
    check_coder_harness_supported,
)


# --------------------------------------------------------------------------- config contract


def _member(**kwargs) -> RoleModelConfig:
    base = {"name": "m", "provider": "anthropic", "model": "claude-sonnet-5"}
    return RoleModelConfig(**{**base, **kwargs})


def test_a_chat_member_needs_no_harness_fields():
    """The new coder fields are additive: every existing chat member stays valid untouched."""
    member = _member()
    assert member.harness is None
    assert member.base_url is None
    assert member.key_env is None


def test_harness_only_provider_resolves_but_refuses_a_chat_call():
    """The placeholder is still the answer for a vendor with no chat adapter.

    It used to be reached by naming DeepSeek, Z.AI or Alibaba, which all gained adapters on
    2026-08-24. Pinning it to those names measured the roster rather than the mechanism, so the
    check now uses a vendor that is deliberately not in the chat set: what must hold is that an
    unwired name resolves to something that raises with the reason, whichever name that is.
    """
    provider = provider_for("a-vendor-with-no-adapter")
    assert isinstance(provider, HarnessOnlyProvider)
    with pytest.raises(RuntimeError, match="only through a coder harness"):
        asyncio.run(provider.call())


def test_every_harness_vendor_can_also_be_reached_as_a_chat_member():
    """Each vendor a coder harness may drive now also backs a chat role.

    This is what makes the lead and co-lead axes configurable across vendors rather than only
    the author's. It is a roster fact, so it is asserted from the harness table rather than from
    a hand-written list that would drift from it.
    """
    for provider, _harness in HARNESS_ENDPOINTS:
        assert provider in CHAT_PROVIDERS
        assert not isinstance(provider_for(provider), HarnessOnlyProvider)


def test_chat_providers_still_get_their_real_adapters():
    for name in sorted(CHAT_PROVIDERS):
        assert not isinstance(provider_for(name), HarnessOnlyProvider)


# --------------------------------------------------------------------------- legality table


def test_every_documented_pairing_names_its_dialect():
    """Each non-native row carries the endpoint its vendor documents for that harness."""
    assert HARNESS_ENDPOINTS[("anthropic", "claude_sdk")] is None
    assert HARNESS_ENDPOINTS[("openai", "codex")] is None
    assert HARNESS_ENDPOINTS[("deepseek", "codex")] == "https://api.deepseek.com/"
    assert HARNESS_ENDPOINTS[("zai", "codex")] == "https://api.z.ai/api/v1"
    assert "compatible-mode" in HARNESS_ENDPOINTS[("dashscope", "codex")]


def test_impossible_and_unwired_pairings_are_both_absent():
    """Two absences are structural: Anthropic publishes no Responses endpoint and OpenAI no
    Anthropic-dialect one. Three more are deliberate: the alt vendors do publish an
    Anthropic-dialect endpoint, but ``claude_sdk`` sets no base URL or auth token, so a member
    naming one would silently author on Claude. The table lists only what the code can do."""
    assert ("anthropic", "codex") not in HARNESS_ENDPOINTS
    assert ("openai", "claude_sdk") not in HARNESS_ENDPOINTS
    assert not any(provider == "google" for provider, _ in HARNESS_ENDPOINTS)
    for vendor in ("deepseek", "zai", "dashscope"):
        assert (vendor, "claude_sdk") not in HARNESS_ENDPOINTS


def _patch_member(monkeypatch, member: RoleModelConfig) -> None:
    monkeypatch.setattr(
        "auto_research.sandbox.coder.harness.coder_member", lambda: member, raising=True
    )


def test_check_rejects_a_member_with_no_harness(monkeypatch):
    _patch_member(monkeypatch, _member())
    with pytest.raises(ValueError, match="names no harness"):
        check_coder_harness_supported()


def test_check_rejects_an_impossible_pairing(monkeypatch):
    _patch_member(monkeypatch, _member(harness="codex"))
    with pytest.raises(ValueError, match="does not exist"):
        check_coder_harness_supported()


def test_check_requires_an_endpoint_for_a_non_native_pairing(monkeypatch):
    _patch_member(
        monkeypatch, _member(provider="deepseek", harness="codex", model="deepseek-v4-pro")
    )
    with pytest.raises(ValueError, match="needs an explicit endpoint"):
        check_coder_harness_supported()


def test_check_requires_the_named_key_variable_to_be_set(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    _patch_member(
        monkeypatch,
        _member(
            provider="deepseek",
            harness="codex",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/",
            key_env="DEEPSEEK_API_KEY",
        ),
    )
    with pytest.raises(ValueError, match=r"\$DEEPSEEK_API_KEY"):
        check_coder_harness_supported()


def test_check_passes_a_fully_configured_alt_vendor(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key")
    _patch_member(
        monkeypatch,
        _member(
            provider="deepseek",
            harness="codex",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/",
            key_env="DEEPSEEK_API_KEY",
        ),
    )
    check_coder_harness_supported()


def test_the_shipped_config_is_a_legal_pairing():
    """The configured coder must be launchable, since the role is required at every launch."""
    check_coder_harness_supported()


# --------------------------------------------------------------------------- backends


class _FakeSdkClient:
    def __init__(self) -> None:
        self.connected = False
        self.sent: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def query(self, message: str) -> None:
        self.sent.append(message)

    async def receive_response(self):
        return
        yield  # pragma: no cover — an empty async generator

    async def disconnect(self) -> None:
        self.connected = False


def test_both_backends_satisfy_the_interface():
    assert isinstance(ClaudeSdkBackend(_FakeSdkClient()), CoderBackend)
    assert isinstance(
        CodexBackend(
            member=_member(harness="codex", provider="deepseek", base_url="https://x/"),
            worktree=Path("/tmp"),
            system_prompt="p",
            guard=_AllowAll(),
        ),
        CoderBackend,
    )


def test_claude_backend_drives_its_injected_client():
    client = _FakeSdkClient()
    backend = ClaudeSdkBackend(client)
    asyncio.run(backend.connect())
    assert client.connected
    assert asyncio.run(backend.drive("go")) == ""
    assert client.sent == ["go"]
    asyncio.run(backend.disconnect())
    assert not client.connected


class _AllowAll:
    def is_allowed(self, path: str) -> bool:
        return True

    def forbidden_in_command(self, command: str):
        return None


class _DenyPath:
    """The shape of the real PathGuard: an allow-list over paths plus a command scan."""

    def __init__(self, denied: str) -> None:
        self.denied = denied

    def is_allowed(self, path: str) -> bool:
        return self.denied not in str(path)

    def forbidden_in_command(self, command: str):
        return self.denied if self.denied in command else None


def _codex(guard) -> CodexBackend:
    return CodexBackend(
        member=_member(harness="codex", provider="deepseek", base_url="https://api.deepseek.com/"),
        worktree=Path("/tmp/wt"),
        system_prompt="prompt",
        guard=guard,
    )


def test_codex_gate_accepts_what_the_allow_list_permits():
    backend = _codex(_AllowAll())
    assert backend.approve(
        "item/commandExecution/requestApproval", {"command": "pytest -q"}
    ) == {"decision": "accept"}
    assert backend.approve(
        "item/fileChange/requestApproval", {"changes": [{"path": "/tmp/wt/a.py"}]}
    ) == {"decision": "accept"}


def test_codex_gate_denies_a_command_naming_a_forbidden_path():
    backend = _codex(_DenyPath("/nfsdata1/fzhou/auto_research"))
    decision = backend.approve(
        "item/commandExecution/requestApproval",
        {"command": "cat /nfsdata1/fzhou/auto_research/CLAUDE.md"},
    )
    assert decision["decision"] == "deny"
    assert "allow-list" in decision["reason"]


def test_codex_gate_denies_a_patch_outside_the_allow_list():
    backend = _codex(_DenyPath("auto_research"))
    decision = backend.approve(
        "item/fileChange/requestApproval",
        {"changes": [{"path": "/tmp/wt/ok.py"}, {"path": "/nfsdata1/fzhou/auto_research/x.py"}]},
    )
    assert decision["decision"] == "deny"
    assert "auto_research" in decision["reason"] or "x.py" in decision["reason"]


def test_codex_gate_reads_the_nested_item_shape_too():
    """The request may carry its subject under `item` rather than at the top level."""
    backend = _codex(_DenyPath("secret"))
    assert backend.approve(
        "item/fileChange/requestApproval", {"item": {"changes": [{"path": "/tmp/secret.py"}]}}
    )["decision"] == "deny"
    assert backend.approve(
        "item/commandExecution/requestApproval", {"item": {"command": "cat /tmp/secret.py"}}
    )["decision"] == "deny"


def test_codex_gate_fails_open_on_a_guard_bug():
    """A guard fault must never brick authoring, matching the Claude hook's behaviour."""

    class _Broken:
        def is_allowed(self, path):
            raise RuntimeError("boom")

        def forbidden_in_command(self, command):
            raise RuntimeError("boom")

    backend = _codex(_Broken())
    assert backend.approve("item/commandExecution/requestApproval", {"command": "ls"}) == {
        "decision": "accept"
    }


def test_codex_thread_config_carries_the_allow_list_and_the_provider_block():
    backend = CodexBackend(
        member=_member(
            harness="codex",
            provider="deepseek",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/",
            key_env="DEEPSEEK_API_KEY",
        ),
        worktree=Path("/tmp/wt"),
        system_prompt="prompt",
        guard=_AllowAll(),
        writable_roots=["/tmp/wt", "/data/obs"],
        escalation_url="http://127.0.0.1:5/mcp",
        escalation_token_env="AR_ESCALATION_TOKEN",
    )
    config = backend._config_payload()
    assert config["sandbox_workspace_write"]["writable_roots"] == ["/tmp/wt", "/data/obs"]
    assert config["sandbox_workspace_write"]["network_access"] is False
    provider_block = config["model_providers"]["deepseek"]
    assert provider_block["base_url"] == "https://api.deepseek.com/"
    assert provider_block["wire_api"] == "responses"
    assert provider_block["env_key"] == "DEEPSEEK_API_KEY"
    assert config["model_provider"] == "deepseek"
    assert config["mcp_servers"]["escalation"] == {
        "url": "http://127.0.0.1:5/mcp",
        "bearer_token_env_var": "AR_ESCALATION_TOKEN",
    }


class _Monitor:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def record(self, team, event, *, level, summary, actor="", data=None):
        self.events.append((event, data or {}))


class _Note:
    def __init__(self, method, payload) -> None:
        self.method = method
        self.payload = payload


class _Payload:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_codex_items_map_onto_the_harness_independent_event_names():
    """A Codex run's ledger must read like a Claude run's: same event names, same fields."""
    monitor = _Monitor()
    backend = CodexBackend(
        member=_member(harness="codex", provider="deepseek", base_url="https://x/"),
        worktree=Path("/tmp/wt"),
        system_prompt="p",
        guard=_AllowAll(),
        monitor=monitor,
    )
    texts: list[str] = []

    async def run() -> None:
        await backend._record_notification(
            _Note("item/completed", _Payload(item=_Payload(type="agentMessage", text="hello"))),
            texts,
        )
        await backend._record_notification(
            _Note(
                "item/completed",
                _Payload(
                    item=_Payload(
                        type="commandExecution",
                        command="pytest -q",
                        exit_code=0,
                        aggregated_output="1 passed",
                        command_actions=[],
                    )
                ),
            ),
            texts,
        )
        await backend._record_notification(
            _Note(
                "item/completed",
                _Payload(item=_Payload(type="fileChange", changes=[_Payload(path="/tmp/wt/a.py")])),
            ),
            texts,
        )
        done = await backend._record_notification(
            _Note("turn/completed", _Payload(turn=_Payload(status="completed", error=None))), texts
        )
        assert done is True

    asyncio.run(run())
    names = [event for event, _ in monitor.events]
    assert names == ["coder.text", "coder.tool_use", "coder.tool_use", "coder.turn_end"]
    assert texts == ["hello"]


def test_codex_records_a_read_outside_the_allow_list():
    """Codex grants full-disk read access, so a crossing is recorded rather than prevented."""
    monitor = _Monitor()
    backend = CodexBackend(
        member=_member(harness="codex", provider="deepseek", base_url="https://x/"),
        worktree=Path("/tmp/wt"),
        system_prompt="p",
        guard=_DenyPath("auto_research"),
        monitor=monitor,
    )

    async def run() -> None:
        await backend._record_notification(
            _Note(
                "item/completed",
                _Payload(
                    item=_Payload(
                        type="commandExecution",
                        command="grep -r x .",
                        exit_code=0,
                        aggregated_output="",
                        command_actions=[
                            _Payload(path="/nfsdata1/fzhou/auto_research/CLAUDE.md"),
                            _Payload(path="/tmp/wt/ok.py"),
                        ],
                    )
                ),
            ),
            [],
        )

    asyncio.run(run())
    audit = [data for event, data in monitor.events if event == "coder.read_outside_allowlist"]
    assert audit and audit[0]["paths"] == ["/nfsdata1/fzhou/auto_research/CLAUDE.md"]


def test_codex_drive_before_connect_is_an_explicit_error():
    backend = _codex(_AllowAll())
    with pytest.raises(RuntimeError, match="before connect"):
        asyncio.run(backend.drive("go"))


# --------------------------------------------------------------------------- escalation endpoint


def test_escalation_server_serves_both_tools_over_localhost_and_needs_the_token():
    """The handlers stay in this process, which is the whole reason for an HTTP endpoint: a
    subprocess server could not reach the live lead object or the run's HITL connector."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def ask_lead(message: str) -> str:
        return f"lead:{message}"

    async def ask_human(message: str) -> str:  # pragma: no cover — not exercised here
        return f"human:{message}"

    async def run() -> None:
        server = EscalationServer(ask_lead, ask_human)
        await server.start()
        try:
            assert server.url.startswith("http://127.0.0.1:")
            assert server.env() == {server.token_env: server.token}
            authorized = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {server.token}"}, timeout=30
            )
            async with authorized:
                async with streamable_http_client(server.url, http_client=authorized) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {tool.name for tool in tools.tools} == {"ask_lead", "ask_human"}
                        result = await session.call_tool("ask_lead", {"message": "which helper?"})
                        assert "lead:which helper?" in result.content[0].text

            wrong = httpx.AsyncClient(headers={"Authorization": "Bearer wrong"}, timeout=30)
            async with wrong:
                with pytest.raises(BaseException):  # 401 surfaces as a transport-level failure
                    async with streamable_http_client(server.url, http_client=wrong) as (
                        read,
                        write,
                        _,
                    ):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
        finally:
            await server.aclose()

    asyncio.run(run())

# --------------------------------------------------------------------------- harness dispatch


def test_thread_params_layer_the_role_prompt_rather_than_replacing_codex_instructions():
    """`prompts/coder.md` is policy; Codex's own account of its tools is harness knowledge worth
    keeping, so the prompt goes in as developer instructions (both were live-checked to be
    honoured, 2026-08-19)."""
    backend = _codex(_AllowAll())
    params = None

    class _Client:
        def thread_start(self, payload):
            nonlocal params
            params = payload

            class _Started:
                thread = type("T", (), {"id": "t-1"})()

            return _Started()

    backend._client = _Client()
    assert backend._start_thread() == "t-1"
    assert params["developerInstructions"] == "prompt"
    assert "baseInstructions" not in params
    # approvalPolicy depends on the jail (machine-local config); its own test sweeps both.
    assert params["approvalsReviewer"] == "user"
    # Not asserted here: the sandbox mode depends on whether the kernel jail is on, which is
    # machine-local config. Its own test sweeps both values.
    assert params["modelProvider"] == "deepseek"


def test_build_backend_dispatches_on_the_members_harness(tmp_path):
    """One config field decides which author process a run spawns; everything downstream of
    ``drive`` is identical."""
    from auto_research.sandbox import merge_flow
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    async def ask_lead(message: str) -> str:  # pragma: no cover — never called here
        return ""

    async def ask_human(message: str) -> str:  # pragma: no cover — never called here
        return ""

    kwargs = dict(
        worktree=tmp_path,
        toolkit_root=tmp_path,
        system_prompt="p",
        ask_lead=ask_lead,
        ask_human=ask_human,
        max_turns=5,
        monitor=None,
        sdk_client_factory=lambda options: _FakeSdkClient(),
    )

    claude = merge_flow._build_backend(
        member=_member(provider="anthropic", harness="claude_sdk", model="claude-sonnet-5"),
        **kwargs,
    )
    assert isinstance(claude, ClaudeSdkBackend)

    # The member is an argument, not ambient config, so one process can build both backends —
    # which is what lets a benchmark sweep the author across vendors in one batch.
    codex = merge_flow._build_backend(
        member=_member(
            provider="deepseek",
            harness="codex",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/",
        ),
        **kwargs,
    )
    assert isinstance(codex, CodexBackend)
    # The configured allow-list becomes the harness's writable roots, with the working tree in it.
    assert str(tmp_path) in codex._writable_roots

# --------------------------------------------------------------------------- escalation fail-safe


def test_ask_human_with_no_handler_raises_rather_than_hanging_or_inventing():
    """§9.8's headless fail-safe, at the handler the harnesses actually serve.

    Both backends expose `ask_human` to the author; unattended, no handler is wired, and the
    contract is a typed raise — not a hang, and never an invented answer. Asserted here rather
    than live, because the interesting case is the *absence* of a human.
    """
    from auto_research.hitl import HumanInputRequired
    from auto_research.sandbox.merge_flow import _make_ask_human_handler

    handler = _make_ask_human_handler(None, model="matrix", monitor=_Monitor())
    with pytest.raises(HumanInputRequired):
        asyncio.run(handler("Which calibration epoch applies to this observation?"))


def test_ask_human_with_a_handler_returns_its_answer_verbatim():
    from auto_research.sandbox.merge_flow import _make_ask_human_handler

    def answer(query):
        return "Use epoch 2 and state the assumption."

    handler = _make_ask_human_handler(answer, model="matrix", monitor=_Monitor())
    assert asyncio.run(handler("Which epoch?")) == "Use epoch 2 and state the assumption."

def test_codex_audit_resolves_a_relative_read_against_the_command_cwd():
    """The regression the live matrix found: Codex's command parse yields paths relative to the
    command's directory, and resolving them against this process's cwd instead flagged every
    ordinary in-worktree read (47 false events in one session)."""
    monitor = _Monitor()
    backend = CodexBackend(
        member=_member(harness="codex", provider="deepseek", base_url="https://x/"),
        worktree=Path("/tmp/wt"),
        system_prompt="p",
        guard=_DenyPath("auto_research"),
        monitor=monitor,
    )

    async def run() -> None:
        await backend._record_notification(
            _Note(
                "item/completed",
                _Payload(
                    item=_Payload(
                        type="commandExecution",
                        command="sed -n '1,40p' demo_utils/lib/demo_help.py",
                        cwd="/tmp/wt",
                        exit_code=0,
                        aggregated_output="",
                        command_actions=[_Payload(path="demo_utils/lib/demo_help.py")],
                    )
                ),
            ),
            [],
        )

    asyncio.run(run())
    assert not [event for event, _ in monitor.events if event == "coder.read_outside_allowlist"], (
        "an in-worktree relative read was recorded as a boundary crossing"
    )


def test_codex_config_is_flattened_into_cli_overrides():
    """MCP servers and provider blocks are read when the app-server process starts, not per
    thread (measured live: a thread-level escalation server never connected), so the config is
    handed over as `--config key.path=value` with TOML literals."""
    from auto_research.sandbox.coder.codex_sdk import _flatten_overrides

    flat = _flatten_overrides(
        {
            "mcp_servers": {"escalation": {"url": "http://127.0.0.1:5/mcp"}},
            "sandbox_workspace_write": {"writable_roots": ["/a", "/b"], "network_access": False},
            "model_provider": "deepseek",
        }
    )
    assert 'mcp_servers.escalation.url="http://127.0.0.1:5/mcp"' in flat
    assert 'sandbox_workspace_write.writable_roots=["/a", "/b"]' in flat
    assert "sandbox_workspace_write.network_access=false" in flat
    assert 'model_provider="deepseek"' in flat


def test_codex_thread_params_carry_no_config_block():
    """Whatever the app-server does with a thread-level `config`, it is not where MCP servers or
    provider blocks take effect, so nothing is sent there twice."""
    backend = _codex(_AllowAll())
    params = None

    class _Client:
        def thread_start(self, payload):
            nonlocal params
            params = payload

            class _Started:
                thread = type("T", (), {"id": "t-2"})()

            return _Started()

    backend._client = _Client()
    backend._start_thread()
    assert "config" not in params

def test_codex_answers_an_mcp_elicitation_in_its_own_vocabulary():
    """The live matrix's second find: an MCP tool call is offered as an *elicitation*, which takes
    `{"action": ...}`. Replying with `{"decision": ...}` reads as a rejection, and the author loses
    `ask_lead`/`ask_human` entirely while appearing to have called them."""
    backend = _codex(_AllowAll())
    request = {
        "serverName": "escalation",
        "mode": "form",
        "message": 'Allow the escalation MCP server to run tool "ask_lead"?',
        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": {"message": "ping"}},
        "requestedSchema": {"type": "object", "properties": {}},
    }
    assert backend.approve("mcpServer/elicitation/request", request) == {
        "action": "accept",
        "content": {},
    }


def test_codex_declines_an_elicitation_from_a_server_the_runtime_did_not_configure():
    """Our escalation server is this process; anything else asking to run a tool is not ours."""
    backend = _codex(_AllowAll())
    decision = backend.approve(
        "mcpServer/elicitation/request", {"serverName": "somebody-elses-server"}
    )
    assert decision == {"action": "decline"}

def test_codex_audit_unwraps_a_model_wrapped_cwd():
    """`CommandExecutionThreadItem.cwd` is a RootModel, so `str()` on it yields `root='/tmp/…'`.
    Using that as the base turned every relative read into a boundary crossing (40 false events
    in one live session, after the first fix and before this one)."""
    monitor = _Monitor()
    backend = CodexBackend(
        member=_member(harness="codex", provider="deepseek", base_url="https://x/"),
        worktree=Path("/tmp/wt"),
        system_prompt="p",
        guard=_DenyPath("auto_research"),
        monitor=monitor,
    )

    class _Wrapped:
        def __init__(self, value):
            self.root = value

    async def run() -> None:
        await backend._record_notification(
            _Note(
                "item/completed",
                _Payload(
                    item=_Payload(
                        type="commandExecution",
                        command="ls demo_utils",
                        cwd=_Wrapped("/tmp/wt"),
                        exit_code=0,
                        aggregated_output="",
                        command_actions=[_Payload(path=_Wrapped("demo_utils"))],
                    )
                ),
            ),
            [],
        )

    asyncio.run(run())
    assert not [e for e, _ in monitor.events if e == "coder.read_outside_allowlist"]

# --------------------------------------------------------------------------- unverified pairings


def test_an_unverified_pairing_warns_at_launch_without_blocking_it(monkeypatch):
    """Qwen on Codex is configured correctly and has never run: as of 2026-08-19 the account was
    not entitled (`AccessDenied.Unpurchased`). Blocking the launch would be wrong, and passing
    silently would make a first-time failure look like a defect in the authoring flow."""
    from auto_research.preflight import PreflightWarning
    from auto_research.sandbox.coder.harness import UNVERIFIED_PAIRINGS

    assert ("dashscope", "codex") in UNVERIFIED_PAIRINGS
    monkeypatch.setenv("DASHSCOPE_API_KEY", "not-a-real-key")
    _patch_member(
        monkeypatch,
        _member(
            provider="dashscope",
            harness="codex",
            model="qwen3.7-plus",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            key_env="DASHSCOPE_API_KEY",
        ),
    )
    with pytest.raises(PreflightWarning, match="never exercised live"):
        check_coder_harness_supported()


def test_a_verified_pairing_raises_no_warning(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key")
    _patch_member(
        monkeypatch,
        _member(
            provider="deepseek",
            harness="codex",
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/",
            key_env="DEEPSEEK_API_KEY",
        ),
    )
    check_coder_harness_supported()

def test_member_parameters_reach_the_codex_config_and_win():
    """A vendor requirement travels as member config, not as a special case in the backend:
    `glm-5.3` fails every turn unless a reasoning effort is named."""
    member = _member(
        harness="codex",
        provider="zai",
        model="glm-5.3",
        base_url="https://api.z.ai/api/v1",
        parameters={"model_reasoning_effort": "high", "tools": {"web_search": False}},
    )
    backend = CodexBackend(
        member=member, worktree=Path("/tmp/wt"), system_prompt="p", guard=_AllowAll()
    )
    config = backend._config_payload()
    assert config["model_reasoning_effort"] == "high"
    # Applied last, so a member can override a default this module sets.
    assert config["tools"] == {"web_search": False}


# --------------------------------------------------------------------------- vendor key pool


def _codex_backend(member, env=None, monkeypatch=None, tmp_path=None):
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    return CodexBackend(
        member=member, worktree=tmp_path, system_prompt="p", guard=None,
        escalation=None, env=env, writable_roots=[str(tmp_path)], monitor=None,
    )


def test_a_rotating_provider_author_launches_on_the_live_key_not_the_primary(monkeypatch, tmp_path):
    """The failure this exists to prevent: a spent primary retires for the whole process, every
    chat role rotates to the fallback, and the author — reading the raw variable — launches on the
    dead key and cannot run at all."""
    from auto_research.models import keys as keymod

    monkeypatch.setenv("OPENAI_API_KEY", "dead-primary")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "live-fallback")
    keymod.reset_pools()
    pool = keymod.key_pool("openai")
    assert pool.rotate() == "fallback1", "precondition: the pool can rotate"

    member = _member(provider="openai", harness="codex", model="gpt-5.6-terra",
                     key_env="OPENAI_API_KEY")
    backend = _codex_backend(member, tmp_path=tmp_path)
    key, slot = backend._live_vendor_key()

    assert key == "live-fallback"
    assert slot == "fallback1", "the slot is named for the log, never the key itself"
    keymod.reset_pools()


def test_a_harness_only_vendor_reads_its_own_variable(monkeypatch, tmp_path):
    """DeepSeek, Z.AI and Alibaba are deliberately outside the rotating set — no cited error
    taxonomy, so nothing may rotate them — and their key must come from their own variable."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    member = _member(provider="deepseek", harness="codex", model="deepseek-v4-flash",
                     base_url="https://api.deepseek.com/", key_env="DEEPSEEK_API_KEY")
    backend = _codex_backend(member, tmp_path=tmp_path)

    assert backend._live_vendor_key() == ("ds-key", "env")


def test_a_plan_specific_variable_is_not_shadowed_by_the_pool(monkeypatch, tmp_path):
    """A member may name a variable other than the provider's standard primary (a plan-specific
    endpoint does). The pool describes only the standard pair, so it must not answer for one."""
    from auto_research.models import keys as keymod

    monkeypatch.setenv("OPENAI_API_KEY", "standard")
    monkeypatch.setenv("OPENAI_PLAN_KEY", "plan-specific")
    keymod.reset_pools()

    member = _member(provider="openai", harness="codex", model="gpt-5.6-terra",
                     key_env="OPENAI_PLAN_KEY")
    backend = _codex_backend(member, tmp_path=tmp_path)

    assert backend._live_vendor_key() == ("plan-specific", "env")
    keymod.reset_pools()


def test_a_pooled_key_overrides_an_inherited_one_but_an_env_key_does_not(monkeypatch, tmp_path):
    """The author's environment is an overlay on the analyst init script's. A pooled key must win
    over a stale inherited value, since the whole point is not to launch on a rotated-away key; a
    vendor with no pool has nothing more authoritative than the environment, so it leaves an
    inherited value alone."""
    from auto_research.models import keys as keymod

    monkeypatch.setenv("OPENAI_API_KEY", "dead-primary")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "live-fallback")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ours")
    keymod.reset_pools()
    keymod.key_pool("openai").rotate()

    rotating = _codex_backend(
        _member(provider="openai", harness="codex", model="gpt-5.6-terra",
                key_env="OPENAI_API_KEY"),
        env={"OPENAI_API_KEY": "stale-inherited"}, tmp_path=tmp_path,
    )
    value, slot = rotating._live_vendor_key()
    assert (value, slot) == ("live-fallback", "fallback1")

    harness_only = _codex_backend(
        _member(provider="deepseek", harness="codex", model="deepseek-v4-flash",
                base_url="https://api.deepseek.com/", key_env="DEEPSEEK_API_KEY"),
        env={"DEEPSEEK_API_KEY": "inherited"}, tmp_path=tmp_path,
    )
    assert harness_only._live_vendor_key() == ("ours", "env")
    keymod.reset_pools()


def test_the_child_environment_carries_the_live_key_not_the_stale_one(monkeypatch, tmp_path):
    """The production path, not just the resolver: what the author subprocess actually inherits.

    ``setdefault`` was the bug's shape — an inherited value would have kept a key the run had
    already rotated away from, and the author would have failed inside the subprocess.
    """
    from auto_research.models import keys as keymod
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    monkeypatch.setenv("OPENAI_API_KEY", "dead-primary")
    monkeypatch.setenv("OPENAI_API_KEY_FALLBACK", "live-fallback")
    keymod.reset_pools()
    keymod.key_pool("openai").rotate()

    captured: dict = {}

    class _Client:
        def start(self): pass
        def initialize(self): pass
        def account_login_start(self, params): captured["login"] = params

    def _factory(config, _approve):
        captured["env"] = dict(config.env)
        return _Client()

    backend = CodexBackend(
        member=_member(provider="openai", harness="codex", model="gpt-5.6-terra",
                       key_env="OPENAI_API_KEY"),
        worktree=tmp_path,
        system_prompt="p",
        guard=None,
        env={"OPENAI_API_KEY": "stale-inherited"},
        client_factory=_factory,
    )
    backend._build_client()

    assert captured["env"]["OPENAI_API_KEY"] == "live-fallback"
    assert captured["login"].root.api_key == "live-fallback"
    keymod.reset_pools()


@pytest.mark.parametrize("jailed, expected", [(True, "danger-full-access"), (False, "workspace-write")])
def test_the_harness_sandbox_stands_down_exactly_when_the_kernel_jail_is_on(
    monkeypatch, tmp_path, jailed, expected
):
    """One enforcement layer, not two — and the invariant is asserted at both settings.

    This harness's own sandbox *is* bubblewrap, so running it inside our bubblewrap jail nests the
    same mechanism: it cannot spawn its sandbox helper, every shell command is rejected, and the
    author reaches the gate having read and written nothing, which looks like a weak model rather
    than broken plumbing. When the jail is on it therefore stands down and the kernel is the wall;
    when the jail is off it is the only wall and stays on.

    Swept rather than pinned to one value, because the flag is gitignored machine-local config: a
    test that reads it is a test of one machine (the 2026-07-28 lesson, and this test hit it).
    """
    from auto_research.sandbox import jail
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    monkeypatch.setattr(jail, "jail_enabled", lambda: jailed, raising=True)
    params = {}

    class _Started:
        thread = type("T", (), {"id": "t-1"})()

    class _Client:
        def thread_start(self, sent, **kw):
            params.update(sent if isinstance(sent, dict) else kw)
            return _Started()

    backend = CodexBackend(
        member=_member(harness="codex", provider="deepseek", base_url="https://api.deepseek.com/"),
        worktree=tmp_path, system_prompt="prompt", guard=None,
    )
    backend._client = _Client()
    backend._start_thread()

    assert params["sandbox"] == expected
    # Jailed, approvals are off too: that path had never run (the harness sandbox used to permit
    # those commands without asking), and when it finally did it rejected `python` and `pytest`, so
    # the author could not run the tests it wrote. The kernel makes out-of-jail paths absent, which
    # is stronger than a command-string check.
    assert params["approvalPolicy"] == ("never" if jailed else "untrusted")


def test_an_author_may_write_only_its_worktree_and_its_own_settings_home(tmp_path):
    """What makes standing the harness sandbox down safe rather than a loosening.

    The surrendered ``workspace-write`` mode confined writes to the working tree; so does this, but
    the kernel enforces it and reads are confined too. The observation database is read-only, so no
    author can write a data file where a fabricated result would have to live.
    """
    from auto_research.sandbox.jail import coder_policy

    worktree = tmp_path / "repo"
    worktree.mkdir()
    policy = coder_policy(label="probe-write", extra_rw=(str(worktree),))

    assert str(worktree) in policy.rw
    assert policy.home in policy.rw
    assert len(policy.rw) == 2, f"an author writes to two places only, got {policy.rw}"
    assert all(entry not in policy.rw for entry in policy.ro)


def test_the_temp_root_is_not_readable_by_an_author():
    """Trial working trees live under it, so leaving it readable would let one author read
    another's solution — which invalidates a repeated-trial measurement instead of breaking
    anything visibly."""
    import tempfile

    from auto_research.sandbox.jail import coder_policy

    policy = coder_policy(label="probe-temp")
    temp_root = str(Path(tempfile.gettempdir()).resolve())
    assert temp_root not in policy.ro
    assert temp_root not in policy.rw
