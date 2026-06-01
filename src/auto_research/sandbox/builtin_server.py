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
    """Run a shell command and return stdout, stderr, and exit_code."""
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
