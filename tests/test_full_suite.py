"""Full end-to-end live test with the persistent sandbox architecture.

Activate with: AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS=1

Runs the IXPE Cyg X-1 soft-state 2023-05-24 research question through the
complete Phase 1 graph: reviewer dispatch → librarian IO (search, ingest) →
analyst debate with persistent per-worker XSPEC sandboxes → reviewer verdict.

Logs: logs/dev/step-12/
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

LIVE = os.getenv("AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS") == "1"
skip_live = pytest.mark.skipif(not LIVE, reason="Set AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS=1 to run")

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "step-12"


def _monitor_paths(name: str) -> tuple[Path, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    jsonl = LOG_DIR / f"{name}_{ts}_monitor.jsonl"
    return jsonl, jsonl.with_suffix(".md")


@skip_live
def test_cyg_x1_ixpe_softstate() -> None:
    """Full Phase 1 run: IXPE Cyg X-1 soft-state 2023-05-24 broadband spectral fit.

    Exercises the complete stack with chatter=10:
    - LibrarianIO: compose search strategy, search arXiv/ADS, ingest PDFs via IODebateToConverge
    - Retrieval: text_search_paths() from SQLite
    - AnalystWorker: debate with persistent XSPEC sandbox, one session per worker rebuilt from the ledger
    - AnalystLead: convergence judge and adjudication
    - Reviewer: literature and analysis review with capped feedback edges
    """
    import asyncio

    from auto_research.graph.runtime import run_research
    from auto_research.schemas import ResearchRequest

    request = ResearchRequest(
        question=(
            "IXPE has performed a Cyg X-1 observation in its soft state on 2023-05-24. "
            "Find simultaneous or near-simultaneous observations from other X-ray observatories "
            "(e.g. NICER, NuSTAR, XMM-Newton) and perform a joint broadband spectral fit "
            "for this epoch. epoch including observations: IXPE: '02008401', NICER: '6643010101' and '6643010102', NuSTAR: '80902318002'."
        ),
        context=(
            "TEST SESSION — evaluation criteria are relaxed as follows:\n"
            "- Absent NuSTAR and NICER backgrounds are acceptable and expected: "
            "Cyg X-1 is an extremely bright source for which background files are not used "
            "(background contribution is negligible; this is standard practice for bright sources).\n"
            "- The primary goal is to verify that analysts correctly invoked the available "
            "toolkit tools (load_data, fit_tbfeo_hss, run_fit, xspec_status, plot_fit, "
            "plot_iqu, group_pha) and that any inline Python executed via "
            "`mcp.builtin.bash_exec` with `python -c` successfully imports xspec and "
            "xspec_utils without errors.\n"
            "- The existing toolkit will not produce publication-level results. "
            "You may send back one round of revision to analysts, asking for better results. "
            "They should attempt to run inline calculations via python -c until they get "
            "non-error outputs and reasonable-looking fit statistics, "
            "but they are not expected to achieve publication-quality fits.\n"
            "- Accept the analysis if tools were called with correct arguments and returned "
            "non-error outputs, even if fit statistics are not publication-quality.\n"
            "- If any of the tools or environments seemed broken, abort the run immediately and report."
        ),
    )

    monitor_jsonl, monitor_md = _monitor_paths("cyg_x1_ixpe_softstate")
    run = asyncio.run(run_research(request, chatter=10, monitor_jsonl=monitor_jsonl))
    run.monitor.write_markdown(monitor_md)

    verdict = run.verdict
    print(f"\nAccepted:               {verdict.accepted}")
    print(f"Assigned to:  {verdict.assign_to}")
    if verdict.follow_up:
        print(f"Follow-up:              {verdict.follow_up}")
    print(f"\nRationale:\n{verdict.rationale}")
    print(f"\nMonitor JSONL:  {monitor_jsonl}")
    print(f"Monitor MD:     {monitor_md}")

    assert verdict is not None, "Expected a ReviewVerdict"
