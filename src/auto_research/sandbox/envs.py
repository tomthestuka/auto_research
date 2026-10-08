"""Software environments as objects: what a tool ran in, fingerprinted and locked (handoff [F18]).

An environment is three layers, all part of its identity:

```
 env = conda prefix     packages (conda + pip)                    → lock: rebuildable
     + activation       init script → the variables it sets,
                        REPL boot file                             → bytes + captured result
     + externals        software outside conda the activation
                        points at (human-declared)                 → stamped, not rebuildable
```

* :func:`fingerprint` — a sha256 over the exact package set (conda: download URL + md5 of every
  ``conda-meta`` record; pip: name, version and the hash of each ``RECORD``), the activation
  script and boot file bytes, the variables the script sets, and each external's stamp files.
  Cached per environment on the timestamps that move when any of those change, so a call costs a
  few ``stat``\\ s when nothing did.
* :func:`write_lock` — the reconstruction artifact for a fingerprint, written once under a lock
  directory: a conda explicit list (``@EXPLICIT`` url#md5 lines, rebuildable with
  ``conda create --file``), pip pins, the activation and the external stamps.

This module is domain-neutral and reads no configuration: the application builds the
:class:`Environment` (``auto_research.env.analysis_environment``) and passes it in.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class External:
    """Software outside conda an environment's activation points at, declared by the human.

    Stamped, not rebuilt: ``stamps`` are small files under ``root`` whose bytes identify the
    installation (a build record, a calibration index); the fingerprint hashes them.
    """

    name: str
    root: Path
    stamps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Environment:
    """One software environment: a conda prefix, its activation, and the externals it uses."""

    name: str
    prefix: Path
    owner: str = "human"
    activate: Path | None = None
    boot: Path | None = None
    externals: tuple[External, ...] = field(default=())

    @property
    def python(self) -> Path:
        return self.prefix / "bin" / "python"

    def activation_env(self) -> dict[str, str]:
        """The variables a process in this environment starts from: what the activation script
        produces when sourced clean, or the current process's environment when there is none.

        A pipeline environment's activation is coder-written and is sourced only inside a jail, by
        the process that runs there (the shell: ``react._in_environment``); asking for it here
        raises rather than running it with the runtime's reach."""
        if self.owner == "pipeline" and self.activate is not None:
            raise RuntimeError(
                f"{self.name}: a pipeline environment's activation is sourced inside its jail only"
            )
        if self.activate is None:
            return dict(os.environ)
        from auto_research.sandbox.runner import _source_ini_env

        return dict(_source_ini_env(str(self.activate)))


# ── fingerprint ──────────────────────────────────────────────────────────────────────────────


def _site_packages(prefix: Path) -> list[Path]:
    return sorted(prefix.glob("lib/python3*/site-packages"))


def _mtime(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _cache_key(env: Environment) -> tuple:
    """What moves when the fingerprint's inputs change: conda's history (every conda transaction),
    each site-packages directory (a pip install touches it, not conda's history), the activation
    and boot files, and every external stamp."""
    return (
        str(env.prefix),
        _mtime(env.prefix / "conda-meta" / "history"),
        _mtime(env.prefix / "conda-meta"),
        tuple(_mtime(sp) for sp in _site_packages(env.prefix)),
        _mtime(env.activate), _mtime(env.boot),
        tuple((x.name, tuple(_mtime(x.root / s) for s in x.stamps)) for x in env.externals),
    )


_CACHE: dict[tuple, tuple[str, dict]] = {}
_LOCK = threading.Lock()


def _state(env: Environment) -> tuple[str, dict]:
    """``(fingerprint, components)`` computed once together, so a lock written from the
    components always hashes to the fingerprint it is filed under; cached on :func:`_cache_key`."""
    key = _cache_key(env)
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    components = _components(env)
    digest = hashlib.sha256(json.dumps(components, sort_keys=True).encode("utf-8")).hexdigest()
    if "error" not in components["activation"]:  # a transient failure is retried, never kept
        with _LOCK:
            _CACHE[key] = (digest, components)
    return digest, components


def fingerprint(env: Environment) -> str:
    """The sha256 identifying ``env``'s exact state; cached on :func:`_cache_key`."""
    return _state(env)[0]


def _components(env: Environment) -> dict:
    # A pipeline prefix was written by a build's own scripts: a link there is not followed, so it
    # cannot bring a file from outside the prefix into a listing (B2b review).
    follow = env.owner != "pipeline"
    return {
        "conda": _conda_records(env.prefix, follow=follow),
        "pip": _pip_records(env.prefix, follow=follow),
        "activation": _activation(env),
        "boot": _file_sha(env.boot),
        "externals": {x.name: _external_stamp(x) for x in env.externals},
    }


def _conda_records(prefix: Path, *, follow: bool = True) -> list[str]:
    """``url#md5`` of every package conda installed (its own records, not a solver)."""
    out: list[str] = []
    for record in sorted((prefix / "conda-meta").glob("*.json")):
        try:
            meta = json.loads(_read_regular(record, follow=follow))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(meta, dict):
            continue
        url = meta.get("url") or meta.get("fn") or record.stem
        out.append(f"{url}#{meta.get('md5', '')}")
    return out


def _pip_records(prefix: Path, *, follow: bool = True) -> list[str]:
    """``name==version`` plus the hash of its ``RECORD`` for every package pip (not conda)
    installed — conda's records do not list them."""
    out: list[str] = []
    for site in _site_packages(prefix):
        for dist in sorted(site.glob("*.dist-info")):
            installer = dist / "INSTALLER"
            try:
                if _read_regular(installer, limit=1024, follow=follow).decode("utf-8", "replace").strip() == "conda":
                    continue
            except (OSError, ValueError):
                pass
            name, _, version = dist.name[: -len(".dist-info")].partition("-")
            source = _pip_source(dist, follow=follow)
            if source is None:
                pin = f"{name}=={version}"
            elif source.startswith("-e "):
                pin = source  # pip's editable requirement: `-e <url>`
            else:
                pin = f"{name} @ {source}"
            out.append(f"{pin}\t{_file_sha(dist / 'RECORD', follow=follow) or ''}".rstrip())
    return out


def _pip_source(dist: Path, *, follow: bool = True) -> str | None:
    """Where a pip package came from when not an index: its ``direct_url.json`` URL (``-e`` for an
    editable install), so the lock never asks an index for a name it never came from."""
    try:
        data = json.loads(_read_regular(dist / "direct_url.json", limit=1024 * 1024, follow=follow))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    if not url:
        return None
    editable = (data.get("dir_info") or {}).get("editable")
    vcs = data.get("vcs_info") or {}
    if vcs.get("vcs") and vcs.get("commit_id"):  # pip's own spelling: git+<url>@<commit>
        url = f"{vcs['vcs']}+{url}@{vcs['commit_id']}"
    if data.get("subdirectory"):
        url = f"{url}#subdirectory={data['subdirectory']}"
    return f"-e {url}" if editable else url


def _activation(env: Environment) -> dict:
    """The activation script's bytes and the variables it sets (nothing for none).

    A pipeline environment's activation is coder-written: it is identified by its bytes alone and
    never sourced here, in the runtime, where it would run with the user's full reach; it runs only
    inside a jail (:func:`Environment.activation_env` refuses it)."""
    if env.activate is None:
        return {}
    if env.owner == "pipeline":
        return {"script": _file_sha(env.activate)}
    from auto_research.sandbox.runner import _INI_BASE_VARS, _source_ini_env

    try:
        result = _source_ini_env(str(env.activate))
    except RuntimeError as exc:
        return {"script": _file_sha(env.activate), "error": str(exc)}
    # What the script derives from HOME (``PFILES=$HOME/pfiles;...``) is written back as ``$HOME``,
    # so the identity is the environment's, not the user's. Only HOME, and only as a whole leading
    # path -- at the start of the value or of a list entry (after ``:``, ``;``, ``=``), followed by
    # ``/``, a separator or the end; a raw substring replace rewrote unrelated paths (USER ``li``
    # inside ``libc``). Sourcing under a placeholder HOME was tried and rejected: a real package
    # setup then failed to make its parameter folder under it and stopped early (6 of 29 variables). Known edge: a
    # HOME that is itself a prefix of unrelated paths (``/opt``) is still rewritten.
    home = (result.get("HOME") or "").rstrip("/")
    # Any slashes after HOME collapse to one, so a HOME spelled with a trailing slash
    # (``$HOME//pfiles``) is the same identity.
    pattern = re.compile(r"(^|[:;=])" + re.escape(home) + r"(/+|(?=[:;]|$))") if len(home) > 1 else None

    def normalised(value: str) -> str:
        if pattern is None:
            return value
        return pattern.sub(lambda m: m.group(1) + "$HOME" + ("/" if m.group(2) else ""), value)

    set_vars = {k: normalised(v) for k, v in result.items() if k not in _INI_BASE_VARS}
    return {"script": _file_sha(env.activate), "vars": set_vars}


def _external_stamp(external: External) -> dict:
    return {
        "root": str(external.root),
        "stamps": {s: _file_sha(external.root / s) for s in external.stamps},
    }


#: Largest file the fingerprint reads or hashes (bytes). Larger files are identified by their
#: size instead: a prefix may hold anything, and the fingerprint is taken on every sandbox call.
_MAX_READ = 64 * 1024 * 1024


def _read_regular(path: Path, *, limit: int = 16 * 1024 * 1024, follow: bool = True) -> bytes:
    """The bytes of ``path`` if it is a regular file of at most ``limit`` bytes: opened without
    blocking (a FIFO would hang the caller) and checked on the descriptor. Raises ``ValueError``
    otherwise. Links are followed (human environments use them) unless ``follow`` is false."""
    import stat

    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"{path.name} is not a regular file under {limit} bytes")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} grew past {limit} bytes")
    return data


def _file_sha(path: Path | None, *, follow: bool = True) -> str | None:
    """sha256 of a regular file, streamed; ``size:<n>`` for one over :data:`_MAX_READ`; ``None``
    when it is missing or not a regular file (a FIFO, a device)."""
    import stat

    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | (0 if follow else os.O_NOFOLLOW))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > _MAX_READ:
            return f"size:{info.st_size}"
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


# ── description (what `list_env` shows) ──────────────────────────────────────────────────────


def describe(env: Environment, *, detail: bool = False) -> dict:
    """What an agent sees of an environment: name, owner, Python version, fingerprint and, with
    ``detail``, its packages (conda and pip), the names of the variables its activation sets, and
    its externals. Variable *values* are not shown: names say what the activation provides."""
    digest, parts = _state(env)
    conda = [_package_of(line) for line in parts["conda"]]
    python = next((version for name, version in conda if name == "python"), None)
    out = {"name": env.name, "owner": env.owner, "python": python, "fingerprint": f"env:{digest[:12]}"}
    if detail:
        out["conda"] = sorted(f"{name}=={version}" for name, version in conda)
        out["pip"] = sorted(line.split("\t", 1)[0] for line in parts["pip"])
        if env.owner != "pipeline":  # a pipeline activation is never sourced here, so not listed
            out["activation_sets"] = sorted((parts["activation"].get("vars") or {}).keys())
        out["externals"] = {
            name: {"root": x["root"], "stamped": sorted(k for k, v in x["stamps"].items() if v)}
            for name, x in parts["externals"].items()
        }
    return out


def _package_of(record_line: str) -> tuple[str, str]:
    """``(name, version)`` from a conda record's ``url#md5`` line (``name-version-build.conda``)."""
    filename = record_line.split("#", 1)[0].rsplit("/", 1)[-1]
    stem = filename.removesuffix(".conda").removesuffix(".tar.bz2")
    parts = stem.rsplit("-", 2)
    return (parts[0], parts[1]) if len(parts) == 3 else (stem, "")


# ── locks ────────────────────────────────────────────────────────────────────────────────────


def lock(env: Environment) -> dict:
    """The reconstruction artifact for ``env`` as it is now, keyed by its fingerprint."""
    digest, parts = _state(env)
    return _lock_from(env, digest, parts)


def _lock_from(env: Environment, digest: str, parts: dict) -> dict:
    conda_lines = ["# platform: " + _platform(), "@EXPLICIT"]
    conda_lines += [line for line in parts["conda"] if line.startswith(("http://", "https://", "file://"))]
    return {
        "fingerprint": digest,
        "components": parts,  # what the fingerprint hashes, verbatim
        "environment": env.name,
        "owner": env.owner,
        "prefix": str(env.prefix),
        "conda_explicit": "\n".join(conda_lines) + "\n",
        "pip": [line.split("\t", 1)[0] for line in parts["pip"]],
        # The files themselves, not only their hashes: a rebuild needs them (they are small).
        "activation_script": _file_text(env.activate),
        "boot_file": _file_text(env.boot),
        "externals": parts["externals"],
    }


def write_lock(env: Environment, lock_dir: Path) -> Path:
    """Write ``env``'s lock under ``lock_dir/<fingerprint>.json`` once, and return its path."""
    digest, parts = _state(env)
    return _write(env, digest, parts, Path(lock_dir))


def _write(env: Environment, digest: str, parts: dict, lock_dir: Path) -> Path:
    import uuid

    target = lock_dir / f"{digest}.json"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer: threads of one process share a pid, and two writing the same new
        # state at once must not unlink each other's temp file (measured: 200/200 races failed).
        tmp = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(_lock_from(env, digest, parts), indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, target)
    return target


def _file_text(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return _read_regular(path, limit=_MAX_READ).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def fingerprint_and_lock(env: Environment, lock_dir: Path) -> str:
    """``env``'s fingerprint, with its lock written the first time that state is seen.

    What a receipt stamps: the fingerprint names the state, and the lock written beside it is what
    rebuilds it, so no receipt names a state with no lock.
    """
    digest, parts = _state(env)
    if "error" in parts["activation"]:
        # A state that cannot be rebuilt is not stamped: the caller leaves the receipt unstamped.
        raise RuntimeError(f"activation of {env.name} failed: {parts['activation']['error']}")
    _write(env, digest, parts, Path(lock_dir))  # one stat when the lock is already there
    return digest


def _platform() -> str:
    import platform

    machine = platform.machine()
    arch = {"x86_64": "64", "aarch64": "aarch64", "arm64": "arm64"}.get(machine, machine)
    return f"{sys.platform.replace('darwin', 'osx')}-{arch}"


#: Variables a clean shell keeps from the one it starts in (as the activation scripts' own start,
#: ``runner._INI_BASE_VARS``): identity, locale, time zone and proxies, when set.
_SHELL_KEEP = ("USER", "LOGNAME", "LC_ALL", "LC_CTYPE", "TZ", "http_proxy", "https_proxy", "no_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def in_environment(
    environment, command: str, toolkit_root: Path | None, *, pass_args: bool = False, keep: tuple[str, ...] = (),
    quiet: bool = False,
) -> str:
    """``command`` rewritten to run in ``environment``, inside the caller's jail: a fresh shell with
    only the jail's own ``HOME``/``TMPDIR``/``PFILES``, locale, identity and proxies, that
    environment's activation sourced there (never in the runtime: it may be coder-written, and its
    values would carry whatever it could read outside the jail), then ``<prefix>/bin`` first on
    ``PATH`` and the toolkit on ``PYTHONPATH``. Used by a worker's shell (``use_env``) and by the
    coder's candidate wrappers (``coder.env_tools``).

    What an activation could do to the command is undone before it runs: functions, aliases and
    shell options it set are cleared, the working folder restored, and only the writable half of
    ``PFILES`` replaced (the jail's own). An activation that fails, exits or ends the shell is
    reported: the outer shell checks a marker the inner one writes just before the command.
    ``pass_args``: the outer script's arguments reach ``command`` as ``"$@"`` (a wrapper). ``keep``:
    more variables carried into the clean start when set (the gate's test recorder, B3c-2).
    ``quiet``: the activation's own output goes to stderr (a REPL's stdout is its protocol)."""
    import shlex
    import uuid

    toolkit = [str(toolkit_root), str(Path(toolkit_root).parent)] if toolkit_root is not None else []
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", environment.name)
    steps = ['cd -- "$AR_SHELL_PWD" 2>/dev/null', 'ar_args=("$@")']  # `source` shares "$@"
    if environment.activate is not None:
        steps += [
            f"trap \"echo 'activation of {label} did not finish (it exited, or its last command "
            f"returned non-zero)' >&2; exit 97\" EXIT",
            f"source {shlex.quote(str(environment.activate))}{' >&2' if quiet else ''} || exit 97",
            "trap - EXIT",
        ]
    steps += [
        'builtin unset -f $(builtin compgen -A function) 2>/dev/null',
        "builtin unalias -a 2>/dev/null; builtin shopt -u expand_aliases",
        "builtin set +euo pipefail; builtin set +f; IFS=$' \\t\\n'",
        'builtin cd -- "$AR_SHELL_PWD" 2>/dev/null',
        # The jail's own writable parameter folder; the environment's system half when it set one.
        'if [ -n "$AR_SHELL_PFILES" ]; then case "$PFILES" in *";"*) '
        'builtin export PFILES="${AR_SHELL_PFILES%%;*};${PFILES#*;}";; '
        '*) builtin export PFILES="$AR_SHELL_PFILES";; esac; fi',
        f'builtin export PATH={shlex.quote(str(environment.prefix / "bin"))}:"$PATH"',
        f'builtin export PYTHONPATH={shlex.quote(os.pathsep.join(toolkit))}${{PYTHONPATH:+:$PYTHONPATH}}',
        "builtin export PYTHONNOUSERSITE=1",
        'builtin set -- "${ar_args[@]}"; builtin unset ar_args',
        ': > "$AR_SHELL_MARK"',
        command,
    ]
    # `$$` too: a wrapper written once runs many times at once (`cmd & cmd`, `make -j`).
    mark = f'"${{TMPDIR:-/tmp}}/.ar-shell-{uuid.uuid4().hex}-$$"'
    for name in keep:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"not a variable name: {name!r}")
    keep = " ".join(f'${{{name}:+"{name}=${name}"}}' for name in (*_SHELL_KEEP, *keep))
    return (
        f"AR_SHELL_MARK={mark}; "
        # By absolute path: a candidate's own `env` may sit first on PATH (as a wrapper of itself).
        '/usr/bin/env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" AR_SHELL_PFILES="$PFILES" AR_SHELL_PWD="$PWD" '
        f'AR_SHELL_MARK="$AR_SHELL_MARK" LANG="${{LANG:-C.UTF-8}}" {keep} PATH=/usr/local/bin:/usr/bin:/bin '
        f"/bin/bash --noprofile --norc -c {shlex.quote(chr(10).join(steps))}"
        + (' _ "$@"' if pass_args else "") + "; "
        # A failed activation has said so (97); one that exited quietly is reported here. By path:
        # a candidate's own `rm` may sit first on PATH.
        'ar_rc=$?; if [ ! -e "$AR_SHELL_MARK" ]; then [ "$ar_rc" = 97 ] || '
        f"echo 'activation of {label} ended the shell before your command' >&2; exit 97; fi; "
        '/usr/bin/rm -f "$AR_SHELL_MARK"; exit $ar_rc'
    )
