"""Opt-in LIVE check that every lead-axis vendor can fill the lead's **nested** form.

    AUTO_RESEARCH_LIVE_FORM_SHAPES=1 pytest tests/test_live_form_shapes.py -s

**Why this exists.** The whole lead-as-forms refactor turns on one tool. `author_chain`'s argument
is a **list of objects** (`tool_specs: [ToolSpec, …]`), which in JSON Schema is `items: {$ref}` —
measured 2026-08-24 as the exact position **Qwen refuses** (0/5 draws) while DeepSeek is the mirror
case, each needing opposite treatment. That measurement was taken on **output** schemas, and a
tool's `input_schema` travels a different path through each adapter, so it did not settle this.

It was settled by probing before the forms were written (`tmp_scripts/probe_nested_form.py`, for
cents, the way `tool_choice="none"` was established before that change was made): all five vendors
accept the nested object array and fill it. This is that probe promoted into the suite, so the
measurement is re-runnable rather than a note in a changelog — and upgraded, because it sends the
**real** `author_chain_tool_def()` rather than a miniature of it, and validates what comes back
through the **real** handler into a real `ToolSpec`.

Routed through `ModelHandle`, never `provider.call`: the 2026-08-06 lesson is that a live test
below the handle skips the layer the defects live in (the schema shims, the media guard, key
rotation).

**Assertions stay on our side of the boundary**: that the vendor accepted the tool, filed at least
one link, and that every link validates into `ToolSpec` — the object the harness then acts on.
Which `disposition` a model chose is *quality* and is recorded, never asserted; the responses land
under `logs/dev/form-shapes/` for hand reading.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_FORM_SHAPES") != "1",
    reason="set AUTO_RESEARCH_LIVE_FORM_SHAPES=1 to spend provider tokens on the form shapes",
)

load_dotenv()

# The lead axis of the science sweep, plus the two vendors that back the co-lead and the coder —
# every seat that could ever be handed this tool. Model ids are named here the way
# `tests/test_live_schema_matrix.py` names them: a live test states the vendor build it measured,
# while the runtime still resolves its own models from config.
# The output budget is 21,000, which is what `config/models.yaml` gives every top-tier seat and
# what `benchmarks/science_bench/arms.py` pins for every vendor. It is not a round number chosen
# here: at 8,000 this test measured `deepseek-v4-pro` returning an empty 200 on the real form —
# `output_tokens: 8000, reasoning_tokens: 8000, parts: ['reasoning']`, i.e. `high` effort spent the
# entire budget reasoning about a large tool schema and emitted nothing. That is a fact about the
# budget, not about the form, and a live test that sends a request production never sends measures
# the wrong thing. (It is also why the first record was misleading: it said only `text: ""`, which
# reads as "the model said nothing" — hence the `usage` in every record below.)
MEMBERS = [
    ("anthropic", "claude-opus-5", {"thinking": {"type": "adaptive"}, "max_tokens": 21000}),
    ("openai", "gpt-5.6-sol", {"max_output_tokens": 21000, "reasoning": {"effort": "high"}}),
    ("dashscope", "qwen3.8-max", {"max_output_tokens": 21000, "reasoning": {"effort": "high"}}),
    ("zai", "glm-5.3", {"max_output_tokens": 21000, "reasoning": {"effort": "high"}}),
    ("deepseek", "deepseek-v4-pro", {"max_output_tokens": 21000, "reasoning": {"effort": "high"}}),
]

# Domain-neutral in its shape and specific in its content: one capability that exists in `lib/`
# unexposed and one that is absent, which is the pair that makes `disposition` a real choice
# rather than a coin flip. It names no test scenario the sweep measures.
PROMPT = (
    "An analyst reports the toolkit cannot produce energy-resolved IXPE polarization cubes for "
    "more than one observation at a time, and cannot fit a linear model to PD versus energy. "
    "The reduction code already exists in xspec_utils/lib/red_ixpe.py but is not exposed as a "
    "tool; the curve fit does not exist anywhere.\n\n"
    "File the chain of tool briefs needed, using author_chain. Two links at minimum."
)


def _handle(provider_name: str, model: str, parameters: dict):
    from auto_research.models.providers import provider_for
    from auto_research.models.types import ModelHandle, RoleModelConfig

    config = RoleModelConfig(
        name=f"form_probe_{provider_name}",
        provider=provider_name,
        model=model,
        parameters=parameters,
    )
    return ModelHandle(role="analyst_lead", config=config, provider=provider_for(provider_name))


@pytest.mark.parametrize(
    "provider_name,model,parameters", MEMBERS, ids=[f"{p}:{m}" for p, m, _ in MEMBERS]
)
def test_live_vendor_fills_the_nested_author_chain_form(provider_name, model, parameters):
    from auto_research.agents.react import (
        AUTHOR_CHAIN_TOOL,
        DECIDE_STAGE,
        LeadCycle,
        author_chain_tool_def,
        execute_author_chain,
    )
    from auto_research.models.types import ModelMessage

    definition = author_chain_tool_def()
    handle = _handle(provider_name, model, parameters)

    async def run():
        response = await handle.call(
            [ModelMessage(role="user", content=PROMPT)],
            tools=[definition],
            tool_choice="auto",
        )
        calls = [c for c in (response.tool_calls or ()) if c.name == AUTHOR_CHAIN_TOOL]
        if not calls:
            return response, None, None
        # The real handler, so what is asserted is that the arguments survive the path the
        # runtime actually puts them through — not that the JSON merely parsed.
        cycle = LeadCycle(stage=DECIDE_STAGE, authoring_enabled=True, rounds_left=0)
        outcome = await execute_author_chain(cycle, dict(calls[0].arguments or {}))
        return response, cycle, outcome

    response, cycle, outcome = asyncio.run(run())

    # `usage` and the part count are load-bearing, not decoration: a vendor that returns an empty
    # 200 could have refused the shape or spent its whole output budget reasoning, and those call
    # for opposite fixes. Without them this record reads as "said nothing", which is the
    # results-reader failure that has cost this project paid runs before — a reader pointed at the
    # wrong field looks exactly like a boring finding.
    usage = getattr(response, "usage", None)
    record = {
        "provider": provider_name,
        "model": model,
        "filed": cycle.filed if cycle is not None else None,
        "handler_error": outcome.is_error if outcome is not None else None,
        "handler_text": outcome.text if outcome is not None else None,
        "usage": usage.model_dump(mode="json") if usage is not None else None,
        "provider_part_types": sorted(
            {str(part.get("type", "?")) for part in (response.provider_parts or []) if isinstance(part, dict)}
        ),
        "n_tool_calls": len(response.tool_calls or ()),
        "text": (response.content or "")[:600],
        "specs": (
            [spec.model_dump(mode="json") for spec in cycle.decision.tool_specs]
            if cycle is not None and cycle.decision is not None
            else []
        ),
    }
    path = dev_log_path("form-shapes", f"{provider_name}_{model}".replace("/", "_"), ".json")
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")

    specs = record["specs"]
    print(
        f"\n[{provider_name}:{model}] filed={record['filed']} links={len(specs)} "
        f"dispositions={[s.get('disposition') for s in specs]} "
        f"optional_float={[type(s.get('replication_tolerance')).__name__ for s in specs]} "
        f"→ {path}"
    )

    assert cycle is not None, (
        f"{provider_name}:{model} accepted the tool but filed nothing "
        f"(usage={record['usage']}, parts={record['provider_part_types']}); "
        f"it answered: {record['text'][:200]!r}"
    )
    assert outcome is not None and not outcome.is_error, (
        f"the real handler rejected {provider_name}:{model}'s arguments: {record['handler_text']}"
    )
    assert cycle.filed == AUTHOR_CHAIN_TOOL
    assert cycle.decision is not None and cycle.decision.tool_specs, "an empty chain is unfileable"
    for spec in cycle.decision.tool_specs:
        # Validated into the real model by the handler; these are the lead-owned fields that
        # appeared in no channel before the forms existed.
        assert spec.target_domain and spec.disposition and spec.validation_data
