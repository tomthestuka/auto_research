"""Sandbox + MCP env smoke test.

Activate with: AUTO_RESEARCH_SMOKE_TESTS=1

Runs a minimal directive request through the complete Phase 1 graph.  The prompt
instructs the analyst to:
  1. List the IXPE data directory via MCP (mcp.builtin.list_dir).
  2. Load IXPE obsid 02008401 via research_toolkit.xspec_utils.load_data.
  3. Report session state via research_toolkit.xspec_utils.xspec_status.

No spectral fitting is performed — the goal is to verify that the analyst sandbox
environment is correctly set up (HEASoft/CALDB sourced, PYTHONPATH correct) and
that both MCP file-tree tools and xspec_utils skills tools execute without error.

After the run the monitor JSONL is scanned for tool failures.  If any tool call
returned a non-zero exit_code the assertion fails immediately, naming the failing
tools grouped by kind prefix (mcp.* vs research_toolkit.xspec_utils.*).

Logs: logs/dev/step-11/
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

LIVE = os.getenv("AUTO_RESEARCH_SMOKE_TESTS") == "1"
skip_live = pytest.mark.skipif(not LIVE, reason="Set AUTO_RESEARCH_SMOKE_TESTS=1 to run")

LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "step-11"


def _monitor_paths(name: str) -> tuple[Path, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    jsonl = LOG_DIR / f"{name}_{ts}_monitor.jsonl"
    return jsonl, jsonl.with_suffix(".md")


def _scan_tool_failures(monitor_jsonl: Path) -> dict[str, list[str]]:
    """Return failed tool kinds grouped by prefix (mcp.* / research_toolkit.*)."""
    failures: dict[str, list[str]] = defaultdict(list)
    if not monitor_jsonl.exists():
        return failures
    with monitor_jsonl.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "analyst_worker.tools.result":
                continue
            result = event.get("data", {}).get("result", {})
            if result.get("exit_code", 0) != 0:
                kind = result.get("tool_name", "unknown")
                prefix = "mcp" if kind.startswith("mcp.") else "research_toolkit.xspec_utils"
                failures[prefix].append(kind)
    return failures


@skip_live
def test_sandbox_env_smoke() -> None:
    """Smoke test: verify analyst sandbox env and both MCP + xspec_utils tool paths.

    Exercises:
    - MCP built-in file-tree tool (list_dir on the IXPE data directory)
    - xspec_utils.load_data with IXPE obsid 02008401 (requires HEASoft/CALDB env)
    - xspec_utils.xspec_status (reports loaded session state)

    Fails immediately with a clear bucket message if any tool call returns non-zero.
    """
    load_dotenv()
    import asyncio

    from auto_research.graph.runtime import run_research
    from auto_research.schemas import ResearchRequest

    request = ResearchRequest(
        question=(
            "DIAGNOSTIC SMOKE TEST — follow these steps exactly, do not deviate:\n"
            "1. Use mcp.builtin.list_dir to list the directory /data/fzhou/XSPEC/Cyg_X-1/IXPE "
            "and report what you find.\n"
            "2. Use research_toolkit.xspec_utils.load_data to load ONLY IXPE obsid '02008401' "
            "from /data/fzhou/XSPEC/Cyg_X-1/IXPE (epoch = {\"IXPE\":[\"02008401\"],\"STATE\":\"HSS\"}). "
            "No other instruments.\n"
            "3. Use research_toolkit.xspec_utils.xspec_status to report the current session state.\n"
            "4. Report all results and confirm all three steps completed without error.\n"
            "This is a tooling verification run. No spectral fitting is needed."
        )
    )

    monitor_jsonl, monitor_md = _monitor_paths("smoke_env")
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

    # Fail-fast scan: any tool failure is a hard error with a clear bucket report.
    failures = _scan_tool_failures(monitor_jsonl)
    if failures:
        lines = ["Tool failures detected:"]
        for prefix, kinds in sorted(failures.items()):
            lines.append(f"  [{prefix}] " + ", ".join(sorted(set(kinds))))
        raise AssertionError("\n".join(lines))
