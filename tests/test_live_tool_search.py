"""Opt-in live check for provider-native tool search / deferred tool loading.

Exercises the real endpoints with the REAL ``research_toolkit`` manifest: every registry tool
deferred, the harness tool resident, and a question whose answer needs a deferred tool. Asserts
the model searched, the discovered tool became callable, and — the point of the whole exercise —
that resident input tokens dropped well below the all-loaded baseline.

**NOT LIVE TESTED as of 2026-07-27.** Run once provider usage is available::

    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_tool_search.py -s

The Anthropic half additionally needs a valid ``ANTHROPIC_API_KEY`` (401 as of 2026-07-27).
Flip ``tool_search_enabled: true`` in ``config/orchestration.yaml`` only after this passes.
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
from auto_research.models.types import ModelMessage, RoleModelConfig
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
                "provider_blocks": deferred.provider_blocks,
                "tool_calls": [c.name for c in deferred.tool_calls],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # The whole point: deferring must cut resident input tokens materially.
    assert deferred.usage.input_tokens < loaded.usage.input_tokens
    # ...and the model must actually reach the tool through search.
    assert deferred.provider_blocks or deferred.tool_calls, (
        "expected a tool-search discovery or a direct tool call"
    )
