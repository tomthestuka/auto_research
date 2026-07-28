"""Opt-in live check for Anthropic provider-native context editing (the context-pressure
valve). Exercises the real ``client.beta.messages`` path with a deliberately low token
trigger so clearing fires on a small trajectory, and asserts the applied-edits report comes
back on ``ModelResponse.context_edits``.

**PASSED 2026-07-28**, after two false starts (a 'credit balance too low' 400 on 2026-07-22,
then an invalid API key through 2026-07-27). ``context_mgmt_enabled`` was flipped true on the
strength of this run; re-run it whenever the beta identifiers move::

    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_context_editing.py -s
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from auto_research.env import ContextEditingConfig
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.types import (
    AssistantToolUse,
    ModelMessage,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)

_LOW_TRIGGER = ContextEditingConfig(
    enabled=True,
    beta_header="context-management-2025-06-27",
    strategy="clear_tool_uses_20250919",
    trigger_tokens=1000,        # deliberately low so a small trajectory forces a clear
    clear_at_least_tokens=100,
    keep_tool_uses=1,
)
_FILLER = "The observed count rate was steady across the interval. " * 60


class _Answer(BaseModel):
    answer: str


def _cfg() -> RoleModelConfig:
    return RoleModelConfig(
        name="worker",
        provider="anthropic",
        model="claude-sonnet-5",
        vision=False,
        parameters={"thinking": {"type": "adaptive"}, "max_tokens": 4096},
    )


def _trajectory():
    msgs: list = [
        ModelMessage(role="system", content="You are a terse analysis assistant."),
        ModelMessage(role="user", content="Summarize the reduction steps taken so far."),
    ]
    for i in range(3):
        msgs.append(
            AssistantToolUse(
                content=f"Calling step {i}.",
                tool_calls=[ToolCall(id=f"call_{i}", name="echo", arguments={"x": f"step {i}"})],
            )
        )
        msgs.append(
            ToolResultMessage(
                results=[ToolResultPart(
                    tool_call_id=f"call_{i}", name="echo", content=f"Step {i}. {_FILLER}"
                )]
            )
        )
    return msgs


def test_live_context_editing_clears_and_reports(monkeypatch):
    load_dotenv()
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.context_editing_config", lambda: _LOW_TRIGGER
    )
    tools = [{
        "name": "echo",
        "description": "Echo the input.",
        "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    }]
    log = dev_log_path("context-editing", "live_context_editing", ".jsonl")

    async def run():
        prov = AnthropicProvider()
        create_resp = await prov.call(_trajectory(), _cfg(), tools=tools, cache_prefix=True)
        parse_resp = await prov.call(
            [ModelMessage(role="system", content="Be terse."),
             ModelMessage(role="user", content="Return {answer: ok}.")],
            _cfg(), schema=_Answer, cache_prefix=True,
        )
        return create_resp, parse_resp

    create_resp, parse_resp = asyncio.run(run())
    with log.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "create_context_edits": create_resp.context_edits,
            "create_usage": create_resp.usage.model_dump(),
            "parse_parsed": parse_resp.parsed,
            "parse_context_edits": parse_resp.context_edits,
        }, indent=2))

    # The forced-low trigger over a filler trajectory must have cleared ≥1 tool result.
    assert create_resp.context_edits, "context editing reported no cleared tool results"
    assert create_resp.context_edits[0].get("cleared_tool_uses", 0) >= 1
    # Structured output on the beta namespace still returns a parsed object.
    assert parse_resp.parsed is not None
