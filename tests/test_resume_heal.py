"""Deterministic tests for Stage 2 durable-resume branch-safety (§12).

Real git on a temp repo (no network, no LLM): `heal_toolkit_branch_if_dirty` tidies a dirty
authoring branch to the persisted per-attempt base-ref — dropping a commit the killed attempt
slipped while keeping tools committed earlier — and no-ops on a clean / non-git / wrong-branch
tree; `check_toolkit_branch_clean` raises the distinct `ToolkitBranchUnclean` on a dirty tree so
the mid-run path can degrade instead of crashing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from auto_research.sandbox import merge_flow as mf
from auto_research.sandbox.merge_flow import (
    ToolkitBranchUnclean,
    _base_ref_path,
    _read_persisted_base_ref,
    check_toolkit_branch_clean,
    heal_toolkit_branch_if_dirty,
)


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _init_repo(root: Path, branch: str = "ai") -> str:
    """Init a repo on ``branch`` with one commit; return that commit sha."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "checkout", "-q", "-b", branch)
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("v1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c1")
    return _git(root, "rev-parse", "HEAD")


def _persist_base_ref(run_dir: Path, ref: str) -> None:
    d = run_dir / "authoring"
    d.mkdir(parents=True, exist_ok=True)
    _base_ref_path(d).write_text(ref, encoding="utf-8")


def test_heal_resets_to_base_ref_drops_slip_keeps_earlier(tmp_path):
    root = tmp_path / "toolkit"
    base = _init_repo(root)                 # C1 == the recorded per-attempt base
    run_dir = tmp_path / "run"
    _persist_base_ref(run_dir, base)
    # Simulate the killed attempt: a slipped commit C2 + a dirty working tree on top.
    (root / "f.txt").write_text("v2\n"); _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "slip")
    (root / "f.txt").write_text("v3-dirty\n")   # uncommitted tracked edit
    (root / "junk.py").write_text("x\n")        # untracked
    assert mf._is_dirty(root)

    res = heal_toolkit_branch_if_dirty(run_dir, root=root, branch="ai")

    assert res.healed and res.reason == "healed" and res.base_ref == base
    assert not mf._is_dirty(root)                       # clean again
    assert _git(root, "rev-parse", "HEAD") == base      # the slipped commit C2 was dropped
    assert (root / "f.txt").read_text() == "v1\n"       # base content restored
    assert not (root / "junk.py").exists()              # untracked cleaned
    assert res.patch_path and res.patch_path.exists()   # discarded work saved
    assert res.patch_path.name.startswith("resume_heal_")


def test_heal_falls_back_to_head_without_base_ref(tmp_path):
    root = tmp_path / "toolkit"
    _init_repo(root)
    (root / "f.txt").write_text("v2\n"); _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "c2")
    head = _git(root, "rev-parse", "HEAD")
    (root / "f.txt").write_text("dirty\n")              # uncommitted
    res = heal_toolkit_branch_if_dirty(tmp_path / "run", root=root, branch="ai")  # no base-ref file
    assert res.healed and res.base_ref is None
    assert _git(root, "rev-parse", "HEAD") == head      # commits kept (reset to HEAD)
    assert (root / "f.txt").read_text() == "v2\n"       # only the uncommitted edit dropped


def test_heal_noop_on_clean_tree(tmp_path):
    root = tmp_path / "toolkit"
    _init_repo(root)
    res = heal_toolkit_branch_if_dirty(tmp_path / "run", root=root, branch="ai")
    assert not res.healed and res.reason == "clean"


def test_heal_noop_on_non_git(tmp_path):
    root = tmp_path / "plain"; root.mkdir()
    res = heal_toolkit_branch_if_dirty(tmp_path / "run", root=root, branch="ai")
    assert not res.healed and res.reason == "not_git"


def test_heal_noop_on_wrong_branch(tmp_path):
    root = tmp_path / "toolkit"
    _init_repo(root, branch="main")                     # not the authoring branch
    (root / "f.txt").write_text("dirty\n")
    res = heal_toolkit_branch_if_dirty(tmp_path / "run", root=root, branch="ai")
    assert not res.healed and res.reason == "wrong_branch"
    assert mf._is_dirty(root)                           # left untouched for the human/preflight


def test_base_ref_persist_roundtrip(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "authoring").mkdir(parents=True)
    _base_ref_path(run_dir / "authoring").write_text("abc123", encoding="utf-8")
    assert _read_persisted_base_ref(run_dir) == "abc123"
    assert _read_persisted_base_ref(tmp_path / "absent") is None


def test_check_branch_clean_raises_unclean_on_dirty(tmp_path):
    root = tmp_path / "toolkit"
    _init_repo(root, branch="ai")
    (root / "f.txt").write_text("dirty\n")
    with pytest.raises(ToolkitBranchUnclean):
        check_toolkit_branch_clean(root, "ai")


def test_check_branch_clean_passes_when_clean(tmp_path):
    root = tmp_path / "toolkit"
    _init_repo(root, branch="ai")
    check_toolkit_branch_clean(root, "ai")              # no raise on a clean authoring branch
