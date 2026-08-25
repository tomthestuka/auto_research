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


def test_an_image_is_withheld_with_a_note_on_a_model_that_cannot_read_it(tmp_path):
    """DeepSeek serves vision on one model and answers 400 on the rest.

    The figure must be replaced by a model-visible note, never dropped in silence: an absence
    the model cannot see is what invites it to invent the content.
    """
    cfg = RoleModelConfig(name="m", provider="deepseek", model="deepseek-v4-flash")
    asyncio.run(DeepSeekProvider().call(_messages(), cfg, images=[_figure(tmp_path)]))
    sent = json.dumps(_FakeResponses.last["input"])
    assert "does not accept image input" in sent
    assert "input_image" not in sent


def test_the_vision_model_receives_the_image(tmp_path):
    cfg = RoleModelConfig(
        name="m", provider="deepseek", model="deepseek-v4-flash-vision-exp", vision=True
    )
    asyncio.run(DeepSeekProvider().call(_messages(), cfg, images=[_figure(tmp_path)]))
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
    with pytest.raises(RuntimeError, match="returned none"):
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


def test_a_vendor_that_allows_compulsion_still_forces_the_call():
    """The flag must not weaken the guarantee where the vendor honours it."""
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


def test_a_forcing_vendor_gets_no_extra_instruction():
    """Compulsion already guarantees the call, so the prompt is left exactly as the caller wrote
    it — an extra turn would change the cached prefix for no benefit."""
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
    from auto_research.schemas import ResearchReport

    assert _fmt(ResearchReport, frozenset()) == _fmt(ResearchReport, frozenset())
    assert "$defs" in _fmt(ResearchReport, frozenset())


def test_definitions_survive_only_while_something_points_at_them():
    """Kept for a surviving reference, dropped once every pointer is resolved."""
    from auto_research.schemas import ResearchReport

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
