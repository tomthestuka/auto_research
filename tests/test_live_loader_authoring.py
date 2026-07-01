"""Env-gated LIVE end-to-end test of the lead-driven tool-authoring path (Phase-2 step 7).

Activate with:
    AUTO_RESEARCH_LIVE_LOADER_AUTHORING_TESTS=1 pytest tests/test_live_loader_authoring.py -q -s

Poses a missing-capability request — a joint multi-instrument spectral *loader*,
``load_epoch_spectra`` — to the analyst lead against a **throwaway, empty from-scratch toolkit**
scaffolded in a temp dir on a fresh ``ai`` branch (never the live ``toolkit_root``). The lead must
author it through the whole A1 flow: ``decide_authoring`` -> ``drive_authoring`` (Sonnet coder ->
deterministic gate -> provider-decorrelated GPT co-lead review -> atomic commit), with NO
``load_data`` source, signature, or mechanics leaked — only the capability + requirements.

This is observational/live (CLAUDE.md §10): it drives the real Opus lead, the real Sonnet coder
(``coder`` role), and the real GPT co-lead (``analyst_colead``, ``colead=None`` resolves it from
config decorrelated from the Claude author) against real E5 data in the science env, and writes a
monitor JSONL+MD under ``logs/dev/P2/loader-test/``. It asserts only that the path runs to a
terminal ``AuthoringOutcome`` without crashing (``status`` in {committed, failed, needs_human});
on ``committed`` it checks the tool landed as one commit carrying a ``TOOL_MANIFEST`` entry plus a
test file. It does NOT assert subjective LLM quality — a human judges the authored loader from the
logged outcome against the settled E5 rubric: 13 spectra across 7 data groups — IXPE DU1-3 = data
groups 1-3 (I/Q/U as sources 1/2/3), NICER x2 = data groups 4-5 (merged mpu7 PHA), NuSTAR FPMA/FPMB
= data groups 6-7 — IXPE-first, Stokes I->ARF / Q,U->MRF, per-instrument bands.

The capability-only ``SCIENCE_CONTEXT`` / ``EXPECTED_BEHAVIOR`` / ``NOTES`` live here in the test,
never in a runtime prompt — so there is no eval leak into the generic prompts. Requires the analyst
science env (HEASoft/XSPEC) for the gate's Layer-B tests and live LLM credentials.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents.roles import AnalystLeadAgent
from auto_research.monitor import RunMonitor
from auto_research.sandbox.workspace import create_run_workspace, run_dir
from auto_research.schemas import AnalystReport, HumanResponse
from auto_research.schemas.toolspec import AnalystToolRequest, ValidationDataset

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_LOADER_AUTHORING_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_LOADER_AUTHORING_TESTS=1 to run the live loader-authoring test",
)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "P2" / "loader-test"

# Capability + requirements only — NO load_data source, signature, or implementation mechanics.
SCIENCE_CONTEXT = (
    "Joint broadband + polarization spectral fitting of Cyg X-1 needs all of an observing epoch's "
    "spectra loaded into a single XSPEC session as one fittable joint dataset. The toolkit has no "
    "such loader, so the analysis cannot begin. An epoch spans three instruments: IXPE (X-ray "
    "polarimetry — each of its detector units yields a Stokes I, a Stokes Q, and a Stokes U "
    "spectrum), NICER, and NuSTAR (FPMA and FPMB). Each instrument has its own usable energy band "
    "and its own response and background conventions."
)
EXPECTED_BEHAVIOR = (
    "A function that, given an epoch's per-instrument observation IDs and the mission data "
    "directories, loads every spectrum of the epoch into XSPEC as one joint dataset and returns a "
    "structured map of the resulting XSPEC spectrum indices per instrument and detector. "
    "Requirements (the data-group layout is SETTLED — the analyst and the human debated and fixed "
    "it): (1) one XSPEC data group per IXPE detector unit — that DU's Stokes I, Q, U load as sources "
    "1, 2, 3 within its data group; one data group per NICER observation (a single merged mpu7 source "
    "PHA, no per-detector splitting); one data group per NuSTAR FPM (FPMA and FPMB each its own). IXPE "
    "data groups lead the numbering (data groups 1..N_DU), then NICER, then NuSTAR — the leading data "
    "groups must be the IXPE I/Q/U groups, and each detector/observation owns its group so its "
    "cross-calibration tunes independently; (2) for EVERY loaded spectrum attach its MATCHING response "
    "and background — the "
    "correct RMF, the correct effective-area response (and note that IXPE's Stokes Q and U spectra "
    "use the modulation response, not the ordinary ancillary response), and the background spectrum "
    "wherever one exists for that instrument; (3) restrict each instrument to its usable energy band "
    "via caller-overridable per-instrument energy-range arguments. The loaded result must match the "
    "epoch's reference dataset spectrum-for-spectrum, including the response and background attached "
    "to each."
)
NOTES = (
    "Validation epoch (call it E5): IXPE obs 02008401, NICER obs 6643010101 and 6643010102, NuSTAR "
    "obs 80902318002, all under the configured mission data directories. Settled E5 layout: 13 "
    "spectra across 7 data groups — IXPE DU1/DU2/DU3 are data groups 1-3 (each carrying Stokes "
    "I->ARF, Q,U->MRF, and its background), the two NICER observations are data groups 4-5 (the merged "
    "mpu7 source PHA + RMF + ARF + background each), and NuSTAR FPMA/FPMB are data groups 6-7 (grouped "
    "source PHA + RMF + ARF per FPM, background where present); IXPE leads. Suggested checks "
    "(advisory): loading E5 reproduces that 13-spectrum / 7-data-group layout with the correct "
    "RMF/response/background attached to each; and a re-run loads an identical dataset. Reduction "
    "products are already on disk for this epoch."
)

# The empty from-scratch scaffold the lead authors into (standard two-halves layout, §9.2).
_INIT_PY = '''\
"""joint_utils — domain toolkit package (empty scaffold for a live authoring test).

Mirrors the standard two-halves layout: human-half lib/ + ai-half skills/ (TOOL_MANIFEST).
Re-exports TOOL_MANIFEST for ToolkitRegistry discovery and puts lib/ on sys.path.
"""
from __future__ import annotations
import sys
from pathlib import Path

_lib = Path(__file__).parent / "lib"
if str(_lib) not in sys.path:
    sys.path.insert(0, str(_lib))

from .skills import TOOL_MANIFEST  # noqa: E402

__all__ = ["TOOL_MANIFEST"]
'''
_SKILLS_INIT_PY = '''\
"""joint_utils.skills — the ai-half tool interface. Pure data; no science imports here
so ToolkitRegistry can load it in the orchestration env. lib/ holds the human-half code."""
from __future__ import annotations

TOOL_MANIFEST: dict = {}
'''
_SESSION_PY = (
    '"""joint_utils.skills.session — science-stack-dependent wrappers (REPL subprocess only)."""\n'
    "from __future__ import annotations\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _scaffold_fake_toolkit(tmp_path: Path) -> Path:
    """Build a throwaway, empty ``joint_utils`` toolkit on a fresh ``ai`` branch under ``tmp_path``
    (which lives under ``/tmp`` — inside the coder's fs-jail allow-list). The lead must author the
    loader from scratch; only the scaffold (no ``lib/`` code, empty ``TOOL_MANIFEST``) exists."""
    root = tmp_path / "ar_faketk_loader"
    pkg = root / "joint_utils"
    (pkg / "lib").mkdir(parents=True)
    (pkg / "skills").mkdir()
    (pkg / "tests").mkdir()
    # Mirror the real research_toolkit's .gitignore so auto-generated bytecode the coder
    # produces by running its own code stays out of git's view — and therefore out of the
    # gate's `git status` enumeration (the gate already respects .gitignore; the real toolkit
    # ships this same ignore, so without it here the fixture diverged from production). [C3]
    (root / ".gitignore").write_text("*/__pycache__/\n*.pyc\n")
    (pkg / "__init__.py").write_text(_INIT_PY)
    (pkg / "lib" / ".gitkeep").write_text("")
    (pkg / "skills" / "__init__.py").write_text(_SKILLS_INIT_PY)
    (pkg / "skills" / "session.py").write_text(_SESSION_PY)
    (pkg / "tests" / ".gitkeep").write_text("")

    _git(root, "init", "-q", "-b", "ai")
    _git(root, "config", "user.name", "auto_research")
    _git(root, "config", "user.email", "auto-research@localhost")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "scaffold empty joint_utils for live authoring test")
    return root


def _make_handler(monitor: RunMonitor):
    """Self-contained in-process human handler: answers the lead's design questions
    permissively-but-honestly and logs each to the monitor (no Slack/CLI human needed)."""

    async def human_handler(query):
        await monitor.record(
            "hitl", "human.query", level=2,
            summary=getattr(query, "headline", "human query"),
            data={"kind": getattr(query, "kind", "?"), "message": getattr(query, "message", "")},
        )
        answer = (
            "Proceed with your proposed design. Validate on epoch E5 — IXPE 02008401, NICER "
            "6643010101 and 6643010102, NuSTAR 80902318002, under the configured mission "
            "directories (/data/fzhou/XSPEC/Cyg_X-1/{IXPE,NICER,NuSTAR}). Attach the correct "
            "response and background to each spectrum, IXPE first. An honest failure is an "
            "acceptable outcome; never fabricate a loaded result."
        )
        return HumanResponse(query_id=query.id, answer=answer)

    return human_handler


async def _run_authoring(fake_tk: Path, ts: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = LOG_DIR / f"loader_authoring_{ts}_monitor.jsonl"
    monitor = RunMonitor(chatter=5, jsonl_path=jsonl, stream=sys.stdout)
    handler = _make_handler(monitor)
    run_id = f"loadertest_{ts}"
    create_run_workspace(run_id, ["analyst_lead"])

    lead = AnalystLeadAgent(monitor=monitor)
    lead.tools = ReadOnlyToolProvider(
        roots=[run_dir(run_id), fake_tk], include_data_catalog=True,
        include_plan_and_debate=True, include_text_search=False,
        human_handler=handler, asked_by="analyst_lead", model="",
    )

    request = AnalystToolRequest(
        request_kind="new_capability",
        science_context=SCIENCE_CONTEXT,
        expected_behavior=EXPECTED_BEHAVIOR,
        proposed_tool_name="load_epoch_spectra",
        validation_data=[
            ValidationDataset(mission="IXPE", obsid="02008401"),
            ValidationDataset(mission="NICER", obsid="6643010101"),
            ValidationDataset(mission="NICER", obsid="6643010102"),
            ValidationDataset(mission="NuSTAR", obsid="80902318002"),
        ],
        notes=NOTES,
    )
    report = AnalystReport(
        summary="Cannot start the joint fit: no tool loads an epoch's multi-instrument spectra.",
        findings=["No loader exists for joint IXPE+NICER+NuSTAR spectra; analysis is blocked."],
        tool_request=request, status="needs_human",
    )

    await monitor.record("run", "run.start", level=1, summary="loader authoring live test",
                         data={"toolkit_root": str(fake_tk)})
    decision = await lead.decide_authoring([report])
    await monitor.record(
        "analyst", "analyst.lead.authoring_decision", level=1,
        summary=f"should_author={decision.should_author}", data={"decision": decision},
    )
    if not decision.should_author or decision.tool_spec is None:
        monitor.write_markdown(jsonl.with_suffix(".md"))
        return decision, None

    result = await lead.drive_authoring(
        decision.tool_spec,
        toolkit_root=fake_tk,
        worktree_base=run_dir(run_id) / "authoring",
        direct_branch="ai",
        colead=None,  # resolves the GPT co-lead from config, decorrelated from the Claude author
        human_handler=handler,
    )
    await monitor.record(
        "analyst", "analyst.tool.authored", level=1,
        summary=f"authoring {result.status}", data={"result": result},
    )
    monitor.write_markdown(jsonl.with_suffix(".md"))
    return decision, result


def test_loader_authoring_end_to_end(tmp_path):
    """Drive the full lead-driven authoring path on a from-scratch toolkit and verify it reaches a
    terminal outcome without crashing. Observational: the authored tool is judged by a human from
    the logged JSONL/MD against the E5 rubric."""
    fake_tk = _scaffold_fake_toolkit(tmp_path)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")

    decision, result = asyncio.run(_run_authoring(fake_tk, ts))

    print(f"\n=== AUTHORING DECISION === should_author={decision.should_author}")
    assert decision.should_author and decision.tool_spec is not None, (
        f"lead declined to author: {decision.rationale}"
    )

    print(f"=== AUTHORING RESULT === status={result.status} "
          f"sha={result.commit_sha} reason={result.reason}")
    assert result.status in {"committed", "failed", "needs_human"}, result.status

    if result.status == "committed":
        # One atomic commit landed on ai carrying the ai-half (manifest entry) + a test file.
        log = subprocess.run(
            ["git", "-C", str(fake_tk), "log", "--oneline", "ai"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        assert len(log) >= 2, f"expected a scaffold commit + the authored commit, got: {log}"
        assert result.commit_sha is not None
        manifest = (fake_tk / "joint_utils" / "skills" / "__init__.py").read_text()
        assert "load_epoch_spectra" in manifest, "TOOL_MANIFEST missing the authored tool"
        tests = list((fake_tk / "joint_utils" / "tests").glob("test_*.py"))
        assert tests, "no Layer-B test file committed beside the tool"
