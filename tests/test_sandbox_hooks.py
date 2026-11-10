"""Deterministic tests for the analysis environment's hooks.

Covers the domain-agnostic boot-file and env-probe machinery — the parts decidable without the
science stack: the ``bash_exec`` description injection, ``check_analysis_probe`` skip/pass/fail on
a fixture toolkit whose default environment layers on this interpreter, ``check_human_envs``, the
default environment's activation read back (``analysis_activation_env``), and an environment's
``boot.py`` execution at REPL boot.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from auto_research import toolkit_contract
from auto_research.config_paths import CONFIG_DIR_ENV
from auto_research.sandbox.envs import Environment
from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.sandbox.runner import analysis_activation_env, check_analysis_probe
from tests.fixtures import toolkit as fixture


from auto_research.sandbox import jail

#: The tests that start a process: it runs inside the OS jail (unconditional, D25).
jail_required = pytest.mark.skipif(not jail.jail_available()[0],
                                   reason="the OS jail is required and bwrap cannot run here")


@pytest.fixture(autouse=True)
def _allow_this_interpreter(monkeypatch):
    """These tests boot a REPL in this *orchestration* interpreter, standing in for a human
    environment: the jail's allow-list names it read-only, as a configured one names its prefix,
    rather than reading gitignored machine-local config."""
    monkeypatch.setattr(jail, "_config", lambda: {"agent_fs_allow": [sys.prefix], "agent_fs_readonly": [sys.prefix]})


def test_bash_exec_description_injects_probe(monkeypatch):
    import auto_research.sandbox.builtin_server as bs

    monkeypatch.setenv("AR_ENV_PROBE", "import mypkg, mypkg_utils")
    desc = bs._bash_exec_description()
    assert "import mypkg, mypkg_utils" in desc
    assert "FITS" not in desc  # neutralized domain term


def test_bash_exec_description_neutral_without_probe(monkeypatch):
    import auto_research.sandbox.builtin_server as bs

    monkeypatch.delenv("AR_ENV_PROBE", raising=False)
    desc = bs._bash_exec_description()
    assert "importing your toolkit package" in desc


@pytest.fixture
def toolkit_config(tmp_path, monkeypatch):
    """``make(probe=..., activate=..., **overrides) -> orchestration.yaml``: the fixture toolkit,
    its default environment (``envs/demo/``, derived from this interpreter) given ``probe`` and
    ``activate``, and this process configured to run on it."""

    def make(*, probe: str | None = None, activate: str | None = None, **overrides) -> Path:
        root = tmp_path / "toolkit"
        if not root.exists():
            fixture.build_toolkit(root, env_base=Path(sys.prefix).name)
        env = root / "envs" / fixture.ENV_ID
        if probe is not None:
            with (env / "env.yaml").open("a", encoding="utf-8") as handle:
                handle.write(f"probe: {probe!r}\n")
        if activate is not None:
            (env / "activate.sh").write_text(activate, encoding="utf-8")
        config = fixture.write_config(tmp_path / "config", root, analysis_prefix=Path(sys.prefix), **overrides)
        monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
        toolkit_contract.reset()
        return config / "orchestration.yaml"

    yield make
    toolkit_contract.reset()


def test_analysis_probe_skips_when_the_definition_has_none(toolkit_config):
    # No probe ⇒ nothing runs (a domain / lit-only deployment).
    toolkit_config()
    check_analysis_probe()


@jail_required
def test_analysis_probe_passes_on_good_import(toolkit_config):
    # The toolkit is on PYTHONPATH, as in the REPL: its package imports.
    toolkit_config(probe=f"import json, {fixture.PKG_NAME}")
    check_analysis_probe()


@jail_required
def test_analysis_probe_raises_on_bad_import(toolkit_config):
    with pytest.raises(RuntimeError, match="probe `import __no_such_module_xyz__` of ar-demo exited"):
        toolkit_config(probe="import __no_such_module_xyz__")
        check_analysis_probe()


@jail_required
def test_analysis_probe_runs_after_the_activation(toolkit_config):
    """The probe sees what the default environment's activation exports (sourced by the launcher)."""
    toolkit_config(probe="import os; assert os.environ['DEMO_PROBE_SEEN'] == '1'",
                   activate="export DEMO_PROBE_SEEN=1\n")
    check_analysis_probe()


@jail_required
def test_activation_env_reads_back_what_the_activation_sets(toolkit_config):
    import yaml

    path = toolkit_config(activate="export DEMO_VAR='a;b' DEMO_PARAMS=\"$HOME/params;/opt/sys\"\n")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    activated = analysis_activation_env(config)
    assert activated["DEMO_VAR"] == "a;b"
    # As the activation set it in its jail, unless a consumer names the home of its own jail.
    assert activated["DEMO_PARAMS"] == f"{jail.JAIL_HOME_AT}/params;/opt/sys"
    assert analysis_activation_env(config, home="/elsewhere")["DEMO_PARAMS"] == "/elsewhere/params;/opt/sys"
    # The launcher's own variables stay out: a caller adds its own HOME and toolkit path.
    assert "HOME" not in activated and not any(k.startswith("AR_") for k in activated)
    assert str(fixture.TOOLKIT_NAME) not in activated.get("PYTHONPATH", "")


def test_human_envs_refuses_an_undeclared_base(toolkit_config):
    from auto_research.env import check_human_envs

    toolkit_config()
    check_human_envs()  # the fixture declares the base its definition names
    toolkit_config(human_envs={"other": sys.prefix})
    with pytest.raises(RuntimeError, match="does not declare"):
        check_human_envs()


def test_human_envs_refuses_a_prefix_without_python(toolkit_config, tmp_path):
    from auto_research.env import check_human_envs

    toolkit_config(human_envs={Path(sys.prefix).name: sys.prefix, "empty": str(tmp_path / "nowhere")})
    with pytest.raises(RuntimeError, match="human_envs.empty"):
        check_human_envs()


def _human_env(tmp_path, *, boot: Path | None = None) -> Environment:
    ini = tmp_path / "env.sh"
    ini.write_text("#!/bin/bash\nexport AR_TEST=1\n", encoding="utf-8")
    return Environment(name="host", prefix=Path(sys.prefix), activate=ini, boot=boot)


@jail_required
def test_boot_file_runs_at_boot(tmp_path):
    """An environment's boot file is exec'd once at REPL subprocess boot (in the jail, where the
    working folder is what it writes)."""
    sentinel = tmp_path / "wd" / "ran.txt"
    boot = tmp_path / "boot.py"
    boot.write_text(f"open({str(sentinel)!r}, 'w').write('yes')\n", encoding="utf-8")

    async def _run():
        sb = PersistentSandbox(toolkit_root=str(tmp_path), workdir=str(tmp_path / "wd"),
                               environment=_human_env(tmp_path, boot=boot))
        await sb.start()
        res = await sb.call_tool("os", "getcwd", {})
        await sb.shutdown()
        return res

    res = asyncio.run(_run())
    assert res.get("exit_code") == 0
    assert sentinel.exists() and sentinel.read_text() == "yes"


@jail_required
def test_no_boot_file_is_bare_repl(tmp_path):
    """No boot file ⇒ AR_BOOT_PY unset ⇒ bare REPL still serves calls."""

    async def _run():
        sb = PersistentSandbox(toolkit_root=str(tmp_path), workdir=str(tmp_path / "wd"),
                               environment=_human_env(tmp_path))
        await sb.start()
        res = await sb.call_tool("os", "getcwd", {})
        await sb.shutdown()
        return res

    assert asyncio.run(_run()).get("exit_code") == 0


@jail_required
def test_non_json_return_fails_its_call_and_the_sandbox_survives(tmp_path):
    """A return the REPL cannot serialise fails that one call; the next call is still served."""
    (tmp_path / "badret.py").write_text("def f():\n    return {'stdout': 'x', 's': {1, 2}}\n")

    async def _run():
        sb = PersistentSandbox(toolkit_root=str(tmp_path), workdir=str(tmp_path / "wd"),
                               environment=_human_env(tmp_path))
        await sb.start()
        bad = await sb.call_tool("badret", "f", {})
        good = await sb.call_tool("os", "getcwd", {})
        await sb.shutdown()
        return bad, good

    bad, good = asyncio.run(_run())
    assert bad.get("exit_code") == 1 and "not JSON" in bad.get("stderr", "")
    assert good.get("exit_code") == 0


def test_human_envs_refuses_a_name_that_is_no_environment_name(toolkit_config):
    from auto_research.env import check_human_envs

    toolkit_config(human_envs={Path(sys.prefix).name: sys.prefix, "Not A Name": sys.prefix})
    with pytest.raises(RuntimeError, match="'Not A Name' is not an environment name"):
        check_human_envs()


def test_human_envs_refuses_a_prefix_in_the_runtimes_conda_folder(toolkit_config):
    from auto_research.env import agent_conda_dir, check_human_envs

    built = agent_conda_dir() / "envs" / "ar-demo"
    toolkit_config(human_envs={Path(sys.prefix).name: sys.prefix, "built": str(built)})
    with pytest.raises(RuntimeError, match="lies in the runtime's conda folder"):
        check_human_envs()


def test_a_malformed_definition_is_named_by_its_file(toolkit_config, tmp_path):
    from auto_research.env import check_human_envs, default_definition

    toolkit_config()
    env_yaml = tmp_path / "toolkit" / "envs" / fixture.ENV_ID / "env.yaml"
    env_yaml.write_text("purpose: [unclosed\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as refused:
        check_human_envs()
    assert str(env_yaml) in str(refused.value) and "['" not in str(refused.value)
    with pytest.raises(ValueError) as unreadable:
        default_definition()
    assert str(env_yaml) in str(unreadable.value) and "['" not in str(unreadable.value)
