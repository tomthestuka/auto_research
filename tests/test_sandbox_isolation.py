"""Opt-in analyst sandbox isolation test.

Activate with: AUTO_RESEARCH_LIVE_SANDBOX_ISOLATION_TESTS=1

Starts two persistent analyst subprocesses with the same setup used by the
analysis team.  The first subprocess loads IXPE obsid 02008401; the second
subprocess then asks XSPEC for session status.  The second status must report an
empty session, proving XSPEC state did not leak across worker subprocesses.

Logs: logs/dev/step-11/
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from tests.dev_logs import dev_log_path


LIVE = os.getenv("AUTO_RESEARCH_LIVE_SANDBOX_ISOLATION_TESTS") == "1"
skip_live = pytest.mark.skipif(
    not LIVE,
    reason="Set AUTO_RESEARCH_LIVE_SANDBOX_ISOLATION_TESTS=1 to run",
)


def _write_log(data: dict) -> Path:
    json_path = dev_log_path("step-11", "sandbox_isolation", ".json")
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    md_path = json_path.with_suffix(".md")
    md_path.write_text(
        "\n".join(
            [
                "# sandbox_isolation",
                "",
                f"- Loader PID: `{data.get('loader_pid')}`",
                f"- Observer PID: `{data.get('observer_pid')}`",
                f"- Load exit code: `{data.get('load_result', {}).get('exit_code')}`",
                f"- Observer status exit code: `{data.get('observer_status', {}).get('exit_code')}`",
                f"- Observer spectra: `{data.get('observer_status', {}).get('n_spectra')}`",
                "",
                "## Observer status",
                "",
                "```json",
                json.dumps(data.get("observer_status"), indent=2, default=str),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Logs written to {json_path} and {md_path}")
    return json_path


@skip_live
def test_persistent_sandboxes_do_not_share_xspec_state(monkeypatch, tmp_path: Path) -> None:
    """Load IXPE in one worker subprocess; another worker's XSPEC status is empty."""

    from auto_research.graph.runtime import load_orchestration_config
    from auto_research.observations import load_observations_config
    from auto_research.sandbox.persistent import PersistentSandbox
    from auto_research.sandbox.workspace import create_run_workspace, memory_path

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))

    orch = load_orchestration_config()
    obs = load_observations_config()
    toolkit_root = Path(orch.get("toolkit_root", "/data/fzhou/research_toolkit"))
    analyze_env = Path(orch["analyze_env"]) if orch.get("analyze_env") else None
    analyze_ini_sh = orch.get("analyze_ini_sh") or None
    ixpe_path = obs.mission("ixpe").obs_db_path

    run_id = f"sandbox_isolation_{uuid.uuid4().hex}"
    keys = ["analyst_worker_0", "analyst_worker_1"]
    create_run_workspace(run_id, keys)

    loader = PersistentSandbox(
        toolkit_root=toolkit_root,
        workdir=memory_path(run_id, keys[0]).parent,
        analyze_env=analyze_env,
        analyze_ini_sh=analyze_ini_sh,
    )
    observer = PersistentSandbox(
        toolkit_root=toolkit_root,
        workdir=memory_path(run_id, keys[1]).parent,
        analyze_env=analyze_env,
        analyze_ini_sh=analyze_ini_sh,
    )

    async def run() -> dict:
        await asyncio.gather(loader.start(), observer.start())
        loader_pid = loader._proc.pid if loader._proc else None
        observer_pid = observer._proc.pid if observer._proc else None
        try:
            load_result = await loader.call_tool(
                "xspec_utils.lib.xspec_help",
                "load_data",
                {
                    "epoch": json.dumps({"IXPE": ["02008401"], "STATE": "HSS"}),
                    "ixpe_path": str(ixpe_path),
                    "clear_existing": "true",
                    "chatter": "false",
                },
                timeout_s=300,
            )
            observer_status = await observer.call_tool(
                "xspec_utils.skills.session",
                "xspec_status",
                {},
                timeout_s=60,
            )
            return {
                "run_id": run_id,
                "loader_pid": loader_pid,
                "observer_pid": observer_pid,
                "loader_workdir": memory_path(run_id, keys[0]).parent,
                "observer_workdir": memory_path(run_id, keys[1]).parent,
                "ixpe_path": ixpe_path,
                "load_result": load_result,
                "observer_status": observer_status,
            }
        finally:
            await asyncio.gather(loader.shutdown(), observer.shutdown())

    data = asyncio.run(run())
    _write_log(data)

    assert data["loader_pid"] != data["observer_pid"], "expected two distinct subprocesses"
    assert data["load_result"].get("exit_code") == 0, data["load_result"]
    assert "IXPE" in data["load_result"], data["load_result"]
    assert data["observer_status"].get("exit_code") == 0, data["observer_status"]
    assert data["observer_status"].get("n_spectra") == 0, data["observer_status"]
    assert data["observer_status"].get("model_expression") is None, data["observer_status"]
    assert data["observer_status"].get("fit_stat") is None, data["observer_status"]
    assert data["observer_status"].get("dof") is None, data["observer_status"]
