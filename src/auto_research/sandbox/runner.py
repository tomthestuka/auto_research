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
    toolkit_on_path: bool = True, store_env: dict[str, str] | None = None,
    readonly: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """Run one shell ``command`` in ``environment`` the way a unit's REPL and shell start: the
    shared launcher (``envs.in_environment``: a clean start, the activation sourced there, the
    toolkit on ``PYTHONPATH``) inside the analysis jail (``jail.analysis_policy``, the environment's
    prefix, activation and boot file bound read-only). A pipeline environment's
    activation is never sourced in this process. Used by the launch checks (the probe, the private
    variables) and the coder's shell variables. ``toolkit_on_path=False``: the toolkit is bound but
    left off ``PYTHONPATH``. ``store_env``: the calling unit's store credentials
    (``store.keys.Credentials.env``), with the toolkit's commit and the environment's stamped
    fingerprint beside them (:func:`keyed_env`), exported to ``command`` after the activation from a
    file in the jail's private ``/tmp`` (:func:`write_store_env`), never on a command line.
    ``readonly``: further folders bound read-only (the human's producer job reads its sources there,
    ``python -m auto_research.store produce``)."""
    from auto_research.sandbox.envs import in_environment
    from auto_research.sandbox.jail import analysis_policy, jail_home, private_tmp, wrap

    label = _keyed_label(label, store_env)
    home = jail_home(label)
    tmp = private_tmp(label, home)
    if store_env:
        from auto_research.provenance.capture import toolkit_commit

        store_env = keyed_env(store_env, commit=toolkit_commit(Path(toolkit_root)),
                              fingerprint=_call_stamp(environment))
    secrets_file = write_store_env(store_env, tmp, shell=True)
    try:
        if secrets_file is not None:
            command = (f"builtin set -a; builtin source /tmp/{secrets_file.name}; builtin set +a; "
                       f"/usr/bin/rm -f /tmp/{secrets_file.name}; {command}")
        script = in_environment(environment, command, toolkit_root if toolkit_on_path else None)
        readonly = tuple(str(p) for p in (environment.prefix, environment.activate, environment.boot)
                         if p is not None) + tuple(str(p) for p in readonly)
        argv = wrap(["/bin/bash", "--noprofile", "--norc", "-c", script],
                    analysis_policy(label=label, toolkit_root=toolkit_root, tmp=tmp, home=home,
                                    readonly=readonly))
        env = {name: os.environ[name] for name in _INI_BASE_VARS if name in os.environ}
        env.update(PATH=_INI_BASE_PATH, HOME=str(home), TMPDIR="/tmp")
        return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, cwd=str(home),
                              stdin=subprocess.DEVNULL)
    finally:
        if secrets_file is not None:
            secrets_file.unlink(missing_ok=True)


def _keyed_label(label: str, store_env: dict[str, str] | None) -> str:
    """``label``, made the keys' own when a call carries store keys: a one-shot jail's home and
    private ``/tmp`` are one per label, so two units' calls of one label never share the folder their
    keys pass through."""
    if not store_env:
        return label
    import hashlib

    digest = hashlib.sha256(json.dumps(sorted(store_env.items())).encode()).hexdigest()[:12]
    return f"{label}-{digest}"


#: The commit a keyed tool process runs the toolkit at, and the environment state its receipts stamp:
#: the toolkit's store helper writes both onto what it produces, and produces nothing without them.
TOOLKIT_COMMIT_VARIABLE = "AR_TOOLKIT_COMMIT"
ENV_FINGERPRINT_VARIABLE = "AR_ENV_FINGERPRINT"


def keyed_env(store_env: dict[str, str], *, commit: str | None, fingerprint: str | None) -> dict[str, str]:
    """A unit's store keys with :data:`TOOLKIT_COMMIT_VARIABLE` and :data:`ENV_FINGERPRINT_VARIABLE`
    beside them: the values the runtime stamps on that process's receipts. One left out when unknown
    (no commit, an environment with no lock), so the helper refuses rather than writing a guess."""
    out = dict(store_env)
    out.update({name: value for name, value in ((TOOLKIT_COMMIT_VARIABLE, commit),
                                                 (ENV_FINGERPRINT_VARIABLE, fingerprint)) if value})
    return out


def _call_stamp(environment) -> str | None:
    """The environment state a one-shot call's receipt stamps: its fingerprint with the lock written
    (``envs.fingerprint_and_lock``), None when that cannot be read."""
    from auto_research.env import env_lock_dir
    from auto_research.sandbox.envs import fingerprint_and_lock

    try:
        return fingerprint_and_lock(environment, env_lock_dir())
    except Exception:  # noqa: BLE001 - unknown: left out, never guessed
        return None


#: The REPL reads its unit's store credentials from the file this variable names (in its private
#: ``/tmp``), sets them in its environment and removes the file (``persistent._REPL_SCRIPT``).
STORE_ENV_VARIABLE = "AR_STORE_ENV"


def write_store_env(store_env: dict[str, str] | None, private_dir: Path, *, shell: bool = False) -> Path | None:
    """Write a unit's store credentials into ``private_dir`` (a jail's private ``/tmp``, mode 0700,
    local disk) as a fresh mode-0600 file: JSON for the REPL, ``NAME='value'`` lines for a shell
    (``shell``). The way credentials reach a jailed process without a command line (world-readable
    in ``/proc``) or the launcher's clean start. None when there are none."""
    import shlex
    import uuid

    if not store_env:
        return None
    bad = [name for name in store_env if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name)]
    if bad:
        raise ValueError(f"not variable names: {bad}")
    path = Path(private_dir) / f"ar-store-{uuid.uuid4().hex}.env"
    text = ("".join(f"{name}={shlex.quote(value)}\n" for name, value in store_env.items()) if shell
            else json.dumps(store_env))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


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
