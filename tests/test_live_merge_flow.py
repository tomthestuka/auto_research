"""Env-gated LIVE test of the authoring auto-reset (todo #5) — drives the REAL deterministic
gate (no stub) with scripted "bad diffs" and asserts the working tree self-heals.

Activate with:
    AUTO_RESEARCH_LIVE_MERGE_FLOW_TESTS=1 pytest tests/test_live_merge_flow.py -q

Unlike the deterministic ``test_merge_flow.py`` (which stubs ``run_gate``), this runs the real
``gate.run_gate`` so the gate↔reset interaction is exercised end-to-end. It operates on a
**throwaway clone** of the configured ``toolkit_root`` checked out on a fresh ``ai`` branch —
never the live checkout — and feeds diffs that fail Layer A (a fabrication + a structural
break), so the gate rejects without needing the science stack for Layer-B tests.

``ScriptedCoder`` is the reusable shortcut: it applies one chosen edit to the worktree per
``query``, so any future deep authoring-flow patch can be live-tested by scripting the coder
instead of spawning a real LLM. Requires the analyst env (HEASoft) available, like other live
tests.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from auto_research.graph.runtime import load_orchestration_config
from auto_research.sandbox.merge_flow import propose_and_pr
from auto_research.schemas import ToolSpec


def _coder_member(model: str):
    """A stand-in author member, so the flow needs no configured ``coder`` role to be exercised."""
    from auto_research.models.types import RoleModelConfig

    return RoleModelConfig(
        name="test_coder", provider="anthropic", harness="claude_sdk", model=model
    )

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_MERGE_FLOW_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_MERGE_FLOW_TESTS=1 to run the live authoring-reset test",
)


class ScriptedCoder:
    """Stand-in for the SDK coder: applies one scripted edit to the worktree per ``query`` so
    deterministic 'bad diffs' flow through the REAL gate without an LLM. The reusable shortcut
    for live-testing any deep authoring-flow change."""

    def __init__(self, worktree, edits):
        self.worktree = Path(worktree)
        self.edits = list(edits)
        self._i = 0
        self.queries: list[str] = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def query(self, message, session_id="default"):
        self.queries.append(message)
        if self._i < len(self.edits):
            self.edits[self._i](self.worktree)
            self._i += 1

    async def receive_response(self):
        for _ in ():  # the author "works" by applying the scripted edit in query()
            yield _


class _FakeLead:
    async def clarify_for_author(self, message, *, kind="clarify_code"):
        return "n/a — scripted coder never escalates"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _git_out(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


def _scratch_clone(tmp_path) -> Path:
    """A throwaway shallow clone of the configured toolkit_root on a fresh ``ai`` branch."""
    root = Path(load_orchestration_config().get("toolkit_root", "")).expanduser()
    if not (root / ".git").exists():
        pytest.skip(f"toolkit_root {root} is not a git checkout")
    if not (root / "xspec_utils" / "lib").is_dir():
        pytest.skip(f"{root} has no xspec_utils/lib to write a probe into")
    clone = tmp_path / "toolkit_clone"
    subprocess.run(
        ["git", "clone", "--depth", "1", str(root), str(clone)],
        check=True, capture_output=True, text=True,
    )
    _git(clone, "config", "user.email", "live@test")
    _git(clone, "config", "user.name", "live")
    _git(clone, "checkout", "-q", "-B", "ai")  # create-or-reset (clone may already be on ai)
    return clone


def _spec():
    return ToolSpec(
        science_context="live reset check",
        expected_behavior="live_reset_probe()",
        proposed_tool_name="live_reset_probe",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
    )


def _bad_diff_fabrication(worktree: Path) -> None:
    # np.random in a *_utils file → no-fabrication scan rejects (Layer A). Also no manifest
    # entry → structural rejects too, so Layer-B tests (science stack) never run.
    (worktree / "xspec_utils" / "lib" / "live_reset_probe.py").write_text(
        "import numpy as np\n\n\ndef live_reset_probe():\n    return np.random.rand(3)\n"
    )


def _bad_diff_structural(worktree: Path) -> None:
    # A second rework: a lib fn with no TOOL_MANIFEST entry + scratch outside any *_utils pkg.
    (worktree / "xspec_utils" / "lib" / "live_reset_probe2.py").write_text(
        "def live_reset_probe2():\n    return 1\n"
    )
    (worktree / "scratch_outside_pkg.txt").write_text("worker scratch — must be cleaned\n")


def test_live_authoring_reset_after_bad_diffs(tmp_path):
    """Real gate + scripted bad diffs: the gate rejects and the working tree resets to HEAD,
    with the discarded work preserved as a patch artifact."""
    clone = _scratch_clone(tmp_path)
    head = _git_out(clone, "rev-parse", "HEAD").strip()
    artifacts = tmp_path / "authoring"

    async def _review_must_not_run(worktree, spec, gate):
        raise AssertionError("review should be unreachable — the gate must reject the bad diffs")

    result = asyncio.run(
        propose_and_pr(
            _spec(),
            lead=_FakeLead(),
            toolkit_root=clone,
            worktree_base=artifacts,
            coder=_coder_member("x"),
            direct_branch="ai",
            gate_rework_cap=1,  # initial diff + one rework diff, then fail → reset
            run_review=_review_must_not_run,
            sdk_client_factory=lambda options: ScriptedCoder(
                clone, [_bad_diff_fabrication, _bad_diff_structural]
            ),
        )
    )

    assert result.status == "failed"  # the REAL gate rejected the bad diffs
    # Confirm it was a genuine gate REJECTION (cap exhausted), not the unexpected-error fallback
    # — i.e. the real gate actually ran and judged the diffs.
    assert "gate rework cap exhausted" in result.reason, result.reason
    assert result.gate_report is not None and not result.gate_report.passed
    assert _git_out(clone, "status", "--porcelain") == ""  # tree reset to a clean state
    assert _git_out(clone, "rev-parse", "HEAD").strip() == head  # nothing committed
    assert not (clone / "xspec_utils" / "lib" / "live_reset_probe.py").exists()
    assert not (clone / "scratch_outside_pkg.txt").exists()
    assert list(artifacts.glob("tool_author_failed_*.patch"))  # discarded work preserved
