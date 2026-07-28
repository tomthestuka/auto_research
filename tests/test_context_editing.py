"""Deterministic tests for Anthropic provider-native context editing (the context-pressure
valve). These exercise config parsing, request construction on the beta namespace, the
edit-report extraction, and the monitor event — all with a mocked SDK, no tokens spent.

The live round-trip (does the real beta endpoint accept the block and clear as configured)
is in ``tests/test_live_context_editing.py`` — **passed 2026-07-28**.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from auto_research.env import ContextEditingConfig, context_editing_config
from auto_research.models.providers.anthropic import AnthropicProvider, _context_edits
from auto_research.models.types import ModelMessage, ModelResponse, RoleModelConfig
from auto_research.agents.roles import AgentRole


class _Verdict(BaseModel):
    accepted: bool
    rationale: str


def _cfg() -> RoleModelConfig:
    return RoleModelConfig(name="m", provider="anthropic", model="fake-model")


def _messages():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="hi")]


def _enabled() -> ContextEditingConfig:
    return ContextEditingConfig(
        enabled=True,
        beta_header="context-management-2025-06-27",
        strategy="clear_tool_uses_20250919",
        trigger_tokens=120000,
        clear_at_least_tokens=20000,
        keep_tool_uses=3,
    )


class _FakeEdit:
    """A stand-in for a BetaClearToolUses…EditResponse (only ``model_dump`` is used)."""

    def __init__(self, **d):
        self._d = d

    def model_dump(self, mode=None):
        return dict(self._d)


class _FakeMessages:
    """Records the namespace ('std' vs 'beta') and kwargs of the last parse/create."""

    def __init__(self, store: dict, namespace: str):
        self._store = store
        self._ns = namespace

    def _report(self, request):
        # A context-management block on the request → the SDK returns an applied-edits report.
        if "context_management" in request:
            return SimpleNamespace(
                applied_edits=[
                    _FakeEdit(
                        type="clear_tool_uses_20250919",
                        cleared_tool_uses=2,
                        cleared_input_tokens=1234,
                    )
                ]
            )
        return None

    async def parse(self, **request):
        self._store["parse"] = {"ns": self._ns, "request": request}
        instance = request["output_format"](accepted=True, rationale="ok")
        return SimpleNamespace(
            parsed_output=instance,
            content=[SimpleNamespace(type="text", text='{"accepted": true, "rationale": "ok"}')],
            usage=SimpleNamespace(
                input_tokens=11, output_tokens=7,
                cache_read_input_tokens=5, cache_creation_input_tokens=0,
            ),
            context_management=self._report(request),
        )

    async def create(self, **request):
        self._store["create"] = {"ns": self._ns, "request": request}
        name = request["tools"][0]["name"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id="tu_1", name=name, input={"x": "y"})],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            context_management=self._report(request),
        )


class _FakeAnthropic:
    store: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    @property
    def messages(self):
        return _FakeMessages(_FakeAnthropic.store, "std")

    @property
    def beta(self):
        return SimpleNamespace(messages=_FakeMessages(_FakeAnthropic.store, "beta"))


@pytest.fixture(autouse=True)
def _reset_store():
    _FakeAnthropic.store = {}
    yield


# ── config ──────────────────────────────────────────────────────────────────

def test_context_editing_config_defaults(monkeypatch):
    monkeypatch.setattr("auto_research.env._orch_config", lambda: {})
    cfg = context_editing_config()
    assert cfg.enabled is False  # a config lacking the key keeps the non-beta path
    assert cfg.strategy == "clear_tool_uses_20250919"
    assert cfg.trigger_tokens == 120000
    assert cfg.keep_tool_uses == 3


def test_context_editing_config_reads_keys(monkeypatch):
    monkeypatch.setattr(
        "auto_research.env._orch_config",
        lambda: {
            "context_mgmt_enabled": True,
            "context_mgmt_trigger_tokens": 50000,
            "context_mgmt_clear_at_least_tokens": 8000,
            "context_mgmt_keep_tool_uses": 1,
        },
    )
    cfg = context_editing_config()
    assert cfg.enabled is True
    assert cfg.trigger_tokens == 50000
    assert cfg.clear_at_least_tokens == 8000
    assert cfg.keep_tool_uses == 1


# ── adapter request construction ──────────────────────────────────────────────

def test_context_editing_attaches_block_on_beta_when_enabled(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.context_editing_config", _enabled
    )
    tools = [{"name": "echo", "description": "e", "input_schema": {"type": "object"}}]
    resp = asyncio.run(
        AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=True)
    )
    rec = _FakeAnthropic.store["create"]
    assert rec["ns"] == "beta"  # routed to client.beta.messages
    assert rec["request"]["betas"] == ["context-management-2025-06-27"]
    edit = rec["request"]["context_management"]["edits"][0]
    assert edit["type"] == "clear_tool_uses_20250919"
    assert edit["trigger"] == {"type": "input_tokens", "value": 120000}
    assert edit["keep"] == {"type": "tool_uses", "value": 3}
    assert edit["clear_at_least"] == {"type": "input_tokens", "value": 20000}
    # The applied-edits report is normalized onto the neutral response.
    assert resp.context_edits == [
        {"type": "clear_tool_uses_20250919", "cleared_tool_uses": 2, "cleared_input_tokens": 1234}
    ]


def test_context_editing_on_parse_path_when_enabled(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.context_editing_config", _enabled
    )
    resp = asyncio.run(
        AnthropicProvider().call(_messages(), _cfg(), schema=_Verdict, cache_prefix=True)
    )
    rec = _FakeAnthropic.store["parse"]
    assert rec["ns"] == "beta"
    assert "context_management" in rec["request"]
    assert resp.parsed == {"accepted": True, "rationale": "ok"}  # parsed_output still works on beta
    assert resp.context_edits and resp.context_edits[0]["cleared_tool_uses"] == 2


def test_no_context_editing_when_disabled(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    # Default config: disabled. Even with cache_prefix, stay on the non-beta namespace.
    monkeypatch.setattr("auto_research.env._orch_config", lambda: {})
    tools = [{"name": "echo", "description": "e", "input_schema": {"type": "object"}}]
    resp = asyncio.run(
        AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=True)
    )
    rec = _FakeAnthropic.store["create"]
    assert rec["ns"] == "std"  # untouched non-beta path
    assert "context_management" not in rec["request"]
    assert "betas" not in rec["request"]
    assert resp.context_edits == []


def test_no_context_editing_without_cache_prefix(monkeypatch):
    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAnthropic)
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.context_editing_config", _enabled
    )
    tools = [{"name": "echo", "description": "e", "input_schema": {"type": "object"}}]
    asyncio.run(AnthropicProvider().call(_messages(), _cfg(), tools=tools, cache_prefix=False))
    rec = _FakeAnthropic.store["create"]
    assert rec["ns"] == "std"  # short single-shot calls never route to the beta editing path
    assert "context_management" not in rec["request"]


# ── extraction + monitor event ───────────────────────────────────────────────

def test_context_edits_extracts_applied_edits():
    resp = SimpleNamespace(
        context_management=SimpleNamespace(
            applied_edits=[_FakeEdit(type="clear_tool_uses_20250919", cleared_tool_uses=3)]
        )
    )
    assert _context_edits(resp) == [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 3}]


def test_context_edits_empty_when_absent():
    assert _context_edits(SimpleNamespace()) == []
    assert _context_edits(SimpleNamespace(context_management=None)) == []
    assert _context_edits(SimpleNamespace(context_management=SimpleNamespace(applied_edits=[]))) == []


class _FakeMonitor:
    def __init__(self):
        self.events: list = []

    async def record(self, team, event, *, level, summary, actor=None, data=None):
        self.events.append(SimpleNamespace(event=event, level=level, summary=summary, data=data))


def _bare_role(role_name: str = "analyst_worker") -> AgentRole:
    inst = object.__new__(AgentRole)
    inst.role = role_name
    inst.monitor = _FakeMonitor()
    inst.model = None
    return inst


def test_record_context_edit_emits_event_with_counts():
    role = _bare_role()
    resp = ModelResponse(
        context_edits=[
            {"type": "clear_tool_uses_20250919", "cleared_tool_uses": 2, "cleared_input_tokens": 1234}
        ]
    )
    asyncio.run(role._record_context_edit(resp, "analyze"))
    assert len(role.monitor.events) == 1
    evt = role.monitor.events[0]
    assert evt.event == "analyst_worker.analyze.context_edit"
    assert "2 tool result" in evt.summary and "1234" in evt.summary


def test_record_context_edit_noop_when_empty():
    role = _bare_role()
    asyncio.run(role._record_context_edit(ModelResponse(), "analyze"))
    assert role.monitor.events == []
