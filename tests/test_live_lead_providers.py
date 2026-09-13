"""Opt-in LIVE probe: can every vendor hold the **analyst lead** seat, on the rewritten lead?

    AUTO_RESEARCH_LIVE_FORM_SHAPES=1 pytest tests/test_live_lead_providers.py -s

**What this is now.** It began as a single-call check that each vendor could fill `author_chain`'s
nested argument. The 2026-09-12 refactor replaced the lead's five decision forms with **one loop
that ends on `turn_end`, whose report IS its decision**, so the question worth asking per vendor
grew with it: not "can it fill this one schema" but "can it hold the seat for a realistic turn".
Three cells, in increasing realism — the schema, the full cycle, and the replay.

**Tool search does not apply to this role, and that is by construction rather than an omission.**
`ReadOnlyToolProvider` sets `defer_loading` on nothing, so a lead's tools are all resident and the
provider-side search step never runs. (Measured the hard way: the injection-race probe was written
against the lead first and recorded six turns with no server-tool block at all.) The role that
defers is the worker, and its probe is `tests/test_live_injection_race.py`.

**Why this exists.** The whole lead-as-forms refactor turns on one tool. `author_chain`'s argument
nests objects (`first_link: ToolSpec` plus `additional_links: [ToolSpec, …]`), and the array half
is `items: {$ref}` in JSON Schema —
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
# No per-call output cap on the Responses-dialect vendors, which is what production sends after
# the 2026-09-02 ruling — and this test is where the reason was measured. At 8,000,
# `deepseek-v4-pro` at `high` effort returned an empty 200 on the real form:
# `output_tokens: 8000, reasoning_tokens: 8000`, `parts: ['reasoning']`. It had spent the entire
# allowance reasoning about a large tool schema and emitted nothing — billed in full, delivering
# nothing, which is strictly worse than no cap at all. Anthropic keeps a number because its
# Messages API requires one and the adapter falls back to 1024 without it.
#
# It is also why every record below carries `usage`: the first one recorded only `text: ""`, which
# reads as "the model said nothing" and hides the actual cause.
MEMBERS = [
    ("anthropic", "claude-opus-5", {"thinking": {"type": "adaptive"}, "max_tokens": 21000}),
    ("openai", "gpt-5.6-sol", {"reasoning": {"effort": "high"}}),
    ("dashscope", "qwen3.8-max", {"reasoning": {"effort": "high"}}),
    ("zai", "glm-5.3", {"reasoning": {"effort": "high"}}),
    ("deepseek", "deepseek-v4-pro", {"reasoning": {"effort": "high"}}),
    # Wired and holding a graph role, so it is measured here even though [I8] records its
    # multi-turn tool use as broken: a cell that reports a limitation is a result, and being
    # absent from the matrix is not. Its thinking control is per-vendor, hence the different key.
    ("google", "gemini-3.5-flash", {"thinking_config": {"thinking_level": "high"}}),
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
        name=f"lead_probe_{provider_name}",
        provider=provider_name,
        model=model,
        parameters=parameters,
    )
    return ModelHandle(role="analyst_lead", config=config, provider=provider_for(provider_name))


@pytest.mark.parametrize(
    "provider_name,model,parameters", MEMBERS, ids=[f"{p}:{m}" for p, m, _ in MEMBERS]
)
def test_live_vendor_fills_the_nested_author_chain_brief(provider_name, model, parameters):
    """Cell 1 — the schema. `author_chain` nests an object array, the position Qwen was measured
    refusing on OUTPUT schemas, and it survives as the lead's only form."""
    from auto_research.agents.react import (
        AUTHOR_CHAIN_TOOL,
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
        cycle = LeadCycle(authoring_enabled=True, rounds_left=0)
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
        "brief": bool(cycle.brief) if cycle is not None else None,
        "handler_error": outcome.is_error if outcome is not None else None,
        "handler_text": outcome.text if outcome is not None else None,
        "usage": usage.model_dump(mode="json") if usage is not None else None,
        "provider_part_types": sorted(
            {str(part.get("type", "?")) for part in (response.provider_parts or []) if isinstance(part, dict)}
        ),
        "n_tool_calls": len(response.tool_calls or ()),
        "text": (response.content or "")[:600],
        "specs": (
            [spec.model_dump(mode="json") for spec in cycle.brief.tool_specs]
            if cycle is not None and cycle.brief is not None
            else []
        ),
    }
    path = dev_log_path("lead-providers", f"schema_{provider_name}_{model}".replace("/", "_"), ".json")
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")

    specs = record["specs"]
    print(
        f"\n[schema {provider_name}:{model}] brief={record['brief']} links={len(specs)} "
        f"dispositions={[s.get('disposition') for s in specs]} → {path}"
    )

    assert cycle is not None, (
        f"{provider_name}:{model} accepted the tool but filed nothing "
        f"(usage={record['usage']}, parts={record['provider_part_types']}); "
        f"it answered: {record['text'][:200]!r}"
    )
    assert outcome is not None and not outcome.is_error, (
        f"the real handler rejected {provider_name}:{model}'s arguments: {record['handler_text']}"
    )
    assert cycle.brief is not None and cycle.brief.tool_specs, (
        "an empty chain came through: `first_link` is a required property, so this means the "
        "vendor enforced neither it nor anything else"
    )
    for spec in cycle.brief.tool_specs:
        # Validated into the real model by the handler; these are the lead-owned fields that
        # appeared in no channel before the forms existed.
        assert spec.target_domain and spec.disposition and spec.validation_data


# ── cell 2: the whole cycle, per vendor — the assertion of the rewritten lead ─


def _seeded_reports():
    """Worker reports as the lead really receives them: one carrying a `tool_request`, both with
    figures cited, and a disagreement worth judging.

    This is the "faked history" the lead actually gets — its conversation opens with these, so a
    vendor that mishandles a large nested payload fails here rather than in production.
    """
    from auto_research.schemas import AnalystReport, AnalystToolRequest

    request = AnalystToolRequest(
        science_context=(
            "Energy-resolved polarization cubes are needed for more than one observation at a "
            "time; the reduction exists in xspec_utils/lib/red_ixpe.py but is not exposed."
        ),
        expected_behavior=(
            "ixpe_pcube_bands(ixpe_path, obsids, bands) -> per-band PD/PA with 1-sigma errors."
        ),
        proposed_tool_name="ixpe_pcube_bands",
        validation_data=[{"mission": "IXPE", "obsid": "01002901"}],
    )
    return [
        AnalystReport(
            summary="PD = 4.0 +/- 0.2 % over 2-8 keV; I could not do the per-band split.",
            findings=["broadband PD ~ 4%", "per-band split blocked by a missing tool"],
            status="failed",
            tool_request=request,
        ),
        AnalystReport(
            summary="I get PD = 3.4 +/- 0.2 % on the same data, which disagrees with my peer.",
            findings=["broadband PD ~ 3.4%", "unreconciled >2-sigma disagreement"],
            status="succeeded",
        ),
    ]


def _pinned_toolkit(folder):
    """A private toolkit checkout at `science_bench`'s pinned base, the way a trial gets one.

    **This is the fix for a cell that measured nothing.** The first run pointed the lead at the
    LIVE toolkit and asked it to author `ixpe_pcube_bands` — which already exists there. All five
    vendors read `lib/`, found the capability, and correctly refused to author: *"Do NOT request
    or wait for a new tool. The capability reported missing already exists and is exposed."* A
    strong result, and not the one this cell is for.

    `BASE_REF` is the commit before this question's own tool chain existed, so the capability is
    genuinely absent and authoring is the honest answer. Same helper `science_bench` uses, so the
    checkout is shaped like a trial's rather than like something invented here.
    """
    from benchmarks.science_bench.environment import clone_toolkit
    from benchmarks.science_bench.trial import BASE_REF
    from auto_research.env import toolkit_root as configured_toolkit_root

    dest = folder / "toolkit"
    clone_toolkit(dest, configured_toolkit_root(), BASE_REF, "ai")
    return dest


def _lead_on(provider_name: str, model: str, parameters: dict, folder, toolkit_root):
    """The production lead, seated on one vendor through the configuration pointer's own shape."""
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.graph.runtime import readonly_tools_for

    lead = AnalystLeadAgent(
        model=_handle(provider_name, model, parameters), prompt=load_prompt("analyst_lead")
    )
    lead.agent_key = "analyst_lead"
    lead.report_dir = folder
    lead.tools = readonly_tools_for("analyst_lead", roots=[folder, toolkit_root])
    return lead


@pytest.mark.parametrize(
    "provider_name,model,parameters", MEMBERS, ids=[f"{p}:{m}" for p, m, _ in MEMBERS]
)
def test_live_vendor_holds_the_lead_seat_through_a_full_cycle(
    provider_name, model, parameters, tmp_path, monkeypatch
):
    """Cell 2 — the rewritten lead, per vendor, on the production path.

    One real `run_cycle`: a payload carrying two worker reports and a tool request, the real
    read-only tool loop, a briefed chain whose coding sessions are stubbed, and a turn that must
    end on a **validated** `turn_end`. This is the cell that asserts the new topology holds on a
    vendor other than the one it was developed against.

    Assertions stay on our side: a report exists, it validates, its conditional fields are
    consistent with its own `rework` flag, and the declared tool set never shrank mid-conversation.
    Decision *quality* is recorded for hand reading, never asserted.
    """
    from auto_research.agents.react import TURN_END_TOOL
    from auto_research.schemas import AnalysisTask
    from tests.chain_stub import stub_chain_driver

    toolkit_root = _pinned_toolkit(tmp_path)
    lead = _lead_on(provider_name, model, parameters, tmp_path, toolkit_root)
    driven = stub_chain_driver(monkeypatch)
    declared: list[frozenset] = []

    class _Recorder:
        """A proxy, because `ModelHandle` is a FROZEN dataclass — assigning `.call` on it raises
        `FrozenInstanceError`, which is how the first run of this cell died on all six vendors
        before a single token was spent."""

        def __init__(self, handle):
            self._handle = handle
            self.config = getattr(handle, "config", None)

        async def call(self, messages, **kwargs):
            declared.append(frozenset(t["name"] for t in (kwargs.get("tools") or [])))
            return await self._handle.call(messages, **kwargs)

    lead.model = _Recorder(lead.model)

    outcome = asyncio.run(
        lead.run_cycle(
            AnalysisTask(
                question="Settle the per-band IXPE polarization question for Cyg X-1.",
                instructions="Two workers disagree and one reports a missing capability.",
            ),
            _seeded_reports(),
            sources=["analyst_worker_0", "analyst_worker_1"],
            # ZERO debate rounds on purpose: with rounds available, rework is always cheaper than
            # authoring and every vendor took it, so the chain path was never entered. With none,
            # the lead must brief a chain or conclude — which is the branch this cell exists for.
            rounds_left=0,
            author_budget=2,
            toolkit_root=toolkit_root,
            worktree_base=toolkit_root,
            direct_branch="ai",
        )
    )

    report = outcome.report
    record = {
        "provider": provider_name,
        "model": model,
        "filed": outcome.filed,
        "requests": len(declared),
        "links_driven": [s.proposed_tool_name for s in driven],
        "committed": outcome.committed,
        "report": report.model_dump(mode="json") if report is not None else None,
    }
    path = dev_log_path("lead-providers", f"cycle_{provider_name}_{model}".replace("/", "_"), ".json")
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        f"\n[cycle {provider_name}:{model}] filed={outcome.filed} turns={len(declared)} "
        f"links={len(driven)} rework={getattr(report, 'rework', None)} "
        f"status={getattr(report, 'status', None)} → {path}"
    )

    # The declared set is monotonic: a set that shrinks mid-conversation lets a replayed
    # trajectory name a tool the request no longer declares (the 2026-07-29 run-killer).
    for earlier, later in zip(declared, declared[1:]):
        assert not (earlier - later), f"the declared tool set lost {sorted(earlier - later)}"

    assert report is not None, (
        f"{provider_name}:{model} held the seat but wrote no report — the report IS the decision, "
        "and nothing is composed on its behalf"
    )
    assert not report.rework, (
        "rework was accepted with no rounds left — turn_end must refuse that with the cap named"
    )
    assert outcome.filed == TURN_END_TOOL, (
        f"the cycle ended without a validated turn_end (filed={outcome.filed!r}); a loop that ran "
        "out of budget leaves whatever it wrote, which is not the same as ending deliberately"
    )
    # `turn_end` accepted it, so the conditional requirements must hold — this re-checks them
    # from outside the validator, which is the only way to catch a validator that passed wrongly.
    if report.rework:
        assert report.rationale and report.what_to_probe, (
            "turn_end accepted a rework report missing its rationale or probe"
        )
    else:
        assert report.summary and report.findings, (
            "turn_end accepted a concluding report with no summary or no findings"
        )


# ── cell 3: replay — the run-killer class, per vendor ────────────────────────


@pytest.mark.parametrize(
    "provider_name,model,parameters", MEMBERS, ids=[f"{p}:{m}" for p, m, _ in MEMBERS]
)
def test_live_vendor_accepts_its_own_replayed_trajectory(
    provider_name, model, parameters, tmp_path
):
    """Cell 3 — hand the vendor back its own history and ask for one more turn.

    Three run-killers in one week (2026-08-03 to 08-06) were all the same mistake: a trajectory
    replayed in a shape the provider would not take back. The history is **real, not invented** —
    a first turn is driven live, and its own messages are the payload for the second — because a
    hand-built `provider_parts` array would test my idea of the vendor's shape rather than the
    vendor's.
    """
    from auto_research.models.types import ModelMessage
    from auto_research.agents.react import ReadOnlyToolProvider

    handle = _handle(provider_name, model, parameters)
    tools = ReadOnlyToolProvider(roots=[tmp_path], include_text_search=False)
    definitions = tools.tool_definitions()
    (tmp_path / "notes.txt").write_text("PD = 4.0 +/- 0.2 %\n", encoding="utf-8")

    async def run():
        first = [
            ModelMessage(role="user", content=(
                f"Read {tmp_path / 'notes.txt'} with your tools and tell me the value it holds."
            )),
        ]
        turn1 = await handle.call(first, tools=definitions, tool_choice="auto")
        if not turn1.tool_calls:
            return turn1, None, None
        # Execute for real, then hand the WHOLE turn back — this is what `_react` does.
        from auto_research.models.types import ToolResultMessage, ToolResultPart

        parts = []
        for call in turn1.tool_calls:
            outcome = await tools.execute(call.name, dict(call.arguments or {}))
            parts.append(
                ToolResultPart(tool_call_id=call.id, name=call.name, content=outcome.text[:2000])
            )
        replayed = [*first, turn1, ToolResultMessage(results=parts)]
        turn2 = await handle.call(replayed, tools=definitions, tool_choice="auto")
        return turn1, turn2, replayed

    turn1, turn2, replayed = asyncio.run(run())

    record = {
        "provider": provider_name,
        "model": model,
        "turn1_parts": sorted({str(p.get("type", "?")) for p in (turn1.provider_parts or [])}),
        "turn1_tool_calls": [c.name for c in (turn1.tool_calls or ())],
        "replayed_turns": len(replayed) if replayed else 0,
        "turn2_text": (turn2.content or "")[:600] if turn2 is not None else None,
        "turn2_stop": getattr(turn2, "stop", None) if turn2 is not None else None,
    }
    path = dev_log_path("lead-providers", f"replay_{provider_name}_{model}".replace("/", "_"), ".json")
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n[replay {provider_name}:{model}] parts={record['turn1_parts']} "
          f"replayed={record['replayed_turns']} → {path}")

    if turn2 is None:
        pytest.skip(
            f"{provider_name}:{model} called no tool on the first turn, so there was no trajectory "
            "to replay — see the recorded turn for what it did instead"
        )
    # The vendor took its own history back. That is the whole assertion: the failures this guards
    # were 400s at the transport, not bad answers.
    assert turn2.stop not in ("transient", "refusal"), (
        f"{provider_name}:{model} rejected its own replayed trajectory (stop={turn2.stop})"
    )
