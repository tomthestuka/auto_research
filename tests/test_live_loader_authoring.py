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
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents.roles import AnalystLeadAgent
from auto_research.monitor import RunMonitor
from auto_research.sandbox.workspace import create_run_workspace, run_dir
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    HumanResponse,
    MergeFlowResult,
)
from auto_research.schemas.toolspec import AnalystToolRequest, ToolSpec, ValidationDataset

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


# ── A8: chain-planning (decide_authoring only — cheap, no coder/gate/science env) ────────────
# A multi-step prescription that genuinely needs several tools the empty toolkit lacks. The
# capability text lives here (never in a runtime prompt — no eval leak). This drives ONLY the real
# lead's decide_authoring to validate it plans an ORDERED CHAIN of ≥2 tool_specs up front — the
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
    lead.tools = ReadOnlyToolProvider(
        roots=[run_dir(run_id), fake_tk], include_data_catalog=True,
        include_plan_and_debate=True, include_text_search=False,
        human_handler=neutral_handler, asked_by="analyst_lead", model="",
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
        tool_request=request, status="needs_human",
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


# ── A8: sequential authoring EXECUTION (real coder → gate → review → commit, TWICE) ──────────
# Unlike the planning-only test above, this drives the actual authoring harness on TWO dependent
# tools back-to-back — exactly what DebateToConverge._maybe_author_and_resume does — to prove the
# harness can run sequential sessions bug-free: after tool 1 commits, tool 2's session starts and
# **sees tool 1's committed code**. Tool 2's Layer-B test IMPORTS tool 1's just-committed module, so
# tool 2 committing is itself the proof of sequential visibility. Runs against a COPY of the real
# research_toolkit under /tmp (inside the coder's fs-jail), never the live toolkit_root.
#
# Human channel OFF (unattended): human_handler=None (no plan_and_debate / ask_human tools) plus an
# explicit unattended instruction in each brief — the lead proceeds on its own without debate.
_UNATTENDED = (
    "UNATTENDED SESSION — no human is available. Do not use plan_and_debate or ask_human, and "
    "ignore any instruction to debate the design with a human; proceed with this brief as-is and "
    "author the tool directly. An honest failure is acceptable; never fabricate a result."
)


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
    _git(dst, "commit", "-q", "-m", "seed copy of research_toolkit for sequential-authoring live test")
    return dst


def _seq_tool_1() -> ToolSpec:
    return ToolSpec(
        request_kind="new_capability",
        science_context=(
            "A small pure-numpy building block is missing: the toolkit has no standalone weighted-mean "
            "+ constancy statistic over a list of measurements with 1-sigma errors. " + _UNATTENDED
        ),
        expected_behavior=(
            "Add a pure-numpy human-half module xspec_utils/lib/seq_weighted_mean.py (imports numpy "
            "ONLY — no xspec, no ixpeobssim, no matplotlib) exposing "
            "weighted_mean(values, errors) -> dict. Given two equal-length sequences (values and "
            "their 1-sigma errors): w_i = 1/err_i**2; weighted_mean = sum(w*v)/sum(w); "
            "weighted_mean_err = sqrt(1/sum(w)); chi2 = sum((v - weighted_mean)**2 / err**2); "
            "dof = len(values) - 1. Return a dict with exactly these float/int keys: "
            "'weighted_mean', 'weighted_mean_err', 'chi2', 'dof'. Raise ValueError on empty input, on "
            "a length mismatch, or if any error <= 0. Surface it as skills TOOL_MANIFEST key "
            "'seq_weighted_mean' (module 'xspec_utils.lib.seq_weighted_mean', fn 'weighted_mean'). "
            "Add xspec_utils/tests/test_seq_weighted_mean.py with: (1) a known-input->known-output "
            "check — values=[1.0,2.0,3.0], errors=[1.0,1.0,1.0] gives weighted_mean 2.0, chi2 2.0, "
            "dof 2 (assert within 1e-9); (2) a call-twice reproducibility check (identical dict); "
            "(3) a ValueError check for a non-positive error."
        ),
        proposed_tool_name="seq_weighted_mean",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="01002901")],
        notes=_UNATTENDED,
    )


def _seq_tool_2() -> ToolSpec:
    return ToolSpec(
        request_kind="new_capability",
        science_context=(
            "Downstream of seq_weighted_mean, the toolkit has no way to summarise several labelled "
            "measurement sets into one table of weighted means and reduced chi-squared. This tool "
            "MUST reuse the just-added seq_weighted_mean (do not recompute the statistic). " + _UNATTENDED
        ),
        expected_behavior=(
            "Add xspec_utils/lib/seq_weighted_mean_table.py (pure python + numpy; no xspec/matplotlib) "
            "that IMPORTS the already-committed helper: `from xspec_utils.lib.seq_weighted_mean import "
            "weighted_mean`. Expose summarize(datasets) -> list[dict], where datasets is a list of "
            "{'label': str, 'values': [...], 'errors': [...]}. For each dataset call weighted_mean(...) "
            "and return {'label', 'weighted_mean', 'weighted_mean_err', 'chi2', 'dof', 'reduced_chi2'} "
            "where reduced_chi2 = chi2/dof when dof > 0 else float('nan'). Preserve input order. Surface "
            "it as skills TOOL_MANIFEST key 'seq_weighted_mean_table' (module "
            "'xspec_utils.lib.seq_weighted_mean_table', fn 'summarize'). Add "
            "xspec_utils/tests/test_seq_weighted_mean_table.py that: imports summarize, runs it on two "
            "labelled datasets, asserts each row's reduced_chi2 == that row's chi2/dof, asserts the "
            "'weighted_mean' matches xspec_utils.lib.seq_weighted_mean.weighted_mean on the same input "
            "(so the tool genuinely reuses the committed helper), and includes a call-twice "
            "reproducibility check. Do NOT reimplement the weighted mean."
        ),
        proposed_tool_name="seq_weighted_mean_table",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="01002901")],
        notes=_UNATTENDED,
    )


def test_sequential_chain_authoring_end_to_end(tmp_path):
    """Drive the real authoring harness on TWO dependent tools sequentially (as
    ``_maybe_author_and_resume`` does) and verify it runs bug-free: both commit, as two distinct
    commits on ``ai``, and tool 2 — whose Layer-B test imports tool 1's just-committed module — only
    commits if its session saw tool 1's commit. Human channel OFF (unattended)."""
    from dotenv import load_dotenv

    load_dotenv()  # surface live LLM credentials (never read .env; just load it)
    tk = _copy_real_toolkit(tmp_path)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = LOG_DIR / f"seq_authoring_{ts}_monitor.jsonl"
    monitor = RunMonitor(chatter=5, jsonl_path=jsonl, stream=sys.stdout)
    run_id = f"seqtest_{ts}"
    create_run_workspace(run_id, ["analyst_lead"])

    lead = AnalystLeadAgent(monitor=monitor)
    lead.tools = ReadOnlyToolProvider(
        roots=[run_dir(run_id), tk], include_data_catalog=True,
        include_plan_and_debate=False,  # unattended: no human debate channel
        include_text_search=False, human_handler=None, asked_by="analyst_lead", model="",
    )
    specs = [_seq_tool_1(), _seq_tool_2()]

    async def _drive():
        await monitor.record(
            "run", "run.start", level=1, summary="sequential authoring live test",
            data={"toolkit_root": str(tk)},
        )
        results = []
        for i, spec in enumerate(specs, 1):
            r = await lead.drive_authoring(
                spec, toolkit_root=tk, worktree_base=run_dir(run_id) / "authoring",
                direct_branch="ai", colead=None, human_handler=None,
            )
            await monitor.record(
                "analyst", "analyst.tool.authored", level=1,
                summary=f"tool {i}/{len(specs)} {r.status}: {spec.proposed_tool_name}",
                data={"result": r},
            )
            results.append(r)
            if r.status != "committed":
                break  # chain stops on a non-commit (downstream depends on it)
        monitor.write_markdown(jsonl.with_suffix(".md"))
        return results

    results = asyncio.run(_drive())

    statuses = [(r.status, r.reason) for r in results]
    print(f"\n=== SEQUENTIAL AUTHORING === {statuses}")
    assert len(results) == 2 and all(r.status == "committed" for r in results), (
        f"sequential authoring did not complete both tools bug-free: {statuses}"
    )
    shas = [r.commit_sha for r in results]
    assert shas[0] and shas[1] and shas[0] != shas[1], f"expected two distinct commits, got {shas}"

    log = subprocess.run(
        ["git", "-C", str(tk), "log", "--oneline", "ai"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    assert len(log) >= 3, f"expected seed + two authored commits, got: {log}"
    manifest = (tk / "xspec_utils" / "skills" / "__init__.py").read_text()
    assert "seq_weighted_mean" in manifest and "seq_weighted_mean_table" in manifest, manifest
    # tool 2's committed test imports tool 1's module → its passing under the gate proves tool 1
    # was already committed and visible when tool 2's session ran (the sequential guarantee).
    tests = list((tk / "xspec_utils" / "tests").glob("test_seq_weighted_mean*.py"))
    assert len(tests) >= 2, f"expected both tools' Layer-B tests committed, got {tests}"
