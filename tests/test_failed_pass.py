"""A4b — a lead or librarian pass whose model call failed returns to the reviewer with no report.

Ruled option 1 (2026-11-04): the literature/analysis node catches the unit's ``ModelCallFailed``,
records ``<team>.failed``, counts the pass against the team's cap (a), and the reviewer's next turn
is told which unit failed and why — a runtime notice, navigation, never a receipt. A lead that fails
after its chain committed still leaves the toolkit reloaded and the workers restarted (b). An
account-wide provider error (401/403, every key spent) keeps ending the run (c).

Every case runs the real graph (``build_graph`` + ``ainvoke``) over real role turns — the real
``AnalystLeadAgent.run_cycle`` inside the real ``DebateToConverge``, the real
``LibrarianIOAgent.research`` — with only each vendor's network faked. The reviewer is a recording
stand-in: what it was handed is what is under test, not its own loop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anthropic
import httpx
import pytest

import auto_research.models.retry as retry_mod
from auto_research.agents.roles import LibrarianIOAgent, ModelCallFailed
from auto_research.consensus import DebateToConverge
from auto_research.env import ProviderRetryPolicy
from auto_research.graph import build_graph
from auto_research.models.types import ModelHandle, RoleModelConfig
from auto_research.schemas import ResearchRequest, ReviewerReport
from auto_research.schemas.review import ReviewerSection
from tests.test_graph import FakeIOAgent, FakeRetrieval, _invoke
from tests.test_librarian import FakeStorage
from tests.test_provider_containment import (
    _BrokenAdapter,
    _FilingAdapter,
    _LeadAdapter,
    _events,
    _monitor,
    _real_lead,
    _worker,
)

_REQUEST = httpx.Request("POST", "https://api.anthropic.test/v1/messages")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(
        retry_mod, "provider_retry_policy",
        lambda: ProviderRetryPolicy(max_retries=0, base_cooldown_s=0.0, max_cooldown_s=0.0),
    )


class _Reviewer:
    """Files the next assignment of a script and records every keyword each turn was handed."""

    def __init__(self, script):
        self.script = list(script)
        self.turns: list[dict] = []

    async def review(self, request, **kwargs):
        self.turns.append(kwargs)
        target = self.script[len(self.turns) - 1] if len(self.turns) <= len(self.script) else "human"
        if target == "human":
            return ReviewerReport(rationale="r", assign_to="human", title="T", abstract="A",
                                  sections=[ReviewerSection(heading="H", body="B")],
                                  key_findings=["K"])
        return ReviewerReport(rationale="r", assign_to=target, follow_up="the task")


def _graph(reviewer, *, io_agent=None, workers=(), lead=None, consensus=None, monitor=None,
           max_iterations=3):
    return build_graph(
        reviewer=reviewer, io_agent=io_agent or FakeIOAgent(), retrieval=FakeRetrieval(),
        workers=list(workers), lead=lead, consensus=consensus, monitor=monitor,
        max_iterations=max_iterations, report_formats=[],
    )


def _analysis(tmp_path, monitor, lead_adapter, **consensus):
    workers = [_worker(tmp_path, _FilingAdapter(tag="w0"), 0, monitor)]
    lead = _real_lead(tmp_path, lead_adapter, monitor)
    return workers, lead, DebateToConverge(max_rounds=3, monitor=monitor, **consensus)


def _librarian(tmp_path, monitor, adapter) -> LibrarianIOAgent:
    agent = LibrarianIOAgent(
        sources={}, storage=FakeStorage(), monitor=monitor, prompt="p",
        model=ModelHandle(role="librarian_io",
                          config=RoleModelConfig(name="lib", provider="anthropic", model="fake-lib"),
                          provider=adapter),
    )
    agent.report_dir = tmp_path / "librarian_io"
    return agent


def test_a_failed_lead_returns_the_analysis_pass_to_the_reviewer_with_the_notice(tmp_path):
    monitor = _monitor(tmp_path)
    workers, lead, consensus = _analysis(
        tmp_path, monitor, _LeadAdapter(fail=ValueError("request building failed")))
    reviewer = _Reviewer(("analysts", "human"))

    state = _invoke(_graph(reviewer, workers=workers, lead=lead, consensus=consensus,
                           monitor=monitor), ResearchRequest(question="q"))

    first, second = reviewer.turns
    assert second["arrived"] == "analysts_failed"
    assert second["failure"] == (
        "analyst_lead's model call raised ValueError (request building failed), so its pass "
        "filed no report. The exception was raised in the runtime's own code (it carries no "
        "vendor status), so the same pass is likely to fail the same way. Report it to the human "
        "as a defect of the system.")
    assert "failure" not in first  # the notice rides only the failed pass's arrival
    assert second["budgets"] == {"librarian": 3, "analysts": 2}  # (a): the pass was spent
    assert state["analysis_iterations"] == 1 and state.get("report") is None  # nothing composed
    (event,) = _events(monitor, "analyst.failed")
    assert event.level == 1 and event.actor == "analyst_lead"
    assert event.data["error_type"] == "ValueError" and event.data["passes_used"] == 1
    assert _events(monitor, "analyst_lead.run_cycle.model_call_failed")


def test_a_failed_librarian_returns_the_literature_pass_to_the_reviewer(tmp_path):
    monitor = _monitor(tmp_path)
    librarian = _librarian(tmp_path, monitor, _BrokenAdapter(ValueError("bad request shape")))
    reviewer = _Reviewer(("librarian", "human"))

    state = _invoke(_graph(reviewer, io_agent=librarian, monitor=monitor),
                    ResearchRequest(question="q"))

    second = reviewer.turns[1]
    assert second["arrived"] == "librarian_failed"
    assert second["failure"].startswith("librarian_io's model call raised ValueError")
    assert second["budgets"]["librarian"] == 2
    assert state["lit_iterations"] == 1 and not state.get("literature")
    (event,) = _events(monitor, "librarian.failed")
    assert event.actor == "librarian_io" and event.data["action"] == "research"


def test_failures_that_spend_the_cap_leave_the_reviewer_only_the_human(tmp_path):
    """With one analysis pass and a failing lead, the reviewer's next turn has none left, so its
    `turn_end` takes only the librarian or the human (it refuses an assignment past a cap)."""
    from auto_research.agents.reviewer_tools import reviewer_turn_problems

    monitor = _monitor(tmp_path)
    workers, lead, consensus = _analysis(
        tmp_path, monitor, _LeadAdapter(fail=ValueError("request building failed")))
    reviewer = _Reviewer(("analysts", "human"))

    _invoke(_graph(reviewer, workers=workers, lead=lead, consensus=consensus, monitor=monitor,
                   max_iterations=1), ResearchRequest(question="q"))

    budgets = reviewer.turns[1]["budgets"]
    assert budgets == {"librarian": 1, "analysts": 0}
    again = ReviewerReport(rationale="r", assign_to="analysts", follow_up="once more")
    problems = reviewer_turn_problems(again, lookup=None, scope=(), budgets=budgets)
    assert any("analysts" in p for p in problems)


class _Registry:
    def __init__(self):
        self.reloads = 0

    def tool_catalogue(self):
        return ""

    def reload(self):
        self.reloads += 1


class _RestartingSandbox:
    def __init__(self):
        self.restarts = 0

    async def restart(self):
        self.restarts += 1


def test_a_lead_failing_where_authoring_runs_leaves_the_toolkit_reloaded(tmp_path):
    """(b) The cycle's commit count goes with the failure, so a debate that can author reloads
    discovery and restarts every worker backend before the pass returns to the reviewer."""
    monitor = _monitor(tmp_path)
    registry = _Registry()
    workers, lead, consensus = _analysis(
        tmp_path, monitor, _LeadAdapter(fail=ValueError("request building failed")),
        registry=registry, toolkit_root=tmp_path / "toolkit")
    sandbox = _RestartingSandbox()
    workers[0].sandbox.restart = sandbox.restart
    reviewer = _Reviewer(("analysts", "human"))

    graph = build_graph(
        reviewer=reviewer, io_agent=FakeIOAgent(), retrieval=FakeRetrieval(), workers=workers,
        lead=lead, consensus=consensus, monitor=monitor, registry=registry, report_formats=[],
    )
    _invoke(graph, ResearchRequest(question="q"))

    assert reviewer.turns[1]["arrived"] == "analysts_failed"
    assert registry.reloads == 1 and sandbox.restarts == 1


def _unauthorized() -> anthropic.AuthenticationError:
    body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    return anthropic.AuthenticationError(
        "Error code: 401", response=httpx.Response(401, request=_REQUEST, json=body), body=body)


def _spent() -> anthropic.BadRequestError:
    body = {"type": "error", "error": {"type": "invalid_request_error",
                                       "message": "Your credit balance is too low"}}
    return anthropic.BadRequestError(
        "Error code: 400 - credit balance", response=httpx.Response(400, request=_REQUEST, json=body),
        body=body)


@pytest.mark.parametrize("error", [_unauthorized, _spent], ids=["401", "every key spent"])
def test_an_account_wide_error_from_the_lead_still_ends_the_run(tmp_path, error):
    """(c) pending its own ruling (CLAUDE.md §13): the reviewer's call would meet it next."""
    monitor = _monitor(tmp_path)
    workers, lead, consensus = _analysis(tmp_path, monitor, _LeadAdapter(fail=error()))
    reviewer = _Reviewer(("analysts", "human"))

    with pytest.raises(ModelCallFailed) as caught:
        _invoke(_graph(reviewer, workers=workers, lead=lead, consensus=consensus,
                       monitor=monitor), ResearchRequest(question="q"))
    assert caught.value.provider == "anthropic"  # classified in the failing seat's vocabulary
    assert len(reviewer.turns) == 1 and not _events(monitor, "analyst.failed")


def test_an_account_wide_error_from_the_librarian_still_ends_the_run(tmp_path):
    monitor = _monitor(tmp_path)
    librarian = _librarian(tmp_path, monitor, _BrokenAdapter(_unauthorized()))
    reviewer = _Reviewer(("librarian", "human"))

    with pytest.raises(ModelCallFailed):
        _invoke(_graph(reviewer, io_agent=librarian, monitor=monitor),
                ResearchRequest(question="q"))
    assert not _events(monitor, "librarian.failed")


def test_the_reviewer_reads_the_failure_in_its_turn(tmp_path):
    """The real reviewer's turn text names the failed pass, the notice and the passes left."""
    from auto_research.agents.roles import _review_instruction

    text = _review_instruction("analysts_failed", {"librarian": 2, "analysts": 1}, False,
                               failure="analyst_lead's model call raised ValueError (x).")
    assert text.startswith("The analysts' pass ended without a report, and it counts as one of "
                           "their passes. analyst_lead's model call raised ValueError (x).")
    assert "1 analysis pass(es) remain" in text


def test_a_worker_failure_stays_inside_the_debate(tmp_path):
    """D1 is unchanged: a worker's failure never reaches the node; the lead files over the rest."""
    from tests.test_provider_containment import _RecordingLead

    monitor = _monitor(tmp_path)
    workers = [_worker(tmp_path, _BrokenAdapter(ValueError("x")), 0, monitor),
               _worker(tmp_path, _FilingAdapter(tag="w1"), 1, monitor)]
    reviewer = _Reviewer(("analysts", "human"))
    state = _invoke(_graph(reviewer, workers=workers, lead=_RecordingLead(),
                           consensus=DebateToConverge(max_rounds=3, monitor=monitor),
                           monitor=monitor), ResearchRequest(question="q"))
    assert reviewer.turns[1]["arrived"] == "analysts" and state["report"] is not None
    assert not _events(monitor, "analyst.failed")


def test_a_co_leads_spent_key_is_classified_by_the_co_leads_vendor(tmp_path):
    """Inside the lead's pass the failing seat may be the co-lead on another vendor: its error is
    read in that vendor's vocabulary (an OpenAI spent balance arrives as a 429)."""
    import openai

    request = httpx.Request("POST", "https://api.openai.test/v1/responses")
    body = {"error": {"type": "insufficient_quota", "code": "insufficient_quota",
                      "message": "You exceeded your current quota"}}
    spent = openai.RateLimitError("Error code: 429", response=httpx.Response(429, request=request,
                                  json=body), body=body["error"])

    class _ColeadFails:
        agent_key = "analyst_lead"
        model = SimpleNamespace(config=RoleModelConfig(name="l", provider="anthropic", model="m"))

        async def run_cycle(self, task, reports, **kwargs):
            raise ModelCallFailed(agent="analyst_colead", action="review_tool", cause=spent,
                                  provider="openai")

    monitor = _monitor(tmp_path)
    workers = [_worker(tmp_path, _FilingAdapter(tag="w0"), 0, monitor)]
    with pytest.raises(ModelCallFailed):
        _invoke(_graph(_Reviewer(("analysts", "human")), workers=workers, lead=_ColeadFails(),
                       consensus=DebateToConverge(max_rounds=3, monitor=monitor), monitor=monitor),
                ResearchRequest(question="q"))
    assert not _events(monitor, "analyst.failed")


def test_a_reload_that_fails_after_the_leads_failure_still_returns_the_pass(tmp_path):
    monitor = _monitor(tmp_path)
    registry = _Registry()

    def _broken_reload():
        raise RuntimeError("toolkit import failed")

    registry.reload = _broken_reload
    workers, lead, consensus = _analysis(
        tmp_path, monitor, _LeadAdapter(fail=ValueError("request building failed")),
        registry=registry, toolkit_root=tmp_path / "toolkit")
    reviewer = _Reviewer(("analysts", "human"))
    graph = build_graph(
        reviewer=reviewer, io_agent=FakeIOAgent(), retrieval=FakeRetrieval(), workers=workers,
        lead=lead, consensus=consensus, monitor=monitor, registry=registry, report_formats=[],
    )
    _invoke(graph, ResearchRequest(question="q"))
    assert reviewer.turns[1]["arrived"] == "analysts_failed"
    (event,) = _events(monitor, "analyst.toolkit.reload_error")
    assert event.level == 1 and "toolkit import failed" in event.data["error"]


def test_the_real_reviewer_is_told_the_failed_pass_in_its_turn(tmp_path):
    """Through the real `ReviewerAgent.review` (its model faked): the turn's instruction names the
    failed pass and the notice, and no analysis report is presented as arrived."""
    import json

    from auto_research.agents.react import TURN_END_TOOL, WRITE_REPORT_TOOL
    from tests.test_agents import _reviewer

    agent, fake = _reviewer(tmp_path, [
        (WRITE_REPORT_TOOL, {"content": "# assign_to\nlibrarian\n# rationale\nRetry the "
                                        "search.\n# follow_up\nFind the lag.\n"}),
        (TURN_END_TOOL, {"summary": "dispatched"}),
    ])
    notice = "analyst_lead's model call raised ValueError (x), so its pass filed no report."
    asyncio.run(agent.review(ResearchRequest(question="q"), arrived="analysts_failed",
                             failure=notice, budgets={"librarian": 2, "analysts": 1}))
    payload = json.loads(fake.messages[1].content)
    assert payload["arrived"] == "analysts_failed"
    assert payload["your_turn"].startswith(
        "The analysts' pass ended without a report, and it counts as one of their passes. "
        + notice)
    assert payload["analysis_report"] is None


def _too_long() -> anthropic.BadRequestError:
    body = {"type": "error", "error": {"type": "invalid_request_error",
                                       "message": "prompt is too long"}}
    return anthropic.BadRequestError(
        "Error code: 400 - prompt is too long",
        response=httpx.Response(400, request=_REQUEST, json=body), body=body)


def _overloaded() -> anthropic.APIStatusError:
    return anthropic.APIStatusError("Error code: 529 - overloaded",
                                    response=httpx.Response(529, request=_REQUEST), body=None)


@pytest.mark.parametrize(("error", "kind", "reading"), [
    (_too_long, "vendor", "The vendor refused this request with HTTP 400: the same request is "
                          "likely to be refused again. Report it to the human as a defect of the "
                          "system."),
    (_overloaded, "vendor", "The vendor answered with HTTP 529: a vendor error (capacity or "
                            "rate), which a later pass may clear."),
    (lambda: httpx.ConnectError("connection refused", request=_REQUEST), "network",
     "The vendor could not be reached after the retries: a network failure, which a later pass "
     "may clear."),
    (lambda: ValueError("request building failed"), "runtime",
     "The exception was raised in the runtime's own code (it carries no vendor status), so the "
     "same pass is likely to fail the same way. Report it to the human as a defect of the system."),
], ids=["vendor refusal", "vendor capacity", "network", "runtime"])
def test_the_notice_says_what_kind_of_failure_it_was(tmp_path, error, kind, reading):
    """A4c: the reviewer can tell a failure a later pass may clear from a runtime defect that
    recurs on the same input, and report the latter to the human."""
    monitor = _monitor(tmp_path)
    workers, lead, consensus = _analysis(tmp_path, monitor, _LeadAdapter(fail=error()))
    reviewer = _Reviewer(("analysts", "human"))

    _invoke(_graph(reviewer, workers=workers, lead=lead, consensus=consensus, monitor=monitor),
            ResearchRequest(question="q"))

    assert reviewer.turns[1]["failure"].endswith("so its pass filed no report. " + reading)
    (event,) = _events(monitor, "analyst.failed")
    assert event.data["kind"] == kind
