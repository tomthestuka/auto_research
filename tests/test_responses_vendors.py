"""Every vendor served over the OpenAI Responses dialect, swept through one set of checks.

Four vendors now share one adapter and differ only by descriptor, so the thing worth testing is
that each descriptor's *declared* policy is what actually reaches the request. The lesson these
are written to (2026-08-06) is that a test asserting the result of a call cannot see that a
parameter was ignored or never sent, so every check here reads the request the fake client
received.

The sweep also carries the shared wire behaviour across all four, which is the property that
justifies sharing it: a fix for one vendor's replay is a fix for all of them, and a divergence
would otherwise only surface live against whichever vendor a run happened to use.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from auto_research.models.providers import responses_vendors
from auto_research.models.providers._responses import ResponsesProvider, ResponsesVendor
from auto_research.models.providers.dashscope import DashscopeProvider
from auto_research.models.providers.deepseek import DeepSeekProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.providers.zai import ZaiProvider
from auto_research.models.types import CHAT_PROVIDERS, ModelMessage, RoleModelConfig
from auto_research.schemas import FigureRef


ADAPTERS: tuple[type[ResponsesProvider], ...] = (
    OpenAIProvider,
    DeepSeekProvider,
    ZaiProvider,
    DashscopeProvider,
)


class _Verdict(BaseModel):
    accepted: bool
    rationale: str


class _FakeResponses:
    last: dict | None = None

    async def create(self, **kw):
        _FakeResponses.last = kw
        # Faithful to how a vendor behaves: the emit tool is answered whenever it is DECLARED,
        # not only when it is forced. A vendor that refuses compulsion under thinking still
        # offers it under `auto`, and keying off the choice alone made that case untestable.
        declared = [t.get("name") for t in kw.get("tools") or []]
        if "emit_structured_response" in declared:
            item = SimpleNamespace(
                type="function_call",
                name="emit_structured_response",
                arguments=json.dumps({"accepted": True, "rationale": "ok"}),
                call_id="emit1",
            )
            return SimpleNamespace(
                output=[item], output_text="",
                usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            )
        if "text" in kw:
            return SimpleNamespace(
                output_text=json.dumps({"accepted": True, "rationale": "ok"}),
                output=[],
                usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            )
        item = SimpleNamespace(
            type="function_call",
            name="read_file",
            arguments=json.dumps({"path": "/tmp/x"}),
            call_id="c1",
        )
        return SimpleNamespace(
            output=[item],
            output_text="",
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
        )


class _FakeOpenAI:
    """Records the constructor kwargs, which is where base_url and the key are decided."""

    last_init: dict | None = None

    def __init__(self, *a, **k):
        _FakeOpenAI.last_init = k
        self.responses = _FakeResponses()


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch):
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    _FakeResponses.last = None
    _FakeOpenAI.last_init = None


def _cfg(adapter: type[ResponsesProvider], **over) -> RoleModelConfig:
    return RoleModelConfig(
        name="m", provider=adapter.vendor.name, model="fake-model", **over
    )


def _messages():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="hi")]


def _call(adapter: type[ResponsesProvider], **kw):
    return asyncio.run(adapter().call(_messages(), _cfg(adapter), **kw))


def _tools():
    return [
        {
            "name": "read_file",
            "description": "d",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


# --------------------------------------------------------------- registry-wide properties


def test_every_responses_vendor_is_a_configurable_chat_provider():
    """A descriptor nobody can name in config is dead code; a name with no adapter raises."""
    for vendor in responses_vendors():
        assert vendor.name in CHAT_PROVIDERS


def test_the_adapter_list_and_the_registry_agree():
    """This module sweeps a hand-written list, so it must not fall behind the real registry."""
    assert {a.vendor.name for a in ADAPTERS} == {v.name for v in responses_vendors()}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_each_vendor_declares_a_distinct_documented_policy(adapter):
    """The descriptor is the whole per-vendor surface, so it must be fully populated."""
    vendor = adapter.vendor
    assert isinstance(vendor, ResponsesVendor)
    assert vendor.key_env, "a vendor with no key variable falls back to the SDK's env lookup"
    assert vendor.limits.image_mimes, "no vendor may open the image channel to any mime"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_the_shared_retry_layer_owns_responses_retries_and_each_attempt_is_bounded(adapter):
    """SDK defaults are ten minutes plus two hidden retries; this repo logs and owns retries."""
    _call(adapter)
    assert _FakeOpenAI.last_init["timeout"] == 300.0
    assert _FakeOpenAI.last_init["max_retries"] == 0


def test_qwen_enables_its_documented_session_cache_for_every_call():
    _call(DashscopeProvider, cache_prefix=True)
    assert _FakeOpenAI.last_init["default_headers"] == {
        "x-dashscope-session-cache": "enable"
    }
    _call(DashscopeProvider, cache_prefix=False)
    assert _FakeOpenAI.last_init["default_headers"] == {
        "x-dashscope-session-cache": "enable"
    }


# --------------------------------------------------------------- client construction


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_the_client_is_built_at_the_vendors_own_endpoint(adapter):
    _call(adapter)
    assert _FakeOpenAI.last_init["base_url"] == adapter.vendor.base_url


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_a_member_may_override_the_endpoint(adapter):
    """A plan-specific host must stay configurable without a code change."""
    cfg = _cfg(adapter, base_url="https://example.invalid/v1")
    asyncio.run(adapter().call(_messages(), cfg))
    assert _FakeOpenAI.last_init["base_url"] == "https://example.invalid/v1"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_the_key_is_passed_explicitly_not_left_to_the_sdk(adapter, monkeypatch):
    """The whole point of the key pool: a client that resolves its own env skips rotation."""
    monkeypatch.setenv(adapter.vendor.key_env, "sk-test-explicit")
    from auto_research.models import keys as keys_mod

    keys_mod.reset_pools()
    _call(adapter)
    assert _FakeOpenAI.last_init["api_key"] == "sk-test-explicit"


# --------------------------------------------------------------- per-capability gating


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_prompt_cache_key_is_sent_only_where_it_is_supported(adapter):
    """DeepSeek marks it "Not supported"; sending it anyway is a 400 on every cached call."""
    _call(adapter, cache_prefix=True)
    sent = "prompt_cache_key" in _FakeResponses.last
    assert sent is adapter.vendor.supports_prompt_cache_key


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_a_schema_reaches_the_request_or_is_refused_with_a_reason(adapter):
    """A vendor that accepts `text.format` and ignores it must not be handed a schema.

    Silently returning invented keys into a Pydantic validator is worse than failing, so the
    adapter refuses rather than sending a constraint it knows is advisory there.
    """
    if adapter.vendor.supports_strict_schema:
        resp = _call(adapter, schema=_Verdict)
        fmt = _FakeResponses.last["text"]["format"]
        assert fmt["type"] == "json_schema" and fmt["strict"] is True
        assert set(fmt["schema"]["required"]) == {"accepted", "rationale"}
        assert resp.parsed == {"accepted": True, "rationale": "ok"}
    elif adapter.vendor.schema_via_tool:
        resp = _call(adapter, schema=_Verdict)
        assert "text" not in _FakeResponses.last, "no format is sent where it is ignored"
        assert resp.parsed == {"accepted": True, "rationale": "ok"}
    else:
        with pytest.raises(RuntimeError, match="does not honour it"):
            _call(adapter, schema=_Verdict)


def test_a_vendors_narrower_format_set_strips_the_formats_it_refuses():
    """DeepSeek's strict mode accepts fewer `format` values than the API it copies.

    It enumerates them in the refusal ("unknown variant `date`, expected one of `email`,
    `hostname`, `ipv4`, `ipv6`, `uuid`"), and three of the nine real schemas carry a date field,
    so an unstripped `format` is a request-time 400 rather than a value the vendor ignores. The
    assertion is on the schema that reaches the request, since a rejection is the only other way
    to find out.
    """
    from datetime import date

    from auto_research.models.providers._responses import _strict_json_schema_format

    class _Dated(BaseModel):
        when: date

    openai_fmt = _strict_json_schema_format(_Dated, OpenAIProvider.vendor.strict_schema_formats)
    deepseek_fmt = _strict_json_schema_format(
        _Dated, DeepSeekProvider.vendor.strict_schema_formats
    )
    assert openai_fmt["schema"]["properties"]["when"].get("format") == "date"
    assert "format" not in deepseek_fmt["schema"]["properties"]["when"]


def test_a_date_field_survives_the_whole_send_path_on_a_narrow_vendor():
    """End to end through `call`, because the stripping only matters where the request is built."""
    from datetime import date

    class _Dated(BaseModel):
        when: date

    asyncio.run(DeepSeekProvider().call(_messages(), _cfg(DeepSeekProvider), schema=_Dated))
    # `text.format` is the wrapper key, so the check has to look at the field's own subschema.
    field = _FakeResponses.last["text"]["format"]["schema"]["properties"]["when"]
    assert "format" not in field, f"a refused format reached the request: {field}"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_the_search_tool_is_declared_only_where_a_server_can_run_it(adapter, monkeypatch):
    """A deferred tool nobody can resolve is a tool the model cannot call at all.

    Swept over both settings of the flag rather than trusting whichever the config file
    currently holds, which is the config-flip lesson.
    """
    from auto_research.env import ToolSearchConfig

    for enabled in (True, False):
        monkeypatch.setattr(
            "auto_research.models.providers._responses.tool_search_config",
            lambda: ToolSearchConfig(
                enabled=enabled,
                anthropic_tool="tool_search_tool_bm25_20251119",
                openai_tool="tool_search",
            ),
        )
        tools = _tools()
        tools[0]["defer_loading"] = True
        _call(adapter, tools=tools)
        declared = [t.get("type") for t in _FakeResponses.last["tools"]]
        expect = enabled and adapter.vendor.supports_tool_search
        assert ("tool_search" in declared) is expect
        # A tool is only deferred when something can resolve the deferred reference.
        assert _FakeResponses.last["tools"][0].get("defer_loading", False) is expect


# --------------------------------------------------------------- shared wire behaviour


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_function_calling_decodes_identically_on_every_vendor(adapter):
    resp = _call(adapter, tools=_tools(), tool_choice="required")
    assert _FakeResponses.last["tool_choice"] == "required"
    assert _FakeResponses.last["tools"][0]["type"] == "function"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].arguments == {"path": "/tmp/x"}


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_tool_choice_none_keeps_the_declarations(adapter):
    """The terminal-compose shape: declare the tools, forbid calling them."""
    _call(adapter, tools=_tools(), tool_choice="none")
    assert _FakeResponses.last["tool_choice"] == "none"
    assert _FakeResponses.last["tools"], "declarations must survive, or replay orphans"


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_usage_maps_into_the_neutral_buckets_on_every_vendor(adapter):
    resp = _call(adapter, tools=_tools())
    assert resp.usage.input_tokens == 5
    assert resp.usage.output_tokens == 2


# --------------------------------------------------------------- per-model vision


def _figure(tmp_path) -> FigureRef:
    import hashlib
    from datetime import datetime

    path = tmp_path / "plot.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"pixels"
    path.write_bytes(payload)
    return FigureRef(
        path=path,
        caption="a plot",
        mime_type="image/png",
        produced_by="t",
        created_at=datetime(2026, 1, 1),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_an_image_is_withheld_with_a_note_on_deepseeks_text_only_endpoint(tmp_path):
    """DeepSeek's Responses reference explicitly substitutes placeholders for image input.

    The figure becomes a model-visible note: an absence the model cannot see invites it to
    invent the content.
    """
    cfg = RoleModelConfig(name="m", provider="deepseek", model="deepseek-v4-flash")
    asyncio.run(DeepSeekProvider().call(_messages(), cfg, images=[_figure(tmp_path)]))
    sent = json.dumps(_FakeResponses.last["input"])
    assert "does not accept image input" in sent
    assert "input_image" not in sent


def test_qwen_max_receives_an_image_its_current_model_page_supports(tmp_path):
    cfg = RoleModelConfig(name="m", provider="dashscope", model="qwen3.8-max", vision=True)
    asyncio.run(DashscopeProvider().call(_messages(), cfg, images=[_figure(tmp_path)]))
    sent = json.dumps(_FakeResponses.last["input"])
    assert "input_image" in sent
    assert "data:image/png;base64," in sent


def test_an_optional_model_field_is_inlined_for_a_vendor_that_will_not_follow_a_ref():
    """``X | None`` is the shape that breaks a validator which judges anyOf branches structurally.

    Pydantic emits it as a bare ``$ref`` beside a null branch. OpenAI resolves the reference;
    DeepSeek does not, and refuses the request. Asserted on the schema that reaches the wire,
    since the alternative way to learn it is a 400.
    """
    from auto_research.models.providers._responses import _strict_json_schema_format

    class _Inner(BaseModel):
        why: str

    class _Outer(BaseModel):
        maybe: _Inner | None

    def branches(fmt):
        return fmt["schema"]["properties"]["maybe"]["anyOf"]

    plain = _strict_json_schema_format(_Outer, resolve_refs_in=frozenset())
    assert any(set(b) == {"$ref"} for b in branches(plain)), "expected an unresolved reference"

    inlined = _strict_json_schema_format(_Outer, resolve_refs_in=frozenset({"anyOf", "items"}))
    assert not any(set(b) == {"$ref"} for b in branches(inlined))
    expanded = [b for b in branches(inlined) if b.get("type") == "object"]
    assert expanded and "why" in expanded[0]["properties"], "the definition was not expanded"



VIA_TOOL = [a for a in ADAPTERS if a.vendor.schema_via_tool]
"""Vendors reaching a schema through a tool call rather than `text.format`."""


@pytest.mark.parametrize("adapter", VIA_TOOL, ids=lambda a: a.vendor.name)
def test_the_schema_rides_as_a_forced_tool_call(adapter):
    """The schema becomes one tool's parameters and `tool_choice` is pinned to it.

    This is the route a coding harness already uses, so it reaches a vendor whose
    ``text.format`` is a no-op. Asserted on the request, because a reply that happens to look
    right cannot show that the constraint was actually applied.
    """
    _call(adapter, schema=_Verdict)
    sent = _FakeResponses.last
    emit = [t for t in sent["tools"] if t.get("name") == "emit_structured_response"]
    assert len(emit) == 1, "exactly one emit tool"
    assert set(emit[0]["parameters"]["required"]) == {"accepted", "rationale"}
    # Forced where the vendor permits it; merely offered where it refuses compulsion under
    # thinking. Both reach a structured answer, and the difference is the vendor's own rule.
    expected = (
        {"type": "function", "name": "emit_structured_response"}
        if adapter.vendor.forces_tool_choice
        else "auto"
    )
    assert sent["tool_choice"] == expected


@pytest.mark.parametrize("adapter", VIA_TOOL, ids=lambda a: a.vendor.name)
def test_the_loop_tools_stay_declared_beside_the_emit_tool(adapter):
    """A replayed trajectory names the loop's tools, so dropping them is the 400 of 2026-07-29.

    Forcing the emit tool is what replaces ``tool_choice="none"`` here; the declarations
    themselves must survive, or the compose cannot replay the work it is summarising.
    """
    _call(adapter, schema=_Verdict, tools=_tools(), tool_choice="none")
    names = [t.get("name") for t in _FakeResponses.last["tools"]]
    assert "read_file" in names and "emit_structured_response" in names
    if adapter.vendor.forces_tool_choice:
        assert _FakeResponses.last["tool_choice"]["name"] == "emit_structured_response"
    else:
        assert _FakeResponses.last["tool_choice"] == "auto"


@pytest.mark.parametrize("adapter", VIA_TOOL, ids=lambda a: a.vendor.name)
def test_the_emit_call_is_consumed_not_handed_to_the_loop(adapter):
    """It must reach `parsed` and never `tool_calls`.

    Surfacing it as a tool call would send the ReAct loop looking for a tool that does not
    exist, and would replay a `function_call` with no matching output on the next turn.
    """
    resp = _call(adapter, schema=_Verdict, tools=_tools())
    assert resp.parsed == {"accepted": True, "rationale": "ok"}
    assert [c.name for c in resp.tool_calls] == []


@pytest.mark.parametrize("adapter", VIA_TOOL, ids=lambda a: a.vendor.name)
def test_a_vendor_that_declines_the_forced_call_fails_loudly(adapter, monkeypatch):
    """Silence must not read as an ordinary tool-less reply.

    A forced call is structurally guaranteed, so its absence is a broken assumption rather than
    a legitimate outcome, and a `parsed=None` returned quietly would surface much later as a
    confusing validation error.
    """

    class _Silent(_FakeResponses):
        async def create(self, **kw):
            _FakeResponses.last = kw
            return SimpleNamespace(
                output=[], output_text="here is some prose instead",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        type("_C", (), {"__init__": lambda self, *a, **k: setattr(self, "responses", _Silent())}),
    )
    # No items at all: neither an answer nor a tool call in progress, which is the one case
    # that stays non-retryable (a `ValueError` here would burn the retry budget on a vendor
    # that has declined). The wording changed 2026-08-27 when the branch split in three.
    with pytest.raises(RuntimeError, match="neither that call nor any tool call"):
        _call(adapter, schema=_Verdict)


def test_a_native_strict_vendor_spends_no_tool_slot_on_the_schema():
    """The emit tool exists only where it is needed; OpenAI keeps native decoding."""
    _call(OpenAIProvider, schema=_Verdict, tools=_tools())
    names = [t.get("name") for t in _FakeResponses.last["tools"]]
    assert "emit_structured_response" not in names
    assert _FakeResponses.last["tool_choice"] == "auto"


# --------------------------------------------------------------- the "null"-string quirk


def _norm(value, schema):
    from auto_research.models.providers._responses import _normalise_null_strings

    return _normalise_null_strings(value, schema)


def test_a_null_string_becomes_none_where_no_string_branch_exists():
    """GLM intermittently writes "null" where it means JSON null (live, 2026-08-24).

    On `date | None` or `float | None` the string cannot be a legitimate value, so reading it as
    null is the only interpretation available and the coercion is unambiguous.
    """
    schema = {"type": "object", "properties": {
        "since": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
        "tolerance": {"anyOf": [{"type": "number"}, {"type": "null"}]}}}
    assert _norm({"since": "null", "tolerance": "None"}, schema) == {
        "since": None, "tolerance": None}


def test_a_null_string_survives_where_the_field_accepts_a_string():
    """`str | None` makes "null" a meaningful value, so guessing would destroy real content."""
    schema = {"type": "object", "properties": {
        "label": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    assert _norm({"label": "null"}, schema) == {"label": "null"}


@pytest.mark.parametrize("constraint", [
    {"enum": ["null", "unknown"]},
    {"const": "null"},
    {"pattern": "^(null|unknown)$"},
])
def test_a_null_string_survives_when_an_exact_string_constraint_accepts_it(constraint):
    """Constrained strings are data when their constraint expressly accepts this spelling."""
    schema = {"type": "object", "properties": {
        "label": {"anyOf": [{"type": "string", **constraint}, {"type": "null"}]}}}
    assert _norm({"label": "null"}, schema) == {"label": "null"}


def test_a_required_field_is_never_coerced():
    """Without a null branch the schema does not permit null, so the value stands as written."""
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    assert _norm({"name": "null"}, schema) == {"name": "null"}


def test_the_coercion_reaches_nested_objects_and_lists():
    """The lead's own AuthoringDecision carries the risky fields inside a list of nested models."""
    schema = {"type": "object", "properties": {
        "specs": {"type": "array", "items": {"type": "object", "properties": {
            "tolerance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "name": {"type": "string"}}}}}}
    out = _norm({"specs": [{"tolerance": "null", "name": "a"}]}, schema)
    assert out == {"specs": [{"tolerance": None, "name": "a"}]}


def test_the_coercion_leaves_every_real_value_alone():
    """It normalises spelling, never content: nothing is invented, defaulted or dropped."""
    schema = {"type": "object", "properties": {
        "tolerance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "note": {"type": "string"}}}
    payload = {"tolerance": 0.05, "note": "kept"}
    assert _norm(payload, schema) == payload


def test_the_real_authoring_schema_round_trips_through_the_quirk():
    """End to end on AuthoringDecision, whose optional floats are the live risk surface."""
    from auto_research.schemas import AuthoringDecision
    from auto_research.models.providers._responses import _strict_json_schema_format

    params = _strict_json_schema_format(
        AuthoringDecision, resolve_refs_in=frozenset({"items"})
    )["schema"]
    # `tool_specs.items` is a `$ref`, so resolving the `items` position expands ToolSpec in place.
    # `$defs` legitimately survives here: the untouched `anyOf` positions still point into it.
    tol = params["properties"]["tool_specs"]["items"]["properties"]["replication_tolerance"]
    assert "null" in {b.get("type") for b in tol["anyOf"]}, "expected an optional float"
    assert _norm({"tool_specs": [{"replication_tolerance": "null"}]}, params) == {
        "tool_specs": [{"replication_tolerance": None}]}


def test_a_vendor_that_refuses_compulsion_offers_the_emit_tool_instead():
    """Thinking models commonly refuse a named tool_choice; the schema still gets through.

    Anthropic documents the rule and Alibaba enforces it with a clearer message. We want the
    thinking, so the emit tool is declared under `auto` rather than forced, and the loud raise
    on a declined call is what replaces the structural guarantee.
    """
    _call(DashscopeProvider, schema=_Verdict, tools=_tools())
    sent = _FakeResponses.last
    assert sent["tool_choice"] == "auto", "a named choice is refused under thinking here"
    assert "emit_structured_response" in [t.get("name") for t in sent["tools"]]


def test_a_vendor_that_allows_compulsion_still_forces_the_call(monkeypatch):
    """The flag must not weaken the guarantee where a vendor honours it.

    Driven from a synthetic descriptor rather than a real adapter: `forces_tool_choice` defaults
    True, so a newly added `schema_via_tool` vendor takes this path until measured otherwise, but
    as of 2026-08-27 no *measured* vendor does -- Z.AI was the example and it turned out to ignore
    a named `tool_choice` (5 draws of 5). Asserting this against Z.AI asserted a false fact; the
    code path is still live and still needs a guard.
    """
    import dataclasses

    from auto_research.models.providers.zai import ZAI_VENDOR

    compellable = dataclasses.replace(ZAI_VENDOR, forces_tool_choice=True)
    monkeypatch.setattr(ZaiProvider, "vendor", compellable)
    _call(ZaiProvider, schema=_Verdict, tools=_tools())
    assert _FakeResponses.last["tool_choice"] == {
        "type": "function", "name": "emit_structured_response"}


def test_a_non_forcing_vendor_is_told_the_emit_call_is_the_answer():
    """Without compulsion the instruction is the only thing that carries.

    A task prompt written for native structured output may end "do not call any tool", which on
    this route forbids the answer itself: measured 2026-08-24, Qwen then made no emit call at
    all. The instruction goes last so it is the most recent thing the model reads.
    """
    _call(DashscopeProvider, schema=_Verdict, tools=_tools())
    tail = _FakeResponses.last["input"][-1]
    text = json.dumps(tail)
    assert tail["role"] == "user"
    assert "emit_structured_response" in text
    assert "even if you were told not to use tools" in text


def test_a_structured_answer_with_an_extra_tool_call_is_retried_loudly(monkeypatch):
    """Qwen's automatic choice may select both the terminal emitter and a working tool.

    Accepting the emitted arguments would silently end the ReAct trajectory before the requested
    work ran. The adapter marks that mixed response as an off-schema attempt instead.
    """
    class _Mixed(_FakeResponses):
        async def create(self, **kw):
            _FakeResponses.last = kw
            calls = [
                SimpleNamespace(
                    type="function_call", name="emit_structured_response",
                    arguments=json.dumps({"accepted": True, "rationale": "premature"}),
                    call_id="emit1",
                ),
                SimpleNamespace(
                    type="function_call", name="read_file",
                    arguments=json.dumps({"path": "/tmp/x"}), call_id="c1",
                ),
            ]
            return SimpleNamespace(
                output=calls, output_text="",
                usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            )

    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        type("_C", (), {"__init__": lambda self, *a, **k: setattr(self, "responses", _Mixed())}),
    )
    with pytest.raises(ValueError, match="premature"):
        _call(DashscopeProvider, schema=_Verdict, tools=_tools())


def test_a_forcing_vendor_gets_no_extra_instruction(monkeypatch):
    """Compulsion already guarantees the call, so the prompt is left exactly as the caller wrote
    it — an extra turn would change the cached prefix for no benefit. Synthetic descriptor, for
    the reason given in the test above."""
    import dataclasses

    from auto_research.models.providers.zai import ZAI_VENDOR

    monkeypatch.setattr(
        ZaiProvider, "vendor", dataclasses.replace(ZAI_VENDOR, forces_tool_choice=True)
    )
    _call(ZaiProvider, schema=_Verdict, tools=_tools())
    assert "emit_structured_response" not in json.dumps(_FakeResponses.last["input"])





# --------------------------------------------------------------- $ref resolution, per position


def _fmt(model, positions):
    from auto_research.models.providers._responses import _strict_json_schema_format

    return _strict_json_schema_format(model, resolve_refs_in=positions)["schema"]


def test_each_vendor_resolves_only_the_positions_it_was_measured_to_need():
    """Measured 2026-08-24 at 5 draws per cell, every cell 0/5 or 5/5.

    | vendor   | anyOf: [{$ref}, null] | items: {$ref} |
    |----------|-----------------------|---------------|
    | DeepSeek | refused 0/5           | fine 5/5      |
    | Z.AI     | fine 5/5              | fine 5/5      |
    | Qwen     | fine 5/5              | refused 0/5   |

    Qwen is why this is not a boolean: it needs opposite treatment in the two positions.
    """
    assert DeepSeekProvider.vendor.resolve_refs_in == frozenset({"anyOf"})
    assert ZaiProvider.vendor.resolve_refs_in == frozenset(), "Z.AI needs no help at all"
    assert DashscopeProvider.vendor.resolve_refs_in == frozenset({"items"})
    assert OpenAIProvider.vendor.resolve_refs_in == frozenset()


def test_resolving_one_position_leaves_the_other_untouched():
    """The whole point of the per-position policy: resolving is targeted, never blanket.

    On Qwen, resolving `anyOf` turns a working shape into a failing one, so a policy that could
    only be all-or-nothing would be wrong for that vendor whichever way it was set.
    """
    from pydantic import BaseModel

    class Inner(BaseModel):
        label: str

    class Both(BaseModel):
        maybe: Inner | None
        many: list[Inner]

    only_any = _fmt(Both, frozenset({"anyOf"}))
    assert set(only_any["properties"]["many"]["items"]) == {"$ref"}, "items left alone"
    assert any("properties" in b for b in only_any["properties"]["maybe"]["anyOf"])

    only_items = _fmt(Both, frozenset({"items"}))
    assert "properties" in only_items["properties"]["many"]["items"], "items expanded"
    assert {"$ref"} in [set(b) for b in only_items["properties"]["maybe"]["anyOf"]]


def test_no_position_means_the_schema_is_untouched():
    """A vendor that resolves references itself is sent exactly what Pydantic produced."""
    # A schema whose only references sit in `items` (two list[Model] fields).
    from auto_research.schemas import ReviewerReport as ResearchReport

    assert _fmt(ResearchReport, frozenset()) == _fmt(ResearchReport, frozenset())
    assert "$defs" in _fmt(ResearchReport, frozenset())


def test_definitions_survive_only_while_something_points_at_them():
    """Kept for a surviving reference, dropped once every pointer is resolved."""
    # A schema whose only references sit in `items` (two list[Model] fields).
    from auto_research.schemas import ReviewerReport as ResearchReport

    resolved = _fmt(ResearchReport, frozenset({"items"}))
    assert "$ref" not in json.dumps(resolved)
    assert "$defs" not in resolved, "a second unusable copy would be pure token cost"

    partial = _fmt(ResearchReport, frozenset({"anyOf"}))
    assert "$ref" in json.dumps(partial)
    assert "$defs" in partial, "the untouched items refs still need their target"


def test_a_recursive_schema_expands_once_and_keeps_its_target():
    """Inlining leaves one back-reference by design; dropping `$defs` would strand it.

    The naive version also returned the bare pointer `{"$ref": ...}` as the ENTIRE schema,
    because Pydantic puts `$defs` and `$ref` together at the root of a self-referencing model.
    """
    from pydantic import BaseModel

    class Node(BaseModel):
        name: str
        child: "Node | None"

    Node.model_rebuild()
    out = _fmt(Node, frozenset({"anyOf", "items"}))

    assert "properties" in out, "the root must carry the real shape, not a bare pointer"
    assert "name" in out["properties"]
    assert isinstance(out.get("$defs"), dict), "the surviving back-reference needs its target"


def test_a_reference_carrying_siblings_is_left_alone():
    """It already says something a definition would overwrite."""
    from auto_research.models.providers._responses import _dereference

    schema = {"$defs": {"X": {"type": "object", "properties": {"a": {"type": "string"}}}},
              "properties": {"f": {"anyOf": [{"$ref": "#/$defs/X", "description": "keep"},
                                             {"type": "null"}]}}}
    out = _dereference(schema, frozenset({"anyOf"}))
    assert out["properties"]["f"]["anyOf"][0] == {"$ref": "#/$defs/X", "description": "keep"}


def test_a_root_reference_keeps_constraint_siblings():
    """JSON Schema 2020-12 evaluates `$ref` siblings; replacing the root would erase them."""
    from auto_research.models.providers._responses import _dereference

    schema = {
        "$defs": {"X": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "$ref": "#/$defs/X",
        "description": "keep",
        "required": ["a"],
    }
    out = _dereference(schema, frozenset({"anyOf"}))
    assert out["$ref"] == "#/$defs/X"
    assert out["description"] == "keep"
    assert out["required"] == ["a"]


# ── the emit-tool failure modes are not one condition (2026-08-27) ──────────


def test_zai_does_not_claim_its_tool_choice_can_be_pinned():
    """Measured 5 draws of 5 on 2026-08-27: pinned to `emit_structured_response`, Z.AI called
    `read_file` every time. It ignores a named `tool_choice` exactly as it ignores `text.format`,
    so the descriptor must not assert the emit call is compellable -- asserting it cost 14 of 116
    reviews in the eight-reviewer gate_bench swap."""
    from auto_research.models.providers.zai import ZAI_VENDOR

    assert ZAI_VENDOR.schema_via_tool is True
    assert ZAI_VENDOR.forces_tool_choice is False


def test_a_loop_tool_call_instead_of_the_emit_call_is_retryable():
    """The model asking for a tool instead of answering is an unfinished turn, not a dead vendor.

    `roles.py` retries `ValueError` and lets `RuntimeError` end the call, so this distinction is
    what decides whether a recoverable turn is recovered. Before the fix this fell into the
    non-retryable branch and one `read_file` call ended a whole review.
    """
    import pytest

    from auto_research.schemas import ToolReviewVerdict

    vendors = [v for v in responses_vendors() if v.schema_via_tool]
    assert vendors, "no schema_via_tool vendor to exercise"
    for vendor in vendors:
        with pytest.raises(ValueError) as exc:
            _raise_for_emit_outcome(
                vendor, ToolReviewVerdict, emitted=None, tool_call_names=["read_file"]
            )
        assert "read_file" in str(exc.value)
        assert "retried" in str(exc.value)


def test_no_emit_and_no_tool_call_stays_non_retryable():
    """A vendor that returns neither an answer nor anything in progress has declined. A reworded
    retry cannot fix that, so it must stay loud rather than burn the retry budget."""
    import pytest

    from auto_research.schemas import ToolReviewVerdict

    for vendor in [v for v in responses_vendors() if v.schema_via_tool]:
        with pytest.raises(RuntimeError):
            _raise_for_emit_outcome(
                vendor, ToolReviewVerdict, emitted=None, tool_call_names=[]
            )


def _raise_for_emit_outcome(vendor, schema, *, emitted, tool_call_names):
    """Mirror of the adapter's emit-outcome branch, so the three cases are asserted directly.

    Kept as a helper rather than driving a live call: the property under test is which exception
    type each outcome produces, and that is what the role layer's retry keys on.
    """
    from auto_research.models.providers._responses import _EMIT_TOOL

    if emitted is None and tool_call_names:
        raise ValueError(
            f"{vendor.name!r} requested tool calls ({', '.join(tool_call_names)}) instead of "
            f"delivering {schema.__name__} through `{_EMIT_TOOL}`; the turn is unfinished and "
            "will be retried."
        )
    if emitted is None:
        raise RuntimeError(
            f"{vendor.name!r} was asked for {schema.__name__} through a `{_EMIT_TOOL}` call and "
            "returned neither that call nor any tool call."
        )


def test_native_schema_emptiness_is_retryable_and_names_the_status(monkeypatch):
    """An empty native `text.format` reply is retryable, and says enough to be diagnosable.

    DeepSeek documents this as transient ("the API may occasionally return empty content"), and
    deepseek-ai/DeepSeek-V3#1453 reports it specifically after tool results are fed back. So it
    must NOT be a hard failure. It must also not be opaque: empty content, output-budget
    truncation and a refusal are three different fixes, and the generic "no structured output"
    message distinguished none of them.
    """
    import pytest

    class _Empty(_FakeResponses):
        async def create(self, **kw):
            _FakeResponses.last = kw
            return SimpleNamespace(
                output=[], output_text="", status="completed",
                incomplete_details=None,
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            )

    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        type("_C", (), {"__init__": lambda self, *a, **k: setattr(self, "responses", _Empty())}),
    )
    # A native-schema vendor, i.e. one that does NOT ride the emit tool.
    native = [a for a in ADAPTERS if not a.vendor.schema_via_tool]
    assert native, "no native-strict-schema adapter to exercise"
    with pytest.raises(ValueError) as exc:
        _call(native[0], schema=_Verdict)
    message = str(exc.value)
    assert "status=" in message and "incomplete_details=" in message
    assert "output_chars=0" in message
    assert "retrying" in message


# ----------------------------------------------- A1: has_unresolved_server_tool (server tools)


def _hosted_response(*items):
    """A fake Responses object whose `output` is the given items."""
    return SimpleNamespace(
        output=list(items), output_text="", usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )


def _item(itype, **fields):
    return SimpleNamespace(type=itype, **fields)


def _unresolved(adapter: type[ResponsesProvider], *items) -> bool:
    from auto_research.models.providers._responses import _has_unresolved_server_tool

    return _has_unresolved_server_tool(_hosted_response(*items), adapter.vendor)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_a_turn_with_no_server_tools_is_resolved(adapter):
    """The overwhelming majority of real turns: reasoning + function calls, nothing hosted."""
    assert not _unresolved(
        adapter,
        _item("reasoning", id="rs_1"),
        _item("function_call", call_id="c1", name="read_file", arguments="{}"),
    )
    assert not _unresolved(adapter)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.vendor.name)
def test_a_pending_client_function_call_is_not_a_server_tool(adapter):
    """`function_call` is OUR pending work, not the provider's — the loop answers it next turn."""
    assert not _unresolved(adapter, _item("function_call", call_id="c1", status="in_progress"))


def test_the_real_hosted_discovery_pair_is_resolved():
    """The shape 27 of 27 real discovery pairs take, frozen from the production dumps:
    `execution="server"` and `call_id=None` on BOTH items.

    This cell does NOT discriminate count-pairing from id-pairing — with `call_id` null on both
    sides an id-matching predicate also answers False here, by accident. The discrimination lives
    in `test_two_hosted_calls_and_one_output_is_unresolved` below; this one just pins the common
    case against the real bytes.
    """
    call = _item("tool_search_call", id="tsc_1", call_id=None, execution="server",
                 status="completed", arguments={"paths": ["a", "b"]})
    out = _item("tool_search_output", id="tso_1", call_id=None, execution="server",
                status="completed", tools=[{"name": "a"}])
    assert not _unresolved(OpenAIProvider, _item("reasoning", id="rs_1"), call, out)


def test_a_tool_search_call_with_no_output_is_unresolved():
    """Client execution mode: the model emits the call and stops there."""
    call = _item("tool_search_call", id="tsc_1", call_id="tsc_call_1", execution="client",
                 status="completed")
    assert _unresolved(OpenAIProvider, _item("reasoning", id="rs_1"), call)


def test_two_hosted_calls_and_one_output_is_unresolved():
    """THE discriminating cell for count-over-id pairing.

    Hosted mode sets `call_id` to null on every item, so an id-set predicate sees `{None}` on both
    sides, computes an empty difference, and calls this resolved — wrong, and silently so. Counting
    gets it right. Verified against a naive id-set implementation before this was kept: it answers
    False here and True is correct.
    """
    call = lambda: _item("tool_search_call", id="t", call_id=None, status="completed")
    out = _item("tool_search_output", id="o", call_id=None, status="completed")
    assert _unresolved(OpenAIProvider, call(), call(), out) is True
    assert _unresolved(OpenAIProvider, call(), call(), out, out) is False


@pytest.mark.parametrize("status", ["in_progress", "searching", None])
def test_an_inline_hosted_tool_that_has_not_finished_is_unresolved(status):
    """Alibaba folds the result into the call item, so only `status` can say it is pending —
    and an ABSENT status reads pending too, which is the fail-closed direction."""
    fields = {"id": "ws_1", "action": {}}
    if status is not None:
        fields["status"] = status
    assert _unresolved(DashscopeProvider, _item("web_search_call", **fields))


def test_a_completed_inline_hosted_tool_is_resolved():
    assert not _unresolved(
        DashscopeProvider,
        _item("web_search_call", id="ws_1", status="completed", action={"sources": []}),
    )
    assert not _unresolved(
        DashscopeProvider, _item("mcp_call", id="m1", status="completed", output="{}")
    )


def test_a_vendor_that_declares_no_hosted_types_is_always_resolved():
    """Pins that DeepSeek/Z.AI's constant is a DECLARATION, not an accident of the shared walker:
    the same item that fires for Alibaba must not fire for them."""
    pending = _item("web_search_call", id="ws_1", status="in_progress")
    assert _unresolved(DashscopeProvider, pending)
    assert not _unresolved(DeepSeekProvider, pending)
    assert not _unresolved(ZaiProvider, pending)


def test_the_property_reaches_the_model_response():
    """Not just the helper: the value has to survive the adapter's own construction path."""
    from auto_research.models.providers import _responses as R

    response = _hosted_response(
        _item("tool_search_call", id="t1", call_id=None, status="completed")
    )
    built = R._response(raw=response, vendor=OpenAIProvider.vendor)
    assert built.has_unresolved_server_tool is True
    assert R._response(raw=response, vendor=DeepSeekProvider.vendor).has_unresolved_server_tool is False
    # No vendor (a hand-built or synthesized turn) is honestly "nothing captured".
    assert R._response(raw=response).has_unresolved_server_tool is False


# --- the census tripwires: assert the SET, not the constant -------------------------------


def test_every_vendors_hosted_set_is_documented_or_deliberately_empty():
    """A vendor added later inherits the fail-safe default rather than a silent empty set.

    Four of these are a constant False, and a test asserting a constant cannot fail for the
    reason we care about — that a vendor ships a hosted item type we do not classify. This
    asserts the declared SET instead, so a change to it is a deliberate edit with a citation.
    """
    from auto_research.models.providers._responses import _OPENAI_SERVER_TOOL_ITEMS

    declared = {v.name: v.server_tool_item_types for v in responses_vendors()}
    assert declared["openai"] == _OPENAI_SERVER_TOOL_ITEMS
    assert declared["deepseek"] == frozenset()
    assert declared["zai"] == frozenset()
    assert declared["dashscope"] == frozenset({
        "web_search_call", "web_extractor_call", "code_interpreter_call",
        "web_search_image_call", "image_search_call", "file_search_call", "mcp_call",
    })
    # The default is the fail-safe one: a new vendor that declares nothing is assumed capable.
    assert ResponsesVendor(
        name="openai", limits=OpenAIProvider.vendor.limits
    ).server_tool_item_types == _OPENAI_SERVER_TOOL_ITEMS


def test_a_hosted_item_type_we_do_not_classify_is_visible_rather_than_silent():
    """Three of Alibaba's documented types are absent from openai's own input union, so an
    unclassified hosted item rides through capture verbatim. This is the tripwire: the carried
    part keeps its `type`, so a census over captured turns can name it."""
    from auto_research.models.providers._responses import _as_input_item

    item = _item("web_extractor_call", id="we_1", status="in_progress", output="")
    carried = _as_input_item(item)
    assert carried.get("type") == "web_extractor_call"
    # Declared by Alibaba, so it is classified there and fires; unknown to OpenAI's set, so it
    # does not — which is exactly the silence the census test above exists to break.
    assert _unresolved(DashscopeProvider, item)
    assert not _unresolved(OpenAIProvider, item)


# ── the emit call that carries nothing: what the record must say ─────────────
#
# On 2026-09-14 a live reviewer (`qwen3.8-max`) died four times on
# `2 validation errors for ReviewVerdict … input_value={}`, and the run ended. A 12-draw probe
# on a 99.7%-faithful reconstruction could not reproduce it, and the archive refuted the obvious
# size explanation (the same seat succeeded at 186k and 433k input tokens). The reason it stayed
# unexplained is in these lines: the adapter swallowed the raw `arguments` string, so truncated
# JSON and an empty call were the same value by the time anything could look.


def _emitting(arguments, *, status="completed", incomplete=None):
    """A fake vendor whose emit call carries exactly `arguments`."""

    class _R:
        last = None

        async def create(self, **kw):
            _R.last = kw
            item = SimpleNamespace(
                type="function_call", name="emit_structured_response",
                arguments=arguments, call_id="emit1",
            )
            return SimpleNamespace(
                output=[item], output_text="", status=status,
                incomplete_details=incomplete,
                usage=SimpleNamespace(input_tokens=107622, output_tokens=4497),
            )

    class _C:
        def __init__(self, *a, **k):
            self.responses = _R()

    return _C


@pytest.mark.parametrize(
    "arguments,expect",
    [
        ('{"rationale": "x"', "JSONDecodeError"),   # truncated mid-object
        ("", None),                                  # the call carried nothing at all
        ("{}", None),                                # a syntactically fine, empty answer
        ("not json at all", "JSONDecodeError"),
    ],
    ids=["truncated", "empty-string", "empty-object", "prose"],
)
def test_an_empty_emit_answer_reports_what_the_vendor_actually_sent(
    monkeypatch, arguments, expect
):
    """The four shapes are distinguishable in the message, which is the whole point.

    Before this, all four produced `parsed={}` and an identical downstream Pydantic error.
    """
    monkeypatch.setattr("openai.AsyncOpenAI", _emitting(arguments))
    provider = DashscopeProvider()
    with pytest.raises(ValueError) as caught:
        asyncio.run(provider.call(_messages(), _cfg(DashscopeProvider), schema=_Verdict))
    message = str(caught.value)
    assert "_Verdict" in message and "emit_structured_response" in message
    assert f"raw_argument_chars={len(arguments)}" in message
    assert "status='completed'" in message
    if expect is None:
        assert "json_fault=None" in message
    else:
        assert expect in message
    if arguments:
        assert repr(arguments)[1:-1] in message or arguments[:50] in message


def test_the_empty_emit_answer_stays_retryable(monkeypatch):
    """A ValueError, so the role layer's bounded structured retry gets it — the same class as
    the native branch's unparseable answer. A RuntimeError here would end the call outright."""
    monkeypatch.setattr("openai.AsyncOpenAI", _emitting("{}"))
    provider = DashscopeProvider()
    with pytest.raises(ValueError) as caught:
        asyncio.run(provider.call(_messages(), _cfg(DashscopeProvider), schema=_Verdict))
    assert not isinstance(caught.value, RuntimeError)


def test_a_huge_unusable_argument_string_is_capped_in_the_message(monkeypatch):
    """The failing production attempts billed ~4,500 output tokens, so the raw string can be
    large; it rides a ledger event that replays on every later turn, hence the cap."""
    from auto_research.models.providers._responses import _RAW_ARGUMENTS_CAP

    huge = '{"rationale": "' + "x" * 20000
    monkeypatch.setattr("openai.AsyncOpenAI", _emitting(huge))
    with pytest.raises(ValueError) as caught:
        asyncio.run(
            DashscopeProvider().call(_messages(), _cfg(DashscopeProvider), schema=_Verdict)
        )
    message = str(caught.value)
    assert f"raw_argument_chars={len(huge)}" in message
    assert f"[+{len(huge) - _RAW_ARGUMENTS_CAP} chars]" in message
    assert len(message) < _RAW_ARGUMENTS_CAP + 2000


def test_a_populated_emit_answer_is_untouched(monkeypatch):
    """The common path stays exactly as it was — this adds a branch, it does not gate the good
    case behind a new check."""
    monkeypatch.setattr(
        "openai.AsyncOpenAI",
        _emitting(json.dumps({"accepted": True, "rationale": "sound"})),
    )
    response = asyncio.run(
        DashscopeProvider().call(_messages(), _cfg(DashscopeProvider), schema=_Verdict)
    )
    assert response.parsed == {"accepted": True, "rationale": "sound"}

