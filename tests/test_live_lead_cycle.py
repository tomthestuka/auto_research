"""Opt-in LIVE check of the analyst lead's **cycle** — one ReAct loop that ends by filing a form.

    AUTO_RESEARCH_LIVE_LEAD_CYCLE=1 pytest tests/test_live_lead_cycle.py -s

**Why this exists.** Block 0's third launch died at $19.43 because the lead answered the authoring
question with ``should_author=true`` and an empty ``tool_specs`` — schema-VALID output that a
Pydantic validator then killed the run over. The cause was informational, not a model limitation:
the schema it received marked the chain optional, carried no field descriptions at all (every
explanation was a ``#`` comment, which never reaches JSON Schema), and the one rule stating
otherwise was written only in the direction that did not bind. The lead is now one loop that files
a form whose fields carry their own descriptions, and ``author_chain`` requires at least one link
in its own schema, so the state that killed the run cannot be expressed.

Whether that actually holds in front of a real model is not a deterministic question, which is why
this exists. The 2026-08-06 lesson binds the altitude: live tests that called ``provider.call``
directly never executed ``roles.py``, so a defect above the adapter was invisible. Every cell here
drives the **real** ``AnalystLeadAgent.run_cycle`` on the **real** ``prompts/analyst_lead.md``
through a **real** ``ModelHandle``, so the role layer, the media guard and key rotation are all in
the path.

**The stimulus is frozen out of a real ledger, not written for this test** — the discipline
``tests/fixtures/coder_history.json`` already uses. ``tests/fixtures/lead_cycle_reports.json`` is
the two worker ``AnalystReport``s that launch verbatim (22k characters of real prose, real ObsIDs,
both carrying real ``tool_request``s), regenerable with
``tmp_scripts/extract_lead_cycle_fixture.py``. A brief written for the test would measure a brief
tuned to the test.

**Assertions stay on our side of the boundary** (CLAUDE.md §10): which form was filed, that its
arguments validate into the model the downstream consumer expects, that the declared tool set never
shrank, and that the attachments reached the request. Decision *quality* is never asserted — the
filed forms land under ``logs/dev/lead-cycle/`` for a human to read.

**What is stubbed, and what is not.** The coding session is stubbed: ``_drive_one_link`` returns a
committed result without building a session or spawning a coder, because a real chain costs
$27–45 per link and the loop above it is what is under test here. Everything else is live — the
form definitions, the handlers, the prompt, the payload, the model. The context drop at the coding
boundary is covered deterministically instead
(``tests/test_lead_forms.py::test_the_coding_middle_goes_and_both_anchors_survive``); a real
multi-link chain through this path is the observational check a full run provides.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import load_dotenv

from tests.chain_stub import stub_chain_driver
from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_LEAD_CYCLE") != "1",
    reason="set AUTO_RESEARCH_LIVE_LEAD_CYCLE=1 to spend provider tokens on the lead's cycle",
)

load_dotenv()

FIXTURE = Path(__file__).parent / "fixtures" / "lead_cycle_reports.json"

# Real figures from run 8c75990e — 2683 px wide, which is ABOVE Anthropic's 2000 px many-image
# threshold, so the per-provider media guard is genuinely exercised rather than merely passed by.
_FIGURE_SOURCE = Path(
    "/data/fzhou/ar_runtime/test_runtime/runs/8c75990e862ca45993bea6076a92115c"
)


def _reports() -> list:
    from auto_research.schemas import AnalystReport

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [AnalystReport.model_validate(row["report"]) for row in payload["reports"]]


def _real_figures(limit: int = 2) -> list:
    """Real PNGs a real tool produced, as ``FigureRef``s the lead can cite."""
    import hashlib
    from datetime import UTC, datetime

    from auto_research.schemas import FigureRef

    paths = sorted(_FIGURE_SOURCE.glob("analyst_worker_*/figures/*.png"))[:limit]
    if not paths:
        pytest.skip(f"no real figures under {_FIGURE_SOURCE}")
    return [
        FigureRef(
            path=str(path),
            caption=f"{path.stem} (real tool output)",
            mime_type="image/png",
            produced_by="xspec_utils.lib.qu_lag_dcf_plot.qu_lag_dcf_plot",
            created_at=datetime.now(UTC),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in paths
    ]


class _RecordingHandle:
    """Forwards to a real ``ModelHandle`` and records what each request carried.

    Recording at this boundary is what lets the attachment assertions be about the **request**:
    a test that checks the response cannot see a parameter being dropped. The guard itself runs
    below here, inside the adapter, so a call that returns at all is a call the guard accepted.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self.config = getattr(handle, "config", None)
        self.requests: list[dict] = []
        self.tool_sets: list[frozenset[str]] = []

    async def call(self, messages, **kwargs):
        tools = kwargs.get("tools") or []
        self.tool_sets.append(frozenset(t["name"] for t in tools))
        self.requests.append(
            {
                "images": len(kwargs.get("images") or ()),
                "documents": len(kwargs.get("documents") or ()),
                "messages": len(messages),
                "tools": len(tools),
            }
        )
        return await self._handle.call(messages, **kwargs)


def _lead(folder: Path, *, figures: list | None = None):
    """The production lead: real prompt, real provider surface, one recording handle."""
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.react import ReadOnlyToolProvider
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.models import load_registry

    handle = _RecordingHandle(load_registry().resolve("analyst_lead").handles[0])
    lead = AnalystLeadAgent(model=handle, prompt=load_prompt("analyst_lead"))
    lead.agent_key = "analyst_lead"
    lead.report_dir = folder
    toolkit_root = configured_toolkit_root()
    lead.tools = ReadOnlyToolProvider(
        roots=[folder, toolkit_root],
        include_data_catalog=True,
        include_text_search=False,
    )
    if figures:
        lead._figure_catalog["analyst_worker_0"] = list(figures)
    return lead, handle, toolkit_root


def _record(cell: str, lead, handle, outcome, extra: dict | None = None) -> None:
    """Persist the cell for hand reading — quality is judged from the artifact, never asserted."""
    path = dev_log_path("lead-cycle", cell, ".json")
    body = {
        "cell": cell,
        "model": getattr(getattr(handle, "config", None), "model", "?"),
        "filed": outcome.filed,
        "filed_form": lead.last_filed_form[1] if lead.last_filed_form else None,
        "committed": outcome.committed,
        "requests": handle.requests,
        "decision": outcome.decision.model_dump(mode="json") if outcome.decision else None,
        "verdict": outcome.verdict.model_dump(mode="json") if outcome.verdict else None,
        "report": outcome.report.model_dump(mode="json") if outcome.report else None,
        **(extra or {}),
    }
    path.write_text(json.dumps(body, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"\n[{cell}] filed={outcome.filed} → {path}")


def _assert_tool_set_never_shrank(handle) -> None:
    """The declared set is monotonic across every re-entry of the cycle's loop.

    A set that shrinks mid-conversation lets a replayed trajectory name a tool the request no
    longer declares — the "Tool reference 'x' not found in available tools" run-killer
    (2026-07-29).
    """
    for earlier, later in zip(handle.tool_sets, handle.tool_sets[1:]):
        missing = earlier - later
        assert not missing, f"the declared tool set lost {sorted(missing)} mid-conversation"


# ── cell 1: a cycle with a tool request open — the regression cell ───────────


def test_live_lead_cycle_with_a_tool_request_open_reaches_a_usable_decision(tmp_path, monkeypatch):
    """The case that killed launch 3, driven end to end.

    Two answers are legal here — brief a chain (the capability needs writing), or brief none and
    conclude (the request resolves without new code). What must not happen is what did: a decision
    the harness cannot act on. So the assertions are that a report was written at all, and that if
    a chain was briefed, every link carries the two fields the lead owns and appeared in no channel
    before (``target_domain``, ``disposition``).
    """
    from auto_research.agents.react import TURN_END_TOOL
    from auto_research.schemas import AnalysisTask, ToolSpec

    reports = _reports()
    lead, handle, toolkit_root = _lead(tmp_path)
    driven = stub_chain_driver(monkeypatch)
    task = AnalysisTask(
        question=(
            "Reproduce the standard IXPE polarimetry result for Cyg X-1 from the local "
            "observations, per-DU and combined."
        ),
        instructions="The workers have reported. Settle the toolkit question, then judge.",
    )

    outcome = asyncio.run(
        lead.run_cycle(
            task,
            reports,
            sources=["analyst_worker_0", "analyst_worker_1"],
            rounds_left=2,
            author_budget=3,
            toolkit_root=toolkit_root,
            worktree_base=toolkit_root,
            direct_branch="ai",
        )
    )
    _record("authoring_stage", lead, handle, outcome, {"links_driven": len(driven)})

    assert outcome.filed == TURN_END_TOOL, "the lead ended its cycle without a validated turn_end"
    assert outcome.report is not None, "the report IS the decision; there is none"
    _assert_tool_set_never_shrank(handle)
    if outcome.decision is not None:
        assert outcome.decision.should_author is True
        assert outcome.decision.tool_specs, "a chain was briefed with no links — the old bug"
        for spec in outcome.decision.tool_specs:
            assert isinstance(spec, ToolSpec)
            assert spec.target_domain and spec.disposition
            assert spec.validation_data
        # A7: every briefed link is attempted while credits remain — a dead link no longer ends
        # the chain, and nothing ends the turn but turn_end.
        assert len(driven) == len(outcome.decision.tool_specs) or outcome.author_budget == 0


# ── cell 2: the judging stage, with rounds available ─────────────────────────


def test_live_lead_cycle_judging_reaches_a_report_or_asks_for_another_round(tmp_path):
    """Authoring off: the lead judges and writes its report through ``edit_report``, either
    concluding or setting ``rework``. Both are legal; a cycle that writes nothing is not."""
    from auto_research.schemas import AnalysisTask

    figures = _real_figures()
    lead, handle, _ = _lead(tmp_path, figures=figures)
    outcome = asyncio.run(
        lead.run_cycle(
            AnalysisTask(
                question="Reproduce the standard IXPE polarimetry result for Cyg X-1.",
                instructions="No toolkit work is available this cycle — judge what the workers did.",
            ),
            _reports(),
            sources=["analyst_worker_0", "analyst_worker_1"],
            rounds_left=2,
        )
    )
    _record("judging_stage", lead, handle, outcome, {"catalog_figures": len(figures)})

    assert outcome.report is not None, "the report IS the decision; there is none"
    _assert_tool_set_never_shrank(handle)
    if outcome.report.rework:
        assert outcome.report.what_to_probe, "the probe is handed to the workers verbatim"
        assert outcome.report.rationale, "and it has to say what gap one more pass would close"
    else:
        assert outcome.report.summary and outcome.report.findings

    # The attachments rode the request: the lead's own catalog figures are on the first call, and
    # the guard below this boundary accepted them (an over-limit image is resized, never dropped
    # silently). 2683 px wide is above Anthropic's many-image dimension cap on purpose.
    assert handle.requests, "no model call was made"
    assert handle.requests[0]["images"] == len(figures)


# ── cell 3: the round cap is Python's ────────────────────────────────────────


def test_live_lead_cycle_with_no_rounds_left_reaches_a_report(tmp_path):
    """``rounds_left=0``: `turn_end` refuses a report asking for another round, naming the cap, so
    the only accepted turn concludes. The cap lives in Python; the choice lives in the loop."""
    from auto_research.agents.react import TURN_END_TOOL
    from auto_research.schemas import AnalysisTask

    lead, handle, _ = _lead(tmp_path, figures=_real_figures(limit=1))
    outcome = asyncio.run(
        lead.run_cycle(
            AnalysisTask(
                question="Reproduce the standard IXPE polarimetry result for Cyg X-1.",
                instructions="This is the final round — no further analyst round will run.",
            ),
            _reports(),
            sources=["analyst_worker_0", "analyst_worker_1"],
            rounds_left=0,
        )
    )
    _record("no_rounds_left", lead, handle, outcome)

    assert outcome.filed == TURN_END_TOOL, f"filed {outcome.filed!r} with no rounds left"
    assert outcome.report is not None
    # The cap is Python's: `turn_end` refuses a rework report with no rounds left, naming the cap,
    # so the only accepted turn is one that concludes.
    assert outcome.report.rework is False, "rework was accepted with no rounds left"
    _assert_tool_set_never_shrank(handle)
