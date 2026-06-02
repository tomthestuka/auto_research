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
