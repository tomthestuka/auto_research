"""C3b (from A4b): a failed lead cycle charges its author credits, and a co-lead that fails inside
``submit_for_review`` or ``respond_to_review`` is an error result of that tool.

Driven through the lead's real cycle (``tests/test_chain_advance.py``'s wiring: real
``run_cycle``, ``_ChainDriver``, ``AuthoringSession``) and, for the co-lead, the real review hook
(``sandbox.review.make_review_hook``) over a real ``AnalystColeadAgent`` whose model is scripted:
its investigation turn, then the verdict call — which raises where the test needs it. The credits
are charged by the real ``DebateToConverge``.
"""

from __future__ import annotations

import asyncio

import pytest

from auto_research.agents.roles import AnalystColeadAgent, ModelCallFailed
from auto_research.consensus import DebateToConverge
from auto_research.models import ModelResponse
from auto_research.sandbox.review import make_review_hook
from auto_research.schemas import AnalysisTask
from tests.test_chain_advance import FINISH, REBUT, SUBMIT, _cycle, _link, _wire
from tests.test_consensus_authoring import AuthoringWorker

ONE = ("author_chain", {"first_link": _link(0), "additional_links": [], "rationale": "r"})


class _VendorError(Exception):
    """A provider error as an SDK raises it: a status code and a message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status


class _ColeadModel:
    """The co-lead's model: a prose investigation turn, then the verdict (or a raise)."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)  # True / False / an exception, one per review

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto", images=(),
                   documents=(), cache_prefix=False):
        if schema is None:
            return ModelResponse(content="I read the diff and the tests.")
        verdict = self.verdicts.pop(0)
        if isinstance(verdict, BaseException):
            raise verdict
        return ModelResponse(parsed={
            "approved": verdict, "rationale": "ok" if verdict else "the edge case is untested",
            "issues": [] if verdict else ["edge case untested"],
            "commit_summary": "Add tool_0" if verdict else "",
        })


def _hook(verdicts):
    colead = AnalystColeadAgent(model=_ColeadModel(verdicts), prompt="co-lead")
    return make_review_hook(colead, author_provider="anthropic", web_source=object())


def _results(model, call):
    return model.seen[call][1]


def test_a_failing_colead_is_an_error_result_and_the_lead_submits_again(tmp_path, monkeypatch):
    overloaded = _VendorError(529, "overloaded_error: the vendor is busy")
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[ONE], [SUBMIT], [SUBMIT], *FINISH], verdicts=[],
        review_hook=_hook([overloaded, True]),
    )
    (failed,) = _results(model, 2)
    assert ("The co-lead review did not run: its vendor did not answer (_VendorError: "
            "overloaded_error: the vendor is busy). Nothing was committed, and this call is not "
            "counted against your budget. Call submit_for_review again to have the work "
            "reviewed.") in failed
    assert outcome is not None and outcome.report is not None, "the lead's turn went on"
    assert [r.status for r in outcome.authored] == ["committed"]
    assert built[0][1].submits_used == 1, "the failed review was not counted"


def test_a_failing_colead_on_a_rebuttal_is_an_error_result(tmp_path, monkeypatch):
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[ONE], [SUBMIT], [REBUT], [REBUT], *FINISH], verdicts=[],
        review_hook=_hook([False, _VendorError(500, "internal error"), True]),
    )
    (failed,) = _results(model, 3)
    assert "The co-lead review did not run: its vendor did not answer (_VendorError: internal error)" in failed
    assert "Call respond_to_review again" in failed
    assert [r.status for r in outcome.authored] == ["committed"]
    assert built[0][1].debates_used == 1, "the failed reconsideration was not counted"


def test_an_account_wide_colead_error_still_ends_the_run(tmp_path, monkeypatch):
    """Ruling (c): an auth refusal is the account's, not the review's; it leaves run_cycle."""
    with pytest.raises(ModelCallFailed) as raised:
        _cycle(tmp_path, monkeypatch, [[ONE], [SUBMIT], *FINISH], verdicts=[],
               review_hook=_hook([_VendorError(401, "invalid x-api-key")]))
    assert raised.value.action == "review_tool" and raised.value.account_wide


def test_a_failed_lead_cycle_charges_the_credits_it_spent(tmp_path, monkeypatch):
    """N11 (no refill): the lead's cycle opens a link (one credit) and its model call fails; the
    debate charges that credit, so the analysis's next cycle starts with one fewer."""

    def fails(model):
        raise _VendorError(529, "overloaded")

    lead, model, built, queries, reviews, repo, store = _wire(
        tmp_path, monkeypatch, [[ONE], fails, *FINISH], verdicts=[True],
    )
    budgets: list[int] = []
    real = type(lead).run_cycle

    async def spy(self, task, reports, **kwargs):  # records what the debate grants, then runs it
        budgets.append(kwargs["author_budget"])
        return await real(self, task, reports, **kwargs)

    monkeypatch.setattr(type(lead), "run_cycle", spy)
    state = {"authored": 0}
    workers = [AuthoringWorker("w1", state, stop_after=5)]
    debate = DebateToConverge(max_rounds=3, tool_author_cap=3, toolkit_root=repo,
                              worktree_base=tmp_path / "wt", direct_branch="ai")
    with pytest.raises(ModelCallFailed) as raised:
        asyncio.run(debate.run(workers, lead, AnalysisTask(question="q")))
    assert raised.value.cycle_outcome.author_budget == 2
    asyncio.run(debate.run(workers, lead, AnalysisTask(question="q")))  # a send-back re-enters
    assert budgets == [3, 2], "the failed cycle's credit is charged and never refilled"


@pytest.mark.parametrize("failure, cause", [
    (_VendorError(400, "prompt is too long"), "_VendorError: prompt is too long"),
    (ValueError("analyst_colead.review_tool: provider returned no structured output"),
     "ValueError: analyst_colead.review_tool: provider returned no structured output"),
], ids=["vendor refusal", "off-schema verdict"])
def test_a_colead_failure_likely_to_recur_is_counted(tmp_path, monkeypatch, failure, cause):
    """A refusal of the request or a verdict still off-schema is said as likely to recur and
    counts against the budget, so the link's own cap bounds the retries (review of C3b)."""
    # One attempt per verdict call, so an off-schema answer is the verdict's last (config).
    monkeypatch.setattr("auto_research.agents.roles._structured_retry_cap", lambda: 1)
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[ONE], [SUBMIT], [SUBMIT], *FINISH], verdicts=[],
        review_hook=_hook([failure, True]),
    )
    (failed,) = _results(model, 2)
    assert (f"The co-lead review did not run: its model call failed ({cause}), and the same request "
            "is likely to fail again. Nothing was committed; this call counts against your budget.") in failed
    assert built[0][1].submits_used == 2, "the failed review was counted"
    assert [r.status for r in outcome.authored] == ["committed"]


def test_a_fault_in_our_own_code_is_raised_as_itself(tmp_path, monkeypatch):
    """Only a model failure is contained: a TypeError from the runtime's own code leaves the
    lead's turn as itself, never as 'call again' (review of C3b)."""
    with pytest.raises(TypeError, match="a bug of ours"):
        _cycle(tmp_path, monkeypatch, [[ONE], [SUBMIT], *FINISH], verdicts=[],
               review_hook=_hook([TypeError("a bug of ours")]))



def test_outside_a_cycle_a_colead_failure_is_raised_as_before(tmp_path):
    """``drive_authoring``'s path (no cycle) backs published measurements, so it is unchanged:
    the co-lead's failure leaves ``submit_for_review`` (review of C3b)."""
    from auto_research.agents.react import execute_submit
    from auto_research.schemas import ToolSpec
    from tests.test_chain_advance import _session
    from tests.test_merge_flow import _FakeLead, _git, _toolkit_repo

    repo = _toolkit_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "ai")
    session = _session(ToolSpec.model_validate(_link(0)), lead=_FakeLead(), repo=repo,
                       worktree_base=tmp_path / "wt", review=_hook([_VendorError(529, "busy")]),
                       queries={})

    async def scenario():
        await session.connect()
        await session.drive("write it")
        try:
            await execute_submit(session)
        finally:
            await session.aclose()

    with pytest.raises(ModelCallFailed):
        asyncio.run(scenario())


def test_passing_failures_are_refunded_three_times_per_session(tmp_path, monkeypatch):
    """Ruling C3c: the fourth passing co-lead failure on a link counts against its budget."""
    busy = [_VendorError(529, "busy") for _ in range(4)]
    outcome, model, built, queries, reviews, repo, store = _cycle(
        tmp_path, monkeypatch, [[ONE], [SUBMIT], [SUBMIT], [SUBMIT], [SUBMIT], [SUBMIT], *FINISH],
        verdicts=[], review_hook=_hook([*busy, True]),
    )
    assert "not counted against your budget" in _results(model, 4)[0]          # the third
    (fourth,) = _results(model, 5)
    assert ("its vendor did not answer (_VendorError: busy). After 3 such failures on this link were "
            "not counted, this call counts against your budget, as will each further one.") in fourth
    assert built[0][1].submits_used == 2 and built[0][1].colead_refunds == 3
    assert [r.status for r in outcome.authored] == ["committed"]


@pytest.mark.parametrize("fault, label", [
    ("correlated", "choosing the co-lead failed: ValueError: injected co-lead shares the author's provider"),
    ("resolve", "choosing the co-lead failed: KeyError"),
    ("web", "building the co-lead's tools failed: RuntimeError: no web source"),
], ids=["shares the author's provider", "no co-lead role", "no web source"])
def test_a_review_set_up_fault_is_raised_as_what_it_is(tmp_path, monkeypatch, fault, label):
    """Ruling C3c: a configuration fault in setting the review up ends the run, labelled as such,
    never as an unreadable changeset."""
    from types import SimpleNamespace

    from auto_research.sandbox.review import ReviewSetupError

    if fault == "correlated":
        model = _ColeadModel([True])
        model.config = SimpleNamespace(provider="anthropic", model="m")
        hook = make_review_hook(AnalystColeadAgent(model=model, prompt="p"),
                                author_provider="anthropic", web_source=object())
    elif fault == "resolve":
        def missing(role):
            raise KeyError(role)

        monkeypatch.setattr("auto_research.models.resolve", missing)
        hook = make_review_hook(None, author_provider="anthropic", web_source=object())
    else:
        def broken():
            raise RuntimeError("no web source")

        monkeypatch.setattr("auto_research.sources.WebSource", broken)
        hook = make_review_hook(AnalystColeadAgent(model=_ColeadModel([True]), prompt="p"),
                                author_provider="anthropic")
    with pytest.raises(ReviewSetupError, match=label.replace("(", r"\(")) as raised:
        _cycle(tmp_path, monkeypatch, [[ONE], [SUBMIT], *FINISH], verdicts=[], review_hook=hook)
    assert "changeset" not in str(raised.value)

