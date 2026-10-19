"""Built-in MCP stdio server: bash_exec, list_dir (``read_file`` is in-process, `agents.react.execute_read_file`).

Run as a subprocess by MCPToolClient, from the analysis env's interpreter (which carries ``mcp``),
with the environment the analyst init script produces so HEADAS/CALDB are available to bash_exec
commands. Launched inside the OS jail when it is on (``graph.runtime._builtin_server_config``), its
source passed with ``-c``: keep this module stdlib + ``mcp`` only, since nothing else is importable
from inside the jail.
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


def _bash_exec_description() -> str:
    """Build the LLM-facing bash_exec description, injecting the configured stack probe.

    The concrete probe example comes from AR_ANALYZE_PROBE (config: analyze_probe) so no
    domain-specific string lives in this orchestration source; absent ⇒ a neutral probe line.
    """
    probe = os.environ.get("AR_ANALYZE_PROBE", "").strip()
    probe_line = (
        f'Probe the analysis env with `python -c "{probe}; print(\'OK\')"`.'
        if probe
        else "Probe the analysis env by importing your toolkit package."
    )
    return (
        "Run a shell command for read-only inspection; returns stdout, stderr, and exit_code.\n\n"
        "Scope this to inspection: list files, read a data-file header, check an array shape\n"
        "or a path, and probe the analysis environment. Every real analysis step —\n"
        "reduction, fitting, computation, figure production — and any result that\n"
        "persists run through the toolkit tools; a capability the toolkit lacks is raised\n"
        "as a `tool_request` so the coder authors a tested tool, rather than computed here.\n"
        "Keep the filesystem as you found it (the toolkit tools own what lands on disk).\n\n"
        "Invoke every command so it runs to completion on its own, with each parameter\n"
        "supplied up front — stdin is closed, so a command that waits for prompted input\n"
        "receives EOF immediately and exits. Run any interactive analysis tool\n"
        "non-interactively: pass all parameters on the command line, or set the tool's\n"
        "hidden/batch mode, rather than relying on it to prompt.\n\n"
        "Inline Python via `python -c \"...\"` is for inspection too. Separate multiple\n"
        "statements with a semicolon — an escaped newline is a literal two-character\n"
        "sequence, not a newline, and raises SyntaxError (good: `python -c \"import os;\n"
        "print(os.getcwd())\"`). " + probe_line
    )


@mcp.tool(description=_bash_exec_description())
def bash_exec(command: str, cwd: str | None = None, timeout_s: int = 60) -> dict:
    """Run a shell command for read-only inspection; returns stdout, stderr, and exit_code."""
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
        _RUNNING.add(proc)
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
    finally:
        if proc is not None:
            _RUNNING.discard(proc)


@mcp.tool()
def list_dir(path: str) -> dict:
    """List entries in a directory, sorted alphabetically."""
    try:
        entries = sorted(os.listdir(path))
        return {"path": path, "entries": entries}
    except Exception as exc:
        return {"path": path, "entries": [], "error": str(exc)}


#: Commands in flight. Each runs in its own session (so a timeout kills its whole group), which
#: also puts it outside the server's own process group: when the server goes, it takes them with
#: it explicitly, or a command a /stop interrupted outlives the run (ruling D8).
_RUNNING: set[subprocess.Popen] = set()


def _kill_running(*_: object) -> None:
    for proc in list(_RUNNING):
        if proc.poll() is None:
            _kill_process_group(proc)


def _on_terminate(signum: int, frame: object) -> None:
    _kill_running()
    os._exit(128 + signum)


if __name__ == "__main__":
    # The client closes stdin (the run ending) and then terminates the server; either way the
    # commands in flight go first.
    signal.signal(signal.SIGTERM, _on_terminate)
    try:
        asyncio.run(mcp.run_stdio_async())
    finally:
        _kill_running()
