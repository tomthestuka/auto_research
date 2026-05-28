"""Subprocess execution sandbox for research_toolkit tools."""

from __future__ import annotations

import asyncio
import json
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
) -> ToolResult:
    """Execute a research_toolkit callable in an isolated subprocess.

    The subprocess runs with PYTHONPATH limited to toolkit_root, and its
    working directory set to workdir. Any write above workdir will resolve
    inside it. On timeout or non-zero exit the result captures stderr.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

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

    env = {"PYTHONPATH": str(toolkit_root)}

    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", runner_src,
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
