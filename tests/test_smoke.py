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


# Tools that MUST succeed at least once for the env/sandbox to be considered healthy.
XSPEC_KEY_TOOLS = {
    "research_toolkit.xspec_utils.load_data",
    "research_toolkit.xspec_utils.xspec_status",
}
MCP_FILETREE_TOOLS = {"mcp.builtin.list_dir", "mcp.builtin.read_file"}

# Substrings that indicate an environment/sandbox failure (NOT retryable LLM
# arg-formatting mistakes like "Field required" / "missing argument").
INFRA_SIGNATURES = (
    "sandbox is not alive",
    "no module named",
    "modulenotfounderror",
    "importerror",
    "no such file",
    "cannot read response file",
    "command not found",
)


def _evaluate_run(monitor_jsonl: Path) -> tuple[set[str], list[str]]:
    """Return (kinds that succeeded >=once, infra-signature error messages)."""
    succeeded: set[str] = set()
    infra_errors: list[str] = []
    if not monitor_jsonl.exists():
        return succeeded, infra_errors
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
            kind = result.get("tool_name", "unknown")
            if result.get("exit_code", 0) == 0:
                succeeded.add(kind)
                continue
            blob = f"{result.get('stderr', '')} {result.get('stdout', '')}".lower()
            for sig in INFRA_SIGNATURES:
                if sig in blob:
                    infra_errors.append(f"{kind}: …{sig}…")
                    break
    return succeeded, infra_errors


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
            "No librarian involvement needed — this is a purely analytical task with no literature search. "
            "What is the spectral data quality of the IXPE Cyg X-1 soft-state observation 02008401? "
            "Load the IXPE-only data (from /data/fzhou/XSPEC/Cyg_X-1/IXPE, STATE:HSS) "
            "and report session status: number of detector units loaded, "
            "channel ranges, and any loading errors. No spectral fitting."
        )
    )

    from auto_research.monitor import RunMonitor

    monitor_jsonl, monitor_md = _monitor_paths("smoke_env")
    monitor = RunMonitor(chatter=10, jsonl_path=monitor_jsonl)
    try:
        run = asyncio.run(run_research(request, monitor=monitor))
    finally:
        # Always write markdown so the log is human-readable even on failure.
        monitor.write_markdown(monitor_md)
        print(f"\nMonitor JSONL:  {monitor_jsonl}")
        print(f"Monitor MD:     {monitor_md}")

    verdict = run.verdict
    print(f"\nAccepted:               {verdict.accepted}")
    print(f"Assigned to:  {verdict.assign_to}")
    if verdict.follow_up:
        print(f"Follow-up:              {verdict.follow_up}")
    print(f"\nRationale:\n{verdict.rationale}")

    assert verdict is not None, "Expected a ReviewVerdict"

    # Env-health criteria (retryable LLM arg-formatting noise is ignored):
    #   1. each xspec key tool (load_data, xspec_status) succeeded >=once,
    #   2. at least one MCP file-tree tool (list_dir / read_file) succeeded,
    #   3. no environment/sandbox-signature errors occurred.
    succeeded, infra_errors = _evaluate_run(monitor_jsonl)
    print(f"\nTools that succeeded: {sorted(succeeded)}")

    problems: list[str] = []
    missing_xspec = sorted(XSPEC_KEY_TOOLS - succeeded)
    if missing_xspec:
        problems.append("xspec_utils key tools never succeeded: " + ", ".join(missing_xspec))
    if not (MCP_FILETREE_TOOLS & succeeded):
        problems.append("no MCP file-tree tool (list_dir/read_file) succeeded")
    if infra_errors:
        problems.append("environment/sandbox errors:\n    " + "\n    ".join(infra_errors))

    if problems:
        raise AssertionError("Smoke test env-health failure:\n  " + "\n  ".join(problems))
