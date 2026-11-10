"""The OS-level filesystem jail: what it builds, and the rules it learned the hard way.

Deterministic. The jailed processes launched here (``bwrap``'s own availability probe and the
launch probe of a fixture toolkit's default environment) are skipped when the host has no ``bwrap``. What is asserted is the argv the builder produces, since
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


def test_wrap_always_prefixes_the_jail(bwrap):
    """The jail is unconditional (D25): the command runs after bwrap's own argv and ``--``."""
    argv = ["/bin/python", "-c", "print(1)"]
    wrapped = jail.wrap(argv, JailPolicy())
    assert wrapped[0] == "/usr/bin/bwrap" and wrapped[-4:] == ["--", *argv]


def test_nothing_runs_unjailed_when_bwrap_is_absent(monkeypatch, tmp_path):
    """A host without bwrap gets no unjailed fallback: wrapping a command and writing a harness
    launcher both raise, naming bwrap."""
    monkeypatch.setattr(jail, "bwrap_path", lambda: None)
    with pytest.raises(RuntimeError, match="bwrap"):
        jail.wrap(["/bin/python", "-c", "print(1)"], JailPolicy())
    with pytest.raises(RuntimeError, match="bwrap"):
        jail.write_launcher(tmp_path / "codex-jailed", "/opt/real/codex", JailPolicy())
    assert not (tmp_path / "codex-jailed").exists()


def test_network_is_denied_only_when_the_policy_says_so(bwrap):
    assert "--unshare-net" in jail.jail_argv(JailPolicy(network=False))
    assert "--unshare-net" not in jail.jail_argv(JailPolicy(network=True))


def test_the_jailed_process_dies_with_its_parent(bwrap):
    """The interrupt path kills what the runtime spawned; without this the jailed tree could
    outlive it and keep writing to the toolkit, which is exactly what §9.9 exists to prevent."""
    assert "--die-with-parent" in jail.jail_argv(JailPolicy())


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_a_jailed_process_has_no_controlling_terminal_session():
    """TIOCSTI on /dev/tty from inside the jail injected keystrokes into the parent shell
    (legacy_tiocsti=1, reproduced 2026-10-06). The jailed process must be in a session created
    inside the jail: the caller's session lies outside the pid namespace, where getsid reads 0."""
    import subprocess

    probe = subprocess.run(
        [*jail.jail_argv(JailPolicy(network=False)), "--", "/usr/bin/python3", "-c",
         "import os; print(os.getsid(0) != 0)"],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "True"


def test_every_jailed_process_starts_its_own_session(bwrap):
    """No jailed process shares the caller's terminal session (TIOCSTI, 2026-10-06)."""
    assert "--new-session" in jail.jail_argv(JailPolicy())


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


def test_the_coder_policy_binds_dot_git_read_only_inside_every_writable_root(bwrap, tmp_path):
    """The coder's git ban is a kernel rule, not a command-string rule.

    A Bash deny-rule is prefix-matched and slips on a pipe, a subshell or ``python -c subprocess``
    (§13). Making ``.git`` read-only inside the writable tree refuses the write itself, however it
    is spelled, and leaves ``status``/``diff``/``log`` working — which is what the coder was denied
    on 2026-09-09 while reporting its own change set (one wasted turn).
    """
    tree = tmp_path / "worktree"
    (tree / ".git").mkdir(parents=True)
    policy = jail.coder_policy(label="probe", extra_rw=(str(tree),))
    assert str(tree) in policy.rw
    assert str(tree / ".git") in policy.ro
    argv = jail.jail_argv(policy)
    applied = [
        argv[index + 1] for index, token in enumerate(argv)
        if token in {"--bind", "--ro-bind"} and argv[index + 1] in {str(tree), str(tree / ".git")}
    ]
    # Outermost first, or the writable tree would land on top and silently re-open .git.
    assert applied.index(str(tree)) < applied.index(str(tree / ".git"))
    assert (str(tree / ".git"), str(tree / ".git")) in _pairs(argv, "--ro-bind")


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_a_jailed_process_can_read_git_history_and_cannot_write_it(tmp_path):
    """The property itself, measured against a real repository rather than argv.

    Asserting the flags proves the plan; only running it proves git behaves as the plan intends —
    and the interesting half is that a *read* still works, since a read-only ``.git`` could
    plausibly have broken ``status`` (it refreshes the index when it can).
    """
    import subprocess

    tree = tmp_path / "repo"
    tree.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731 — one-line local
        ["git", "-C", str(tree), *args], check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (tree / "f.txt").write_text("a\n", encoding="utf-8")
    run("add", "f.txt")
    run("commit", "-qm", "init")
    (tree / "f.txt").write_text("a\nb\n", encoding="utf-8")

    policy = JailPolicy(ro=(str(tree / ".git"),), rw=(str(tree),), network=False)
    def jailed(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*jail.jail_argv(policy), "--", "git", "-C", str(tree), *args],
            capture_output=True, text=True,
        )

    assert jailed("status", "--short").returncode == 0
    assert jailed("diff", "--stat").stdout.strip()
    assert jailed("log", "--oneline").returncode == 0
    for write in (("add", "f.txt"), ("commit", "-qm", "x"), ("checkout", "--", "f.txt")):
        refused = jailed(*write)
        assert refused.returncode != 0, f"{write} was allowed inside the jail"
        assert "Read-only file system" in refused.stderr, refused.stderr


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


def test_a_host_that_cannot_run_the_jail_is_refused_at_launch(monkeypatch):
    """Unjailed is not accepted (D25): the launch preflight fails, with the host's reason, when
    bwrap cannot run here; it is a failure, never a warning."""
    import asyncio

    from auto_research.preflight import Preflight, PreflightError, build_default_preflight

    monkeypatch.setattr(jail, "jail_available", lambda: (False, "bwrap is not installed"))
    # The launch checklist carries the check; run alone, it fails the launch.
    check = dict(build_default_preflight()._checks)["os_jail_available"]
    assert check is jail.check_os_jail_available
    preflight = Preflight()
    preflight.register("os_jail_available", check)
    with pytest.raises(PreflightError, match=r"\[os_jail_available\] this host cannot run the OS jail "
                                             r"\(bwrap is not installed\)"):
        asyncio.run(preflight.run_checklist())


@pytest.mark.skipif(not jail.jail_available()[0], reason="the OS jail is required and bwrap cannot run here")
def test_a_host_that_runs_the_jail_passes_the_launch_check():
    jail.check_os_jail_available()


@pytest.fixture
def jailed_toolkit(tmp_path, tmp_path_factory, monkeypatch):
    """``make(probe) -> orchestration.yaml``: the fixture toolkit with ``probe`` on its default
    environment (derived from this interpreter), this process configured on it with the jail on."""
    import sys

    from auto_research import toolkit_contract
    from auto_research.config_paths import CONFIG_DIR_ENV
    from tests.fixtures import toolkit as fixture

    def make(probe: str) -> Path:
        root = fixture.build_toolkit(tmp_path / "toolkit", env_base=Path(sys.prefix).name)
        with (root / "envs" / fixture.ENV_ID / "env.yaml").open("a", encoding="utf-8") as handle:
            handle.write(f"probe: {probe!r}\n")
        config = fixture.write_config(tmp_path / "config", root, analysis_prefix=Path(sys.prefix),
                                      allow=(tmp_path_factory.getbasetemp(),))
        monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
        toolkit_contract.reset()
        return config / "orchestration.yaml"

    yield make
    toolkit_contract.reset()


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_the_probe_runs_the_default_environment_inside_the_jail(jailed_toolkit):
    """With the jail on, the launch probe runs where a unit's REPL runs: inside it, so a stack whose
    files symlink out of the allow-list (PyXspec out of `$HEADAS`, 2026-08-19) fails at launch
    instead of thirty Layer-B suites deep. This repository is outside the allow-list: unseen there."""
    from auto_research.sandbox.runner import check_analysis_probe

    outside = Path(__file__).resolve()
    jailed_toolkit(f"import os, demo_utils; assert not os.path.exists({str(outside)!r})")
    check_analysis_probe()


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_the_activation_read_back_from_the_jail_lands_on_the_consumers_jail_home(jailed_toolkit, tmp_path):
    """The activation runs in the analysis jail, where HOME is that jail's own: what it derived from
    that home comes back on the home of the jail the values are handed to (an author's), or as the
    activation set it when no home is named."""
    import yaml

    from auto_research.sandbox.runner import analysis_activation_env
    from tests.fixtures import toolkit as fixture

    path = jailed_toolkit("import sys")
    (tmp_path / "toolkit" / "envs" / fixture.ENV_ID / "activate.sh").write_text('export DEMO_PARAMS="$HOME/params;/opt/sys"\n')
    activated = analysis_activation_env(yaml.safe_load(path.read_text(encoding="utf-8")), home="/host/home")
    assert activated["DEMO_PARAMS"] == "/host/home/params;/opt/sys", activated["DEMO_PARAMS"]
    assert jail.JAIL_HOME_AT not in "".join(activated.values())
    as_set = analysis_activation_env(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert as_set["DEMO_PARAMS"] == f"{jail.JAIL_HOME_AT}/params;/opt/sys"


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_a_probe_failing_in_the_jail_blocks_the_launch_and_names_the_allow_list(jailed_toolkit):
    """A jail that breaks the stack is fatal: every tool call and every Layer-B test would fail, so
    refusing once beats failing forty times; the refusal names the allow-list as the usual fix."""
    from auto_research.sandbox.runner import check_analysis_probe

    with pytest.raises(RuntimeError, match="agent_fs_allow"):
        jailed_toolkit("import __no_such_module_xyz__")
        check_analysis_probe()


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_availability_probe_actually_runs_a_jailed_command():
    """The availability check runs a jailed command rather than reading kernel flags, because user
    namespaces can be present and administratively denied. Its own first version under-specified
    the mounts and reported a working host as broken."""
    ok, reason = jail.jail_available()
    assert ok, reason


# --------------------------------------------------------------------------- harness binaries


def test_the_launcher_binds_the_binary_it_execs(tmp_path):
    """A wrapper that execs a path the mount namespace does not contain fails with
    ``execvp: No such file or directory`` — which is how the first jailed sweep lost all 40 of its
    trials. Both harnesses ship their CLI inside the orchestration environment's site-packages, a
    tree the allow-list deliberately does not name, so nothing else would ever bind it.
    """
    from auto_research.sandbox.jail import JailPolicy, write_launcher

    vendor = tmp_path / "vendor" / "bin"
    vendor.mkdir(parents=True)
    binary = vendor / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    script = write_launcher(
        tmp_path / "launcher", str(binary),
        JailPolicy(ro=(), rw=(str(tmp_path),), network=True, home=None, label="t"),
    )
    body = script.read_text()

    assert str(vendor.resolve()) in body, "the binary's own directory must be bound read-only"
    assert f"--ro-bind {vendor.resolve()}" in body or "--ro-bind" in body


def test_the_launcher_binds_the_directory_not_only_the_file(tmp_path):
    """A bundled CLI normally needs its siblings (a runtime, a wasm blob, a licence file), so the
    parent directory is bound rather than the single file."""
    from auto_research.sandbox.jail import JailPolicy, write_launcher

    vendor = tmp_path / "pkg" / "bin"
    vendor.mkdir(parents=True)
    binary = vendor / "cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    (vendor / "runtime.so").write_text("x")

    body = write_launcher(
        tmp_path / "l", str(binary),
        JailPolicy(ro=(), rw=(str(tmp_path),), network=True, home=None, label="t"),
    ).read_text()

    assert str(vendor.resolve()) in body
    assert f"{binary.resolve()} " not in body.replace(str(vendor.resolve()), "")


def test_every_harness_this_project_can_run_resolves_a_binary():
    """A harness whose CLI cannot be resolved cannot be jailed or checked, so the roster and the
    resolver must agree about what exists."""
    from auto_research.sandbox.coder.harness import HARNESS_ENDPOINTS
    from auto_research.sandbox.jail import harness_binary

    for harness in sorted({h for _, h in HARNESS_ENDPOINTS}):
        assert harness_binary(harness) is not None, harness


def test_binary_reachability_fails_on_a_host_that_cannot_run_the_jail(monkeypatch):
    """No host passes the check vacuously: one that cannot run the jail reports why."""
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "jail_available", lambda: (False, "bwrap is not installed"), raising=True)
    assert jail.binary_reachable_in_jail("/definitely/not/here", label="t") == (False, "bwrap is not installed")


def test_a_missing_binary_is_reported_rather_than_probed(monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "jail_available", lambda: (True, ""), raising=True)
    ok, why = jail.binary_reachable_in_jail("/definitely/not/here", label="t")
    assert ok is False and "does not exist outside the jail" in why


def test_the_launcher_execs_the_same_path_it_binds(tmp_path):
    """Bind and exec must agree on one spelling of the path.

    They did not: the bind resolved symlinks and the exec did not, so on a host that reaches its
    environment through a symlink the Claude author still failed ``execvp`` after its own directory
    had been bound. The launch check resolved too, so it passed while the launcher failed.
    """
    from auto_research.sandbox.jail import JailPolicy, write_launcher

    real_dir = tmp_path / "real" / "bin"
    real_dir.mkdir(parents=True)
    binary = real_dir / "cli"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    link = tmp_path / "via-symlink"
    link.symlink_to(tmp_path / "real")

    body = write_launcher(
        tmp_path / "l", str(link / "bin" / "cli"),
        JailPolicy(ro=(), rw=(str(tmp_path),), network=True, home=None, label="t"),
    ).read_text()

    assert str(binary.resolve()) in body, "the exec target must be the resolved path"
    assert str(link) not in body, "the unresolved path is not bound, so it must not be exec'd"


def test_a_re_addressed_credential_lands_inside_the_jail_home(tmp_path, monkeypatch):
    """The jail replaces HOME, so a credential bound at its own path is somewhere the process will
    never look. It is carried to the address the process actually reads, and that address has to be
    built from the *resolved* home, since the home itself is bound resolved."""
    from auto_research.sandbox import jail

    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text("{}")
    monkeypatch.setattr(jail.Path, "home", staticmethod(lambda: fake_home), raising=True)
    monkeypatch.setattr(jail, "coder_home", lambda label: tmp_path / "jail" / label, raising=True)
    (tmp_path / "jail" / "claude_sdk").mkdir(parents=True)

    policy = jail.coder_policy(label="claude_sdk")

    assert len(policy.ro_files_at) == 1
    source, destination = policy.ro_files_at[0]
    assert source == str(fake_home / ".claude" / ".credentials.json")
    assert destination.startswith(str((tmp_path / "jail" / "claude_sdk").resolve()))
    assert destination.endswith("/.claude/.credentials.json")
    # Its parent exists on disk, so the CLI's own writes to that directory have a writable place.
    assert Path(destination).parent.is_dir()


def test_a_re_addressed_file_is_bound_after_the_home(tmp_path):
    """A later bind lands on top of an earlier one, and the destination is inside the home, so
    emitting the file first would let the home bind hide it."""
    from auto_research.sandbox.jail import JailPolicy, jail_argv

    home = tmp_path / "home"
    home.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text("{}")
    argv = jail_argv(JailPolicy(
        ro=(), rw=(str(tmp_path),), network=True, home=str(home), label="t",
        ro_files_at=((str(secret), str(home / ".claude" / ".credentials.json")),),
    ))

    assert "--setenv" in argv
    assert argv.index("--setenv") < argv.index(str(home / ".claude" / ".credentials.json"))


def test_each_author_gets_its_own_settings_home(tmp_path):
    """Concurrent authors must not share a settings directory: the CLI writes login state and
    history into it, and a sweep runs several at once."""
    from auto_research.sandbox.jail import codex_settings_home

    a = codex_settings_home("codex-trial-a")
    b = codex_settings_home("codex-trial-b")
    assert a != b and a.is_dir() and b.is_dir()


def test_the_author_settings_home_is_on_local_disk():
    """Both harness CLIs keep their state in SQLite, which cannot take locks on an NFS mount.

    The runtime data directory is NFS on this cluster, so every Codex author failed with "failed to
    initialize sqlite state runtime" while its settings home lived there. The author home therefore
    does *not* follow ``jail_home`` under the runtime dir.
    """
    import tempfile

    from auto_research.sandbox.jail import coder_home, jail_home

    home = coder_home("probe-label")
    assert str(home).startswith(tempfile.gettempdir())
    assert not str(home).startswith(str(jail_home("probe-label").parent.parent))


def test_the_author_home_is_bound_writable_by_the_policy_itself():
    """It must not depend on the allow-list happening to name the temp root."""
    from auto_research.sandbox.jail import coder_policy

    policy = coder_policy(label="probe-label")
    assert policy.home in policy.rw


def test_two_authors_on_identically_named_trees_get_different_homes(tmp_path):
    """Every trial clones its toolkit into a directory called ``repo``, so a label derived from the
    directory *name* collided across all concurrent authors and they raced on one state database."""
    from auto_research.models.types import RoleModelConfig
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    member = RoleModelConfig(name="m", provider="deepseek", harness="codex",
                             model="deepseek-v4-flash", base_url="https://api.deepseek.com/")
    labels = set()
    for parent in ("trial-01", "trial-02"):
        worktree = tmp_path / parent / "repo"
        worktree.mkdir(parents=True)
        labels.add(
            CodexBackend(member=member, worktree=worktree, system_prompt="p")
            ._jail_label()
        )
    assert len(labels) == 2, f"both authors got the same settings home: {labels}"


def test_the_claude_launcher_binds_the_worktree_the_author_edits(tmp_path):
    """The author's own working tree must be inside its jail.

    Binding only the artifacts directory left the clone outside, so the author silently read the
    *live* toolkit checkout instead of its own and could write nowhere: 10 and 16 "File does not
    exist" tool results in the first two trials of 2026-08-21, with the CLI reporting its cwd as the
    settings home. Latent while the allow-list made the temp root writable for authors; tightening
    the policy to two writable places removed that accidental coverage and exposed it.
    """
    from auto_research.sandbox.coder.claude_sdk import _jail_launcher

    base = tmp_path / "authoring"
    base.mkdir()
    worktree = tmp_path / "clone" / "repo"
    worktree.mkdir(parents=True)

    body = Path(_jail_launcher(base, worktree)).read_text()

    assert f"--bind {worktree}" in body, "the author cannot edit a tree outside its jail"
    assert f"--bind {base}" in body, "artifacts still need a writable home"


def test_both_harness_launchers_bind_their_worktree(tmp_path):
    """The property is per-harness, and only one of them had it. Asserted for both so a future
    harness cannot be added without it."""
    from auto_research.models.types import RoleModelConfig
    from auto_research.sandbox.coder.claude_sdk import _jail_launcher
    from auto_research.sandbox.coder.codex_sdk import CodexBackend

    worktree = tmp_path / "repo"
    worktree.mkdir()

    claude = Path(_jail_launcher(tmp_path / "authoring", worktree)).read_text()
    codex_backend = CodexBackend(
        member=RoleModelConfig(name="m", provider="deepseek", harness="codex",
                               model="deepseek-v4-flash", base_url="https://api.deepseek.com/"),
        worktree=worktree, system_prompt="p",
    )
    codex_body = Path(codex_backend._jail_launcher(None)).read_text()

    assert f"--bind {worktree}" in claude
    assert f"--bind {worktree}" in codex_body


def test_the_codex_launcher_is_not_written_into_the_working_tree(tmp_path, monkeypatch):
    """A runtime artifact in `research_toolkit` breaks the next tool in an authoring chain.

    Writing the launcher into the worktree left an untracked `.codex-jailed` behind after every
    session, so the next link found a dirty branch, `check_toolkit_branch_clean` raised, and the
    chain took the honest terminal. A codex author under the jail could land exactly ONE tool
    (measured 2026-09-01: a five-tool chain committed the first and died on the second with one
    untracked file). Invisible until then because `author_bench` authors one tool per trial.

    §9.9 is the general rule this restores: the only thing the runtime puts in that repository is
    an `approve_edit` commit.
    """
    from auto_research.sandbox.coder import codex_sdk

    worktree = tmp_path / "toolkit"
    worktree.mkdir()
    (worktree / ".git").mkdir()

    backend = codex_sdk.CodexBackend.__new__(codex_sdk.CodexBackend)
    backend._worktree = worktree
    backend._jail_readonly = ()

    from auto_research.sandbox import jail

    written: dict[str, Path] = {}

    def _fake_write_launcher(path, target, policy):
        written["path"] = Path(path)
        return Path(path)

    monkeypatch.setattr(jail, "write_launcher", _fake_write_launcher)
    monkeypatch.setattr(jail, "coder_policy", lambda **kw: object())
    monkeypatch.setattr(jail, "coder_home", lambda label: tmp_path / "home" / label)
    (tmp_path / "home").mkdir(exist_ok=True)

    backend._jail_launcher("/usr/bin/codex")

    assert "path" in written, "the launcher was never written"
    launcher = written["path"].resolve()
    assert worktree.resolve() not in launcher.parents, (
        f"the launcher landed inside the working tree at {launcher}; the next tool in the chain "
        f"would find a dirty branch"
    )
    assert launcher.name == ".codex-jailed"


@pytest.mark.skipif(not jail.bwrap_path(), reason="bwrap is not installed on this host")
def test_a_masked_path_is_empty_inside_the_jail(tmp_path):
    import subprocess

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "secret.py").write_text("x = 1\n")
    policy = JailPolicy(ro=(str(tmp_path),), masked=(str(tree),), network=False)
    probe = subprocess.run([*jail.jail_argv(policy), "--", "/bin/ls", "-A", str(tree)],
                           capture_output=True, text=True)
    assert probe.returncode == 0 and probe.stdout.strip() == "", probe

