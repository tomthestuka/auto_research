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
    """The configured agent_fs_allow includes the toolkit_root but NOT auto_research — so the
    orchestration repo is denied. Paths derive from the live config, so the test is portable."""
    from pathlib import Path

    from auto_research.graph.runtime import load_orchestration_config

    roots = load_fs_allowlist()
    if not roots:
        pytest.skip("agent_fs_allow not configured")
    g = PathGuard(roots)
    # This test's repo IS auto_research; its root is deliberately absent from the allow-list.
    repo_root = Path(__file__).resolve().parents[1]
    assert not g.is_allowed(repo_root / "src" / "auto_research" / "sandbox" / "gate.py")
    # The configured toolkit_root is allowed (same config that produced `roots`).
    toolkit_root = load_orchestration_config().get("toolkit_root")
    if toolkit_root:
        assert g.is_allowed(Path(toolkit_root) / "xspec_utils" / "lib" / "red_ixpe.py")


# ── F10: reads follow the allow-list, writes follow the role's own work folder ──


def test_f10_reads_and_writes_are_separate_sets(tmp_path):
    """The guard made no read/write distinction, which was only safe while every graph role was
    read-only. F5e gives the reviewer `file_edit` on its own `report.tex`, and once one formerly
    read-only role becomes a writer, "read-only role" stops being the boundary and *its own work
    product* becomes it."""
    from auto_research.sandbox.fs_guard import PathGuard

    run = tmp_path / "run"
    mine = run / "reviewer" / "report"
    other = run / "analyst_worker_0"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)

    guard = PathGuard([run], writable=[mine])

    # Reads stay broad: a role must be able to read its own run logs (ruling 2026-09-03).
    assert guard.is_allowed(other / "monitor.jsonl")
    assert guard.is_allowed(mine / "report.tex")
    # Writes are its own folder only.
    assert guard.is_writable(mine / "report.tex")
    assert not guard.is_writable(other / "report.jsonl")
    assert not guard.is_writable(run / "anything.txt")


def test_f10_a_role_with_no_writable_set_can_write_nothing(tmp_path):
    from auto_research.sandbox.fs_guard import PathGuard

    guard = PathGuard([tmp_path])
    assert guard.is_allowed(tmp_path / "x")
    assert not guard.is_writable(tmp_path / "x")


def test_f10_an_unconfigured_guard_is_inactive_for_writes_too(tmp_path):
    """No roots means unconfigured, and an unconfigured environment must be unaffected."""
    from auto_research.sandbox.fs_guard import PathGuard

    assert PathGuard([]).is_writable(tmp_path / "x")


def test_f10_a_governance_file_is_never_writable(tmp_path):
    from auto_research.sandbox.fs_guard import PathGuard

    guard = PathGuard([tmp_path], writable=[tmp_path])
    assert guard.is_writable(tmp_path / "report.tex")
    assert not guard.is_writable(tmp_path / "CLAUDE.md")
    assert not guard.is_writable(tmp_path / "DOCS.md")


def test_f10_file_edit_refuses_a_path_outside_the_writable_scope(tmp_path):
    """The second, independent bound: the tool never takes a path from the model, but a mis-wired
    provider must not be able to widen the write surface either."""
    import asyncio

    from auto_research.agents.react import execute_file_edit
    from auto_research.sandbox.fs_guard import PathGuard

    allowed = tmp_path / "mine"
    allowed.mkdir()
    guard = PathGuard([tmp_path], writable=[allowed])

    out = asyncio.run(execute_file_edit(
        tmp_path / "elsewhere" / "report.tex", {"content": "x", "old_text": None, "new_text": None},
        guard,
    ))
    assert out.is_error
    assert "writable scope" in out.text

    ok = asyncio.run(execute_file_edit(
        allowed / "report.tex", {"content": "\\documentclass{article}", "old_text": None,
                                 "new_text": None}, guard,
    ))
    assert not ok.is_error
    assert (allowed / "report.tex").is_file()
