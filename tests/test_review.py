"""Deterministic tests for the Phase-2 step-4 provider-decorrelated co-lead review.

Three seams, no live LLM/network:
  * ``select_decorrelated_member`` — the trust-boundary routing (author ≠ reviewer provider).
  * ``build_diff_bundle`` — the worktree IO (new file in full, modified file as a diff),
    exercised over a throwaway git repo like ``tests/test_gate.py``.
  * ``make_review_hook`` — the hook wiring, with an injected co-lead backed by a fake model,
    so the ReAct review path runs without tools being called and the verdict is mapped.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from auto_research.agents.roles import AnalystColeadAgent
from auto_research.models import ModelResponse
from auto_research.sandbox.review import (
    build_diff_bundle,
    make_review_hook,
    select_decorrelated_member,
)
from auto_research.schemas import GateReport, ToolReviewVerdict, ToolSpec, ValidationDataset


# ── helpers ─────────────────────────────────────────────────────────────────


class _FakeModel:
    """Returns a fixed structured object; records the last messages it saw."""

    def __init__(self, obj):
        self.obj = obj
        self.messages = None

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        self.messages = messages
        return ModelResponse(parsed=self.obj)


def _handle(provider, name):
    return SimpleNamespace(config=SimpleNamespace(provider=provider, name=name, model=name))


def _group(*handles):
    return SimpleNamespace(handles=list(handles))


def _spec(**kw):
    base = dict(
        science_context="ctx",
        expected_behavior="beh",
        proposed_tool_name="my_tool",
        target_domain="demo_utils",
        disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="0001")],
    )
    base.update(kw)
    return ToolSpec(**base)


def _git(repo, *args):
    import subprocess

    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "research_toolkit"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # A tracked baseline file we will later modify.
    pkg = repo / "demo_utils" / "lib"
    pkg.mkdir(parents=True)
    (pkg / "demo_help.py").write_text("def existing():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


# ── decorrelation routing (the trust boundary) ──────────────────────────────


def test_select_decorrelated_member_picks_non_author_provider():
    group = _group(_handle("anthropic", "claude"), _handle("openai", "gpt"))
    chosen = select_decorrelated_member(group, "anthropic")
    assert chosen.config.provider == "openai"


def test_select_decorrelated_member_raises_when_all_share_author_provider():
    group = _group(_handle("anthropic", "claude-a"), _handle("anthropic", "claude-b"))
    with pytest.raises(ValueError, match="provider-decorrelated"):
        select_decorrelated_member(group, "anthropic")


# ── diff bundle (worktree IO) ───────────────────────────────────────────────


def test_build_diff_bundle_shows_new_full_and_modified_diff(tmp_path):
    repo = _make_repo(tmp_path)
    # New file (untracked) under a *_utils package + a modification to the tracked file.
    (repo / "demo_utils" / "skills").mkdir(parents=True)
    (repo / "demo_utils" / "skills" / "__init__.py").write_text(
        'TOOL_MANIFEST = {"demo_utils.my_tool": {}}\n'
    )
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text(
        "def existing():\n    return 1\n\ndef my_tool(obsid):\n    return existing()\n"
    )

    bundle = build_diff_bundle(repo)

    assert "NEW FILE: demo_utils/skills/__init__.py" in bundle
    assert "TOOL_MANIFEST" in bundle  # full content of the new file is included
    assert "MODIFIED: demo_utils/lib/demo_help.py" in bundle
    assert "+def my_tool(obsid):" in bundle  # the modification shows as an added diff line


def test_build_diff_bundle_empty_when_no_changes(tmp_path):
    repo = _make_repo(tmp_path)
    assert build_diff_bundle(repo) == "(no toolkit changes detected in the worktree)"


# ── make_review_hook wiring (injected co-lead, fake model) ──────────────────


def test_review_hook_maps_verdict_and_carries_diff_and_gate(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        "def test_my_tool():\n    assert True\n"
    )

    fake = _FakeModel({
        "approved": False,
        "rationale": "tolerance too loose",
        "issues": ["tighten injection-recovery tolerance"],
        "checks_adequate": False,
    })
    colead = AnalystColeadAgent(model=fake)  # fake has no .config → decorrelation skipped
    hook = make_review_hook(colead=colead, author_provider="anthropic")

    gate = GateReport(passed=True, findings=[])
    verdict = asyncio.run(hook(repo, _spec(), gate))

    assert isinstance(verdict, ToolReviewVerdict)
    assert verdict.approved is False
    assert verdict.checks_adequate is False
    assert "tighten injection-recovery tolerance" in verdict.issues

    # The co-lead saw the diff bundle and the gate report in its payload.
    payload = json.loads(fake.messages[1].content)
    assert "diff" in payload and "NEW FILE: demo_utils/lib/test_demo.py" in payload["diff"]
    assert payload["gate_report"]["passed"] is True


def test_review_hook_rejects_injected_colead_sharing_author_provider(tmp_path):
    repo = _make_repo(tmp_path)
    fake = _FakeModel({"approved": True, "rationale": "ok"})
    colead = AnalystColeadAgent(model=fake)
    # Force a provider that matches the author → trust boundary must fail loudly.
    colead.model = SimpleNamespace(config=SimpleNamespace(provider="anthropic"))
    hook = make_review_hook(colead=colead, author_provider="anthropic")

    with pytest.raises(ValueError, match="provider-decorrelated"):
        asyncio.run(hook(repo, _spec(), GateReport(passed=True)))
