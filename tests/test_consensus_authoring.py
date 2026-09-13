"""Deterministic tests for Phase-2 step-7 consensus authoring/resume.

These exercise ``DebateToConverge``'s authoring path with fakes — no live LLM, git, or
sandbox. The real ``merge_flow.propose_and_pr`` is monkeypatched (it is imported lazily
inside the strategy, so patching the module attribute intercepts the call).
"""

import asyncio
from pathlib import Path

from types import SimpleNamespace

import pytest

from auto_research.agents.react import (
    TURN_END_TOOL,
    LeadCycleOutcome,
)
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
            status="succeeded",
        )


class AuthoringLead:
    """A lead shaped like ``run_cycle``: one cycle either authors a chain, or judges.

    It mirrors the three production decisions the strategy depends on, and nothing else:

    - the **authoring stage** exists only while a worker's request is open, credits remain and a
      toolkit is wired (the same condition ``run_cycle`` computes), and a cycle that authors ends
      *before* judging so the caller reruns the workers on what committed (A8);
    - a link that cannot commit **stops the chain** there, leaving a dependency-satisfied prefix;
    - credits spent with nothing ever committed files the honest terminal (§9.6) rather than
      re-entering debate.

    ``_drive_link`` is the seam a subclass overrides to inject a per-link failure (the real
    ``_author_chain`` catches ``ToolkitBranchUnclean`` per link the same way).
    """

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
        # B-topology (A1): the lead drives authoring itself; the test stubs the OUTCOME of each
        # link (no real session/SDK) instead of monkeypatching propose_and_pr.
        self.state = state
        self.calls = calls
        self.author_status = author_status
        # A8: one cycle plans an ordered chain of ``chain_len`` tools; ``fail_on`` (1-based) marks
        # the link that returns a non-commit (to exercise a mid-chain stop).
        self.chain_len = chain_len
        self.fail_on = fail_on
        self._drive_calls = 0
        self._committed_total = 0
        # Attempts counted in run_cycle, not in _drive_link: a subclass that raises before the
        # link runs has still attempted one, and that is what "nothing ever committed" measures.
        self._attempts = 0

    async def _drive_link(self, spec, *, direct_branch, toolkit_root) -> MergeFlowResult:
        """One link's authoring outcome — the override point for an injected failure."""
        self._drive_calls += 1
        if self.calls is not None:
            self.calls.append({"direct_branch": direct_branch, "toolkit_root": toolkit_root})
        failed = self.author_status != "committed" or self._drive_calls == self.fail_on
        if not failed and self.state is not None:
            self.state["authored"] += 1
        return MergeFlowResult(
            tool_spec_id=spec.id,
            status="failed" if failed else "committed",
            branch=direct_branch or "ai",
            commit_sha=None if failed else "abc123def456",
            reason="fake",
        )

    async def run_cycle(
        self,
        task,
        reports,
        *,
        sources=None,
        rounds_left=0,
        author_budget=0,
        toolkit_root=None,
        worktree_base=None,
        direct_branch=None,
        colead=None,
        human_handler=None,
    ):
        from auto_research.sandbox.merge_flow import ToolkitBranchUnclean

        authoring_open = (
            toolkit_root is not None
            and author_budget > 0
            and any(r.tool_request is not None for r in reports)
        )
        if authoring_open and self.should_author:
            self.decided += 1
            specs = [_toolspec(f"pcube_tool_{i}") for i in range(self.chain_len)]
            outcome = LeadCycleOutcome(
                filed=TURN_END_TOOL,
                # A3/A7: the cycle ends on turn_end with a report; `decision` records the brief
                # it wrote, and `authored_chain` is now derived from that rather than from a
                # filed form's name.
                report=AnalystReport(summary="chain briefed", findings=["authored"]),
                decision=AuthoringDecision(
                    should_author=True, tool_specs=specs, rationale="fake decision"
                ),
                author_budget=author_budget,
            )
            for spec in specs:
                if outcome.author_budget <= 0:
                    break
                outcome.author_budget -= 1
                self._attempts += 1
                try:
                    result = await self._drive_link(
                        spec, direct_branch=direct_branch, toolkit_root=toolkit_root
                    )
                except ToolkitBranchUnclean as exc:
                    result = MergeFlowResult(
                        tool_spec_id=spec.id, status="failed", reason=f"unclean branch: {exc}"
                    )
                outcome.authored.append(result)
                if result.status == "committed":
                    outcome.committed += 1
                    self._committed_total += 1
                else:
                    break
            return outcome
        if self._attempts > 0 and self._committed_total == 0 and author_budget <= 0:
            # Credits spent, nothing ever committed: the honest §9.6 terminal, not another debate
            # round. Judging never happens, so `judged` stays empty.
            self.finalized += 1
            return LeadCycleOutcome(
                filed=TURN_END_TOOL,
                report=AnalystReport(
                    summary="authoring impossible under current construction",
                    findings=["honest failure"],
                    status="failed",
                ),
                author_budget=author_budget,
            )
        self.judged.append(len(self.judged) + 1)
        if len(self.judged) < self.converge_on and rounds_left > 0:
            return LeadCycleOutcome(
                filed=TURN_END_TOOL,
                report=AnalystReport(
                    summary="not settled",
                    rework=True,
                    rationale="r",
                    what_to_probe="probe further",
                ),
                author_budget=author_budget,
            )
        return LeadCycleOutcome(
            filed=TURN_END_TOOL,
            report=AnalystReport(summary="final", findings=["done"]),
            author_budget=author_budget,
        )


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


def test_mid_run_unclean_branch_degrades_to_honest_terminal():
    """A ToolkitBranchUnclean raised mid-run (branch dirtied at an attempt's start) degrades to
    the honest §9.6 terminal via finalize_authoring_outcome instead of crashing the run — the
    Stage-2 in-flow guard (F6-class). The across-a-death case is handled by the resume heal."""
    from auto_research.sandbox.merge_flow import ToolkitBranchUnclean

    class _UncleanLead(AuthoringLead):
        async def _drive_link(self, spec, **kwargs):
            raise ToolkitBranchUnclean("research_toolkit has uncommitted changes on 'ai'")

    state = {"authored": 0}
    workers = [AuthoringWorker("w1", state, stop_after=1)]
    lead = _UncleanLead(should_author=True, converge_on=1, state=state)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=3,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert lead.finalized == 1        # degraded to the honest terminal, not a crash
    assert final.status == "failed"   # honest failure report
    assert registry.reloads == 0      # nothing committed → no registry reload


def test_stop_flag_no_longer_gates_authoring():
    """The pre-authoring stop check went with the cooperative wind-down (2026-07-31).

    It existed so a stopped run would not *start* a tool chain while it wound itself down. A
    stop now cancels the run task outright, so there is no wind-down to protect and no window in
    which the flag is set but the strategy still runs — the flag alone changes nothing here.
    """
    from auto_research.run_control import RunControl

    rc = RunControl()
    rc.request_stop("human stop")
    state = {"authored": 0}
    workers = [AuthoringWorker("w1", state, stop_after=1)]      # requests a tool
    lead = AuthoringLead(should_author=True, converge_on=1, state=state)
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=2, registry=registry, tool_author_cap=3,
            toolkit_root=TOOLKIT, direct_branch="ai", run_control=rc,
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert lead.decided == 1        # authoring proceeds; the flag is not consulted
    assert final.summary == "final"


class TerminalMidChainLead(AuthoringLead):
    """A lead that files the honest terminal from inside the authoring stage.

    Shaped on the real trajectory of block 0 attempt 5, read back from its ledger: a 3-link
    chain in which links 1-2 commit, link 3 exhausts its five submissions, and the lead files
    `submit_terminal_report` **inside** link 3 rather than returning `author_chain`. The
    production event is `analyst.lead.cycle | filed=submit_terminal_report committed=2`, so the
    outcome the strategy has to act on carries a terminal form *and* a non-zero commit count —
    and gating the resume on which form arrived is what let two gated, approved, committed
    tools reach the analysts unregistered.
    """

    async def run_cycle(self, task, reports, **kwargs):
        outcome = await super().run_cycle(task, reports, **kwargs)
        if outcome.decision is not None and outcome.committed >= 1:
            # The A16 shape, re-expressed: a cycle that committed and then ended honestly. Under
            # A3 that is a report with status="failed", not a distinct terminal form.
            outcome.report = AnalystReport(
                summary="stopped after committing", findings=["partial"], status="failed"
            )
            outcome.report = AnalystReport(
                summary="the chain stopped short; reporting honestly",
                findings=["two tools landed, the third could not be made mergeable"],
                status="failed",
            )
        return outcome


def test_terminal_filed_mid_chain_still_reloads_restarts_and_reruns():
    """A terminal filed from the authoring stage ends the CHAIN, never the analysis (A11).

    Whatever committed still has to become callable: without the registry reload and a sandbox
    restart the freshly committed tools are not registered, and the reran workers get
    ``exit 127 Tool not registered`` on the capability the run just built — which is what the
    reviewer's send-back actually received in attempt 5.
    """
    state = {"authored": 0}
    calls: list = []
    workers = [AuthoringWorker("w1", state, stop_after=1), AuthoringWorker("w2", state, stop_after=1)]
    # The live shape: three links, the third fails, the terminal is filed with committed=2.
    lead = TerminalMidChainLead(
        should_author=True, converge_on=1, state=state, calls=calls, chain_len=3, fail_on=3,
    )
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=12,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert len(calls) == 3               # three links attempted, as the ledger shows
    assert lead._committed_total == 2    # links 1-2 landed, link 3 did not
    assert registry.reloads == 1         # discovery refreshed despite the terminal form
    assert all(w.sandbox.restarts == 1 for w in workers)
    assert all(w.calls == 2 for w in workers)  # the workers reran on the committed prefix
    # The analysis continued to a judged report rather than shipping the mid-chain terminal.
    assert final.summary == "final"


def test_terminal_with_nothing_committed_still_ends_the_analysis():
    """The §9.6 terminal keeps its meaning when no capability landed.

    The resume is driven by the commit count, so a terminal with zero commits is what it says
    it is: there is no new evidence to rerun the workers on, and the honest report stands.
    """
    state = {"authored": 0}
    workers = [AuthoringWorker("w1", state, stop_after=99)]
    lead = AuthoringLead(
        should_author=True, converge_on=99, state=state, author_status="failed",
    )
    registry = FakeRegistry()

    final = asyncio.run(
        DebateToConverge(
            max_rounds=3, registry=registry, tool_author_cap=2,
            toolkit_root=TOOLKIT, direct_branch="ai",
        ).run(workers, lead, AnalysisTask(question="q"))
    )

    assert registry.reloads == 0
    assert final.status == "failed"
    assert lead.finalized == 1


def test_n11_author_credits_are_per_analysis_and_never_refilled_by_a_send_back():
    """N11: `tool_author_cap` is documented as a per-analysis ceiling, but the budget was a local
    initialised inside `run()`, and `run()` is re-entered on every reviewer send-back — so a
    send-back silently refilled it and the real ceiling was `max_iterations x tool_author_cap`.
    Observed in slot 2's ledger as `author_budget: 11 -> 10 -> 10` across cycles that spent 1, 1
    and 2 credits."""
    from auto_research.consensus.strategy import DebateToConverge

    strategy = DebateToConverge(tool_author_cap=3, toolkit_root=Path("/tmp"))

    # Entry 1 of the analysis: the full cap is available.
    assert strategy.tool_author_cap - strategy._credits.spent == 3

    # Two credits spent inside that entry.
    strategy._credits.spent += 2

    # Entry 2 — what a reviewer send-back causes. One credit left, not three.
    assert max(0, strategy.tool_author_cap - strategy._credits.spent) == 1

    strategy._credits.spent += 1
    assert max(0, strategy.tool_author_cap - strategy._credits.spent) == 0


def test_n11_the_credit_holder_is_mutable_under_a_frozen_strategy():
    """The strategy is a frozen dataclass, so the count lives in a mutable holder. A plain int
    field would raise `FrozenInstanceError` at the first spend (caught during F1-F9 work)."""
    from auto_research.consensus.strategy import DebateToConverge

    a, b = DebateToConverge(), DebateToConverge()
    a._credits.spent += 1
    assert a._credits.spent == 1
    assert b._credits.spent == 0, "the holder must not be shared between instances"


# ── F9: the chain outlives one failed link; status is agent-declared ─────────


def test_f9_a_failed_link_does_not_end_the_chain_and_the_lead_is_told_why():
    """It used to `break`, so one failure ended the chain with the credits that would have paid
    for a retry still unspent — slot 3 spent 2h43m and ~$18 on six cycles refused before a coder
    session opened, slot 4 queued ~8 more. Human ruling: the LEAD owns the stop decision, and
    spending the budget discovering a capability cannot be built is an honest answer.

    So the loop continues AND the lead is told, in its own conversation, before the next brief —
    that is what makes "the lead decides" real rather than nominal.

    A7 strengthened this rather than replacing it. The chain no longer runs as a Python loop over
    nested ReAct calls; it advances on a tool result, so a failed link is a sentence the lead
    reads mid-turn and the next session opens underneath it. Asserted on the behaviour of the
    driver rather than on the source text of a method that no longer exists.
    """
    import asyncio

    from auto_research.agents.react import LeadCycle, LeadCycleOutcome
    from auto_research.agents.roles import _ChainDriver
    from auto_research.schemas import AuthoringDecision

    specs = [_toolspec("link_one"), _toolspec("link_two")]
    cycle = LeadCycle(authoring_enabled=True, rounds_left=1)
    cycle.brief = AuthoringDecision(should_author=True, tool_specs=specs, rationale="r")
    outcome = LeadCycleOutcome(filed=None, author_budget=4)

    opened: list[str] = []

    class _Lead:
        monitor = None
        run_control = None

        async def _record(self, *a, **k):
            return None

    driver = _ChainDriver(
        lead=_Lead(), provider=SimpleNamespace(authoring_session=None), cycle=cycle,
        outcome=outcome, messages=[], toolkit_root=None, worktree_base=None,
        direct_branch=None, colead=None, human_handler=None, coder=None,
        allow_correlated_review=False,
    )

    async def _open(spec):
        opened.append(spec.proposed_tool_name)
        driver.session = SimpleNamespace(proposed=spec.proposed_tool_name)
        driver.spec = spec
        cycle.session_open = True

    async def _close():
        if driver.session is None:
            return
        spec, driver.session, driver.spec = driver.spec, None, None
        cycle.session_open = False
        outcome.authored.append(
            MergeFlowResult(tool_spec_id=spec.id, status="failed", reason="co-lead rejected")
        )

    driver._open = _open
    driver._close_current = _close

    first = asyncio.run(driver.advance())
    assert opened == ["link_one"] and "link_one" in first
    # The first link dies. The chain does NOT end: the next one opens, in the same turn.
    second = asyncio.run(driver.advance())
    assert opened == ["link_one", "link_two"], "a failed link ended the chain"
    assert "link_two" in second
    assert len(outcome.authored) == 1 and outcome.authored[0].status == "failed"
    # And when the chain runs out, the lead is told to write its report rather than being cut off.
    done = asyncio.run(driver.advance())
    assert "turn_end" in done and "report" in done


def test_f9_the_worker_note_carries_the_cause_but_not_the_remediation():
    """N8, inside F9's scope: the raw unclean-branch text carries a §9.9 citation, an internal
    repo path and two destructive git commands, and it reached slot 4's worker prompt three
    times — in a role whose own prompt forbids it from touching git."""
    from auto_research.consensus.strategy import _worker_safe_reason

    raw = (
        "authoring branch was not clean at attempt start: research_toolkit (/x/tk) has "
        "uncommitted changes on 'ai'.\n"
        "Uncommitted paths:\n  obsdb_catalog/neutral.py\n"
        "Review and clean before the next run, or re-run with resume=True to auto-heal:\n"
        "  git -C /x/tk status\n"
        "  git -C /x/tk restore . && git -C /x/tk clean -fd   # discard, if appropriate"
    )
    out = _worker_safe_reason(raw)
    assert "was not clean at attempt start" in out, "the cause is what an analyst acts on"
    assert "obsdb_catalog/neutral.py" in out, "and the offending path is legitimate detail"
    assert "git " not in out and "restore ." not in out, "no shell remediation reaches a worker"


def test_f9_the_unclean_error_names_the_dirty_paths(tmp_path):
    """N4: the message used to print remediation and no paths, which is why slot 5's dirtying
    file is permanently undetermined — its clone was swept at teardown."""
    import subprocess

    from auto_research.sandbox.merge_flow import ToolkitBranchUnclean, check_toolkit_branch_clean

    repo = tmp_path / "tk"
    (repo / "obsdb_catalog").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "ai"], check=True, capture_output=True)
    (repo / "obsdb_catalog" / "neutral.py").write_text("def advance_many(): ...\n")

    with pytest.raises(ToolkitBranchUnclean) as exc:
        check_toolkit_branch_clean(repo, "ai")
    assert "obsdb_catalog/neutral.py" in str(exc.value)
