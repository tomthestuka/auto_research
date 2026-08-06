"""Deterministic provider-adapter tests with mocked SDKs.

These verify the native structured-output and function-calling normalization in
each provider adapter without spending tokens. Live provider checks remain in
``tests/test_live_models.py``.

The Anthropic fake implements **both** namespaces the adapter routes between —
``client.messages`` and ``client.beta.messages`` (context editing selects the latter) — so these
tests hold whichever way ``context_mgmt_enabled`` is set. Config is always pinned per test rather
than read from the machine-local ``orchestration.yaml``, and the namespace-sensitive tests sweep
both settings explicitly. What the beta path *does* (the ``context_management`` block, edit
extraction, monitor event) belongs to ``tests/test_context_editing.py``; here it is only routing.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from auto_research.env import ContextEditingConfig
from auto_research.models.providers.anthropic import (
    AnthropicProvider,
    _mark_last_block_cacheable,
    _to_anthropic_messages,
)
from auto_research.models.providers.google import GoogleProvider
from auto_research.models.providers.openai import OpenAIProvider, _as_input_item
from auto_research.models.types import ModelHandle, ModelMessage, RoleModelConfig


def _pin_context_editing(monkeypatch, *, enabled: bool):
    """Pin the context-editing config so a test never reads machine-local `orchestration.yaml`.

    Left unpinned, anything touching `cache_prefix` silently tests whichever namespace this
    host's ``context_mgmt_enabled`` selects — passing or failing on a config value rather than
    on the adapter's behaviour.
    """
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.context_editing_config",
        lambda: ContextEditingConfig(
            enabled=enabled,
            beta_header="context-management-2025-06-27",
            strategy="clear_tool_uses_20250919",
            trigger_tokens=120000,
            clear_at_least_tokens=20000,
            keep_tool_uses=3,
        ),
    )


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch):
    """Default the file to the base namespace; namespace-sensitive tests re-pin per-parameter."""
    _pin_context_editing(monkeypatch, enabled=False)


class _Verdict(BaseModel):
    accepted: bool
    rationale: str


def _cfg() -> RoleModelConfig:
    return RoleModelConfig(name="m", provider="anthropic", model="fake-model")


def _messages():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="hi")]


# Minimal PNG header bytes — enough to assert the adapter loads and base64s the file.
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-plot-pixels"


def _figure(tmp_path):
    """Write a real image file and return a FigureRef pointing at it."""
    import hashlib
    from datetime import datetime

    from auto_research.schemas import FigureRef

    path = tmp_path / "pa_pd_stability.png"
    path.write_bytes(_PNG_BYTES)
    return FigureRef(
        path=path,
        caption="PD vs. time",
        mime_type="image/png",
        produced_by="ixpe_pa_pd_stability_plot",
        created_at=datetime(2026, 1, 1),
        sha256=hashlib.sha256(_PNG_BYTES).hexdigest(),
    )


class _FakeAnthropic:
    """Stand-in for anthropic.AsyncAnthropic; records the last request.

    ``parse`` serves the native-structured-output path; ``create`` serves the
    function-calling path (echoing a ``tool_use`` block, now with an ``id``).

    It exposes **both** namespaces the adapter can route to — ``client.messages`` and
    ``client.beta.messages`` — because context editing selects between them at call time
    (`anthropic.py`: ``messages_api = client.beta.messages if use_ctx_edit else client.messages``).
    A fake carrying only ``.messages`` silently constrains these tests to whichever branch the
    host's ``context_mgmt_enabled`` happens to select. ``last_namespace`` records which one ran,
    so a test can assert the routing rather than assume it.
    """

    last_request: dict | None = None
    last_parse: dict | None = None
    last_namespace: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    @property
    def messages(self):
        _FakeAnthropic.last_namespace = "base"
        return self

    @property
    def beta(self):
        return _FakeAnthropicBeta()

    async def parse(self, **request):
        _FakeAnthropic.last_parse = request
        instance = request["output_format"](accepted=True, rationale="ok")
        content = [SimpleNamespace(type="text", text='{"accepted": true, "rationale": "ok"}')]
        return SimpleNamespace(
            parsed_output=instance,
            content=content,
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )

    async def create(self, **request):
        _FakeAnthropic.last_request = request
        name = request["tools"][0]["name"]
        content = [
            SimpleNamespace(type="text", text="thinking"),
            SimpleNamespace(type="tool_use", id="tu_1", name=name, input={"command": "echo hi"}),
        ]
        return SimpleNamespace(content=content, usage=SimpleNamespace(input_tokens=11, output_tokens=7))


class _FakeAnthropicBeta:
    """``client.beta`` — the namespace context editing routes to, and the only one that accepts
    ``betas`` + ``context_management``. It reuses the same request-capturing surface, so an
    assertion reads identically whichever namespace the adapter chose."""

    @property
    def messages(self):
        _FakeAnthropic.last_namespace = "beta"
        return _FakeAnthropic()


def test_anthropic_structured_output_uses_parse(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    provider = AnthropicProvider()
    resp = asyncio.run(provider.call(_messages(), _cfg(), schema=_Verdict))

    # Native structured output via messages.parse(output_format=schema).
    assert _FakeAnthropic.last_parse["output_format"] is _Verdict
    assert resp.parsed == {"accepted": True, "rationale": "ok"}
    assert _Verdict.model_validate(resp.parsed).accepted is True
    assert resp.tool_calls == []
    assert resp.usage.input_tokens == 11


def test_anthropic_mark_last_block_cacheable_helper():
    text_message = [{"role": "user", "content": "hi"}]
    _mark_last_block_cacheable(text_message)
    assert text_message[-1]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]

    block_message = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ],
        }
    ]
    _mark_last_block_cacheable(block_message)
    blocks = block_message[-1]["content"]
    assert "cache_control" not in blocks[0]
    assert blocks[-1]["cache_control"] == {"type": "ephemeral"}

    _mark_last_block_cacheable([])


@pytest.mark.parametrize(
    "ctx_editing,namespace", [(False, "base"), (True, "beta")], ids=["base", "beta"]
)
def test_anthropic_cache_prefix_marks_system(monkeypatch, ctx_editing, namespace):
    """`cache_control` placement is namespace-independent, so assert it on both.

    Context editing routes `cache_prefix` calls to `client.beta.messages`; the breakpoints must
    land identically there. Swept rather than pinned so a change to either branch is caught
    whichever way `context_mgmt_enabled` is set on the host.
    """
    _pin_context_editing(monkeypatch, enabled=ctx_editing)
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    _FakeAnthropic.last_namespace = None
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=True))
    assert _FakeAnthropic.last_namespace == namespace
    system = _FakeAnthropic.last_request["system"]
    assert isinstance(system, list) and system[-1]["cache_control"] == {"type": "ephemeral"}
    tail = _FakeAnthropic.last_request["messages"][-1]["content"][-1]
    assert tail["cache_control"] == {"type": "ephemeral"}
    # The context-management block rides only the beta namespace.
    assert ("context_management" in _FakeAnthropic.last_request) is ctx_editing


def test_anthropic_cache_prefix_off_leaves_messages_unmarked(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=False))
    req = _FakeAnthropic.last_request
    assert req["system"] == "sys"
    assert req["messages"][-1]["content"] == "hi"
    assert "cache_control" not in req["tools"][0]


@pytest.mark.parametrize(
    "ctx_editing,namespace", [(False, "base"), (True, "beta")], ids=["base", "beta"]
)
def test_anthropic_cache_prefix_marks_schema_tail(monkeypatch, ctx_editing, namespace):
    """Same sweep on the structured-output path — `parse` exists on both namespaces."""
    _pin_context_editing(monkeypatch, enabled=ctx_editing)
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    _FakeAnthropic.last_namespace = None
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), schema=_Verdict, cache_prefix=True))
    assert _FakeAnthropic.last_namespace == namespace
    tail = _FakeAnthropic.last_parse["messages"][-1]["content"][-1]
    assert tail["cache_control"] == {"type": "ephemeral"}


def test_anthropic_function_calling(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    provider = AnthropicProvider()
    tools = [
        {
            "name": "mcp_builtin_bash_exec",
            "description": "run a shell command",
            "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
        }
    ]
    resp = asyncio.run(provider.call(_messages(), _cfg(), tools=tools, tool_choice="auto"))

    req = _FakeAnthropic.last_request
    assert req["tool_choice"] == {"type": "auto"}
    assert resp.parsed is None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "tu_1"
    assert resp.tool_calls[0].name == "mcp_builtin_bash_exec"
    assert resp.tool_calls[0].arguments == {"command": "echo hi"}
    assert resp.content == "thinking"


def test_anthropic_required_maps_to_any(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    provider = AnthropicProvider()
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    asyncio.run(provider.call(_messages(), _cfg(), tools=tools, tool_choice="required"))
    assert _FakeAnthropic.last_request["tool_choice"] == {"type": "any"}


def test_providers_roundtrip_tool_turns():
    """ModelResponse/ToolResultMessage translate to each provider's native shape."""
    from auto_research.models.providers.anthropic import _to_anthropic_messages
    from auto_research.models.providers.openai import _to_openai_input
    from auto_research.models.types import ModelResponse, ToolCall, ToolResultMessage, ToolResultPart

    convo = [
        ModelMessage(role="system", content="s"),
        ModelMessage(role="user", content="u"),
        ModelResponse(tool_calls=[ToolCall(id="c1", name="foo", arguments={"a": 1})]),
        ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="foo", content="ok")]),
    ]
    system, msgs = _to_anthropic_messages(convo)
    assert system == "s"
    assert msgs[1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "foo", "input": {"a": 1}}
    assert msgs[2]["content"][0]["tool_use_id"] == "c1"

    items = _to_openai_input(convo)
    assert {"type": "function_call", "call_id": "c1", "name": "foo", "arguments": '{"a": 1}'} in items
    assert any(i.get("type") == "function_call_output" and i["call_id"] == "c1" for i in items)


# --- OpenAI ----------------------------------------------------------------

_NESTED = {"IXPE": {"02008401": {"DU1": [1, 2, 3]}}}


class _FakeResponses:
    last: dict | None = None

    async def create(self, **kw):
        _FakeResponses.last = kw
        if "text" in kw:  # structured-output path: JSON string in output_text
            return SimpleNamespace(
                output_text=json.dumps({"accepted": True, "rationale": "ok"}),
                usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            )
        item = SimpleNamespace(  # function-calling path
            type="function_call",
            name="research_toolkit_xspec_utils_plot_fit",
            arguments=json.dumps({"indices": _NESTED}),
        )
        return SimpleNamespace(
            output=[item], output_text="",
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )


class _FakeOpenAI:
    def __init__(self, *a, **k):
        self.responses = _FakeResponses()


def test_openai_structured_output_parse(monkeypatch):
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    resp = asyncio.run(OpenAIProvider().call(_messages(), _cfg(), schema=_Verdict))
    fmt = _FakeResponses.last["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    # strict-mode sanitization: object marked closed + all properties required.
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == {"accepted", "rationale"}
    assert resp.parsed == {"accepted": True, "rationale": "ok"}
    assert resp.tool_calls == []


def test_openai_strict_schema_strips_unsupported_format_and_default():
    """A Path field emits format:path, which OpenAI rejects — it must be stripped."""
    from pathlib import Path as _P

    from auto_research.models.providers.openai import _strict_json_schema_format
    from auto_research.schemas import AnalystReport

    class _HasPath(BaseModel):
        path: _P
        note: str = "x"  # has a default → must be stripped + forced required

    fmt = _strict_json_schema_format(_HasPath)
    props = fmt["schema"]["properties"]
    assert "format" not in props["path"]              # format:path stripped
    assert "default" not in props["note"]             # default stripped
    assert set(fmt["schema"]["required"]) == {"path", "note"}
    assert fmt["schema"]["additionalProperties"] is False

    # The real AnalystReport (embeds FigureRef.path) must sanitize end to end —
    # no surviving unsupported `format` anywhere in the schema or its $defs.
    real = _strict_json_schema_format(AnalystReport)

    def _formats(node):
        out = []
        if isinstance(node, dict):
            if "format" in node:
                out.append(node["format"])
            for v in node.values():
                out.extend(_formats(v))
        elif isinstance(node, list):
            for it in node:
                out.extend(_formats(it))
        return out

    from auto_research.models.providers.openai import _OPENAI_SUPPORTED_FORMATS
    assert all(f in _OPENAI_SUPPORTED_FORMATS for f in _formats(real["schema"]))


def test_sanitizers_preserve_field_named_title():
    """A field literally named `title` must survive — sanitizers strip the JSON
    Schema `title` *annotation*, not a property whose name is `title`. Regression:
    ResearchReport.required listed `title` while properties had dropped it."""
    from auto_research.models.providers.google import _to_gemini_schema
    from auto_research.models.providers.openai import _strict_json_schema_format
    from auto_research.schemas.report import ResearchReport

    g = _to_gemini_schema(ResearchReport)
    assert "title" in g["properties"]
    assert set(g.get("required", [])) <= set(g["properties"])  # required ⊆ properties

    o = _strict_json_schema_format(ResearchReport)["schema"]
    assert "title" in o["properties"]
    assert set(o["required"]) == set(o["properties"])  # strict: all properties required


def test_loads_first_json_object_handles_surrounding_content():
    """The receive-side parser: clean input round-trips; leading/trailing content
    is tolerated (the first complete value wins); no-JSON returns None so the
    caller's structured-retry loop re-prompts instead of the call crashing."""
    from auto_research.models.providers._json import loads_first_json_object

    obj = {"accepted": True, "rationale": "ok"}
    # clean — identical to json.loads
    assert loads_first_json_object(json.dumps(obj)) == obj
    # trailing content after the object (the live GPT-co-lead 'Extra data' crash)
    assert loads_first_json_object(json.dumps(obj) + '\n\nNote: done.\n{"stray": 1}') == obj
    # leading prose before the object
    assert loads_first_json_object("Here is the verdict:\n" + json.dumps(obj)) == obj
    # a top-level list value
    assert loads_first_json_object("[1, 2] trailing") == [1, 2]
    # no JSON at all / empty → None (caller retries, no raise)
    assert loads_first_json_object("sorry, I cannot comply") is None
    assert loads_first_json_object("") is None


class _TrailingResponses(_FakeResponses):
    async def create(self, **kw):
        _FakeResponses.last = kw
        return SimpleNamespace(
            output_text=json.dumps({"accepted": True, "rationale": "ok"}) + "\n\nNote: extra.",
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )


class _TrailingOpenAI:
    def __init__(self, *a, **k):
        self.responses = _TrailingResponses()


def test_openai_structured_output_tolerates_trailing_data(monkeypatch):
    """A valid JSON object followed by extra content (a reasoning-model quirk that
    crashed a live run via a bare json.loads) must parse to the object, not raise."""
    monkeypatch.setattr("openai.AsyncOpenAI", _TrailingOpenAI)
    resp = asyncio.run(OpenAIProvider().call(_messages(), _cfg(), schema=_Verdict))
    assert resp.parsed == {"accepted": True, "rationale": "ok"}
    assert _Verdict.model_validate(resp.parsed).accepted is True


def test_openai_function_calling(monkeypatch):
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    tools = [{"name": "research_toolkit_xspec_utils_plot_fit", "description": "d",
              "input_schema": {"type": "object", "properties": {"indices": {"type": "object"}}}}]
    resp = asyncio.run(OpenAIProvider().call(_messages(), _cfg(), tools=tools, tool_choice="required"))
    assert _FakeResponses.last["tool_choice"] == "required"
    assert _FakeResponses.last["tools"][0]["type"] == "function"
    assert len(resp.tool_calls) == 1
    # JSON-string arguments from the API are decoded into a real nested dict.
    assert resp.tool_calls[0].arguments == {"indices": _NESTED}
    assert isinstance(resp.tool_calls[0].arguments["indices"], dict)


# --- Google ----------------------------------------------------------------

def _g_part(**fields):
    """A fake Gemini Part: a struct where every unset field reads as None (not a tagged union),
    so the adapter's part walk sees the same shape the SDK produces."""
    base = {"text": None, "thought": None, "thought_signature": None, "function_call": None,
            "function_response": None, "executable_code": None, "code_execution_result": None,
            "inline_data": None}
    return SimpleNamespace(**{**base, **fields},
                           model_dump=lambda **_: {k: v for k, v in {**base, **fields}.items()
                                                   if v is not None})


_G_CALL_PART = _g_part(
    function_call=SimpleNamespace(name="research_toolkit_xspec_utils_plot_fit",
                                  args={"indices": _NESTED}),
    thought_signature="sig-abc",
)

def _g_resp(*parts, parsed=None, text=""):
    """Shape-complete fake response: the adapter walks candidates[0].content.parts, so a fake
    that only implements the `.text` / `.function_calls` convenience views (which flatten a
    mixed turn and never expose thought_signature) would hide that walk entirely."""
    return SimpleNamespace(
        parsed=parsed,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))],
        function_calls=[p.function_call for p in parts if p.function_call is not None],
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=1),
    )


_FAKE_G_RESP = _g_resp(_G_CALL_PART)


class _FakeGClient:
    """Returns ``response`` (set per test); defaults to the function-calling turn."""

    response = _FAKE_G_RESP
    last_config = None

    def __init__(self, *a, **k):
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model, contents, config):
        _FakeGClient.last_config = config
        return type(self).response


def test_google_structured_output(monkeypatch):
    monkeypatch.setattr("google.genai.Client", _FakeGClient)
    monkeypatch.setattr(
        _FakeGClient, "response",
        _g_resp(_g_part(text='{"accepted": true, "rationale": "ok"}'),
                parsed=_Verdict(accepted=True, rationale="ok")),
    )
    cfg = RoleModelConfig(name="g", provider="google", model="fake")
    resp = asyncio.run(GoogleProvider().call(_messages(), cfg, schema=_Verdict))
    assert resp.parsed == {"accepted": True, "rationale": "ok"}
    assert resp.tool_calls == []


def test_google_function_calling(monkeypatch):
    monkeypatch.setattr("google.genai.Client", _FakeGClient)
    cfg = RoleModelConfig(name="g", provider="google", model="fake")
    tools = [{"name": "research_toolkit_xspec_utils_plot_fit", "description": "d",
              "input_schema": {"type": "object", "properties": {"indices": {"type": "object"}}}}]
    resp = asyncio.run(GoogleProvider().call(_messages(), cfg, tools=tools))
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == {"indices": _NESTED}


def test_google_thought_signature_survives_the_round_trip():
    """[I8]: a Gemini call's thought_signature rides on the same Part as the function_call, so it
    is carried on that call and re-attached on replay — Gemini 3.x 400s on the second call of a
    loop without it."""
    from google.genai import types

    from auto_research.models.providers.google import _to_gemini_contents
    from auto_research.models.types import ModelResponse, ToolCall

    def replay(signature):
        call = ToolCall(id="c1", name="foo", arguments={"a": 1},
                        provider_fields={"thought_signature": signature})
        _, contents = _to_gemini_contents([ModelResponse(tool_calls=[call])], types)
        return contents[0].parts[0]

    # The SDK types the signature as `bytes`, which is what ingest captures.
    part = replay(b"sig-abc")
    assert part.function_call.name == "foo"
    assert part.thought_signature == b"sig-abc"

    # And its JSON form (what a `model_dump(mode="json")` of the carried turn produces) decodes
    # back to the same bytes — so a serialized round trip is lossless. A signature carried as an
    # arbitrary str would NOT be: the SDK reads it as base64.
    assert replay(base64.b64encode(b"sig-abc").decode()).thought_signature == b"sig-abc"


# --- Figures: bytes at the multimodal boundary ([FIG-VIEW]) + inline-in-history ([V2]) ---

import base64  # noqa: E402


def _tool_turn_with_figure(fig):
    from auto_research.models.types import ToolResultMessage, ToolResultPart

    return ToolResultMessage(
        results=[ToolResultPart(tool_call_id="c1", name="plot", content="wrote plot", figures=[fig])]
    )


def test_anthropic_tool_result_figure_inline(tmp_path):
    """A tool's figure renders as an image block in the SAME user turn as its tool_result."""
    from auto_research.models.providers.anthropic import _to_anthropic_messages

    fig = _figure(tmp_path)
    _, msgs = _to_anthropic_messages([_tool_turn_with_figure(fig)])
    content = msgs[0]["content"]
    assert content[0]["type"] == "tool_result"  # tool_result stays first
    images = [b for b in content if b.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["source"]["data"]) == _PNG_BYTES  # real bytes loaded


def test_anthropic_images_kwarg_loads_bytes(monkeypatch, tmp_path):
    """Cross-agent figures passed via images= are base64'd into an image block, not text."""
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    fig = _figure(tmp_path)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools, images=[fig]))
    last = _FakeAnthropic.last_request["messages"][-1]
    assert any(b.get("type") == "image" for b in last["content"])


def test_openai_tool_result_figure_inline(tmp_path):
    """A tool's figure renders as an input_image user item right after function_call_output."""
    from auto_research.models.providers.openai import _to_openai_input

    fig = _figure(tmp_path)
    items = _to_openai_input([_tool_turn_with_figure(fig)])
    assert items[0]["type"] == "function_call_output"
    user_items = [i for i in items if i.get("role") == "user"]
    assert user_items
    imgs = [c for c in user_items[-1]["content"] if c["type"] == "input_image"]
    assert len(imgs) == 1
    assert imgs[0]["image_url"].startswith("data:image/png;base64,")
    assert base64.b64decode(imgs[0]["image_url"].split(",", 1)[1]) == _PNG_BYTES


def test_google_tool_result_figure_inline(tmp_path):
    """A tool's figure renders as an inline-data image Part in a user Content after the response."""
    from google.genai import types

    from auto_research.models.providers.google import _to_gemini_contents

    fig = _figure(tmp_path)
    _, contents = _to_gemini_contents([_tool_turn_with_figure(fig)], types)
    last = contents[-1]
    assert last.role == "user"
    image_parts = [p for p in last.parts if getattr(p, "inline_data", None) is not None]
    assert len(image_parts) == 1
    assert image_parts[0].inline_data.mime_type == "image/png"
    assert image_parts[0].inline_data.data == _PNG_BYTES


# --- The open part carrier: ModelResponse as the neutral assistant turn ------
#
# `ModelResponse` is the provider-authored turn in BOTH directions: an adapter returns it, and
# `_react` appends it straight back. So the properties worth pinning are (a) no part the model
# authored is dropped on ingest, (b) each adapter reassembles its own dialect's order on replay,
# and (c) a request that cannot legally carry a part drops only that part.


def _block(btype, **fields):
    dumped = {"type": btype, **fields}
    return SimpleNamespace(type=btype, model_dump=lambda mode=None, d=dumped: dict(d), **fields)


class _FakeAnthropicRichTurn(_FakeAnthropic):
    """A turn shaped like a real one, observed live 2026-08-05 on claude-sonnet-5 under adaptive
    thinking with tool search:

        thinking → server_tool_use → tool_search_tool_result → thinking → tool_use

    Two things about that layout are load-bearing. Reasoning is **interleaved** with tool use, not
    hoisted to the front — an earlier replay that reassembled the turn thinking-first moved the
    second block from index 3 to index 1 and the API rejected it (*"blocks must remain as they were
    in the original response"*). And the last block type is one this code has never heard of, which
    is the case the open carrier exists for: the installed `ContentBlock` union already lags
    Anthropic's own docs.
    """

    async def create(self, **request):
        _FakeAnthropic.last_request = request
        return SimpleNamespace(
            content=[
                _block("thinking", thinking="first", signature="sig-1"),
                _block("text", text="on it"),
                _block("server_tool_use", id="srvtoolu_1"),
                _block("tool_search_tool_result", tool_use_id="srvtoolu_1"),
                _block("thinking", thinking="second", signature="sig-2"),
                _block("totally_new_block", payload=1),
                _block("tool_use", id="tu_1", name=request["tools"][0]["name"],
                       input={"command": "echo hi"}),
            ],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )


def _rich_turn(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropicRichTurn)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    return asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools))


_LIVE_TURN_SHAPE = [
    "thinking", "text", "server_tool_use", "tool_search_tool_result",
    "thinking", "totally_new_block", "tool_use",
]


def test_anthropic_carries_the_whole_turn(monkeypatch):
    """Every block is carried, in emission order — the carried list IS the turn. `content` and
    `tool_calls` are views onto it for the harness, not a second source of truth."""
    resp = _rich_turn(monkeypatch)
    assert resp.content == "on it"
    assert [c.name for c in resp.tool_calls] == ["t"]
    assert [p["type"] for p in resp.provider_parts] == _LIVE_TURN_SHAPE
    assert resp.provider_parts[0]["signature"] == "sig-1"   # verbatim, not re-typed


def test_anthropic_replays_the_turn_verbatim(monkeypatch):
    """A turn Anthropic authored goes back exactly as it came, interleaving intact.

    Reassembling it from a dialect rule is what the API rejects: hoisting the second `thinking`
    block from index 4 to index 1 returns *"blocks must remain as they were in the original
    response"* (400, reproduced live 2026-08-05 on the second turn of a loop)."""
    resp = _rich_turn(monkeypatch)
    _, msgs = _to_anthropic_messages([*_messages(), resp])
    assert [b.get("type") for b in msgs[-1]["content"]] == _LIVE_TURN_SHAPE
    assert msgs[-1]["content"] == resp.provider_parts     # the same blocks, not rebuilt ones


def test_anthropic_echoes_the_turn_whatever_the_request_declares(monkeypatch):
    """There is no longer a request shape that drops the carried blocks.

    Anthropic documents the continuation rule as *"echo the assistant message exactly as
    received"*, where *"rebuilding the message or filtering out `redacted_thinking` blocks
    triggers a 400 error"* — and a block naming a tool is rejected when that tool is absent
    (2026-07-29 run-killer), with filtering just those blocks also a 400 (live 2026-08-05).
    Those two rules used to leave a tool-less request no legal option, so it rebuilt from the
    views. The dilemma is resolved upstream instead: a terminal compose declares its tools with
    `tool_choice="none"`, so every reference resolves and the echo path always applies."""
    resp = _rich_turn(monkeypatch)
    _, msgs = _to_anthropic_messages([*_messages(), resp])
    assert msgs[-1]["content"] == resp.provider_parts
    assert [b.get("type") for b in msgs[-1]["content"]] == _LIVE_TURN_SHAPE


def test_anthropic_reconstructs_a_turn_we_synthesized():
    """A turn with no carried blocks — every test fake, anything built as
    `ModelResponse(tool_calls=[…])` — is rebuilt from the typed views. This is the only case
    where the adapter authors block order."""
    from auto_research.models.types import ModelResponse, ToolCall

    turn = ModelResponse(content="hi", tool_calls=[ToolCall(id="c1", name="t", arguments={"a": 1})])
    _, msgs = _to_anthropic_messages([turn])
    assert msgs[0]["content"] == [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "c1", "name": "t", "input": {"a": 1}},
    ]


_BOTH_TOOLS = [
    {"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}
]


def test_schema_and_tools_are_not_mutually_exclusive(monkeypatch):
    """All three providers accept a schema and tools in one request (live-verified 2026-08-04);
    the model then either calls a tool or emits the structured answer. The exclusivity this
    replaced was self-imposed, and it is what forced the tool-less compose."""
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    handle = ModelHandle(role="reviewer", config=_cfg(), provider=AnthropicProvider())
    resp = asyncio.run(handle.call(_messages(), schema=_Verdict, tools=_BOTH_TOOLS))
    assert resp.parsed == {"accepted": True, "rationale": "ok"}


def test_anthropic_sends_the_tools_alongside_the_schema(monkeypatch):
    """Both reach the request — the assertion the test above was missing.

    Until 2026-08-06 each adapter ran `if schema is not None: … return` BEFORE `if tools is not
    None`, so passing both silently dropped the tools while still returning `parsed`. A test that
    only checked `parsed` therefore passed against an adapter that honoured neither the parameter
    nor the documented contract, and the terminal compose could not declare its tools — which is
    the request shape behind two run-killers.
    """
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), schema=_Verdict,
                                         tools=_BOTH_TOOLS, tool_choice="none"))
    sent = _FakeAnthropic.last_parse
    assert sent["output_format"] is _Verdict, "structured output rides the parse path"
    assert [t["name"] for t in sent["tools"]] == ["t"], "and the tools ride the same call"
    assert sent["tool_choice"] == {"type": "none"}


def test_openai_sends_the_tools_alongside_the_schema(monkeypatch):
    """The OpenAI half of the same property: one `responses.create`, both parameters."""
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    asyncio.run(OpenAIProvider().call(_messages(), _cfg(), schema=_Verdict,
                                      tools=_BOTH_TOOLS, tool_choice="none"))
    sent = _FakeResponses.last
    assert sent["text"]["format"]["type"] == "json_schema"
    assert [t["name"] for t in sent["tools"]] == ["t"]
    assert sent["tool_choice"] == "none"


def test_tool_choice_none_uses_each_provider_spelling(monkeypatch):
    """`"none"` declares the tools and forbids calling them — the terminal-compose shape.

    Documented on all three: OpenAI *"set `tool_choice` to `none` to imitate the behavior of
    passing no functions"*; Anthropic prices `none` beside `auto` in the tool-use system-prompt
    table (so the definitions are still sent); Google *"Model is prohibited from making function
    calls"*. Live-verified on OpenAI and Anthropic 2026-08-06.
    """
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=_BOTH_TOOLS,
                                         tool_choice="none"))
    assert _FakeAnthropic.last_request["tool_choice"] == {"type": "none"}

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    asyncio.run(OpenAIProvider().call(_messages(), _cfg(), tools=_BOTH_TOOLS,
                                      tool_choice="none"))
    assert _FakeResponses.last["tool_choice"] == "none"

    monkeypatch.setattr("google.genai.Client", _FakeGClient)
    gcfg = RoleModelConfig(name="g", provider="google", model="fake")
    asyncio.run(GoogleProvider().call(_messages(), gcfg, tools=_BOTH_TOOLS, tool_choice="none"))
    assert _FakeGClient.last_config.tool_config.function_calling_config.mode == "NONE"


def test_openai_input_allowlist_covers_types_no_hand_list_had():
    """The allowlist is derived from the SDK's own input union, not hand-written. With an open
    carrier an unmapped item type would get NO field filtering and take its server-stamped
    fields into the next request — the 2026-08-03 400, one item type later."""
    from auto_research.models.providers.openai import _input_fields, _input_shapes

    shapes = _input_shapes()
    assert len(shapes) > 20, "the union walk found the input item types"
    # Types the old 4-entry map never covered:
    for item_type in ("web_search_call", "mcp_call", "computer_call", "custom_tool_call"):
        assert shapes[item_type], item_type
        assert "created_by" not in shapes[item_type]

    item = SimpleNamespace(type="web_search_call",
                           model_dump=lambda mode=None: {"type": "web_search_call", "id": "ws_1",
                                                         "status": "completed",
                                                         "created_by": "assistant"})
    assert "created_by" not in _as_input_item(item)
    assert _input_fields("a_type_openai_has_not_shipped_yet") == frozenset()
