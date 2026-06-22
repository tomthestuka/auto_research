"""Built-in MCP stdio server: bash_exec, list_dir, read_file.

Run as a subprocess by MCPToolClient. Uses the auto_research Python env (mcp
installed); does NOT need the analysis env (XSPEC/HEASoft).  Inherits the
parent process environment so HEADAS/CALDB are available to bash_exec commands.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("auto_research_builtin")


@mcp.tool()
def bash_exec(command: str, cwd: str | None = None, timeout_s: int = 60) -> dict:
    """Run a shell command for read-only inspection; returns stdout, stderr, and exit_code.

    Scope this to inspection: list files, read a FITS header, check an array shape
    or a path, and probe the analysis environment. Every real analysis step —
    reduction, fitting, computation, figure production — and any result that
    persists run through the toolkit tools; a capability the toolkit lacks is raised
    as a `tool_request` so the coder authors a tested tool, rather than computed here.
    Keep the filesystem as you found it (the toolkit tools own what lands on disk).

    Inline Python via `python -c "..."` is for inspection too. Separate multiple
    statements with a semicolon — an escaped newline is a literal two-character
    sequence, not a newline, and raises SyntaxError (good: `python -c "import os;
    print(os.getcwd())"`). Probe the analysis env with
    `python -c "import xspec, xspec_utils; print('OK')"` (the xspec module has
    no __version__ attribute)."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Command timed out after {timeout_s}s", "exit_code": 124}
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": 1}


@mcp.tool()
def list_dir(path: str) -> dict:
    """List entries in a directory, sorted alphabetically."""
    try:
        entries = sorted(os.listdir(path))
        return {"path": path, "entries": entries}
    except Exception as exc:
        return {"path": path, "entries": [], "error": str(exc)}


@mcp.tool()
def read_file(path: str, max_bytes: int = 65536) -> dict:
    """Read a text file. Returns content and whether it was truncated."""
    try:
        size = os.path.getsize(path)
        with open(path, errors="replace") as f:
            content = f.read(max_bytes)
        return {"content": content, "truncated": size > max_bytes, "size_bytes": size}
    except Exception as exc:
        return {"content": "", "truncated": False, "size_bytes": 0, "error": str(exc)}


if __name__ == "__main__":
    asyncio.run(mcp.run_stdio_async())
