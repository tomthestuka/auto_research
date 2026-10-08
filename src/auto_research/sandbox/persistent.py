"""Persistent REPL subprocess sandbox for one analyst worker."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

from typing import Any

from auto_research.sandbox.runner import with_private_param_dir


def _descendant_pids(pid: int) -> list[int]:
    """Every descendant of ``pid``, breadth-first (nearest child first).

    Reads ``/proc/<pid>/task/*/children``, the kernel's own child list — no dependency, and it
    sees a child regardless of which thread spawned it. Returns an empty list wherever ``/proc``
    is unavailable or a pid has already exited, so a caller can always kill what it found and
    treat the rest as gone.
    """
    found: list[int] = []
    queue = [pid]
    while queue:
        current = queue.pop(0)
        for task in Path(f"/proc/{current}/task").glob("*/children"):
            try:
                kids = [int(p) for p in task.read_text().split()]
            except (OSError, ValueError):
                continue
            for kid in kids:
                if kid not in found:
                    found.append(kid)
                    queue.append(kid)
    return found

def repl_payload(result):
    """The protocol payload for one tool return: a dict as-is, anything else as its ``stdout``.

    Defined once, as real code, and spliced into both the REPL below and the gate's return-check
    recorder (``gate._RETURNS_RECORDER``), so the gate transmits a return exactly as the REPL does.
    Stdlib-only for that reason: it runs in the analysis interpreter.
    """
    if isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {"stdout": str(result)}
    payload.setdefault("exit_code", 0)
    return payload


# REPL script executed inside the persistent subprocess.
#
# Protocol integrity: tools (XSPEC, ixpeobssim) write large amounts of console
# output to stdout/stderr — including C-level writes that bypass sys.stdout.
# That output must never reach the JSON-line protocol stream, or one noisy call
# corrupts the parser and kills the sandbox.  We therefore (1) keep a private
# copy of the original stdout fd for protocol responses only, and (2) redirect
# fd 1/2 to a capture file at the OS level for the duration of each tool call.
# First statement: `python -c` puts the working folder first on sys.path, so a package written into
# the workdir would shadow the toolkit one and its results be stamped as toolkit evidence. Removed
# here rather than with `-P`, which only Python 3.11+ accepts (a 3.9 pipeline environment's REPL
# died at launch on it, B5 live cell 2026-10-07); `sys` is built in, so nothing can shadow it.
#: Prepended to a ``python -c`` program in place of ``-P`` (shared with the shell server).
SAFE_PATH_PRELUDE = "import sys as _sys\n_sys.path[:] = [_p for _p in _sys.path if _p not in ('', '.')]\n"
_REPL_SCRIPT = SAFE_PATH_PRELUDE + \
    inspect.getsource(repl_payload) + "\n" + textwrap.dedent("""
    import sys, os, json, importlib, traceback, tempfile

    def _coerce(v):
        \"\"\"Back-compat guard for argument values.

        Native function calling delivers correctly-typed arguments, so values
        pass through untouched. Only a string that is clearly a JSON container
        ('{' or '[') is parsed — a legacy/edge path. Scalar strings are left as
        strings so a value like '123', 'true', or a title is never mis-coerced.
        \"\"\"
        if not isinstance(v, str):
            return v
        s = v.strip()
        if s[:1] in ('{', '['):
            try:
                return json.loads(s)
            except (json.JSONDecodeError, ValueError):
                return v
        return v

    def _run_repl_init():
        \"\"\"Exec the configured domain REPL-init file at subprocess boot (best-effort).

        Path comes from AR_REPL_INIT_PY (config: repl_init_py); empty/absent ⇒ bare REPL.
        A failure is swallowed so a missing optional model never kills the REPL — the
        domain owns what its session setup does.\"\"\"
        _init = os.environ.get("AR_REPL_INIT_PY", "").strip()
        if not _init:
            return
        try:
            with open(_init, "r") as _f:
                _src = _f.read()
            exec(compile(_src, _init, "exec"), {"__name__": "__repl_init__"})
        except Exception:
            pass

    # The boot file's own output (an XSPEC banner flushed at C level) goes to stderr: fd 1 is the
    # protocol stream once the loop starts.
    _s1 = os.dup(1)
    os.dup2(2, 1)
    try:
        _run_repl_init()
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os.dup2(_s1, 1)
        os.close(_s1)

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
            payload = repl_payload(result)
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
            payload.setdefault("console_tail", _console[-8000:])
        # A return that is not JSON fails its own call like any tool error; unguarded, the
        # exception ended this loop and took the worker's sandbox with it (2026-09-25).
        try:
            _line = json.dumps(payload)
        except Exception as _exc:
            _line = json.dumps({
                "exit_code": 1,
                "stdout": "",
                "stderr": (f"the tool returned a value that is not JSON ({type(_exc).__name__}: "
                           f"{_exc}). Return plain JSON types: float()/int() for numpy scalars, "
                           ".tolist() for arrays, str() for paths."),
            })
        _proto.write(_line + "\\n")
        _proto.flush()
""").strip()


#: Largest tool return (one JSON line) the REPL protocol reads, in bytes.
_PROTOCOL_LINE_LIMIT = 256 * 1024 * 1024


class _EnvironmentRestartFailed(Exception):
    """A restart the environment check started failed; returned as the call's error."""


class PersistentSandbox:
    """Persistent REPL subprocess for one analyst worker.

    XSPEC session state (loaded spectra, fitted models, plots) survives across
    all debate rounds and rework cycles within one research run.  The subprocess
    is started once via ``start()`` and torn down via ``shutdown()``.

    If the subprocess exits unexpectedly or times out, ``call_tool()`` returns
    the non-zero result for that call and starts a fresh REPL for subsequent
    calls. The replacement process has a clean XSPEC session.
    """

    def __init__(
        self,
        toolkit_root: Path | str,
        workdir: Path | str,
        *,
        analyze_env: Path | str | None = None,
        analyze_ini_sh: str | None = None,
        repl_init_py: Path | str | None = None,
        default_timeout_s: int = 300,
        mcp_client: Any | None = None,
        environment: Any | None = None,
        environments: Any | None = None,
        label: str = "repl",
    ) -> None:
        self._toolkit_root = Path(toolkit_root)
        self._workdir = Path(workdir)
        if environment is None:
            # The legacy spelling of the same thing (prefix, activation, boot file).
            from auto_research.sandbox.envs import Environment

            prefix = Path(analyze_env) if analyze_env else Path(sys.prefix)
            environment = Environment(
                name=prefix.name, prefix=prefix,
                activate=Path(analyze_ini_sh) if analyze_ini_sh else None,
                boot=Path(repl_init_py) if repl_init_py else None,
            )
        #: The software environment this REPL runs in; its fingerprint stamps every receipt.
        self.environment = environment
        #: ``() -> list[Environment]``: what this worker may list and point its shell at.
        self.environments = environments
        #: The name of the environment this worker's shell (``bash_exec``) runs in when it chose
        #: another with ``use_env``; ``None`` is its own. Kept here, so the choice lasts across rounds.
        self.shell_environment: str | None = None
        self._default_timeout_s = default_timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._alive = False
        self.mcp_client = mcp_client
        self._label = label
        #: One REPL per other environment a toolkit call is routed to (env plan B3b), started at
        #: its first call, in the same folder, toolkit and jail policy as this one.
        self._pool: dict[str, PersistentSandbox] = {}
        #: The environment the last ``call_tool`` ran in (this REPL's, or a pool REPL's).
        self.call_environment = self.environment

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
        # Fingerprinted before the activation is sourced: a change in between then costs one
        # spurious restart at the next call, never an undetected one. The lock flag is cleared
        # first, so a start that fails midway never leaves an earlier boot's True behind.
        self.booted_lock_written = False
        self.booted_fingerprint = await asyncio.to_thread(self._current_fingerprint)
        python_exec = str(self.environment.python)
        activation = self.environment.activate
        # Coder-written files: a pipeline environment's activation or boot copy (under the agent
        # conda dir). They run only in the jail, bound as files; the launcher sources a coder-written
        # activation (and a pipeline environment with none) inside it.
        owned: tuple[Path, ...] = ()
        if self.environment.owner == "pipeline":
            from auto_research.env import agent_conda_dir

            agent_dir = agent_conda_dir().resolve()
            owned = tuple(f for f in (activation, self.environment.boot)
                          if f is not None and f.resolve().is_relative_to(agent_dir))
        pipeline = self.environment.owner == "pipeline" and (activation is None or activation in owned)
        if pipeline:
            # A pipeline environment's activation is coder-written: sourced inside the jail by the
            # shared launcher (envs.in_environment), never here (env plan B3b).
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
        else:
            if self.environment.activate is None:
                raise ValueError("analyze_ini_sh must be configured — no env fallback")
            # A derived environment with no activation of its own runs its human base's, as the
            # analysis REPL does.
            import dataclasses

            base_env = dataclasses.replace(self.environment, owner="human").activation_env()
            existing = base_env.get("PYTHONPATH", "")
            pythonpath = str(self._toolkit_root) + (":" + existing if existing else "")
            env = {**base_env, "PYTHONPATH": pythonpath}
        if self.environment.boot is not None:
            env["AR_REPL_INIT_PY"] = str(self.environment.boot)
        # The OS jail (§12) when configured: the REPL is where toolkit tools and ``bash_exec`` run,
        # and a shell command's implicit reads are exactly what the command-string guard cannot see.
        # Network stays on — acquisition tools download observations. The toolkit is read-only and
        # /tmp private (``jail.analysis_policy``, F20 review 2026-10-06).
        from auto_research.sandbox.jail import analysis_policy, private_tmp, wrap

        env["PYTHONNOUSERSITE"] = "1"  # the jail home persists; a user site there would run at boot
        # The working folder is kept off sys.path by the script's first statement (any Python version;
        # the REPL's own child processes keep their normal path rules).
        # Fresh at every start (incl. a restart after a timeout): nothing the agent left in its /tmp
        # survives into the next launch's mounts.
        import shutil

        shutil.rmtree(private_tmp(self._label, self._workdir), ignore_errors=True)
        self._private_tmp = private_tmp(self._label, self._workdir)
        command = [python_exec, "-c", _REPL_SCRIPT]
        readonly: tuple[str, ...] = (
            (str(self.environment.prefix), *(str(f) for f in owned)) if self.environment.owner == "pipeline" else ())
        if pipeline:
            import shlex

            from auto_research.sandbox.envs import in_environment

            script = in_environment(self.environment, f'{shlex.quote(python_exec)} "$@"', self._toolkit_root,
                                    pass_args=True, keep=("AR_REPL_INIT_PY", "PYTHONPYCACHEPREFIX"), quiet=True)
            command = ["/bin/bash", "--noprofile", "--norc", "-c", script, "_", *command[1:]]
        argv = wrap(
            command,
            analysis_policy(
                label=self._label, toolkit_root=self._toolkit_root,
                writable=(str(self._workdir),), tmp=self._private_tmp, readonly=readonly,
                # This worker's own home, outside every agent-writable folder (a planted symlink there
                # became a writable bind of its target), checked like the private /tmp.
                home=private_tmp(f"{self._label}-home", self._workdir),
            ),
        )
        # Its own HEASoft parameter folder (F23): /tmp is private when jailed.
        jailed = argv[0] != command[0]
        if owned and not jailed:
            from auto_research.sandbox.jail import jail_enabled

            if jail_enabled():  # configured but not runnable here: a coder-written activation waits
                raise RuntimeError(f"{self.environment.name} runs only in the jail, which cannot start on this host")
        env = with_private_param_dir(
            env, "/tmp/pfiles" if jailed else self._private_tmp / "pfiles",
            create=self._private_tmp / "pfiles",
        )
        if pipeline:  # the launcher puts this writable half back after the activation
            (self._private_tmp / "pfiles").mkdir(parents=True, exist_ok=True)
            env["PFILES"] = "/tmp/pfiles" if jailed else str(self._private_tmp / "pfiles")
        # Bytecode in this REPL's own private folder: the toolkit's `__pycache__` (git-ignored, so
        # never judged) is never read — a planted `.pyc` matching its source cannot replace
        # reviewed code. Private either way: never the shared host /tmp.
        env["PYTHONPYCACHEPREFIX"] = "/tmp/ar-pycache" if jailed else str(self._private_tmp / "ar-pycache")
        # The environment state this REPL booted on (taken above): a call after it changes
        # restarts it first (env plan B2a); its receipts stamp it only once its lock is on disk.
        self.booted_lock_written = await asyncio.to_thread(self._lock_booted)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(self._workdir),
            # One tool return is one protocol line; asyncio's default 64 KB line limit failed every
            # larger return (B5 live cell 2026-10-07: "Separator is found, but chunk is longer than
            # limit").
            limit=_PROTOCOL_LINE_LIMIT,
        )
        self._alive = True

    def _current_fingerprint(self) -> str | None:
        """The environment's fingerprint now, or ``None`` when it cannot be known (unreadable, or
        its activation failed): an unknown state never triggers a restart."""
        try:
            from auto_research.sandbox.envs import _state

            digest, parts = _state(self.environment)
        except Exception:  # noqa: BLE001 — an unreadable env disables the check, never the call
            return None
        return None if "error" in parts.get("activation", {}) else digest

    def _lock_booted(self) -> bool:
        """Write the lock for the state this REPL booted on; True when it is on disk."""
        if self.booted_fingerprint is None:
            return False
        try:
            from auto_research.env import env_lock_dir
            from auto_research.sandbox.envs import fingerprint_and_lock

            return fingerprint_and_lock(self.environment, env_lock_dir()) == self.booted_fingerprint
        except Exception:  # noqa: BLE001 — unstamped receipts, never a REPL that cannot start
            return False

    def take_notice(self) -> str | None:
        """The runtime's notice for the call just made (a restart), handed over once."""
        notice, self._notice = getattr(self, "_notice", None), None
        return notice

    async def _restart_if_environment_changed(self) -> str | None:
        """Restart first when the environment changed since boot; return what the model is told.

        A restart clears whatever the REPL session held (a loaded dataset, a fit), so the call that
        triggered it says so; a restart that fails comes back as that call's error, never as an
        exception out of the tool loop (contained failure).
        """
        if not self.is_alive:
            return None
        booted = getattr(self, "booted_fingerprint", None)
        current = await asyncio.to_thread(self._current_fingerprint)
        if booted is None or current is None or current == booted:
            return None
        try:
            await self._restart_own()
        except Exception as exc:  # noqa: BLE001
            raise _EnvironmentRestartFailed(str(exc)) from exc
        return (f"[environment changed (env:{booted[:12]} -> env:{current[:12]}): the REPL was restarted "
                "and its session state cleared; reload what this step needs]")

    async def restart(self) -> None:
        """Kill any current subprocess and spawn a fresh REPL; the pool's REPLs go and start again at
        their next call (a commit reloads what every one of them imports)."""
        await self._close_pool()
        await self._kill_current()
        await self.start()

    async def _restart_own(self) -> None:
        """This REPL alone, fresh (a failure or an environment change): the pool's sessions stay."""
        await self._kill_current()
        await self.start()

    async def _close_pool(self) -> None:
        pool, self._pool = self._pool, {}
        for child in pool.values():
            await child.shutdown()

    async def _kill_current(self) -> None:
        """Terminate the current subprocess **and its descendants**, if any.

        Killing the REPL alone leaves whatever it shelled out still running — an XSPEC fit, a
        multi-GB HEASARC download — and those children keep writing into the obs-db after the
        caller believes the sandbox is gone. They are killed first (deepest last, so a parent
        cannot spawn more while we work) and then the REPL itself.

        Descendants are collected by walking ``/proc``, deliberately not by ``killpg``: the REPL
        is spawned without ``start_new_session``, so it shares the *backend's* process group and
        a group kill would take the run down with it. That shared group is also what lets an
        external ``kill -9 -<pgid>`` reach the sandboxes when our own cleanup never runs (the §12
        hard-kill fallback), which is why it stays that way.
        """
        proc = self._proc
        if proc is None:
            self._alive = False
            return
        try:
            if proc.returncode is None:
                for child in reversed(_descendant_pids(proc.pid)):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(child, signal.SIGKILL)
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._alive = False

    async def abort(self) -> bool:
        """Cancel whatever this sandbox is running right now; return True if it was busy.

        The on-demand form of the timeout path, and deliberately nothing more: killing the REPL
        child ends the in-flight ``readline`` with EOF, so ``call_tool`` returns its ordinary
        non-zero "sandbox subprocess exited unexpectedly" result and the caller gets a real
        result for the call it made — which is what keeps a tool_use/tool_result transcript
        valid. The next ``call_tool`` spawns a fresh REPL at entry, so nothing inherits a dead
        one. This is how a human ``/stop`` stops a 25-minute download instead of waiting it out
        (live 2026-07-29: a hand-killed REPL produced exactly this result, then the loop honored
        the stop).
        """

        was_alive = self.is_alive or any(child.is_alive for child in self._pool.values())
        for child in list(self._pool.values()):
            await child._kill_current()
        await self._kill_current()
        return was_alive

    async def kill_now(self) -> None:
        """Kill the REPL and its descendants immediately — the human-interrupt path.

        Distinct from :meth:`shutdown`, which first writes a shutdown line and waits up to 5 s for
        a clean exit. An interrupt has no time for a handshake and no interest in one: the point
        is that nothing in the run can still write to disk when the next step (the toolkit-branch
        reset) runs.
        """
        for child in list(self._pool.values()):
            await child._kill_current()
        await self._kill_current()

    async def _restart_for_failure(self) -> str:
        """Restart after a tool-call failure; return a suffix on restart failure."""
        try:
            await self._restart_own()
        except Exception as exc:  # noqa: BLE001
            return f"; sandbox restart failed: {exc}"
        return ""

    async def call_tool(
        self,
        module: str,
        fn: str,
        args: dict,
        *,
        timeout_s: int | None = None,
        environment: Any | None = None,
    ) -> dict:
        """Send one tool call to the persistent subprocess and return the result dict.

        Returns a dict with at minimum ``exit_code``, ``stdout``, and ``stderr``.
        On timeout or subprocess death ``exit_code`` is non-zero and the sandbox
        attempts to spawn a replacement subprocess for subsequent calls.

        ``environment`` (env plan B3b): another environment to run the call in — the tool package's
        tested one, or the caller's choice — on its own REPL from the pool; ``call_environment``,
        ``call_stamp`` and the notice then name that REPL's.
        """
        if environment is not None and environment.name != self.environment.name:
            child = self._pool.get(environment.name)
            if child is not None and child.environment != environment:  # rebuilt: a fresh record
                await child.shutdown()
                child = None
            if child is None:
                child = PersistentSandbox(
                    self._toolkit_root, self._workdir, environment=environment,
                    default_timeout_s=self._default_timeout_s,
                    label=f"repl-{environment.name}",
                )
                self._pool[environment.name] = child
            self.call_environment = child.environment
            result = await child.call_tool(module, fn, args, timeout_s=timeout_s)
            self._notice = child.take_notice()
            self.call_stamp = child.call_stamp
            return result
        self.call_environment = self.environment
        self.call_stamp = None
        if not self.is_alive:
            # Started before the stamp is taken: a first call (a pool REPL's, or after a restart)
            # names the state it runs in like every other.
            try:
                await self.start()
            except Exception as exc:  # noqa: BLE001
                return {"exit_code": 1, "stderr": f"sandbox start failed: {exc}", "stdout": ""}
        try:
            notice = await self._restart_if_environment_changed()
        except _EnvironmentRestartFailed as exc:
            return {"exit_code": 1, "stdout": "",
                    "stderr": f"the environment changed and the REPL could not restart in it: {exc}"}
        # Out of band, never in the result dict: a tool's own return is not where runtime text
        # comes from (a tool returning a key of that name must not write into the header).
        self._notice = notice
        # The state this call runs in, fixed before it runs: a timeout or crash restarts the REPL
        # inside `_call`, and the receipt must still name the state the call actually ran in.
        self.call_stamp = self.booted_fingerprint if getattr(self, "booted_lock_written", False) else None
        return await self._call(module, fn, args, timeout_s=timeout_s)

    async def _call(self, module: str, fn: str, args: dict, *, timeout_s: int | None) -> dict:
        if not self.is_alive:
            try:
                await self.start()
            except Exception as exc:  # noqa: BLE001
                return {"exit_code": 1, "stderr": f"sandbox restart failed: {exc}", "stdout": ""}
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
                said = await self._stderr_tail()
                restart_error = await self._restart_for_failure()
                return {
                    "exit_code": 1,
                    "stderr": "sandbox subprocess exited unexpectedly" + (f": {said}" if said else "") + restart_error,
                    "stdout": "",
                }
            return json.loads(line.decode())
        except asyncio.TimeoutError:
            restart_error = await self._restart_for_failure()
            return {"exit_code": 124, "stderr": f"Timed out after {timed}s{restart_error}", "stdout": ""}
        except Exception as exc:  # noqa: BLE001
            restart_error = await self._restart_for_failure()
            return {"exit_code": 1, "stderr": str(exc) + restart_error, "stdout": ""}

    async def _stderr_tail(self, limit: int = 1200) -> str:
        """What a REPL that ended wrote to stderr (an activation that did not finish says so there)."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        data = b""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        try:
            while (left := deadline - loop.time()) > 0:
                chunk = await asyncio.wait_for(proc.stderr.read(64 * 1024), timeout=left)
                if not chunk:
                    break
                data = (data + chunk)[-64 * 1024:]  # the newest: the cause comes last
        except Exception:  # noqa: BLE001 — the tail is a courtesy; the call's error stands without it
            pass
        return data.decode("utf-8", "replace").strip()[-limit:]

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
        await self._close_pool()
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
            tmp = getattr(self, "_private_tmp", None)
            if tmp is not None:
                import shutil

                shutil.rmtree(tmp, ignore_errors=True)  # the REPL's private /tmp dies with it
