"""Persistent REPL subprocess sandbox for one analyst worker."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path

from typing import Any

from auto_research.sandbox.runner import _source_ini_env

# REPL script executed inside the persistent subprocess.
#
# Protocol integrity: tools (XSPEC, ixpeobssim) write large amounts of console
# output to stdout/stderr — including C-level writes that bypass sys.stdout.
# That output must never reach the JSON-line protocol stream, or one noisy call
# corrupts the parser and kills the sandbox.  We therefore (1) keep a private
# copy of the original stdout fd for protocol responses only, and (2) redirect
# fd 1/2 to a capture file at the OS level for the duration of each tool call.
_REPL_SCRIPT = textwrap.dedent("""
    import sys, os, json, importlib, traceback, tempfile

    def _coerce(v):
        \"\"\"Try to parse a string value as JSON; return as-is if it isn't valid JSON.\"\"\"
        if not isinstance(v, str):
            return v
        try:
            return json.loads(v)
        except (json.JSONDecodeError, ValueError):
            return v

    # Private duplicate of the real stdout — used ONLY for JSON protocol lines.
    _proto = os.fdopen(os.dup(1), "w")

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            cmd = json.loads(line)
        except Exception:
            break
        if cmd.get("action") == "shutdown":
            break
        # Redirect fd 1/2 to a capture file so tool/C-level console output
        # cannot pollute the protocol stream.
        _cap = tempfile.TemporaryFile(mode="w+")
        _s1, _s2 = os.dup(1), os.dup(2)
        os.dup2(_cap.fileno(), 1)
        os.dup2(_cap.fileno(), 2)
        try:
            mod = importlib.import_module(cmd["module"])
            fn = getattr(mod, cmd["fn"])
            args = {k: _coerce(v) for k, v in cmd.get("args", {}).items()}
            result = fn(**args)
            if isinstance(result, dict):
                payload = dict(result)
            else:
                payload = {"stdout": str(result)}
            payload.setdefault("exit_code", 0)
        except Exception:
            payload = {
                "exit_code": 1,
                "stderr": traceback.format_exc(),
                "stdout": "",
            }
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os.dup2(_s1, 1)
            os.dup2(_s2, 2)
            os.close(_s1)
            os.close(_s2)
            try:
                _cap.seek(0)
                _console = _cap.read()
            except Exception:
                _console = ""
            _cap.close()
        # Attach a truncated tail of captured console output for diagnosis.
        if _console:
            payload.setdefault("console_tail", _console[-2000:])
        _proto.write(json.dumps(payload) + "\\n")
        _proto.flush()
""").strip()


class PersistentSandbox:
    """Persistent REPL subprocess for one analyst worker.

    XSPEC session state (loaded spectra, fitted models, plots) survives across
    all debate rounds and rework cycles within one research run.  The subprocess
    is started once via ``start()`` and torn down via ``shutdown()``.

    If the subprocess exits unexpectedly ``is_alive`` becomes ``False`` and
    ``call_tool()`` immediately returns an error result — the worker stops
    requesting tools for the rest of the session.
    """

    def __init__(
        self,
        toolkit_root: Path | str,
        workdir: Path | str,
        *,
        analyze_env: Path | str | None = None,
        analyze_ini_sh: str | None = None,
        default_timeout_s: int = 300,
        mcp_client: Any | None = None,
    ) -> None:
        self._toolkit_root = Path(toolkit_root)
        self._workdir = Path(workdir)
        self._analyze_env = Path(analyze_env) if analyze_env else None
        self._analyze_ini_sh = analyze_ini_sh
        self._default_timeout_s = default_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._alive = False
        self.mcp_client = mcp_client

    @property
    def is_alive(self) -> bool:
        """True while the subprocess is running."""
        if self._proc is None:
            return False
        if self._proc.returncode is not None:
            self._alive = False
        return self._alive

    async def start(self) -> None:
        """Spawn the REPL subprocess.  Idempotent if already running."""
        if self.is_alive:
            return
        self._workdir.mkdir(parents=True, exist_ok=True)
        python_exec = (
            str(self._analyze_env / "bin" / "python")
            if self._analyze_env
            else sys.executable
        )
        base_env = (
            _source_ini_env(self._analyze_ini_sh)
            if self._analyze_ini_sh
            else dict(os.environ)
        )
        existing = base_env.get("PYTHONPATH", "")
        pythonpath = str(self._toolkit_root) + (":" + existing if existing else "")
        env = {**base_env, "PYTHONPATH": pythonpath}
        self._proc = await asyncio.create_subprocess_exec(
            python_exec,
            "-c",
            _REPL_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self._workdir),
        )
        self._alive = True

    async def call_tool(
        self,
        module: str,
        fn: str,
        args: dict,
        *,
        timeout_s: int | None = None,
    ) -> dict:
        """Send one tool call to the persistent subprocess and return the result dict.

        Returns a dict with at minimum ``exit_code``, ``stdout``, and ``stderr``.
        On timeout or subprocess death ``exit_code`` is non-zero and ``is_alive``
        becomes ``False``.
        """
        if not self.is_alive:
            return {"exit_code": 1, "stderr": "sandbox is not alive", "stdout": ""}
        timed = timeout_s if timeout_s is not None else self._default_timeout_s
        cmd = json.dumps({"module": module, "fn": fn, "args": args}) + "\n"
        assert self._proc is not None
        try:
            self._proc.stdin.write(cmd.encode())
            await self._proc.stdin.drain()
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=timed,
            )
            if not line:
                self._alive = False
                return {
                    "exit_code": 1,
                    "stderr": "sandbox subprocess exited unexpectedly",
                    "stdout": "",
                }
            return json.loads(line.decode())
        except asyncio.TimeoutError:
            self._alive = False
            self._proc.kill()
            await self._proc.communicate()
            return {"exit_code": 124, "stderr": f"Timed out after {timed}s", "stdout": ""}
        except Exception as exc:  # noqa: BLE001
            self._alive = False
            return {"exit_code": 1, "stderr": str(exc), "stdout": ""}

    async def call_mcp_tool(
        self,
        kind: str,
        args: dict,
        *,
        timeout_s: int | None = None,
    ) -> dict:
        """Forward a tool call to the attached MCPToolClient.

        Returns an error dict if no MCP client is configured.
        """
        if self.mcp_client is None:
            return {"exit_code": 127, "stderr": "no MCP client configured", "stdout": ""}
        return await self.mcp_client.call_tool(kind, args)

    async def shutdown(self) -> None:
        """Gracefully stop the subprocess; kill if it does not exit within 5 s."""
        if self._proc is None:
            return
        try:
            if self._proc.returncode is None:
                self._proc.stdin.write(
                    (json.dumps({"action": "shutdown"}) + "\n").encode()
                )
                await self._proc.stdin.drain()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._proc.returncode is None:
                self._proc.kill()
                await self._proc.communicate()
            self._alive = False
