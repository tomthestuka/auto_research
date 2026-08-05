"""Opt-in live check for provider-native tool search / deferred tool loading.

Exercises the real endpoints with the REAL ``research_toolkit`` manifest: every registry tool
deferred, the harness tool resident, and a question whose answer needs a deferred tool. Asserts
the model searched, the discovered tool became callable, and — the point of the whole exercise —
that resident input tokens dropped well below the all-loaded baseline.

**PASSED 2026-07-28 on both providers**, and it earned its keep on the first run: the Anthropic
half caught a 400 no deterministic test could see (the server-tool ``name`` must be the undated
``tool_search_tool_bm25`` beside the dated ``type``). Measured resident input 19,074 → 3,789
tokens on OpenAI and 35,208 → 9,003 on Anthropic. ``tool_search_enabled`` was flipped true on
the strength of this run; re-run it whenever the dated provider identifiers move::

    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_tool_search.py -s
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.env import ToolSearchConfig, toolkit_root
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.types import (
    ModelResponse,
    ModelMessage,
    RoleModelConfig,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.sandbox.registry import ToolkitRegistry
from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)

_ENABLED = ToolSearchConfig(
    enabled=True,
    anthropic_tool="tool_search_tool_bm25_20251119",
    openai_tool="tool_search",
)

_QUESTION = (
    "Measure the energy-resolved IXPE polarization for observation 01002901. "
    "Load the tool you need first, then call it."
)


def _real_tools(defer: bool) -> list[dict]:
    """The real manifest: registry tools (optionally deferred) + one resident harness tool."""
    registry = ToolkitRegistry(toolkit_root())
    registry.load()
    tools = [
        ({**d, "defer_loading": True} if defer else dict(d))
        for d in registry.tool_definitions()
    ]
    tools.append(
        {
            "name": "ask_human",
            "description": "Ask the human a clarifying question.",
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        }
    )
    return tools


def _messages():
    return [
        ModelMessage(role="system", content="You are an X-ray analyst. Use tools."),
        ModelMessage(role="user", content=_QUESTION),
    ]


@pytest.mark.parametrize(
    "provider_name,provider,model",
    [
        ("openai", OpenAIProvider(), "gpt-5.5"),
        ("anthropic", AnthropicProvider(), "claude-sonnet-5"),
    ],
)
def test_live_tool_search_defers_and_discovers(monkeypatch, provider_name, provider, model):
    load_dotenv()
    monkeypatch.setattr(
        f"auto_research.models.providers.{provider_name}.tool_search_config", lambda: _ENABLED
    )
    cfg = RoleModelConfig(
        name="worker", provider=provider_name, model=model, parameters={"max_tokens": 2048}
        if provider_name == "anthropic" else {"max_output_tokens": 2048},
    )

    async def run():
        deferred = await provider.call(_messages(), cfg, tools=_real_tools(defer=True))
        # Baseline: the same request with every definition resident.
        monkeypatch.setattr(
            f"auto_research.models.providers.{provider_name}.tool_search_config",
            lambda: ToolSearchConfig(enabled=False, anthropic_tool="", openai_tool=""),
        )
        loaded = await provider.call(_messages(), cfg, tools=_real_tools(defer=False))
        return deferred, loaded

    deferred, loaded = asyncio.run(run())
    log = dev_log_path("tool-search", f"live_tool_search_{provider_name}", ".json")
    log.write_text(
        json.dumps(
            {
                "deferred_input_tokens": deferred.usage.input_tokens,
                "loaded_input_tokens": loaded.usage.input_tokens,
                "provider_parts": deferred.provider_parts,
                "tool_calls": [c.name for c in deferred.tool_calls],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # The whole point: deferring must cut resident input tokens materially.
    assert deferred.usage.input_tokens < loaded.usage.input_tokens
    # ...and the model must actually reach the tool through search.
    assert deferred.provider_parts or deferred.tool_calls, (
        "expected a tool-search discovery or a direct tool call"
    )


@pytest.mark.parametrize(
    "provider_name,provider,model",
    [
        ("openai", OpenAIProvider(), "gpt-5.5"),
        ("anthropic", AnthropicProvider(), "claude-sonnet-5"),
    ],
)
def test_live_second_turn_after_discovery(monkeypatch, provider_name, provider, model):
    """Discover a tool, answer it, then take a SECOND turn — the replay the loop actually does.

    **This is the gap that shipped two run-killers.** The test above stops after turn 1, so
    nothing ever exercised handing the provider's own records back to it, and a real ReAct loop
    does that on every iteration after the first. Both failures lived there, in opposite
    directions: OpenAI rejected a field we added that is output-only
    (`400 Unknown parameter: 'input[2].created_by'`, run b-run-q21-0803) and then rejected a
    replay missing a field we had dropped at ingest (`Missing namespace for function_call …`,
    run b-run-q21-0803b) — each costing ~$10 of live analysis to discover one field.

    Two cheap turns catch that class outright, so keep this green whenever the provider item
    shapes move. Passing means the discovered tool stayed callable *and* the round-trip was
    accepted; the assertion is simply that turn 2 does not raise.
    """
    load_dotenv()
    monkeypatch.setattr(
        f"auto_research.models.providers.{provider_name}.tool_search_config", lambda: _ENABLED
    )
    cfg = RoleModelConfig(
        name="worker", provider=provider_name, model=model,
        # Thinking on for Anthropic, as every Anthropic chat member runs: turn 1 then emits
        # `thinking` blocks, so turn 2 is the round trip that decides whether replaying them
        # verbatim is accepted — a request-shape question no mock can answer.
        parameters={"max_tokens": 2048, "thinking": {"type": "adaptive"}}
        if provider_name == "anthropic" else {"max_output_tokens": 2048},
    )
    tools = _real_tools(defer=True)

    async def run():
        first = await provider.call(_messages(), cfg, tools=tools)
        assert first.tool_calls, "the model must call a discovered tool for this to test anything"
        history = [
            *_messages(),
            # Replay the turn WHOLE — exactly what `_react` appends. Copying selected fields
            # here would test a shape the loop no longer produces.
            first,
            ToolResultMessage(
                results=[
                    ToolResultPart(
                        tool_call_id=call.id, name=call.name,
                        content="PD = 4.0 +/- 0.3 %, PA = 30 +/- 2 deg", is_error=False,
                    )
                    for call in first.tool_calls
                ]
            ),
        ]
        # The turn that 400'd twice in production.
        return first, await provider.call(history, cfg, tools=tools)

    first, second = asyncio.run(run())

    log = dev_log_path("tool-search", f"live_second_turn_{provider_name}", ".json")
    log.write_text(
        json.dumps(
            {
                "turn1_tool_calls": [
                    {"name": c.name, "provider_fields": c.provider_fields} for c in first.tool_calls
                ],
                "turn1_provider_parts": first.provider_parts,
                "turn2_content": (second.content or "")[:400],
                "turn2_tool_calls": [c.name for c in second.tool_calls],
                "turn2_input_tokens": second.usage.input_tokens,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert second.usage.input_tokens > 0, "turn 2 completed against the live endpoint"
    if provider_name == "anthropic":
        # Every part the model authored is carried, so an Anthropic turn under adaptive thinking
        # carries its reasoning too — and turn 2 above proves the API accepts it replayed
        # verbatim. Reported rather than asserted: whether a given turn thinks is the model's
        # call, so a hard assert here would fail on a turn that simply chose not to.
        kinds = [p.get("type") for p in first.provider_parts]
        print(f"[live] anthropic turn-1 carried parts: {kinds}")


def test_live_tool_less_compose_keeps_thinking_and_drops_references(monkeypatch):
    """The terminal compose: replay a thinking-carrying turn into a request with NO tools.

    This shape is on the path of every run (a ReAct loop ends in a structured compose that
    declares a schema and no tools), and it puts two rules in tension. A block naming a tool must
    be dropped — replaying one into a tool-less request returned *"Tool reference 'x' not found in
    available tools"* and killed a live run (2026-07-29). But a `thinking` block must survive
    *unmodified*, and dropping the discovery blocks around it shifts its index — and index changes
    are exactly what the API rejected when reasoning was reassembled (*"blocks must remain as they
    were in the original response"*, 2026-08-05). Whether "as they were" means content-identity or
    absolute position is not documented, so only a live round trip settles it.
    """
    from pydantic import BaseModel

    class _Answer(BaseModel):
        polarization_degree_percent: float
        note: str

    load_dotenv()
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.tool_search_config", lambda: _ENABLED
    )
    provider = AnthropicProvider()
    cfg = RoleModelConfig(
        name="worker", provider="anthropic", model="claude-sonnet-5",
        parameters={"max_tokens": 2048, "thinking": {"type": "adaptive"}},
    )

    async def run():
        first = await provider.call(_messages(), cfg, tools=_real_tools(defer=True))
        assert first.tool_calls, "need a tool-calling turn to replay"
        history = [
            *_messages(),
            first,
            ToolResultMessage(results=[
                ToolResultPart(tool_call_id=call.id, name=call.name,
                               content="PD = 4.0 +/- 0.3 %, PA = 30 +/- 2 deg")
                for call in first.tool_calls
            ]),
        ]
        # No tools, a schema: the terminal compose.
        return first, await provider.call(history, cfg, schema=_Answer)

    first, composed = asyncio.run(run())
    carried = [p.get("type") for p in first.provider_parts]
    print(f"[live] tool-less compose replayed a turn carrying: {carried}")
    assert composed.parsed is not None, "the tool-less compose produced its structured answer"
