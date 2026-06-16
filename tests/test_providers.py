"""Deterministic provider-adapter tests with mocked SDKs.

These verify the native structured-output and function-calling normalization in
each provider adapter without spending tokens. Live provider checks remain in
``tests/test_live_models.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from pydantic import BaseModel

from auto_research.models.providers.anthropic import AnthropicProvider, _mark_last_block_cacheable
from auto_research.models.providers.google import GoogleProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.types import ModelMessage, RoleModelConfig


class _Verdict(BaseModel):
    accepted: bool
    rationale: str


def _cfg() -> RoleModelConfig:
    return RoleModelConfig(name="m", provider="anthropic", model="fake-model")


def _messages():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="hi")]


class _FakeAnthropic:
    """Stand-in for anthropic.AsyncAnthropic; records the last request.

    ``parse`` serves the native-structured-output path; ``create`` serves the
    function-calling path (echoing a ``tool_use`` block, now with an ``id``).
    """

    last_request: dict | None = None
    last_parse: dict | None = None

    def __init__(self, *args, **kwargs):
        pass

    @property
    def messages(self):
        return self

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


def test_anthropic_cache_prefix_marks_system(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=True))
    system = _FakeAnthropic.last_request["system"]
    assert isinstance(system, list) and system[-1]["cache_control"] == {"type": "ephemeral"}
    tail = _FakeAnthropic.last_request["messages"][-1]["content"][-1]
    assert tail["cache_control"] == {"type": "ephemeral"}


def test_anthropic_cache_prefix_off_leaves_messages_unmarked(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=False))
    req = _FakeAnthropic.last_request
    assert req["system"] == "sys"
    assert req["messages"][-1]["content"] == "hi"
    assert "cache_control" not in req["tools"][0]


def test_anthropic_cache_prefix_marks_schema_tail(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), schema=_Verdict, cache_prefix=True))
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
    """AssistantToolUse/ToolResultMessage translate to each provider's native shape."""
    from auto_research.models.providers.anthropic import _to_anthropic_messages
    from auto_research.models.providers.openai import _to_openai_input
    from auto_research.models.types import AssistantToolUse, ToolCall, ToolResultMessage, ToolResultPart

    convo = [
        ModelMessage(role="system", content="s"),
        ModelMessage(role="user", content="u"),
        AssistantToolUse(tool_calls=[ToolCall(id="c1", name="foo", arguments={"a": 1})]),
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

_FAKE_G_RESP = SimpleNamespace(
    parsed=_Verdict(accepted=True, rationale="ok"),
    function_calls=[SimpleNamespace(name="research_toolkit_xspec_utils_plot_fit", args={"indices": _NESTED})],
    text="",
    usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=1),
)


class _FakeGClient:
    def __init__(self, *a, **k):
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model, contents, config):
        return _FAKE_G_RESP


def test_google_structured_output(monkeypatch):
    monkeypatch.setattr("google.genai.Client", _FakeGClient)
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
