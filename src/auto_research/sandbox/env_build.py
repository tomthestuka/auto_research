"""Pipeline-owned environments: a committed definition, checked, then built by the runtime (env plan B3).

A definition is a folder the coding pipeline commits (``<toolkit>/envs/<name>/``):

```
env.yaml      purpose, base (a human environment it layers on, or none), externals it uses
env.lock      conda explicit list: @EXPLICIT, then url#md5 (or #sha256:…) per package, conda-forge only
env.pip       pip wheels conda-forge lacks: name==version --hash=sha256:… per line — optional
activate.sh   the activation (variables, sourcing a declared external's init) — optional
boot.py       REPL session setup — optional
```

Two shapes. A **built** environment has an ``env.lock`` and no base: the runtime creates it from
the lock (no solver, the exact files) into ``<conda_dir>/envs/ar-<name>``. A **derived**
environment names a human ``base`` and no lock: it reuses that prefix read-only and layers its own
activation and boot file. Agents never run conda themselves; :func:`build` does, jailed, with the
layout that keeps it away from the human's environments, package cache, conda settings and secrets.

Domain-neutral; reads no configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from auto_research.sandbox.envs import Environment, External

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_CONDA_LINE = re.compile(
    r"^https://conda\.anaconda\.org/conda-forge/(?:noarch|linux-64|linux-aarch64|osx-64|osx-arm64)/"
    r"[A-Za-z0-9_.+-]+\.(?:conda|tar\.bz2)#(?:[0-9a-f]{32}|sha256:[0-9a-f]{64})$"
)
#: A pip requirement the build accepts: an exact version with at least one sha256, nothing else —
#: no URL, no editable path, no option. Installed with --require-hashes --only-binary=:all:, so no
#: package's own build code runs and the bytes are the ones the reviewed line names.
_PIP_LINE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9_.!+-]+(?: --hash=sha256:[0-9a-f]{64})+$"
)
_YAML_KEYS = {"purpose", "base", "externals"}
#: Largest definition file read (bytes); a lock of thousands of packages is well under it.
_MAX_FILE = 4 * 1024 * 1024
#: ``env.yaml`` holds three short keys; PyYAML's scanner is superlinear on nested brackets (40k
#: openers took over a minute), so it is capped hard and its nesting counted before any parse.
_MAX_YAML = 16 * 1024
_MAX_YAML_BRACKETS = 64
#: Most problems one check reports; the rest are counted. A 4 MB file of bad lines once produced two
#: million of them (270 MB of text, 1 GB resident) for the gate and the model.
_MAX_PROBLEMS = 20
_FILES = ("env.yaml", "env.lock", "env.pip", "activate.sh", "boot.py")


@dataclass(frozen=True)
class EnvDefinition:
    """A committed environment definition, as read from its folder."""

    name: str
    root: Path
    purpose: str
    base: str | None
    externals: tuple[str, ...]

    @property
    def lock_path(self) -> Path:
        return self.root / "env.lock"

    @property
    def pip_path(self) -> Path:
        return self.root / "env.pip"

    @property
    def activate(self) -> Path | None:
        path = self.root / "activate.sh"
        return path if path.is_file() else None

    @property
    def boot(self) -> Path | None:
        path = self.root / "boot.py"
        return path if path.is_file() else None

    def digest(self) -> str:
        """sha256 of what a built environment is created from (the lock and pip files)."""
        digest = hashlib.sha256()
        for path in (self.lock_path, self.pip_path):
            data = path.read_bytes() if path.is_file() else b""
            digest.update(len(data).to_bytes(8, "big") + data)
        return digest.hexdigest()



def read_definition(folder: Path) -> EnvDefinition:
    """The definition in ``folder``. ``env.yaml`` is read the way staging reads it (no link, no
    other hard link, a regular file under the size cap, no blocking) and parsed without anchors or
    aliases; a value of the wrong type reads as empty — :func:`validate` names it. Raises
    ``ValueError`` (with no file text) when the folder or ``env.yaml`` cannot be read safely."""
    folder = Path(folder)
    folder_fd = _open_folder(folder)
    try:
        data = _load_yaml("env.yaml", dir_fd=folder_fd)
    finally:
        os.close(folder_fd)
    externals = data.get("externals")
    return EnvDefinition(
        name=folder.name,
        root=folder,
        purpose=data["purpose"] if isinstance(data.get("purpose"), str) else "",
        base=data["base"] if isinstance(data.get("base"), str) and data["base"] else None,
        externals=(tuple(x if isinstance(x, str) else "<not a name>" for x in externals)
                   if isinstance(externals, list) else () if externals is None else ("<not a list>",)),
    )


def _load_yaml(path, *, dir_fd: int | None = None) -> dict:
    """``env.yaml`` as a mapping, read safely (:func:`_read_plain`) and parsed with anchors and
    aliases refused (a few hundred bytes of nested aliases expand to gigabytes). Raises
    ``ValueError`` carrying a list of problems that name at most the line, never the file's text."""
    import yaml

    try:
        text = _read_plain(path, single_link=True, limit=_MAX_YAML, dir_fd=dir_fd).decode("utf-8")
    except FileNotFoundError:
        raise ValueError(["env.yaml is missing: add it (purpose, base or none, externals)."]) from None
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError([f"env.yaml: make it a plain UTF-8 file under {_MAX_YAML} bytes ({type(exc).__name__})."]) from None
    if text.count("[") + text.count("{") > _MAX_YAML_BRACKETS:
        raise ValueError(["env.yaml: write externals as a short list; it holds too many brackets to read."])
    aliased = False
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                aliased = True
                break
        data = None if aliased else yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError, MemoryError) as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ValueError([f"env.yaml does not parse{where} ({type(exc).__name__}); write plain keys and text."]) from None
    if aliased:
        raise ValueError(["env.yaml: write values out in full; anchors and aliases (&, *) are not read."])
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(["env.yaml must be a mapping."])
    return data


def validate(
    folder: Path, *, base_envs: Mapping[str, Environment], externals: Mapping[str, External],
) -> list[str]:
    """Every reason the definition in ``folder`` cannot be built, or an empty list.

    The files are first copied once into a private folder (links, other hard links, non-files and
    stray files refused before anything is read), and the copy is what is checked — so a folder
    another process is changing is never followed through a link. See :func:`_validate`.
    """
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="env-validate-"))
    try:
        return _validate(_stage(Path(folder), work), base_envs=base_envs, externals=externals)
    except ValueError as exc:
        return list(exc.args[0]) if exc.args and isinstance(exc.args[0], list) else [_quoted(str(exc))]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _validate(
    folder: Path, *, base_envs: Mapping[str, Environment], externals: Mapping[str, External],
) -> list[str]:
    """Every reason the definition in ``folder`` cannot be built, or an empty list.

    What the gate checks before any build, each a sentence naming the file and the fix: a name of
    lowercase letters, digits, ``_`` and ``-``; an ``env.yaml`` with only the known keys and a
    purpose; exactly one of a ``base`` (a human environment that exists) and an ``env.lock``; a lock
    of conda-forge URLs each with its md5 or sha256 (explicit format, no solver), this machine's
    platform or noarch, one line per package; pip lines each an exact version with a sha256;
    externals only from the human's declared list.
    """
    folder = Path(folder)
    problems: list[str] = []
    if not _NAME.match(folder.name):
        problems.append(f"{folder.name}: name the folder with lowercase letters, digits, '_' and '-'.")
    try:
        raw = _load_yaml(folder / "env.yaml")
    except ValueError as exc:
        return problems + list(exc.args[0])
    unknown = set(raw) - _YAML_KEYS
    if unknown:
        shown = [_quoted(str(k)) for k in sorted(map(str, unknown))[:5]]
        problems.append(f"env.yaml: remove unknown keys {shown}{' and more' if len(unknown) > 5 else ''}; "
                        f"allowed: {sorted(_YAML_KEYS)}.")
    if not isinstance(raw.get("purpose"), str):
        problems.append("env.yaml: give `purpose` as one line of text.")
    if "base" in raw and raw["base"] is not None and not isinstance(raw["base"], str):
        problems.append("env.yaml: give `base` as a human environment's name, or leave it out.")
    if "externals" in raw and not isinstance(raw.get("externals") or [], list):
        problems.append("env.yaml: give `externals` as a list of declared names.")
    elif any(not isinstance(x, str) for x in raw.get("externals") or []):
        problems.append("env.yaml: give each external as a declared name (text).")
    definition = read_definition(folder)
    if not definition.purpose.strip():
        problems.append("env.yaml: state the environment's purpose in one line.")
    has_lock = definition.lock_path.is_file()
    if definition.base and has_lock:
        problems.append("Give either a base (a derived environment) or an env.lock (a built one), not both.")
    if not definition.base and not has_lock:
        problems.append("Add an env.lock (conda explicit list), or name a human base environment.")
    if definition.base and definition.pip_path.is_file():
        problems.append("A derived environment installs nothing: drop env.pip, or make it a built one (env.lock).")
    if definition.base and definition.base not in base_envs:
        problems.append(f"base {_quoted(definition.base)} is not a human environment; available: {sorted(base_envs)}.")
    for name in definition.externals:
        if name not in externals:
            problems.append(f"external {_quoted(name)} is not declared by the human; declared: {sorted(externals)}.")
    if has_lock:
        problems += _lock_problems(definition.lock_path)
    if definition.pip_path.is_file():
        problems += _pip_problems(definition.pip_path)
    return _capped(problems)


def _capped(problems) -> list[str]:
    """The first :data:`_MAX_PROBLEMS` of ``problems`` (any iterable), plus a count of the rest —
    consumed one at a time, so a file of a million bad lines never becomes a million strings."""
    shown, more = [], 0
    for problem in problems:
        if len(shown) < _MAX_PROBLEMS:
            shown.append(problem)
        else:
            more += 1
    return shown + ([f"… and {more} more problems of the same kinds."] if more else [])


def _open_folder(folder: Path) -> int:
    """A descriptor for ``folder``, refused when it is a link or not a folder (``ValueError`` with
    the problem list). Every later read goes through it, so swapping the folder afterwards for a
    link changes nothing."""
    try:
        return os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ValueError([f"{folder.name}: the definition folder is missing."]) from None
    except OSError:
        raise ValueError([f"{folder.name}: make the definition a plain folder (not a link)."]) from None


def _unsafe_entries(folder_fd: int, name: str) -> list[str]:
    """Why the folder open as ``folder_fd`` cannot be read as a definition: a file in it is a link
    or not a regular file, has other hard links, is over the size cap, or is not one of
    :data:`_FILES`."""
    import stat

    problems = []
    with os.scandir(folder_fd) as listing:  # streamed and cut short: a folder may hold anything
        entries = []
        for item in listing:
            entries.append(item.name)
            if len(entries) > 4 * len(_FILES):
                return [f"{name}: keep only {', '.join(_FILES)} in a definition (it holds many more files)."]
    for entry in sorted(entries):
        try:
            mode = os.stat(entry, dir_fd=folder_fd, follow_symlinks=False)
        except OSError:
            problems.append(f"{name}/{_quoted(entry)}: it changed while being checked; try again.")
            continue
        if entry not in _FILES:
            problems.append(f"{name}/{_quoted(entry)}: keep only {', '.join(_FILES)} in a definition.")
        elif not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1:
            problems.append(f"{name}/{entry}: make it a plain file (not a link).")
        elif mode.st_size > _MAX_FILE:
            problems.append(f"{name}/{entry}: keep it under {_MAX_FILE} bytes.")
    return problems


def _text_problems(path: Path) -> list[str]:
    """A lock or pip file must be ASCII with ``\\n`` line ends: conda and pip split lines on fewer
    characters than Python does, so anything else could be read as different lines."""
    data = path.read_bytes()
    if any(b > 0x7E or (b < 0x20 and b not in (0x0A, 0x09)) for b in data):
        return [f"{path.name}: use plain ASCII with \\n line ends."]
    return []


def _quoted(line: str) -> str:
    """A rejected line, shortened: enough to find it, never a whole line of whatever was read."""
    return repr(line[:48] + ("…" if len(line) > 48 else ""))


def host_subdir() -> str:
    """conda's name for this machine's platform (``linux-64``, ``osx-arm64``, …)."""
    import platform

    machine = platform.machine()
    arch = {"x86_64": "64", "aarch64": "aarch64", "arm64": "arm64"}.get(machine, machine)
    return f"{'osx' if os.uname().sysname == 'Darwin' else 'linux'}-{arch}"


def _lock_problems(path: Path) -> list[str]:
    if problems := _text_problems(path):
        return problems
    return _capped(_lock_issues(path.read_text(encoding="utf-8")))


def _lock_issues(text: str):
    """Each problem with a lock's text, one line at a time."""
    from collections import Counter

    host = host_subdir()
    seen_explicit, packages, names = False, 0, Counter()
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("# platform:") and ln.split(":", 1)[1].strip() != host:
            yield f"env.lock: its '# platform:' is not this machine's ({host})."
        if not ln or ln.startswith("#"):
            continue
        if not seen_explicit:
            if ln != "@EXPLICIT":
                yield "env.lock: start with the line @EXPLICIT (conda's explicit format, no solver)."
                return
            seen_explicit = True
            continue
        packages += 1
        if not _CONDA_LINE.match(ln):
            yield f"env.lock line {_quoted(ln)}: use a conda-forge package URL with its md5 (url#md5)."
            continue
        # conda's explicit install never checks the platform: a foreign binary would install and
        # fail only when run.
        if f"/{host}/" not in ln and "/noarch/" not in ln:
            yield f"env.lock line {_quoted(ln)}: this machine is {host}; use {host} or noarch packages."
        names[_package_name(ln)] += 1
    if not seen_explicit:
        yield "env.lock: start with the line @EXPLICIT (conda's explicit format, no solver)."
        return
    duplicates = sorted(n for n, count in names.items() if count > 1)
    if duplicates:
        yield f"env.lock: one line per package; repeated: {[_quoted(n) for n in duplicates[:5]]}."
    if packages == 0:
        yield "env.lock lists no package."


def _package_name(line: str) -> str:
    filename = line.split("#", 1)[0].rsplit("/", 1)[-1]
    stem = filename.removesuffix(".conda").removesuffix(".tar.bz2")
    return stem.rsplit("-", 2)[0]


def _pip_problems(path: Path) -> list[str]:
    if problems := _text_problems(path):
        return problems
    lines = (ln.strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return _capped(
        f"env.pip line {_quoted(ln)}: give an exact version with its sha256, "
        "`name==version --hash=sha256:<64 hex>` (a binary wheel from the index)."
        for ln in lines if ln and not ln.startswith("#") and not _PIP_LINE.match(ln)
    )


# ── build ────────────────────────────────────────────────────────────────────────────────────


class BuildMismatch(RuntimeError):
    """A built prefix whose conda set is not the one its lock names."""


Runner = Callable[..., subprocess.CompletedProcess]


#: Host variables a build may see. Everything else — API keys, the user's conda and pip settings
#: (PIP_*, CONDA_*), PYTHONPATH — stays out: a build runs package scripts with the network on.
#: Proxy variables pass because a networked build needs them; a proxy URL carrying credentials is
#: then visible to those scripts (accepted: the packages are pinned by hash and from conda-forge).
_BUILD_PASS = ("PATH", "LANG", "LC_ALL", "TZ", "http_proxy", "https_proxy", "no_proxy",
               "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR")
Wrap = Callable[[list[str]], list[str]]


def build(
    definition: EnvDefinition,
    *,
    conda_dir: Path,
    conda_exe: str,
    base_envs: Mapping[str, Environment],
    externals: Mapping[str, External],
    jailed: bool,
    runner: Runner = subprocess.run,
    timeout_s: int = 3600,
) -> Environment:
    """The :class:`Environment` a definition names, built if it is not there yet.

    Derived: the base's prefix with this definition's activation and boot file (falling back to the
    base's). Built: ``conda create --file env.lock`` into ``<conda_dir>/envs/ar-<name>``
    (explicit: the exact files, no solver), then ``pip install`` of the hash-pinned wheels in
    ``env.pip``. Steps, all under an exclusive lock on ``<conda_dir>/.install.lock``:

    1. the definition's lock and pip files are copied once into a private staging folder, and
       **that copy** is validated, hashed (the build id), built from and verified against — a
       definition changed after its review is never what gets installed;
    2. a prefix whose record (``<conda_dir>/built/<prefix>.json``, written only here, outside
       anything a build can write) names this build id is verified and reused; any other prefix
       at that path is an interrupted build, removed;
    3. conda's effective ``pkgs_dirs`` must be exactly the build's own cache;
    4. create, pip, :func:`verify`, then the record.

    Each build has its own package cache, home and ``/tmp``, deleted afterwards; only the target
    prefix and those are writable to it. With ``jailed`` the commands run under :func:`build_policy`
    (refused when the jail cannot run); ``jailed=False`` is the caller's explicit choice and is
    written into the record (:func:`build_record`). The environment is only :data:`_BUILD_PASS`
    plus the build's own settings — no secret, nothing of the user's conda or pip configuration,
    nothing registered in the user's ``~/.conda``; safety checks are pinned on.
    """
    if definition.base:
        return _derived(definition, conda_dir=_own_dir(Path(conda_dir)), base_envs=base_envs, externals=externals)
    import fcntl
    import tempfile

    conda_dir = _own_dir(Path(conda_dir))
    for name in ("envs", "built", "staging"):
        _own_dir(conda_dir / name)
    fd = os.open(conda_dir / ".install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        work = Path(tempfile.mkdtemp(prefix="build-", dir=conda_dir / "staging"))
        try:
            try:
                staged_root = _stage(Path(definition.root), work)  # work/def/<name>
            except ValueError as exc:
                raise ValueError(f"definition {definition.name} is not valid: {exc.args[0]}") from None
            if problems := _validate(staged_root, base_envs=base_envs, externals=externals):
                raise ValueError(f"definition {definition.name} is not valid: {problems}")
            staged = read_definition(staged_root)
            if staged.base:
                raise ValueError(f"definition {definition.name} changed to a derived one while building")
            digest = staged.digest()
            # The name a human types (`ar-<name>`): what it holds is the current definition; a past
            # state is rebuilt from the lock store by the fingerprint a receipt stamps.
            target = conda_dir / "envs" / f"ar-{staged.name}"
            environment = Environment(
                name=f"ar-{staged.name}", prefix=target, owner="pipeline",
                activate=_activation(conda_dir, staged, "activate.sh"),
                boot=_activation(conda_dir, staged, "boot.py"),
                externals=tuple(externals[name] for name in staged.externals),
            )
            record = build_record(conda_dir, target)
            # Reused only as strong as asked: a jailed caller never gets a prefix built unjailed, nor
            # one from a conda_dir an unjailed build has run in (its scripts could rewrite records).
            tainted = os.path.lexists(conda_dir / "built" / f"{_UNJAILED}.json")
            if (record and record.get("definition_sha256") == digest
                    and (not jailed or (record.get("jailed") is True and not tainted))
                    and target.is_dir() and not target.is_symlink()):
                _verify(staged, target)
                if recorded_dists(conda_dir, target) is None:  # a build from before the baseline
                    _write_dists(conda_dir, target, installed_dists(target))
                # The prefix is keyed on the lock and pins; activation, boot file and externals can
                # change alone (a reviewed activate.sh fix) and the record must say so (B3c-4).
                current = _record_fields(environment)
                if any(record.get(key) != value for key, value in current.items()):
                    _write_record(conda_dir, target, {**record, **current})
                return environment
            if not jailed:  # sticky: written before anything unjailed runs
                _write_record(conda_dir, Path(_UNJAILED), {"unjailed_build": staged.name})
            record_path = conda_dir / "built" / f"{target.name}.json"
            kept = None
            if target.is_symlink():
                target.unlink()
            elif target.exists() and record and isinstance(record.get("definition_sha256"), str):
                # A finished build is moved aside first, in one rename (a crash leaves it intact). A
                # process already running from it keeps its open files; a new path lookup (a shell
                # binding the whole envs folder, a lazy import) sees the new build at once.
                kept = target.with_name(f"{target.name}.prev-{record['definition_sha256'][:8]}-{uuid.uuid4().hex[:6]}")
                target.rename(kept)
            # Then the old record: an interrupted rebuild must never be reused under it.
            if os.path.lexists(record_path):
                os.unlink(record_path)
            if target.exists():  # an interrupted build: nothing runs from it
                _remove_tree(target)
            for older in (conda_dir / "envs").glob(f"{target.name}.prev-*"):  # only the latest kept
                if older != kept:
                    _remove_tree(older)
            target.mkdir()
            home, pkgs = work / "home", work / "pkgs"
            home.mkdir()
            pkgs.mkdir()
            env = {k: os.environ[k] for k in _BUILD_PASS if k in os.environ}
            env.update({
                "HOME": str(home), "CONDA_PKGS_DIRS": str(pkgs), "CONDA_ALWAYS_COPY": "true",
                "CONDA_CHANNELS": "conda-forge", "CONDA_REGISTER_ENVS": "false",
                "CONDA_SAFETY_CHECKS": "enabled", "CONDA_EXTRA_SAFETY_CHECKS": "true",
                "CONDA_SSL_VERIFY": "true", "PYTHONNOUSERSITE": "1",
            })
            wrap = _jail(conda_exe, staged, target, work) if jailed else (lambda argv: list(argv))
            log = work / "output.log"  # in work, which no build binds
            _check_pkgs_dirs(runner, wrap, conda_exe, env, pkgs, timeout_s, log)
            _run(runner, wrap([conda_exe, "create", "--yes", "--quiet", "--prefix", str(target),
                               "--file", str(staged.lock_path)]), env, timeout_s, log)
            if _pins(staged):
                _run(runner, wrap([str(target / "bin" / "python"), "-m", "pip", "install", "--isolated",
                                   "--no-deps", "--no-input", "--no-cache-dir", "--require-hashes",
                                   "--only-binary=:all:", "-r", str(staged.pip_path)]), env, timeout_s, log)
            _verify(staged, target)
            _write_dists(conda_dir, target, installed_dists(target))
            _write_record(conda_dir, target, {
                "definition_sha256": digest, "jailed": jailed, **_record_fields(environment),
            })
            return environment
        finally:
            _remove_tree(work, missing_ok=True)


#: Written into ``<conda_dir>/built/`` once an unjailed build has run there; no jailed reuse after.
_UNJAILED = "unjailed"


def _activation(conda_dir: Path, staged: EnvDefinition, name: str) -> Path | None:
    """The reviewed ``name`` file (``activate.sh`` / ``boot.py``) as a runtime-owned, read-only copy
    under ``<conda_dir>/activation/<sha16>/``, or ``None`` when the definition has none.

    The copy, not the toolkit file, is what the environment sources and fingerprints: the toolkit
    folder is writable to an author during authoring, and the fingerprint sources the activation
    outside any jail. Content-addressed, so a copy is written once and never changed. A script that
    finds files relative to its own location sees this folder, not the toolkit."""
    import uuid

    source = staged.root / name
    if not source.is_file():
        return None
    data = source.read_bytes()
    folder = _own_dir(_own_dir(Path(conda_dir) / "activation") / hashlib.sha256(data).hexdigest()[:16])
    final = folder / name
    if _is_our_copy(final, data):
        return final
    if final.is_dir() and not final.is_symlink():
        raise RuntimeError(f"{final} is a folder; refusing to replace it")
    temp = folder / f".{name}.{uuid.uuid4().hex}"
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, final)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)
    return final


def _is_our_copy(path: Path, data: bytes) -> bool:
    """Whether ``path`` is a copy this runtime wrote: a regular file (not a link), one hard link,
    this user's, no write bit, holding exactly ``data``."""
    import stat

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o222):
            return False
        return handle.read(len(data) + 1) == data


def _derived(definition: EnvDefinition, *, conda_dir: Path, base_envs, externals) -> Environment:
    """A derived environment from one staged read of the definition (nothing is built)."""
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="env-derive-"))
    try:
        try:
            root = _stage(Path(definition.root), work)
        except ValueError as exc:
            raise ValueError(f"definition {definition.name} is not valid: {exc.args[0]}") from None
        if problems := _validate(root, base_envs=base_envs, externals=externals):
            raise ValueError(f"definition {definition.name} is not valid: {problems}")
        staged = read_definition(root)
        if not staged.base:
            raise ValueError(f"definition {definition.name} changed to a built one; build it as one")
        base = base_envs[staged.base]
        picked = tuple(externals[name] for name in staged.externals)
        own_activate = _activation(conda_dir, staged, "activate.sh")
        own_boot = _activation(conda_dir, staged, "boot.py")
        # Recorded like a built one, so the catalogue lists it (B3c-4); the record names the base,
        # resolved again from the human's configuration when read (`built_environments`).
        _own_dir(Path(conda_dir) / "built")
        _write_record(conda_dir, Path(f"ar-{staged.name}"), {
            "definition_sha256": staged.digest(), "name": f"ar-{staged.name}", "base": staged.base,
            "activate": str(own_activate) if own_activate else None,
            "boot": str(own_boot) if own_boot else None,
            "externals": [external.name for external in picked],
        })
        return Environment(
            name=f"ar-{staged.name}", prefix=base.prefix, owner="pipeline",
            activate=own_activate or base.activate, boot=own_boot or base.boot,
            externals=picked or base.externals,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _own_dir(path: Path) -> Path:
    """``path`` created if absent, and required to be a real folder (not a link) of this user."""
    import stat

    path.mkdir(parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError(f"{path} is not a plain folder owned by this user; refusing to build there")
    return path


def _remove_tree(path: Path, *, missing_ok: bool = False) -> None:
    """``rmtree`` that also removes what a build left read-only (``chmod u+rwx`` and retry)."""
    import shutil
    import stat

    retried: set[str] = set()

    def again(function, failed, exc):
        # Once per path, so a folder chmod cannot fix fails loudly instead of recursing.
        if failed in retried:
            raise exc
        retried.add(failed)
        parent = os.path.dirname(failed)
        for target in (parent, failed):
            try:
                if not os.path.islink(target):
                    os.chmod(target, os.lstat(target).st_mode | stat.S_IRWXU)
            except OSError:
                pass
        if function in (os.unlink, os.rmdir, os.remove):
            function(failed)
        elif os.path.isdir(failed) and not os.path.islink(failed):  # could not open/list it
            shutil.rmtree(failed, onexc=again)
        else:
            os.unlink(failed)

    if missing_ok and not os.path.lexists(path):
        return
    shutil.rmtree(path, onexc=again)


#: A conda spec the solver accepts from the coder: a package name with an optional version
#: constraint (``python=3.9``, ``libboost-python>=1.87``), nothing that names a channel or a URL.
_SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:[=<>!~]{1,2}[A-Za-z0-9_.*+!,<>=]+)?$")
#: A pip requirement to pin: an exact version.
_PIN_REQUEST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9_.!+-]+$")


def solve(
    specs: list[str], *, conda_exe: str, work: Path, jailed: bool,
    runner: Runner = subprocess.run, timeout_s: int = 1800,
) -> str:
    """An explicit lock (``env.lock`` text) for ``specs``, solved against conda-forge only.

    A dry run into a throwaway prefix with an empty package cache, so every package appears in the
    solver's download list with its URL and md5; the lock lists them in the solver's link order.
    Nothing is installed. Runs with the build's environment (only :data:`_BUILD_PASS`, a private
    home), jailed when ``jailed`` (network on, only ``work`` writable). Raises ``ValueError`` for a
    spec that is not a plain package constraint, ``RuntimeError`` when conda cannot solve.
    """
    bad = [s for s in specs if not isinstance(s, str) or not _SPEC.match(s)]
    if not specs or bad:
        raise ValueError(f"give package specs like `python=3.9` or `numpy>=2`; refused: {bad or 'none given'}")
    work = Path(work)
    home, pkgs, tmp = work / "home", work / "pkgs", work / "tmp"
    for folder in (home, pkgs, tmp):
        folder.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in _BUILD_PASS if k in os.environ}
    env.update({"HOME": str(home), "CONDA_PKGS_DIRS": str(pkgs), "CONDA_REGISTER_ENVS": "false",
                "CONDA_SSL_VERIFY": "true", "PYTHONNOUSERSITE": "1"})
    argv = [conda_exe, "create", "--dry-run", "--json", "--override-channels", "-c", "conda-forge",
            "--prefix", str(work / "target"), *specs]
    if jailed:
        from auto_research.sandbox.jail import JAIL_HOME_AT, JailPolicy, jail_argv

        prefix = jail_argv(JailPolicy(ro=(str(conda_root(conda_exe)),), rw=(str(work),), network=True,
                                      home=str(home), home_at=JAIL_HOME_AT, tmp=str(tmp), label="env-solve"))
        if not prefix:
            raise RuntimeError("a jailed solve needs bwrap, which is not available here")
        argv = [*prefix, "--", *argv]
    result = runner(argv, env=env, capture_output=True, text=True, timeout=timeout_s)
    try:
        plan = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        plan = {}
    if result.returncode != 0 or not isinstance(plan, dict) or "actions" not in plan:
        message = plan.get("message") if isinstance(plan, dict) else None
        raise RuntimeError(f"conda could not solve {specs}: "
                           f"{(message or result.stderr or result.stdout or '').strip()[-600:]}")
    fetch = {Path(f["fn"]).name.removesuffix(".conda").removesuffix(".tar.bz2"): f
             for f in plan["actions"].get("FETCH", []) if isinstance(f, dict) and "fn" in f}
    lines = []
    for link in plan["actions"].get("LINK", []):
        entry = fetch.get(link.get("dist_name", "")) if isinstance(link, dict) else None
        if entry is None or not entry.get("url") or not entry.get("md5"):
            raise RuntimeError(f"the solver named {link!r} without a download URL and md5")
        lines.append(f"{entry['url']}#{entry['md5']}")
    return f"# platform: {host_subdir()}\n@EXPLICIT\n" + "\n".join(lines) + "\n"


def pin_wheels(
    python: Path, requirements: list[str], *, work: Path, jailed: bool,
    runner: Runner = subprocess.run, timeout_s: int = 900,
) -> list[str]:
    """``env.pip`` lines for exact requirements: each binary wheel downloaded with the environment's
    own ``python`` (so the wheel matches its interpreter and platform), then hashed here.

    Only ``name==version`` is accepted; ``--only-binary=:all: --no-deps --isolated``, with the
    build's environment and, when ``jailed``, only ``work`` writable. Returns
    ``name==version --hash=sha256:<hex>`` per requirement, in order."""
    bad = [r for r in requirements if not isinstance(r, str) or not _PIN_REQUEST.match(r)]
    if not requirements or bad:
        raise ValueError(f"give exact versions like `pkg==1.2.3`; refused: {bad or 'none given'}")
    work = Path(work)
    home, tmp, dest = work / "home", work / "tmp", work / "wheels"
    for folder in (home, tmp, dest):
        folder.mkdir(parents=True, exist_ok=True)
    env = {k: os.environ[k] for k in _BUILD_PASS if k in os.environ}
    env.update({"HOME": str(home), "PYTHONNOUSERSITE": "1"})
    lines = []
    for requirement in requirements:
        target = dest / requirement.split("==")[0]
        target.mkdir(exist_ok=True)
        argv = [str(python), "-m", "pip", "download", "--isolated", "--no-deps", "--no-input",
                "--no-cache-dir", "--only-binary=:all:", "-d", str(target), requirement]
        if jailed:
            from auto_research.sandbox.jail import JAIL_HOME_AT, JailPolicy, jail_argv

            # The prefix as spelled (the argv names `python` that way; see build_policy).
            prefix = jail_argv(JailPolicy(ro=(str(Path(python).parent.parent),), rw=(str(work),),
                                          network=True, home=str(home), home_at=JAIL_HOME_AT, tmp=str(tmp),
                                          label="env-pin"))
            if not prefix:
                raise RuntimeError("a jailed download needs bwrap, which is not available here")
            argv = [*prefix, "--", *argv]
        result = runner(argv, env=env, capture_output=True, text=True, timeout=timeout_s)
        wheels = sorted(target.glob("*.whl"))
        if result.returncode != 0 or len(wheels) != 1:
            raise RuntimeError(f"no single binary wheel for {requirement}: "
                               f"{(result.stderr or result.stdout or '').strip()[-600:]}")
        digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
        lines.append(f"{requirement} --hash=sha256:{digest}")
    return lines


def build_record(conda_dir: Path, prefix: Path) -> dict | None:
    """What :func:`build` recorded for ``prefix`` (``definition_sha256``, ``jailed``), or ``None``."""
    path = Path(conda_dir) / "built" / f"{Path(prefix).name}.json"
    try:
        data = json.loads(_read_plain(path, limit=64 * 1024))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def built_environments(
    conda_dir: Path, externals: Mapping[str, External], base_envs: Mapping[str, Environment] | None = None,
) -> list[Environment]:
    """Every environment :func:`build` has finished in ``conda_dir``, read back from its records:
    the most recent build of each name, its prefix present, its activation and boot file only from
    the runtime's own ``activation/`` folder, its externals only from the human's declared list. For
    listing and the shell (``use_env``); nothing here re-verifies a prefix."""
    conda_dir = Path(conda_dir)
    folder = conda_dir / "built"
    if not folder.is_dir():
        return []
    owned = (conda_dir / "activation").resolve()

    def owned_file(value) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        return path if path.resolve().is_relative_to(owned) and path.is_file() else None

    latest: dict[str, tuple[int, Environment]] = {}
    with os.scandir(folder) as listing:
        for count, entry in enumerate(listing):
            if count >= _MAX_ENTRIES:
                break
            if not entry.name.endswith(".json") or entry.name == f"{_UNJAILED}.json":
                continue
            prefix = conda_dir / "envs" / entry.name.removesuffix(".json")
            record = build_record(conda_dir, prefix)
            name = (record or {}).get("name")
            if not isinstance(name, str) or not _NAME.fullmatch(name.removeprefix("ar-")):
                continue
            names = record.get("externals") if isinstance(record.get("externals"), list) else []
            picked = tuple(externals[n] for n in names if isinstance(n, str) and n in externals)
            activate, boot = owned_file(record.get("activate")), owned_file(record.get("boot"))
            if record.get("base") is not None:
                # Derived: the human base as configured now, layered with the recorded copies.
                base = (base_envs or {}).get(record["base"]) if isinstance(record["base"], str) else None
                if base is None or not base.prefix.is_dir():
                    continue
                environment = Environment(
                    name=name, prefix=base.prefix, owner="pipeline", activate=activate or base.activate,
                    boot=boot or base.boot, externals=picked or base.externals,
                )
            elif prefix.is_dir():
                environment = Environment(name=name, prefix=prefix, owner="pipeline",
                                          activate=activate, boot=boot, externals=picked)
            else:
                continue
            stamp = entry.stat(follow_symlinks=False).st_mtime_ns
            if name not in latest or stamp > latest[name][0]:
                latest[name] = (stamp, environment)
    return [environment for _, environment in sorted(latest.values(), key=lambda item: item[1].name)]


def _record_fields(environment: Environment) -> dict:
    """What a build record says about an environment besides its digest: name, activation, boot
    file and externals."""
    return {
        "name": environment.name,
        "activate": str(environment.activate) if environment.activate else None,
        "boot": str(environment.boot) if environment.boot else None,
        "externals": [external.name for external in environment.externals],
    }


#: A package line a replay accepts. A lock the runtime wrote from a real prefix may name Anaconda's
#: own channel as well as conda-forge (the human's environments do), always with its checksum.
_REPLAY_LINE = re.compile(
    r"^https://(?:conda\.anaconda\.org|repo\.anaconda\.com)/[A-Za-z0-9_./+-]+\.(?:conda|tar\.bz2)"
    r"#(?:[0-9a-f]{32}|sha256:[0-9a-f]{64})$"
)


def replay(
    lock: dict, *, conda_dir: Path, conda_exe: str, jailed: bool,
    runner: Runner = subprocess.run, timeout_s: int = 3600,
) -> dict:
    """Rebuild the conda part of a recorded state (an :func:`envs.lock`) into its own prefix,
    ``<conda_dir>/replay/<fingerprint[:16]>``, and compare it with the record (env plan B4).

    The lock's explicit list is installed as it is (no solver), jailed when ``jailed`` exactly as a
    build is, from a private package cache; a prefix already holding the recorded packages is
    reused. Returns ``fingerprint``, ``prefix``, ``conda_match`` and the ``missing``/``extra``
    package lines; pip packages are listed as ``pip_not_replayed`` (the lock names their versions,
    not their files). Raises ``ValueError`` for a lock that names a foreign platform, an unknown
    host or a package without its checksum, ``RuntimeError`` when conda fails."""
    import fcntl
    import tempfile

    from auto_research.sandbox.envs import _conda_records

    fp = lock.get("fingerprint")
    if not isinstance(fp, str) or not re.fullmatch(r"[0-9a-f]{64}", fp):
        raise ValueError("the lock carries no fingerprint")
    text = lock.get("conda_explicit") if isinstance(lock.get("conda_explicit"), str) else ""
    host = host_subdir()
    problems = []
    for ln in (line.strip() for line in text.splitlines()):
        if ln.startswith("# platform:") and ln.split(":", 1)[1].strip() != host:
            problems.append(f"recorded on {ln.split(':', 1)[1].strip()}; this machine is {host}")
        elif ln and not ln.startswith("#") and ln != "@EXPLICIT" and (
                not _REPLAY_LINE.match(ln) or (f"/{host}/" not in ln and "/noarch/" not in ln)):
            problems.append(f"cannot install {_quoted(ln)}")
    if problems:
        raise ValueError(f"the lock of {fp[:12]} cannot be replayed: {problems[:5]}")
    wanted = sorted(line for line in (lock.get("components") or {}).get("conda", []) if isinstance(line, str))
    installable = {ln.strip() for ln in text.splitlines() if ln.strip().startswith("https://")}
    if unlisted := [ln for ln in wanted if ln not in installable]:
        raise ValueError(f"the lock of {fp[:12]} records packages it cannot install (no URL): {unlisted[:5]}")
    if len(set(wanted)) != len(wanted):
        raise ValueError(f"the lock of {fp[:12]} records a package twice")
    conda_dir = _own_dir(Path(conda_dir))
    for name in ("replay", "staging", "built"):
        _own_dir(conda_dir / name)
    target = conda_dir / "replay" / fp[:16]
    fd = os.open(conda_dir / ".install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        if not (target.is_dir() and not target.is_symlink() and sorted(_conda_records(target, follow=False)) == wanted):
            if os.path.lexists(target):
                if target.is_dir() and not target.is_symlink() and _conda_records(target, follow=False):
                    # A finished replay someone changed (a package installed to debug): kept aside.
                    target.rename(target.with_name(f"{target.name}.changed-{uuid.uuid4().hex[:6]}"))
                else:
                    _remove_tree(target)  # an interrupted replay
            work = Path(tempfile.mkdtemp(prefix="replay-", dir=conda_dir / "staging"))
            try:
                root = work / "def" / f"replay-{fp[:12]}"
                root.mkdir(parents=True)
                (root / "env.lock").write_text(text, encoding="utf-8")
                staged = EnvDefinition(name=root.name, root=root, purpose="replay", base=None, externals=())
                if not jailed:  # as a build: an unjailed run's scripts could rewrite records here
                    _write_record(conda_dir, Path(_UNJAILED), {"unjailed_build": root.name})
                target.mkdir()
                for folder in ("home", "pkgs"):
                    (work / folder).mkdir()
                env = {k: os.environ[k] for k in _BUILD_PASS if k in os.environ}
                env.update({
                    "HOME": str(work / "home"), "CONDA_PKGS_DIRS": str(work / "pkgs"),
                    "CONDA_ALWAYS_COPY": "true", "CONDA_REGISTER_ENVS": "false",
                    "CONDA_SAFETY_CHECKS": "enabled", "CONDA_EXTRA_SAFETY_CHECKS": "true",
                    "CONDA_SSL_VERIFY": "true", "PYTHONNOUSERSITE": "1",
                })
                wrap = _jail(conda_exe, staged, target, work) if jailed else (lambda argv: list(argv))
                log = work / "output.log"
                _check_pkgs_dirs(runner, wrap, conda_exe, env, work / "pkgs", timeout_s, log)
                _run(runner, wrap([conda_exe, "create", "--yes", "--quiet", "--prefix", str(target),
                                   "--file", str(root / "env.lock")]), env, timeout_s, log)
            finally:
                _remove_tree(work, missing_ok=True)
        rebuilt = sorted(_conda_records(target, follow=False))
    return {
        "fingerprint": fp, "prefix": str(target), "conda_match": rebuilt == wanted,
        "missing": sorted(set(wanted) - set(rebuilt)), "extra": sorted(set(rebuilt) - set(wanted)),
        "pip_not_replayed": [p for p in lock.get("pip") or [] if isinstance(p, str)],
    }


def installed_dists(prefix: Path) -> list[str]:
    """``<lib>/<name>.dist-info`` for every installed Python distribution of ``prefix``, sorted:
    what a ``pip install`` or removal by hand changes, though :func:`verify` (conda records and
    pins only) does not see it. Links are not followed; listings are capped."""
    root = Path(prefix)
    names: list[str] = []
    libs = [n for n, is_dir, _ in _listing(root / "lib", root) if is_dir and n.startswith("python")]
    for lib in libs[:_MAX_LIB_FOLDERS]:
        names += [f"{lib}/{n}" for n, _d, _l in _listing(root / "lib" / lib / "site-packages", root)
                  if n.endswith(".dist-info")]
    return sorted(names)


def _dists_path(conda_dir: Path, prefix: Path) -> Path:
    return Path(conda_dir) / "built" / f"{Path(prefix).name}.dists"


def _write_dists(conda_dir: Path, prefix: Path, names: list[str]) -> None:
    final = _dists_path(conda_dir, prefix)
    temp = final.with_name(f".{final.name}.{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(names))
    os.replace(temp, final)


def recorded_dists(conda_dir: Path, prefix: Path) -> list[str] | None:
    """The distributions ``prefix`` held when it was built (:func:`installed_dists`), or ``None``."""
    try:
        text = _read_plain(_dists_path(conda_dir, prefix), limit=_MAX_RECORD).decode("utf-8")
    except (OSError, ValueError):
        return None
    return [line for line in text.split("\n") if line]


def forget(conda_dir: Path, name: str) -> bool:
    """Drop the build record of ``ar-<name>`` (its definition was deleted), so the catalogue stops
    listing it; the prefix stays for any process still running in it. Whether one was dropped."""
    path = Path(conda_dir) / "built" / f"ar-{name}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


class StoppableRunner:
    """A ``subprocess.run`` stand-in whose running commands :meth:`stop` kills, each started in a
    session of its own (a jailed one takes its jail with it, ``--die-with-parent``). For a build or
    solve that a thread runs and a stop must end: a thread cannot be cancelled, its process can."""

    def __init__(self) -> None:
        import threading

        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.stopped = False

    def __call__(self, argv, *, timeout=None, capture_output=False, text=False, **kwargs):
        if capture_output:
            kwargs.setdefault("stdout", subprocess.PIPE)
            kwargs.setdefault("stderr", subprocess.PIPE)
        with self._lock:
            if self.stopped:
                raise RuntimeError("stopped before it ran")
            proc = subprocess.Popen(argv, start_new_session=True, text=text, **kwargs)
            self._procs.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            # As subprocess.run: kill and reap; never read on (an escaped child may hold the pipes).
            self._kill(proc)
            proc.wait()
            raise
        finally:
            with self._lock:
                self._procs.discard(proc)
        if self.stopped:
            raise RuntimeError("stopped while it ran")
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            procs = list(self._procs)
        for proc in procs:
            self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _write_record(conda_dir: Path, prefix: Path, data: dict) -> None:
    import uuid

    final = Path(conda_dir) / "built" / f"{Path(prefix).name}.json"
    temp = final.with_name(f".{final.name}.{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temp, final)


def _stage(folder: Path, work: Path) -> Path:
    """A private copy of the definition in ``folder``, at ``work/def/<name>/``, read once.

    ``def/`` keeps the copy apart from whatever else ``work`` holds (a build's ``tmp``, ``home`` and
    ``pkgs`` are bound writable in its jail; a definition named ``tmp`` once landed on the jail's
    ``/tmp``). Refused (``ValueError`` carrying the list of problems) before anything is read when
    the folder or a file in it is unsafe (:func:`_unsafe_entries`); each file is then opened
    relative to the folder's descriptor, without following links and without blocking, and
    re-checked on its own descriptor."""
    if not _NAME.match(Path(folder).name):
        raise ValueError([f"{_quoted(Path(folder).name)}: name the folder with lowercase letters, digits, '_' and '-'."])
    folder_fd = _open_folder(Path(folder))
    try:
        if unsafe := _unsafe_entries(folder_fd, Path(folder).name):
            raise ValueError(unsafe)
        root = work / "def" / Path(folder).name
        root.mkdir(parents=True)
        for name in _FILES:
            try:
                data = _read_plain(name, single_link=True, dir_fd=folder_fd)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                raise ValueError(
                    [f"{Path(folder).name}/{name}: make it a plain file under {_MAX_FILE} bytes ({exc})."]
                ) from None
            (root / name).write_bytes(data)
        return root
    finally:
        os.close(folder_fd)


def _read_plain(path, *, single_link: bool = False, limit: int | None = None, dir_fd: int | None = None) -> bytes:
    """The bytes of ``path``, opened without following a link and without blocking (a FIFO would
    hang the caller), required to be a regular file (with no other hard link when asked) of at most
    ``limit`` bytes — checked on the open descriptor, so a swap after any earlier check is caught."""
    import stat

    limit = _MAX_FILE if limit is None else limit  # read at call time, so the cap can be changed
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
            raise ValueError("not a plain file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"larger than {limit} bytes")
    return data


def _jail(conda_exe: str, staged: EnvDefinition, target: Path, work: Path) -> Wrap:
    from auto_research.sandbox.jail import jail_argv

    argv = jail_argv(build_policy(conda_exe=conda_exe, staged=staged, target=target, work=work))
    if not argv:
        raise RuntimeError("a jailed build needs bwrap, which is not available here")
    return lambda command: [*argv, "--", *command]


def build_policy(*, conda_exe: str, staged: EnvDefinition, target: Path, work: Path):
    """The jail for one build: the conda installation and the staged definition read-only; only
    the target prefix and this build's ``work`` folder (its cache and home) writable, a private
    ``/tmp`` inside ``work``; network on (packages are downloaded)."""
    from auto_research.sandbox.jail import JAIL_HOME_AT, JailPolicy

    root = conda_root(conda_exe)
    tmp = Path(work) / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    return JailPolicy(
        # As spelled, like every other path here: conda is handed this spelling, and the jail binds
        # the resolved folder and recreates the spelling as a link (`/data` → `/nfsdata1` on this
        # host; a resolved-only bind left the staged lock unreachable, B5 live cell 2026-10-07).
        ro=(str(root), str(staged.root)),
        rw=(str(target), str(Path(work) / "pkgs"), str(Path(work) / "home")),
        network=True, home=str(Path(work) / "home"), home_at=JAIL_HOME_AT, tmp=str(tmp),
        label="env-build",
    )


def conda_root(conda_exe: str) -> Path:
    """The installation ``conda_exe`` belongs to, as spelled (the jail recreates a link to it, so
    conda's shebang, written with the installation's spelling, still resolves). Refused unless it is
    a conda installation, and never the home folder or a folder containing it."""
    root = Path(os.path.abspath(conda_exe)).parent.parent
    home = Path.home().resolve()
    if not (root / "conda-meta" / "history").is_file():
        raise RuntimeError(f"{conda_exe} is not inside a conda installation ({root} has no conda-meta)")
    if home == root.resolve() or home.is_relative_to(root.resolve()):
        raise RuntimeError(f"conda installation {root} contains the home folder; refusing to bind it")
    return root


def _check_pkgs_dirs(runner: Runner, wrap: Wrap, conda_exe: str, env: dict, pkgs_dir: Path,
                     timeout_s: int, log: Path) -> None:
    code, out = _call(runner, wrap([conda_exe, "config", "--show", "pkgs_dirs", "--json"]), env, timeout_s,
                      log, keep=1024 * 1024)
    if code != 0:
        raise RuntimeError(f"conda config --show exited {code}:\n{out[-400:]}")
    try:
        shown = json.loads(out or "{}").get("pkgs_dirs")
    except (json.JSONDecodeError, AttributeError):
        shown = None
    if not isinstance(shown, list) or [str(Path(p)) if isinstance(p, str) else p for p in shown] != [str(pkgs_dir)]:
        raise RuntimeError(
            f"conda would use package caches {_quoted(str(shown))}, not only {pkgs_dir}: a condarc "
            "outside the build sets pkgs_dirs; remove it there before building."
        )


def _call(runner: Runner, argv: list[str], env: dict, timeout_s: int, log: Path, *, keep: int = 400) -> tuple[int, str]:
    """Run ``argv`` with its output going to ``log`` (a file, so a command printing gigabytes costs
    disk, not the runtime's memory), and return its exit code and the last ``keep`` bytes."""
    with open(log, "w+b") as out:
        result = runner(argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                        timeout=timeout_s)
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - keep))
        tail = out.read(keep).decode("utf-8", "replace")
    return result.returncode, tail


def _pins(definition: EnvDefinition) -> dict[str, str]:
    """``{normalised name: version}`` from ``env.pip``."""
    if not definition.pip_path.is_file():
        return {}
    pins = {}
    for ln in definition.pip_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            name, version = ln.split(" ", 1)[0].split("==", 1)
            pins[_dist_name(name)] = version
    return pins


def _dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _run(runner: Runner, argv: list[str], env: dict, timeout_s: int, log: Path) -> None:
    # Short: build output can carry what a package script chose to print.
    code, tail = _call(runner, argv, env, timeout_s, log)
    if code != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} … exited {code}:\n{tail.strip()}")


#: Largest conda record or dist-info file ``verify`` reads, and the most it reads in all (bytes).
#: Bounds, not expectations: a record lists every file its package installs, so a header-only
#: library is large (2026-10-07, B5 live cell: libboost-headers' file list alone is 3.5 MB and its
#: record over the old 4 MB cap, which failed every Boost build after its 35-minute install).
_MAX_RECORD = 64 * 1024 * 1024
_MAX_RECORDS_TOTAL = 1024 * 1024 * 1024


def verify(definition: EnvDefinition, prefix: Path) -> None:
    """Raise :class:`BuildMismatch` unless ``prefix`` holds exactly what the definition names.

    The definition is read once through a staged copy (links refused, as :func:`validate`), so a
    linked ``env.lock`` is never read or quoted. See :func:`_verify` for what is checked.
    """
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="env-verify-"))
    try:
        try:
            root = _stage(Path(definition.root), work)
        except ValueError as exc:
            raise BuildMismatch(f"definition {definition.name} cannot be read: {exc.args[0]}") from None
        staged = read_definition(root)
        # What _verify reads, checked first: a malformed pip line or lock is a refusal, not a crash.
        problems = _lock_problems(staged.lock_path) if staged.lock_path.is_file() else ["env.lock is missing."]
        if staged.pip_path.is_file():
            problems += _pip_problems(staged.pip_path)
        if problems:
            raise BuildMismatch(f"definition {definition.name} is not valid: {problems}")
        _verify(staged, prefix)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify(definition: EnvDefinition, prefix: Path) -> None:
    """``definition`` is a staged copy. Checked:

    * the lock's conda records — same URLs, each with the md5 or ``sha256:`` the lock gives,
      nothing else; a record that is a link, not a regular file, too large, or not a JSON object
      is a mismatch;
    * every ``env.pip`` pin installed at its version (compared as versions), as exactly one
      ``<name>-<version>.dist-info`` folder whose METADATA names it;
    * no ``pyvenv.cfg`` (``<prefix>/`` or ``bin/``) and no ``bin/python*._pth``: Python reads them
      at start-up and each can move the whole standard library elsewhere — the only start-up
      files looked for; ``lib`` and ``site-packages`` must not be links.

    A consistency check, not a defence against the build: records live in the prefix, which the
    build's own code (link scripts, pip) can write while it runs. And records, not file contents:
    nothing re-hashes the prefix's files afterwards (the environment fingerprint covers records).
    Files a build writes outside its records — a start-up hook a package script drops — are not
    looked for (ruling 2026-10-06): they escape the fingerprint, an accepted residual risk."""
    from packaging.version import InvalidVersion, Version

    def same(have_v: str | None, want: str) -> bool:
        try:
            return have_v is not None and Version(have_v) == Version(want)
        except InvalidVersion:
            return have_v == want

    root = Path(prefix).resolve()
    meta_dir = (root / "conda-meta").resolve()
    if not meta_dir.is_relative_to(root) or meta_dir == root:
        raise BuildMismatch(f"{prefix}: conda-meta lies outside the prefix")
    wanted = {}
    for ln in definition.lock_path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("#", "@")):
            url, _, digest = ln.partition("#")
            wanted[url] = digest
    records = []
    try:
        listing = os.scandir(meta_dir)
    except OSError as exc:
        raise BuildMismatch(f"{prefix}: conda-meta cannot be listed ({exc.strerror})") from None
    with listing as entries:  # stops one past the lock's count, and at the entry cap
        for count, entry in enumerate(entries):
            if count >= _MAX_ENTRIES:
                raise BuildMismatch(f"{prefix}: conda-meta holds more than {_MAX_ENTRIES} entries")
            if entry.name.endswith(".json"):
                records.append(meta_dir / entry.name)
                if len(records) > len(wanted):
                    break
    if len(records) != len(wanted):  # one record per locked package: checked before reading any
        raise BuildMismatch(f"{prefix}: conda records do not number the lock's {len(wanted)} packages")
    records.sort()
    pins = _pins(definition)
    from fnmatch import fnmatchcase

    planted = [f for f in _STARTUP_FILES if os.path.lexists(root / f)]
    planted += [f"bin/{name}" for name, _d, _l in _listing(root / "bin", root) if fnmatchcase(name, "python*._pth")]
    if planted:
        raise BuildMismatch(f"{prefix}: {planted} would move Python's standard library; no package installs one")
    dist_infos = _pin_dist_infos(root, pins)
    have, bad, budget = {}, [], _MAX_RECORDS_TOTAL
    for record in records:
        try:
            raw = _read_plain(record, limit=min(_MAX_RECORD, budget))
            budget -= len(raw)
            meta = json.loads(raw)
            del raw
            if not isinstance(meta, dict) or not isinstance(meta.get("url"), str):
                raise ValueError
        except (OSError, ValueError, RecursionError):
            bad.append(record.name)
            continue
        have[meta["url"]] = (str(meta.get("md5", "")), f"sha256:{meta.get('sha256', '')}")
        del meta
    if bad:
        raise BuildMismatch(f"{prefix}: unreadable conda records {_quoted(str(bad))}")
    missing = sorted(u for u, d in wanted.items() if u not in have or d not in have[u])[:5]
    extra = sorted(str(u) for u in have if u not in wanted)[:5]
    if missing or extra:
        raise BuildMismatch(
            f"{prefix} does not match its lock: missing {[_quoted(u) for u in missing]}, "
            f"unexpected {[_quoted(u) for u in extra]}"
        )
    extra_pins = sorted(n for n, v in dist_infos.items() if len(v) > 1)
    if extra_pins:  # refused before any of them is read
        raise BuildMismatch(f"{prefix}: more than one install of {_quoted(str(extra_pins))}")
    found = {}
    for pin, [(site_rel, name, version)] in dist_infos.items():
        info = root / site_rel / name
        try:
            metadata = _read_plain(info / "METADATA", limit=min(_MAX_RECORD, budget))
            budget -= len(metadata)
        except (OSError, ValueError):
            raise BuildMismatch(f"{prefix}: {_quoted(name)} is unreadable") from None
        lines = metadata.decode("utf-8", "replace").splitlines()[:20]
        fields = dict(line.split(": ", 1) for line in lines if ": " in line)
        if _dist_name(fields.get("Name", "")) != pin or not same(fields.get("Version"), version):
            raise BuildMismatch(f"{prefix}: {_quoted(name)} names another distribution")
        found[pin] = version
    wrong = {n: found.get(n) for n, v in pins.items() if n not in found or not same(found[n], v)}
    if wrong:
        raise BuildMismatch(f"{prefix}: pip pins not installed as locked (installed: {wrong})")


#: Most entries ``verify`` lists in one folder of a prefix (a real site-packages holds about a
#: thousand); a folder with more is refused rather than listed.
_MAX_ENTRIES = 200_000
#: Most ``lib/python*`` folders a prefix may hold (conda-forge's python has one, plus a link).
_MAX_LIB_FOLDERS = 4
#: Python reads these at start-up; each can move the standard library out of the prefix
#: (``bin/python*._pth`` too, matched by name). The only start-up files ``verify`` looks for.
_STARTUP_FILES = ("pyvenv.cfg", "bin/pyvenv.cfg")


def _listing(folder: Path, prefix: Path):
    """``(name, is_dir, is_link)`` for each entry of ``folder``, streamed, refused past
    :data:`_MAX_ENTRIES`. A missing folder lists nothing."""
    try:
        listing = os.scandir(folder)
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        raise BuildMismatch(f"{prefix}: {_quoted(folder.name)} cannot be listed ({exc.strerror})") from None
    with listing:
        for count, entry in enumerate(listing):
            if count >= _MAX_ENTRIES:
                raise BuildMismatch(f"{prefix}: {_quoted(folder.name)} holds more than {_MAX_ENTRIES} entries")
            link = entry.is_symlink()
            yield entry.name, (not link and entry.is_dir(follow_symlinks=False)), link


def _pin_dist_infos(root: Path, pins: dict[str, str]) -> dict[str, list[tuple[str, str, str]]]:
    """The dist-info folders claiming a pin, ``{pin: [(site_rel, name, version)]}``, from each real
    ``lib/python*/site-packages`` (links are not followed; listings streamed and capped)."""
    dist_infos: dict[str, list[tuple[str, str, str]]] = {}
    if not pins:
        return dist_infos
    for folder in ("lib",):
        if (root / folder).is_symlink():
            raise BuildMismatch(f"{root}: {folder} is a link; Python would load through it")
    real = []
    for name, is_dir, _is_link in _listing(root / "lib", root):
        if is_dir and name.startswith("python"):
            real.append(name)
            if len(real) > _MAX_LIB_FOLDERS:
                raise BuildMismatch(f"{root}: more than {_MAX_LIB_FOLDERS} lib/python* folders; a Python prefix has one")
    for lib in real:
        site_rel = f"lib/{lib}/site-packages"
        if (root / site_rel).is_symlink():
            raise BuildMismatch(f"{root}: {site_rel} is a link; Python would load through it")
        for name, _is_dir, is_link in _listing(root / site_rel, root):
            if not name.endswith(".dist-info"):
                continue
            dist, _, version = name.removesuffix(".dist-info").rpartition("-")
            if _dist_name(dist) in pins:
                if is_link:
                    raise BuildMismatch(f"{root}: {_quoted(name)} is a link")
                claims = dist_infos.setdefault(_dist_name(dist), [])
                if claims:  # refused at the second claim: nothing is gathered, nothing is read
                    raise BuildMismatch(f"{root}: more than one install of {_quoted(_dist_name(dist))}")
                claims.append((site_rel, name, version))
    return dist_infos
