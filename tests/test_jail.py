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


def test_binary_reachability_is_vacuously_true_with_the_jail_off(monkeypatch):
    """The check must never block a host that does not run the jail at all."""
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "jail_enabled", lambda: False, raising=True)
    assert jail.binary_reachable_in_jail("/definitely/not/here", label="t") == (True, "")


def test_a_missing_binary_is_reported_rather_than_probed(monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "jail_enabled", lambda: True, raising=True)
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
            CodexBackend(member=member, worktree=worktree, system_prompt="p", guard=None)
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
        worktree=worktree, system_prompt="p", guard=None,
    )
    codex = Path(codex_backend._jail_launcher(None) or "/dev/null")
    codex_body = codex.read_text() if codex.exists() and codex.name != "null" else ""

    assert f"--bind {worktree}" in claude
    if codex_body:
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

    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "jail_enabled", lambda: True)
    monkeypatch.setattr(jail, "jail_available", lambda: (True, ""))
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
