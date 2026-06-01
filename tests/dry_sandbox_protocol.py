"""Dry test of the PersistentSandbox JSON-line protocol under noisy tools.

Run manually (not via pytest).  Verifies that a tool which emits heavy console
output (load_data -> ixpeobssim/XSPEC) does NOT corrupt the protocol or kill the
sandbox, and that a follow-up call still works.

Usage (from repo root, auto_research env):
    /grad/fzhou/.conda/envs/auto_research/bin/python tests/dry_sandbox_protocol.py
"""
import asyncio
import tempfile
from pathlib import Path

from auto_research.sandbox.persistent import PersistentSandbox

TOOLKIT = "/data/fzhou/research_toolkit"
ANALYZE_ENV = "/grad/fzhou/.conda/envs/astropy"
ANALYZE_INI = "/nfsdata1/fzhou/auto_research/src/auto_research/sandbox/analyst_env.sh"


async def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="sandbox_protocol_"))
    sb = PersistentSandbox(
        toolkit_root=TOOLKIT,
        workdir=workdir,
        analyze_env=ANALYZE_ENV,
        analyze_ini_sh=ANALYZE_INI,
    )
    await sb.start()
    print("started; is_alive =", sb.is_alive)

    print("\n-- call load_data (noisy: ixpeobssim/XSPEC chatter) --")
    r1 = await sb.call_tool(
        "xspec_utils.lib.xspec_help",
        "load_data",
        {
            "epoch": '{"IXPE":["02008401"],"STATE":"HSS"}',
            "ixpe_path": "/data/fzhou/XSPEC/Cyg_X-1/IXPE",
        },
        timeout_s=300,
    )
    print("load_data exit_code =", r1.get("exit_code"))
    print("stderr:", (r1.get("stderr", "") or "")[:300])
    print("console_tail present:", "console_tail" in r1, "| is_alive =", sb.is_alive)

    print("\n-- call xspec_status (protocol must still be intact) --")
    r2 = await sb.call_tool("xspec_utils.skills.session", "xspec_status", {})
    print("xspec_status exit_code =", r2.get("exit_code"))
    print("stdout:", (r2.get("stdout", "") or "")[:200])
    print("is_alive =", sb.is_alive)

    await sb.shutdown()
    ok = r1.get("exit_code") == 0 and r2.get("exit_code") == 0 and sb.is_alive is False
    print("\nRESULT:", "PASS" if (r1.get("exit_code") == 0 and r2.get("exit_code") == 0) else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
