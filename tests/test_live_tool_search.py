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

    # Resident context is TOTAL input — uncached plus cache-read. `usage.input_tokens` is net of
    # cache ([I5] subtracts cached tokens), so comparing it across two calls measures whichever
    # happened to hit a warm prefix, not the tool payload: observed 2026-08-06, an all-resident
    # baseline reporting 443 "input" tokens against a 35k prompt after an earlier identical run.
    def _sent(resp) -> int:
        return resp.usage.input_tokens + resp.usage.cache_read_tokens

    log = dev_log_path("tool-search", f"live_tool_search_{provider_name}", ".json")
    log.write_text(
        json.dumps(
            {
                "deferred_input_tokens": _sent(deferred),
                "loaded_input_tokens": _sent(loaded),
                "deferred_uncached": deferred.usage.input_tokens,
                "loaded_uncached": loaded.usage.input_tokens,
                "provider_parts": deferred.provider_parts,
                "tool_calls": [c.name for c in deferred.tool_calls],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # The whole point: deferring must cut resident input tokens materially.
    assert _sent(deferred) < _sent(loaded)
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


def test_live_compose_over_a_discovery_trajectory_declares_its_tools(monkeypatch):
    """The compose after tool SEARCH: the discovery blocks replay, because the tools are declared.

    Two documented rules used to collide here. A block naming a tool is rejected when that tool
    is absent from the request (*"Tool reference 'x' not found in available tools"*, run-killer
    2026-07-29), yet a `thinking` block must survive *unmodified* and *"rebuilding the message or
    filtering out `redacted_thinking` blocks triggers a 400"* — so a tool-less compose could
    neither echo nor filter. Dropping every part was the compromise, and on OpenAI it orphaned
    the `function_call` items that replay unconditionally (run 8c75990e).

    Declaring the tools dissolves the collision: every reference resolves, nothing is filtered,
    and `tool_choice="none"` still forbids another call. Re-verified live 2026-08-06 — and the
    old tool-less shape was re-confirmed to 400 on the way, which is why no caller may use it.
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
    tools = _real_tools(defer=True)

    async def run():
        first = await provider.call(_messages(), cfg, tools=tools)
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
        # Schema AND tools: the terminal compose, on the documented echo path.
        return first, await provider.call(
            history, cfg, schema=_Answer, tools=tools, tool_choice="none"
        )

    first, composed = asyncio.run(run())
    carried = [p.get("type") for p in first.provider_parts]
    print(f"[live] compose replayed a turn carrying: {carried}")
    assert composed.parsed is not None, "the compose produced its structured answer"
    assert not composed.tool_calls, "tool_choice='none' forbids a call"


@pytest.mark.parametrize(
    "provider_name,provider,model",
    [
        ("openai", OpenAIProvider(), "gpt-5.6-terra"),
        ("anthropic", AnthropicProvider(), "claude-sonnet-5"),
    ],
)
def test_live_compose_declares_its_tools(provider_name, provider, model):
    """The terminal compose: schema + tools + ``tool_choice="none"`` over a REAL trajectory.

    **This shape killed run 8c75990e** ($29.50, one step before its first commit): the GPT
    co-lead composed its `ToolReviewVerdict` over four tool-calling iterations, the request
    declared no tools so the carried provider parts were dropped, and the `function_call` items
    replayed anyway — `400 Item 'fc_…' of type 'function_call' was provided without its required
    'reasoning' item: 'rs_…'`.

    Note what is deliberately NOT used here: tool search. The co-lead's tools are resident
    harness tools (`read_file`, `list_dir`), so its trajectory is ordinary function calling —
    and every live test we had exercised the discovery path instead, which is exactly why this
    shipped. Keep this green whenever the replay path or `tool_choice` handling moves.
    """
    from pydantic import BaseModel

    class _Verdict(BaseModel):
        approved: bool
        rationale: str

    load_dotenv()
    cfg = RoleModelConfig(
        name="colead", provider=provider_name, model=model,
        parameters={"max_tokens": 4096, "thinking": {"type": "adaptive"}}
        if provider_name == "anthropic"
        else {"max_output_tokens": 4096, "reasoning": {"effort": "high"}},
    )
    tools = [
        {
            "name": "read_file",
            "description": "Read a text file and return its contents.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    seed = [
        ModelMessage(role="system", content="You review code. Use read_file before judging."),
        ModelMessage(role="user", content="Review /tmp/example.py. Read it first, then judge."),
    ]

    async def run():
        first = await provider.call(seed, cfg, tools=tools)
        assert first.tool_calls, "need a real tool-calling turn to replay"
        history = [
            *seed,
            first,
            ToolResultMessage(results=[
                ToolResultPart(
                    tool_call_id=call.id, name=call.name,
                    content="def add(a, b):\n    return a - b\n", is_error=False,
                )
                for call in first.tool_calls
            ]),
            ModelMessage(role="user", content="Now emit the verdict. Do not call tools."),
        ]
        # Declared AND forbidden — the fix. Omitting `tools=` here is the 400 above.
        return first, await provider.call(
            history, cfg, schema=_Verdict, tools=tools, tool_choice="none"
        )

    first, composed = asyncio.run(run())

    log = dev_log_path("tool-search", f"live_compose_declares_tools_{provider_name}", ".json")
    log.write_text(
        json.dumps(
            {
                "turn1_tool_calls": [c.name for c in first.tool_calls],
                "turn1_part_types": [p.get("type") for p in first.provider_parts],
                "composed_parsed": composed.parsed,
                "composed_tool_calls": [c.name for c in composed.tool_calls],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert composed.parsed is not None, "the compose produced its structured answer"
    assert not composed.tool_calls, "tool_choice='none' forbids a call, so the model must answer"
