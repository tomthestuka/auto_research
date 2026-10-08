"""B5 live cell (env plan): a worker meets a tool that needs software its environment lacks; the
coding pipeline gives the tool's package its own environment, the runtime builds it on commit, the
workers' calls run in it; a second run reuses it.

Opt-in (spends on every seat): ``AR_LIVE_B5=1``. Drives the detached entry point
(``run_research_nohup``: the backend survives this process, re-running the test reconnects, a dead
backend resumes from its checkpoint), against a throwaway workspace this test creates:

- a clone of the toolkit (``AR_B5_TOOLKIT``, default ``/data/fzhou/research_toolkit``, branch
  ``AR_B5_BRANCH``, default ``ai``) checked out on the authoring branch;
- a copy of this repository's ``config/`` with only the paths changed (toolkit, runtime dir,
  run workspaces) and the human's sherpana folder granted read-only;
- its own runtime dir, so environments, locks and ledgers land there, never in the real one.

Nothing in the run path is replaced. Logs: ``logs/dev/b5_live_<stamp>/``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from auto_research.graph.detached import run_research_nohup
from auto_research.sandbox.workspace import ledger_dir
from auto_research.schemas import ResearchRequest

pytestmark = pytest.mark.skipif(not os.environ.get("AR_LIVE_B5"), reason="live: set AR_LIVE_B5=1 (spends)")

REPO = Path(__file__).resolve().parents[1]
SHERPANA = Path("/data/fzhou/sherpana")
REQUEST = ResearchRequest(
    question=(
        "Use the sherpana KerrC polarization model in /data/fzhou/sherpana, scaffold tools for it "
        "and perform a minimal KerrC simulation. Report how you made it work."
    ),
    context="TEST SESSION. This session evaluates whether the graph can actively create and use environments.",
)


def _workspace(stamp: str) -> tuple[Path, Path]:
    """The throwaway toolkit clone and config dir; returns ``(workspace, config_dir)``."""
    root = Path(os.environ.get("AR_B5_ROOT", "/data/fzhou/ar_runtime")) / f"b5_live_{stamp}"
    toolkit, config, runtime = root / "research_toolkit", root / "config", root / "runtime"
    (runtime / "runs").mkdir(parents=True)
    config.mkdir()
    source = os.environ.get("AR_B5_TOOLKIT", "/data/fzhou/research_toolkit")
    branch = os.environ.get("AR_B5_BRANCH", "ai")
    subprocess.run(["git", "clone", "-q", "--branch", branch, source, str(toolkit)], check=True)
    if branch != "ai":
        subprocess.run(["git", "-C", str(toolkit), "checkout", "-q", "-b", "ai"], check=True)
    for name in ("orchestration", "models", "observations", "code_gate"):
        shutil.copy(REPO / "config" / f"{name}.yaml", config / f"{name}.yaml")
    text = (config / "orchestration.yaml").read_text()
    real = {k: re.search(rf"^{k}:\s*(\S+)", text, re.M).group(1) for k in ("runtime_dir", "toolkit_root")}
    text = re.sub(r"^runtime_dir:\s*\S+", f"runtime_dir: {runtime}", text, flags=re.M)
    text = re.sub(r"^toolkit_root:\s*\S+", f"toolkit_root: {toolkit}", text, flags=re.M)
    text = text.replace(f"  - {real['toolkit_root']} ", f"  - {toolkit} ")
    text = text.replace(f"  - {real['runtime_dir']}/runs", f"  - {runtime}/runs")
    text = re.sub(r"^agent_fs_allow:\n", f"agent_fs_allow:\n  - {SHERPANA}\n", text, flags=re.M)
    text = re.sub(r"^agent_fs_readonly:(.*)\n", rf"agent_fs_readonly:\1\n  - {SHERPANA}\n", text, flags=re.M)
    (config / "orchestration.yaml").write_text(text)
    return root, config


def _run(run_id: str, logs: Path) -> list[dict]:
    """One detached research run (the config dir is in this process's environment, which the
    backend inherits); its ledger events, copied into ``logs``."""
    import asyncio

    ledger = ledger_dir(run_id) / "monitor.jsonl"
    started = ledger.exists()  # an earlier launch of this run: reconnect, or resume a dead backend
    with open(logs / f"{run_id}.stream", "a") as stream:
        asyncio.run(run_research_nohup(REQUEST, run_id=run_id, human_channel="none",
                                       report_formats=["jsonl"], stream=stream, resume=started))
    shutil.copy(ledger, logs / f"{run_id}.jsonl")
    return [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]


def _routed_results(events: list[dict]) -> list[str]:
    """Worker toolkit results that ran in a pipeline environment (their header says where) and
    returned without an error."""
    texts = []
    for e in events:
        data = e.get("data") or {}
        if e.get("event", "").startswith("analyst_worker") and e["event"].endswith("tool_result"):
            text = str(data.get("text", ""))
            if "[ran in ar-" in text and not data.get("is_error"):
                texts.append(text)
    return texts


def test_live_a_missing_runtime_becomes_a_pipeline_environment_and_is_reused(monkeypatch):
    # Re-running with the same AR_B5_STAMP reconnects to (or resumes) that workspace's runs.
    stamp = os.environ.get("AR_B5_STAMP") or time.strftime("%Y%m%d_%H%M")
    logs = REPO / "logs" / "dev" / f"b5_live_{stamp}"
    logs.mkdir(parents=True, exist_ok=True)
    root = Path(os.environ.get("AR_B5_ROOT", "/data/fzhou/ar_runtime")) / f"b5_live_{stamp}"
    config = root / "config" if (root / "config").is_dir() else _workspace(stamp)[1]
    (logs / "workspace.txt").write_text(str(root) + "\n")
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(config))
    monkeypatch.setenv("CONDA_EXE", os.environ.get("CONDA_EXE") or shutil.which("conda")
                       or "/opt/anaconda/anaconda3/bin/conda")

    run1 = _run("b5-run1", logs)
    built = [e["data"] for e in run1 if e.get("event") == "env.build" and (e.get("data") or {}).get("ok")]
    assert [e for e in run1 if e.get("event") == "commit"], "the pipeline committed a tool"
    assert built, "the commit built a pipeline environment (env.build)"
    names = {b["name"] for b in built}
    toolkit = root / "research_toolkit"
    for name in names:
        env_id = name.removeprefix("ar-")
        assert (toolkit / "envs" / env_id / "env.yaml").is_file(), f"{name}'s definition is committed"
        assert any(f"env: {env_id}" in p.read_text() for p in toolkit.glob("*_utils/tool_config.yaml"))
    routed1 = _routed_results(run1)
    assert routed1, "a worker's toolkit call ran in the pipeline environment"
    assert any("dataReader" in t for t in routed1), "a worker's call reached dataReader in the pipeline environment"

    run2 = _run("b5-run2", logs)
    builds2 = [e["data"] for e in run2 if e.get("event") == "env.build" and (e.get("data") or {}).get("name") in names]
    # The launch reconcile records a rebuild with its cause; a commit's build of an unchanged
    # definition reuses the prefix (verify only, seconds).
    assert not [b for b in builds2 if b.get("cause")], "run 2's launch rebuilt the environment"
    assert all(b.get("seconds", 0) < 60 for b in builds2), "run 2 rebuilt the environment at a commit"
    assert not [e for e in run2 if e.get("event") == "env.drift"]
    assert _routed_results(run2), "run 2's workers call the tool in the same environment"
    (logs / "summary.md").write_text(
        f"# B5 live cell {stamp}\n\n- workspace: {root}\n- environments: {sorted(names)}\n"
        f"- run 1 cost: ${run1[-1].get('cost_usd_total')}\n- run 2 cost: ${run2[-1].get('cost_usd_total')}\n"
    )
