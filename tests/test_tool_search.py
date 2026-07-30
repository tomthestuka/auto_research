"""Deterministic tests for provider-native tool search / deferred tool loading.

Toolkit tool definitions are withheld from the model's context until it discovers them via the
provider's server-side search tool, which appends the definition inline in the conversation
**tail** so the cached prefix survives. These exercise config parsing, request construction on
both adapters, the discovery-block round-trip through the neutral types, the tombstone guard,
and the disabled-by-default path — all against mocked SDKs, no tokens spent.

The live round-trips are in ``tests/test_live_tool_search.py`` — **passed 2026-07-28 on both
providers**: the real endpoints accept the deferred/search shape, the model does search, and an
expanded definition does land inside the conversation cache breakpoint. That run also exposed a
gap these tests had left open — the Anthropic search tool's ``name``, asserted here since.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from auto_research.env import ToolSearchConfig, tool_search_config
from auto_research.models.providers._tool_search import (
    TOMBSTONE_DESCRIPTION,
    anthropic_search_tool_name,
    missing_tool_names,
    referenced_tool_names,
)
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.openai import OpenAIProvider, _to_openai_input
from auto_research.models.types import (
    AssistantToolUse,
    ModelMessage,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)

_ANTHROPIC_TOOL = "tool_search_tool_bm25_20251119"


def _enabled() -> ToolSearchConfig:
    return ToolSearchConfig(
        enabled=True, anthropic_tool=_ANTHROPIC_TOOL, openai_tool="tool_search"
    )


def _disabled() -> ToolSearchConfig:
    return ToolSearchConfig(
        enabled=False, anthropic_tool=_ANTHROPIC_TOOL, openai_tool="tool_search"
    )


def _tools():
    """One deferred registry tool and one resident harness tool."""
    return [
        {
            "name": "research_toolkit_xspec_utils_ixpe_pcube",
            "description": "Energy-resolved IXPE PCUBE polarimetry.",
            "input_schema": {"type": "object", "properties": {"obsid": {"type": "string"}}},
            "defer_loading": True,
        },
        {
            "name": "ask_human",
            "description": "Ask the human a question.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def _messages():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="hi")]


# ── config ──────────────────────────────────────────────────────────────────

def test_tool_search_config_defaults_disabled(monkeypatch):
    monkeypatch.setattr("auto_research.env._orch_config", lambda: {})
    cfg = tool_search_config()
    assert cfg.enabled is False  # a config lacking the key keeps every tool resident
    assert cfg.anthropic_tool == _ANTHROPIC_TOOL
    assert cfg.openai_tool == "tool_search"


def test_tool_search_config_reads_keys(monkeypatch):
    monkeypatch.setattr(
        "auto_research.env._orch_config",
        lambda: {"tool_search_enabled": True, "tool_search_openai_tool": "tool_search_v2"},
    )
    cfg = tool_search_config()
    assert cfg.enabled is True
    assert cfg.openai_tool == "tool_search_v2"


# ── the SandboxToolProvider marks registry tools, never harness tools ────────

def test_provider_defers_registry_tools_only(monkeypatch):
    from auto_research.agents.react import SandboxToolProvider

    monkeypatch.setattr("auto_research.agents.react.tool_search_config", _enabled)
    registry = SimpleNamespace(
        tool_definitions=lambda: [
            {"name": "research_toolkit_x_utils_a", "description": "d", "input_schema": {}}
        ]
    )
    provider = SandboxToolProvider.__new__(SandboxToolProvider)
    provider.registry = registry
    provider.human_handler = None
    provider.web_source = None
    provider.report_workspace = None
    defs = provider.tool_definitions()
    assert defs[0]["defer_loading"] is True


def test_provider_defers_nothing_when_disabled(monkeypatch):
    from auto_research.agents.react import SandboxToolProvider

    monkeypatch.setattr("auto_research.agents.react.tool_search_config", _disabled)
    registry = SimpleNamespace(
        tool_definitions=lambda: [
            {"name": "research_toolkit_x_utils_a", "description": "d", "input_schema": {}}
        ]
    )
    provider = SandboxToolProvider.__new__(SandboxToolProvider)
    provider.registry = registry
    provider.human_handler = None
    provider.web_source = None
    provider.report_workspace = None
    assert "defer_loading" not in provider.tool_definitions()[0]


# ── Anthropic request construction ──────────────────────────────────────────

class _FakeAnthropicMessages:
    def __init__(self, store):
        self._store = store

    async def create(self, **request):
        self._store["request"] = request
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="server_tool_use", id="srvtoolu_1", name="tool_search",
                    model_dump=lambda mode=None: {
                        "type": "server_tool_use", "id": "srvtoolu_1", "name": "tool_search",
                    },
                ),
                SimpleNamespace(
                    type="tool_search_tool_result", tool_use_id="srvtoolu_1",
                    model_dump=lambda mode=None: {
                        "type": "tool_search_tool_result",
                        "tool_use_id": "srvtoolu_1",
                        "content": {
                            "type": "tool_search_tool_search_result",
                            "tool_references": [
                                {"type": "tool_reference",
                                 "tool_name": "research_toolkit_xspec_utils_ixpe_pcube"}
                            ],
                        },
                    },
                ),
                SimpleNamespace(type="tool_use", id="tu_1", name="ask_human", input={}),
            ],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            context_management=None,
        )

    async def parse(self, **request):
        """The structured-output path (`messages.parse`), which a terminal compose uses.

        Shape-complete like the real client: a fake that implements less than the client
        silently narrows what the suite can cover (the 2026-07-28 lesson), and the tool-less
        replay this file now guards runs through exactly this method.
        """
        self._store["request"] = request
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"summary": "done"}')],
            parsed_output=SimpleNamespace(summary="done", model_dump=lambda mode=None: {"summary": "done"}),
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            context_management=None,
        )


class _FakeAnthropic:
    store: dict = {}

    def __init__(self, *a, **k):
        pass

    @property
    def messages(self):
        return _FakeAnthropicMessages(_FakeAnthropic.store)

    @property
    def beta(self):
        return SimpleNamespace(messages=_FakeAnthropicMessages(_FakeAnthropic.store))


@pytest.fixture(autouse=True)
def _reset():
    _FakeAnthropic.store = {}
    yield


def _call_anthropic(monkeypatch, messages=None, cache_prefix=False):
    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.tool_search_config", _enabled
    )
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake-model")
    return asyncio.run(
        AnthropicProvider().call(
            messages or _messages(), cfg, tools=_tools(), cache_prefix=cache_prefix
        )
    )


def test_anthropic_declares_search_tool_and_defers(monkeypatch):
    _call_anthropic(monkeypatch)
    sent = _FakeAnthropic.store["request"]["tools"]
    assert sent[0]["defer_loading"] is True          # the registry tool
    assert "defer_loading" not in sent[1]            # the harness tool stays resident
    search = [t for t in sent if t.get("type") == _ANTHROPIC_TOOL]
    assert search, "search tool declared"
    # The name must be the UNDATED identifier beside the dated type — a mismatch is a 400
    # ("Input should be 'tool_search_tool_bm25'"), which is how the live check first failed.
    assert search[0]["name"] == "tool_search_tool_bm25"


def test_anthropic_search_tool_name_strips_the_dated_suffix():
    assert anthropic_search_tool_name("tool_search_tool_bm25_20251119") == "tool_search_tool_bm25"
    assert anthropic_search_tool_name("tool_search_tool_regex_20251119") == "tool_search_tool_regex"
    assert anthropic_search_tool_name("tool_search_tool_bm25") == "tool_search_tool_bm25"


def test_anthropic_cache_breakpoint_never_on_a_deferred_tool(monkeypatch):
    # A deferred tool carrying cache_control is a 400: it is not part of the cached prefix.
    _call_anthropic(monkeypatch, cache_prefix=True)
    sent = _FakeAnthropic.store["request"]["tools"]
    assert all(not (t.get("defer_loading") and "cache_control" in t) for t in sent)


def test_anthropic_captures_and_replays_discovery_blocks(monkeypatch):
    response = _call_anthropic(monkeypatch)
    types = [b["type"] for b in response.provider_blocks]
    assert types == ["server_tool_use", "tool_search_tool_result"]

    # Replaying that turn must re-emit the blocks verbatim — the API re-expands the
    # referenced definitions from history, so dropping them de-activates the tool.
    turn = AssistantToolUse(
        content="", tool_calls=[ToolCall(id="tu_1", name="ask_human", arguments={})],
        provider_blocks=response.provider_blocks,
    )
    _call_anthropic(monkeypatch, messages=[*_messages(), turn])
    replayed = _FakeAnthropic.store["request"]["messages"][-1]["content"]
    assert [b.get("type") for b in replayed] == [
        "server_tool_use", "tool_search_tool_result", "tool_use"
    ]


def test_anthropic_tombstones_a_vanished_discovered_tool(monkeypatch):
    """A failed authoring attempt + registry reload drops a tool the conversation already
    discovered; without a tombstone the next request 400s on the dangling reference."""
    response = _call_anthropic(monkeypatch)
    turn = AssistantToolUse(content="", provider_blocks=response.provider_blocks)

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.tool_search_config", _enabled
    )
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake-model")
    gone = [t for t in _tools() if t["name"] != "research_toolkit_xspec_utils_ixpe_pcube"]
    asyncio.run(
        AnthropicProvider().call([*_messages(), turn], cfg, tools=gone)
    )
    sent = _FakeAnthropic.store["request"]["tools"]
    stub = [t for t in sent if t.get("name") == "research_toolkit_xspec_utils_ixpe_pcube"]
    assert stub and stub[0]["description"] == TOMBSTONE_DESCRIPTION


# ── OpenAI request construction ─────────────────────────────────────────────

class _FakeResponses:
    def __init__(self, store):
        self._store = store

    async def create(self, **request):
        self._store["request"] = request
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="tool_search_call", call_id=None,
                    model_dump=lambda mode=None: {"type": "tool_search_call", "call_id": None},
                ),
                SimpleNamespace(
                    type="tool_search_output",
                    model_dump=lambda mode=None: {
                        "type": "tool_search_output",
                        "tools": [{"tool_name": "research_toolkit_xspec_utils_ixpe_pcube"}],
                    },
                ),
            ],
            output_text="",
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


class _FakeOpenAI:
    store: dict = {}

    def __init__(self, *a, **k):
        pass

    @property
    def responses(self):
        return _FakeResponses(_FakeOpenAI.store)


def test_openai_declares_search_tool_and_defers(monkeypatch):
    import openai

    _FakeOpenAI.store = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("auto_research.models.providers.openai.tool_search_config", _enabled)
    cfg = RoleModelConfig(name="m", provider="openai", model="gpt-5.5")
    response = asyncio.run(OpenAIProvider().call(_messages(), cfg, tools=_tools()))
    sent = _FakeOpenAI.store["request"]["tools"]
    assert sent[0]["defer_loading"] is True
    assert "defer_loading" not in sent[1]
    assert [t for t in sent if t.get("type") == "tool_search"], "search tool declared"
    assert [b["type"] for b in response.provider_blocks] == [
        "tool_search_call", "tool_search_output"
    ]


def test_openai_replays_discovery_items(monkeypatch):
    import openai

    _FakeOpenAI.store = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("auto_research.models.providers.openai.tool_search_config", _enabled)
    cfg = RoleModelConfig(name="m", provider="openai", model="gpt-5.5")
    turn = AssistantToolUse(
        content="",
        provider_blocks=[{"type": "tool_search_call", "call_id": None}],
        tool_calls=[ToolCall(id="c1", name="ask_human", arguments={})],
    )
    asyncio.run(OpenAIProvider().call([*_messages(), turn], cfg, tools=_tools()))
    item_types = [i.get("type") for i in _FakeOpenAI.store["request"]["input"]]
    assert "tool_search_call" in item_types
    # the discovery item replays BEFORE the call it enabled
    assert item_types.index("tool_search_call") < item_types.index("function_call")


def test_openai_disabled_sends_no_search_tool(monkeypatch):
    import openai

    _FakeOpenAI.store = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("auto_research.models.providers.openai.tool_search_config", _disabled)
    cfg = RoleModelConfig(name="m", provider="openai", model="gpt-5.5")
    asyncio.run(OpenAIProvider().call(_messages(), cfg, tools=_tools()))
    sent = _FakeOpenAI.store["request"]["tools"]
    assert all(t.get("type") == "function" for t in sent)
    assert all("defer_loading" not in t for t in sent)


# ── the shared reference scanner ────────────────────────────────────────────

def test_referenced_and_missing_tool_names():
    turn = AssistantToolUse(
        content="",
        provider_blocks=[
            {
                "type": "tool_search_tool_result",
                "content": {"tool_references": [{"tool_name": "alpha"}, {"tool_name": "beta"}]},
            }
        ],
    )
    assert referenced_tool_names([turn]) == {"alpha", "beta"}
    assert missing_tool_names([turn], ["alpha"]) == ["beta"]
    assert missing_tool_names([turn], ["alpha", "beta"]) == []


# ── the 2026-07-29 run-killer: discovery blocks replayed on a TOOL-LESS request ──────────────


def test_anthropic_drops_discovery_blocks_when_no_tools_are_declared(monkeypatch):
    """A terminal structured compose sends a schema and NO tools; replaying a discovery block
    there made the API reject the whole request — "Tool reference 'x' not found in available
    tools" — which killed a live run. With no tools on the request there is nothing for the
    reference to point at, so the blocks are dropped and the trajectory still replays.
    """
    import anthropic
    from pydantic import BaseModel

    class _Out(BaseModel):
        summary: str

    response = _call_anthropic(monkeypatch)
    turn = AssistantToolUse(
        content="looked it up",
        tool_calls=[ToolCall(id="tu_1", name="research_toolkit_xspec_utils_ixpe_pcube",
                             arguments={})],
        provider_blocks=response.provider_blocks,
    )
    convo = [*_messages(), turn, ToolResultMessage(results=[
        ToolResultPart(tool_call_id="tu_1", name="research_toolkit_xspec_utils_ixpe_pcube",
                       content="ok")
    ])]

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.tool_search_config", _enabled
    )
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake-model")
    asyncio.run(AnthropicProvider().call(convo, cfg, schema=_Out))

    sent = _FakeAnthropic.store["request"]
    assert "tools" not in sent or not sent["tools"]        # schema call declares no tools
    assistant = [m for m in sent["messages"] if m["role"] == "assistant"][-1]
    types = [b.get("type") for b in assistant["content"]]
    assert "server_tool_use" not in types                  # the dangling reference is gone
    assert "tool_search_tool_result" not in types
    assert types == ["text", "tool_use"]                   # the trajectory itself is intact


def test_openai_drops_discovery_items_when_no_tools_are_declared(monkeypatch):
    """Same guard on the OpenAI half, so no tool-less replay path can regress."""
    from pydantic import BaseModel

    class _Out(BaseModel):
        summary: str

    turn = AssistantToolUse(
        content="",
        tool_calls=[ToolCall(id="call_1", name="research_toolkit_xspec_utils_ixpe_pcube",
                            arguments={})],
        provider_blocks=[
            {"type": "tool_search_call", "id": "ts_1"},
            {"type": "tool_search_output", "id": "ts_1"},
        ],
    )
    items = _to_openai_input([*_messages(), turn], include_discovery=False)
    assert not [i for i in items if i.get("type") in ("tool_search_call", "tool_search_output")]
    assert [i for i in items if i.get("type") == "function_call"]      # calls still replay
