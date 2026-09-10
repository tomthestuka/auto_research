"""Env-gated LIVE end-to-end test of the lead-driven tool-authoring path (Phase-2 step 7).

Activate with:
    AUTO_RESEARCH_LIVE_LOADER_AUTHORING_TESTS=1 pytest tests/test_live_loader_authoring.py -q -s

Poses a missing-capability request — a joint multi-instrument spectral *loader*,
``load_epoch_spectra`` — to the analyst lead against a **throwaway, empty from-scratch toolkit**
scaffolded in a temp dir on a fresh ``ai`` branch (never the live ``toolkit_root``). The lead must
author it through the whole A1 flow: one ``run_cycle`` in which it files ``author_chain`` and
``_author_chain`` drives each link (coder -> deterministic gate -> provider-decorrelated co-lead
review -> atomic commit), with NO
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
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from auto_research.graph.runtime import readonly_tools_for
from auto_research.agents.roles import AnalystLeadAgent
from auto_research.monitor import RunMonitor
from auto_research.sandbox.workspace import create_run_workspace, run_dir
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    HumanResponse,
    MergeFlowResult,
)
from auto_research.schemas.toolspec import AnalystToolRequest, ValidationDataset
from tests.seats import SeatOverride, from_seat, repo_config_dir, seat, seated

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
    # Production's own factory, not a look-alike (the 2026-09-08 seams lesson): getting one of
    # these flags wrong does not fail, it silently measures a role the pipeline does not have.
    # `readonly_tools_for` derives `include_plan_and_debate` from the handler, so passing the
    # handler is what opens the design channel — the same derivation `run_research` uses.
    lead.tools = readonly_tools_for(
        "analyst_lead", roots=[run_dir(run_id), fake_tk], human_handler=handler,
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
        tool_request=request, status="failed",
    )

    await monitor.record("run", "run.start", level=1, summary="loader authoring live test",
                         data={"toolkit_root": str(fake_tk)})
    # ONE cycle does the whole thing: the lead triages, files `author_chain`, and drives each link
    # through the coder + gate + co-lead review — all in its own conversation, with the coding
    # middle dropped between links (A8; the sequencing used to live in this test's own loop).
    outcome = await lead.run_cycle(
        _task_for_report(report),
        [report],
        sources=["analyst_worker_0"],
        rounds_left=0,
        author_budget=3,
        toolkit_root=fake_tk,
        worktree_base=run_dir(run_id) / "authoring",
        direct_branch="ai",
        colead=None,  # resolves the GPT co-lead from config, decorrelated from the author
        human_handler=handler,
    )
    await monitor.record(
        "analyst", "analyst.lead.cycle", level=1,
        summary=f"filed={outcome.filed} committed={outcome.committed}",
        data={"filed": outcome.filed, "decision": outcome.decision},
    )
    monitor.write_markdown(jsonl.with_suffix(".md"))
    result = outcome.authored[-1] if outcome.authored else None
    return outcome.decision, result


def _task_for_report(report) -> AnalysisTask:
    """The analysis task the worker's blocked report belongs to."""
    return AnalysisTask(
        question=(
            "Run the joint IXPE+NICER+NuSTAR spectro-polarimetric fit for the E5 epoch of "
            "Cyg X-1."
        ),
        instructions="The worker is blocked on a missing loader; settle the toolkit question.",
    )


def test_loader_authoring_end_to_end(tmp_path):
    """Drive the full lead-driven authoring path on a from-scratch toolkit and verify it reaches a
    terminal outcome without crashing. Observational: the authored tool is judged by a human from
    the logged JSONL/MD against the E5 rubric."""
    fake_tk = _scaffold_fake_toolkit(tmp_path)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")

    decision, result = asyncio.run(_run_authoring(fake_tk, ts))

    assert decision is not None, "the lead ended its cycle without filing an authoring form"
    print(f"\n=== AUTHORING DECISION === should_author={decision.should_author} "
          f"chain={[s.proposed_tool_name for s in decision.tool_specs]}")
    assert decision.should_author and decision.tool_specs, (
        f"lead declined to author: {decision.rationale}"
    )
    assert result is not None, "author_chain was filed but no link was driven"

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


# ── A8: chain-planning (the planning stage only — cheap, no coder/gate/science env) ──────────
# A multi-step prescription that genuinely needs several tools the empty toolkit lacks. The
# capability text lives here (never in a runtime prompt — no eval leak). This drives ONLY the real
# lead's planning stage to validate it plans an ORDERED CHAIN of ≥2 tool_specs up front — the
# core A8 behavior — without the expensive Sonnet-coder / gate / GPT-review path.
_CHAIN_SCIENCE_CONTEXT = (
    "A broadband polarization-vs-hardness survey of a black-hole X-ray binary cannot begin: the "
    "toolkit exposes no way to (a) reduce an IXPE observation's per-detector event files into a "
    "broadband polarization cube (PD, PA, MDP99), (b) extract background-subtracted Stokes-I source "
    "and background spectra and compute a hardness ratio from them, or (c) render the resulting "
    "PD-vs-hardness relation as a reviewable figure. None of these capabilities exist, so the whole "
    "survey pipeline is blocked at the first step."
)
_CHAIN_EXPECTED_BEHAVIOR = (
    "The analysis prescription runs in dependency order: STEP 1 reduce each observation to a "
    "broadband (2-8 keV) PCUBE and read PD/PA/MDP99; STEP 2 extract source+background Stokes-I "
    "spectra and compute a background-subtracted hardness ratio HR with errors; STEP 3 classify "
    "detections vs upper limits and compute the PD-HR correlation across the sample; STEP 4 render "
    "the PD-vs-HR scatter (with upper limits) as a figure the reviewer can see. Each step depends on "
    "the previous step's product, so the missing capabilities form a chain."
)
_CHAIN_NOTES = (
    "Validation ObsID 01002901 (IXPE), reduction products already on disk. The gap the worker hit "
    "first is the broadband PCUBE reduction (STEP 1); the downstream hardness-ratio, correlation, and "
    "plotting capabilities are equally absent and needed to complete the survey."
)


def test_chain_planning_returns_ordered_chain(tmp_path, monkeypatch):
    """A8: given a multi-step prescription against an empty toolkit, the real lead plans an ORDERED
    CHAIN of ≥2 tool briefs up front (not a single first-gap tool). Observational — a human
    eyeballs the chain's names/dispositions/order from the logged MD; the deterministic assertion
    is only that a multi-tool chain is planned.

    Planning is now one branch of the lead's cycle, and filing ``author_chain`` opens the coding
    session on the first link — so the coding session is stubbed here to keep this test what it has
    always been: the *plan*, at planning cost, with no coder, gate or science environment.
    """
    from dotenv import load_dotenv

    load_dotenv()  # surface live LLM credentials (never read .env; just load it), as other live tests do
    fake_tk = _scaffold_fake_toolkit(tmp_path)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = LOG_DIR / f"chain_planning_{ts}_monitor.jsonl"
    monitor = RunMonitor(chatter=5, jsonl_path=jsonl, stream=sys.stdout)
    run_id = f"chaintest_{ts}"
    create_run_workspace(run_id, ["analyst_lead"])

    # A design-agnostic handler: proceed with the lead's OWN proposed design (no capability-specific
    # steer that would bias the chain — the loader test's E5/response-background steer does not fit a
    # PD-hardness survey and would inject an off-topic tool).
    async def neutral_handler(query):
        await monitor.record(
            "hitl", "human.query", level=2,
            summary=getattr(query, "headline", "human query"),
            data={"kind": getattr(query, "kind", "?"), "message": getattr(query, "message", "")},
        )
        return HumanResponse(
            query_id=query.id,
            answer=(
                "Proceed with your proposed design. Validate on the provided ObsID (IXPE 01002901). "
                "An honest failure is an acceptable outcome; never fabricate a result."
            ),
        )

    lead = AnalystLeadAgent(monitor=monitor)
    lead.tools = readonly_tools_for(
        "analyst_lead", roots=[run_dir(run_id), fake_tk], human_handler=neutral_handler,
    )
    request = AnalystToolRequest(
        request_kind="new_capability",
        science_context=_CHAIN_SCIENCE_CONTEXT,
        expected_behavior=_CHAIN_EXPECTED_BEHAVIOR,
        proposed_tool_name="reduce_broadband_pcube",
        validation_data=[ValidationDataset(mission="IXPE", obsid="01002901")],
        notes=_CHAIN_NOTES,
    )
    report = AnalystReport(
        summary="Survey blocked: no broadband PCUBE reduction (and no downstream HR/stats/plot tools).",
        findings=["The broadband PD-hardness survey pipeline has no tools for any of its steps."],
        tool_request=request, status="failed",
    )

    async def _fake_link(self, messages, provider, spec, **kwargs):
        return MergeFlowResult(
            tool_spec_id=spec.id, status="committed", branch="ai", commit_sha="0" * 12,
            reason="stubbed coding session (this test measures the plan, not the coder)",
        )

    monkeypatch.setattr(AnalystLeadAgent, "_drive_one_link", _fake_link)

    async def _plan():
        await monitor.record("run", "run.start", level=1, summary="A8 chain-planning live test")
        outcome = await lead.run_cycle(
            AnalysisTask(
                question="Survey broadband polarization vs hardness across the sample.",
                instructions="The worker is blocked at the first step; settle the toolkit question.",
            ),
            [report],
            sources=["analyst_worker_0"],
            rounds_left=0,
            author_budget=8,
            toolkit_root=fake_tk,
            worktree_base=run_dir(run_id) / "authoring",
            direct_branch="ai",
        )
        await monitor.record(
            "analyst", "analyst.lead.cycle", level=1,
            summary=f"filed={outcome.filed}", data={"decision": outcome.decision},
        )
        monitor.write_markdown(jsonl.with_suffix(".md"))
        return outcome.decision

    decision = asyncio.run(_plan())
    assert decision is not None, "the lead ended its cycle without filing an authoring form"
    names = [(s.proposed_tool_name, s.disposition) for s in decision.tool_specs]
    print(f"\n=== A8 CHAIN === should_author={decision.should_author} chain={names}")
    print(f"rationale: {decision.rationale}")

    assert decision.should_author, f"lead declined to author: {decision.rationale}"
    assert len(decision.tool_specs) >= 2, (
        f"A8 expects an up-front multi-tool chain for a multi-step prescription; got {names}"
    )


# ── A8: chain authoring EXECUTION, through the lead's own cycle ──────────────────────────────
# This drives the **production** seam: `AnalystLeadAgent.run_cycle`, the one loop a real analysis
# runs, in which the lead reads the workers' reports, files `author_chain` itself, and `_author_chain`
# drives each link. The earlier version of this test looped the links itself and called
# `drive_authoring` per spec — a driver production no longer uses — so three behaviours could not be
# observed at all: a failed link continuing the chain (F9), the stage machinery that decides which
# form is open ([A20]), and the silent-turn classification on the loop that carries `terminal_tools`.
# Measuring the wrong driver is the 2026-09-08 seams lesson at a second address.
#
# Runs against a COPY of the real research_toolkit under /tmp (inside the coder's fs-jail), never the
# live `toolkit_root`, and **through the configuration pointer**: the coder seat is written into a
# faked configuration directory and resolved by the ordinary registry, so the run states what it
# measured instead of inheriting it.
#
# Human channel OFF (unattended): `human_handler=None`, so `readonly_tools_for` withholds the design
# channel — the derivation production makes, not a flag set by hand here.
_UNATTENDED = (
    "UNATTENDED SESSION — no human is available. Do not use plan_and_debate or ask_human, and "
    "ignore any instruction to debate the design with a human; proceed with this brief as-is and "
    "author the tool directly. An honest failure is acceptable; never fabricate a result."
)

#: Which author harness this run measures. Absent, the configured coder is used unchanged — still
#: through the pointer, so the written `models.yaml` records the seat either way.
CODER_HARNESS_ENV = "AUTO_RESEARCH_CODER_HARNESS"

#: Authoring credits for the cycle. Two links plus one, so a rejected link can be re-attempted and
#: F9's continue-the-chain behaviour is reachable rather than cut off by the budget.
_AUTHOR_BUDGET = 3


def _toolkit_root_from_config() -> Path:
    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "orchestration.yaml").read_text()
    )
    return Path(cfg["toolkit_root"])


def _copy_real_toolkit(tmp_path: Path) -> Path:
    """Copy the real research_toolkit tree (no .git/bytecode/logs) into /tmp on a fresh ``ai`` branch,
    so the coder authors into a realistic toolkit (xspec_utils as its template) without touching the
    live checkout."""
    src = _toolkit_root_from_config()
    dst = tmp_path / "research_toolkit_copy"
    shutil.copytree(
        src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "logs")
    )
    _git(dst, "init", "-q", "-b", "ai")
    _git(dst, "config", "user.name", "auto_research")
    _git(dst, "config", "user.email", "auto-research@localhost")
    _git(dst, "add", "-A")
    _git(dst, "commit", "-q", "-m", "seed copy of research_toolkit for chain-authoring live test")
    return dst


def _coder_seat() -> SeatOverride:
    """The coder seat to write into the faked configuration.

    ``AUTO_RESEARCH_CODER_HARNESS=codex`` measures the campaign's own author (the seat
    ``science_bench`` pins, carried whole so the test cannot drift from the batch);
    ``claude_sdk`` measures the other backend. Absent, the repository's configured coder is
    re-declared unchanged — the point being that the configuration is *written*, so the log says
    which author produced the result.
    """
    choice = os.getenv(CODER_HARNESS_ENV, "").strip().lower()
    if choice == "codex":
        from benchmarks.science_bench import arms

        # `replace`, not attribute assignment: SeatOverride is a frozen dataclass.
        return replace(
            from_seat(arms.CODER),
            harness=arms.CODER_HARNESS,
            key_env=arms.CODER_KEY_ENV,
            base_url=arms.CODER_BASE_URL,
        )
    if choice == "claude_sdk":
        return seat(
            "anthropic", "claude-sonnet-5",
            harness="claude_sdk", vision=False, documents=False,
        )
    if choice:
        raise ValueError(f"{CODER_HARNESS_ENV} takes 'codex' or 'claude_sdk', got {choice!r}")
    configured = (
        yaml.safe_load((repo_config_dir() / "models.yaml").read_text())["roles"]["coder"]["members"][0]
    )
    return SeatOverride(
        provider=configured["provider"],
        model=configured["model"],
        vision=bool(configured.get("vision", False)),
        documents=bool(configured.get("documents", False)),
        parameters=dict(configured.get("parameters") or {}),
        priceMTk=dict(configured.get("priceMTk") or {}),
        harness=configured.get("harness"),
        base_url=configured.get("base_url"),
        key_env=configured.get("key_env"),
    )


def _worker_tool_request() -> AnalystToolRequest:
    """The missing capability a worker raises. Two dependent pieces, stated as capability and
    numeric contract only — the decomposition into chain links is the lead's call (§9.3), which is
    exactly what this test leaves to it."""
    return AnalystToolRequest(
        request_kind="new_capability",
        science_context=(
            "Summarising per-segment measurements needs two things the toolkit does not have: a "
            "standalone weighted-mean-plus-constancy statistic over values with 1-sigma errors, and "
            "a table step that applies it to several labelled measurement sets and reports a reduced "
            "chi-squared per set. The second depends on the first and must reuse it rather than "
            "recompute the statistic. " + _UNATTENDED
        ),
        expected_behavior=(
            "A pure-numpy human-half entry point weighted_mean(values, errors) -> dict (imports numpy "
            "ONLY — no xspec, no ixpeobssim, no matplotlib), where w_i = 1/err_i**2; "
            "weighted_mean = sum(w*v)/sum(w); weighted_mean_err = sqrt(1/sum(w)); "
            "chi2 = sum((v - weighted_mean)**2 / err**2); dof = len(values) - 1; returning exactly the "
            "float/int keys 'weighted_mean', 'weighted_mean_err', 'chi2', 'dof', and raising ValueError "
            "on empty input, on a length mismatch, or if any error <= 0. On top of it, "
            "summarize(datasets) -> list[dict] over a list of {'label': str, 'values': [...], "
            "'errors': [...]}, returning {'label', 'weighted_mean', 'weighted_mean_err', 'chi2', 'dof', "
            "'reduced_chi2'} per input row in input order, with reduced_chi2 = chi2/dof when dof > 0 "
            "else float('nan'), computed by CALLING the weighted-mean entry point rather than "
            "reimplementing it. Both belong in xspec_utils and both need a Layer-B test file; the "
            "second one's test must assert its 'weighted_mean' agrees with the first's on the same "
            "input, which is what makes the reuse checkable. Known-input check for the first: "
            "values=[1.0,2.0,3.0], errors=[1.0,1.0,1.0] gives weighted_mean 2.0, chi2 2.0, dof 2 "
            "(within 1e-9)."
        ),
        proposed_tool_name="seq_weighted_mean",
        validation_data=[ValidationDataset(mission="IXPE", obsid="01002901")],
        notes=_UNATTENDED,
    )


def _reports_with_request() -> list[AnalystReport]:
    """Two worker reports, one carrying the tool request. ``authoring_possible`` is derived from a
    report carrying one (`roles.run_cycle`), so this is what opens the authoring stage."""
    return [
        AnalystReport(
            summary=(
                "Computed the per-segment values and their 1-sigma errors, then stopped: combining "
                "them needs a weighted mean with a constancy statistic, and no toolkit tool exposes "
                "one. Reporting the segments rather than an average computed by hand."
            ),
            findings=[
                "the per-segment measurements and their errors are in hand",
                "no toolkit tool combines them with a constancy statistic",
            ],
            uncertainty="the combined value and its reduced chi-squared are not available",
            status="failed",
            tool_request=_worker_tool_request(),
        ),
        AnalystReport(
            summary=(
                "Agrees the per-segment values are sound and that the combination step is missing. "
                "A table over several labelled sets is what the analysis actually needs."
            ),
            findings=["the segment-level results are reproducible"],
            status="succeeded",
        ),
    ]


def _ledger(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _failed_coder_turns(events: list[dict]) -> list[dict]:
    """Coder turns the harness recorded as failed, on either backend's vocabulary."""
    failed = []
    for event in events:
        if event.get("event") != "coder.turn_end":
            continue
        data = event.get("data") or {}
        status = str(data.get("status") or "")
        if data.get("is_error") or "fail" in status.lower() or "abort" in status.lower():
            failed.append(event)
    return failed


def _drive_coder_results(events: list[dict]) -> list[dict]:
    return [
        event for event in events
        if event.get("event", "").endswith(".tool_result")
        and (event.get("data") or {}).get("tool") == "drive_coder"
    ]


def _layer_b_skips(events: list[dict]) -> list[tuple[int, str]]:
    """Every ``N skipped`` pytest summary the coder saw, with its sequence number.

    Recorded, never asserted. Layer B passes on ``rc == 0`` and at least one PASSED, so a test that
    skips at runtime — a data guard the coder wrote itself — leaves the gate truthfully reporting
    that everything which ran, passed, while the check the brief commissioned never executed. The
    live run of 2026-09-09 committed nothing on exactly this shape and the skip appears in no gate
    field, so the count is written down here until the gate reports it.
    """
    found = []
    for event in events:
        text = str((event.get("data") or {}).get("text") or "")
        for line in text.splitlines():
            if "skipped" in line and ("passed" in line or "failed" in line):
                found.append((int(event.get("sequence", 0)), line.strip()))
    return found


def test_lead_cycle_authors_a_chain_end_to_end(tmp_path):
    """One real lead cycle, from two worker reports to a filed form, with the authoring chain driven
    by production's own `_author_chain`.

    What is asserted is what the **harness** must do regardless of how well any model performs: the
    cycle ends on a filed form rather than a silent stop; every commit it claims is reachable in the
    repository and carries a manifest entry plus a test file; every planned link is attempted, so a
    rejected link does not end the chain (F9); a failed coder turn is never delivered to the lead as
    a successful reply; and the tree is left clean when nothing was banked (F1). Model *judgment* —
    whether a co-lead approves a given tool — is recorded, never asserted (a rejection on a real
    science point is a correct outcome, not a test failure).
    """
    from dotenv import load_dotenv

    load_dotenv()  # surface live LLM credentials (never read .env; just load it)
    tk = _copy_real_toolkit(tmp_path)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = LOG_DIR / f"chain_cycle_{ts}_monitor.jsonl"
    monitor = RunMonitor(chatter=5, jsonl_path=jsonl, stream=sys.stdout)
    run_id = f"chaintest_{ts}"
    create_run_workspace(run_id, ["analyst_lead"])
    reports = _reports_with_request()

    async def _drive(config_dir: Path):
        lead = AnalystLeadAgent(monitor=monitor)
        lead.tools = readonly_tools_for("analyst_lead", roots=[run_dir(run_id), tk])
        await monitor.record(
            "run", "run.start", level=1, summary="lead-cycle chain authoring live test",
            data={
                "toolkit_root": str(tk),
                # The configuration this run resolved, not the one it meant to: the seat is read
                # back out of the written file.
                "coder": yaml.safe_load(
                    (config_dir / "models.yaml").read_text()
                )["roles"]["coder"]["members"][0],
            },
        )
        outcome = await lead.run_cycle(
            AnalysisTask(
                question=(
                    "Combine the per-segment measurements into one value per labelled set, with a "
                    "statistic that says whether each set is consistent with being constant."
                ),
                instructions=_UNATTENDED,
            ),
            reports,
            sources=["worker_0", "worker_1"],
            rounds_left=0,
            author_budget=_AUTHOR_BUDGET,
            toolkit_root=tk,
            worktree_base=run_dir(run_id) / "authoring",
            direct_branch="ai",
            colead=None,
            human_handler=None,
        )
        await monitor.record(
            "analyst", "analyst.lead.cycle", level=1,
            summary=f"Lead filed {outcome.filed or 'nothing'}",
            data={
                "filed": outcome.filed,
                "committed": outcome.committed,
                "author_budget": outcome.author_budget,
                "authored": [r.model_dump(mode="json") for r in outcome.authored],
            },
        )
        monitor.write_markdown(jsonl.with_suffix(".md"))
        return outcome

    # Every live test resolves its models through the configuration pointer, so a seat is a written
    # file the registry reads rather than a handle built beside the pipeline.
    with seated(coder=_coder_seat()) as config_dir:
        outcome = asyncio.run(_drive(config_dir))

    events = _ledger(jsonl)
    planned = list(outcome.decision.tool_specs) if outcome.decision is not None else []
    attempted = list(outcome.authored)
    statuses = [(r.tool_spec_id, r.status, r.reason) for r in attempted]
    skips = _layer_b_skips(events)
    print(
        f"\n=== LEAD-CYCLE CHAIN AUTHORING ===\n"
        f"filed={outcome.filed} committed={outcome.committed} "
        f"budget_left={outcome.author_budget}\n"
        f"planned={[s.proposed_tool_name for s in planned]}\n"
        f"attempted={statuses}\n"
        f"layer_b_skips={skips}"
    )

    # 1. The cycle ends by filing a form. A cycle that stops without one is the failure the caller
    #    treats as such (`LeadCycleOutcome.filed is None`), and it is the state that leaves no
    #    record of why the lead stopped.
    assert outcome.filed is not None, "the lead ended its cycle without filing any form"

    # 2. A failed coder turn is never handed to the lead as a successful reply. The Codex backend
    #    injects the vendor's own message into the returned text; a backend that does not is what
    #    let a lead spend three drives steering a dead session on 2026-09-09.
    failed_turns = _failed_coder_turns(events)
    if failed_turns:
        flagged = [
            result for result in _drive_coder_results(events)
            if (result.get("data") or {}).get("is_error")
            or "[harness error]" in str((result.get("data") or {}).get("text") or "")
        ]
        assert flagged, (
            f"{len(failed_turns)} coder turn(s) failed and not one reached the lead as an error "
            f"or carried a harness-error marker: "
            f"{[e.get('sequence') for e in failed_turns]}"
        )

    # 3. Every planned link is attempted: a link the co-lead rejects tells the lead why and the
    #    chain continues (F9), rather than the chain ending on the first non-commit. Credits are the
    #    only thing that may cut it short, so the exhausted case is excluded rather than assumed away.
    if planned and outcome.author_budget > 0:
        assert len(attempted) == len(planned), (
            f"the chain stopped early with credits left: planned {len(planned)}, "
            f"attempted {len(attempted)}, budget {outcome.author_budget}"
        )

    # 4. Every commit the run claims is in the repository, and carries what makes a tool callable.
    reachable = subprocess.run(
        ["git", "-C", str(tk), "rev-list", "--all"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    manifest = (tk / "xspec_utils" / "skills" / "__init__.py").read_text()
    for result in attempted:
        if result.status != "committed":
            continue
        assert result.commit_sha, f"a committed link carries no SHA: {result}"
        assert any(sha.startswith(result.commit_sha) or result.commit_sha.startswith(sha)
                   for sha in reachable), (
            f"commit {result.commit_sha} is recorded but unreachable in the repository"
        )
    committed_names = [
        s.proposed_tool_name for s, r in zip(planned, attempted) if r.status == "committed"
    ]
    for name in committed_names:
        assert name in manifest, f"{name} committed without a TOOL_MANIFEST entry"
        assert list((tk / "xspec_utils" / "tests").glob(f"test_{name}*.py")), (
            f"{name} committed without a Layer-B test file"
        )

    # 5. Nothing banked ⇒ the tree is clean: the failed attempt's edits were reset to the base ref
    #    (F1), so a rejected tool cannot surface to a re-running worker through `registry.reload()`.
    if not committed_names:
        dirty = subprocess.run(
            ["git", "-C", str(tk), "status", "--porcelain", "-uall"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert not dirty, f"a non-committed attempt left the tree dirty:\n{dirty}"

    # 6. The sequential guarantee, when the lead did plan and land both: the second tool's test
    #    imports the first's module, so its passing under the gate proves the first was committed and
    #    visible when the second link ran.
    if len(committed_names) >= 2:
        shas = [r.commit_sha for r in attempted if r.status == "committed"]
        assert len(set(shas)) == len(shas), f"expected distinct commits per tool, got {shas}"
