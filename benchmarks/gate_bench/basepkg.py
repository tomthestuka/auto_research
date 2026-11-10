"""The synthetic base: a minimal, correct ``demo_utils`` toolkit package, plus the file
editing helpers every mutant builds its changeset with.

One of the corpus's two bases (CLAUDE.md §9.2 layout; the other is the real toolkit at a
pinned ref). The package is deliberately *real*: pure standard library, so it runs anywhere,
but with genuine arithmetic (a two-band count ratio with propagated errors, and an
error-weighted position angle on the 180-degree circle) that a defect can be planted in and
a committed test can actually detect. Written unmodified it passes every gate layer, which is
what makes it usable as the clean control.

The package and the repository are written by the one fixture-toolkit builder the test suite
also uses (``tests/fixtures/toolkit.py``, Phase 3 step 0a: one builder, not two); with its
defaults it writes this corpus's base byte for byte. What stays here is the benchmark's own:
the repository's cover README and the changeset editing helpers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.fixtures.toolkit import PKG_NAME, TOOL_NAME, git as _git, init_repo, write_base_package

__all__ = [
    "PKG_NAME", "TOOL_NAME", "add_catalog_writer", "append", "keep_only_passing_tests",
    "make_synthetic_repo", "patch", "write", "write_base_package",
]

#: The synthetic repository's README. The reviewer READS this file (measured 2026-08-27: it ran
#: `read_file` on it and was told "Throwaway base for the gate benchmark"). The planted
#: repository's own contents are a prompt channel like any other, and the fourth one found that
#: day — after the brief, the ToolSpec id and the worktree path. It reads like an ordinary
#: package README now.
_README = "demo_utils\n\nReduced-product summary helpers for an IXPE polarimetric census.\n"


def make_synthetic_repo(dest: Path) -> Path:
    """Initialise a throwaway git repo with one base commit (no toolkit package yet).

    The mutant then writes the package as untracked files, which is the shape the gate sees
    for a ``new_package`` authoring: whole-file scan scope. The committer is a plausible
    operator name ("gate-bench" was the fifth prompt channel found naming the benchmark,
    2026-08-28 audit), and the ignore rules mirror the real toolkit's, so byte-compiled output
    does not masquerade as part of a changeset.
    """
    return init_repo(dest, _README)


def add_catalog_writer(repo: Path, toolkit_root: Path, toolkit_sha: str) -> None:
    """Commit the toolkit's catalog writer (``obsdb_catalog``, whole, at ``toolkit_sha``) into
    ``repo``'s base, as the toolkit an obs-db tool is authored in carries it: the gate
    imports it from the tree under test to judge an obs-db write."""
    import io
    import tarfile

    archive = subprocess.run(
        ["git", "archive", "--format=tar", toolkit_sha, "obsdb_catalog"],
        cwd=str(toolkit_root), capture_output=True, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(repo, filter="data")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "catalog writer")


# ----------------------------------------------------------------------------
# Changeset editing helpers (used by every mutant builder, both bases)
# ----------------------------------------------------------------------------


def patch(path: Path, old: str, new: str, *, count: int = 1) -> None:
    """Replace ``old`` with ``new`` in ``path``, asserting it occurs exactly ``count`` times.

    Failing loudly on a missed anchor is what keeps a real-base mutant honest: if the pinned
    toolkit ref moves under the corpus, the mutant errors instead of silently planting
    nothing and recording a false miss.
    """
    text = Path(path).read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"anchor occurs {found} time(s), expected {count}, in {path}: {old[:80]!r}"
        )
    Path(path).write_text(text.replace(old, new, count), encoding="utf-8")


def append(path: Path, text: str) -> None:
    """Append ``text`` to ``path`` (creating it if absent)."""
    path = Path(path)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(existing + text, encoding="utf-8")


def write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

def keep_only_passing_tests(repo: Path, package: str, *, timeout: int = 300) -> list[str]:
    """Restrict a changeset's committed tests to the ones it passes, and say which were dropped.

    The corpus's rule for a behavioural defect: it ships the honest suite **minus the checks that
    would have caught it**. That is the shape the campaign ledgers record, where a defect reached
    the independent reviewer precisely because the author's own verification did not cover it, and
    it is what the "inadequate test" family means.

    Applying the rule mechanically rather than by hand-editing each test matters for two reasons.
    A hand-relaxed suite has to be re-relaxed whenever an honest check is added, and forgetting
    silently converts a reviewer-caught defect into a free Layer-B catch. And a rule stated once
    and applied uniformly is auditable, where a pile of individual deletions is not.

    Returns the node ids removed, so a builder can record what its plant is not pinned by.
    """
    import json as _json
    import os as _os
    import re as _re
    import sys as _sys

    tests_dir = Path(repo) / package / "tests"
    if not tests_dir.is_dir():
        return []
    dropped: list[str] = []
    # Iterate: removing one failing test can expose another (pytest -x stops at the first).
    for _ in range(60):
        # No cache, no bytecode: this pytest runs on the DEFECT leg only, and anything it left
        # behind would exist in the defect worktree and never in the control's. It did, until
        # 2026-09-03: `.pytest_cache/v/cache/lastfailed` named the very honest test the plant
        # breaks, the reviewer is rooted at the worktree with an unfiltered `list_dir`, and one
        # reviewer (qwen3_8_max, swapclean batch) cited that file verbatim in two catches. The
        # pair test that compares root listings without any artifact filter pins this.
        result = subprocess.run(
            [_sys.executable, "-m", "pytest", "-q", "--no-header", "-x", "-p", "no:cacheprovider",
             f"{package}/tests"],
            cwd=str(repo), capture_output=True, text=True, timeout=timeout,
            env={**_os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if result.returncode == 0:
            return dropped
        failing = _re.findall(r"^(?:FAILED|ERROR) ([^\s:]+)::([A-Za-z0-9_]+)",
                              result.stdout, flags=_re.M)
        if not failing:
            failing = _re.findall(r"^_+ ([A-Za-z0-9_]+) _+$", result.stdout, flags=_re.M)
            failing = [(None, name) for name in failing]
        if not failing:
            raise AssertionError(
                f"the suite fails but no test name could be read from it:\n{result.stdout[-800:]}"
            )
        for path_hint, name in failing[:1]:
            target = (Path(repo) / path_hint) if path_hint else None
            candidates = [target] if target and target.is_file() else sorted(tests_dir.glob("*.py"))
            for candidate in candidates:
                if _drop_test_function(candidate, name):
                    dropped.append(f"{candidate.name}::{name}")
                    break
            else:
                raise AssertionError(f"cannot find {name} in {package}/tests to drop")
    raise AssertionError("the suite still fails after dropping sixty checks")


def _drop_test_function(path: Path, name: str) -> bool:
    """Remove one top-level ``def <name>`` block from a test module. True when it was there."""
    text = path.read_text(encoding="utf-8")
    marker = f"\ndef {name}("
    start = text.find(marker)
    if start < 0:
        return False
    body = start + 1
    nxt = text.find("\ndef ", body + 1)
    end = len(text) if nxt < 0 else nxt + 1
    path.write_text(text[:body] + text[end:], encoding="utf-8")
    return True
