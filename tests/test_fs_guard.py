"""Deterministic tests for the shared filesystem allow-list (sandbox/fs_guard.PathGuard)."""

import pytest

from auto_research.sandbox.fs_guard import PathGuard, load_fs_allowlist


def test_allows_inside_roots_denies_outside(tmp_path):
    tk = tmp_path / "research_toolkit"
    data = tmp_path / "obsdb"
    (tk / "xspec_utils" / "lib").mkdir(parents=True)
    data.mkdir()
    g = PathGuard([tk, data])
    assert g.active
    assert g.is_allowed(tk / "xspec_utils" / "lib" / "red.py")
    assert g.is_allowed(data / "IXPE" / "01002901" / "evt.fits")
    assert g.is_allowed(tk)  # the root itself
    assert not g.is_allowed(tmp_path / "auto_research" / "sandbox" / "gate.py")
    assert not g.is_allowed("/etc/passwd")


def test_denies_governance_basenames_even_inside_root(tmp_path):
    tk = tmp_path / "research_toolkit"
    (tk / "xspec_utils").mkdir(parents=True)
    g = PathGuard([tk])
    assert not g.is_allowed(tk / "CLAUDE.md")
    assert not g.is_allowed(tk / "xspec_utils" / "DOCS.md")
    assert not g.is_allowed(tk / "AGENTS.md")
    assert g.is_allowed(tk / "README.md")  # a non-governance markdown file is fine


def test_empty_allowlist_is_inactive():
    g = PathGuard([])
    assert not g.active
    assert g.is_allowed("/anything/at/all")  # inactive guard allows everything
    assert g.forbidden_in_command("cat /etc/shadow") is None


def test_forbidden_in_command(tmp_path):
    tk = tmp_path / "research_toolkit"
    tk.mkdir()
    g = PathGuard([tk])
    assert g.forbidden_in_command('python -c "import xspec_utils"') is None  # no abs path named
    assert g.forbidden_in_command(f"pytest {tk}/xspec_utils/tests -q") is None  # inside a root
    bad = g.forbidden_in_command(f"cat {tmp_path}/auto_research/sandbox/gate.py")
    assert bad is not None and "auto_research" in bad


def test_real_config_denies_auto_research_allows_toolkit():
    """The repo's orchestration.yaml agent_fs_allow includes research_toolkit but NOT
    auto_research — so the orchestration repo is denied via either mount path."""
    roots = load_fs_allowlist()
    if not roots:
        pytest.skip("agent_fs_allow not configured")
    g = PathGuard(roots)
    assert not g.is_allowed("/data/fzhou/auto_research/src/auto_research/sandbox/gate.py")
    assert not g.is_allowed("/nfsdata1/fzhou/auto_research/src/auto_research/sandbox/gate.py")
    assert g.is_allowed("/data/fzhou/research_toolkit/xspec_utils/lib/red_ixpe.py")
