"""D3b ([F30]) — the shared package archive, a kept build put back, and an earlier definition's
build kept in use flagged untested.

conda is faked at the process boundary with the behaviour probed on conda 24.9.2 (2026-11-04):
a later `pkgs_dirs` entry is read as a cache when it lists the URL in `urls.txt` and the file
matches the lock's md5; anything else is downloaded into the first entry. Everything else is the
real code: `env_build.build`, the gate's `_gate_environment`, `build_committed_environments`,
`reconcile_environments`, the worker's environment routing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import shutil
from pathlib import Path

from auto_research.sandbox import env_build as B
from tests.test_env_build import _build, _conda_install, _definition, _fake_conda

#: What the package server holds (the network): URL → bytes.
SERVER = {
    "https://conda.anaconda.org/conda-forge/linux-64/zlib-1.3-h0_0.conda": b"zlib archive bytes",
    "https://conda.anaconda.org/conda-forge/noarch/certs-1.0-h0_0.conda": b"certs archive bytes",
}
LOCK = ("@EXPLICIT", *(f"{url}#{hashlib.md5(data).hexdigest()}" for url, data in SERVER.items()))


def _caching_conda(downloads: list[str]):
    """conda's cache behaviour on top of the record-writing fake: each lock URL is copied from a
    later `pkgs_dirs` entry that lists it in urls.txt with matching md5, else downloaded."""
    inner = _fake_conda([])

    def run(argv, env=None, **kw):
        command = argv[argv.index("--") + 1:] if "--" in argv else argv
        if "create" in command:
            first, *later = [Path(p) for p in env["CONDA_PKGS_DIRS"].split(",")]
            lock = Path(command[command.index("--file") + 1])
            for line in lock.read_text().splitlines():
                url, _, md5 = line.partition("#")
                if not url.startswith("http"):
                    continue
                name = url.rsplit("/", 1)[-1]
                cached = next((d / name for d in later
                               if url in (d / "urls.txt").read_text().split()
                               and (d / name).is_file()
                               and hashlib.md5((d / name).read_bytes()).hexdigest() == md5), None)
                first.mkdir(parents=True, exist_ok=True)
                if cached is not None:
                    shutil.copyfile(cached, first / name)
                else:
                    downloads.append(url)
                    (first / name).write_bytes(SERVER[url])
        return inner(argv, env=env, **kw)

    return run


def test_the_gate_and_a_commit_time_build_of_one_lock_share_the_archive(tmp_path, monkeypatch):
    """The gate builds the judged definition itself, but its downloads go into the archive; the
    commit-time build of the same lock that follows downloads nothing."""
    from tests.test_env_tools import _reconcile_setup

    from auto_research import env as app_env
    from auto_research.sandbox import gate
    from auto_research.sandbox.merge_flow import build_committed_environments

    from auto_research.sandbox import jail

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    (folder / "env.lock").write_text("# platform: linux-64\n" + "\n".join(LOCK) + "\n")
    # Jailed builds: only they read the archive (unjailed, conda would find it writable).
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: True)
    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    conda_exe = str(_conda_install(tmp_path / "opt" / "conda"))
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: conda_exe)
    downloads: list[str] = []
    conda = _caching_conda(downloads)

    class _Conda(B.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return conda(argv, **kwargs)

    monkeypatch.setattr(B, "StoppableRunner", _Conda)
    environment, error = asyncio.run(gate._gate_environment(toolkit, "built"))
    assert environment is not None, error
    assert sorted(downloads) == sorted(SERVER)  # the gate's build fetched everything once
    archive = app_env.agent_conda_dir() / B.PACKAGE_ARCHIVE
    assert sorted(p.name for p in archive.glob("*.conda")) == sorted(u.rsplit("/", 1)[-1] for u in SERVER)

    downloads.clear()
    [result] = build_committed_environments(toolkit, ["built"], runner=_Conda())
    assert result["ok"] and downloads == [], "the commit-time build took every package from the archive"


def test_a_file_that_does_not_match_its_lock_line_never_enters_the_archive(tmp_path):
    lock = tmp_path / "env.lock"
    lock.write_text("# platform: linux-64\n" + "\n".join(LOCK) + "\n")
    pkgs, archive = tmp_path / "pkgs", tmp_path / "archive"
    pkgs.mkdir()
    (zlib_url, zlib), (certs_url, certs) = SERVER.items()
    (pkgs / zlib_url.rsplit("/", 1)[-1]).write_bytes(zlib + b" changed by the build")  # not the lock's
    (pkgs / certs_url.rsplit("/", 1)[-1]).write_bytes(certs)

    added = B._archive_packages(lock, pkgs, archive)

    assert added == [certs_url.rsplit("/", 1)[-1]]
    assert [p.name for p in archive.glob("*.conda")] == added
    assert (archive / "urls.txt").read_text().split() == [certs_url]
    assert B._archive_packages(lock, pkgs, archive) == []  # nothing twice


def test_a_jailed_build_sees_the_archive_read_only(tmp_path):
    staged = B.read_definition(_definition(tmp_path / "defs", name="one"))
    conda_exe = str(_conda_install(tmp_path / "opt" / "conda"))
    work = tmp_path / "work"
    work.mkdir()
    policy = B.build_policy(conda_exe=conda_exe, staged=staged, target=tmp_path / "t", work=work,
                            archive=tmp_path / "archive")
    assert str(tmp_path / "archive") in policy.ro and str(tmp_path / "archive") not in policy.rw


def test_a_build_of_a_lock_kept_aside_is_put_back_not_rebuilt(tmp_path):
    """A branch switched back (run polevo-20261104): the build of that very lock is still kept aside
    by the rebuild that replaced it, so it is verified and put back."""
    folder = _definition(tmp_path / "defs", name="one")
    first_lock = (folder / "env.lock").read_text()
    _build(tmp_path, folder, [])
    first = B.build_record(tmp_path / "conda", tmp_path / "conda" / "envs" / "ar-one")
    (folder / "env.lock").write_text(first_lock.replace("h1_0", "h2_0"))
    _build(tmp_path, folder, [])  # the first build is kept aside
    (folder / "env.lock").write_text(first_lock)
    calls: list = []
    _build(tmp_path, folder, calls)

    assert not any("create" in argv for argv, _ in calls), "put back, not rebuilt"
    back = B.build_record(tmp_path / "conda", tmp_path / "conda" / "envs" / "ar-one")
    assert back["definition_sha256"] == first["definition_sha256"]
    kept = list((tmp_path / "conda" / "envs").glob("ar-one.prev-*"))
    assert len(kept) == 1  # the second build, kept aside in its turn


def test_a_failed_build_of_a_changed_definition_keeps_the_previous_one_flagged(tmp_path, monkeypatch):
    """The ruling: the restored build is the previous definition, so the catalogue marks it, every
    toolkit call routed into it is flagged untested env, and the note says so."""
    from tests.test_env_tools import _reconcile_setup, _routing_provider

    from auto_research import env as app_env
    from auto_research.sandbox.merge_flow import PREVIOUS_IN_USE, build_committed_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    [ok] = build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    assert ok["ok"]
    (folder / "env.lock").write_text((folder / "env.lock").read_text().replace("h1_0", "h9_0"))
    fake = _fake_conda([])

    def fails(argv, env=None, **kw):
        if "create" in argv:
            raise RuntimeError("conda create failed")
        return fake(argv, env=env, **kw)

    [failed] = build_committed_environments(toolkit, ["built"], runner=fails)
    assert not failed["ok"] and failed["previous_in_use"] is True
    [listed] = [e for e in app_env.env_catalogue({"toolkit_root": str(toolkit)}) if e.owner == "pipeline"]
    assert listed.name == "ar-built" and listed.superseded is True
    # Derived from the digests (D3c): the record is put back exactly as it was, nothing stamped.
    record = B.build_record(app_env.agent_conda_dir(), listed.prefix)
    assert record["definition_sha256"] == ok["definition_sha256"] and "superseded_by" not in record
    assert PREVIOUS_IN_USE.strip() == "The previous build stays in use; its results are flagged untested env."

    provider, sandbox, store = _routing_provider(tmp_path / "route", monkeypatch)
    listed_now = sandbox.environments
    sandbox.environments = lambda: [dataclasses.replace(e, superseded=True) for e in listed_now()]
    outcome = asyncio.run(provider.execute("t", {}))
    assert "untested environment for this tool" in outcome.text
    [handle] = re.findall(r"\bw0:r\d+", outcome.text)
    assert store.get(handle).env_untested is True
    assert json.dumps(failed)  # the entry the commit note and the launch event are written from


def test_a_revert_to_the_kept_definition_clears_its_superseded_mark(tmp_path, monkeypatch):
    """Review finding: A built, B fails (A kept in use, marked), the definition goes back to A —
    A is the current definition again and is never flagged."""
    from tests.test_env_tools import _reconcile_setup

    from auto_research import env as app_env
    from auto_research.sandbox.merge_flow import build_committed_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    first = (folder / "env.lock").read_text()
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    (folder / "env.lock").write_text(first.replace("h1_0", "h9_0"))
    fake = _fake_conda([])

    def fails(argv, env=None, **kw):
        if "create" in argv:
            raise RuntimeError("conda create failed")
        return fake(argv, env=env, **kw)

    build_committed_environments(toolkit, ["built"], runner=fails)
    catalogue = lambda: [e for e in app_env.env_catalogue({"toolkit_root": str(toolkit)})  # noqa: E731
                         if e.owner == "pipeline"]
    assert catalogue()[0].superseded is True
    (folder / "env.lock").write_text(first)
    assert catalogue()[0].superseded is False  # by construction: the build matches the branch again
    [again] = build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    assert again["ok"] and catalogue()[0].superseded is False


def test_a_kept_build_changed_by_hand_is_not_put_back(tmp_path):
    """Review finding: a distribution installed by hand into a kept prefix is drift, never its new
    baseline, so the switch back builds afresh."""
    folder = _definition(tmp_path / "defs", name="one")
    first_lock = (folder / "env.lock").read_text()
    _build(tmp_path, folder, [])
    (folder / "env.lock").write_text(first_lock.replace("h1_0", "h2_0"))
    _build(tmp_path, folder, [])
    [kept] = (tmp_path / "conda" / "envs").glob("ar-one.prev-*")
    (kept / "lib/python3.12/site-packages/evil-1.0.dist-info").mkdir(parents=True)
    (folder / "env.lock").write_text(first_lock)
    calls: list = []
    _build(tmp_path, folder, calls)
    assert any("create" in argv for argv, _ in calls), "rebuilt, not put back"


def test_only_a_jailed_build_reads_the_archive_and_never_one_holding_a_folder(tmp_path, monkeypatch):
    """Review finding: unjailed, conda would find the archive writable and extract into it; and an
    archive holding a folder (an extracted package nothing re-checks) is not used."""
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    conda_exe = str(_conda_install(tmp_path / "opt" / "conda"))
    archive = tmp_path / "archive"

    def pkgs_dirs_of(folder, *, jailed):
        calls: list = []
        B.build(B.read_definition(folder), conda_dir=tmp_path / f"conda-{folder.name}", conda_exe=conda_exe,
                base_envs={}, externals={}, jailed=jailed, runner=_fake_conda(calls),
                package_archive=archive)
        return next(env["CONDA_PKGS_DIRS"] for argv, env in calls if "config" in argv).split(",")

    assert len(pkgs_dirs_of(_definition(tmp_path / "a", name="one"), jailed=False)) == 1
    assert pkgs_dirs_of(_definition(tmp_path / "b", name="two"), jailed=True)[1] == str(archive)
    (archive / "zlib-1.3-h0_0").mkdir()
    assert len(pkgs_dirs_of(_definition(tmp_path / "c", name="three"), jailed=True)) == 1


def _failing(fake, calls):
    def fails(argv, env=None, **kw):
        if "create" in argv:
            calls.append(argv)
            raise RuntimeError("conda create failed")
        return fake(argv, env=env, **kw)

    return fails


def test_each_launch_tries_a_failing_build_once_more(tmp_path, monkeypatch):
    """D3c ruling (2): a fix or a passing network fault may have cleared it, so every launch tries
    the build of the current definition again, once, and keeps the earlier build in use."""
    from tests.test_env_tools import _reconcile_setup

    from auto_research.sandbox.merge_flow import build_committed_environments, reconcile_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    (folder / "env.lock").write_text((folder / "env.lock").read_text().replace("h1_0", "h9_0"))
    creates: list = []
    for launch in (1, 2):
        [result] = reconcile_environments(toolkit, runner=_failing(_fake_conda([]), creates))
        assert result["status"] == "failed" and result["previous_in_use"] is True
        assert len(creates) == launch


def test_a_kept_build_put_back_at_launch_is_logged_as_one_restore(tmp_path, monkeypatch):
    """D3c ruling (3): a switch back to a kept build is one `env.restored` line, not a build."""
    from tests.test_env_reconcile_progress import _Ledger
    from tests.test_env_tools import _reconcile_setup

    from auto_research import env as app_env
    from auto_research.graph import runtime
    from auto_research.sandbox import env_build
    from auto_research.sandbox.merge_flow import build_committed_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    first = (folder / "env.lock").read_text()
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    (folder / "env.lock").write_text(first.replace("h1_0", "h9_0"))
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    [kept] = (app_env.agent_conda_dir() / "envs").glob("ar-built.prev-*")
    (folder / "env.lock").write_text(first)  # the branch goes back to the first definition

    calls: list = []
    fake = _fake_conda(calls)

    class _Conda(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _Conda)
    mon = _Ledger()
    asyncio.run(runtime._reconcile_environments(toolkit, mon))
    names = [e["event"] for e in mon.events]
    assert "env.build.start" not in names and "env.build" not in names
    [restored] = mon.of("env.restored")
    assert restored["summary"] == (f"restored kept build {kept.name} as ar-built "
                                   "(built earlier from this definition)")
    assert not [argv for argv, _env in calls if "create" in argv]
    assert [e.superseded for e in app_env.env_catalogue({"toolkit_root": str(toolkit)})
            if e.owner == "pipeline"] == [False]


def test_list_env_shows_a_superseded_build(tmp_path):
    from auto_research.agents.react import execute_list_env, list_env_tool_def
    from auto_research.sandbox.envs import Environment

    current = Environment(name="ar-a", prefix=tmp_path / "a", owner="pipeline")
    earlier = Environment(name="ar-b", prefix=tmp_path / "b", owner="pipeline", superseded=True)

    class _Sandbox:
        environment = current
        environments = staticmethod(lambda: [current, earlier])

    rows = json.loads(execute_list_env(_Sandbox(), {}).text)["environments"]
    assert [row.get("superseded") for row in rows] == [None, True]
    assert "`superseded`" in list_env_tool_def()["description"]


def test_a_kept_build_put_back_at_commit_is_one_restore_and_the_lead_is_told(tmp_path, monkeypatch):
    """Review finding: the commit-time path logs one `env.restored` and the note says restored."""
    from types import SimpleNamespace

    from tests.test_env_reconcile_progress import _Ledger
    from tests.test_env_tools import _reconcile_setup

    from auto_research.sandbox import env_build
    from auto_research.sandbox.merge_flow import AuthoringSession, build_committed_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    first = (folder / "env.lock").read_text()
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    (folder / "env.lock").write_text(first.replace("h1_0", "h9_0"))
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    (folder / "env.lock").write_text(first)
    fake = _fake_conda([])

    class _Conda(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _Conda)
    mon = _Ledger()
    session = SimpleNamespace(_direct_branch="ai", worktree=toolkit, _monitor=mon, last_commit_sha="c0ffee")
    note = asyncio.run(AuthoringSession._build_environments(session, {"envs/built/env.lock": "modified"}))
    assert [e["event"] for e in mon.events] == ["env.restored"]
    assert mon.events[0]["summary"].startswith("restored kept build ar-built.prev-")
    assert note == " Environment restored from a kept build of its definition: ar-built."


def test_a_definition_that_no_longer_validates_keeps_the_earlier_build_said_and_flagged(tmp_path, monkeypatch):
    """Review finding: the launch event names the earlier build in use, as the catalogue marks it."""
    from tests.test_env_tools import _reconcile_setup

    from auto_research import env as app_env
    from auto_research.sandbox.merge_flow import build_committed_environments, reconcile_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    (folder / "env.lock").write_text("not a lock\n")
    [result] = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert result["status"] == "failed" and result["previous_in_use"] is True
    [listed] = [e for e in app_env.env_catalogue({"toolkit_root": str(toolkit)}) if e.owner == "pipeline"]
    assert listed.superseded is True


def test_a_build_whose_definition_is_gone_reads_as_superseded(tmp_path, monkeypatch):
    from tests.test_env_tools import _reconcile_setup

    from auto_research import env as app_env
    from auto_research.sandbox.merge_flow import build_committed_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    build_committed_environments(toolkit, ["built"], runner=_fake_conda([]))
    shutil.rmtree(folder)
    [listed] = [e for e in app_env.env_catalogue({"toolkit_root": str(toolkit)}) if e.owner == "pipeline"]
    assert listed.superseded is True
