"""Subprocess execution sandbox for research_toolkit tools."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from auto_research.schemas.figures import FigureRef

# Cache: ini_sh path → env dict sourced from that script.
_ini_env_cache: dict[str, dict[str, str]] = {}


#: The only variables the init script is sourced *with*: identity, locale and proxy settings a
#: download may need. Everything else the parent holds — every API key ``load_dotenv`` put there,
#: the launching shell's ``PWD`` (this repository), host-tooling variables — stays out, because the
#: result becomes the environment of the analysis REPL, the jailed shell and the gate's test run.
#: (The author harnesses also start from it, but both SDKs merge their ``env`` over the parent's
#: ``os.environ``, so an author still inherits the parent's variables.) Measured 2026-10-06 (F20
#: review): 21 key/token variables reached the jailed, network-enabled shell before this.
_INI_BASE_VARS = (
    "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)
_INI_BASE_PATH = "/usr/local/bin:/usr/bin:/bin"
#: Bash's own bookkeeping, dropped from the result so nothing names the directory it ran in.
_INI_SHELL_VARS = frozenset({"PWD", "OLDPWD", "SHLVL", "_"})


def _source_ini_env(ini_sh: str) -> dict[str, str]:
    """Source a bash init script in a **clean** environment and return the result.

    The script starts from :data:`_INI_BASE_VARS` and a system ``PATH`` only (``env -i``
    semantics), from ``/`` as its working directory, under ``bash --norc --noprofile`` with stdin
    from ``/dev/null``: bash otherwise sources ``~/.bashrc`` whenever stdin is a socket (the
    detached backend's case — measured 2026-10-06, 66 variables instead of 35, conda activation
    and ``SSH_*`` among them). The result is exactly what the script sets plus that base, never a
    credential of the parent process. A missing script is an error. Uses ``env -0``
    (NUL-separated output) for robustness. Errors from interactive shell components
    (gnome-keyring, SSH agent) are suppressed via ``2>/dev/null``. The result is cached so the
    script is only sourced once per interpreter lifetime.

    Raises ``RuntimeError`` on any failure — no fallback to ``os.environ``.
    """
    try:  # keyed on the script's mtime too: an edited script is sourced again
        cache_key = f"{ini_sh}@{Path(ini_sh).expanduser().stat().st_mtime_ns}"
    except OSError:
        cache_key = ini_sh

    if cache_key in _ini_env_cache:
        return _ini_env_cache[cache_key]
    base = {name: os.environ[name] for name in _INI_BASE_VARS if name in os.environ}
    base["PATH"] = _INI_BASE_PATH
    script = str(Path(ini_sh).expanduser().resolve())  # cwd is "/", so a relative path would miss
    if not Path(script).is_file():
        # `source` of a missing file fails silently under 2>/dev/null and leaves only the base.
        raise RuntimeError(f"Analyst env script {ini_sh!r} does not exist (resolved: {script})")
    try:
        result = subprocess.run(
            ["bash", "--norc", "--noprofile", "-c", f"source {script!r} 2>/dev/null; env -0"],
            capture_output=True,
            text=True,
            timeout=15,
            env=base,
            cwd="/",
            stdin=subprocess.DEVNULL,
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
            if k not in _INI_SHELL_VARS:
                env[k] = v
    if not env:
        raise RuntimeError(f"Sourcing analyst env {ini_sh!r} produced an empty environment")
    _ini_env_cache[cache_key] = env
    return env


def builtin_function_source(name: str) -> str:
    """The source text of one function of ``builtin_server.py``, read without importing the server
    (it starts an MCP instance on import): the one definition of a rule the REPL script and the
    gate's recorder run too (``private_folders``)."""
    import ast

    path = Path(__file__).with_name("builtin_server.py")
    text = path.read_text(encoding="utf-8")
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(text, node) + "\n"


def _builtin_function(name: str):
    """One function of ``builtin_server.py`` as this process runs it: the same source the REPL
    script and the gate's recorder carry, so the rule has one definition."""
    namespace: dict = {}
    exec(compile(builtin_function_source(name), f"<builtin_server.{name}>", "exec"), namespace)
    return namespace[name]


#: ``builtin_server.private_folders``: each declared private variable's first folder replaced by one
#: of the process's own (F23) or of one call's (G4); the declaration comes from ``env.private_variables``.
private_folders = _builtin_function("private_folders")


def run_in_environment(
    environment, command: str, *, toolkit_root: Path, label: str, timeout: int = 300,
    toolkit_on_path: bool = True,
) -> subprocess.CompletedProcess:
    """Run one shell ``command`` in ``environment`` the way a unit's REPL and shell start: the
    shared launcher (``envs.in_environment``: a clean start, the activation sourced there, the
    toolkit on ``PYTHONPATH``) inside the analysis jail (``jail.analysis_policy``, the environment's
    prefix, activation and boot file bound read-only). A pipeline environment's
    activation is never sourced in this process. Used by the launch checks (the probe, the private
    variables) and the coder's shell variables. ``toolkit_on_path=False``: the toolkit is bound but
    left off ``PYTHONPATH``."""
    from auto_research.sandbox.envs import in_environment
    from auto_research.sandbox.jail import analysis_policy, jail_home, private_tmp, wrap

    home = jail_home(label)
    tmp = private_tmp(label, home)
    script = in_environment(environment, command, toolkit_root if toolkit_on_path else None)
    readonly = tuple(str(p) for p in (environment.prefix, environment.activate, environment.boot) if p is not None)
    argv = wrap(["/bin/bash", "--noprofile", "--norc", "-c", script],
                analysis_policy(label=label, toolkit_root=toolkit_root, tmp=tmp, home=home,
                                readonly=readonly))
    env = {name: os.environ[name] for name in _INI_BASE_VARS if name in os.environ}
    env.update(PATH=_INI_BASE_PATH, HOME=str(home), TMPDIR="/tmp")
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, cwd=str(home),
                          stdin=subprocess.DEVNULL)


#: The jailed one-shot call: ``argv[1]`` a JSON ``[module, fn, args, result file]``; the return is
#: written as JSON to the result file, in the jail's private ``/tmp`` (stdout stays the activation's).
_CALL_SCRIPT = (
    "import importlib, json, sys\n"
    "module, fn, args, out = json.loads(sys.argv[1])\n"
    "value = getattr(importlib.import_module(module), fn)(*args)\n"
    "with open(out, 'w', encoding='utf-8') as handle:\n"
    "    json.dump(value, handle, default=str)\n"
)


#: The largest return a jailed one-shot call may hand back.
_CALL_RESULT_CAP = 64 * 1024 * 1024


def call_in_jail(module: str, fn: str, args: list, *, toolkit_root: Path, label: str, timeout: int = 300,
                 environment=None):
    """``module.fn(*args)`` from the toolkit at ``toolkit_root``, run once inside the analysis jail in
    ``environment`` (default: the toolkit's default one; :func:`run_in_environment`, ``python -P``,
    bytecode in the jail's private ``/tmp``), and its JSON-decoded return, written by the call to a
    file in that private ``/tmp`` and read here. The orchestrator never imports toolkit code (D25):
    the lead's catalog read and the launch's catalog health check come here. Raises
    ``RuntimeError`` naming the call when it fails or its return does not decode."""
    import shlex
    import uuid

    from auto_research.env import analysis_environment
    from auto_research.sandbox.jail import jail_home, private_tmp

    environment = environment if environment is not None else analysis_environment()
    name = f"ar-call-{uuid.uuid4().hex}.json"
    command = ("PYTHONPYCACHEPREFIX=/tmp/ar-pycache "  # never a planted in-tree `.pyc`
               + " ".join(shlex.quote(part) for part in (
                   str(environment.python), "-P", "-c", _CALL_SCRIPT,
                   json.dumps([module, fn, list(args), f"/tmp/{name}"]))))
    completed = run_in_environment(environment, command, toolkit_root=Path(toolkit_root), label=label,
                                   timeout=timeout)
    # The host side of the jail's /tmp (run_in_environment's own: the label's private folder).
    result = private_tmp(label, jail_home(label)) / name
    call = f"{module}.{fn}"
    try:
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            raise RuntimeError(f"{call} failed in the jail: "
                               + (detail[-1] if detail else f"exit {completed.returncode}"))
        try:
            # Written by toolkit code: opened without following a link, a regular file, capped.
            import stat

            fd = os.open(result, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("not a regular file")
                data = handle.read(_CALL_RESULT_CAP + 1)
            if len(data) > _CALL_RESULT_CAP:
                raise ValueError(f"larger than {_CALL_RESULT_CAP} bytes")
            return json.loads(data.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"{call} returned no readable JSON from the jail: {exc}") from None
    finally:
        result.unlink(missing_ok=True)


def analysis_activation_env(orch: dict, *, home: str | None = None) -> dict[str, str]:
    """The variables the toolkit's default environment's activation (``env.analysis_environment``)
    sets: sourced by :func:`run_in_environment` inside the analysis jail and read back, without the
    launcher's own variables (``HOME``, ``TMPDIR``, ``AR_*``, the toolkit on ``PYTHONPATH``): a
    caller adds its own. What the activation derived from that jail's home (``/home/ar``) is
    rewritten onto ``home``, the home of the jail the caller hands these values to (an author
    harness's, ``jail.coder_home``); ``None`` leaves them as the activation set them (a launch check
    reading a separator). Raises when the activation fails."""
    from auto_research.env import analysis_environment
    from auto_research.toolkit_contract import toolkit

    completed = run_in_environment(analysis_environment(orch), "/usr/bin/env -0",
                                   toolkit_root=toolkit().root, label="activation", toolkit_on_path=False)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[-800:]
        raise RuntimeError(f"the default environment's activation failed (exit {completed.returncode}): {detail}")
    pairs = [entry.partition("=") for entry in completed.stdout.split("\0") if "=" in entry]
    seen_home = next((value for name, _, value in pairs if name == "HOME"), "").rstrip("/")
    home = (home or "").rstrip("/")
    # The launcher's home as a whole leading path (a value's start, or after ':', ';', '='), as the
    # environment fingerprint normalises HOME.
    at_home = (re.compile(r"(^|[:;=])" + re.escape(seen_home) + r"(?=/|[:;]|$)")
               if len(seen_home) > 1 and home and home != seen_home else None)
    env = {name: (at_home.sub(lambda m: m.group(1) + home, value) if at_home else value)
           for name, _, value in pairs
           if name not in _INI_SHELL_VARS and name not in ("HOME", "TMPDIR") and not name.startswith("AR_")}
    # The launcher's empty toolkit entry would read as the working folder on sys.path.
    path = os.pathsep.join(part for part in env.pop("PYTHONPATH", "").split(os.pathsep) if part)
    return {**env, **({"PYTHONPATH": path} if path else {})}


def check_analysis_probe() -> None:
    """Preflight: the toolkit's default environment runs its definition's ``probe`` (one line of
    Python in ``env.yaml``; none, nothing runs), started as a unit's REPL is
    (:func:`run_in_environment`, inside the jail). A non-zero exit fails the launch with the captured
    error, which usually names a root the jail does not bind. The definition comes
    from the process's bound toolkit contract, as every other reader takes it."""
    import shlex

    from auto_research.env import analysis_environment, default_definition
    from auto_research.toolkit_contract import toolkit

    probe = default_definition().probe
    if not probe:
        return
    environment = analysis_environment()
    command = f"{shlex.quote(str(environment.python))} -c {shlex.quote(probe)}"
    try:
        completed = run_in_environment(environment, command, toolkit_root=toolkit().root, label="probe")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"the probe of {environment.name} failed to run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-800:]
        hint = (" Inside the OS jail this usually means something the stack needs lies outside "
                "`agent_fs_allow` (often a folder whose contents symlink out of it): add that root to "
                "`agent_fs_readonly`.")
        raise RuntimeError(f"probe `{probe}` of {environment.name} exited {completed.returncode}:\n{detail}{hint}")


PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"stdout", "stderr", "exit_code", "figures", "products", "console_tail"}
)


def normalize_tool_raw(raw: dict) -> dict:
    """Ensure every tool result has a non-empty ``stdout`` for the model to read — the text the
    model sees and the call's result receipt holds. Shared by the analyst runtime and the gate.

    Tools that return structured payloads without a ``stdout`` key (e.g.
    ``list_dir`` → ``{path, entries}``, ``load_data`` → nested indices dict)
    are invisible to the model otherwise.  When ``stdout`` is absent or empty,
    serialize the non-protocol payload keys as compact JSON. Also folds any
    captured ``console_tail`` (XSPEC/C-level prints) into ``stdout``.
    """
    stdout = raw.get("stdout") or ""
    if not stdout:
        extra = {k: v for k, v in raw.items() if k not in PROTOCOL_KEYS}
        if extra:
            stdout = json.dumps(extra, indent=2, default=str)

    console = raw.get("console_tail") or ""
    if console:
        sep = "\n--- console ---\n"
        stdout = (stdout + sep + console) if stdout else (sep.lstrip() + console)

    return {**raw, "stdout": stdout}


def tool_text(raw: dict) -> tuple[str, int]:
    """``(text, exit_code)`` for one normalised tool return: the text the model reads and the call's
    result receipt holds. Shared by the analyst runtime and the gate's return check."""
    exit_code = raw.get("exit_code", 1)
    if exit_code == 0:
        return raw.get("stdout", "") or "(tool returned no output)", exit_code
    return f"[exit {exit_code}]\n{raw.get('stderr') or raw.get('stdout') or '(no error detail)'}", exit_code


def parse_tool_figures(raw_figures: object) -> tuple[list[FigureRef], list[str]]:
    """Validate a tool's returned ``figures`` value into ``FigureRef``s, dropping each malformed
    entry with a reason. The one reading of the figure contract, shared by every tool path.

    A tool — most often a freshly authored plotter — that emits a slightly-off figure dict
    (bad ``sha256``, an extra key under ``extra="forbid"``, a missing field, a bad
    ``created_at``) costs only that figure: each bad entry is dropped and a short reason is
    returned so the analyst learns its plotter is malformed and can fix or re-author it — what
    ``coder.md`` promises ("that figure is dropped"). Returns ``(figures, drop_notes)``.
    """
    figures: list[FigureRef] = []
    notes: list[str] = []
    if not isinstance(raw_figures, list):
        if raw_figures:
            notes.append(f"'figures' is not a list ({type(raw_figures).__name__}); ignored")
        return figures, notes
    for index, entry in enumerate(raw_figures):
        try:
            figures.append(FigureRef.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 — any malformed figure is dropped, never fatal
            notes.append(f"figure[{index}] dropped — invalid FigureRef: {exc}")
    return figures, notes
