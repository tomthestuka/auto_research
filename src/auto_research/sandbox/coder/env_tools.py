"""The coder's environment tools (env plan B3c-3): solve a lock, pin wheels, activate a candidate.

The coder edits an environment definition (``envs/<env_id>/``) in its worktree like any other
file, but never runs conda itself: these tools run in the runtime process, jailed, and write only
the lock and pip files into the worktree and the candidate environment into the attempt's own
folder (``cand_dir``), which the coder's jail sees read-only.

``cand_env_activate`` points ``<cand_dir>/active/bin`` — first on the coder's ``PATH`` from launch —
at small wrappers for the candidate's executables, so a plain ``python`` or ``pytest`` in the
coder's shell runs in the candidate from a clean start (as a toolkit call runs in an environment),
with the worktree's own ``activate.sh`` sourced first (an edit takes effect on the next command, no
rebuild). With no name the wrappers go and plain commands return to the analysis environment. The
folder is cleared when the attempt starts and removed when it ends; after :meth:`close` a tool still
running writes nothing into the worktree.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path

ENV_SOLVE_DESCRIPTION = (
    "Solve conda-forge packages into your environment's exact lock: give the env id (the folder "
    "under envs/, named in your package's tool_config.yaml as `env:`) and package specs such as "
    "`python=3.11`, `numpy=2.1` or `pytest`. The runtime solves them and rewrites "
    "envs/<env_id>/env.lock (every package's URL and checksum) from exactly this list, so give the "
    "full list each time, pytest included. Then call cand_env_activate to build and use it."
)
ENV_PIN_DESCRIPTION = (
    "Pin pip wheels for what conda-forge lacks: give the env id and exact requirements such as "
    "`pkg==1.2.3`. The runtime downloads each binary wheel with your candidate environment's "
    "Python (activate it first) and rewrites envs/<env_id>/env.pip with each wheel's sha256. Each "
    "wheel installs alone: pin every dependency it needs that env.lock lacks too, or add that "
    "dependency to env_solve's specs."
)
ENV_ACTIVATE_DESCRIPTION = (
    "Build your environment from its definition in the worktree and make it your shell's: after "
    "this, a plain `python`, `pytest` or `pip list` runs in it, with envs/<env_id>/activate.sh "
    "sourced first. The build is cached until env.lock or env.pip changes. Call it with no env id "
    "to return to the analysis environment. Run your package's tests this way before you submit."
)


class CandidateEnvs:
    """One authoring attempt's environment tools. ``worktree`` holds the definitions; ``cand_dir``
    the candidate builds and the ``active/bin`` wrappers (read-only to the coder)."""

    #: Commands a coder runs by name; one the candidate lacks answers so instead of falling through
    #: to the analysis environment on ``PATH``.
    CORE = ("python", "pytest", "pip")

    def __init__(self, worktree: Path, cand_dir: Path) -> None:
        self.worktree = Path(worktree)
        self.cand_dir = Path(cand_dir)
        self.active_bin = self.cand_dir / "active" / "bin"
        self.active_bin.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()  # one activation at a time: wrappers come from one prefix
        self._built_lock: dict[str, str | None] = {}  # env id → env.lock digest of its last build
        from auto_research.sandbox.env_build import StoppableRunner

        self._runner = StoppableRunner()  # close() kills a solve, pin or build still running
        self._closed = False

    def close(self) -> None:
        """The attempt is ending: a tool still running writes nothing more into the worktree or
        the wrappers (its tree is about to be reset)."""
        self._closed = True
        self._runner.stop()

    # ── tools ────────────────────────────────────────────────────────────────────────────────

    async def solve(self, env_id: str, specs: list[str]) -> str:
        folder = self._folder(env_id)
        if isinstance(folder, str):
            return folder
        return await asyncio.to_thread(self._locked, self._solve, folder, list(specs or []))

    async def pin(self, env_id: str, requirements: list[str]) -> str:
        folder = self._folder(env_id)
        if isinstance(folder, str):
            return folder
        return await asyncio.to_thread(self._locked, self._pin, env_id, folder, list(requirements or []))

    async def activate(self, env_id: str | None) -> str:
        if not env_id:
            await asyncio.to_thread(self._deactivate)
            return "Your shell runs in the analysis environment again."
        folder = self._folder(env_id)
        if isinstance(folder, str):
            return folder
        return await asyncio.to_thread(self._locked, self._activate, env_id, folder)

    def cleanup(self) -> None:
        """Remove the candidate builds and wrappers (the attempt has ended)."""
        from auto_research.sandbox.env_build import _remove_tree

        _remove_tree(self.cand_dir, missing_ok=True)

    # ── work ─────────────────────────────────────────────────────────────────────────────────

    def _folder(self, env_id: str) -> Path | str:
        from auto_research.sandbox.env_build import _NAME

        if not isinstance(env_id, str) or not _NAME.fullmatch(env_id):
            return ("Give the env id: the folder name under envs/ (lowercase letters, digits, '_' "
                    "and '-'), as your package's tool_config.yaml names it with `env:`.")
        folder = self.worktree / "envs" / env_id
        # The runtime writes here outside the coder's jail: only a real folder inside the worktree.
        if any(p.is_symlink() for p in (folder.parent, folder)):
            return f"Make envs/ and envs/{env_id}/ plain folders in your working directory."
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to(self.worktree.resolve()):
            return f"Make envs/{env_id}/ a plain folder inside your working directory."
        return folder

    def _write(self, path: Path, text: str) -> str | None:
        """Write a definition file, or say why not: never after close, never through a link (the
        replace swaps the name itself)."""
        if self._closed:
            return "The authoring attempt has ended; nothing was written."
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return None

    def _solve(self, folder: Path, specs: list[str]) -> str:
        from auto_research.env import conda_executable
        from auto_research.sandbox.env_build import solve
        from auto_research.sandbox.jail import jail_enabled

        work = Path(tempfile.mkdtemp(prefix="solve-", dir=self._scratch()))
        try:
            lock = solve(specs, conda_exe=conda_executable(), work=work, jailed=jail_enabled(),
                         runner=self._runner)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return f"Not solved: {exc}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if refused := self._write(folder / "env.lock", lock):
            return refused
        count = sum(1 for line in lock.splitlines() if line.startswith("https://"))
        return (f"Wrote envs/{folder.name}/env.lock: {count} conda-forge packages for {specs}. "
                f"Call cand_env_activate('{folder.name}') to build it and run in it.")

    def _pin(self, env_id: str, folder: Path, requirements: list[str]) -> str:
        from auto_research.sandbox.env_build import pin_wheels
        from auto_research.sandbox.jail import jail_enabled

        from auto_research.sandbox.env_build import read_definition

        try:
            derived = read_definition(folder).base
        except ValueError:
            derived = None
        if derived:
            return (f"envs/{env_id} layers on the human environment {derived}, which takes no env.pip: "
                    "remove `base` from env.yaml and solve a lock to pin wheels.")
        python = self.cand_dir / "conda" / "envs" / f"ar-{env_id}" / "bin" / "python"
        if self._built_lock.get(env_id) != _digest(folder / "env.lock"):
            return (f"Activate the candidate built from the current env.lock first "
                    f"(cand_env_activate('{env_id}')): its Python picks the wheels.")
        if not python.exists():
            return f"Activate the candidate first (cand_env_activate('{env_id}')): its Python picks the wheels."
        work = Path(tempfile.mkdtemp(prefix="pin-", dir=self._scratch()))
        try:
            lines = pin_wheels(python, requirements, work=work, jailed=jail_enabled(), runner=self._runner)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return f"Not pinned: {exc}"
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if refused := self._write(folder / "env.pip", "\n".join(lines) + "\n"):
            return refused
        return (f"Wrote envs/{env_id}/env.pip ({len(lines)} wheels). Call cand_env_activate('{env_id}') "
                "again to rebuild with them.")

    def _locked(self, step, *args):
        """One tool at a time: a solve, pin or build sees the definition and candidate the previous
        one left (the model may issue several in one turn)."""
        with self._lock:
            return step(*args)

    def _activate(self, env_id: str, folder: Path) -> str:
        from auto_research.env import analysis_environment, conda_executable, declared_externals
        from auto_research.sandbox.env_build import build, read_definition, validate
        from auto_research.sandbox.jail import jail_enabled

        human = analysis_environment()
        bases = {human.name: human} if human is not None else {}
        externals = declared_externals()
        problems = validate(folder, base_envs=bases, externals=externals)
        if problems:
            return "The definition cannot be built yet:\n" + "\n".join(f"- {p}" for p in problems)
        try:
            environment = build(read_definition(folder), conda_dir=self.cand_dir / "conda",
                                conda_exe=conda_executable(), base_envs=bases, externals=externals,
                                jailed=jail_enabled(), runner=self._runner)
        except Exception as exc:  # noqa: BLE001 — a failed build is the tool's answer, never a crash
            # A rebuild replaces the prefix the wrappers point at: they go with it.
            self._clear_wrappers()
            return (f"The candidate did not build, and your shell runs in the analysis environment: "
                    f"{type(exc).__name__}: {str(exc)[-800:]}")
        if self._closed:
            return "The authoring attempt has ended; the candidate was not activated."
        self._built_lock[env_id] = _digest(folder / "env.lock")
        # A derived environment runs the human base as it is; adding a package means a built one.
        where = ("env_solve's specs, as a built environment (remove `base` from env.yaml)"
                 if read_definition(folder).base else "env_solve's specs")
        missing = self._write_wrappers(environment, folder / "activate.sh", where)
        note = (f" It lacks {', '.join(missing)}; to run {'them' if len(missing) > 1 else 'it'} here, "
                f"add {'them' if len(missing) > 1 else 'it'} to {where}." if missing else "")
        sourced = (f"envs/{env_id}/activate.sh" if (folder / "activate.sh").is_file()
                   else "its base environment's activation" if environment.activate else "no activation")
        return (f"Your shell now runs in the candidate ar-{env_id}, from a clean start: plain `python`, "
                f"`pytest` and the environment's other commands use it, with {sourced} sourced first "
                f"and your working directory on PYTHONPATH.{note} Call cand_env_activate('{env_id}') "
                f"again after adding an activate.sh, and cand_env_activate() with no env id to return.")

    def _write_wrappers(self, environment, activate: Path, where: str) -> list[str]:
        """One wrapper per executable of the candidate, each running it the way a worker's shell
        runs in an environment (:func:`~auto_research.sandbox.envs.in_environment`: a clean start,
        the activation, the candidate first on ``PATH``, the worktree on ``PYTHONPATH``, the jail's
        parameter folder kept), never on top of the analysis environment. The activation is the
        worktree's ``activate.sh`` (edits apply on the next command), or the built one when the
        definition has none (a derived environment's base activation). A core command the candidate
        lacks answers so."""
        import dataclasses

        from auto_research.sandbox.envs import in_environment

        self._clear_wrappers()
        bin_dir = environment.prefix / "bin"
        if activate.is_file():
            environment = dataclasses.replace(environment, activate=activate)
        elif environment.activate is not None and not any(
            Path(environment.activate).resolve().is_relative_to(root.resolve())
            for root in (self.cand_dir, self.worktree)
        ):
            # A derived candidate's base activation (the human's init script) can lie outside the
            # coder's jail: a copy in the candidate folder, which the jail shows read-only.
            copy = self.cand_dir / "activation" / Path(environment.activate).name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(environment.activate, copy)
            environment = dataclasses.replace(environment, activate=copy)
        names = set()
        for entry in sorted(bin_dir.iterdir()):
            if not (entry.is_file() or entry.is_symlink()) or entry.name.startswith("."):
                continue
            names.add(entry.name)
            command = f'{shlex.quote(str(entry))} "$@"'
            self._wrapper(entry.name, in_environment(environment, command, self.worktree, pass_args=True) + "\n")
        missing = [name for name in self.CORE if name not in names]
        for name in missing:
            message = shlex.quote(f"`{name}` is not in this candidate: add it to {where}.")
            self._wrapper(name, f"echo {message} >&2\nexit 127\n")
        return missing

    def _wrapper(self, name: str, body: str) -> None:
        wrapper = self.active_bin / name
        wrapper.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        wrapper.chmod(0o755)

    def _deactivate(self) -> None:
        with self._lock:
            self._clear_wrappers()

    @staticmethod
    def attempt_dir(root: Path, tool: str) -> Path:
        """A fresh candidate folder for one attempt at ``tool`` under ``root``; earlier attempts'
        folders are removed first, best-effort (a build orphaned by a stop may still be writing
        into one, so each attempt has its own)."""
        from auto_research.sandbox.env_build import _remove_tree

        root = Path(root)
        if root.is_dir():
            for old in root.glob(f"{tool}-*"):
                try:
                    _remove_tree(old, missing_ok=True)
                except Exception:  # noqa: BLE001 — leftovers cost disk only; the new folder is fresh
                    pass
        return root / f"{tool}-{uuid.uuid4().hex[:8]}"

    def _clear_wrappers(self) -> None:
        for entry in self.active_bin.iterdir():
            entry.unlink(missing_ok=True)

    def _scratch(self) -> Path:
        scratch = self.cand_dir / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch


def _digest(path: Path) -> str | None:
    """The sha256 of ``path``, or ``None`` when absent."""
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
