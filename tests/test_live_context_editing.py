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
    ModelResponse,
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
            ModelResponse(
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


# ── B5 (2026-10-18): the retuned edit — inputs cleared, report/channel/tool-search uses kept ──
#
#     AUTO_RESEARCH_LIVE_MODEL_TESTS=1 pytest tests/test_live_context_editing.py -k retuned -s
#
# What it measures, on the configured worker model (claude-sonnet-5, ~4 calls, est. < $0.30):
#   1. the model discovers a deferred tool through tool search and calls it (a real discovery
#      block in the history);
#   2. a request over a deliberately low trigger clears tool uses with `clear_tool_inputs` while
#      `exclude_tools` names the report and channel tools and the search tool;
#   3. the discovered tool is still callable after the clear (tool search intact);
#   4. whether a cleared result that carried an image is counted in `cleared_tool_uses`;
#   5. cache writes on the call after the clear (the next call should read the cleared prefix).


def _figure(tmp_path):
    import hashlib
    from datetime import UTC, datetime

    from PIL import Image

    from auto_research.schemas import FigureRef

    path = tmp_path / "probe.png"
    Image.new("RGB", (600, 400), (30, 90, 160)).save(path)
    return FigureRef(path=path, caption="probe figure", mime_type="image/png", produced_by="echo",
                     created_at=datetime.now(UTC),
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _probe_tools():
    return [
        {"name": "echo", "description": "Echo the input text.",
         "input_schema": {"type": "object", "properties": {"x": {"type": "string"}},
                          "required": ["x"]}},
        {"name": "edit_report", "description": "Save the report draft.",
         "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                          "required": ["summary"]}},
        {"name": "message", "description": "Send a text to a peer.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"]}},
        {"name": "fit_spectrum", "description": "Fit a spectrum and return the best-fit index.",
         "defer_loading": True,
         "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}},
                          "required": ["x"]}},
    ]


def _calls_fit_without_searching(response) -> bool:
    """The model called fit_spectrum in this turn with no new tool search before it: the earlier
    discovery is what made the tool callable (a fresh search would hide a cleared discovery)."""
    for part in response.provider_parts:
        if part.get("type") == "server_tool_use":
            return False
        if part.get("type") == "tool_use" and part.get("name") == "fit_spectrum":
            return True
    return False


def test_live_retuned_clearing_keeps_tool_search_and_reports(monkeypatch, tmp_path):
    """Three arms over one real trajectory (configured worker model; ~12 calls, est. < $0.60):

    * ``retuned`` — clear_tool_inputs + exclude report/channel/search tools, then four more steps,
      each appending the model's turn and a new filler result, logging cache read/write per step
      (does a clear stay stable across later calls, or is the prefix rewritten each time?);
    * ``no_inputs`` — the same, results only: the difference in ``cleared_input_tokens`` is what
      clearing the inputs buys;
    * ``no_exclusions`` — nothing excluded: ``cleared_tool_uses`` should rise by the excluded uses.

    The image-bearing result is the oldest clearable one, so whether a cleared image counts shows
    in ``cleared_tool_uses`` against the count logged as ``expected_clearable``. Every row is
    written and flushed as it happens, so a failed paid run still leaves its record.
    """
    from auto_research.env import ToolSearchConfig, context_editing_config

    load_dotenv()
    base = context_editing_config()
    arms = {
        "retuned": base._replace(enabled=True, trigger_tokens=3000, clear_at_least_tokens=500,
                                 keep_tool_uses=1, clear_tool_inputs=True),
        "no_inputs": base._replace(enabled=True, trigger_tokens=3000, clear_at_least_tokens=500,
                                   keep_tool_uses=1, clear_tool_inputs=False),
        "no_exclusions": base._replace(enabled=True, trigger_tokens=3000, clear_at_least_tokens=500,
                                       keep_tool_uses=1, clear_tool_inputs=True, exclude_tools=()),
    }
    current = {"cfg": arms["retuned"]}
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.context_editing_config", lambda: current["cfg"]
    )
    monkeypatch.setattr(
        "auto_research.models.providers.anthropic.tool_search_config",
        lambda: ToolSearchConfig(enabled=True, anthropic_tool="tool_search_tool_bm25_20251119",
                                 openai_tool="tool_search"),
    )
    tools = _probe_tools()
    # The configured worker member through its ModelHandle, as production calls it: key rotation,
    # transient retry and the cost meter in front of the adapter.
    from auto_research.models import resolve

    handle_ = next(h for h in resolve("analyst_worker").handles if h.config.provider == "anthropic")
    figure = _figure(tmp_path)
    log = dev_log_path("context-editing", "live_retuned_clearing", ".jsonl")
    handle = log.open("w", encoding="utf-8")

    def write(row):
        handle.write(json.dumps(row, default=str) + "\n")
        handle.flush()

    async def discover(prov):
        msgs: list = [
            ModelMessage(role="system", content="You are a terse analysis assistant."),
            ModelMessage(role="user", content=(
                "Use tool search to find the tool named fit_spectrum, then call it with x=1."
            )),
        ]
        for _ in range(3):
            turn = await prov.call(msgs, tools=tools, cache_prefix=True)
            write({"step": "discover", "calls": [c.name for c in turn.tool_calls],
                   "usage": turn.usage.model_dump()})
            msgs.append(turn)
            if turn.tool_calls:
                msgs.append(ToolResultMessage(results=[
                    ToolResultPart(tool_call_id=c.id, name=c.name,
                                   content=(f"index = 1.7. {_FILLER}" if c.name == "fit_spectrum"
                                            else "ok"))
                    for c in turn.tool_calls
                ]))
            if any(c.name == "fit_spectrum" for c in turn.tool_calls):
                return msgs
            if not turn.tool_calls:
                msgs.append(ModelMessage(role="user", content="Search for fit_spectrum now."))
        raise AssertionError("the model never called fit_spectrum after a tool search")

    def filler(msgs):
        # The image-bearing result is the OLDEST clearable one; then echoes with big inputs, a
        # report draft and a channel text (excluded in the retuned arm), and a last echo.
        plan = [
            ("echo", {"x": "with a figure"}, [figure]),
            ("echo", {"x": "input A " + _FILLER}, []),
            ("edit_report", {"summary": "draft " + _FILLER}, []),
            ("message", {"text": "note to peer"}, []),
            ("echo", {"x": "input B " + _FILLER}, []),
            ("echo", {"x": "last " + _FILLER}, []),
        ]
        for i, (name, args, figs) in enumerate(plan):
            msgs.append(ModelResponse(tool_calls=[ToolCall(id=f"syn_{i}", name=name, arguments=args)]))
            msgs.append(ToolResultMessage(results=[ToolResultPart(
                tool_call_id=f"syn_{i}", name=name, content=f"result {i}. {_FILLER}", figures=figs,
            )]))
        return msgs

    async def arm(prov, name, msgs, steps):
        current["cfg"] = arms[name]
        msgs = list(msgs) + [ModelMessage(role="user", content="Call fit_spectrum again with x=2.")]
        out = []
        for step in range(steps):
            turn = await prov.call(msgs, tools=tools, cache_prefix=True)
            write({"arm": name, "step": step, "edits": turn.context_edits,
                   "calls": [c.name for c in turn.tool_calls],
                   "fit_without_search": _calls_fit_without_searching(turn),
                   "usage": turn.usage.model_dump()})
            out.append(turn)
            msgs.append(turn)
            msgs.append(ToolResultMessage(results=[
                ToolResultPart(tool_call_id=c.id, name=c.name, content=f"step {step}. {_FILLER}")
                for c in turn.tool_calls
            ]) if turn.tool_calls else ModelMessage(role="user", content="Continue."))
            if not turn.tool_calls:
                msgs.append(ModelMessage(role="user", content="Call echo with x=next."))
        return out

    async def run():
        from auto_research.observability import install_cost_meter

        meter = install_cost_meter(run_id="b5-probe")
        prov = handle_
        msgs = filler(await discover(prov))
        clearable = sum(
            1 for m in msgs if isinstance(m, ModelResponse) for c in m.tool_calls
        ) - 1  # keep_tool_uses=1
        write({"expected_clearable_before_exclusions": clearable,
               "excluded_in_trajectory": ["edit_report", "message", "tool search"]})
        retuned = await arm(prov, "retuned", msgs, steps=5)
        no_inputs = await arm(prov, "no_inputs", msgs, steps=1)
        no_excl = await arm(prov, "no_exclusions", msgs, steps=1)
        write({"cost_usd_total": meter.total_usd, "calls": meter.calls})
        return retuned, no_inputs, no_excl

    try:
        retuned, no_inputs, no_excl = asyncio.run(run())
    finally:
        handle.close()
        print(f"\n[context-editing] {log}")
    first = retuned[0]
    assert first.context_edits, "the retuned edit cleared nothing over a 3k trigger"
    assert _calls_fit_without_searching(first), (
        "the discovered tool must be callable after the clear without a fresh search"
    )
    tokens = lambda r: sum(int(e.get("cleared_input_tokens", 0) or 0) for e in r.context_edits)
    uses = lambda r: sum(int(e.get("cleared_tool_uses", 0) or 0) for e in r.context_edits)
    assert tokens(first) > tokens(no_inputs[0]), "clearing inputs clears more"
    assert uses(no_excl[0]) > uses(first), "the exclusions keep report and channel uses"
