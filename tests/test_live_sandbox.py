"""Opt-in live run: the analysts acquire archive data into the data store.

Activate with: pytest tests/test_live_sandbox.py --live

HEASARC retrieval (agent self-corrects): a full ``run_research`` on the default configuration in
which the analysts call the toolkit's archive tool (``heasarc_utils.acquire``), which stores each
download as a product in the data store; when their first arguments are wrong, the error-feedback
loop corrects them. Scope: NICER observations of Cyg X-1 since 2024-01-01. Needs the real store
(the run's launch step starts it; migration item S11 makes it).

The former ``test_xspec_local`` (a full run fitting one self-selected Cyg X-1 epoch) was deleted
2026-11-11; the full-run fit is exercised by the per-role live cells and the end-to-end live runs.

Logs: logs/dev/step-11/
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

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


def test_heasarc_selfcorrect() -> None:
    """Analyst agents retrieve NICER Cyg X-1 observations from HEASARC since 2024-01-01.

    The acquire tool stores each download in the data store. If initial arguments are wrong, the
    error-feedback loop self-corrects.
    No LLM guidance: agents self-direct using the tool catalogue.
    """
    import asyncio
    from auto_research.graph.runtime import run_research
    from auto_research.schemas import ResearchRequest

    request = ResearchRequest(
        question=(
            "Retrieve NICER monitoring observations of Cyg X-1 from the HEASARC public "
            "archive since 2024-01-01, identify which are absent from the data store, "
            "and characterize the long-term timing behavior."
        )
    )

    from tests.live_store import require_store

    require_store()  # a store that is not ready skips the run with its own refusal
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
