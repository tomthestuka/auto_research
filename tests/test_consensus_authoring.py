"""Deterministic tests for Phase-2 step-7 consensus authoring/resume.

These exercise ``DebateToConverge``'s authoring path with fakes — no live LLM, git, or
sandbox. The real ``merge_flow.propose_and_pr`` is monkeypatched (it is imported lazily
inside the strategy, so patching the module attribute intercepts the call).
"""

import asyncio
from pathlib import Path

import pytest

from auto_research.consensus import DebateToConverge
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    AuthoringDecision,
    MergeFlowResult,
    ReviewVerdict,
    ToolSpec,
    ValidationDataset,
)

TOOLKIT = Path("/tmp/fake_toolkit")


def _toolspec(name: str = "pcube_tool") -> ToolSpec:
    return ToolSpec(
        science_context="workers need a PCUBE polarization tool the toolkit doesn't expose",
        expected_behavior="run xpbin PCUBE on a merged event file and return the Stokes table",
        proposed_tool_name=name,
        target_domain="xspec_utils",
        disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="01002901")],
    )


class FakeSandbox:
    def __init__(self) -> None:
        self.restarts = 0
        self.is_alive = True

    async def restart(self) -> None:
        self.restarts += 1
        self.is_alive = True

    async def start(self) -> None:
        self.is_alive = True


class FakeRegistry:
    def __init__(self) -> None:
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1


class AuthoringWorker:
    """Emits a ``tool_request`` until ``state['authored']`` reaches ``stop_after``."""

    def __init__(self, name: str, state: dict, *, stop_after: int = 1) -> None:
        self.name = name
        self.state = state
        self.stop_after = stop_after
        self.calls = 0
        self.sandbox = FakeSandbox()
        self.last_tool_results: list = []

    async def analyze(self, task: AnalysisTask, *, peer_reports=()) -> AnalystReport:
        self.calls += 1
        needs_tool = self.state["authored"] < self.stop_after
        return AnalystReport(
            summary=f"{self.name} call {self.calls}",
            findings=[f"{self.name} f{self.calls}"],
            tool_request=_toolspec() if needs_tool else None,
            status="ok",
        )


class AuthoringLead:
    def __init__(self, *, should_author: bool = True, converge_on: int = 1) -> None:
        self.should_author = should_author
        self.converge_on = converge_on
        self.decided = 0
        self.judged: list[int] = []

    async def decide_authoring(self, reports) -> AuthoringDecision:
        self.decided += 1
        return AuthoringDecision(
            should_author=self.should_author,
            tool_spec=_toolspec() if self.should_author else None,
            rationale="fake decision",
        )

    async def judge_convergence(self, task, reports, *, round_index, max_rounds) -> ReviewVerdict:
        self.judged.append(round_index)
        return ReviewVerdict(accepted=round_index >= self.converge_on, rationale="r")

    async def adjudicate(self, task, reports) -> AnalystReport:
        return AnalystReport(summary="final", findings=["done"])


def _patch_ppr(monkeypatch, state, *, status="committed", calls=None):
    async def fake_propose_and_pr(tool_spec, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        if status == "committed":
            state["authored"] += 1
        return MergeFlowResult(
            tool_spec_id=tool_spec.id,
            status=status,
            branch=kwargs.get("direct_branch") or "ai",
            commit_sha="abc123def456" if status == "committed" else None,
            reason="fake",
        )

    monkeypatch.setattr(
        "auto_research.sandbox.merge_flow.propose_and_pr", fake_propose_and_pr
    )


def test_committed_authoring_reloads_restarts_and_reruns(monkeypatch):
    state = {"authored": 0}
    calls: list = []
    _patch_ppr(monkeypatch, state, status="committed", calls=calls)
    workers = [AuthoringWorker("w1", state, stop_after=1), AuthoringWorker("w2", state, stop_after=1)]
    lead = AuthoringLead(should_author=True, converge_on=1)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=3,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="Intra-obs PA/PD stability."))
    )

    assert final.summary == "final"
    assert len(calls) == 1  # one tool authored
    assert calls[0]["direct_branch"] == "ai"
    assert registry.reloads == 1  # registry refreshed after commit
    assert all(w.sandbox.restarts == 1 for w in workers)  # sandboxes restarted
    assert lead.decided == 1
    # The resume did NOT consume a debate round — converge_on=1 ⇒ exactly one judge call.
    assert lead.judged == [1]
    # provenance landed in the task instructions reran workers saw it (worker called twice).
    assert all(w.calls == 2 for w in workers)


def test_should_author_false_falls_through_to_debate(monkeypatch):
    state = {"authored": 0}
    calls: list = []
    _patch_ppr(monkeypatch, state, status="committed", calls=calls)
    workers = [AuthoringWorker("w1", state, stop_after=99)]  # would keep requesting
    lead = AuthoringLead(should_author=False, converge_on=99)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=2, registry=registry, tool_author_cap=3,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"
    assert calls == []  # never authored
    assert registry.reloads == 0
    assert lead.judged == [1, 2]  # ordinary debate, capped at max_rounds


def test_author_cap_bounds_attempts(monkeypatch):
    state = {"authored": 0}
    calls: list = []
    _patch_ppr(monkeypatch, state, status="committed", calls=calls)
    # Worker is never satisfied (stop_after huge), so authoring would loop forever but the cap bounds it.
    workers = [AuthoringWorker("w1", state, stop_after=99)]
    lead = AuthoringLead(should_author=True, converge_on=99)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=1, registry=registry, tool_author_cap=2,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"  # honest finish, no fabrication
    assert len(calls) == 2  # exactly tool_author_cap attempts
    assert registry.reloads == 2


def test_failed_authoring_does_not_resume(monkeypatch):
    state = {"authored": 0}
    calls: list = []
    _patch_ppr(monkeypatch, state, status="failed", calls=calls)
    workers = [AuthoringWorker("w1", state, stop_after=99)]
    lead = AuthoringLead(should_author=True, converge_on=99)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=1, registry=registry, tool_author_cap=1,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"
    assert len(calls) == 1  # one attempt (budget=1), then no resume
    assert registry.reloads == 0  # failed → no reload
    assert workers[0].sandbox.restarts == 0  # failed → no restart


def test_no_authoring_when_toolkit_unwired():
    """Without toolkit_root, the strategy is plain Phase-1 debate (a lead lacking decide_authoring works)."""

    class PlainLead(AuthoringLead):
        pass

    state = {"authored": 0}
    workers = [AuthoringWorker("w1", state, stop_after=99)]  # always requests, but ignored
    lead = PlainLead(should_author=True, converge_on=1)

    final = asyncio.run(
        DebateToConverge(max_rounds=2).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"
    assert lead.decided == 0  # decide_authoring never called
    assert lead.judged == [1]
