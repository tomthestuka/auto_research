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

from auto_research.config_paths import orchestration_yaml
from auto_research.schemas.figures import FigureRef
from auto_research.schemas.tools import ToolResult

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
    if ini_sh in _ini_env_cache:
        return _ini_env_cache[ini_sh]
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
    _ini_env_cache[ini_sh] = env
    return env


def check_analyze_probe(*, config_path: str | Path | None = None) -> None:
    """Preflight: run the configured stack smoke-test under ``analyze_env`` (skip if unset).

    Reads ``analyze_probe``/``analyze_env``/``analyze_ini_sh``/``toolkit_root`` from
    ``config/orchestration.yaml``, sources the analyst env + sets PYTHONPATH the same way the
    sandbox does, then runs ``{analyze_env}/bin/python -c "{analyze_probe}"``. A non-zero exit
    (broken or missing analysis stack) fails the launch with the captured error. An empty or
    absent ``analyze_probe`` is a no-op — the escape hatch for a domain (or a literature-only
    deployment) that configures no probe.
    """
    import yaml  # local — keep the preflight import-light

    config_path = Path(config_path) if config_path is not None else orchestration_yaml()
    try:
        with Path(config_path).open("r", encoding="utf-8") as stream:
            orch = yaml.safe_load(stream) or {}
    except OSError:
        return  # missing live config is reported by the config-loader checks, not here
    probe = (orch.get("analyze_probe") or "").strip()
    if not probe:
        return
    analyze_env = orch.get("analyze_env")
    analyze_ini_sh = orch.get("analyze_ini_sh") or None
    toolkit_root = orch.get("toolkit_root")
    python_exec = str(Path(analyze_env) / "bin" / "python") if analyze_env else sys.executable
    base_env = _source_ini_env(analyze_ini_sh) if analyze_ini_sh else dict(os.environ)
    if toolkit_root:
        existing = base_env.get("PYTHONPATH", "")
        base_env = {
            **base_env,
            "PYTHONPATH": str(toolkit_root) + (":" + existing if existing else ""),
        }
    try:
        completed = subprocess.run(
            [python_exec, "-c", probe],
            capture_output=True,
            text=True,
            env=base_env,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"analyze_probe failed to run ({python_exec}): {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-800:]
        raise RuntimeError(
            f"analyze_probe `{probe}` exited {completed.returncode} under {python_exec}:\n{detail}"
        )


PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"stdout", "stderr", "exit_code", "figures", "console_tail"}
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

    # A dict payload carries `figures` + `stdout`; anything else is plain text, left as-is.
    figures: list[FigureRef] = []
    try:
        payload = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        figures, notes = parse_tool_figures(payload.get("figures"))
        stdout = payload.get("stdout", stdout)
        if notes:
            stderr = (stderr + "\n" if stderr else "") + "[figure capture] " + " | ".join(notes)

    return ToolResult(
        tool_name=f"{module}.{fn}",
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        figures=figures,
        elapsed_s=elapsed,
    )
