"""Deterministic tests for the Phase-2 tool-authoring harness (merge_flow.propose_and_pr).

The bounded rework loop is exercised with injected fakes — a fake SDK client, a fake
analyst lead, and stub gate/review hooks — so no live LLM or git is involved.
"""

import asyncio

import pytest

from auto_research.sandbox.merge_flow import (
    HumanInputRequired,
    _make_ask_human_handler,
    _make_ask_lead_handler,
    propose_and_pr,
)
from auto_research.schemas import GateReport, ToolReviewVerdict, ToolSpec


def _spec():
    return ToolSpec(
        science_context="need retBB",
        expected_behavior="add_retbb(indices)",
        proposed_tool_name="fit_retbb_hss",
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[{"mission": "IXPE", "obsid": "02008401"}],
    )


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient — records the turns it is driven with."""

    def __init__(self):
        self.queries: list[str] = []
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, message, session_id="default"):
        self.queries.append(message)

    async def receive_response(self):
        for _ in ():  # empty async generator — the author "does its work" silently
            yield _


class _FakeLead:
    def __init__(self):
        self.questions: list[str] = []

    async def clarify_for_author(self, question, *, kind="clarify_code", context=""):
        self.questions.append(question)
        return f"lead-answer:{question}"


def _run(spec, **kwargs):
    client = _FakeClient()
    kwargs.setdefault("lead", _FakeLead())
    kwargs.setdefault("coder_model", "fake-model")
    kwargs.setdefault("worktree_factory", lambda root, base, branch: base / branch.replace("/", "_"))
    kwargs.setdefault("sdk_client_factory", lambda options: client)
    result = asyncio.run(propose_and_pr(spec, **kwargs))
    return result, client


def test_happy_path_returns_committed(tmp_path):
    result, client = _run(_spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt")
    assert result.status == "committed"
    assert result.branch and result.branch.startswith("tool/fit_retbb_hss-")
    assert result.gate_report.passed and result.review.approved
    assert client.connected is False  # disconnected in finally
    assert len(client.queries) == 1  # one initial author turn, no rework


def test_gate_failures_drive_exactly_n_reworks_then_succeed(tmp_path):
    calls = {"n": 0}

    async def flaky_gate(worktree, spec):
        calls["n"] += 1
        if calls["n"] <= 2:  # fail twice, pass on the third evaluation
            return GateReport(
                passed=False,
                reason="bad",
                layer_b=[{"name": "inj_rec", "layer": "B", "passed": False, "detail": "off"}],
            )
        return GateReport(passed=True)

    result, client = _run(
        _spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", run_gate=flaky_gate
    )
    assert result.status == "committed"
    assert calls["n"] == 3  # initial gate + 2 re-evaluations
    assert len(client.queries) == 3  # initial author turn + 2 gate-feedback re-drives


def test_gate_cap_exhaustion_returns_failed_with_reason(tmp_path):
    async def always_fail(worktree, spec):
        return GateReport(passed=False, reason="never converges")

    result, _ = _run(
        _spec(),
        toolkit_root=tmp_path,
        worktree_base=tmp_path / "wt",
        run_gate=always_fail,
        gate_rework_cap=2,
    )
    assert result.status == "failed"
    assert "gate rework cap exhausted" in result.reason
    assert result.commit_sha is None
    assert result.branch is not None  # carried for the lead's human summary


def test_review_rejection_reworks_then_passes(tmp_path):
    review_calls = {"n": 0}

    async def flaky_review(worktree, spec, gate):
        review_calls["n"] += 1
        if review_calls["n"] == 1:
            return ToolReviewVerdict(approved=False, rationale="checks too loose", issues=["tighten tol"])
        return ToolReviewVerdict(approved=True, rationale="ok")

    result, client = _run(
        _spec(), toolkit_root=tmp_path, worktree_base=tmp_path / "wt", run_review=flaky_review
    )
    assert result.status == "committed"
    assert review_calls["n"] == 2
    # initial author turn + one review-feedback re-drive
    assert len(client.queries) == 2


def test_review_cap_exhaustion_returns_failed(tmp_path):
    async def reject(worktree, spec, gate):
        return ToolReviewVerdict(approved=False, rationale="subtle bug remains")

    result, _ = _run(
        _spec(),
        toolkit_root=tmp_path,
        worktree_base=tmp_path / "wt",
        run_review=reject,
        review_rework_cap=1,
    )
    assert result.status == "failed"
    assert "review rework cap exhausted" in result.reason
    assert result.review is not None and result.review.approved is False


def test_ask_lead_handler_routes_to_lead():
    lead = _FakeLead()
    handler = _make_ask_lead_handler(lead)
    answer = asyncio.run(handler("which lib fn?", context="ctx"))
    assert answer == "lead-answer:which lib fn?"
    assert lead.questions == ["which lib fn?"]


def test_ask_human_handler_routes_to_injected_handler():
    seen = {}

    def human(query):
        seen.update(query)
        return "human-says-go"

    handler = _make_ask_human_handler(human)
    answer = asyncio.run(handler("response convention?", context="expert"))
    assert answer == "human-says-go"
    assert seen["kind"] == "blocked" and seen["question"] == "response convention?"


def test_ask_human_handler_without_handler_raises_fail_safe():
    handler = _make_ask_human_handler(None)
    with pytest.raises(HumanInputRequired):
        asyncio.run(handler("need an expert"))
