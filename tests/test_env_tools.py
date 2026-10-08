"""The coder's environment tools (env plan B3c-3): solve, pin, activate a candidate, and their wiring
into both author harnesses and the authoring session."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import types

import pytest

from auto_research.sandbox import env_build as B
from auto_research.sandbox.coder.env_tools import CandidateEnvs
from auto_research.sandbox.envs import Environment


def _fake_base(tmp_path: pathlib.Path) -> Environment:
    """A human base prefix whose `python` prints what the activation set."""
    prefix = tmp_path / "astropy"
    (prefix / "bin").mkdir(parents=True)
    python = prefix / "bin" / "python"
    python.write_text('#!/bin/bash\necho "base-python X=$X args=$*"\n')
    python.chmod(0o755)
    return Environment(name="astropy", prefix=prefix, owner="human")


def _derived_definition(worktree: pathlib.Path, name: str = "stack") -> pathlib.Path:
    folder = worktree / "envs" / name
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: test stack\nbase: astropy\n")
    (folder / "activate.sh").write_text("export X=first\n")
    return folder


def _patch_config(monkeypatch, base: Environment | None) -> None:
    monkeypatch.setattr("auto_research.env.analysis_environment", lambda config=None: base)
    monkeypatch.setattr("auto_research.env.declared_externals", lambda config=None: {})
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: "conda")


def test_activate_points_plain_commands_at_the_candidate_and_back(tmp_path, monkeypatch):
    base = _fake_base(tmp_path)
    _patch_config(monkeypatch, base)
    worktree = tmp_path / "tk"
    folder = _derived_definition(worktree)
    cand = CandidateEnvs(worktree, tmp_path / "art" / "envs_cand" / "tool")

    answer = asyncio.run(cand.activate("stack"))
    assert "ar-stack" in answer and "lacks pytest, pip" in answer
    # The analysis environment's variables stay out: the candidate starts clean.
    shell_env = {"PATH": f"{cand.active_bin}:/usr/bin:/bin", "LEAKED": "analysis", "HOME": str(tmp_path)}
    (folder / "activate.sh").write_text('export X="first${LEAKED}"\n')
    out = subprocess.run(["python", "-V"], env=shell_env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "base-python X=first args=-V"
    (folder / "activate.sh").write_text("export X=first\n")
    missing = subprocess.run(["pytest"], env=shell_env, capture_output=True, text=True)
    assert missing.returncode == 127 and "remove `base`" in missing.stderr

    # The worktree's activate.sh is sourced on every call: an edit applies without a rebuild.
    (folder / "activate.sh").write_text("export X=second\n")
    out = subprocess.run(["python"], env=shell_env, capture_output=True, text=True, check=True).stdout
    assert "X=second" in out

    assert "analysis environment" in asyncio.run(cand.activate(None))
    assert list(cand.active_bin.iterdir()) == []

    cand.cleanup()
    assert not cand.cand_dir.exists()


def test_activate_answers_a_bad_name_or_definition_without_building(tmp_path, monkeypatch):
    _patch_config(monkeypatch, None)
    worktree = tmp_path / "tk"
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    assert "env id" in asyncio.run(cand.activate("../escape"))
    assert not (worktree / "envs").exists()
    (worktree / "envs" / "broken").mkdir(parents=True)
    answer = asyncio.run(cand.activate("broken"))
    assert answer.startswith("The definition cannot be built yet") and "env.yaml" in answer
    assert list(cand.active_bin.iterdir()) == []


def test_pin_asks_for_an_activated_candidate_first(tmp_path):
    cand = CandidateEnvs(tmp_path / "tk", tmp_path / "cand")
    assert "Activate the candidate first" in asyncio.run(cand.pin("stack", ["pkg==1.0"]))
    assert not (tmp_path / "tk" / "envs" / "stack" / "env.pip").exists()


def _solver(fetch, link, code=0):
    def run(argv, **kwargs):
        assert "--dry-run" in argv and "--override-channels" in argv
        assert kwargs["env"]["CONDA_PKGS_DIRS"].endswith("pkgs")
        return types.SimpleNamespace(returncode=code, stdout=json.dumps({"actions": {"FETCH": fetch, "LINK": link}}),
                                     stderr="")
    return run


def test_solve_writes_an_explicit_lock_the_build_accepts(tmp_path):
    url = "https://conda.anaconda.org/conda-forge/noarch/"
    fetch = [{"fn": "tzdata-2025a-h0.conda", "url": url + "tzdata-2025a-h0.conda", "md5": "a" * 32},
             {"fn": "pip-25.0-pyh0.conda", "url": url + "pip-25.0-pyh0.conda", "md5": "b" * 32}]
    link = [{"dist_name": "pip-25.0-pyh0"}, {"dist_name": "tzdata-2025a-h0"}]
    lock = B.solve(["pip", "tzdata"], conda_exe="conda", work=tmp_path / "w", jailed=False,
                   runner=_solver(fetch, link))
    lines = lock.splitlines()
    assert lines[1] == "@EXPLICIT" and lines[2:] == [url + "pip-25.0-pyh0.conda#" + "b" * 32,
                                                     url + "tzdata-2025a-h0.conda#" + "a" * 32]
    folder = tmp_path / "defs" / "tiny"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: tiny\n")
    (folder / "env.lock").write_text(lock)
    assert B.validate(folder, base_envs={}, externals={}) == []


def test_solve_refuses_option_shaped_specs_and_reports_a_failed_solve(tmp_path):
    with pytest.raises(ValueError, match="refused"):
        B.solve(["--channel=evil"], conda_exe="conda", work=tmp_path, jailed=False, runner=_solver([], []))
    with pytest.raises(ValueError):
        B.solve([], conda_exe="conda", work=tmp_path, jailed=False, runner=_solver([], []))
    with pytest.raises(RuntimeError, match="named"):
        B.solve(["pip"], conda_exe="conda", work=tmp_path, jailed=False,
                runner=_solver([], [{"dist_name": "pip-25.0-pyh0"}]))


def test_pin_wheels_hashes_the_downloaded_wheel(tmp_path):
    def run(argv, **kwargs):
        target = pathlib.Path(argv[argv.index("-d") + 1])
        (target / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    import hashlib

    lines = B.pin_wheels(tmp_path / "py", ["pkg==1.0"], work=tmp_path / "w", jailed=False, runner=run)
    assert lines == [f"pkg==1.0 --hash=sha256:{hashlib.sha256(b'wheel').hexdigest()}"]
    with pytest.raises(ValueError):
        B.pin_wheels(tmp_path / "py", ["pkg>=1"], work=tmp_path / "w", jailed=False, runner=run)


def test_the_claude_escalation_server_declares_the_env_tools_only_when_given(tmp_path):
    from auto_research.sandbox.coder.claude_sdk import _make_escalation_server

    async def handler(*_a, **_k):
        return ""

    def names(server) -> set[str]:
        instance = server["instance"]
        tools = asyncio.run(instance.request_handlers[__import__("mcp").types.ListToolsRequest](
            __import__("mcp").types.ListToolsRequest(method="tools/list")))
        return {tool.name for tool in tools.root.tools}

    assert names(_make_escalation_server(handler, handler)) == {"message"}
    with_env = _make_escalation_server(handler, handler, CandidateEnvs(tmp_path / "tk", tmp_path / "cand"))
    assert names(with_env) == {"message", "env_solve", "env_pin_wheels", "cand_env_activate"}


def test_the_codex_escalation_server_serves_the_env_tools(tmp_path):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from auto_research.sandbox.coder.escalation_server import EscalationServer

    async def handler(*_a, **_k):
        return ""

    async def run() -> None:
        server = EscalationServer(handler, handler, env_tools=CandidateEnvs(tmp_path / "tk", tmp_path / "cand"))
        await server.start()
        try:
            client = httpx.AsyncClient(headers={"Authorization": f"Bearer {server.token}"}, timeout=30)
            async with client, streamable_http_client(server.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert tools == {"message", "env_solve", "env_pin_wheels", "cand_env_activate"}
                    result = await session.call_tool("cand_env_activate", {"env_id": "Bad Name"})
                    assert "env id" in result.content[0].text
        finally:
            await server.aclose()

    asyncio.run(run())


def test_the_coder_jail_mounts_the_candidates_read_only_over_its_writable_base(tmp_path):
    from auto_research.sandbox.jail import coder_policy, jail_argv

    base = tmp_path / "wt"
    cand = base / "envs_cand"
    policy = coder_policy(label="t-cand", extra_rw=(str(base),), readonly=(str(cand),))
    assert str(cand) in policy.ro and str(base) in policy.rw
    argv = jail_argv(policy)
    if not argv:
        pytest.skip("bwrap not available")
    binds = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] in ("--bind", "--ro-bind")]
    assert binds.index(("--ro-bind", str(cand))) > binds.index(("--bind", str(base))), (
        "the read-only overlay must land on top of the writable root")


def test_the_authoring_session_wires_candidates_into_path_and_clears_them_at_close(tmp_path, monkeypatch):
    """Built through `build_authoring_session`, as production does: the coder's PATH starts at the
    candidate wrappers, the escalation server carries the env tools, and close removes the folder."""
    from tests.test_merge_flow import _coder_member, _spec

    from auto_research.sandbox.merge_flow import build_authoring_session

    monkeypatch.setattr("auto_research.sandbox.review.make_review_hook", lambda **_k: (lambda *a, **k: None))
    captured = {}

    class _Client:
        async def disconnect(self):
            return None

    def factory(options):
        captured["options"] = options
        return _Client()

    toolkit = tmp_path / "tk"
    (toolkit / ".git").mkdir(parents=True)
    session = build_authoring_session(
        _spec(), lead=object(), toolkit_root=toolkit, worktree_base=tmp_path / "wt",
        coder=_coder_member("test-model"), sdk_client_factory=factory,
        worktree_factory=lambda _r, base, slug: _mk(pathlib.Path(base) / slug.replace("/", "_")),
        run_gate=lambda *_a, **_k: None,
    )
    cand = session._env_tools
    assert cand.cand_dir.parent == tmp_path / "wt" / "envs_cand"
    assert cand.cand_dir.name.startswith(_spec().proposed_tool_name + "-")
    options = captured["options"]
    assert options.env["PATH"].split(":")[0] == str(cand.active_bin)
    assert {"mcp__escalation__env_solve", "mcp__escalation__cand_env_activate"} <= set(options.allowed_tools)
    asyncio.run(session.aclose())
    assert not cand.cand_dir.exists()


def _mk(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_the_seed_names_each_package_environment_and_the_definitions(tmp_path, monkeypatch):
    from auto_research.sandbox.merge_flow import _build_structure_context

    _patch_config(monkeypatch, Environment(name="astropy", prefix=tmp_path / "astropy", owner="human"))
    (tmp_path / "spec_utils").mkdir()
    (tmp_path / "spec_utils" / "tool_config.yaml").write_text("env: sherpa\n")
    (tmp_path / "envs" / "sherpa").mkdir(parents=True)
    context = _build_structure_context(tmp_path)
    assert "- package spec_utils: env `sherpa`" in context
    assert "- definitions under envs/: sherpa" in context
    assert "as `base`: astropy" in context


@pytest.mark.skipif(not os.environ.get("AR_LIVE_ENV_BUILD"), reason="live: set AR_LIVE_ENV_BUILD=1 (network, ~1 GB)")
def test_live_solve_then_build_then_activate(tmp_path, monkeypatch):
    """Real conda: the coder's three steps — solve a tiny lock, build the candidate, run in it."""
    _patch_config(monkeypatch, None)
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: os.environ["CONDA_EXE"])
    worktree = tmp_path / "tk"
    folder = worktree / "envs" / "tiny"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: tiny\n")
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    assert "Wrote envs/tiny/env.lock" in asyncio.run(cand.solve("tiny", ["python=3.12"]))
    assert "ar-tiny" in asyncio.run(cand.activate("tiny"))
    out = subprocess.run(["python", "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True,
                         env={"PATH": f"{cand.active_bin}:/usr/bin:/bin"}, check=True).stdout.strip()
    assert out == str(cand.cand_dir / "conda" / "envs" / "ar-tiny")


def test_a_failed_rebuild_returns_the_shell_to_the_analysis_environment(tmp_path, monkeypatch):
    _patch_config(monkeypatch, _fake_base(tmp_path))
    worktree = tmp_path / "tk"
    _derived_definition(worktree)
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    asyncio.run(cand.activate("stack"))
    assert (cand.active_bin / "python").exists()

    def broken(*_a, **_k):
        raise RuntimeError("conda failed half way")

    monkeypatch.setattr(B, "build", broken)
    answer = asyncio.run(cand.activate("stack"))
    assert "did not build" in answer and "analysis environment" in answer
    assert list(cand.active_bin.iterdir()) == []


def test_after_close_a_running_tool_writes_nothing_into_the_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "solve", lambda specs, **_k: "@EXPLICIT\n")
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: "conda")
    cand = CandidateEnvs(tmp_path / "tk", tmp_path / "cand")
    assert "Wrote envs/stack/env.lock" in asyncio.run(cand.solve("stack", ["python"]))
    (tmp_path / "tk" / "envs" / "stack" / "env.lock").unlink()
    cand.close()
    assert "ended" in asyncio.run(cand.solve("stack", ["python"]))
    assert not (tmp_path / "tk" / "envs" / "stack" / "env.lock").exists()


def test_the_runtime_writes_only_into_a_plain_folder_of_the_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "solve", lambda specs, **_k: "@EXPLICIT\n")
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: "conda")
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = tmp_path / "tk"
    worktree.mkdir()
    (worktree / "envs").symlink_to(outside)
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    assert "plain folders" in asyncio.run(cand.solve("stack", ["python"]))
    assert list(outside.iterdir()) == []


def test_each_attempt_gets_a_fresh_folder_and_earlier_ones_go(tmp_path):
    first = CandidateEnvs(tmp_path / "tk", CandidateEnvs.attempt_dir(tmp_path / "cand", "tool"))
    (first.active_bin / "python").write_text("#!/bin/bash\n")
    second = CandidateEnvs(tmp_path / "tk", CandidateEnvs.attempt_dir(tmp_path / "cand", "tool"))
    assert second.cand_dir != first.cand_dir and not first.cand_dir.exists()
    assert list(second.active_bin.iterdir()) == []


def test_the_codex_backend_carries_the_env_tools_their_timeout_and_the_read_only_candidates(tmp_path, monkeypatch):
    from tests.test_merge_flow import _spec

    from auto_research.models.types import RoleModelConfig
    from auto_research.sandbox.merge_flow import build_authoring_session

    monkeypatch.setattr("auto_research.sandbox.review.make_review_hook", lambda **_k: (lambda *a, **k: None))
    toolkit = tmp_path / "tk"
    (toolkit / ".git").mkdir(parents=True)
    session = build_authoring_session(
        _spec(), lead=object(), toolkit_root=toolkit, worktree_base=tmp_path / "wt",
        coder=RoleModelConfig(name="c", provider="openai", harness="codex", model="m"),
        sdk_client_factory=lambda _o: object(),
        worktree_factory=lambda _r, base, slug: _mk(pathlib.Path(base) / slug.replace("/", "_")),
        run_gate=lambda *_a, **_k: None,
    )
    backend, cand = session._backend, session._env_tools
    assert backend._jail_readonly == (str(cand.cand_dir),)
    assert backend._env["PATH"].split(":")[0] == str(cand.active_bin)
    assert backend._escalation._env_tools is cand
    backend._escalation_url = "http://127.0.0.1:1/mcp"
    assert backend._config_payload()["mcp_servers"]["escalation"]["tool_timeout_sec"] >= 1800


def test_the_claude_jail_launcher_binds_the_candidates_read_only(tmp_path, monkeypatch):
    from auto_research.sandbox import jail
    from auto_research.sandbox.coder.claude_sdk import _jail_launcher

    if not jail.jail_available()[0]:
        pytest.skip("bwrap not available")
    monkeypatch.setattr(jail, "jail_enabled", lambda: True)
    base, tree = tmp_path / "wt", tmp_path / "wt" / "tree"
    tree.mkdir(parents=True)
    launcher = _jail_launcher(base, tree, (str(base / "envs_cand"),))
    if launcher is None:
        pytest.skip("no Claude CLI to wrap")
    text = pathlib.Path(launcher).read_text()
    assert f"--ro-bind {base / 'envs_cand'} {base / 'envs_cand'}" in text


def test_a_session_whose_artifacts_dir_is_the_toolkit_gets_no_env_tools(tmp_path, monkeypatch):
    from tests.test_merge_flow import _coder_member, _spec

    from auto_research.sandbox.merge_flow import build_authoring_session

    monkeypatch.setattr("auto_research.sandbox.review.make_review_hook", lambda **_k: (lambda *a, **k: None))
    toolkit = tmp_path / "tk"
    (toolkit / ".git").mkdir(parents=True)
    session = build_authoring_session(
        _spec(), lead=object(), toolkit_root=toolkit, worktree_base=toolkit,
        coder=_coder_member("test-model"), sdk_client_factory=lambda _o: object(),
        worktree_factory=lambda _r, base, slug: _mk(pathlib.Path(base) / slug.replace("/", "_")),
        run_gate=lambda *_a, **_k: None,
    )
    assert session._env_tools is None and not (toolkit / "envs_cand").exists()


def test_a_derived_candidate_without_its_own_activation_runs_the_base_activation(tmp_path, monkeypatch):
    base = _fake_base(tmp_path)
    init = tmp_path / "base-init.sh"
    init.write_text("export X=from-base\n")
    base = Environment(name=base.name, prefix=base.prefix, owner="human", activate=init)
    _patch_config(monkeypatch, base)
    worktree = tmp_path / "tk"
    folder = _derived_definition(worktree)
    (folder / "activate.sh").unlink()
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    answer = asyncio.run(cand.activate("stack"))
    assert "base environment's activation" in answer and "remove `base`" in answer
    out = subprocess.run(["python"], env={"PATH": f"{cand.active_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
                         capture_output=True, text=True, check=True).stdout
    assert "X=from-base" in out


def test_a_candidate_activation_that_exits_or_moves_pfiles_is_contained(tmp_path, monkeypatch):
    base = _fake_base(tmp_path)
    show = base.prefix / "bin" / "showpf"
    show.write_text('#!/bin/bash\necho "PFILES=$PFILES"\n')
    show.chmod(0o755)
    _patch_config(monkeypatch, base)
    worktree = tmp_path / "tk"
    folder = _derived_definition(worktree)
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    asyncio.run(cand.activate("stack"))
    env = {"PATH": f"{cand.active_bin}:/usr/bin:/bin", "HOME": str(tmp_path), "PFILES": "/jail/pfiles;/sys"}
    # A HEASoft-style init puts the human's folder first; the jail's writable half comes back.
    (folder / "activate.sh").write_text('export PFILES="$HOME/pfiles:$PFILES"\n')
    out = subprocess.run(["showpf"], env=env, capture_output=True, text=True, check=True).stdout
    assert out.strip() == "PFILES=/jail/pfiles;/sys"
    (folder / "activate.sh").write_text("exit 0\n")
    ended = subprocess.run(["python"], env=env, capture_output=True, text=True)
    assert ended.returncode == 97 and "did not finish" in ended.stderr and ended.stdout == ""


def test_a_derived_candidate_runs_its_base_activation_inside_the_coder_jail(tmp_path, monkeypatch):
    """The human base activation lies outside the coder's jail; the candidate carries a copy."""
    from auto_research.sandbox.jail import coder_policy, jail_argv

    base = _fake_base(tmp_path)
    init = tmp_path / "outside" / "base-init.sh"
    init.parent.mkdir()
    init.write_text("export X=from-base\n")
    base = Environment(name=base.name, prefix=base.prefix, owner="human", activate=init)
    _patch_config(monkeypatch, base)
    worktree = tmp_path / "tk"
    folder = _derived_definition(worktree)
    (folder / "activate.sh").unlink()
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    asyncio.run(cand.activate("stack"))
    argv = jail_argv(coder_policy(label="t-cand-jail", extra_rw=(str(worktree),),
                                  readonly=(str(cand.cand_dir), str(base.prefix))))
    if not argv:
        pytest.skip("bwrap not available")
    run = subprocess.run([*argv, "--", "/bin/bash", "-c", f"PATH={cand.active_bin}:/usr/bin:/bin; cd {worktree}; python"],
                         capture_output=True, text=True)
    assert run.returncode == 0 and "X=from-base" in run.stdout, run.stderr


def test_concurrent_runs_of_one_wrapper_keep_their_own_exit_codes_and_arguments(tmp_path, monkeypatch):
    base = _fake_base(tmp_path)
    (base.prefix / "bin" / "python").write_text('#!/bin/bash\nsleep "$1"; echo "args=$*"; exit "$2"\n')
    _patch_config(monkeypatch, base)
    worktree = tmp_path / "tk"
    folder = _derived_definition(worktree)
    (folder / "activate.sh").write_text("set -- hijacked\n")  # an activation touching "$@"
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    asyncio.run(cand.activate("stack"))
    script = "python 0.6 3 & a=$!; python 0.1 5; b=$?; wait $a; echo \"rc=$? $b\""
    out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                         env={"PATH": f"{cand.active_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}).stdout
    assert "rc=3 5" in out and "args=0.6 3" in out and "hijacked" not in out


def test_pin_needs_a_candidate_built_from_the_current_lock(tmp_path, monkeypatch):
    _patch_config(monkeypatch, _fake_base(tmp_path))
    worktree = tmp_path / "tk"
    _derived_definition(worktree)
    cand = CandidateEnvs(worktree, tmp_path / "cand")
    assert "takes no env.pip" in asyncio.run(cand.pin("stack", ["pkg==1.0"]))
    folder = worktree / "envs" / "built"
    folder.mkdir()
    (folder / "env.yaml").write_text("purpose: x\n")
    (folder / "env.lock").write_text("@EXPLICIT\n")
    cand._built_lock["built"] = "stale"
    assert "current env.lock" in asyncio.run(cand.pin("built", ["pkg==1.0"]))


def _manifest_gate():
    """A passing gate that records what the real one does: the changeset manifest and its base."""
    from auto_research.sandbox.gate import changeset_manifest
    from auto_research.schemas.merge import GateReport

    async def gate(worktree, spec):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True,
                              text=True, check=True).stdout.strip()
        return GateReport(passed=True, manifest=changeset_manifest(worktree), base=head)
    return gate


def test_a_direct_branch_commit_builds_its_environment_and_the_catalogue_lists_it(tmp_path, monkeypatch):
    """B3c-4, through the lead's submit path: the committed definition is built into the runtime's
    agent conda dir, the ledger records env.build, the lead is told, and `use_env` can see it."""
    from tests.test_merge_flow import (_FakeClient, _FakeLead, _RecordingMonitor, _coder_member, _git,
                                       _pass_review, _spec, _toolkit_repo)

    from auto_research.agents.react import execute_submit
    from auto_research.env import env_catalogue
    from auto_research.sandbox.merge_flow import approve_edit, build_authoring_session

    base = _fake_base(tmp_path)
    _patch_config(monkeypatch, base)
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "agent-conda")
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    mon = _RecordingMonitor()
    session = build_authoring_session(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
        lead=_FakeLead(), coder=_coder_member("fake-model"), run_gate=_manifest_gate(),
        run_review=_pass_review, sdk_client_factory=lambda _o: _FakeClient(), approve_edit=approve_edit,
        monitor=mon,
    )
    (repo / "demo_utils" / "tool_config.yaml").write_text("env: stack\n")
    (repo / "demo_utils" / "lib" / "x.py").write_text("X = 1\n")
    _derived_definition(repo)
    answer = asyncio.run(execute_submit(session)).text
    assert "COMMITTED" in answer and "Environment built: ar-stack" in answer
    built = [e for e in mon.events if e["event"] == "env.build"]
    assert len(built) == 1 and built[0]["data"]["name"] == "ar-stack" and built[0]["data"]["commit_sha"]
    assert "ar-stack" in [env.name for env in env_catalogue({})]


def test_a_commit_whose_environment_fails_to_build_still_stands_and_says_so(tmp_path, monkeypatch):
    from tests.test_merge_flow import (_FakeClient, _FakeLead, _RecordingMonitor, _coder_member, _git,
                                       _pass_review, _spec, _toolkit_repo)

    from auto_research.agents.react import execute_submit
    from auto_research.sandbox.merge_flow import approve_edit, build_authoring_session

    _patch_config(monkeypatch, _fake_base(tmp_path))
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "agent-conda")

    def broken(*_a, **_k):
        raise RuntimeError("conda: network unreachable")

    monkeypatch.setattr(B, "build", broken)
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    mon = _RecordingMonitor()
    session = build_authoring_session(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
        lead=_FakeLead(), coder=_coder_member("fake-model"), run_gate=_manifest_gate(),
        run_review=_pass_review, sdk_client_factory=lambda _o: _FakeClient(), approve_edit=approve_edit,
        monitor=mon,
    )
    _derived_definition(repo)
    (repo / "demo_utils" / "tool_config.yaml").write_text("env: stack\n")
    answer = asyncio.run(execute_submit(session)).text
    assert "COMMITTED" in answer and "ar-stack did not build" in answer and "network unreachable" in answer
    assert [e for e in mon.events if e["event"] == "env.build.error"]
    assert session.last_commit_sha


def test_a_commit_deleting_a_definition_drops_it_from_the_catalogue(tmp_path, monkeypatch):
    from tests.test_merge_flow import (_FakeClient, _FakeLead, _RecordingMonitor, _coder_member, _git,
                                       _pass_review, _spec, _toolkit_repo)

    from auto_research.agents.react import execute_submit
    from auto_research.env import env_catalogue
    from auto_research.sandbox.merge_flow import approve_edit, build_authoring_session

    base = _fake_base(tmp_path)
    _patch_config(monkeypatch, base)
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "agent-conda")
    repo = _toolkit_repo(tmp_path)
    _derived_definition(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "env")
    _git(repo, "checkout", "-q", "-b", "ai")
    B.build(B.read_definition(repo / "envs" / "stack"), conda_dir=tmp_path / "agent-conda", conda_exe="conda",
            base_envs={"astropy": base}, externals={}, jailed=True)
    assert "ar-stack" in [e.name for e in env_catalogue({})]
    mon = _RecordingMonitor()
    session = build_authoring_session(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
        lead=_FakeLead(), coder=_coder_member("fake-model"), run_gate=_manifest_gate(),
        run_review=_pass_review, sdk_client_factory=lambda _o: _FakeClient(), approve_edit=approve_edit,
        monitor=mon,
    )
    _git(repo, "rm", "-rq", "--cached", "envs")
    import shutil as _shutil
    _shutil.rmtree(repo / "envs")
    answer = asyncio.run(execute_submit(session)).text
    assert "COMMITTED" in answer and "Removed from the catalogue (definition deleted): ar-stack" in answer
    assert "ar-stack" not in [e.name for e in env_catalogue({})] and mon.of("env.forget")


def test_closing_the_tools_kills_a_running_solve(tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setattr("auto_research.env.conda_executable", lambda: "/bin/sleep")
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)
    monkeypatch.setattr(B, "solve", lambda specs, *, runner, **_k: runner(["/bin/sleep", "30"]) and "")
    cand = CandidateEnvs(tmp_path / "tk", tmp_path / "cand")
    answers = []
    thread = threading.Thread(target=lambda: answers.append(asyncio.run(cand.solve("stack", ["python"]))))
    started = time.monotonic()
    thread.start()
    time.sleep(0.3)
    cand.close()
    thread.join(10)
    assert time.monotonic() - started < 10 and "Not solved" in answers[0]


def test_a_stop_during_the_commit_build_kills_it_and_says_so(tmp_path, monkeypatch):
    import time

    from tests.test_merge_flow import (_FakeClient, _FakeLead, _RecordingMonitor, _coder_member, _git,
                                       _pass_review, _spec, _toolkit_repo)

    from auto_research.sandbox import merge_flow
    from auto_research.sandbox.merge_flow import approve_edit, build_authoring_session

    _patch_config(monkeypatch, _fake_base(tmp_path))

    def slow(toolkit_root, env_ids, *, runner):
        runner(["/bin/sleep", "30"])
        return []

    monkeypatch.setattr(merge_flow, "build_committed_environments", slow)
    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    mon = _RecordingMonitor()
    session = build_authoring_session(
        _spec(), toolkit_root=repo, worktree_base=tmp_path / "wt", direct_branch="ai",
        lead=_FakeLead(), coder=_coder_member("fake-model"), run_gate=_manifest_gate(),
        run_review=_pass_review, sdk_client_factory=lambda _o: _FakeClient(), approve_edit=approve_edit,
        monitor=mon,
    )
    _derived_definition(repo)
    (repo / "demo_utils" / "tool_config.yaml").write_text("env: stack\n")

    async def scenario():
        session.last_gate = await session._run_gate(session.worktree, session.tool_spec)
        task = asyncio.create_task(session.commit("land it"))
        await asyncio.sleep(1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 10 and session.last_commit_sha
    assert [e for e in mon.of("env.build.error") if e["data"]["error"] == "stopped"]



def test_the_gate_admits_a_deleted_definition_unless_a_package_still_names_it(tmp_path, monkeypatch):
    from tests.test_merge_flow import _git, _toolkit_repo

    from auto_research.sandbox.gate import check_env_definitions, collect_all_changes

    _patch_config(monkeypatch, _fake_base(tmp_path))
    repo = _toolkit_repo(tmp_path)
    _derived_definition(repo)
    (repo / "other_utils").mkdir()
    (repo / "other_utils" / "tool_config.yaml").write_text("env: stack\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "env")
    import shutil as _shutil
    _shutil.rmtree(repo / "envs")
    changes = collect_all_changes(repo)
    [result] = [r for r in check_env_definitions(repo, changes, {}) if r.name.endswith(".stack")]
    assert not result.passed and "other_utils still name it" in result.detail
    (repo / "other_utils" / "tool_config.yaml").write_text("env: null\n")
    changes = collect_all_changes(repo)
    [result] = [r for r in check_env_definitions(repo, changes, {}) if r.name.endswith(".stack")]
    assert result.passed


def _reconcile_setup(tmp_path, monkeypatch, externals=None):
    from auto_research.sandbox.envs import External

    _patch_config(monkeypatch, _fake_base(tmp_path))
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "agent-conda")
    monkeypatch.setattr("auto_research.env.env_lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr("auto_research.env.declared_externals", lambda config=None: externals or {})
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)
    toolkit = tmp_path / "tk"
    from tests.test_env_build import _PKG, _PKG2, _definition

    folder = _definition(toolkit / "envs", name="built", yaml="purpose: x\nexternals: [heasoft]\n" if externals
                         else "purpose: x\nexternals: []\n", lock=("@EXPLICIT", _PKG, _PKG2))
    return toolkit, folder, External


def test_launch_builds_a_committed_definition_whose_record_is_missing(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.env import env_catalogue
    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    results = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert [(r["env"], r["status"]) for r in results] == [("built", "built")]
    assert "ar-built" in [e.name for e in env_catalogue({})]
    assert [r["status"] for r in reconcile_environments(toolkit, runner=_fake_conda([]))] == ["ok"]


def test_launch_rebuilds_a_drifted_prefix_visibly_and_keeps_the_drifted_one(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    prefix = tmp_path / "agent-conda" / "envs" / "ar-built"
    # A human installs into it by hand.
    (prefix / "conda-meta" / "extra.json").write_text(json.dumps({"url": "https://x/numpy.conda", "md5": "c" * 32}))
    results = reconcile_environments(toolkit, runner=_fake_conda([]))
    drift = next(r for r in results if r["status"] == "drift")
    kept = pathlib.Path(drift["kept"])
    assert kept.name.startswith("ar-built.drift-") and (kept / "conda-meta" / "extra.json").exists()
    assert (tmp_path / "locks" / f"{drift['fingerprint']}.json").is_file(), "the drifted state stays rebuildable"
    assert drift["rebuilt"] is True
    assert not (prefix / "conda-meta" / "extra.json").exists()
    assert [r["status"] for r in reconcile_environments(toolkit, runner=_fake_conda([]))] == ["ok"]


def test_launch_reports_a_changed_external_once_and_records_it(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.envs import External
    from auto_research.sandbox.merge_flow import reconcile_environments

    root = tmp_path / "heasoft"
    root.mkdir()
    (root / "BUILD").write_text("6.34\n")
    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch, {"heasoft": External("heasoft", root, ("BUILD",))})
    reconcile_environments(toolkit, runner=_fake_conda([]))
    assert [r["status"] for r in reconcile_environments(toolkit, runner=_fake_conda([]))] == ["ok"]
    (root / "BUILD").write_text("6.35\n")
    changed = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert changed == [{"env": "built", "status": "external_changed", "externals": ["heasoft"]}]
    assert [r["status"] for r in reconcile_environments(toolkit, runner=_fake_conda([]))] == ["ok"]


def test_the_launch_step_records_each_reconciled_environment_on_the_ledger(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda
    from tests.test_merge_flow import _RecordingMonitor

    from auto_research.graph import runtime
    from auto_research.sandbox import env_build

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    fake = _fake_conda([])

    class _Runner(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _Runner)
    mon = _RecordingMonitor()
    asyncio.run(runtime._reconcile_environments(toolkit, mon))
    [event] = mon.of("env.build")
    assert event["data"]["name"] == "ar-built" and event["data"]["status"] == "built"



def test_launch_reports_a_definition_problem_without_touching_the_build(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.env import env_catalogue
    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    (folder / ".env.yaml.swp").write_text("editor junk")
    [result] = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert result["status"] == "failed" and "drift" not in result
    assert (tmp_path / "agent-conda" / "envs" / "ar-built").is_dir()
    assert "ar-built" in [e.name for e in env_catalogue({})]


def test_launch_detects_a_pip_install_by_hand_as_drift(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    site = tmp_path / "agent-conda" / "envs" / "ar-built" / "lib" / "python3.12" / "site-packages"
    (site / "requests-2.32.0.dist-info").mkdir(parents=True)
    [result] = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert result["status"] == "drift" and "requests-2.32.0.dist-info" in result["diff"] and result["rebuilt"]


def test_a_failed_drift_rebuild_puts_the_drifted_prefix_back_in_use(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.env import env_catalogue
    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    prefix = tmp_path / "agent-conda" / "envs" / "ar-built"
    (prefix / "conda-meta" / "extra.json").write_text(json.dumps({"url": "https://x/numpy.conda", "md5": "c" * 32}))
    fake = _fake_conda([])

    def offline(argv, **kw):
        if "create" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return fake(argv, **kw)

    [result] = reconcile_environments(toolkit, runner=offline)
    assert result["status"] == "drift" and result["rebuilt"] is False
    assert (prefix / "conda-meta" / "extra.json").exists(), "the drifted prefix is back in place"
    assert "ar-built" in [e.name for e in env_catalogue({})]
    assert not list(prefix.parent.glob("ar-built.drift-*"))


def test_launch_refreshes_a_record_whose_activation_changed_after_a_stopped_build(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.env import env_catalogue
    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    (folder / "activate.sh").write_text("export A=1\n")  # committed; its build never ran
    assert [r["status"] for r in reconcile_environments(toolkit, runner=_fake_conda([]))] == ["ok"]
    [listed] = [e for e in env_catalogue({}) if e.name == "ar-built"]
    assert listed.activate is not None and listed.activate.read_text() == "export A=1\n"


def test_launch_says_when_an_unjailed_build_taints_reuse(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))  # jail off: an unjailed build
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: True)
    monkeypatch.setattr("auto_research.sandbox.env_build.build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no bwrap here")))
    results = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert results[0]["status"] == "tainted" and results[0]["marker"].endswith("unjailed.json")


def test_launch_rebuilds_a_deleted_prefix_and_says_why(tmp_path, monkeypatch):
    import shutil as _shutil

    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    _shutil.rmtree(tmp_path / "agent-conda" / "envs" / "ar-built")
    [result] = reconcile_environments(toolkit, runner=_fake_conda([]))
    assert result["status"] == "built" and result["cause"] == "missing"


def test_a_stopped_launch_leaves_every_environment_as_it_is(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.merge_flow import reconcile_environments

    toolkit, _folder, _ = _reconcile_setup(tmp_path, monkeypatch)
    reconcile_environments(toolkit, runner=_fake_conda([]))
    prefix = tmp_path / "agent-conda" / "envs" / "ar-built"
    (prefix / "conda-meta" / "extra.json").write_text(json.dumps({"url": "https://x/numpy.conda", "md5": "c" * 32}))
    stopped = _fake_conda([])
    stopped.stopped = True
    assert reconcile_environments(toolkit, runner=stopped) == []
    assert (prefix / "conda-meta" / "extra.json").exists() and not list(prefix.parent.glob("*.drift-*"))


def test_an_external_added_to_a_definition_is_a_new_baseline_not_a_change(tmp_path, monkeypatch):
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox.envs import External
    from auto_research.sandbox.merge_flow import reconcile_environments

    root = tmp_path / "heasoft"
    root.mkdir()
    (root / "BUILD").write_text("6.34\n")
    toolkit, folder, _ = _reconcile_setup(tmp_path, monkeypatch, {"heasoft": External("heasoft", root, ("BUILD",))})
    (folder / "env.yaml").write_text("purpose: x\nexternals: []\n")
    reconcile_environments(toolkit, runner=_fake_conda([]))
    reconcile_environments(toolkit, runner=_fake_conda([]))
    (folder / "env.yaml").write_text("purpose: x\nexternals: [heasoft]\n")
    assert [r["status"] for r in reconcile_environments(toolkit, runner=_fake_conda([]))] == ["ok"]


def test_replay_environment_reads_the_lock_store_and_reports_externals(tmp_path, monkeypatch):
    from tests.test_env_build import _PKG, _fake_conda

    from auto_research.env import replay_environment
    from auto_research.sandbox import envs as E

    root = tmp_path / "heasoft"
    root.mkdir()
    (root / "BUILD").write_text("6.34\n")
    heasoft = E.External("heasoft", root, ("BUILD",))
    prefix = tmp_path / "recorded"
    (prefix / "conda-meta").mkdir(parents=True)
    url, md5 = _PKG.split("#")
    (prefix / "conda-meta" / "p.json").write_text(json.dumps({"url": url, "md5": md5}))
    init = tmp_path / "init.sh"
    init.write_text("export X=1\n")
    recorded = E.Environment(name="astropy", prefix=prefix, owner="human", activate=init, externals=(heasoft,))
    monkeypatch.setattr("auto_research.env.env_lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "agent-conda")
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: "conda")
    monkeypatch.setattr("auto_research.env.declared_externals", lambda config=None: {"heasoft": heasoft})
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)
    fp = E.fingerprint_and_lock(recorded, tmp_path / "locks")
    (root / "BUILD").write_text("6.35\n")
    report = replay_environment(fp, runner=_fake_conda([]))
    assert report["conda_match"] and report["externals"] == {"heasoft": "changed"}
    assert pathlib.Path(report["activation_script"]).read_text() == "export X=1\n"
    with pytest.raises(ValueError):
        replay_environment("../../etc")
    forged = tmp_path / "locks" / f"{'0' * 64}.json"
    forged.write_text((tmp_path / "locks" / f"{fp}.json").read_text())
    with pytest.raises(ValueError, match="does not hash"):
        replay_environment("0" * 64, runner=_fake_conda([]))


@pytest.mark.skipif(not os.environ.get("AR_LIVE_ENV_BUILD"), reason="live: set AR_LIVE_ENV_BUILD=1 (network, ~1 GB)")
def test_live_a_recorded_state_replays_and_matches_its_conda_part(tmp_path, monkeypatch):
    """B4 checkpoint: a state a receipt would stamp (its lock in the store) rebuilds from the
    lock alone into its own prefix, and the rebuilt conda part equals the recorded one."""
    from auto_research.env import replay_environment
    from auto_research.sandbox import envs as E

    conda = os.environ["CONDA_EXE"]
    home = tmp_path / "seed-home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CONDA_PKGS_DIRS": str(tmp_path / "seed-pkgs"),
           "CONDA_ALWAYS_COPY": "true", "CONDA_REGISTER_ENVS": "false"}
    subprocess.run([conda, "create", "--yes", "--quiet", "--override-channels", "-c", "conda-forge",
                    "-p", str(tmp_path / "seed"), "python=3.12"], env=env, check=True, capture_output=True)
    monkeypatch.setattr("auto_research.env.env_lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "agent-conda")
    monkeypatch.setattr("auto_research.env.conda_executable", lambda: conda)
    fp = E.fingerprint_and_lock(E.Environment(name="seed", prefix=tmp_path / "seed"), tmp_path / "locks")
    report = replay_environment(fp)
    assert report["conda_match"], (report["missing"], report["extra"])


def _routing_provider(tmp_path, monkeypatch, *, tool_env="stack", built=True, jail=True):
    """A worker's provider over a sandbox stand-in that records where each call was sent (the REPL
    pool itself is driven for real in the next test)."""
    from auto_research import env as app_env
    from auto_research.agents.react import SandboxToolProvider
    from auto_research.provenance import ReceiptStore
    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.registry import ToolkitRegistry

    monkeypatch.setattr(app_env, "env_lock_dir", lambda: tmp_path / "locks")
    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: jail)
    (tmp_path / "own" / "conda-meta").mkdir(parents=True)
    (tmp_path / "stack" / "conda-meta").mkdir(parents=True)
    (tmp_path / "stack" / "conda-meta" / "x.json").write_text(json.dumps({"url": "https://x/sherpa.conda", "md5": "e" * 32}))
    own = Environment(name="analysis", prefix=tmp_path / "own")
    stack = Environment(name="ar-stack", prefix=tmp_path / "stack", owner="pipeline")

    class _Sandbox:
        is_alive = True

        def __init__(self):
            self.environment = own
            self.environments = lambda: [stack] if built else []
            self.sent = []

        async def call_tool(self, module, fn, args, timeout_s=None, environment=None):
            self.sent.append((dict(args), environment))
            self.call_environment = environment or own
            return {"exit_code": 0, "stdout": "PD 4.0"}

    reg = ToolkitRegistry(tmp_path)
    reg._tools = {"t": {"module": "m", "fn": "f", "description": "d",
                        "args_schema": {"type": "object", "properties": {}}}}
    reg._tool_env = {"t": tool_env} if tool_env else {}
    reg._loaded = True
    store = ReceiptStore(tmp_path / "receipts")
    sandbox = _Sandbox()
    return SandboxToolProvider(reg, sandbox, cwd=tmp_path, receipts=store, receipt_source="w0"), sandbox, store


def test_a_toolkit_call_runs_in_its_packages_environment(tmp_path, monkeypatch):
    import re

    from auto_research.sandbox.envs import fingerprint

    provider, sandbox, store = _routing_provider(tmp_path, monkeypatch)
    outcome = asyncio.run(provider.execute("t", {}))
    routed = sandbox.sent[-1][1]
    assert routed.name == "ar-stack" and "[ran in ar-stack" in outcome.text
    assert "untested" not in outcome.text
    [handle] = re.findall(r"\bw0:r\d+", outcome.text)
    receipt = store.get(handle)
    assert receipt.env_untested is False
    assert receipt.env == fingerprint(routed) != fingerprint(sandbox.environment)


def test_a_callers_env_choice_runs_there_and_is_marked_untested(tmp_path, monkeypatch):
    import re

    provider, sandbox, store = _routing_provider(tmp_path, monkeypatch)
    outcome = asyncio.run(provider.execute("t", {"env": "analysis"}))
    args, environment = sandbox.sent[-1]
    assert environment is None and "env" not in args, "the own REPL; the choice is never sent to the tool"
    assert "untested environment for this tool" in outcome.text
    [handle] = re.findall(r"\bw0:r\d+", outcome.text)
    assert store.get(handle).env_untested is True


def test_a_tool_whose_environment_is_not_built_says_so(tmp_path, monkeypatch):
    provider, sandbox, _ = _routing_provider(tmp_path, monkeypatch, built=False)
    outcome = asyncio.run(provider.execute("t", {}))
    assert outcome.is_error and "ar-stack is not available" in outcome.text and not sandbox.sent
    provider, sandbox, _ = _routing_provider(tmp_path / "b", monkeypatch, jail=False)
    outcome = asyncio.run(provider.execute("t", {}))
    assert outcome.is_error and "only in the jail" in outcome.text and not sandbox.sent


def test_the_repl_pool_runs_a_call_in_another_environment_and_keeps_its_own(tmp_path, monkeypatch):
    """PersistentSandbox for real: a derived pipeline environment over this interpreter, its
    activation sourced by the launcher in its own REPL; the worker's own REPL is untouched."""
    import sys

    from auto_research.env import agent_conda_dir
    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)
    init = tmp_path / "own-init.sh"
    init.write_text("export OWN_READY=1\n")
    base = Environment(name="testbase", prefix=pathlib.Path(sys.prefix), owner="human", activate=init)
    folder = tmp_path / "defs" / "stack"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: x\nbase: testbase\n")
    (folder / "activate.sh").write_text("export STACK_READY=1\n")
    stack = B.build(B.read_definition(folder), conda_dir=agent_conda_dir(), conda_exe="conda",
                    base_envs={"testbase": base}, externals={}, jailed=True)
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work", environment=base)

    async def go():
        try:
            return [
                await sandbox.call_tool("os", "getenv", {"key": "STACK_READY"}),
                await sandbox.call_tool("os", "getenv", {"key": "STACK_READY"}, environment=stack),
                sandbox.call_environment.name,
                await sandbox.call_tool("os", "getenv", {"key": "OWN_READY"}),
                sandbox.call_environment.name,
            ]
        finally:
            await sandbox.shutdown()

    own_before, routed, routed_env, own_after, own_env = asyncio.run(go())
    assert own_before["stdout"] == "None" and routed["stdout"] == "1" and routed_env == "ar-stack"
    assert own_after["stdout"] == "1" and own_env == "testbase"
    assert sandbox._pool == {}, "shutdown closes the pool"


def test_a_routed_call_is_stamped_with_its_environment_from_the_first_call(tmp_path, monkeypatch):
    """The first call to a pool REPL, and the first after a restart, name the state they ran in."""
    import sys

    from auto_research.env import agent_conda_dir
    from auto_research.sandbox.envs import Environment, fingerprint
    from auto_research.sandbox.persistent import PersistentSandbox

    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)
    init = tmp_path / "own-init.sh"
    init.write_text("export OWN_READY=1\n")
    base = Environment(name="testbase", prefix=pathlib.Path(sys.prefix), owner="human", activate=init)
    folder = tmp_path / "defs" / "stack"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: x\nbase: testbase\n")
    (folder / "activate.sh").write_text("export STACK_READY=1\n")
    stack = B.build(B.read_definition(folder), conda_dir=agent_conda_dir(), conda_exe="conda",
                    base_envs={"testbase": base}, externals={}, jailed=True)
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work", environment=base)

    async def go():
        stamps = []
        try:
            await sandbox.call_tool("os", "getcwd", {}, environment=stack)
            stamps.append(sandbox.call_stamp)
            await sandbox.restart()
            await sandbox.call_tool("os", "getcwd", {}, environment=stack)
            stamps.append(sandbox.call_stamp)
            await sandbox.call_tool("os", "getcwd", {})
            stamps.append(sandbox.call_stamp)
        finally:
            await sandbox.shutdown()
        return stamps

    first, after_restart, own = asyncio.run(go())
    assert first == after_restart == fingerprint(stack)
    assert own == fingerprint(base) != first


def test_a_pool_repl_sources_its_activation_inside_the_jail(tmp_path, monkeypatch):
    import sys

    from auto_research.env import agent_conda_dir
    from auto_research.sandbox import jail
    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    if not jail.jail_enabled() or not jail.jail_available()[0]:
        pytest.skip("the jail is off or bwrap is unavailable")
    base = Environment(name="testbase", prefix=pathlib.Path(sys.prefix), owner="human")
    folder = tmp_path / "defs" / "stack"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: x\nbase: testbase\n")
    (folder / "activate.sh").write_text('export STACK_READY=1\nexport SEEN_HOME="$HOME"\n')
    stack = B.build(B.read_definition(folder), conda_dir=agent_conda_dir(), conda_exe="conda",
                    base_envs={"testbase": base}, externals={}, jailed=True)
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work", environment=base)

    async def go():
        try:
            return [await sandbox.call_tool("os", "getenv", {"key": k}, environment=stack)
                    for k in ("STACK_READY", "SEEN_HOME")]
        finally:
            await sandbox.shutdown()

    ready, home = asyncio.run(go())
    assert ready["stdout"] == "1", ready
    assert home["stdout"] == jail.JAIL_HOME_AT, "sourced in the jail, with the jail's home"


def _jailed_stack(tmp_path, *, activate=None, boot=None):
    """A derived pipeline environment over this interpreter, built into the test's agent conda dir."""
    import sys

    from auto_research.env import agent_conda_dir
    from auto_research.sandbox import jail
    from auto_research.sandbox.envs import Environment

    if not jail.jail_enabled() or not jail.jail_available()[0]:
        pytest.skip("the jail is off or bwrap is unavailable")
    base_init = tmp_path / "base-init.sh"
    base_init.write_text("export BASE_READY=1\n")
    base = Environment(name="testbase", prefix=pathlib.Path(sys.prefix), owner="human", activate=base_init)
    folder = tmp_path / "defs" / "stack"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: x\nbase: testbase\n")
    if activate is not None:
        (folder / "activate.sh").write_text(activate)
    if boot is not None:
        (folder / "boot.py").write_text(boot)
    stack = B.build(B.read_definition(folder), conda_dir=agent_conda_dir(), conda_exe="conda",
                    base_envs={"testbase": base}, externals={}, jailed=True)
    return base, stack


def test_a_coder_written_boot_file_runs_in_a_pool_repl_with_a_human_activation(tmp_path):
    from auto_research.sandbox.persistent import PersistentSandbox

    base, stack = _jailed_stack(tmp_path, boot="import os\nos.environ['BOOTED'] = '1'\n")
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work", environment=base)

    async def go():
        try:
            return await sandbox.call_tool("os", "getenv", {"key": "BOOTED"}, environment=stack)
        finally:
            await sandbox.shutdown()

    assert asyncio.run(go())["stdout"] == "1"


def test_a_worker_toolkit_call_routes_through_a_real_repl_pool(tmp_path, monkeypatch):
    """The provider over a real PersistentSandbox: a toolkit tool whose package names `stack` runs
    in that environment's REPL, and its receipt carries that REPL's stamp."""
    import re

    from auto_research.agents.react import SandboxToolProvider
    from auto_research.provenance import ReceiptStore
    from auto_research.sandbox.envs import fingerprint
    from auto_research.sandbox.persistent import PersistentSandbox
    from auto_research.sandbox.registry import ToolkitRegistry

    base, stack = _jailed_stack(tmp_path, activate="export STACK_READY=1\n")
    toolkit = tmp_path / "toolkit"
    pkg = toolkit / "routed_probe_utils"
    for sub in ("lib", "skills"):
        (pkg / sub).mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .skills import TOOL_MANIFEST\n")
    (pkg / "lib" / "__init__.py").write_text("")
    (pkg / "lib" / "probe.py").write_text("import os\ndef ready():\n    return {'stdout': os.environ.get('STACK_READY', 'absent')}\n")
    (pkg / "skills" / "__init__.py").write_text(
        'TOOL_MANIFEST = {"research_toolkit.routed_probe_utils.ready": {"module": "routed_probe_utils.lib.probe", '
        '"fn": "ready", "description": "d", "args_schema": {"type": "object", "properties": {}}}}\n')
    (pkg / "tool_config.yaml").write_text("env: stack\n")
    sandbox = PersistentSandbox(toolkit, tmp_path / "work", environment=base, environments=lambda: [stack])
    store = ReceiptStore(tmp_path / "receipts")
    provider = SandboxToolProvider(ToolkitRegistry(toolkit), sandbox, cwd=tmp_path / "work",
                                   receipts=store, receipt_source="w0")

    async def go():
        try:
            return await provider.execute("research_toolkit.routed_probe_utils.ready", {})
        finally:
            await sandbox.shutdown()

    import sys

    try:
        outcome = asyncio.run(go())
    finally:  # the registry imported the manifest in this process
        for name in [m for m in sys.modules if m.split(".")[0] == "routed_probe_utils"]:
            sys.modules.pop(name, None)
    assert "absent" not in outcome.text and "[ran in ar-stack" in outcome.text, outcome.text
    [handle] = re.findall(r"\bw0:r\d+", outcome.text)
    assert store.get(handle).env == fingerprint(stack)


def _pool_call(tmp_path, monkeypatch, activate, key="STACK_READY", calls=2):
    import sys

    from auto_research.env import agent_conda_dir
    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)
    init = tmp_path / "own-init.sh"
    init.write_text("export OWN_READY=1\n")
    base = Environment(name="testbase", prefix=pathlib.Path(sys.prefix), owner="human", activate=init)
    folder = tmp_path / "defs" / "stack"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: x\nbase: testbase\n")
    (folder / "activate.sh").write_text(activate)
    stack = B.build(B.read_definition(folder), conda_dir=agent_conda_dir(), conda_exe="conda",
                    base_envs={"testbase": base}, externals={}, jailed=True)
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work", environment=base)

    async def go():
        try:
            return [await sandbox.call_tool("os", "getenv", {"key": key}, environment=stack) for _ in range(calls)]
        finally:
            await sandbox.shutdown()

    return asyncio.run(go())


def test_an_activation_that_prints_leaves_the_repl_protocol_intact(tmp_path, monkeypatch):
    results = _pool_call(tmp_path, monkeypatch, 'echo "Stack configuration is complete."\nexport STACK_READY=1\n')
    assert [r["stdout"] for r in results] == ["1", "1"], results


def test_a_pool_repl_whose_activation_fails_says_why(tmp_path, monkeypatch):
    [result] = _pool_call(tmp_path, monkeypatch, "false\n", calls=1)
    assert result["exit_code"] == 1 and "did not finish" in result["stderr"], result


def test_a_boot_file_that_prints_at_the_c_level_leaves_the_protocol_intact(tmp_path):
    from auto_research.sandbox.persistent import PersistentSandbox

    base, stack = _jailed_stack(tmp_path, activate="export STACK_READY=1\n",
                                boot="import os\nos.write(1, b'Default fit statistic is set to: C-Statistic\\n')\n")
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", tmp_path / "work", environment=base)

    async def go():
        try:
            return [await sandbox.call_tool("os", "getenv", {"key": "STACK_READY"}, environment=stack)
                    for _ in range(2)]
        finally:
            await sandbox.shutdown()

    assert [r["stdout"] for r in asyncio.run(go())] == ["1", "1"]


def test_a_repl_runs_in_a_python_older_than_3_11(tmp_path, monkeypatch):
    """A pipeline environment may pin Python 3.9 (B5: Boost.Python built for it); the REPL starts
    there and keeps its working folder off sys.path all the same."""
    import shutil

    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)  # the stand-in prefix is not allow-listed
    old = shutil.which("python3.9")
    if old is None:
        pytest.skip("no Python 3.9 on this host")
    prefix = tmp_path / "py39"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "python").symlink_to(old)
    init = tmp_path / "init.sh"
    init.write_text("export READY=1\n")
    workdir = tmp_path / "work"
    (workdir / "json").mkdir(parents=True)  # a package planted in the working folder
    (workdir / "json" / "__init__.py").write_text("SHADOW = True\n")
    (tmp_path / "toolkit").mkdir()
    sandbox = PersistentSandbox(tmp_path / "toolkit", workdir,
                                environment=Environment(name="py39", prefix=prefix, owner="human", activate=init))

    async def go():
        try:
            return [await sandbox.call_tool("platform", "python_version", {}),
                    await sandbox.call_tool("importlib", "import_module", {"name": "json"})]
        finally:
            await sandbox.shutdown()

    version, imported = asyncio.run(go())
    assert version["exit_code"] == 0 and version["stdout"].startswith("3.9"), version
    assert "SHADOW" not in json.dumps(imported)


def test_a_tool_return_larger_than_64_kb_comes_back_whole(tmp_path, monkeypatch):
    """One tool return is one protocol line; a long one (a full environment report) arrives intact."""
    import sys

    from auto_research.sandbox.envs import Environment
    from auto_research.sandbox.persistent import PersistentSandbox

    monkeypatch.setattr("auto_research.sandbox.jail.jail_enabled", lambda: False)  # this interpreter is not allow-listed
    toolkit = tmp_path / "toolkit"
    (toolkit / "big_probe_utils").mkdir(parents=True)
    (toolkit / "big_probe_utils" / "__init__.py").write_text("")
    (toolkit / "big_probe_utils" / "big.py").write_text("def big(n):\n    return {'stdout': 'x' * n}\n")
    init = tmp_path / "init.sh"
    init.write_text("export READY=1\n")
    sandbox = PersistentSandbox(toolkit, tmp_path / "work",
                                environment=Environment(name="here", prefix=pathlib.Path(sys.prefix), owner="human",
                                                        activate=init))

    async def go():
        try:
            return await sandbox.call_tool("big_probe_utils.big", "big", {"n": 1_000_000})
        finally:
            await sandbox.shutdown()

    result = asyncio.run(go())
    assert result.get("exit_code") == 0 and len(result["stdout"]) == 1_000_000, str(result)[:300]


def test_a_folder_input_with_a_non_utf8_name_or_too_many_files_never_ends_the_call(tmp_path, monkeypatch):
    """A worker names a folder as an argument (B5: its /tmp); hashing it must leave the call running:
    a file name that is not UTF-8 is listed, a folder too large to trace is left out."""
    import os

    from auto_research.provenance import ReceiptStore
    from auto_research.provenance import store as store_module
    from auto_research.provenance.capture import inputs_before

    store = ReceiptStore(tmp_path / "receipts")
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / os.fsdecode(b"name-\xff.txt")).write_text("x")  # a byte no UTF-8 name holds
    state = inputs_before(store, {"folder": str(odd)})
    assert [i.basis for i in state.inputs] == ["listing"]

    monkeypatch.setattr(store_module, "_MAX_LISTED_FILES", 5)
    big = tmp_path / "big"
    big.mkdir()
    for i in range(10):
        (big / f"f{i}").write_text("x")
    assert inputs_before(store, {"folder": str(big)}).inputs == []
