"""Full end-to-end live test with the persistent sandbox architecture.

Activate with: AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS=1

Runs the IXPE Cyg X-1 soft-state 2023-05-24 research question through the
complete Phase 1 graph: reviewer dispatch → librarian IO (search, ingest) →
analyst debate with persistent per-worker XSPEC sandboxes → reviewer verdict.

Logs: logs/dev/step-12/
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

LIVE = os.getenv("AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS") == "1"
skip_live = pytest.mark.skipif(not LIVE, reason="Set AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS=1 to run")

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "step-12"


def _monitor_paths(name: str) -> tuple[Path, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    jsonl = LOG_DIR / f"{name}_{ts}_monitor.jsonl"
    return jsonl, jsonl.with_suffix(".md")


@skip_live
def test_cyg_x1_ixpe_softstate() -> None:
    """Full Phase 1 run: IXPE Cyg X-1 soft-state 2023-05-24 broadband spectral fit.

    Exercises the complete stack with chatter=10:
    - LibrarianIO: compose search strategy, search arXiv/ADS, ingest PDFs via IODebateToConverge
    - Retrieval: text_search_paths() from SQLite
    - AnalystWorker: debate with persistent XSPEC sandbox, private memory.md per worker
    - AnalystLead: convergence judge and adjudication
    - Reviewer: literature and analysis review with capped feedback edges
    """
    load_dotenv()
    import asyncio

    from auto_research.graph.runtime import run_research
    from auto_research.schemas import ResearchRequest

    request = ResearchRequest(
        question=(
            "IXPE has performed a Cyg X-1 observation in its soft state on 2023-05-24. "
            "Find simultaneous or near-simultaneous observations from other X-ray observatories "
            "(e.g. NICER, NuSTAR, XMM-Newton) and perform a joint broadband spectral fit "
            "for this epoch."
        )
    )

    monitor_jsonl, monitor_md = _monitor_paths("cyg_x1_ixpe_softstate")
    run = asyncio.run(run_research(request, chatter=10, monitor_jsonl=monitor_jsonl))
    run.monitor.write_markdown(monitor_md)

    verdict = run.verdict
    print(f"\nAccepted:               {verdict.accepted}")
    print(f"Needs more literature:  {verdict.needs_more_literature}")
    print(f"Needs analysis rework:  {verdict.needs_analysis_rework}")
    if verdict.follow_up:
        print(f"Follow-up:              {verdict.follow_up}")
    print(f"\nRationale:\n{verdict.rationale}")
    print(f"\nMonitor JSONL:  {monitor_jsonl}")
    print(f"Monitor MD:     {monitor_md}")

    assert verdict is not None, "Expected a ReviewVerdict"
