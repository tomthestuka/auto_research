"""Opt-in live sandbox tests.

Activate with: AUTO_RESEARCH_LIVE_SANDBOX_TESTS=1

Test 1 — xspec_utils on local Cyg X-1 observations:
  Analyst agents self-direct to use xspec_utils from research_toolkit,
  run it against /data/fzhou/XSPEC/Cyg_X-1/, and produce an AnalystReport.
  Scope: one self-selected simultaneous epoch; continuum characterization only.

Test 2 — HEASARC retrieval tool (agent self-corrects):
  Analyst agents self-direct to call the human-authored HEASARC retrieval
  tool. If initial arguments are wrong the error-feedback loop self-corrects.
  Scope: NICER observations of Cyg X-1 since 2024-01-01.
  Requires the HEASARC retrieval tool to be registered in TOOL_MANIFEST.

Logs: logs/dev/step-11/
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

LIVE = os.getenv("AUTO_RESEARCH_LIVE_SANDBOX_TESTS") == "1"
skip_live = pytest.mark.skipif(not LIVE, reason="Set AUTO_RESEARCH_LIVE_SANDBOX_TESTS=1 to run")

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "step-11"


def _log(name: str, data: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    jsonl = LOG_DIR / f"{name}_{ts}.jsonl"
    md = LOG_DIR / f"{name}_{ts}.md"
    with jsonl.open("w") as f:
        json.dump(data, f, indent=2, default=str)
    with md.open("w") as f:
        f.write(f"# {name}\n\n")
        f.write(f"**Run:** {ts}\n\n")
        for k, v in data.items():
            f.write(f"## {k}\n\n```\n{v}\n```\n\n")
    print(f"Logs written to {LOG_DIR}")


def _monitor_paths(name: str) -> tuple[Path, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    jsonl = LOG_DIR / f"{name}_{ts}_monitor.jsonl"
    return jsonl, jsonl.with_suffix(".md")


@skip_live
def test_xspec_local() -> None:
    """Analyst agents fit the Cyg X-1 X-ray continuum for one self-selected epoch.

    No LLM guidance: agents self-direct based on the tool catalogue
    injected by tool_dispatch_node from ToolkitRegistry.
    """
    import asyncio
    from auto_research.graph.runtime import run_research
    from auto_research.schemas import ResearchRequest

    request = ResearchRequest(
        question=(
            "Select one simultaneous IXPE, NuSTAR, and NICER observation epoch of Cyg X-1, "
            "fit the broadband X-ray continuum, and compare the fitted parameters with "
            "published literature values."
        )
    )

    monitor_jsonl, monitor_md = _monitor_paths("test_xspec_local")
    run = asyncio.run(run_research(request, chatter=10, monitor_jsonl=monitor_jsonl))
    verdict = run.verdict
    run.monitor.write_markdown(monitor_md)

    log_data = {
        "request": request.question,
        "accepted": verdict.accepted,
        "rationale": verdict.rationale,
        "assign_to": verdict.assign_to,
        "monitor_jsonl": str(monitor_jsonl),
        "monitor_markdown": str(monitor_md),
    }
    _log("test_xspec_local", log_data)

    assert verdict is not None, "Expected a ReviewVerdict"


@skip_live
def test_heasarc_selfcorrect() -> None:
    """Analyst agents retrieve NICER Cyg X-1 observations from HEASARC since 2024-01-01.

    Requires the HEASARC retrieval tool to be registered in research_toolkit.
    If initial arguments are wrong, the error-feedback loop self-corrects.
    No LLM guidance: agents self-direct using the tool catalogue.
    """
    import asyncio
    from auto_research.graph.runtime import run_research
    from auto_research.schemas import ResearchRequest

    request = ResearchRequest(
        question=(
            "Retrieve NICER monitoring observations of Cyg X-1 from the HEASARC public "
            "archive since 2024-01-01, identify which are absent from the local database, "
            "and characterize the long-term timing behavior."
        )
    )

    monitor_jsonl, monitor_md = _monitor_paths("test_heasarc_selfcorrect")
    run = asyncio.run(run_research(request, chatter=10, monitor_jsonl=monitor_jsonl))
    verdict = run.verdict
    run.monitor.write_markdown(monitor_md)

    log_data = {
        "request": request.question,
        "accepted": verdict.accepted,
        "rationale": verdict.rationale,
        "assign_to": verdict.assign_to,
        "monitor_jsonl": str(monitor_jsonl),
        "monitor_markdown": str(monitor_md),
    }
    _log("test_heasarc_selfcorrect", log_data)

    assert verdict is not None, "Expected a ReviewVerdict"
