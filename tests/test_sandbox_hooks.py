"""Deterministic tests for the install-script sandbox hooks (increment 0/1).

Covers the domain-agnostic REPL-init and env-probe machinery — the parts decidable
without the science stack: the ``bash_exec`` description injection, ``check_analyze_probe``
skip/pass/fail on a temp config (using ``sys.executable`` so no analyze_env is needed), and
``repl_init_py`` execution at REPL boot.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.sandbox.runner import check_analyze_probe


def test_bash_exec_description_injects_probe(monkeypatch):
    import auto_research.sandbox.builtin_server as bs

    monkeypatch.setenv("AR_ANALYZE_PROBE", "import mypkg, mypkg_utils")
    desc = bs._bash_exec_description()
    assert "import mypkg, mypkg_utils" in desc
    assert "FITS" not in desc  # neutralized domain term


def test_bash_exec_description_neutral_without_probe(monkeypatch):
    import auto_research.sandbox.builtin_server as bs

    monkeypatch.delenv("AR_ANALYZE_PROBE", raising=False)
    desc = bs._bash_exec_description()
    assert "importing your toolkit package" in desc


def _write_orch(tmp_path, probe: str) -> str:
    cfg = tmp_path / "orchestration.yaml"
    # No analyze_env / analyze_ini_sh ⇒ the check uses sys.executable + os.environ (pure python).
    cfg.write_text(f"analyze_probe: {probe!r}\n", encoding="utf-8")
    return str(cfg)


def test_analyze_probe_skips_when_empty(tmp_path):
    # Empty probe ⇒ no-op escape hatch (a domain / lit-only deployment).
    check_analyze_probe(config_path=_write_orch(tmp_path, ""))


def test_analyze_probe_missing_config_is_noop(tmp_path):
    check_analyze_probe(config_path=str(tmp_path / "does_not_exist.yaml"))


def test_analyze_probe_passes_on_good_import(tmp_path):
    check_analyze_probe(config_path=_write_orch(tmp_path, "import sys, json"))


def test_analyze_probe_raises_on_bad_import(tmp_path):
    with pytest.raises(RuntimeError, match="analyze_probe"):
        check_analyze_probe(config_path=_write_orch(tmp_path, "import __no_such_module_xyz__"))


def test_repl_init_py_runs_at_boot(tmp_path):
    """A configured repl_init_py file is exec'd once at REPL subprocess boot."""
    sentinel = tmp_path / "ran.txt"
    repl_init = tmp_path / "repl_init.py"
    repl_init.write_text(f"open({str(sentinel)!r}, 'w').write('yes')\n", encoding="utf-8")
    ini = tmp_path / "env.sh"
    ini.write_text("#!/bin/bash\nexport AR_TEST=1\n", encoding="utf-8")

    async def _run():
        sb = PersistentSandbox(
            toolkit_root=str(tmp_path),
            workdir=str(tmp_path / "wd"),
            analyze_env=None,
            analyze_ini_sh=str(ini),
            repl_init_py=str(repl_init),
        )
        await sb.start()
        res = await sb.call_tool("os", "getcwd", {})
        await sb.shutdown()
        return res

    res = asyncio.run(_run())
    assert res.get("exit_code") == 0
    assert sentinel.exists() and sentinel.read_text() == "yes"


def test_repl_init_py_absent_is_bare_repl(tmp_path):
    """No repl_init_py ⇒ AR_REPL_INIT_PY unset ⇒ bare REPL still serves calls."""
    ini = tmp_path / "env.sh"
    ini.write_text("#!/bin/bash\n", encoding="utf-8")

    async def _run():
        sb = PersistentSandbox(
            toolkit_root=str(tmp_path),
            workdir=str(tmp_path / "wd"),
            analyze_env=None,
            analyze_ini_sh=str(ini),
            repl_init_py=None,
        )
        await sb.start()
        res = await sb.call_tool("os", "getcwd", {})
        await sb.shutdown()
        return res

    assert asyncio.run(_run()).get("exit_code") == 0
