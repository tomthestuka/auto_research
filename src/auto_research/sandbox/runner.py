"""Subprocess execution sandbox for research_toolkit tools."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from auto_research.schemas.tools import ToolResult

# Cache: ini_sh path → env dict sourced from that script.
_ini_env_cache: dict[str, dict[str, str]] = {}


def _source_ini_env(ini_sh: str) -> dict[str, str]:
    """Source a bash init script and return the resulting environment.

    Uses ``env -0`` (NUL-separated output) for robustness.  Errors from
    interactive shell components (gnome-keyring, SSH agent) are suppressed via
    ``2>/dev/null``.  The result is cached so the script is only sourced once
    per interpreter lifetime.

    Raises ``RuntimeError`` on any failure — no fallback to ``os.environ``.
    """
    if ini_sh in _ini_env_cache:
        return _ini_env_cache[ini_sh]
    try:
        result = subprocess.run(
            ["bash", "-c", f"source {ini_sh!r} 2>/dev/null; env -0"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to source analyst env from {ini_sh!r}: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(
            f"Sourcing analyst env {ini_sh!r} failed (exit {result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
    env: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        if "=" in entry:
            k, _, v = entry.partition("=")
            env[k] = v
    if not env:
        raise RuntimeError(f"Sourcing analyst env {ini_sh!r} produced an empty environment")
    _ini_env_cache[ini_sh] = env
    return env


async def run_tool(
    module: str,
    fn: str,
    args: dict,
    *,
    timeout_s: int = 300,
    workdir: Path,
    toolkit_root: Path,
    analyze_env: Path | None = None,
    analyze_ini_sh: str | None = None,
) -> ToolResult:
    """Execute a research_toolkit callable in an isolated subprocess.

    The subprocess uses the Python interpreter from ``analyze_env`` when
    provided (e.g. a conda env with XSPEC/HEASoft). When ``analyze_ini_sh``
    is given, the script is sourced once (result cached) to inject HEADAS,
    CALDB, LD_LIBRARY_PATH, and other HEASoft env vars. PYTHONPATH is always
    overridden to point at toolkit_root. Working directory is set to workdir.
    On timeout or non-zero exit the result captures stderr.
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

    base_env = _source_ini_env(analyze_ini_sh) if analyze_ini_sh else dict(os.environ)
    existing = base_env.get("PYTHONPATH", "")
    pythonpath = str(toolkit_root) + (":" + existing if existing else "")
    env = {**base_env, "PYTHONPATH": pythonpath}

    t0 = time.monotonic()
    stdin_bytes = json.dumps(args).encode()
    try:
        completed = subprocess.run(
            [python_exec, "-c", runner_src],
            input=stdin_bytes,
            capture_output=True,
            env=env,
            cwd=str(workdir),
            timeout=timeout_s,
        )
        stdout_b = completed.stdout
        stderr_b = completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - t0
        stdout = (exc.stdout or b"").decode(errors="replace")
        stderr = (exc.stderr or b"").decode(errors="replace")
        message = f"Timed out after {timeout_s}s"
        return ToolResult(
            tool_name=f"{module}.{fn}",
            stdout=stdout.strip(),
            stderr=(stderr.strip() + "\n" + message).strip(),
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
