"""Opt-in live check: EVERY structured schema we can send, on every provider, WITH tools.

**Why this exists.** Every earlier live test of the structured path sent a hand-made two-field
toy schema. The real payloads are an order of magnitude larger — `AnalystReport` carries figure
citations, `AuthoringDecision` an ordered list of nested `ToolSpec`s — and each provider's strict
mode rejects schema *shapes*, not sizes: `format: path`, `default` values, unions, deep `$defs`.
A rejection is a 400 at request time, so it costs one cheap call per schema to know.

Since 2026-08-06 a terminal compose also declares its tools (`tool_choice="none"`), so every one
of these schemas now travels **alongside a tools array** — a combination no test had ever sent.
That is the pairing under test here: schema × provider × tools-declared.

    AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_schema_matrix.py -s
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.agents.roles import _ClarifyReply
from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.types import ModelMessage, RoleModelConfig
from auto_research.schemas import (
    AnalystReport,
    AuthoringDecision,
    LatexReport,
    ResearchReport,
    ReviewVerdict,
    SearchRequest,
    ToolReviewVerdict,
)
from auto_research.storage import MemoryRecord
from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)

# Every schema reachable from `roles.py`; the five marked COMPOSE also ride the terminal
# compose, which is the path that now declares tools.
SCHEMAS = [
    ("ReviewVerdict", ReviewVerdict),          # COMPOSE (reviewer.review)
    ("AnalystReport", AnalystReport),          # COMPOSE (analyze / adjudicate / finalize)
    ("AuthoringDecision", AuthoringDecision),  # COMPOSE (lead.decide_authoring)
    ("ToolReviewVerdict", ToolReviewVerdict),  # COMPOSE (colead.review_tool)
    ("_ClarifyReply", _ClarifyReply),          # COMPOSE (lead.clarify_for_author)
    ("SearchRequest", SearchRequest),
    ("MemoryRecord", MemoryRecord),
    ("ResearchReport", ResearchReport),
    ("LatexReport", LatexReport),
]

PROVIDERS = [
    ("openai", OpenAIProvider(), "gpt-5.6-terra"),
    ("anthropic", AnthropicProvider(), "claude-sonnet-5"),
]

_TOOLS = [
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


def _config(provider_name: str, model: str) -> RoleModelConfig:
    return RoleModelConfig(
        name="probe",
        provider=provider_name,
        model=model,
        vision=True,
        documents=True,
        parameters={"max_tokens": 8192}
        if provider_name == "anthropic"
        else {"max_output_tokens": 8192},
    )


@pytest.mark.parametrize("provider_name,provider,model", PROVIDERS)
@pytest.mark.parametrize("schema_name,schema", SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_live_schema_is_accepted_with_tools_declared(
    provider_name, provider, model, schema_name, schema
):
    """The request is accepted and a valid instance comes back, with tools on the request."""
    load_dotenv()
    messages = [
        ModelMessage(
            role="system",
            content="You emit structured results for an X-ray astronomy research pipeline.",
        ),
        ModelMessage(
            role="user",
            content=(
                f"Emit a MINIMAL but schema-valid {schema_name}. Use short placeholder-free "
                "strings drawn from this situation: an IXPE polarization analysis of Cyg X-1 "
                "that produced a null result. Keep every list to at most one element. Do not "
                "call any tool."
            ),
        ),
    ]

    async def run():
        return await provider.call(
            messages,
            _config(provider_name, model),
            schema=schema,
            tools=_TOOLS,
            tool_choice="none",
        )

    response = asyncio.run(run())

    log = dev_log_path("schema-matrix", f"{provider_name}_{schema_name}", ".json")
    log.write_text(
        json.dumps(
            {
                "schema": schema_name,
                "provider": provider_name,
                "model": model,
                "parsed": response.parsed,
                "tool_calls": [c.name for c in response.tool_calls],
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    assert not response.tool_calls, "tool_choice='none' forbids a call"
    assert response.parsed is not None, (
        f"{provider_name} returned no structured output for {schema_name}"
    )
    # The strong claim: what came back actually validates as the model we asked for.
    schema.model_validate(response.parsed)
