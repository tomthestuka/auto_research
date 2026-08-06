"""Deterministic tests for provider-native tool search / deferred tool loading.

Toolkit tool definitions are withheld from the model's context until it discovers them via the
provider's server-side search tool, which appends the definition inline in the conversation
**tail** so the cached prefix survives. These exercise config parsing, request construction on
both adapters, the discovery-block round-trip through the neutral types, and the
disabled-by-default path — all against mocked SDKs, no tokens spent.

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
from auto_research.models.providers._tool_search import anthropic_search_tool_name
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.openai import OpenAIProvider, _to_openai_input
from auto_research.models.types import (
    ModelResponse,
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
    # The whole turn is carried, discovery blocks included and in emission order.
    assert [b["type"] for b in response.provider_parts] == [
        "server_tool_use", "tool_search_tool_result", "tool_use"
    ]

    # Replaying the turn (whole, as `_react` appends it) must re-emit those blocks verbatim —
    # the API re-expands the referenced definitions from history, so dropping them de-activates
    # the discovered tool.
    _call_anthropic(monkeypatch, messages=[*_messages(), response])
    replayed = _FakeAnthropic.store["request"]["messages"][-1]["content"]
    assert replayed == response.provider_parts


# ── OpenAI request construction ─────────────────────────────────────────────

class _FakeResponses:
    def __init__(self, store):
        self._store = store

    async def create(self, **request):
        self._store["request"] = request
        # The dumps mirror the REAL response items, `created_by` included: it is an
        # output-only field the input schema rejects as an unknown parameter, and a fake
        # that omitted it is what let that 400 reach a live run (2026-08-03). A mock
        # smaller than the real shape silently narrows what the suite can cover.
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="tool_search_call", call_id=None,
                    model_dump=lambda mode=None: {
                        "type": "tool_search_call", "call_id": None, "id": "ts_1",
                        "arguments": {"query": "pcube"}, "execution": "server",
                        "status": "completed", "created_by": "assistant",
                    },
                ),
                SimpleNamespace(
                    type="tool_search_output",
                    model_dump=lambda mode=None: {
                        "type": "tool_search_output",
                        "tools": [{"tool_name": "research_toolkit_xspec_utils_ixpe_pcube"}],
                        "id": "tso_1", "call_id": None, "execution": "server",
                        "status": "completed", "created_by": "assistant",
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
    assert [b["type"] for b in response.provider_parts] == [
        "tool_search_call", "tool_search_output"
    ]


def test_openai_replays_discovery_items(monkeypatch):
    import openai

    _FakeOpenAI.store = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("auto_research.models.providers.openai.tool_search_config", _enabled)
    cfg = RoleModelConfig(name="m", provider="openai", model="gpt-5.5")
    turn = ModelResponse(
        content="",
        provider_parts=[{"type": "tool_search_call", "call_id": None}],
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


# ── the 2026-07-29 run-killer: discovery blocks replayed on a TOOL-LESS request ──────────────


def test_anthropic_compose_declares_its_tools_so_every_block_resolves(monkeypatch):
    """The terminal compose declares its tools and forbids calling them.

    Replaying a discovery block on a request with NO tools made the API reject the whole
    request — "Tool reference 'x' not found in available tools" — which killed a live run
    (2026-07-29). Dropping the blocks was the first fix; it collided with the echo rule and,
    on OpenAI, orphaned the items that remained (run 8c75990e). Declaring the tools with
    `tool_choice="none"` removes the dilemma: every reference resolves, nothing is filtered,
    and the model still cannot call anything.
    """
    import anthropic
    from pydantic import BaseModel

    class _Out(BaseModel):
        summary: str

    response = _call_anthropic(monkeypatch)
    convo = [*_messages(), response, ToolResultMessage(results=[
        ToolResultPart(tool_call_id="tu_1", name="ask_human", content="ok")
    ])]

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.tool_search_config", _enabled
    )
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake-model")
    asyncio.run(AnthropicProvider().call(convo, cfg, schema=_Out, tools=_tools(),
                                         tool_choice="none"))

    sent = _FakeAnthropic.store["request"]
    assert sent["tools"], "the compose declares its tools"
    assert sent["tool_choice"] == {"type": "none"}, "…and is forbidden from calling them"
    assistant = [m for m in sent["messages"] if m["role"] == "assistant"][-1]
    types = [b.get("type") for b in assistant["content"]]
    assert "server_tool_use" in types and "tool_search_tool_result" in types, (
        "the turn is echoed whole — the documented rule, and now legal because the tools "
        "the blocks name are declared"
    )


def test_openai_never_orphans_a_carried_item():
    """A `function_call` and the `reasoning` item it depends on always travel together.

    The 2026-08-05 run-killer (`Item 'fc_…' of type 'function_call' was provided without its
    required 'reasoning' item: 'rs_…'`, run 8c75990e) came from dropping the carried parts on a
    tool-less request while emitting the `function_call` items unconditionally a few lines
    below. The projection takes no mode argument any more, so no caller can reproduce that
    split.
    """
    import inspect

    turn = ModelResponse(
        content="",
        tool_calls=[ToolCall(id="call_1", name="research_toolkit_xspec_utils_ixpe_pcube",
                            arguments={})],
        provider_parts=[{"type": "reasoning", "id": "rs_1", "summary": []}],
    )
    items = _to_openai_input([*_messages(), turn])
    types = [i.get("type") for i in items if "type" in i]

    assert types == ["reasoning", "function_call"], (
        "the reasoning item replays ahead of the call that required it"
    )
    assert "include_provider_parts" not in inspect.signature(_to_openai_input).parameters, (
        "the drop mode is gone, not merely unused"
    )


# ── the 2026-08-03 run-killer: an OUTPUT-ONLY field replayed as INPUT ─────────────────────────


def test_openai_capture_translates_discovery_items_to_their_input_shape(monkeypatch):
    """A captured discovery item carries only fields the API accepts back as input.

    The provider authors these items and stamps its own bookkeeping on them; the input schema
    rejects that stamp as an unknown parameter, so round-tripping the raw item 400s on the very
    next turn (`Unknown parameter: 'input[2].created_by'`, live run b-run-q21-0803). Translating
    at capture makes the stored item legal to send by construction.
    """
    import openai

    _FakeOpenAI.store = {}
    monkeypatch.setattr(openai, "AsyncOpenAI", _FakeOpenAI)
    monkeypatch.setattr("auto_research.models.providers.openai.tool_search_config", _enabled)
    cfg = RoleModelConfig(name="m", provider="openai", model="gpt-5.5")

    response = asyncio.run(OpenAIProvider().call(_messages(), cfg, tools=_tools()))

    assert [b["type"] for b in response.provider_parts] == [
        "tool_search_call", "tool_search_output"
    ]
    for block in response.provider_parts:
        assert "created_by" not in block, f"output-only field survived into {block['type']}"
    call, output = response.provider_parts
    assert call["arguments"] == {"query": "pcube"}, "the search query is preserved"
    assert output["tools"] == [{"tool_name": "research_toolkit_xspec_utils_ixpe_pcube"}], (
        "the loaded tool definitions are preserved — they are why the item is replayed at all"
    )


def test_openai_input_fields_are_read_from_the_sdk_not_hand_listed():
    """The keep-list comes from the SDK's own input type, so a new output-only field is
    excluded automatically. Hand-listing would rot into a mid-run 400 the next time OpenAI
    adds one — which it has done repeatedly (openai-python #3008, #2323, #2670)."""
    from auto_research.models.providers.openai import _input_fields

    call_fields = _input_fields("tool_search_call")
    output_fields = _input_fields("tool_search_output")

    assert "created_by" not in call_fields and "created_by" not in output_fields
    assert {"arguments", "type", "id"} <= call_fields
    assert {"tools", "type", "id"} <= output_fields


def test_openai_translation_is_stable_across_turns():
    """The same item translates to the same bytes every time — the cache-prefix invariant.

    Prompt caching keys on an exact prefix match, so a *conditional* rewrite (the popular
    `exclude_none` workaround) would serialize an item one way when a field is null and another
    way when it is not; the moment that flips mid-conversation the prefix diverges and every
    later turn silently re-bills at full price.
    """
    import json

    from auto_research.models.providers.openai import _as_input_item

    class _Item:
        type = "tool_search_call"

        def __init__(self, created_by):
            self._created_by = created_by

        def model_dump(self, mode=None):
            return {
                "type": "tool_search_call", "id": "ts_1", "call_id": None,
                "arguments": {"query": "pcube"}, "execution": "server",
                "status": "completed", "created_by": self._created_by,
            }

    populated = _as_input_item(_Item("assistant"))
    absent = _as_input_item(_Item(None))

    assert populated == absent, "translation must not depend on a field's value"
    assert json.dumps(populated) == json.dumps(absent), "identical bytes, identical key order"


# ── the 2026-08-03 second run-killer: a REQUIRED round-trip field we dropped ──────────────────


def test_openai_round_trips_the_namespace_of_a_discovered_tool():
    """A discovered tool is addressed by namespace; replaying without it loses the address.

    A tool loaded through tool search does not live in the default namespace, so OpenAI answers
    a replay that omits it with `Missing namespace for function_call '<name>'. It does not exist
    in the default namespace. Round-trip the model's function_call item with its namespace field
    included.` — which killed the live run b-run-q21-0803b at seq 547. We rebuilt the item from
    our own three fields, so the namespace was destroyed at ingest, not at replay.
    """
    call = ToolCall(
        id="call_1",
        name="research_toolkit_catalog_utils_data_catalog",
        arguments={"obs_db_path": "/x"},
        provider_fields={
            "type": "function_call", "call_id": "call_1", "id": "fc_1",
            "name": "research_toolkit_catalog_utils_data_catalog",
            "arguments": '{"obs_db_path": "/x"}', "namespace": "research_toolkit",
        },
    )
    turn = ModelResponse(content="", tool_calls=[call])

    items = _to_openai_input([*_messages(), turn])
    replayed = [i for i in items if i.get("type") == "function_call"][0]

    assert replayed["namespace"] == "research_toolkit"
    assert replayed["call_id"] == "call_1" and replayed["arguments"] == '{"obs_db_path": "/x"}'


def test_openai_canonical_fields_win_over_the_carried_record():
    """The executed call and the replayed call can never disagree.

    The provider's record rides along, but our own values overwrite it, so a stale or mismatched
    field in the carried record cannot change which tool the transcript says was called.
    """
    call = ToolCall(
        id="call_real", name="real_tool", arguments={"a": 1},
        provider_fields={
            "type": "function_call", "call_id": "call_STALE", "name": "stale_tool",
            "arguments": '{"a": 999}', "namespace": "ns",
        },
    )
    items = _to_openai_input([*_messages(), ModelResponse(content="", tool_calls=[call])])
    replayed = [i for i in items if i.get("type") == "function_call"][0]

    assert replayed["call_id"] == "call_real" and replayed["name"] == "real_tool"
    assert replayed["arguments"] == '{"a": 1}'
    assert replayed["namespace"] == "ns", "fields we do not interpret still ride along"


def test_openai_call_without_provider_fields_replays_unchanged():
    """Additive by construction: adapters that carry nothing are byte-identical to before."""
    call = ToolCall(id="c2", name="t", arguments={})
    items = _to_openai_input([*_messages(), ModelResponse(content="", tool_calls=[call])])
    replayed = [i for i in items if i.get("type") == "function_call"][0]

    assert replayed == {"type": "function_call", "call_id": "c2", "name": "t", "arguments": "{}"}


def test_openai_carries_reasoning_items_alongside_discovery():
    """A `tool_search_call` is rejected without the `reasoning` item that produced it.

    Live: `Item 'tsc_…' of type 'tool_search_call' was provided without its required 'reasoning'
    item: 'rs_…'`. We carried only the two discovery types, so the reasoning item was dropped at
    ingest and the pair arrived incomplete. Carrying is now the default and exclusion the
    exception, so an item type nobody anticipated rides along instead of going missing.
    """
    from auto_research.models.providers.openai import _UNCARRIED_ITEM_TYPES

    assert _UNCARRIED_ITEM_TYPES == {"function_call", "message"}, (
        "only items we replay from our own state are excluded from carrying"
    )

    turn = ModelResponse(
        content="",
        tool_calls=[ToolCall(id="c1", name="t", arguments={})],
        provider_parts=[
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {"type": "tool_search_call", "id": "tsc_1", "arguments": {}},
            {"type": "tool_search_output", "id": "tso_1", "tools": []},
        ],
    )
    items = _to_openai_input([*_messages(), turn])
    carried = [i["type"] for i in items if "type" in i]

    assert carried == ["reasoning", "tool_search_call", "tool_search_output", "function_call"], (
        "provider items replay in emission order, ahead of the call they enabled"
    )


def test_openai_drops_fabricated_nulls():
    """`model_dump()` invents absent optionals as null; the API rejects a null it never sent.

    Live: `Unknown parameter: 'input[2].status'` — on a field the input type *does* declare, so
    filtering by name alone is not enough (openai-python #3008). Both filters run once at capture,
    so the stored item is fixed and replays as identical bytes on every turn.
    """
    from auto_research.models.providers.openai import _as_input_item

    class _Item:
        type = "reasoning"

        def model_dump(self, mode=None):
            return {"type": "reasoning", "id": "rs_1", "summary": [],
                    "status": None, "encrypted_content": None, "content": None}

    out = _as_input_item(_Item())

    assert out == {"type": "reasoning", "id": "rs_1", "summary": []}
    assert "status" not in out and "encrypted_content" not in out
