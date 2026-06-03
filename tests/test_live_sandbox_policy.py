"""Opt-in live XSPEC sandbox policy test.

Activate with: AUTO_RESEARCH_LIVE_SANDBOX_POLICY_TESTS=1

Starts two persistent analyst subprocesses using the configured HEASoft/PyXSPEC
analysis environment and verifies that each subprocess boots with the same
non-interactive XSPEC policy used in the reference notebook:
``Fit.query = "no"`` and ``Xset.allowPrompting = False``.

Logs: logs/dev/step-11/
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.dev_logs import dev_log_path


LIVE = os.getenv("AUTO_RESEARCH_LIVE_SANDBOX_POLICY_TESTS") == "1"
skip_live = pytest.mark.skipif(
    not LIVE,
    reason="Set AUTO_RESEARCH_LIVE_SANDBOX_POLICY_TESTS=1 to run",
)


def _write_log(data: dict) -> Path:
    json_path = dev_log_path("step-11", "sandbox_xspec_policy", ".json")
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    md_path = json_path.with_suffix(".md")
    md_path.write_text(
        "\n".join(
            [
                "# sandbox_xspec_policy",
                "",
                f"- Worker 1 PID: `{data.get('worker_1_pid')}`",
                f"- Worker 2 PID: `{data.get('worker_2_pid')}`",
                f"- Worker 1 policy: `{data.get('worker_1_policy')}`",
                f"- Worker 2 policy: `{data.get('worker_2_policy')}`",
                "",
                "```json",
                json.dumps(data, indent=2, default=str),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Logs written to {json_path} and {md_path}")
    return json_path


@skip_live
def test_persistent_sandbox_sets_xspec_policy_in_each_subprocess(tmp_path: Path) -> None:
    """Both real HEASoft subprocesses boot with non-interactive XSPEC settings."""

    from auto_research.graph.runtime import load_orchestration_config
    from auto_research.sandbox.persistent import PersistentSandbox

    probe = tmp_path / "xspec_policy_probe.py"
    probe.write_text(
        """
def status():
    from xspec import Fit, Xset
    return {
        "fit_query": Fit.query,
        "allow_prompting": Xset.allowPrompting,
        "exit_code": 0,
    }
""",
        encoding="utf-8",
    )

    orch = load_orchestration_config()
    analyze_env = Path(orch["analyze_env"]) if orch.get("analyze_env") else None
    analyze_ini_sh = orch.get("analyze_ini_sh") or None

    worker_1 = PersistentSandbox(
        toolkit_root=tmp_path,
        workdir=tmp_path / "worker_1",
        analyze_env=analyze_env,
        analyze_ini_sh=analyze_ini_sh,
    )
    worker_2 = PersistentSandbox(
        toolkit_root=tmp_path,
        workdir=tmp_path / "worker_2",
        analyze_env=analyze_env,
        analyze_ini_sh=analyze_ini_sh,
    )

    async def run() -> dict:
        await asyncio.gather(worker_1.start(), worker_2.start())
        try:
            worker_1_pid = worker_1._proc.pid if worker_1._proc else None
            worker_2_pid = worker_2._proc.pid if worker_2._proc else None
            worker_1_policy, worker_2_policy = await asyncio.gather(
                worker_1.call_tool("xspec_policy_probe", "status", {}, timeout_s=60),
                worker_2.call_tool("xspec_policy_probe", "status", {}, timeout_s=60),
            )
            return {
                "worker_1_pid": worker_1_pid,
                "worker_2_pid": worker_2_pid,
                "worker_1_policy": worker_1_policy,
                "worker_2_policy": worker_2_policy,
            }
        finally:
            await asyncio.gather(worker_1.shutdown(), worker_2.shutdown())

    data = asyncio.run(run())
    _write_log(data)

    assert data["worker_1_pid"] != data["worker_2_pid"]
    for key in ("worker_1_policy", "worker_2_policy"):
        policy = data[key]
        assert policy.get("exit_code") == 0, policy
        assert policy.get("fit_query") == "no", policy
        assert policy.get("allow_prompting") is False, policy


@skip_live
def test_sandbox_restarts_after_timeout(tmp_path: Path) -> None:
    """A timed-out REPL subprocess is killed and replaced; subsequent calls succeed."""

    from auto_research.graph.runtime import load_orchestration_config
    from auto_research.sandbox.persistent import PersistentSandbox

    (tmp_path / "slow_tool.py").write_text(
        "import time\n"
        "def sleep(secs=60): time.sleep(secs)\n"
        "def ping(): return {'stdout': 'pong', 'exit_code': 0}\n",
        encoding="utf-8",
    )

    orch = load_orchestration_config()
    analyze_env = Path(orch["analyze_env"]) if orch.get("analyze_env") else None
    analyze_ini_sh = orch.get("analyze_ini_sh") or None

    async def run() -> tuple[dict, int | None, int | None, dict, bool]:
        sandbox = PersistentSandbox(
            toolkit_root=tmp_path,
            workdir=tmp_path,
            analyze_env=analyze_env,
            analyze_ini_sh=analyze_ini_sh,
        )
        await sandbox.start()
        first_pid = sandbox._proc.pid if sandbox._proc else None
        try:
            timed_out = await sandbox.call_tool("slow_tool", "sleep", {"secs": 60}, timeout_s=1)
            restarted_pid = sandbox._proc.pid if sandbox._proc else None
            ping = await sandbox.call_tool("slow_tool", "ping", {}, timeout_s=10)
            return timed_out, first_pid, restarted_pid, ping, sandbox.is_alive
        finally:
            await sandbox.shutdown()

    timed_out, first_pid, restarted_pid, ping, alive = asyncio.run(run())

    assert timed_out["exit_code"] == 124
    assert first_pid is not None
    assert restarted_pid is not None
    assert restarted_pid != first_pid, "REPL should have a new PID after restart"
    assert ping.get("exit_code") == 0, ping
    assert ping.get("stdout") == "pong"
    assert alive is True


@skip_live
def test_mcp_bash_env_xspec_and_toolkit(tmp_path: Path) -> None:
    """MCP bash_exec env has xspec on PATH and both xspec_utils import styles work.

    Mimics _builtin_server_config exactly: reads the same config keys, builds
    the same env dict, then probes via subprocess.run with the analysis Python.
    Also confirms fit_tbabs_hss is absent from the tool catalogue and
    fit_tbfeo_hss is present.
    """
    import os
    import subprocess

    from auto_research.graph.runtime import load_orchestration_config
    from auto_research.sandbox.registry import ToolkitRegistry
    from auto_research.sandbox.runner import _source_ini_env

    orch = load_orchestration_config()
    analyze_env = Path(orch["analyze_env"])
    analyze_ini_sh = orch["analyze_ini_sh"]
    toolkit_root = Path(orch["toolkit_root"])
    disabled_tools: list[str] = orch.get("disabled_tools") or []

    python = str(analyze_env / "bin" / "python")
    base_env = _source_ini_env(analyze_ini_sh)
    existing = base_env.get("PYTHONPATH", "")
    pythonpath = str(toolkit_root) + os.pathsep + str(toolkit_root.parent)
    if existing:
        pythonpath += os.pathsep + existing
    env = {**base_env, "PYTHONPATH": pythonpath}

    results: dict[str, dict] = {}
    probes = {
        "xspec": "import xspec; print('ok')",
        "xspec_utils": "import xspec_utils; print('ok')",
        "research_toolkit": "from research_toolkit import xspec_utils; print('ok')",
    }
    for name, code in probes.items():
        r = subprocess.run(
            [python, "-c", code],
            capture_output=True, text=True, env=env, timeout=30,
        )
        results[name] = {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}

    log = dev_log_path("step-11", "mcp_bash_env", ".json")
    log.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"MCP env probe log: {log}")

    for name, res in results.items():
        assert res["rc"] == 0, f"{name} import failed: {res['stderr']}"
        assert "ok" in res["stdout"], f"{name} import gave unexpected output: {res}"

    # Tool catalogue: fit_tbabs_hss removed, fit_tbfeo_hss present
    registry = ToolkitRegistry(toolkit_root, disabled_tools=disabled_tools)
    registry.load()
    catalogue = registry.tool_catalogue()
    assert "fit_tbabs_hss" not in catalogue, "fit_tbabs_hss must not appear in catalogue"
    assert "fit_tbfeo_hss" in catalogue, "fit_tbfeo_hss must remain in catalogue"
    assert not registry.is_known("research_toolkit.xspec_utils.fit_tbabs_hss")
    assert registry.is_known("research_toolkit.xspec_utils.fit_tbfeo_hss")
