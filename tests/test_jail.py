"""The OS-level filesystem jail: what it builds, and the rules it learned the hard way.

Deterministic. No jailed process is launched here beyond ``bwrap``'s own availability probe, which
is skipped when the host has no ``bwrap``. What is asserted is the argv the builder produces, since
that argv *is* the policy: a bind in the wrong order or a symlink in the wrong place does not fail
loudly, it silently hands the process a filesystem nobody intended.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from auto_research.sandbox import jail
from auto_research.sandbox.jail import JailPolicy


@pytest.fixture
def bwrap(monkeypatch):
    """Pretend bwrap exists, so argv construction is testable on any host."""
    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    return "/usr/bin/bwrap"


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every ``(src, dst)`` for one bwrap flag, in the order the argv applies them."""
    found = []
    for index, token in enumerate(argv):
        if token == flag:
            found.append((argv[index + 1], argv[index + 2]))
    return found


def test_wrap_is_a_no_op_when_the_jail_is_off(monkeypatch):
    """The default is off, and off must mean *byte-identical* to the command we would have run."""
    monkeypatch.setattr(jail, "jail_enabled", lambda: False)
    argv = ["/bin/python", "-c", "print(1)"]
    assert jail.wrap(argv, JailPolicy()) == argv


def test_wrap_is_a_no_op_when_bwrap_is_absent(monkeypatch):
    """A host without bwrap runs exactly as it did before the jail existed. The launch check is
    what tells the operator the stronger enforcement is not in effect; refusing to run would turn a
    missing optional dependency into an unrunnable pipeline."""
    monkeypatch.setattr(jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(jail, "bwrap_path", lambda: None)
    argv = ["/bin/python", "-c", "print(1)"]
    assert jail.wrap(argv, JailPolicy()) == argv


def test_network_is_denied_only_when_the_policy_says_so(bwrap):
    assert "--unshare-net" in jail.jail_argv(JailPolicy(network=False))
    assert "--unshare-net" not in jail.jail_argv(JailPolicy(network=True))


def test_the_jailed_process_dies_with_its_parent(bwrap):
    """The interrupt path kills what the runtime spawned; without this the jailed tree could
    outlive it and keep writing to the toolkit, which is exactly what §9.9 exists to prevent."""
    assert "--die-with-parent" in jail.jail_argv(JailPolicy())


def test_read_only_roots_are_bound_read_only(bwrap, tmp_path):
    ro, rw = tmp_path / "ro", tmp_path / "rw"
    ro.mkdir()
    rw.mkdir()
    argv = jail.jail_argv(JailPolicy(ro=(str(ro),), rw=(str(rw),)))
    assert (str(ro), str(ro)) in _pairs(argv, "--ro-bind")
    assert (str(rw), str(rw)) in _pairs(argv, "--bind")
    assert (str(rw), str(rw)) not in _pairs(argv, "--ro-bind")


def test_a_nested_root_is_applied_after_its_parent(bwrap, tmp_path):
    """Order is policy. A later bind lands on top of an earlier one, so a read-only subtree inside
    a writable root only means anything if the parent is applied first — otherwise declaration
    order in a config file silently decides which rule wins."""
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    argv = jail.jail_argv(JailPolicy(ro=(str(inner),), rw=(str(outer),)))
    positions = [
        index for index, token in enumerate(argv) if token in {"--bind", "--ro-bind"}
        and argv[index + 1] in {str(outer), str(inner)}
    ]
    applied = [argv[index + 1] for index in positions]
    assert applied.index(str(outer)) < applied.index(str(inner))


def test_a_symlinked_root_is_reachable_by_its_configured_spelling(bwrap, tmp_path):
    """Roots are bound where they resolve, but every tool argument and observation path is written
    the configured way, so that spelling needs to exist inside the jail."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    argv = jail.jail_argv(JailPolicy(rw=(str(link),)))
    assert (str(real), str(real)) in _pairs(argv, "--bind")
    assert (str(real), str(link)) in _pairs(argv, "--symlink")


def test_only_the_outermost_symlinked_path_gets_a_link(bwrap, tmp_path):
    """The bug that killed all 30 Layer-B suites twice: bwrap creates a mount point's parents
    itself, so once an outer path exists as a symlink, creating anything beneath it fails with
    "Can't mkdir parents". The inner path needs no link of its own — it is already reachable
    through the outer one."""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    argv = jail.jail_argv(JailPolicy(rw=(str(link), str(link / "sub"))))
    links = [dst for _, dst in _pairs(argv, "--symlink")]
    assert str(link) in links
    assert str(link / "sub") not in links


def test_a_credential_file_is_bound_without_its_directory(bwrap, tmp_path):
    """The Claude author authenticates from a file in a home the jail hides. Binding that one file
    keeps the subscription path working without exposing the directory it lives in."""
    secret = tmp_path / "home" / ".claude" / ".credentials.json"
    secret.parent.mkdir(parents=True)
    secret.write_text("{}")
    argv = jail.jail_argv(JailPolicy(ro_files=(str(secret),)))
    assert (str(secret), str(secret)) in _pairs(argv, "--ro-bind")
    assert not any(dst == str(secret.parent) for _, dst in _pairs(argv, "--ro-bind"))


def test_home_is_set_inside_the_jail(bwrap, tmp_path):
    """Plenty of software insists on a writable home: matplotlib's font cache, pytest's, git's."""
    home = tmp_path / "jail-home"
    home.mkdir()
    argv = jail.jail_argv(JailPolicy(home=str(home)))
    assert (str(home), str(home)) in _pairs(argv, "--bind")
    assert argv[argv.index("--setenv") + 1 : argv.index("--setenv") + 3] == ["HOME", str(home)]


def test_the_policy_splits_the_allow_list_into_read_only_and_writable(monkeypatch):
    """This split is what makes the jail tighter than the guard rather than a copy of it: nothing
    writes to the conda environment, HEASoft or CALDB for the whole life of a run."""
    monkeypatch.setattr(
        jail,
        "_config",
        lambda: {
            "agent_fs_allow": ["/toolkit", "/obs", "/conda", "/caldb"],
            "agent_fs_readonly": ["/conda", "/caldb"],
        },
    )
    policy = jail.policy_from_config(network=False, label="gate")
    assert policy.ro == ("/conda", "/caldb")
    assert policy.rw == ("/toolkit", "/obs")
    assert policy.network is False


def test_an_unconfigured_readonly_list_leaves_everything_writable(monkeypatch):
    """Absent config is not a licence to tighten: a machine that never declared the split keeps
    today's semantics, where the guard treats every allowed root as read and write."""
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": ["/a", "/b"]})
    policy = jail.policy_from_config()
    assert policy.ro == ()
    assert policy.rw == ("/a", "/b")


def test_launcher_execs_the_real_binary_inside_the_jail(bwrap, tmp_path):
    """The harnesses take a path to their binary and build their own arguments after that, so the
    wrapper passes "$@" through: whatever a vendor decides to pass next release still lands inside
    the jail."""
    script = jail.write_launcher(tmp_path / "claude-jailed", "/opt/real/claude", JailPolicy())
    body = script.read_text()
    assert body.startswith("#!/bin/sh\nexec /usr/bin/bwrap ")
    assert body.rstrip().endswith("-- /opt/real/claude \"$@\"")
    assert os.access(script, os.X_OK)


def test_launcher_without_bwrap_still_execs_the_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(jail, "bwrap_path", lambda: None)
    script = jail.write_launcher(tmp_path / "codex-jailed", "/opt/real/codex", JailPolicy())
    assert script.read_text().strip() == '#!/bin/sh\nexec /opt/real/codex "$@"'


def test_availability_check_warns_rather_than_blocking(monkeypatch):
    """Defence in depth: a host without the jail is the configuration every earlier run used, so
    saying so on the record beats refusing to start."""
    from auto_research.preflight import PreflightWarning

    monkeypatch.setattr(jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(jail, "jail_available", lambda: (False, "bwrap is not installed"))
    with pytest.raises(PreflightWarning, match="bwrap is not installed"):
        jail.check_os_jail_available()


def test_availability_check_is_silent_when_the_jail_is_off(monkeypatch):
    monkeypatch.setattr(jail, "jail_enabled", lambda: False)
    jail.check_os_jail_available()


def test_probe_failure_blocks_the_launch(monkeypatch, tmp_path):
    """Unlike availability, a jail that breaks the science stack is fatal: every tool call and
    every Layer-B test would fail, so refusing once beats failing forty times. This is the check
    that would have caught PyXspec's files symlinking out of `$HEADAS` at launch instead of thirty
    suites deep."""
    interpreter = tmp_path / "env" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    interpreter.chmod(0o755)
    monkeypatch.setattr(jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(jail, "jail_available", lambda: (True, "ok"))
    monkeypatch.setattr(
        jail,
        "_config",
        lambda: {
            "analyze_probe": "import xspec",
            "analyze_env": str(tmp_path / "env"),
            "agent_fs_allow": [],
        },
    )
    monkeypatch.setattr(jail, "jail_argv", lambda policy: [])

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "ImportError: cannot import name 'AllData' from 'xspec' (unknown location)"

    monkeypatch.setattr(jail.subprocess, "run", lambda *a, **k: _Failed())
    with pytest.raises(ValueError, match="fails inside the OS jail"):
        jail.check_os_jail_probe()


def test_probe_is_silent_when_the_jail_is_off(monkeypatch):
    monkeypatch.setattr(jail, "jail_enabled", lambda: False)
    jail.check_os_jail_probe()


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_availability_probe_actually_runs_a_jailed_command():
    """The availability check runs a jailed command rather than reading kernel flags, because user
    namespaces can be present and administratively denied. Its own first version under-specified
    the mounts and reported a working host as broken."""
    ok, reason = jail.jail_available()
    assert ok, reason
