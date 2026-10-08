"""Pipeline-owned environment definitions: validation and the runtime builder (env plan B3a).

Drives `sandbox.env_build` with a fake conda (the one thing outside the code under test): it
"creates" a prefix by writing the conda-meta records the lock names, the way conda does.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from auto_research.sandbox import env_build as B
from auto_research.sandbox.envs import Environment, External

_PKG = "https://conda.anaconda.org/conda-forge/linux-64/python-3.12.1-h1_0.conda#" + "a" * 32
_PKG2 = "https://conda.anaconda.org/conda-forge/noarch/pip-24.0-pyh_0.conda#" + "b" * 32


def _definition(root: Path, name="tiny", *, yaml="purpose: test env\nexternals: []\n",
                lock=("@EXPLICIT", _PKG, _PKG2), pip=None) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text(yaml)
    if lock is not None:
        (folder / "env.lock").write_text("# platform: linux-64\n" + "\n".join(lock) + "\n")
    if pip is not None:
        (folder / "env.pip").write_text(pip)
    return folder


_H = " --hash=sha256:" + "d" * 64


def _fake_conda(calls, *, pkgs_dirs=None, pip_installs=None):
    """Stands in for conda and pip at the process boundary, writing what each really writes:
    conda-meta records for a create, dist-info for a pip install, JSON for `config --show`."""
    def run(argv, env=None, **kw):
        calls.append((argv, env))
        if "--" in argv:  # a jail prefix wraps the real command
            argv = argv[argv.index("--") + 1:]
        if "config" in argv:
            shown = pkgs_dirs if pkgs_dirs is not None else [env["CONDA_PKGS_DIRS"]]
            kw["stdout"].write(json.dumps({"pkgs_dirs": shown}).encode())  # the build's output file
            return subprocess.CompletedProcess(argv, 0)
        if "create" in argv:
            prefix = Path(argv[argv.index("--prefix") + 1])
            lock = Path(argv[argv.index("--file") + 1])
            meta = prefix / "conda-meta"
            meta.mkdir(parents=True, exist_ok=True)
            (meta / "history").write_text("")
            for i, line in enumerate(ln for ln in lock.read_text().splitlines() if ln.startswith("https")):
                url, digest = line.split("#")
                key = ("sha256", digest[7:]) if digest.startswith("sha256:") else ("md5", digest)
                (meta / f"p{i}.json").write_text(json.dumps({"url": url, key[0]: key[1]}))
        if "pip" in argv:
            prefix = Path(argv[0]).parent.parent
            req = Path(argv[argv.index("-r") + 1])
            for line in req.read_text().split("\n"):
                if line.strip():
                    name, version = line.split(" ")[0].split("==")
                    version = (pip_installs or {}).get(name, version)
                    info = prefix / "lib/python3.12/site-packages" / f"{name}-{version}.dist-info"
                    info.mkdir(parents=True)
                    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
                    (info / "RECORD").write_text(f"{info.name}/METADATA,,\n{info.name}/RECORD,,\n")  # as pip writes it
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def _build(tmp_path, folder, calls, *, jailed=False, **kw):
    return B.build(B.read_definition(folder), conda_dir=tmp_path / "conda", conda_exe="conda",
                   base_envs={}, externals={}, jailed=jailed,
                   runner=_fake_conda(calls, **{k: kw.pop(k) for k in ("pkgs_dirs", "pip_installs") if k in kw}),
                   **kw)


def _prefix(tmp_path, folder):
    return tmp_path / "conda" / "envs" / f"ar-{folder.name}"


def test_a_good_definition_validates(tmp_path):
    assert B.validate(_definition(tmp_path), base_envs={}, externals={}) == []


@pytest.mark.parametrize("kwargs,needle", [
    ({"lock": ("@EXPLICIT", "https://repo.anaconda.com/pkgs/main/linux-64/x-1-0.conda#" + "a" * 32)}, "conda-forge"),
    ({"lock": ("@EXPLICIT", "https://conda.anaconda.org/conda-forge/linux-64/x-1-0.conda")}, "md5"),
    ({"lock": ("numpy=2.1",)}, "@EXPLICIT"),
    ({"yaml": "purpose: x\nexternals: [heasoft]\n"}, "not declared"),
    ({"yaml": "purpose: x\nchannels: [defaults]\n"}, "unknown keys"),
    ({"yaml": "externals: []\n"}, "purpose"),
    ({"pip": "requests\n"}, "sha256"),
    ({"pip": "requests==2.32.3\n"}, "sha256"),
    ({"pip": "--index-url https://x\n"}, "sha256"),
    ({"pip": "-e git+https://x/y#egg=y\n"}, "sha256"),
    ({"pip": "y @ https://x/y-1-py3-none-any.whl" + _H + "\n"}, "sha256"),
    ({"pip": "requests==2.32.3" + _H + " --no-binary=:all:\n"}, "sha256"),
    ({"lock": ("@EXPLICIT", _PKG.replace("linux-64", "osx-arm64"))}, "this machine is"),
    ({"lock": ("@EXPLICIT", _PKG, _PKG.replace("h1_0", "h2_0"))}, "repeated"),
    ({"yaml": "purpose: [a, b]\n"}, "one line of text"),
    ({"yaml": "purpose: x\nbase: 3\n"}, "human environment's name"),
    ({"yaml": "purpose: x\nexternals: heasoft\n"}, "list of declared names"),
    ({"yaml": "purpose: x\n1: a\nb: c\n"}, "unknown keys"),
    ({"yaml": "purpose: x\nbase: astropy\n", "lock": None, "pip": "numpy==2.1.0" + _H + "\n"}, "installs nothing"),
    ({"lock": None}, "env.lock"),
    ({"yaml": "purpose: x\nbase: astropy\n"}, "either a base"),
])
def test_a_bad_definition_is_refused_with_the_reason(tmp_path, kwargs, needle):
    problems = B.validate(_definition(tmp_path, **kwargs), base_envs={}, externals={})
    assert any(needle in p for p in problems), problems


def test_a_built_environment_is_created_from_its_lock_with_the_safe_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://elsewhere")
    monkeypatch.setenv("CONDA_PKGS_DIRS", "/human/pkgs")
    calls = []
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, calls)
    assert env.name == "ar-tiny" and env.owner == "pipeline" and env.prefix == _prefix(tmp_path, folder)
    (show, _), (create, create_env), (pip, pip_env) = calls
    assert show[1:] == ["config", "--show", "pkgs_dirs", "--json"]
    assert create[1:3] == ["create", "--yes"]
    staged_lock = Path(create[create.index("--file") + 1])
    assert staged_lock != folder / "env.lock" and not staged_lock.exists()  # a private copy, removed after
    work = Path(create_env["CONDA_PKGS_DIRS"]).parent
    assert create_env["HOME"] == str(work / "home") and not work.exists()  # per-build cache + home
    assert create_env["CONDA_ALWAYS_COPY"] == "true" and create_env["CONDA_REGISTER_ENVS"] == "false"
    assert create_env["CONDA_SAFETY_CHECKS"] == "enabled" and create_env["CONDA_EXTRA_SAFETY_CHECKS"] == "true"
    for e in (create_env, pip_env):
        assert "ANTHROPIC_API_KEY" not in e and "PIP_INDEX_URL" not in e
    assert pip[1:4] == ["-m", "pip", "install"]
    for flag in ("--isolated", "--no-deps", "--require-hashes", "--only-binary=:all:"):
        assert flag in pip
    record = B.build_record(tmp_path / "conda", env.prefix)
    assert record == {"definition_sha256": B.read_definition(folder).digest(), "jailed": False,
                      "name": "ar-tiny", "activate": None, "boot": None, "externals": []}

    calls.clear()  # a recorded prefix is verified, not rebuilt
    _build(tmp_path, folder, calls)
    assert calls == []


def _conda_install(root: Path) -> Path:
    (root / "conda-meta").mkdir(parents=True)
    (root / "conda-meta" / "history").write_text("")
    (root / "bin").mkdir()
    return root / "bin" / "conda"


def _binds(argv: list[str]) -> dict[str, str]:
    """``{destination: mode}`` for every bind in a real bwrap argv."""
    out, i = {}, 0
    while i < len(argv):
        if argv[i] in ("--bind", "--ro-bind"):
            out[argv[i + 2]] = argv[i]
            i += 3
        else:
            i += 1
    return out


def test_a_jailed_build_wraps_every_command_and_writes_only_its_own_folders(tmp_path, monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")  # the real argv, whatever the host
    conda_exe = _conda_install(tmp_path / "opt" / "conda")
    calls = []
    folder = _definition(tmp_path / "defs")
    env = B.build(B.read_definition(folder), conda_dir=tmp_path / "conda", conda_exe=str(conda_exe),
                  base_envs={}, externals={}, jailed=True, runner=_fake_conda(calls))
    assert calls and all(argv[0] == "/usr/bin/bwrap" and "--" in argv for argv, _ in calls)
    jail_part = calls[1][0][:calls[1][0].index("--")]
    binds = _binds(jail_part)
    writable = {path for path, mode in binds.items() if mode == "--bind"}
    work = Path(calls[1][1]["CONDA_PKGS_DIRS"]).parent
    assert writable == {str(env.prefix), str(work / "pkgs"), str(work / "home"), "/tmp", jail.JAIL_HOME_AT}
    assert binds[str(tmp_path / "opt" / "conda")] == "--ro-bind"
    assert str(folder) not in binds and "--unshare-net" not in jail_part  # the staged copy; network on
    for private in ("built", ".install.lock", "envs"):
        assert str(tmp_path / "conda" / private) not in binds
    assert B.build_record(tmp_path / "conda", env.prefix)["jailed"] is True


def test_a_jailed_caller_never_reuses_a_prefix_built_unjailed(tmp_path, monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    folder = _definition(tmp_path / "defs")
    _build(tmp_path, folder, [])  # unjailed
    calls = []
    env = B.build(B.read_definition(folder), conda_dir=tmp_path / "conda",
                  conda_exe=str(_conda_install(tmp_path / "opt" / "conda")),
                  base_envs={}, externals={}, jailed=True, runner=_fake_conda(calls))
    assert any("create" in argv for argv, _ in calls)
    assert B.build_record(tmp_path / "conda", env.prefix)["jailed"] is True
    calls.clear()
    _build(tmp_path, folder, calls)  # an unjailed caller may reuse a jailed prefix
    assert calls == []


def test_a_jailed_build_without_bwrap_is_refused(tmp_path, monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: None)
    with pytest.raises(RuntimeError, match="needs bwrap"):
        B.build(B.read_definition(_definition(tmp_path / "defs")), conda_dir=tmp_path / "conda",
                conda_exe=str(_conda_install(tmp_path / "opt" / "conda")), base_envs={}, externals={}, jailed=True,
                runner=lambda *a, **k: pytest.fail("nothing runs unjailed"))


def test_a_conda_root_that_is_not_an_installation_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="not inside a conda installation"):
        B.conda_root(str(tmp_path / "bin" / "conda"))


def test_an_interrupted_build_is_rebuilt_not_reused(tmp_path):
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    with pytest.raises(B.BuildMismatch, match="pip pins"):  # pip "installed" the wrong version
        _build(tmp_path, folder, [], pip_installs={"tabulate": "0.8.0"})
    prefix = _prefix(tmp_path, folder)
    assert (prefix / "conda-meta" / "history").is_file() and B.build_record(tmp_path / "conda", prefix) is None
    calls = []
    env = _build(tmp_path, folder, calls)
    assert any("create" in argv for argv, _ in calls) and B.build_record(tmp_path / "conda", env.prefix)


def test_a_definition_changed_after_review_is_validated_again_at_build(tmp_path):
    folder = _definition(tmp_path / "defs")
    assert B.validate(folder, base_envs={}, externals={}) == []
    (folder / "env.lock").write_text("@EXPLICIT\nfile:///etc/evil.conda#" + "a" * 32 + "\n")
    calls = []
    with pytest.raises(ValueError, match="not valid"):
        _build(tmp_path, folder, calls)
    assert calls == []


@pytest.mark.parametrize("plant", ["symlink", "hardlink", "stray"])
def test_a_definition_with_links_or_stray_files_is_refused_without_reading_them(tmp_path, plant):
    secret = tmp_path / "secret.env"
    secret.write_text("ANTHROPIC_API_KEY=sk-ant-FAKE123\n")
    folder = _definition(tmp_path / "defs")
    if plant == "symlink":
        (folder / "env.pip").symlink_to(secret)
    elif plant == "hardlink":
        os.link(secret, folder / "env.pip")
    else:
        (folder / "notes.txt").write_text("x")
    problems = B.validate(folder, base_envs={}, externals={})
    assert problems and not any("FAKE123" in p for p in problems)
    with pytest.raises(ValueError, match="not valid"):
        _build(tmp_path, folder, [])


def test_a_yaml_error_is_reported_without_the_line(tmp_path):
    folder = _definition(tmp_path / "defs", yaml="purpose: x\nSECRET: [unclosed SECRETTAIL\n")
    problems = B.validate(folder, base_envs={}, externals={})
    assert any("does not parse at line" in p for p in problems) and not any("SECRETTAIL" in p for p in problems)


def test_a_fifo_in_a_definition_is_refused_without_blocking(tmp_path):
    folder = _definition(tmp_path / "defs", lock=None)
    os.mkfifo(folder / "env.lock")
    assert any("plain file" in p for p in B.validate(folder, base_envs={}, externals={}))
    with pytest.raises(ValueError, match="not valid"):
        _build(tmp_path, folder, [])
    assert list((tmp_path / "conda" / "staging").iterdir()) == []  # staging cleaned on refusal


def test_a_read_only_leftover_prefix_is_removed_and_rebuilt(tmp_path):
    folder = _definition(tmp_path / "defs")
    prefix = _prefix(tmp_path, folder)
    stuck = prefix / "pkg" / "sub"
    stuck.mkdir(parents=True)
    (stuck / "f").write_text("x")
    os.chmod(prefix / "pkg", 0o500)
    env = _build(tmp_path, folder, [])
    assert not (env.prefix / "pkg").exists() and B.build_record(tmp_path / "conda", env.prefix)


@pytest.mark.parametrize("where", ["prefix", "work"])
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o300])
def test_untraversable_leftovers_are_removed(tmp_path, where, mode):
    folder = _definition(tmp_path / "defs")
    if where == "prefix":
        stuck = _prefix(tmp_path, folder) / "pkg"
    else:  # a build script locks a folder in its own cache: cleanup must still finish
        stuck = None
    calls = []
    runner = _fake_conda(calls)

    def locking(argv, env=None, **kw):
        result = runner(argv, env=env, **kw)
        if "create" in argv and stuck is None:
            locked = Path(env["CONDA_PKGS_DIRS"]) / "x"
            (locked / "y").mkdir(parents=True)
            os.chmod(locked, mode)
        return result

    if stuck is not None:
        (stuck / "sub").mkdir(parents=True)
        os.chmod(stuck, mode)
    env = B.build(B.read_definition(folder), conda_dir=tmp_path / "conda", conda_exe="conda",
                  base_envs={}, externals={}, jailed=False, runner=locking)
    assert B.build_record(tmp_path / "conda", env.prefix)
    assert list((tmp_path / "conda" / "staging").iterdir()) == []


def test_after_an_unjailed_build_no_jailed_caller_reuses_anything(tmp_path, monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    conda_exe = str(_conda_install(tmp_path / "opt" / "conda"))
    one, two = _definition(tmp_path / "a", name="one"), _definition(tmp_path / "b", name="two")
    B.build(B.read_definition(one), conda_dir=tmp_path / "conda", conda_exe=conda_exe,
            base_envs={}, externals={}, jailed=True, runner=_fake_conda([]))
    _build(tmp_path, two, [])  # unjailed: its scripts could have rewritten one's record
    calls = []
    B.build(B.read_definition(one), conda_dir=tmp_path / "conda", conda_exe=conda_exe,
            base_envs={}, externals={}, jailed=True, runner=_fake_conda(calls))
    assert any("create" in argv for argv, _ in calls)


def test_a_derived_environment_comes_from_one_staged_read(tmp_path):
    base = Environment(name="astropy", prefix=tmp_path / "astropy")
    folder = _definition(tmp_path / "defs", name="xspec", lock=None, yaml="purpose: x\nbase: astropy\n")
    stale = B.read_definition(folder)
    (folder / "env.yaml").write_text("purpose: x\nbase: other\n")
    other = Environment(name="other", prefix=tmp_path / "other")
    env = B.build(stale, conda_dir=tmp_path / "conda", conda_exe="conda",
                  base_envs={"astropy": base, "other": other}, externals={}, jailed=True,
                  runner=lambda *a, **k: pytest.fail("a derived env runs no conda"))
    assert env.prefix == other.prefix  # what was validated is what is returned


def test_verify_reads_the_definition_through_a_staged_copy(tmp_path):
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    secret = tmp_path / "secret"
    secret.write_text("@EXPLICIT\nsk-ant-FAKESECRET\n")
    (folder / "env.lock").unlink()
    (folder / "env.lock").symlink_to(secret)
    with pytest.raises(B.BuildMismatch) as caught:
        B.verify(B.read_definition(folder), env.prefix)
    assert "FAKESECRET" not in str(caught.value)


def test_verify_does_not_block_on_a_fifo_in_a_dist_info(tmp_path):
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, [])
    metadata = env.prefix / "lib/python3.12/site-packages/tabulate-0.9.0.dist-info/METADATA"
    metadata.unlink()
    os.mkfifo(metadata)
    with pytest.raises(B.BuildMismatch, match="unreadable"):
        B.verify(B.read_definition(folder), env.prefix)


def test_a_definition_file_over_the_size_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_MAX_FILE", 64)
    folder = _definition(tmp_path / "defs")
    assert any("under 64 bytes" in p for p in B.validate(folder, base_envs={}, externals={}))


def test_a_rebuild_drops_the_old_record_before_touching_the_prefix(tmp_path, monkeypatch):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    conda_exe = str(_conda_install(tmp_path / "opt" / "conda"))
    one, two = _definition(tmp_path / "a", name="one"), _definition(tmp_path / "b", name="two")
    B.build(B.read_definition(one), conda_dir=tmp_path / "conda", conda_exe=conda_exe,
            base_envs={}, externals={}, jailed=True, runner=_fake_conda([]))
    _build(tmp_path, two, [])  # taints: the next jailed call rebuilds one
    inner = _fake_conda([])

    def killed(argv, env=None, **kw):
        if "create" in argv:
            inner(argv, env=env, **kw)  # conda wrote its records, then the process died
            raise KeyboardInterrupt
        return inner(argv, env=env, **kw)

    with pytest.raises(KeyboardInterrupt):
        B.build(B.read_definition(one), conda_dir=tmp_path / "conda", conda_exe=conda_exe,
                base_envs={}, externals={}, jailed=True, runner=killed)
    assert B.build_record(tmp_path / "conda", _prefix(tmp_path, one)) is None
    calls = []
    _build(tmp_path, one, calls)  # never reused: rebuilt
    assert any("create" in argv for argv, _ in calls)


def test_a_yaml_alias_bomb_is_refused_before_it_expands(tmp_path):
    levels = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    levels += [f"a{i}: &a{i} [" + ", ".join([f"*a{i-1}"] * 9) + "]" for i in range(1, 12)]
    folder = _definition(tmp_path / "defs", yaml="purpose: x\n" + "\n".join(levels) + "\n")
    problems = B.validate(folder, base_envs={}, externals={})
    assert any("anchors and aliases" in p for p in problems)
    with pytest.raises(ValueError):
        B.read_definition(folder)


def test_read_definition_refuses_a_linked_env_yaml_without_quoting_it(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("TOKEN: sk-ant-FAKESECRET123: rest\n")
    folder = _definition(tmp_path / "defs")
    (folder / "env.yaml").unlink()
    (folder / "env.yaml").symlink_to(secret)
    with pytest.raises(ValueError) as caught:
        B.read_definition(folder)
    assert "FAKESECRET" not in str(caught.value)


def test_activation_is_a_runtime_copy_of_the_reviewed_bytes(tmp_path):
    folder = _definition(tmp_path / "defs")
    (folder / "activate.sh").write_text("export A=1\n")
    env = _build(tmp_path, folder, [])
    (folder / "activate.sh").write_text("export A=2  # edited after review\n")
    assert env.activate.read_text() == "export A=1\n" and not os.access(env.activate, os.W_OK)


@pytest.mark.parametrize("name", ["tmp", "home", "pkgs", "def"])
def test_a_definition_named_like_a_build_folder_stays_read_only_in_the_jail(tmp_path, monkeypatch, name):
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
    calls = []
    folder = _definition(tmp_path / "defs", name=name)
    B.build(B.read_definition(folder), conda_dir=tmp_path / "conda",
            conda_exe=str(_conda_install(tmp_path / "opt" / "conda")),
            base_envs={}, externals={}, jailed=True, runner=_fake_conda(calls))
    create = next(argv for argv, _ in calls if "create" in argv)
    staged = Path(create[create.index("--file") + 1]).parent
    jail_part = create[:create.index("--")]
    sources = [Path(jail_part[i + 1]) for i, flag in enumerate(jail_part) if flag == "--bind"]
    assert sources and not any(staged.is_relative_to(source) for source in sources)  # incl. /tmp's


def test_build_output_goes_to_a_file_not_memory(tmp_path):
    folder = _definition(tmp_path / "defs")
    inner = _fake_conda([])

    def loud(argv, env=None, **kw):
        if "create" in argv:
            kw["stdout"].write(b"x" * (8 * 1024 * 1024) + b"TAIL-OF-OUTPUT")
            return subprocess.CompletedProcess(argv, 1)
        return inner(argv, env=env, **kw)

    with pytest.raises(RuntimeError) as caught:
        B.build(B.read_definition(folder), conda_dir=tmp_path / "conda", conda_exe="conda",
                base_envs={}, externals={}, jailed=False, runner=loud)
    assert str(caught.value).endswith("TAIL-OF-OUTPUT") and len(str(caught.value)) < 1000


def test_a_planted_activation_copy_is_replaced(tmp_path):
    folder = _definition(tmp_path / "defs")
    (folder / "activate.sh").write_text("export A=1\n")
    env = _build(tmp_path, folder, [])
    copy = env.activate
    copy.unlink()
    other = tmp_path / "other"
    other.write_text("export A=1\n")
    os.link(other, copy)  # right bytes, but someone else holds a writable link
    calls = []
    env = _build(tmp_path, folder, calls)
    assert env.activate.stat().st_nlink == 1 and not os.access(env.activate, os.W_OK)


@pytest.mark.parametrize("text", ["[" * 5000, "purpose: !!python/object:os.system " + "x" * 200000,
                                  "purpose: " + "9" * 100000])
def test_hostile_yaml_is_a_short_refusal(tmp_path, text):
    folder = _definition(tmp_path / "defs", yaml=text + "\n")
    problems = B.validate(folder, base_envs={}, externals={})
    assert problems and all(len(p) < 300 for p in problems)


def test_verify_stays_linear_on_a_deep_path_in_a_record(tmp_path):
    import time

    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    record = env.prefix / "conda-meta" / "p0.json"
    meta = json.loads(record.read_text())
    meta["files"] = ["lib/python3.12/site-packages/" + "a/" * 100000 + "x"]
    record.write_text(json.dumps(meta))
    started = time.monotonic()
    B.verify(B.read_definition(folder), env.prefix)
    assert time.monotonic() - started < 5


def test_several_installs_of_one_pin_are_refused_before_any_is_read(tmp_path):
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, [])
    decoy = env.prefix / "lib/python3.12/site-packages/tabulate-9.0.dist-info"
    decoy.mkdir()
    os.mkfifo(decoy / "METADATA")  # reading it would block
    with pytest.raises(B.BuildMismatch, match="more than one install"):
        B.verify(B.read_definition(folder), env.prefix)


def test_a_bracket_heavy_env_yaml_is_refused_without_parsing(tmp_path):
    import time

    folder = _definition(tmp_path / "defs", yaml="purpose: " + "[" * 15000 + "\n")
    started = time.monotonic()
    problems = B.validate(folder, base_envs={}, externals={})
    assert any("too many brackets" in p for p in problems) and time.monotonic() - started < 2
    big = _definition(tmp_path / "big", name="big", yaml="purpose: " + "x" * 20000 + "\n")
    assert any("under" in p for p in B.validate(big, base_envs={}, externals={}))


def test_odd_inputs_are_refusals_not_crashes(tmp_path):
    assert B.validate(tmp_path / "defs" / "..", base_envs={}, externals={})
    folder = _definition(tmp_path / "defs")
    with pytest.raises(RuntimeError, match="package caches"):
        _build(tmp_path, folder, [], pkgs_dirs=5)
    conda = tmp_path / "conda"
    os.mkfifo(conda / "built" / "planted.json")
    assert B.build_record(conda, Path("planted")) is None  # no hang


def test_verify_refuses_many_python_folders_and_huge_listings(tmp_path, monkeypatch):
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, [])
    for i in range(5):
        (env.prefix / "lib" / f"python9.{i}").mkdir(parents=True)
    with pytest.raises(B.BuildMismatch, match="more than 4 lib/python"):
        B.verify(B.read_definition(folder), env.prefix)
    for i in range(5):
        (env.prefix / "lib" / f"python9.{i}").rmdir()
    monkeypatch.setattr(B, "_MAX_ENTRIES", 10)
    site = env.prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        (site / f"f{i}").write_text("")
    with pytest.raises(B.BuildMismatch, match="more than 10 entries"):
        B.verify(B.read_definition(folder), env.prefix)


def test_problem_lists_are_capped(tmp_path):
    lock = _definition(tmp_path / "a", name="a", lock=("@EXPLICIT", *(["a"] * 5000)))
    problems = B.validate(lock, base_envs={}, externals={})
    assert len(problems) <= 21 and "more problems" in problems[-1]
    pip = _definition(tmp_path / "b", name="b", pip="a\n" * 5000)
    assert len(B.validate(pip, base_envs={}, externals={})) <= 21
    keys = _definition(tmp_path / "c", name="c", yaml="purpose: x\n" + "".join(f"k{i}{'y' * 60}: 1\n" for i in range(50)))
    assert all(len(p) < 600 for p in B.validate(keys, base_envs={}, externals={}))
    many = _definition(tmp_path / "d", name="d")
    for i in range(200):
        (many / f"stray{i}").write_text("")
    assert len(B.validate(many, base_envs={}, externals={})) == 1


def test_verify_refuses_a_malformed_pip_line_instead_of_crashing(tmp_path):
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    (folder / "env.pip").write_text("requests\n")
    with pytest.raises(B.BuildMismatch, match="not valid"):
        B.verify(B.read_definition(folder), env.prefix)


@pytest.mark.parametrize("planted", ["pyvenv.cfg", "bin/pyvenv.cfg"])
def test_a_pyvenv_cfg_is_a_start_up_entry(tmp_path, planted):
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    (env.prefix / planted).parent.mkdir(parents=True, exist_ok=True)
    (env.prefix / planted).write_text("home = /elsewhere/bin\n")
    with pytest.raises(B.BuildMismatch, match="standard library"):
        B.verify(B.read_definition(folder), env.prefix)


def test_an_unlistable_folder_is_a_mismatch_not_a_crash(tmp_path):
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, [])
    site = env.prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    os.chmod(site, 0)
    try:
        with pytest.raises(B.BuildMismatch, match="cannot be listed"):
            B.verify(B.read_definition(folder), env.prefix)
    finally:
        os.chmod(site, 0o755)


def test_decoy_installs_of_a_pin_are_refused_at_the_second(tmp_path, monkeypatch):
    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, [])
    site = env.prefix / "lib/python3.12/site-packages"
    for i in range(2000):
        (site / f"tabulate-{i}.dist-info").mkdir()
    with pytest.raises(B.BuildMismatch, match="more than one install"):
        B.verify(B.read_definition(folder), env.prefix)


@pytest.mark.parametrize("linked", ["lib", "lib/python3.12/site-packages"])
def test_a_linked_lib_or_site_packages_is_refused(tmp_path, linked):
    import shutil

    folder = _definition(tmp_path / "defs", pip="tabulate==0.9.0" + _H + "\n")
    env = _build(tmp_path, folder, [])
    outside = tmp_path / "outside"
    shutil.copytree(env.prefix / linked, outside)
    shutil.rmtree(env.prefix / linked)
    (env.prefix / linked).symlink_to(outside)
    with pytest.raises(B.BuildMismatch, match="is a link"):
        B.verify(B.read_definition(folder), env.prefix)


def test_a_pth_launcher_file_is_refused(tmp_path):
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    (env.prefix / "bin").mkdir(exist_ok=True)
    (env.prefix / "bin" / "python3.12._pth").write_text("/elsewhere\n")
    with pytest.raises(B.BuildMismatch, match="standard library"):
        B.verify(B.read_definition(folder), env.prefix)


def test_a_conda_dir_folder_that_is_a_link_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "conda").mkdir()
    (tmp_path / "conda" / "envs").symlink_to(elsewhere)
    with pytest.raises(RuntimeError, match="not a plain folder"):
        _build(tmp_path, _definition(tmp_path / "defs"), [])


def test_a_rejected_line_is_quoted_short(tmp_path):
    folder = _definition(tmp_path / "defs", pip="x" * 30 + "=" + "SECRETTAIL" * 10 + "\n")
    problems = B.validate(folder, base_envs={}, externals={})
    assert problems and not any("SECRETTAIL" * 3 in p for p in problems)


def test_a_lock_with_non_ascii_or_odd_line_breaks_is_refused(tmp_path):
    folder = _definition(tmp_path / "defs", lock=("@EXPLICIT", _PKG + "\x0c" + _PKG2))
    assert any("ASCII" in p for p in B.validate(folder, base_envs={}, externals={}))


def test_planted_links_at_the_runtime_s_own_files_are_not_followed(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep")
    conda = tmp_path / "conda"
    (conda / "built").mkdir(parents=True)
    (conda / ".install.lock").symlink_to(victim)
    with pytest.raises(OSError):
        _build(tmp_path, _definition(tmp_path / "defs"), [])
    (conda / ".install.lock").unlink()
    folder = _definition(tmp_path / "defs2", name="tiny")
    (conda / "built" / f"{_prefix(tmp_path, folder).name}.json").symlink_to(victim)
    _build(tmp_path, folder, [])
    assert victim.read_text() == "keep"


def test_a_condarc_package_cache_refuses_the_build(tmp_path):
    folder = _definition(tmp_path / "defs")
    calls = []
    with pytest.raises(RuntimeError, match="package caches"):
        _build(tmp_path, folder, calls, pkgs_dirs=["/somewhere/pkgs"])
    assert not any("create" in argv for argv, _ in calls)


def test_a_sha256_lock_line_verifies(tmp_path):
    sha = "https://conda.anaconda.org/conda-forge/noarch/pip-24.0-pyh_0.conda#sha256:" + "e" * 64
    folder = _definition(tmp_path / "defs", lock=("@EXPLICIT", _PKG, sha))
    assert B.validate(folder, base_envs={}, externals={}) == []
    env = _build(tmp_path, folder, [])
    (env.prefix / "conda-meta" / "p1.json").write_text(json.dumps({"url": sha.split("#")[0], "sha256": "f" * 64}))
    with pytest.raises(B.BuildMismatch, match="missing"):
        B.verify(B.read_definition(folder), env.prefix)


def test_verify_compares_pin_versions_as_versions(tmp_path):
    folder = _definition(tmp_path / "defs", pip="Tab_ulate==1.0" + _H + "\n")
    env = _build(tmp_path, folder, [], pip_installs={"Tab_ulate": "1.0.0"})  # 1.0 == 1.0.0
    B.verify(B.read_definition(folder), env.prefix)


def test_a_start_up_file_outside_the_records_is_not_looked_for(tmp_path):
    """Ruling 2026-10-06: a file a build drops outside its records escapes the fingerprint, an
    accepted residual risk; verify checks records and pins only."""
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    site = env.prefix / "lib/python3.12/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "stray.pth").write_text("import os\n")
    B.verify(B.read_definition(folder), env.prefix)


@pytest.mark.parametrize("record", ["not json", "[1, 2]", "link"])
def test_verify_refuses_unreadable_conda_records(tmp_path, record):
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    planted = env.prefix / "conda-meta" / "p1.json"  # replaces a real record: the count still matches
    planted.unlink()
    if record == "link":
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"url": "SECRETURL"}))
        planted.symlink_to(outside)
    else:
        planted.write_text(record)
    with pytest.raises(B.BuildMismatch, match="unreadable conda records") as caught:
        B.verify(B.read_definition(folder), env.prefix)
    assert "SECRETURL" not in str(caught.value)


def test_a_prefix_that_does_not_match_its_lock_is_refused(tmp_path):
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    (env.prefix / "conda-meta" / "p1.json").write_text(json.dumps({"url": "https://x/y.conda", "md5": "c" * 32}))
    with pytest.raises(B.BuildMismatch, match="unexpected"):
        B.verify(B.read_definition(folder), env.prefix)
    (env.prefix / "conda-meta" / "extra.json").write_text("{}")  # one record too many: refused before any read
    with pytest.raises(B.BuildMismatch, match="do not number the lock's 2 packages"):
        B.verify(B.read_definition(folder), env.prefix)


def test_a_derived_environment_layers_on_a_human_base_read_only(tmp_path):
    base = Environment(name="astropy", prefix=tmp_path / "astropy", activate=tmp_path / "base.sh")
    folder = _definition(tmp_path / "defs", name="xspec", lock=None,
                         yaml="purpose: x\nbase: astropy\nexternals: [heasoft]\n")
    (folder / "activate.sh").write_text("export X=1\n")
    heasoft = External("heasoft", tmp_path / "heasoft", ("build.txt",))
    assert B.validate(folder, base_envs={"astropy": base}, externals={"heasoft": heasoft}) == []
    env = B.build(B.read_definition(folder), conda_dir=tmp_path / "conda", conda_exe="conda",
                  base_envs={"astropy": base}, externals={"heasoft": heasoft}, jailed=True,
                  runner=lambda *a, **k: pytest.fail("a derived env runs no conda"))
    assert env.prefix == base.prefix and env.activate.read_bytes() == (folder / "activate.sh").read_bytes()
    assert env.activate.is_relative_to(tmp_path / "conda" / "activation")  # a runtime copy, not the toolkit file
    assert env.externals == (heasoft,) and env.name == "ar-xspec"


@pytest.mark.skipif(not os.environ.get("AR_LIVE_ENV_BUILD"), reason="live: set AR_LIVE_ENV_BUILD=1 (network, ~1 GB)")
def test_live_a_lock_exported_by_conda_rebuilds_exactly(tmp_path):
    """Real conda: solve a tiny env, export its explicit lock, rebuild from the lock, verify."""
    conda = os.environ["CONDA_EXE"]
    home = tmp_path / "seed-home"
    home.mkdir()
    # The seed, too, stays out of the user's ~/.conda: private home, no registration.
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CONDA_PKGS_DIRS": str(tmp_path / "seed-pkgs"),
           "CONDA_ALWAYS_COPY": "true", "CONDA_REGISTER_ENVS": "false"}
    subprocess.run([conda, "create", "--yes", "--quiet", "--override-channels", "-c", "conda-forge",
                    "-p", str(tmp_path / "seed"), "python=3.12"], env=env, check=True, capture_output=True)
    lock = subprocess.run([conda, "list", "--explicit", "--md5", "-p", str(tmp_path / "seed")],
                          env=env, check=True, capture_output=True, text=True).stdout
    folder = _definition(tmp_path / "defs", lock=None)
    (folder / "env.lock").write_text(lock)
    assert B.validate(folder, base_envs={}, externals={}) == []
    registry = Path.home() / ".conda" / "environments.txt"
    before = registry.read_text() if registry.is_file() else ""
    built = B.build(B.read_definition(folder), conda_dir=tmp_path / "conda", conda_exe=conda,
                    base_envs={}, externals={}, jailed=True)
    assert built.python.exists() and B.build_record(tmp_path / "conda", built.prefix)["jailed"] is True
    assert (registry.read_text() if registry.is_file() else "") == before


def test_a_rebuild_keeps_the_name_and_moves_the_previous_build_aside(tmp_path):
    folder = _definition(tmp_path / "defs")
    first = _build(tmp_path, folder, [])
    old_sha = B.read_definition(folder).digest()
    assert first.prefix.name == "ar-tiny"
    (folder / "env.lock").write_text("# platform: linux-64\n" + "\n".join(("@EXPLICIT", _PKG)) + "\n")
    second = _build(tmp_path, folder, [])
    envs = tmp_path / "conda" / "envs"
    assert second.prefix == first.prefix and B.build_record(tmp_path / "conda", second.prefix)
    assert len(list(envs.glob(f"ar-tiny.prev-{old_sha[:8]}-*"))) == 1
    (folder / "env.lock").write_text("# platform: linux-64\n" + "\n".join(("@EXPLICIT", _PKG2)) + "\n")
    _build(tmp_path, folder, [])
    assert len(list(envs.glob("ar-tiny.prev-*"))) == 1  # only the latest previous build is kept


def test_a_crash_while_clearing_an_older_build_keeps_the_current_one(tmp_path, monkeypatch):
    folder = _definition(tmp_path / "defs")
    _build(tmp_path, folder, [])
    (folder / "env.lock").write_text("# platform: linux-64\n" + "\n".join(("@EXPLICIT", _PKG)) + "\n")
    _build(tmp_path, folder, [])  # one .prev now exists
    (folder / "env.lock").write_text("# platform: linux-64\n" + "\n".join(("@EXPLICIT", _PKG2)) + "\n")
    removed = []
    real = B._remove_tree

    def crash(path, **kw):
        if ".prev-" in str(path):
            removed.append(path)
            raise KeyboardInterrupt  # killed while deleting the older build
        return real(path, **kw)

    monkeypatch.setattr(B, "_remove_tree", crash)
    with pytest.raises(KeyboardInterrupt):
        _build(tmp_path, folder, [])
    envs = tmp_path / "conda" / "envs"
    # the finished build was moved aside intact before anything was deleted
    assert removed and len(list(envs.glob("ar-tiny.prev-*"))) == 2
    assert all((p / "conda-meta").is_dir() for p in envs.glob("ar-tiny.prev-*"))



def test_an_activation_change_alone_reaches_the_record_and_the_catalogue(tmp_path):
    """The prefix is keyed on lock + pins; a reviewed activate.sh fix reuses it but the record
    (what the catalogue and use_env read) carries the new activation."""
    folder = _definition(tmp_path / "defs")
    (folder / "activate.sh").write_text("export A=1\n")
    calls: list = []
    first = _build(tmp_path, folder, calls)
    (folder / "activate.sh").write_text("export A=2\n")
    creates = len(calls)
    second = _build(tmp_path, folder, calls)
    assert len(calls) == creates, "an activation change alone rebuilds nothing"
    assert second.activate != first.activate and second.activate.read_text() == "export A=2\n"
    listed = {env.name: env for env in B.built_environments(tmp_path / "conda", {})}
    assert listed["ar-tiny"].activate == second.activate


def test_forget_drops_a_deleted_definition_from_the_catalogue(tmp_path):
    folder = _definition(tmp_path / "defs")
    _build(tmp_path, folder, [])
    assert [e.name for e in B.built_environments(tmp_path / "conda", {})] == ["ar-tiny"]
    assert B.forget(tmp_path / "conda", "tiny") is True
    assert B.built_environments(tmp_path / "conda", {}) == []
    assert B.forget(tmp_path / "conda", "tiny") is False


def test_a_stopped_runner_kills_the_command_it_runs(tmp_path):
    import threading
    import time

    runner = B.StoppableRunner()
    outcome = {}

    def run():
        try:
            runner(["/bin/sh", "-c", "sleep 30"], timeout=60)
        except RuntimeError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=run)
    started = time.monotonic()
    thread.start()
    time.sleep(0.3)
    runner.stop()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - started < 10
    assert "stopped" in outcome["error"]
    with pytest.raises(RuntimeError):
        runner(["/bin/true"])
    ok = B.StoppableRunner()(["/bin/sh", "-c", "echo hi"], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout == "hi\n"


def test_a_runner_timeout_returns_at_once_even_when_a_child_holds_the_pipes(tmp_path):
    import subprocess
    import time

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        B.StoppableRunner()(["/bin/sh", "-c", "setsid sleep 4 & sleep 4"], timeout=0.5, capture_output=True)
    assert time.monotonic() - started < 3


def _replay_lock(tmp_path, lines, *, platform=None):
    from auto_research.sandbox import envs as E

    prefix = tmp_path / "recorded"
    meta = prefix / "conda-meta"
    meta.mkdir(parents=True)
    for i, line in enumerate(lines):
        url, md5 = line.split("#")
        (meta / f"p{i}.json").write_text(json.dumps({"url": url, "md5": md5}))
    lock = E.lock(E.Environment(name="astropy", prefix=prefix, owner="human"))
    if platform:
        lock["conda_explicit"] = lock["conda_explicit"].replace(B.host_subdir(), platform)
    return lock


def test_replay_rebuilds_a_recorded_state_into_its_own_prefix_and_matches_it(tmp_path):
    defaults = "https://repo.anaconda.com/pkgs/main/linux-64/zlib-1.3-h0_0.conda#" + "e" * 32
    lock = _replay_lock(tmp_path, [_PKG, defaults])
    calls: list = []
    report = B.replay(lock, conda_dir=tmp_path / "conda", conda_exe="conda", jailed=False, runner=_fake_conda(calls))
    assert report["conda_match"] is True and report["missing"] == report["extra"] == []
    assert Path(report["prefix"]) == tmp_path / "conda" / "replay" / lock["fingerprint"][:16]
    creates = [c for c in calls if "create" in c[0]]
    again = B.replay(lock, conda_dir=tmp_path / "conda", conda_exe="conda", jailed=False, runner=_fake_conda(calls))
    assert again["conda_match"] and len([c for c in calls if "create" in c[0]]) == len(creates), "reused"


def test_replay_refuses_a_foreign_platform_or_an_unknown_host(tmp_path):
    with pytest.raises(ValueError, match="this machine is"):
        B.replay(_replay_lock(tmp_path / "a", [_PKG], platform="osx-arm64"), conda_dir=tmp_path / "c",
                 conda_exe="conda", jailed=False, runner=_fake_conda([]))
    other = "https://example.org/pkgs/linux-64/x-1-0.conda#" + "a" * 32
    with pytest.raises(ValueError, match="cannot install"):
        B.replay(_replay_lock(tmp_path / "b", [other]), conda_dir=tmp_path / "c", conda_exe="conda",
                 jailed=False, runner=_fake_conda([]))


def test_replay_keeps_a_changed_replay_aside_and_refuses_unlisted_packages(tmp_path):
    lock = _replay_lock(tmp_path, [_PKG])
    first = B.replay(lock, conda_dir=tmp_path / "conda", conda_exe="conda", jailed=False, runner=_fake_conda([]))
    prefix = Path(first["prefix"])
    (prefix / "conda-meta" / "debug.json").write_text(json.dumps({"url": "https://x/dbg.conda", "md5": "f" * 32}))
    again = B.replay(lock, conda_dir=tmp_path / "conda", conda_exe="conda", jailed=False, runner=_fake_conda([]))
    assert again["conda_match"]
    [kept] = prefix.parent.glob(f"{prefix.name}.changed-*")
    assert (kept / "conda-meta" / "debug.json").exists()
    lock["components"]["conda"].append("local-pkg-1.0-0#" + "a" * 32)
    with pytest.raises(ValueError, match="no URL"):
        B.replay(lock, conda_dir=tmp_path / "conda", conda_exe="conda", jailed=False, runner=_fake_conda([]))


def _fake_conda_installation(root: Path) -> Path:
    """A conda installation whose `conda` is a small program (the process boundary): it answers
    `config --show pkgs_dirs` and writes the records a `create --file <lock>` would."""
    (root / "conda-meta").mkdir(parents=True)
    (root / "conda-meta" / "history").write_text("")
    exe = root / "bin" / "conda"
    exe.parent.mkdir()
    exe.write_text(
        "#!/usr/bin/python3\nimport json, os, sys\nargs = sys.argv[1:]\n"
        "if 'config' in args:\n    print(json.dumps({'pkgs_dirs': [os.environ['CONDA_PKGS_DIRS']]})); sys.exit(0)\n"
        "prefix = args[args.index('--prefix') + 1]; lock = args[args.index('--file') + 1]\n"
        "meta = os.path.join(prefix, 'conda-meta'); os.makedirs(meta, exist_ok=True)\n"
        "open(os.path.join(meta, 'history'), 'w').close()\n"
        "lines = [l.strip() for l in open(lock) if l.startswith('https')]\n"
        "for i, line in enumerate(lines):\n"
        "    url, md5 = line.split('#')\n"
        "    json.dump({'url': url, 'md5': md5}, open(os.path.join(meta, f'p{i}.json'), 'w'))\n"
    )
    exe.chmod(0o755)
    return exe


def test_a_jailed_build_works_under_a_symlinked_folder(tmp_path):
    """The runtime dir reached through a link (`/data` → `/nfsdata1` here): the staged lock must be
    readable in the jail under the spelling conda is given. A real jailed process, not a fake runner."""
    from auto_research.sandbox import jail

    if not jail.jail_available()[0]:
        pytest.skip("bwrap unavailable")
    import shutil
    import tempfile

    # Outside /tmp: the build jail's private /tmp would otherwise sit on top of these very paths.
    base = Path(tempfile.mkdtemp(prefix="ar-symlinked-", dir="/var/tmp"))
    try:
        conda = _fake_conda_installation(base / "conda-install")
        (base / "real").mkdir()
        (base / "linked").symlink_to(base / "real")
        folder = _definition(base / "linked" / "defs")
        env = B.build(B.read_definition(folder), conda_dir=base / "linked" / "conda", conda_exe=str(conda),
                      base_envs={}, externals={}, jailed=True)
        assert env.prefix == base / "linked" / "conda" / "envs" / "ar-tiny"
        assert len(list((base / "real" / "conda" / "envs" / "ar-tiny" / "conda-meta").glob("p*.json"))) == 2
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_jailed_wheel_pin_works_under_a_symlinked_folder():
    import shutil
    import tempfile

    from auto_research.sandbox import jail

    if not jail.jail_available()[0]:
        pytest.skip("bwrap unavailable")
    base = Path(tempfile.mkdtemp(prefix="ar-symlinked-", dir="/var/tmp"))
    try:
        (base / "real" / "prefix" / "bin").mkdir(parents=True)
        (base / "linked").symlink_to(base / "real")
        python = base / "linked" / "prefix" / "bin" / "python"
        # The candidate's Python at the process boundary: `-m pip download … -d <dir> <req>` writes one wheel.
        python.write_text(
            "#!/usr/bin/python3\nimport sys, os\nargs = sys.argv[1:]\nd = args[args.index('-d') + 1]\n"
            "name, version = args[-1].split('==')\n"
            "open(os.path.join(d, f'{name}-{version}-py3-none-any.whl'), 'wb').write(b'wheel')\n")
        python.chmod(0o755)
        lines = B.pin_wheels(python, ["pkg==1.0"], work=base / "linked" / "work", jailed=True)
        assert lines and lines[0].startswith("pkg==1.0 --hash=sha256:")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_package_with_a_large_file_list_verifies(tmp_path):
    """A header-only library's conda record lists tens of thousands of files (libboost-headers:
    over 4 MB); verify reads it like any other (B5 live cell, 2026-10-07)."""
    folder = _definition(tmp_path / "defs")
    env = _build(tmp_path, folder, [])
    record = env.prefix / "conda-meta" / "p0.json"
    data = json.loads(record.read_text())
    data["files"] = [f"include/boost/header_{i:06d}.hpp" for i in range(200_000)]  # ~7 MB
    record.write_text(json.dumps(data))
    assert record.stat().st_size > 6 * 1024 * 1024
    B.verify(B.read_definition(folder), env.prefix)
