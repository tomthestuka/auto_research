"""Built-in MCP stdio server: bash_exec, list_dir, read_file.

Run as a subprocess by MCPToolClient. Uses the auto_research Python env (mcp
installed); does NOT need the analysis env (XSPEC/HEASoft).  Inherits the
parent process environment so HEADAS/CALDB are available to bash_exec commands.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("auto_research_builtin")


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole session group of ``proc`` so grandchildren die with it.

    ``proc`` is a session leader (``start_new_session=True``), so its pid is the
    process-group id. Killing the group reaps forked/interactive grandchildren
    (e.g. a prompting HEASoft FTOOL) that would otherwise outlive the shell and
    hold pipes open. Falls back to killing the direct child alone if the group is
    already gone."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


@mcp.tool()
def bash_exec(command: str, cwd: str | None = None, timeout_s: int = 60) -> dict:
    """Run a shell command for read-only inspection; returns stdout, stderr, and exit_code.

    Scope this to inspection: list files, read a FITS header, check an array shape
    or a path, and probe the analysis environment. Every real analysis step —
    reduction, fitting, computation, figure production — and any result that
    persists run through the toolkit tools; a capability the toolkit lacks is raised
    as a `tool_request` so the coder authors a tested tool, rather than computed here.
    Keep the filesystem as you found it (the toolkit tools own what lands on disk).

    Invoke every command so it runs to completion on its own, with each parameter
    supplied up front — stdin is closed, so a command that waits for prompted input
    receives EOF immediately and exits. Run HEASoft/FTOOLS non-interactively: pass all
    parameters on the command line (e.g. `fdump infile=… columns=… rows=…`) or set the
    tool's hidden mode, rather than relying on it to prompt.

    Inline Python via `python -c "..."` is for inspection too. Separate multiple
    statements with a semicolon — an escaped newline is a literal two-character
    sequence, not a newline, and raises SyntaxError (good: `python -c "import os;
    print(os.getcwd())"`). Probe the analysis env with
    `python -c "import xspec, xspec_utils; print('OK')"` (the xspec module has
    no __version__ attribute)."""
    proc: subprocess.Popen | None = None
    try:
        # start_new_session isolates the command (and any children it forks) into
        # its own process group, so a timeout can kill the whole group; stdin is
        # DEVNULL so a prompting tool gets EOF and cannot read this server's MCP
        # transport (a prompting grandchild there wedges the whole run).
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            return {"stdout": stdout, "stderr": stderr, "exit_code": proc.returncode}
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # The group is dead, so this drain sees EOF promptly and cannot hang.
            stdout, stderr = proc.communicate()
            note = f"Command timed out after {timeout_s}s"
            return {
                "stdout": stdout or "",
                "stderr": (stderr + "\n" if stderr else "") + note,
                "exit_code": 124,
            }
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc)
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
