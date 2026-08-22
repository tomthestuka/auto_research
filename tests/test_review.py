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


def test_review_hook_allows_a_correlated_colead_only_when_measurement_asks(tmp_path):
    """``allow_correlated`` is the one way past the trust boundary, and it is opt-in.

    The default refusal is tested above. This is the measurement path: the sweep that asks what
    provider decorrelation is worth has to be able to run the same brief with a same-provider
    reviewer, and no other caller may.
    """
    repo = _make_repo(tmp_path)
    fake = _FakeModel({"approved": True, "rationale": "ok"})
    fake.config = SimpleNamespace(provider="anthropic", model="claude-opus-5")
    colead = AnalystColeadAgent(model=fake)

    hook = make_review_hook(
        colead=colead, author_provider="anthropic", allow_correlated=True
    )
    verdict = asyncio.run(hook(repo, _spec(), GateReport(passed=True)))
    assert verdict.approved is True


def test_a_correlated_review_is_recorded_so_the_verdict_stays_readable_as_such(tmp_path):
    """A verdict reached under a correlated reviewer does not carry production's guarantee, so
    the ledger has to say so — otherwise a later reader cannot tell the two apart."""
    repo = _make_repo(tmp_path)
    events: list[tuple[str, dict]] = []

    class _Monitor:
        async def record(self, team, event, *, level, summary, actor=None, data=None):
            events.append((event, dict(data or {})))

    fake = _FakeModel({"approved": True, "rationale": "ok"})
    fake.config = SimpleNamespace(provider="openai", model="gpt-5.6-sol")
    colead = AnalystColeadAgent(model=fake)

    hook = make_review_hook(
        colead=colead, author_provider="openai", monitor=_Monitor(), allow_correlated=True
    )
    asyncio.run(hook(repo, _spec(), GateReport(passed=True)))

    correlated = [d for name, d in events if name == "review.correlated"]
    assert correlated, f"expected a review.correlated event, saw {[n for n, _ in events]}"
    assert correlated[0]["author_provider"] == "openai"
    assert correlated[0]["reviewer_model"] == "gpt-5.6-sol"


def test_a_decorrelated_review_records_nothing_about_correlation(tmp_path):
    """The flag being on does not make the event fire; sharing a provider does."""
    repo = _make_repo(tmp_path)
    events: list[str] = []

    class _Monitor:
        async def record(self, team, event, *, level, summary, actor=None, data=None):
            events.append(event)

    fake = _FakeModel({"approved": True, "rationale": "ok"})
    fake.config = SimpleNamespace(provider="openai", model="gpt-5.6-sol")
    colead = AnalystColeadAgent(model=fake)

    hook = make_review_hook(
        colead=colead, author_provider="anthropic", monitor=_Monitor(), allow_correlated=True
    )
    asyncio.run(hook(repo, _spec(), GateReport(passed=True)))
    assert "review.correlated" not in events
