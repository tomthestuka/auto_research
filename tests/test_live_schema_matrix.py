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
import time

import pytest
from dotenv import load_dotenv
from pydantic import BaseModel

from auto_research.models.providers.anthropic import AnthropicProvider
from auto_research.models.providers.dashscope import DashscopeProvider
from auto_research.models.providers.deepseek import DeepSeekProvider
from auto_research.models.providers.google import GoogleProvider
from auto_research.models.providers.openai import OpenAIProvider
from auto_research.models.providers.zai import ZaiProvider
from auto_research.models.types import (
    ModelMessage,
    RoleModelConfig,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.schemas import (
    AnalystReport,
    AuthoringDecision,
    ResearchReport,
    SearchRequest,
    ToolReviewVerdict,
)
from auto_research.storage import MemoryRecord
from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MODEL_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MODEL_TESTS=1 to spend provider tokens",
)

# Every schema reachable from `roles.py`; the ones marked COMPOSE also ride the terminal
# compose, which is the path that now declares tools.
#
# `AuthoringDecision` is **no longer composed by any role** — the lead's turn ends by filing a
# form, and the harness builds this object from the arguments. The row is kept because it is still
# the strictest shape this project sends a provider (a required list of nested objects, each with
# its own nested list), and that shape now travels as `author_chain`'s tool `input_schema`. The
# input-schema side is measured separately, on all five vendors, by
# `tests/test_live_lead_providers.py` (formerly test_live_form_shapes); this row keeps the output-schema side from silently
# regressing while the model class still exists.
#
# `_ClarifyReply` was removed from this list on 2026-09-12 with the class it named: the lead no
# longer answers its coder in a second conversation composed into a schema. `ask_lead` parks the
# question, the lead's own loop collects it at a safe injection point, and the answer goes back
# through `drive_coder` as a mid-turn steer. Nothing composes that shape any more, so measuring
# it on six vendors measured nothing.
SCHEMAS = [
    ("AnalystReport", AnalystReport),          # COMPOSE (analyze / lead report terminals)
    ("AuthoringDecision", AuthoringDecision),  # shape-only: the nested chain, no longer composed
    ("ToolReviewVerdict", ToolReviewVerdict),  # COMPOSE (colead.review_tool)
    ("SearchRequest", SearchRequest),
    ("MemoryRecord", MemoryRecord),
    ("ResearchReport", ResearchReport),
]

PROVIDERS = [
    ("openai", OpenAIProvider(), "gpt-5.6-terra"),
    ("anthropic", AnthropicProvider(), "claude-sonnet-5"),
    # DeepSeek joined the chat set 2026-08-24. It belongs in this matrix and not merely in a
    # smoke test because its Responses endpoint documents `format` as supported without saying
    # whether `strict` constrains anything, and because a compatibility layer is exactly where a
    # large real schema diverges from a two-field toy.
    ("deepseek", DeepSeekProvider(), "deepseek-v4-flash"),
    # Z.AI reaches the same guarantee by the forced-tool route rather than `text.format`
    # (`ResponsesVendor.schema_via_tool`), so it belongs in this matrix on equal terms: what
    # matters to a caller is that a valid instance comes back, not which mechanism produced it.
    ("zai", ZaiProvider(), "glm-5.3"),
    # Qwen joined on 2026-08-24, once Model Studio was activated on the account. It reaches a
    # schema by the same forced-tool route as Z.AI, except that it refuses a *named* tool_choice
    # while thinking (Anthropic's documented rule, enforced here too), so the emit tool is
    # offered under `auto` instead.
    ("dashscope", DashscopeProvider(), "qwen3.8-max"),
    # Keep the balanced tier in the identical fixture shape: same adapter, schema matrix and
    # trajectory tests, with only the model id changing. This catches model-specific behaviour
    # hidden by a vendor-wide descriptor (notably tool/schema compatibility and ref handling).
    ("dashscope", DashscopeProvider(), "qwen3.7-plus"),
    # Google is here because it is WIRED and holds a graph role (`librarian_io`'s third member),
    # not because it is expected to pass every cell. Its multi-turn tool use is the known-broken
    # axis ([I8]), so some cells are expected to record a limitation — and that is a measurement.
    # It had been absent from this list entirely, which meant the one adapter with a documented
    # defect was also the one nobody measured; `tests/test_provider_matrix.py` now fails if any
    # wired vendor goes missing again.
    ("google", GoogleProvider(), "gemini-3.5-flash"),
]

QWEN_MODELS = ["qwen3.8-max", "qwen3.7-plus"]
QWEN_REASONING_EFFORTS = ["none", "minimal", "medium"]
QWEN_MATRIX_REASONING_EFFORT = "minimal"

#: Vendors that cannot be handed a schema by any route this adapter supports, or whose account
#: cannot be exercised. They still have to reach, authenticate and call tools, which is what the
#: check below covers. Z.AI and Qwen left this list on 2026-08-24 when their forced-tool route
#: was live-verified.
SCHEMALESS_PROVIDERS: list[tuple[str, object, str]] = [
    # Empty since 2026-08-24: every configured vendor reaches a schema by one route or the
    # other. Kept because the next vendor added may not, and the checks below are what say so.
]


#: A literal for hash-shaped fields. An instruction to "use 64 hex characters" loses to the
#: model's own sense of what a digest looks like — measured twice on Anthropic, which returned
#: differently-invented stylised values of the wrong length — because no provider enforces
#: JSON-Schema `pattern`. A value to copy verbatim competes with nothing.
_HASH_LITERAL = "0" * 64


def _skip_without_key(provider) -> None:
    """Skip a leg whose credential is absent, naming the variable rather than failing the run."""
    vendor = getattr(provider, "vendor", None)
    if vendor is None or not vendor.key_env:
        return
    if not os.getenv(vendor.key_env):
        pytest.skip(f"{vendor.name}: ${vendor.key_env} is unset")


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


def _config(
    provider_name: str, model: str, *, reasoning_effort: str | None = None
) -> RoleModelConfig:
    parameters = (
        {"max_tokens": 8192}
        if provider_name == "anthropic"
        else {"max_output_tokens": 8192}
    )
    if provider_name == "dashscope":
        # Alibaba defaults Responses calls to medium reasoning. The full capability matrix uses
        # the lowest reasoning mode that preserves deliberation; the focused sweep below measures
        # direct mode separately because it previously failed the complex AuthoringDecision shape.
        parameters["reasoning"] = {
            "effort": reasoning_effort or QWEN_MATRIX_REASONING_EFFORT
        }
    return RoleModelConfig(
        name="probe",
        provider=provider_name,
        model=model,
        vision=True,
        documents=True,
        # Anthropic budgets output with `max_tokens`; every Responses-dialect vendor uses
        # `max_output_tokens`, which DeepSeek and Z.AI both document as supported.
        parameters=parameters,
    )


def _handle(provider_name: str, provider, model, *, reasoning_effort: str | None = None):
    """A real ``ModelHandle``, so a live leg runs the path production runs.

    These tests used to call ``provider.call`` directly. That skipped everything
    ``ModelHandle.call`` adds -- and the omission that bit was **key failover**: a key-scoped
    failure (a rate limit, a spent balance) rotates to the configured fallback inside
    ``call_with_transient_retry``, so a direct call fails on an exhausted primary while the run
    it is meant to model would have continued on the other key. Observed 2026-08-24, when both
    frontier primaries were out of credit and 22 legs failed against working fallbacks.

    It also closes the altitude gap recorded on 2026-08-06: a live test that never executes the
    layer above the adapter cannot see a defect introduced there.
    """
    from auto_research.models.types import ModelHandle

    return ModelHandle(
        role="live_probe",
        config=_config(provider_name, model, reasoning_effort=reasoning_effort),
        provider=provider,
    )


def _log_stem(provider_name: str, model: str, case: str) -> str:
    """Keep live artifacts distinct when one provider contributes multiple model fixtures."""
    return f"{provider_name}_{model.replace('/', '_')}_{case}"


@pytest.mark.parametrize("provider_name,provider,model", PROVIDERS)
@pytest.mark.parametrize("schema_name,schema", SCHEMAS, ids=[n for n, _ in SCHEMAS])
def test_live_schema_is_accepted_with_tools_declared(
    provider_name, provider, model, schema_name, schema
):
    """The request is accepted and a valid instance comes back, with tools on the request.

    The prompt spells out the hash format because **no provider enforces JSON-Schema
    ``pattern``** (measured on Anthropic 2026-08-24: it returned a plausible but wrong-length
    value for ``FigureRef.sha256``, and DeepSeek's docs list ``pattern`` among the unsupported
    keywords). Type, required and ``additionalProperties`` are enforced; a regex is advisory.

    That gap is harmless in production, where a ``FigureRef`` is built by a tool from a real file
    and the model only *cites* figures by index — it is reachable only here, because this test
    asks a model to invent a whole record from nothing. The assertion stays strict: what is
    relaxed is the unnatural demand, not the validation.
    """
    load_dotenv()
    _skip_without_key(provider)
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
                "that produced a null result. Keep every list to at most one element. Leave "
                "the analysis tools alone. For any hash field use this exact value: "
                + _HASH_LITERAL
                + "."
            ),
        ),
    ]

    async def run():
        return await _handle(provider_name, provider, model).call(
            messages,
            schema=schema,
            tools=_TOOLS,
            tool_choice="none",
            cache_prefix=True,
        )

    started = time.perf_counter()
    response = asyncio.run(run())
    elapsed_s = time.perf_counter() - started

    log = dev_log_path(
        "schema-matrix", _log_stem(provider_name, model, schema_name), ".json"
    )
    log.write_text(
        json.dumps(
            {
                "schema": schema_name,
                "provider": provider_name,
                "model": model,
                "parsed": response.parsed,
                "tool_calls": [c.name for c in response.tool_calls],
                "input_tokens": response.usage.input_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "output_tokens": response.usage.output_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
                "elapsed_s": elapsed_s,
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


@pytest.mark.parametrize("provider_name,provider,model", PROVIDERS)
@pytest.mark.parametrize(
    "schema_name,schema",
    [("AnalystReport", AnalystReport), ("ToolReviewVerdict", ToolReviewVerdict)],
    ids=["AnalystReport", "ToolReviewVerdict"],
)
def test_live_compose_with_real_schema_after_a_real_trajectory(
    provider_name, provider, model, schema_name, schema
):
    """The report-composition phase for real: N tool-calling turns, THEN the `none` compose.

    The matrix above sends each schema over a two-message conversation — enough to prove the
    schema converts and parses, but NOT the shape a run actually sends. The real compose replays
    a trajectory: assistant turns carrying provider parts (reasoning on OpenAI, thinking on
    Anthropic), `tool_use`/`function_call` items, and their results. Every run-killer at this
    seam needed that history to reproduce; none of them would have fired on a bare conversation.

    So: two `tool_choice="auto"` turns that really call a tool, then the terminal compose with a
    REAL schema and `tool_choice="none"` over the accumulated history.
    """
    load_dotenv()
    _skip_without_key(provider)
    load_dotenv()
    seed = [
        ModelMessage(
            role="system",
            content="You analyse IXPE X-ray polarization data. Use read_file to inspect inputs.",
        ),
        ModelMessage(
            role="user",
            content=(
                "Assess /tmp/cygx1_qu.csv for a polarization detection. Read it first, then "
                "read /tmp/cygx1_meta.txt, then report."
            ),
        ),
    ]

    async def run():
        handle = _handle(provider_name, provider, model)
        history = list(seed)
        turns = []
        turn_elapsed_s = []
        for reply in ("q=0.012+/-0.009, u=-0.004+/-0.009, n_slices=40",
                      "exposure=524389 s, source=Cyg X-1, obsid=01002901"):
            started = time.perf_counter()
            turn = await handle.call(
                history, tools=_TOOLS, tool_choice="auto", cache_prefix=True
            )
            turn_elapsed_s.append(time.perf_counter() - started)
            turns.append(turn)
            if not turn.tool_calls:
                break
            history = [
                *history,
                turn,
                ToolResultMessage(results=[
                    ToolResultPart(tool_call_id=c.id, name=c.name, content=reply, is_error=False)
                    for c in turn.tool_calls
                ]),
            ]
        history.append(ModelMessage(
            role="user",
            content=f"Emit the final {schema_name} now, grounded only in the tool results above. "
                    "Keep every list to at most one element.",
        ))
        started = time.perf_counter()
        composed = await handle.call(
            history,
            schema=schema,
            tools=_TOOLS,
            tool_choice="none",
            cache_prefix=True,
        )
        compose_elapsed_s = time.perf_counter() - started
        return turns, turn_elapsed_s, composed, compose_elapsed_s

    started = time.perf_counter()
    turns, turn_elapsed_s, composed, compose_elapsed_s = asyncio.run(run())
    total_elapsed_s = time.perf_counter() - started

    log = dev_log_path(
        "schema-matrix",
        _log_stem(provider_name, model, f"{schema_name}_trajectory"),
        ".json",
    )
    log.write_text(
        json.dumps(
            {
                "provider": provider_name,
                "model": model,
                "turns": [
                    {
                        "tool_calls": [c.name for c in turn.tool_calls],
                        "part_types": [p.get("type") for p in turn.provider_parts],
                        "input_tokens": turn.usage.input_tokens,
                        "cache_read_tokens": turn.usage.cache_read_tokens,
                        "output_tokens": turn.usage.output_tokens,
                        "reasoning_tokens": turn.usage.reasoning_tokens,
                        "elapsed_s": elapsed,
                    }
                    for turn, elapsed in zip(turns, turn_elapsed_s, strict=True)
                ],
                "composed_parsed": composed.parsed,
                "composed_tool_calls": [c.name for c in composed.tool_calls],
                "composed_usage": {
                    "input_tokens": composed.usage.input_tokens,
                    "cache_read_tokens": composed.usage.cache_read_tokens,
                    "output_tokens": composed.usage.output_tokens,
                    "reasoning_tokens": composed.usage.reasoning_tokens,
                },
                "compose_elapsed_s": compose_elapsed_s,
                "total_elapsed_s": total_elapsed_s,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    assert any(t.tool_calls for t in turns), "the trajectory must contain a real tool call"
    assert not composed.tool_calls, "tool_choice='none' forbids a call"
    assert composed.parsed is not None, "the compose produced its structured answer"
    schema.model_validate(composed.parsed)


@pytest.mark.parametrize("model", QWEN_MODELS)
@pytest.mark.parametrize("reasoning_effort", QWEN_REASONING_EFFORTS)
def test_live_qwen_reasoning_effort_latency(model, reasoning_effort):
    """Measure Qwen's reasoning cost without changing the task between effort levels.

    The full matrix establishes capability at ``minimal``. This smaller, identical request isolates
    the latency policy: ``none`` tests direct mechanical schema emission, ``minimal`` the proposed
    default for ordinary agent work, and ``medium`` Alibaba's server default. The test asserts only
    schema validity; elapsed time and token classes are evidence written to the development log.
    """
    load_dotenv()
    provider = DashscopeProvider()
    _skip_without_key(provider)
    messages = [
        ModelMessage(
            role="system",
            content="You emit structured results for an X-ray astronomy research pipeline.",
        ),
        ModelMessage(
            role="user",
            content=(
                "Emit a minimal schema-valid MemoryRecord for an IXPE Cyg X-1 null result. "
                "Use this exact value for every hash field: " + _HASH_LITERAL + "."
            ),
        ),
    ]

    async def run():
        return await _handle(
            "dashscope",
            provider,
            model,
            reasoning_effort=reasoning_effort,
        ).call(
            messages,
            schema=MemoryRecord,
            tools=_TOOLS,
            tool_choice="none",
            cache_prefix=True,
        )

    started = time.perf_counter()
    response = asyncio.run(run())
    elapsed_s = time.perf_counter() - started

    log = dev_log_path(
        "schema-matrix",
        _log_stem("dashscope", model, f"reasoning_{reasoning_effort}"),
        ".json",
    )
    log.write_text(
        json.dumps(
            {
                "provider": "dashscope",
                "model": model,
                "reasoning_effort": reasoning_effort,
                "elapsed_s": elapsed_s,
                "input_tokens": response.usage.input_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "output_tokens": response.usage.output_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
                "parsed": response.parsed,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    assert response.parsed is not None
    MemoryRecord.model_validate(response.parsed)


@pytest.mark.skipif(not SCHEMALESS_PROVIDERS, reason="every configured vendor reaches a schema")
@pytest.mark.parametrize(
    "provider_name,provider,model", SCHEMALESS_PROVIDERS or [(None, None, None)],
    ids=[n for n, _, _ in SCHEMALESS_PROVIDERS] or ["none"],
)
def test_live_schemaless_vendor_reaches_and_calls_a_tool(provider_name, provider, model):
    """A vendor that cannot be trusted with a schema must still reach, answer and call a tool.

    Those are the capabilities a role actually needs from it, so they are what is asserted: the
    call authenticates at the vendor's own endpoint, prose comes back, and a declared tool is
    invoked with correctly-typed arguments. Schema behaviour is deliberately not exercised here,
    because the adapter refuses to send one (see the deterministic sweep in
    ``tests/test_responses_vendors.py``); it is refused on our side, so there is nothing live to
    learn.

    A vendor whose account is not entitled skips carrying its own refusal rather than failing.
    Nine consequences of one billing gate would bury the cause, which is the same rule the coder
    matrix follows.
    """
    load_dotenv()
    _skip_without_key(provider)

    messages = [
        ModelMessage(role="system", content="You assist an X-ray astronomy research pipeline."),
        ModelMessage(
            role="user",
            content="Call read_file on the path /etc/hostname. Use the tool, do not answer directly.",
        ),
    ]

    async def run():
        return await _handle(provider_name, provider, model).call(
            messages, tools=_TOOLS, tool_choice="auto"
        )

    try:
        response = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 — a vendor refusal is a skip, not a failure
        text = str(exc)
        # Entitlement and credential refusals are both "this account cannot call the vendor",
        # which is not a defect in our adapter. Alibaba answers `AccessDenied.Unpurchased` when
        # Model Studio is unactivated and `InvalidApiKey` when the key belongs to another plan's
        # host, and a run may meet either depending on which key is configured.
        if any(
            marker in text
            for marker in ("Unpurchased", "AccessDenied", "InvalidApiKey", "invalid api-key")
        ):
            pytest.skip(f"{provider_name}: vendor refuses this credential -- {text[:160]}")
        raise

    log = dev_log_path("schema-matrix", f"{provider_name}_schemaless", ".json")
    log.write_text(
        json.dumps(
            {
                "provider": provider_name,
                "model": model,
                "content": response.content[:400],
                "tool_calls": [
                    {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                ],
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    assert response.tool_calls, f"{provider_name} declared a tool and never called it"
    call = response.tool_calls[0]
    assert call.name == "read_file"
    assert isinstance(call.arguments.get("path"), str) and call.arguments["path"]
    assert response.usage.output_tokens > 0, "usage did not map into the neutral buckets"


@pytest.mark.skipif(not SCHEMALESS_PROVIDERS, reason="every configured vendor reaches a schema")
@pytest.mark.parametrize(
    "provider_name,provider,model", SCHEMALESS_PROVIDERS or [(None, None, None)],
    ids=[n for n, _, _ in SCHEMALESS_PROVIDERS] or ["none"],
)
def test_live_schemaless_vendor_sends_its_schema_by_the_route_it_supports(
    provider_name, provider, model
):
    """These vendors ignore `text.format`, so the schema must ride as a forced tool call.

    Checked against the real adapter, and without spending a call: what could regress is the
    *request*, and a vendor whose account cannot answer still lets us assert how we would ask.
    """
    load_dotenv()
    vendor = provider.vendor
    assert not vendor.supports_strict_schema
    assert vendor.schema_via_tool, (
        f"{provider_name} can be handed no schema by any route, so no role may emit one"
    )


# ── F5a: the normalised stop classification, read live from each vendor ──────
#
# This is the one change in the 2026-09-09 batch that ONLY a live call can settle. Every mapping
# in `models/types.py` was written from that vendor's own documentation, and a documentation page
# says where a field is meant to live, never that this account, this endpoint and this SDK version
# actually put it there. The whole point of `stop` is that the six vendors disagree — Anthropic has
# no `tool_calls` value, Google's enum is 17 wide, and DeepSeek and Z.AI report *infrastructure
# failures* in the terminal field — so a mapping that silently returns "unknown" would restore the
# very defect F5b removed: a turn read as finished when it was not.


def _stop_log(provider_name, model, case, response, **extra):
    log = dev_log_path("stop-classes", _log_stem(provider_name, model, case), ".json")
    log.write_text(json.dumps({
        "provider": provider_name, "model": model, "case": case,
        "stop": response.stop,
        "content": (response.content or "")[:300],
        "tool_calls": [c.name for c in response.tool_calls],
        "output_tokens": response.usage.output_tokens,
        **extra,
    }, indent=2), encoding="utf-8")


@pytest.mark.parametrize("provider_name,provider,model", PROVIDERS)
def test_live_a_plain_answer_classifies_as_end_turn(provider_name, provider, model):
    """A short, complete answer must classify as `end_turn` — not `unknown`.

    `unknown` is the failure this cell exists to catch: it means the adapter did not find the
    field, and `_continuation_for` then judges the turn on its content alone. That is a safe
    fallback and a silent loss of the signal, which is exactly how a stop reason goes unnoticed.
    """
    load_dotenv()
    _skip_without_key(provider)
    response = asyncio.run(_handle(provider_name, provider, model).call(
        [ModelMessage(role="user", content="Reply with exactly: DONE")],
    ))
    _stop_log(provider_name, model, "end_turn", response)
    assert response.stop == "end_turn", (
        f"{provider_name}/{model} classified a completed answer as {response.stop!r} — the "
        "adapter did not find this vendor's finish field where its docs place it"
    )


@pytest.mark.parametrize("provider_name,provider,model", PROVIDERS)
def test_live_a_tool_calling_turn_is_recognised_by_its_tool_calls(provider_name, provider, model):
    """What the loop reads on a tool-calling turn is `tool_calls`, and `stop` is only consulted
    when there are none — so THAT is the invariant this cell asserts, and the stop value is
    recorded rather than asserted.

    Measured 2026-09-09, and it corrected a wrong assertion of mine. Only Anthropic reports a
    `tool_use` stop; every Responses-dialect vendor returns `status="completed"` -> `end_turn`
    **with a tool call present** (openai, deepseek, zai, dashscope x2, all 5/5). That is exactly
    what `stop_from_openai_responses` documents — *"There is no `tool_use` status: tool intent is
    read off the output items"* — so my first version of this cell contradicted the adapter's own
    stated contract, and it would have failed forever on five of seven vendors for no defect.
    Harmless in production precisely because `_react` checks `response.tool_calls` first.

    The tool is a **real production definition** (`turn_end_tool_def`), not a hand-written schema.
    The hand-written one taught this lesson too: its `additionalProperties` reached Gemini as
    `additional_properties` and hard-400'd, i.e. a defect of my fixture rather than of the code.
    """
    load_dotenv()
    _skip_without_key(provider)
    from auto_research.agents.react import TURN_END_TOOL, turn_end_tool_def

    response = asyncio.run(_handle(provider_name, provider, model).call(
        [ModelMessage(role="user", content=(
            "You have finished your work. End your turn now by calling the "
            f"`{TURN_END_TOOL}` tool with a one-sentence summary."))],
        tools=[turn_end_tool_def()],
    ))
    _stop_log(provider_name, model, "tool_use", response)
    if not response.tool_calls:
        pytest.skip(
            f"{provider_name}/{model} answered in prose instead of calling {TURN_END_TOOL} "
            f"(stop={response.stop!r}) — recorded; the loop's continuation handles this"
        )
    assert any(c.name == TURN_END_TOOL for c in response.tool_calls), (
        f"{provider_name}/{model} called {[c.name for c in response.tool_calls]}"
    )
    assert response.stop != "unknown", (
        f"{provider_name}/{model} reported no usable finish field at all on a tool turn"
    )

# **The `truncated` class is deliberately NOT live-tested, and this is the reason rather than an
# omission.** Forcing a truncation needs a per-call output cap, and `max_output_tokens` was
# **removed from every optional path by human ruling** (2026-09-02): a cap buys no cost protection
# because a vendor bills what it generated, and its only effect is on the call that hits it, where
# the failure is total (measured: 8,000 choked a `deepseek-v4-pro` call to an empty 200 billed in
# full, while uncapped used 19,772). `ModelHandle.call` therefore accepts no such argument — my
# first version of this cell invented one and got a clean `TypeError` on all seven vendors, which
# is the API telling the truth.
#
# So the mapping (`max_tokens`/`length`/`MAX_TOKENS`/`incomplete_details.reason` -> `truncated`)
# stays **deterministic-only**, tested in `tests/test_turn_end.py` against each vendor's
# documented value. Same category as F7's transient override, which would need a vendor outage on
# demand. Stated here so a later reader does not mistake the gap for an untested mapping.


# ── F7: a real vendor throttle, provoked ─────────────────────────────────────


@pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_THROTTLE_PROBE") != "1",
    reason="set AUTO_RESEARCH_LIVE_THROTTLE_PROBE=1 to deliberately provoke a vendor throttle",
)
def test_live_a_provoked_qwen_throttle_is_waited_out_not_fatal():
    """F7, live: hammer Alibaba concurrently until it throttles, and prove we now recover.

    I had recorded this class as not live-provable. It is — the human pointed out that
    concurrency provokes it on demand, which is worth more than the reasoning it replaced.

    What killed slot 6 (2026-09-08, $11.45, one HTTP attempt, zero retries) was Alibaba returning
    **HTTP 400** with `code: "ServiceUnavailable"` and a message reading
    `"<503> InternalError.Algo: ... [Too many requests ... throttled ... try again later.]"`.
    `is_transient_provider_error` read `status_code` first, found the int 400, and never reached
    the string-`code` fallback, so a condition the vendor's own reference documents at 500/429
    with *"Retry first"* raised at once.

    **The retry warning is the proof, and it is proof rather than a proxy**:
    `call_with_transient_retry` emits it only on the branch it takes *after*
    `is_transient_provider_error(exc, provider)` returns True. So a captured warning naming a 400
    `ServiceUnavailable` is the predicate classifying the real error correctly, observed through
    the production path rather than by hand-feeding a fixture to it.

    Driven through `ModelHandle`, so the retry layer, the key pool and the media guard are all
    in front of the adapter exactly as in a run. A throttle that never arrives skips: the vendor's
    capacity is not ours to command, and manufacturing a pass would be worse than no measurement.
    """
    import logging

    load_dotenv()
    provider = DashscopeProvider()
    _skip_without_key(provider)
    # Load profile as knobs, because the condition is **serving capacity, not a rate limit** and
    # the two need different load. Measured 2026-09-09: 20 light concurrent calls succeeded 20/20
    # twice, ~18s, zero throttles — which agrees with the slot-6 forensics, where the failing run
    # was at 0.007% of Alibaba's documented 15,000 RPM and a sibling succeeded on the same key
    # 31 seconds later. A rate limit would have refused those 20; capacity did not care.
    concurrency = int(os.getenv("AUTO_RESEARCH_THROTTLE_CONCURRENCY", "20"))
    model = os.getenv("AUTO_RESEARCH_THROTTLE_MODEL", "qwen3.7-plus")
    heavy = os.getenv("AUTO_RESEARCH_THROTTLE_HEAVY") == "1"
    handle = _handle("dashscope", provider, model,
                     reasoning_effort="high" if heavy else None)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    retry_log = logging.getLogger("auto_research.models.retry")
    capture = _Capture(level=logging.WARNING)
    retry_log.addHandler(capture)
    previous = retry_log.level
    retry_log.setLevel(logging.WARNING)

    light = "name one X-ray polarimetry mission. Answer in under 10 words."
    # A heavy turn is what strains capacity: long generation at high reasoning effort, which is
    # the shape the batch that hit slot 6 was running (five concurrent trials of long calls).
    weighty = (
        "Derive, from first principles and in full detail, how a gas pixel detector recovers the "
        "Stokes parameters of an X-ray beam: photoelectron emission, track reconstruction, the "
        "modulation factor, and how background dilutes the measured polarization degree. Show "
        "the algebra step by step and discuss the systematic errors at each stage."
    )

    async def one(i: int):
        return await handle.call(
            [ModelMessage(role="user", content=f"Probe {i}: {weighty if heavy else light}")],
        )

    async def blast():
        return await asyncio.gather(
            *(one(i) for i in range(concurrency)), return_exceptions=True
        )

    started = time.perf_counter()
    try:
        results = asyncio.run(blast())
    finally:
        retry_log.removeHandler(capture)
        retry_log.setLevel(previous)
    elapsed_s = time.perf_counter() - started

    failures = [r for r in results if isinstance(r, BaseException)]
    warnings_seen = [r.getMessage() for r in records]
    throttles = [
        m for m in warnings_seen
        if "transient provider error" in m
        and ("ServiceUnavailable" in m or "throttl" in m.lower() or "capacity" in m.lower())
    ]

    log = dev_log_path(
        "throttle-probe", f"qwen_{model}_c{concurrency}{'_heavy' if heavy else ''}", ".json"
    )
    log.write_text(json.dumps({
        "concurrency": concurrency,
        "model": model,
        "heavy": heavy,
        "elapsed_s": round(elapsed_s, 1),
        "succeeded": len(results) - len(failures),
        "failed": len(failures),
        "failures": [f"{type(e).__name__}: {e}"[:400] for e in failures],
        "retry_warnings": warnings_seen[:20],
        "throttles_classified_transient": len(throttles),
    }, indent=2), encoding="utf-8")

    # Whatever else happened, a throttle must never have been fatal. This is the slot-6
    # regression, stated as the property rather than as a count: no call may have died on a
    # condition the vendor documents as retryable.
    fatal_throttles = [
        e for e in failures
        if "ServiceUnavailable" in str(e) or "throttl" in str(e).lower()
        or "system capacity" in str(e).lower()
    ]
    assert not fatal_throttles, (
        f"a documented-transient throttle was fatal after F7: {fatal_throttles[:2]}"
    )

    if not throttles:
        pytest.skip(
            f"{concurrency} concurrent {'heavy' if heavy else 'light'} calls on {model} did not "
            f"provoke a throttle ({len(failures)} other failure(s), "
            f"{elapsed_s:.0f}s) — the vendor's capacity is not ours to command; recorded"
        )
