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
    AnalystToolRequest,
    AuthoringDecision,
    MergeFlowResult,
    ReviewVerdict,
    ToolSpec,
    ValidationDataset,
)

TOOLKIT = Path("/tmp/fake_toolkit")


def _tool_request(name: str = "pcube_tool") -> AnalystToolRequest:
    """The worker → lead message a worker raises on its ``AnalystReport.tool_request``."""
    return AnalystToolRequest(
        science_context="workers need a PCUBE polarization tool the toolkit doesn't expose",
        expected_behavior="run xpbin PCUBE on a merged event file and return the Stokes table",
        proposed_tool_name=name,
        validation_data=[ValidationDataset(mission="IXPE", obsid="01002901")],
    )


def _toolspec(name: str = "pcube_tool") -> ToolSpec:
    """The lead → coder brief the lead finalises (adds disposition/target/checks)."""
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
            tool_request=_tool_request() if needs_tool else None,
            status="ok",
        )


class AuthoringLead:
    def __init__(
        self,
        *,
        should_author: bool = True,
        converge_on: int = 1,
        state: dict | None = None,
        calls: list | None = None,
        author_status: str = "committed",
        chain_len: int = 1,
        fail_on: int | None = None,
    ) -> None:
        self.should_author = should_author
        self.converge_on = converge_on
        self.decided = 0
        self.finalized = 0
        self.judged: list[int] = []
        # B-topology (A1): the lead drives authoring itself; the test stubs the OUTCOME of
        # drive_authoring (no real session/SDK) instead of monkeypatching propose_and_pr.
        self.state = state
        self.calls = calls
        self.author_status = author_status
        # A8: decide_authoring plans an ordered chain of ``chain_len`` tools; ``fail_on`` (1-based)
        # marks the drive_authoring call that returns a non-commit (to exercise a mid-chain stop).
        self.chain_len = chain_len
        self.fail_on = fail_on
        self._drive_calls = 0

    async def decide_authoring(self, reports) -> AuthoringDecision:
        self.decided += 1
        specs = (
            [_toolspec(f"pcube_tool_{i}") for i in range(self.chain_len)]
            if self.should_author
            else []
        )
        return AuthoringDecision(
            should_author=self.should_author,
            tool_specs=specs,
            rationale="fake decision",
        )

    async def drive_authoring(
        self, tool_spec, *, toolkit_root, worktree_base, direct_branch=None,
        colead=None, human_handler=None,
    ) -> MergeFlowResult:
        self._drive_calls += 1
        if self.calls is not None:
            self.calls.append({"direct_branch": direct_branch, "toolkit_root": toolkit_root})
        failed = self.author_status != "committed" or self._drive_calls == self.fail_on
        if not failed and self.state is not None:
            self.state["authored"] += 1
        return MergeFlowResult(
            tool_spec_id=tool_spec.id,
            status="failed" if failed else "committed",
            branch=direct_branch or "ai",
            commit_sha=None if failed else "abc123def456",
            reason="fake",
        )

    async def finalize_authoring_outcome(self, task, reports, tool_spec, result) -> AnalystReport:
        self.finalized += 1
        return AnalystReport(
            summary="authoring impossible under current construction",
            findings=["honest failure"],
            status="failed",
            uncertainty=result.reason,
        )

    async def judge_convergence(self, task, reports, *, round_index, max_rounds) -> ReviewVerdict:
        self.judged.append(round_index)
        return ReviewVerdict(accepted=round_index >= self.converge_on, rationale="r")

    async def adjudicate(self, task, reports, *, sources=None) -> AnalystReport:
        return AnalystReport(summary="final", findings=["done"])


def test_committed_authoring_reloads_restarts_and_reruns():
    state = {"authored": 0}
    calls: list = []
    workers = [AuthoringWorker("w1", state, stop_after=1), AuthoringWorker("w2", state, stop_after=1)]
    lead = AuthoringLead(should_author=True, converge_on=1, state=state, calls=calls)
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


def test_should_author_false_falls_through_to_debate():
    state = {"authored": 0}
    calls: list = []
    workers = [AuthoringWorker("w1", state, stop_after=99)]  # would keep requesting
    lead = AuthoringLead(should_author=False, converge_on=99, state=state, calls=calls)
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


def test_author_cap_bounds_attempts():
    state = {"authored": 0}
    calls: list = []
    # Worker is never satisfied (stop_after huge), so authoring would loop forever but the cap bounds it.
    workers = [AuthoringWorker("w1", state, stop_after=99)]
    lead = AuthoringLead(should_author=True, converge_on=99, state=state, calls=calls)
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


def test_uncommittable_tool_recovers_then_finalizes_at_exhaustion(monkeypatch):
    # A11: a non-committed attempt no longer fails immediately. With ZERO tools committed and
    # credits remaining, the run keeps re-attempting authoring (burning tool_author_cap credits,
    # rerunning the workers on a failure note so the lead can adapt) — the honest §9.6 terminal
    # fires only when credits are exhausted with nothing ever committed.
    state = {"authored": 0}
    calls: list = []
    workers = [AuthoringWorker("w1", state, stop_after=99)]
    lead = AuthoringLead(should_author=True, converge_on=99, state=state, calls=calls,
                         author_status="failed")
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=3,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    # The terminal report is the lead's honest-failure report, not an adjudicated "final".
    assert final.summary == "authoring impossible under current construction"
    assert final.status == "failed"
    assert lead.finalized == 1
    assert len(calls) == 3  # burned all tool_author_cap credits recovering before finalizing
    assert lead.decided == 3  # re-decided each retry (fed the failure note back)
    assert lead.judged == []  # never entered the debate phase (nothing to adjudicate)
    assert workers[0].calls == 3  # initial report + a rerun after each of the first two misses
    assert registry.reloads == 0  # nothing ever committed → no reload
    assert workers[0].sandbox.restarts == 0  # nothing committed → no restart


def test_chain_authored_sequentially_reruns_once():
    """A8: a multi-tool chain is authored back-to-back on ONE decision, and the workers rerun
    exactly ONCE after the whole chain — not once per tool."""
    state = {"authored": 0}
    calls: list = []
    workers = [
        AuthoringWorker("w1", state, stop_after=2),
        AuthoringWorker("w2", state, stop_after=2),
    ]
    lead = AuthoringLead(should_author=True, converge_on=1, state=state, calls=calls, chain_len=2)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=12,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="Broadband PD-hardness survey."))
    )

    assert final.summary == "final"
    assert len(calls) == 2  # both chain tools authored, in order, on one decision
    assert lead.decided == 1  # ONE authoring decision for the whole chain (not one per tool)
    assert registry.reloads == 1  # a single rerun after the chain, not one reload per tool
    assert all(w.sandbox.restarts == 1 for w in workers)
    assert all(w.calls == 2 for w in workers)  # initial report + exactly one rerun
    assert lead.judged == [1]


def test_partial_chain_commit_reloads_and_reruns():
    """A11: when a chain tool cannot commit, authoring stops at that tool but the committed prefix
    is a complete, dependency-satisfied set — so it IS loaded (reload + restart) and the workers
    rerun on it. The run does NOT choke into an honest terminal (that fires only when NOTHING ever
    committed — see test_uncommittable_tool_recovers_then_finalizes_at_exhaustion)."""
    state = {"authored": 0}
    calls: list = []
    # Workers are satisfied once one tool is authored, so after the committed prefix loads they
    # stop requesting and the run converges normally.
    workers = [
        AuthoringWorker("w1", state, stop_after=1),
        AuthoringWorker("w2", state, stop_after=1),
    ]
    # chain of 2: tool 1 commits, the 2nd drive_authoring returns a non-commit.
    lead = AuthoringLead(
        should_author=True, converge_on=1, state=state, calls=calls, chain_len=2, fail_on=2
    )
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=12,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"  # resumed + converged, not a finalize-terminal
    assert lead.finalized == 0  # the run did NOT choke on the one failed session
    assert len(calls) == 2  # tool 1 committed, tool 2 attempted+failed → chain stopped there
    assert registry.reloads == 1  # committed prefix loaded (A11: was 0 before the fix)
    assert all(w.sandbox.restarts == 1 for w in workers)
    assert all(w.calls == 2 for w in workers)  # initial report + one rerun on the committed prefix
    assert lead.judged == [1]


def test_committed_zero_recovers_on_retry():
    """A11: a transient first-tool failure recovers — a second authoring attempt commits, the tool
    loads, and the analysis completes normally instead of failing on the first miss."""
    state = {"authored": 0}
    calls: list = []
    workers = [AuthoringWorker("w1", state, stop_after=1)]
    # fail_on=1 fails only the FIRST drive_authoring call; the retry commits.
    lead = AuthoringLead(
        should_author=True, converge_on=1, state=state, calls=calls, chain_len=1, fail_on=1
    )
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=12,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"  # recovered to a real result
    assert lead.finalized == 0  # never finalized honestly — it succeeded on retry
    assert len(calls) == 2  # first attempt failed, second committed
    assert lead.decided == 2  # re-decided after the first miss (fed the failure note back)
    assert registry.reloads == 1  # reload once the retry committed
    assert workers[0].calls == 3  # initial + rerun after the miss + rerun after the commit


def test_chain_budget_caps_total_tools():
    """A8: tool_author_cap bounds the TOTAL tools authored across the chain; a chain longer than
    the remaining budget authors up to the cap, then reruns once on what committed."""
    state = {"authored": 0}
    calls: list = []
    workers = [AuthoringWorker("w1", state, stop_after=99)]  # never satisfied
    lead = AuthoringLead(should_author=True, converge_on=1, state=state, calls=calls, chain_len=3)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=1, registry=registry, tool_author_cap=2,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert final.summary == "final"  # honest finish
    assert len(calls) == 2  # cap=2 stops the 3-tool chain at 2
    assert registry.reloads == 1  # a single rerun on the partial chain (budget then exhausted)


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
