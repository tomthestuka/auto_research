"""OS-level filesystem jail for every runtime process that can touch a disk (CLAUDE.md §12).

``PathGuard`` states the allow-list to the models and refuses the calls whose paths it can see.
It cannot see everything: a shell command's implicit reads are invisible to a command-string
scan, and the Codex author harness grants full-disk **read** access by design — measured
2026-08-19, a DeepSeek coder read this repo's own ``coder/codex_sdk.py``, ``sandbox/DOCS.md``, the
test harnessing it and ``~/.codex/config.toml`` without being asked to. That is the gap this
module closes: the same allow-list, enforced by the kernel instead of by a hook.

The mechanism is ``bwrap`` (bubblewrap) unprivileged, which on Linux composes user namespaces
with Landlock. Nothing here needs root. What a jailed process sees is exactly:

* the configured allow-list, split into read-only and writable roots;
* the system directories a dynamically-linked program needs (``/usr``, ``/etc`` and the usual
  top-level symlinks), read-only;
* a private ``/proc``, ``/dev`` and ``$HOME``.

Everything else — this repository, the governance files, another user's data, the rest of the
home directory — is simply not in the mount namespace, so a read fails as *absent* rather than
being refused by a rule someone can talk their way around.

Two policies are per-process rather than global, because the right answer differs:

* **Network.** The author harnesses talk to a model API and the analyst REPL downloads
  observations, so both keep it. The gate's Layer-B test run does not: a mergeability verdict
  that can reach the network is a verdict that depends on something other than the data on disk
  (§9.4), so that one is sealed.
* **Credentials.** The Claude author authenticates from ``~/.claude/.credentials.json``, which a
  jailed home hides. That single file is bound read-only rather than exposing the home directory,
  so the subscription path keeps working and every cost figure measured so far stays comparable.

Availability is a machine fact, so it is a launch check rather than an assumption: see
:func:`check_os_jail_available`, which warns (never blocks) when the jail is configured on a host
that cannot run it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


# Read-only system paths any dynamically-linked program needs. Kept minimal on purpose: this is
# the part of the filesystem a jailed process sees that is not in the project's allow-list.
_SYSTEM_RO = ("/usr", "/etc")
# Top-level compatibility symlinks on a merged-/usr distribution.
_SYSTEM_SYMLINKS = (("usr/bin", "/bin"), ("usr/sbin", "/sbin"),
                    ("usr/lib", "/lib"), ("usr/lib64", "/lib64"))


@dataclass(frozen=True)
class JailPolicy:
    """What one jailed process may see and reach.

    ``ro``/``rw`` come from config by default (:func:`policy_from_config`); a caller adds what its
    own process needs — the coder's credential file, a per-attempt scratch directory.
    """

    ro: tuple[str, ...] = ()
    rw: tuple[str, ...] = ()
    network: bool = True
    home: str | None = None
    label: str = ""
    ro_files: tuple[str, ...] = field(default=())
    """Individual files bound read-only, for a secret that must not bring its directory with it."""


def _config() -> dict:
    from auto_research.graph.runtime import load_orchestration_config

    try:
        return load_orchestration_config() or {}
    except Exception:  # noqa: BLE001 — an unreadable config means "not configured"
        return {}


def jail_enabled() -> bool:
    """Whether ``config/orchestration.yaml`` asks for the jail (default off)."""
    return bool(_config().get("agent_os_jail", False))


@lru_cache(maxsize=1)
def bwrap_path() -> str | None:
    """The ``bwrap`` binary, or ``None`` when it is absent."""
    return shutil.which("bwrap")


def jail_available() -> tuple[bool, str]:
    """Whether this host can run the jail, and why not when it cannot.

    Checked by *running* a trivial jailed command rather than by inspecting kernel flags: user
    namespaces can be present and administratively denied, and a policy that refuses the clone is
    indistinguishable from a missing feature until something tries.
    """
    binary = bwrap_path()
    if not binary:
        return False, "bwrap is not installed (conda-forge: bubblewrap)"
    try:
        # The same system binds a real jail gets: without the library symlinks a dynamically
        # linked binary fails with ENOENT on its ELF interpreter, which reads as "bwrap is
        # broken" when in fact the probe was under-specified.
        argv = [binary, "--unshare-user", "--unshare-pid", "--ro-bind", "/usr", "/usr"]
        for target, link in _SYSTEM_SYMLINKS:
            argv += ["--symlink", target, link]
        probe = subprocess.run(
            [*argv, "/bin/true"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"bwrap could not run: {exc}"
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        return False, f"bwrap failed: {detail[-1] if detail else f'exit {probe.returncode}'}"
    return True, "ok"


def _symlink_pairs(paths: list[str]) -> list[tuple[str, str]]:
    """``(target, link)`` pairs that make each configured path spelling work inside the jail.

    Roots are bound at their *resolved* location, but every tool argument, test fixture and
    observation path is written the **configured** way, which here goes through two symlinks
    (``/data`` → ``/nfsdata1``, and a home whose ``.conda`` is itself a link). So each configured
    path that resolves elsewhere gets one symlink at exactly that path.

    Recreating the symlinked *ancestors* instead does not work, and the failure is not obvious:
    bwrap creates a mount point's parents itself, so once ``/grad`` exists in the jail as a
    symlink, creating ``/grad/fzhou/.conda`` beneath it fails with "Can't mkdir parents" — every
    Layer-B suite died there before running a single test (2026-08-19). Linking only the leaf keeps
    every parent a plain directory.
    """
    pairs: dict[str, str] = {}
    for raw in paths:
        path = Path(raw)
        resolved = path.resolve()
        if resolved != path:
            pairs[str(path)] = str(resolved)
    # Outermost only. Once ``/opt/heasoft/heasoft-6.34`` is a symlink in the jail, creating another
    # at ``…/x86_64-…`` beneath it fails the same way ancestor links did — and it is unnecessary,
    # since the inner path is already reachable through the outer link.
    outermost = {
        link: target
        for link, target in pairs.items()
        if not any(other != link and Path(link).is_relative_to(Path(other)) for other in pairs)
    }
    return [(target, link) for link, target in sorted(outermost.items())]


def policy_from_config(*, network: bool = True, label: str = "") -> JailPolicy:
    """The base policy: ``agent_fs_allow`` as writable roots, ``agent_fs_readonly`` as read-only.

    The split is what makes the jail tighter than the guard rather than a copy of it: the analyst
    conda environment, HEASoft and CALDB are read for the whole run and written by nobody, so a
    process that tries is stopped by the kernel, not by a review.
    """
    config = _config()
    allow = [str(entry) for entry in (config.get("agent_fs_allow") or [])]
    readonly = {str(entry) for entry in (config.get("agent_fs_readonly") or [])}
    return JailPolicy(
        ro=tuple(entry for entry in allow if entry in readonly),
        rw=tuple(entry for entry in allow if entry not in readonly),
        network=network,
        label=label,
    )


def jail_argv(policy: JailPolicy) -> list[str]:
    """The ``bwrap`` prefix for one process, or ``[]`` when there is nothing to prefix.

    Returning an empty list when the jail is unavailable is deliberate: a host without ``bwrap``
    runs exactly as it does today (the guard, the harness sandbox), and the launch check is what
    tells the operator the stronger enforcement is not in effect. A silent hard failure here
    would turn a missing optional dependency into an unrunnable pipeline.
    """
    binary = bwrap_path()
    if not binary:
        return []

    # ``--die-with-parent`` matters for the interrupt path: the jailed process is pid 1 of its own
    # namespace, so when the runtime kills what it spawned, everything inside goes with it rather
    # than surviving as an orphan that can still write to the toolkit (§9.9's whole point).
    argv = [binary, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent"]
    if not policy.network:
        argv += ["--unshare-net"]
    for entry in _SYSTEM_RO:
        if Path(entry).exists():
            argv += ["--ro-bind", entry, entry]
    for target, link in _SYSTEM_SYMLINKS:
        argv += ["--symlink", target, link]

    # Outermost first, so a nested entry with a different mode still takes effect: a later bind
    # lands on top of an earlier one, which is how a read-only subtree inside a writable root (or
    # the reverse) stays meaningful rather than being silently shadowed by declaration order.
    roots = [(entry, "--ro-bind") for entry in policy.ro]
    roots += [(entry, "--bind") for entry in policy.rw]
    for entry, mode in sorted(roots, key=lambda pair: len(Path(pair[0]).resolve().parts)):
        resolved = str(Path(entry).resolve())
        if Path(resolved).exists():
            argv += [mode, resolved, resolved]
    for entry in policy.ro_files:
        resolved = str(Path(entry).resolve())
        if Path(resolved).is_file():
            argv += ["--ro-bind", resolved, resolved]

    for target, link in _symlink_pairs([*policy.ro, *policy.rw]):
        argv += ["--symlink", target, link]

    if policy.home:
        home = str(Path(policy.home).resolve())
        argv += ["--bind", home, home, "--setenv", "HOME", home]

    argv += ["--proc", "/proc", "--dev", "/dev"]
    return argv


def wrap(argv: list[str], policy: JailPolicy) -> list[str]:
    """``argv`` prefixed with the jail, or unchanged when the jail is off or unavailable."""
    if not jail_enabled():
        return list(argv)
    prefix = jail_argv(policy)
    return [*prefix, "--", *argv] if prefix else list(argv)


def check_os_jail_available() -> None:
    """Launch check: when the jail is configured, say whether this host can actually run it.

    A warning rather than a failure. The jail is defence in depth over the ``PathGuard``
    allow-list, so a host without it is the configuration every run used before this existed —
    worth stating on the record, not worth refusing to start.
    """
    if not jail_enabled():
        return
    ok, reason = jail_available()
    if ok:
        return
    from auto_research.preflight import PreflightWarning

    raise PreflightWarning(
        f"agent_os_jail is on but this host cannot run it ({reason}); runtime processes will run "
        "with the PathGuard allow-list only, which cannot see a subprocess's implicit reads."
    )


def write_launcher(path: Path, target: str, policy: JailPolicy) -> Path:
    """Write a wrapper script that execs ``target`` inside the jail, passing its arguments on.

    The author harnesses take a path to their own binary (``ClaudeAgentOptions.cli_path``,
    ``CodexConfig.codex_bin``) rather than a command line, and each SDK builds its own arguments
    after that. Wrapping the *binary* instead of the argv keeps us out of that: whatever a vendor
    decides to pass next release still lands inside the jail.
    """
    prefix = jail_argv(policy)
    command = [*prefix, "--", target] if prefix else [target]
    body = "#!/bin/sh\nexec " + " ".join(_quote(part) for part in command)
    path.write_text(body + ' "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


def _quote(part: str) -> str:
    """Shell-quote one argv element for the launcher script."""
    import shlex

    return shlex.quote(part)

def check_os_jail_probe() -> None:
    """Launch check: the analysis stack still imports **inside** the jail.

    The allow-list was written for a guard that inspects the paths a model names, so nobody had to
    know whether it was *complete* as a mount plan. It was not: every file in PyXspec's package
    directory is a symlink four levels up into a sibling of ``$HEADAS``, so jailing exactly the
    configured roots produced an importable-looking namespace package and an ``ImportError`` on the
    first real symbol — thirty Layer-B suites deep, not at launch (2026-08-19).

    So when the jail is on, the configured ``analyze_probe`` runs inside it before any model is
    built. This is a **failure**, not a warning: with a jail that breaks the science stack every
    tool call and every Layer-B test would fail, and refusing to start says so once instead of
    forty times.
    """
    if not jail_enabled():
        return
    ok, _ = jail_available()
    if not ok:
        return  # check_os_jail_available already warns; nothing to probe

    config = _config()
    probe = str(config.get("analyze_probe") or "").strip()
    if not probe:
        return
    interpreter = Path(str(config.get("analyze_env") or "")) / "bin" / "python"
    if not interpreter.is_file():
        return  # the analyze_probe check owns that failure

    from auto_research.sandbox.runner import _source_ini_env

    ini = config.get("analyze_ini_sh")
    env = dict(_source_ini_env(ini)) if ini else dict(os.environ)
    toolkit = str(config.get("toolkit_root") or "")
    if toolkit:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = toolkit + (":" + existing if existing else "")
    home = Path(str(config.get("runtime_dir") or "/tmp")).expanduser() / "jail-home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)

    policy = policy_from_config(network=True, label="probe")
    policy = JailPolicy(
        ro=policy.ro, rw=(*policy.rw, str(home)), network=True, home=str(home), label="probe"
    )
    try:
        result = subprocess.run(
            [*jail_argv(policy), "--", str(interpreter), "-c", probe],
            capture_output=True, text=True, timeout=300, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"the jailed analysis probe could not run: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ValueError(
            f"`{probe}` fails inside the OS jail: {detail[-1] if detail else 'no output'}. "
            "Something the analysis stack needs is outside `agent_fs_allow` — often a directory "
            "whose contents symlink out of it, which only a mount plan notices. Add the missing "
            "root (read-only in `agent_fs_readonly`), or set `agent_os_jail: false`."
        )

def jail_home(label: str) -> Path:
    """A writable HOME for one jailed process, under the runtime data dir.

    A jailed process cannot see the real home, and plenty of software insists on one: matplotlib
    writes a font cache, pytest and pip want ``.cache``, git wants ``.config``. Giving each kind of
    process its own keeps that noise inside the runtime dir where the rest of a run's scratch lives.
    """
    from auto_research.env import runtime_data_dir

    home = runtime_data_dir() / "jail-home" / (label or "default")
    home.mkdir(parents=True, exist_ok=True)
    return home


def coder_policy(*, label: str, extra_rw: tuple[str, ...] = ()) -> JailPolicy:
    """The jail for an author harness: the allow-list, its own home, and its credential file.

    The Claude harness authenticates from ``~/.claude/.credentials.json``, which a jailed home
    hides. Binding that one file read-only keeps the subscription path working without exposing the
    home directory it lives in — every other harness secret arrives through the environment.
    """
    base = policy_from_config(network=True, label=label)
    home = jail_home(label)
    credential = Path.home() / ".claude" / ".credentials.json"
    return JailPolicy(
        ro=base.ro,
        rw=(*base.rw, *extra_rw, str(home)),
        network=True,
        home=str(home),
        label=label,
        ro_files=(str(credential),) if credential.is_file() else (),
    )
