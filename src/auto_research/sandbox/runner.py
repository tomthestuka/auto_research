"""Subprocess execution sandbox for research_toolkit tools."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path

from auto_research.schemas.tools import ToolResult


async def run_tool(
    module: str,
    fn: str,
    args: dict,
    *,
    timeout_s: int = 300,
    workdir: Path,
    toolkit_root: Path,
    analyze_env: Path | None = None,
) -> ToolResult:
    """Execute a research_toolkit callable in an isolated subprocess.

    The subprocess uses the Python interpreter from ``analyze_env`` when
    provided (e.g. a conda env with XSPEC/HEASoft). It inherits the parent
    process environment so HEADAS, CALDB, LD_LIBRARY_PATH, etc. are available,
    with PYTHONPATH overridden to point at toolkit_root. Working directory is
    set to workdir. On timeout or non-zero exit the result captures stderr.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    python_exec = (
        str(Path(analyze_env) / "bin" / "python") if analyze_env else sys.executable
    )

    # Inline runner script: deserialise args, call fn, serialise result.
    # FigureRefs written by the tool must be JSON-serialisable paths.
    runner_src = textwrap.dedent(f"""
        import json, sys, importlib, os
        os.chdir({str(workdir)!r})
        mod = importlib.import_module({module!r})
        fn  = getattr(mod, {fn!r})
        args = json.loads(sys.stdin.read())
        result = fn(**args)
        # If the tool returns a dict, forward it; otherwise wrap as stdout.
        if isinstance(result, dict):
            print(json.dumps(result))
        else:
            print(json.dumps({{"stdout": str(result)}}))
    """).strip()

    env = {**os.environ, "PYTHONPATH": str(toolkit_root)}

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            python_exec, "-c", runner_src,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(workdir),
        )
        stdin_bytes = json.dumps(args).encode()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_bytes),
                timeout=timeout_s,
            )
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            elapsed = time.monotonic() - t0
            return ToolResult(
                tool_name=f"{module}.{fn}",
                stdout="",
                stderr=f"Timed out after {timeout_s}s",
                exit_code=124,
                elapsed_s=elapsed,
            )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        return ToolResult(
            tool_name=f"{module}.{fn}",
            stdout="",
            stderr=str(exc),
            exit_code=1,
            elapsed_s=elapsed,
        )

    elapsed = time.monotonic() - t0
    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()

    if exit_code != 0:
        return ToolResult(
            tool_name=f"{module}.{fn}",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            elapsed_s=elapsed,
        )

    # Parse any figures returned by the tool (list of {"path": ..., ...} dicts).
    from auto_research.schemas.figures import FigureRef  # local import to avoid circularity

    figures: list[FigureRef] = []
    try:
        payload = json.loads(stdout) if stdout else {}
        for fig_dict in payload.get("figures", []):
            figures.append(FigureRef.model_validate(fig_dict))
        stdout = payload.get("stdout", stdout)
    except (json.JSONDecodeError, Exception):  # noqa: BLE001
        pass  # tool returned plain text; leave stdout as-is

    return ToolResult(
        tool_name=f"{module}.{fn}",
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        figures=figures,
        elapsed_s=elapsed,
    )
