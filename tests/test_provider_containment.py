"""D1 — a provider error that retry cannot fix ends ONE unit's turn, never the run (§9.3.8).

Every worker here is a real ``AnalystWorkerAgent`` over the real ``ModelHandle.call`` →
``call_with_transient_retry`` → ``_react_loop`` path; only the provider adapter (the network) is
faked, and it raises the vendor SDK's own exception classes. The lead is a recording stand-in for
``run_cycle`` (the lead's loop is outside the code under test), holding a real ``RunControl`` so
the inbox notice is the production one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors

import auto_research.models.retry as retry_mod
from auto_research.agents.react import TURN_END_TOOL, LeadCycleOutcome, ReadOnlyToolProvider
from auto_research.agents.roles import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    ProviderTurnFailed,
    ReactBudgetExhausted,
)
from auto_research.consensus import DebateToConverge
from auto_research.env import ProviderRetryPolicy
from auto_research.models.retry import is_provider_error
from auto_research.models.types import ModelHandle, ModelResponse, RoleModelConfig, ToolCall
from auto_research.monitor import RunMonitor
from auto_research.run_control import RunControl
from auto_research.schemas import AnalysisTask, AnalystReport
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

_REQUEST = httpx.Request("POST", "https://api.anthropic.test/v1/messages")


def _too_long() -> anthropic.BadRequestError:
    """The exact error that ended run 32814459 (Anthropic 400, prompt over the window)."""
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: 1005375 tokens > 1000000 maximum",
        },
    }
    return anthropic.BadRequestError(
        "Error code: 400 - prompt is too long",
        response=httpx.Response(400, request=_REQUEST, json=body),
        body=body,
    )


def _overloaded() -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        "Error code: 529 - overloaded",
        response=httpx.Response(529, request=_REQUEST),
        body=None,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    # Cooldowns of zero seconds: the retry path runs for real, it just does not wait.
    monkeypatch.setattr(
        retry_mod,
        "provider_retry_policy",
        lambda: ProviderRetryPolicy(max_retries=2, base_cooldown_s=0.0, max_cooldown_s=0.0),
    )


class _FilingAdapter:
    """A provider adapter scripted to run one tool, write the report, and file it.

    ``failures`` are raised first, one per call — the provider's network, faked. ``fail_at``
    raises a 400 in place of the scripted step of that number (3 = after the report is written,
    before ``turn_end``).
    """

    def __init__(self, failures=(), *, tag: str = "w", fail_at: int | None = None):
        self.failures = list(failures)
        self.calls = 0
        self.tag = tag
        self.fail_at = fail_at
        self._step = 0
        self.seeds: list[str] = []  # the task payload of each turn this adapter answered
        self.requests: list[list] = []  # every request's messages, as sent

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        self.requests.append(list(messages))
        if self.failures:
            raise self.failures.pop(0)
        payload = _fresh_turn_payload(messages)
        if payload is not None:  # a fresh turn: its task payload has no model turn after it
            self._step = 0
            self.seeds.append(payload)
        self._step += 1
        if self._step == self.fail_at:
            raise _too_long()
        if self._step == 1:
            return ModelResponse(
                content="loading", stop="tool_use",
                tool_calls=[ToolCall(id=f"{self.tag}1", name="tk_load", arguments={})],
            )
        if self._step == 2:
            return ModelResponse(content="writing", stop="tool_use", tool_calls=[
                ToolCall(id=f"{self.tag}2", name="write_report", arguments={"content": (
                    f"# summary\n{self.tag} did the fit\n# status\nsucceeded\n"
                    f"# findings\n- {self.tag} finding\n# figures\n- [self:1]\n"
                )}),
            ])
        return ModelResponse(content="done", stop="tool_use", tool_calls=[
            ToolCall(id=f"{self.tag}3", name=TURN_END_TOOL, arguments={"summary": "filed"}),
        ])


def _fresh_turn_payload(messages) -> str | None:
    """The task payload when no model turn follows it (a turn's first call), else ``None``.

    A worker's later turn carries its earlier turns first (the session history rebuilt from the
    ledger), so a fresh turn is recognised by its own payload being last, not by position.
    """
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            return None
        content = getattr(message, "content", None)
        if getattr(message, "role", None) == "user" and isinstance(content, str):
            try:
                data = json.loads(content)
            except ValueError:
                continue
            if isinstance(data, dict) and "task" in data:
                return content
    return None


class _BrokenAdapter:
    """Raises the same exception on every call."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        raise self.exc


def _worker(tmp_path, adapter, index: int, monitor: RunMonitor) -> AnalystWorkerAgent:
    # A real file: `turn_end` refuses a cited figure that is not on disk.
    png = tmp_path / f"fit{index}.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
    fig = {
        "path": str(png), "caption": "fit", "mime_type": "image/png",
        "produced_by": "tk_load", "created_at": datetime.now(UTC).isoformat(),
        "sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
    }
    handle = ModelHandle(
        role="analyst_worker",
        config=RoleModelConfig(name="fake", provider="anthropic", model="fake-model", vision=True),
        provider=adapter,
    )
    worker = AnalystWorkerAgent(
        model=handle,
        prompt="p",
        available_toolkits=[],
        sandbox=_FakeSandbox(tool_fn=lambda m, f, a: {
            "exit_code": 0, "stdout": "loaded", "figures": [fig]}),
        registry=_FakeRegistry(known={"tk_load"}, mcp=set()),
        monitor=monitor,
        worker_index=index,
    )
    worker.agent_key = f"analyst_worker_{index}"
    worker.report_dir = tmp_path / f"w{index}"
    return worker


class _RecordingLead:
    """Stands in for ``AnalystLeadAgent.run_cycle``: records what reached it, then files."""

    def __init__(self, *, rework_first: bool = False):
        self.run_control = RunControl()
        self.agent_key = "analyst_lead"
        self.rework_first = rework_first
        self.cycles: list[dict] = []

    async def run_cycle(self, task, reports, *, sources=None, rounds_left=0, author_budget=0,
                        **kwargs):
        inbox = self.run_control.drain_inbox(self.agent_key)
        self.cycles.append({
            "reports": list(reports),
            "sources": list(sources or []),
            "notices": [(e.kind, e.sender, e.render()) for e in inbox],
        })
        if self.rework_first and len(self.cycles) == 1 and rounds_left > 0:
            return LeadCycleOutcome(
                filed=TURN_END_TOOL,
                report=AnalystReport(summary="again", rework=True, rationale="r",
                                     what_to_probe="refit"),
                author_budget=author_budget,
            )
        return LeadCycleOutcome(
            filed=TURN_END_TOOL,
            report=AnalystReport(summary=f"lead over {len(reports)} report(s)",
                                 findings=["x"], status="succeeded"),
            author_budget=author_budget,
        )


def _monitor(tmp_path) -> RunMonitor:
    """A ledger as production keeps one: every event, written to ``monitor.jsonl``."""
    return RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")


def _events(monitor: RunMonitor, name: str) -> list:
    return [e for e in monitor.events if e.event == name]


# ── classification ──────────────────────────────────────────────────────────


def test_provider_errors_are_the_vendor_sdk_classes_and_nothing_else():
    assert is_provider_error(_too_long())
    assert is_provider_error(_overloaded())
    assert is_provider_error(anthropic.APIConnectionError(request=_REQUEST))
    assert is_provider_error(openai.BadRequestError(
        "context_length_exceeded",
        response=httpx.Response(400, request=_REQUEST), body=None,
    ))
    assert is_provider_error(genai_errors.ClientError(400, {"error": {"message": "too long"}}))
    assert is_provider_error(httpx.ConnectError("boom", request=_REQUEST))
    # Our own defects and the budget's honest failure are not provider errors.
    assert not is_provider_error(ValueError("request building bug"))
    assert not is_provider_error(TypeError("adapter bug"))
    assert not is_provider_error(ReactBudgetExhausted("budget"))


# ── the unit ────────────────────────────────────────────────────────────────


def test_a_400_ends_the_workers_turn_as_its_own_recorded_failure(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _BrokenAdapter(_too_long())
    worker = _worker(tmp_path, adapter, 0, monitor)

    with pytest.raises(ProviderTurnFailed) as caught:
        asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert caught.value.agent == "analyst_worker_0" and caught.value.action == "analyze"
    assert isinstance(caught.value.cause, anthropic.BadRequestError)
    # A 400 is not waited out: exactly one request reached the provider.
    assert adapter.calls == 1
    (event,) = _events(monitor, "analyst_worker.analyze.provider_error")
    assert event.level == 1 and event.actor == "analyst_worker_0"
    assert event.data["status_code"] == 400 and event.data["filed"] is False
    assert "prompt is too long" in event.data["error"]
    assert event.data["iteration"] == 0


def test_a_transient_error_keeps_todays_retry_and_the_worker_files(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _FilingAdapter([_overloaded()], tag="w0")
    worker = _worker(tmp_path, adapter, 0, monitor)

    report = asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert report.summary == "w0 did the fit" and report.status == "succeeded"
    assert adapter.calls == 4  # one 529, retried, then the three scripted turns
    assert not _events(monitor, "analyst_worker.analyze.provider_error")


def test_a_spent_retry_budget_is_contained_like_a_400(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _BrokenAdapter(_overloaded())
    worker = _worker(tmp_path, adapter, 0, monitor)

    with pytest.raises(ProviderTurnFailed):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert adapter.calls == 3  # first try + max_retries=2
    assert _events(monitor, "analyst_worker.analyze.provider_error")


def test_our_own_defect_is_not_contained(tmp_path):
    monitor = _monitor(tmp_path)
    worker = _worker(tmp_path, _BrokenAdapter(ValueError("adapter bug")), 0, monitor)

    with pytest.raises(ValueError, match="adapter bug"):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert not _events(monitor, "analyst_worker.analyze.provider_error")


# ── the debate ──────────────────────────────────────────────────────────────


def test_one_worker_fails_its_peer_files_and_the_run_reaches_the_lead(tmp_path):
    """The D1 check item: the failed worker fails, its peer files, the lead runs, the ledger shows it."""
    monitor = _monitor(tmp_path)
    workers = [
        _worker(tmp_path, _BrokenAdapter(_too_long()), 0, monitor),
        _worker(tmp_path, _FilingAdapter(tag="w1"), 1, monitor),
    ]
    lead = _RecordingLead()

    final = asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        )
    )

    assert final.summary == "lead over 1 report(s)"
    (cycle,) = lead.cycles
    assert cycle["sources"] == ["analyst_worker_1"]
    assert [r.summary for r in cycle["reports"]] == ["w1 did the fit"]
    # The lead is told who is missing, as a runtime notice through its inbox.
    (notice,) = cycle["notices"]
    assert notice[0] == "notice" and notice[1] == "runtime"
    assert "analyst_worker_0 filed no report" in notice[2] and "analyst_worker_1" in notice[2]
    # Ledger: the unit's own failure, and the debate carrying on without it.
    assert _events(monitor, "analyst_worker.analyze.provider_error")
    (failed,) = _events(monitor, "analyst.worker.failed")
    assert failed.actor == "analyst_worker_0" and failed.data["status_code"] == 400
    assert len(_events(monitor, "analyst.worker.initial_report")) == 1
    assert _events(monitor, "analyst.lead.notice")


def test_a_failed_worker_is_asked_again_next_round_and_is_no_ones_peer(tmp_path):
    monitor = _monitor(tmp_path)
    # Fails its first turn only; files in the revision round.
    flaky = _FilingAdapter([_too_long()], tag="w0")
    steady = _FilingAdapter(tag="w1")
    workers = [
        _worker(tmp_path, flaky, 0, monitor),
        _worker(tmp_path, steady, 1, monitor),
    ]
    lead = _RecordingLead(rework_first=True)

    asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        )
    )

    assert [c["sources"] for c in lead.cycles] == [
        ["analyst_worker_1"], ["analyst_worker_0", "analyst_worker_1"]
    ]
    assert lead.cycles[0]["notices"] and not lead.cycles[1]["notices"]
    assert len(_events(monitor, "analyst.worker.revision")) == 2
    # Worker 1's revision turn saw no peer report: the worker that filed nothing is no peer.
    # Worker 0's revision turn saw worker 1's round-1 report.
    assert len(steady.seeds) == 2 and len(flaky.seeds) == 1
    assert json.loads(steady.seeds[1])["peer_reports"] == []
    assert [r["summary"] for r in json.loads(flaky.seeds[0])["peer_reports"]] == ["w1 did the fit"]


def test_every_worker_failing_still_reaches_the_lead_with_nothing_filed(tmp_path):
    monitor = _monitor(tmp_path)
    workers = [
        _worker(tmp_path, _BrokenAdapter(_too_long()), 0, monitor),
        _worker(tmp_path, _BrokenAdapter(_too_long()), 1, monitor),
    ]
    lead = _RecordingLead()

    asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        )
    )

    (cycle,) = lead.cycles
    assert cycle["reports"] == [] and cycle["sources"] == []
    assert "come from: no worker" in cycle["notices"][0][2]
    assert len(_events(monitor, "analyst.worker.failed")) == 2


def test_budget_exhaustion_is_never_caught_by_the_debate():
    class _Exhausted:
        async def analyze(self, task, *, peer_reports=()):
            raise ReactBudgetExhausted("analyst_worker.analyze: budget and grace spent")

    with pytest.raises(ReactBudgetExhausted):
        asyncio.run(DebateToConverge(max_rounds=3).run(
            [_Exhausted()], _RecordingLead(), AnalysisTask(question="q")
        ))


class _LeadAdapter:
    """The lead's provider: records each request, cites worker 1's figure, files."""

    def __init__(self, *, fail: BaseException | None = None):
        self.fail = fail
        self.requests: list[list] = []

    async def call(self, messages, config, **kwargs):
        self.requests.append(list(messages))
        if self.fail is not None:
            raise self.fail
        if _fresh_turn_payload(messages) is not None:
            return ModelResponse(content="writing", stop="tool_use", tool_calls=[
                ToolCall(id="l1", name="write_report", arguments={"content": (
                    "# summary\nlead synthesis over the worker that filed\n# status\nsucceeded\n"
                    "# findings\n- the fit stands on one worker\n# figures\n- [analyst_worker_1:1]\n"
                )}),
            ])
        return ModelResponse(content="done", stop="tool_use", tool_calls=[
            ToolCall(id="l2", name=TURN_END_TOOL, arguments={"summary": "filed"}),
        ])


def _real_lead(tmp_path, adapter, monitor) -> AnalystLeadAgent:
    lead = AnalystLeadAgent(
        model=ModelHandle(
            role="analyst_lead",
            config=RoleModelConfig(name="fake", provider="anthropic", model="fake-lead",
                                   vision=True),
            provider=adapter,
        ),
        prompt="p",
        monitor=monitor,
    )
    lead.report_dir = tmp_path / "lead"
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    return lead


def _text(message) -> str:
    return message.content if isinstance(getattr(message, "content", None), str) else ""


def test_the_real_lead_reads_the_notice_and_sees_only_the_filed_worker(tmp_path):
    monitor = _monitor(tmp_path)
    workers = [
        _worker(tmp_path, _BrokenAdapter(_too_long()), 0, monitor),
        _worker(tmp_path, _FilingAdapter(tag="w1"), 1, monitor),
    ]
    adapter = _LeadAdapter()
    lead = _real_lead(tmp_path, adapter, monitor)

    final = asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        )
    )

    assert final.summary == "lead synthesis over the worker that filed"
    first = adapter.requests[0]
    payload = json.loads(_fresh_turn_payload(first))
    assert payload["worker_sources"] == ["analyst_worker_1"]
    assert [r["summary"] for r in payload["worker_reports"]] == ["w1 did the fit"]
    notices = [_text(m) for m in first[2:] if _text(m).startswith("[Notice]")]
    assert len(notices) == 1 and "analyst_worker_0 filed no report" in notices[0]


def test_a_written_but_unfiled_report_is_not_passed_on(tmp_path):
    """The case §2b guards: the worker wrote its report, then the provider failed before turn_end."""
    monitor = _monitor(tmp_path)
    late = _FilingAdapter(tag="w0", fail_at=3)
    workers = [
        _worker(tmp_path, late, 0, monitor),
        _worker(tmp_path, _FilingAdapter(tag="w1"), 1, monitor),
    ]
    lead = _RecordingLead()

    asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        )
    )

    # The draft exists on disk (edit_report saved it) ...
    assert any("w0 did the fit" in p.read_text() for p in (tmp_path / "w0").rglob("*.jsonl"))
    # ... and the lead receives none of it.
    (cycle,) = lead.cycles
    assert cycle["sources"] == ["analyst_worker_1"]
    assert all("w0" not in r.summary for r in cycle["reports"])
    (event,) = _events(monitor, "analyst_worker.analyze.provider_error")
    assert event.data["iteration"] == 2 and event.data["filed"] is False


def test_the_middle_of_three_workers_failing_keeps_sources_aligned(tmp_path):
    monitor = _monitor(tmp_path)
    workers = [
        _worker(tmp_path, _FilingAdapter(tag="w0"), 0, monitor),
        _worker(tmp_path, _BrokenAdapter(_too_long()), 1, monitor),
        _worker(tmp_path, _FilingAdapter(tag="w2"), 2, monitor),
    ]
    lead = _RecordingLead()

    asyncio.run(
        DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        )
    )

    (cycle,) = lead.cycles
    assert cycle["sources"] == ["analyst_worker_0", "analyst_worker_2"]
    assert [r.summary for r in cycle["reports"]] == ["w0 did the fit", "w2 did the fit"]


def test_a_text_the_failed_call_carried_reaches_the_next_turn(tmp_path):
    """Delivered into the failed request, the text is in that turn's ledger history, which the
    worker's next turn carries (B2) — so it arrives there exactly once, and is not re-queued."""
    monitor = _monitor(tmp_path)
    adapter = _FilingAdapter([_too_long()], tag="w0")
    worker = _worker(tmp_path, adapter, 0, monitor)
    worker.run_control = RunControl()
    worker.run_control.add_steer("analyst_worker_0", "check the background region")
    worker._kept_turn = {"messages": [], "stale": True}  # a turn filed in an earlier round

    with pytest.raises(ProviderTurnFailed):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert any("check the background region" in _text(m) for m in adapter.requests[0])
    # Nothing re-queued, and the stale kept turn can no longer be resumed.
    assert not worker.run_control.has_inbox("analyst_worker_0")
    assert not worker.has_kept_turn()
    (event,) = _events(monitor, "analyst_worker.analyze.provider_error")
    assert event.data["requeued_texts"] == 0

    report = asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert report.summary == "w0 did the fit"
    second_turn = adapter.requests[1]
    # The next turn opens with one transcript of the earlier turn (ruling B6.2), which holds the
    # text once; it is not re-queued as a message of its own.
    joined = "\n".join(_text(m) for m in second_turn)
    assert joined.count("[Human steer] check the background region") == 1


def test_a_text_held_behind_a_server_tool_is_requeued_once(tmp_path, monkeypatch):
    """Held texts never reached the model; they too go back to the inbox, exactly once."""
    import auto_research.agents.roles as roles_mod

    # The hold rule is not under test: force the "unresolved server tool" branch.
    monkeypatch.setattr(roles_mod, "_last_turn_has_unresolved_server_tool", lambda messages: True)
    monitor = _monitor(tmp_path)
    adapter = _FilingAdapter([_too_long()], tag="w0")
    worker = _worker(tmp_path, adapter, 0, monitor)
    worker.run_control = RunControl()
    worker.run_control.add_steer("analyst_worker_0", "check the background region")

    with pytest.raises(ProviderTurnFailed):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert not any("background region" in _text(m) for m in adapter.requests[0])
    entries = worker.run_control.drain_inbox("analyst_worker_0")
    assert [e.render() for e in entries] == ["[Human steer] check the background region"]
    (event,) = _events(monitor, "analyst_worker.analyze.provider_error")
    assert event.data["requeued_texts"] == 1


def _seated(tmp_path, adapter, monitor) -> AnalystWorkerAgent:
    """A worker seated on the quick channel, so a filed turn is kept (E12)."""
    from auto_research.hitl.router import SeatRouter

    worker = _worker(tmp_path, adapter, 0, monitor)
    worker.run_control = RunControl()
    worker.channel = SeatRouter(worker.run_control, monitor=monitor)
    worker.channel.register(worker.agent_key, worker)
    return worker


def test_a_failed_resume_clears_the_turn_it_was_resuming(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _FilingAdapter(tag="w0")
    worker = _seated(tmp_path, adapter, monitor)
    asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert worker.has_kept_turn()

    adapter.failures = [_too_long()]
    # A resume runs because a text arrived (an empty inbox has nothing to resume for).
    worker.run_control.add_steer(worker.agent_key, "one more thing")
    with pytest.raises(ProviderTurnFailed) as caught:
        asyncio.run(worker.resume())
    assert caught.value.action == "resume"
    assert not worker.has_kept_turn()


class _ReplacingAdapter(_FilingAdapter):
    """Fails once; just before failing, a concurrent turn of the same seat files (E12)."""

    def __init__(self, worker_ref: dict, newer: dict, **kwargs):
        super().__init__(**kwargs)
        self.worker_ref = worker_ref
        self.newer = newer
        self.armed = False

    async def call(self, messages, config, **kwargs):
        if self.armed:
            self.armed = False
            self.worker_ref["w"]._kept_turn = self.newer
            raise _too_long()
        return await super().call(messages, config, **kwargs)


def test_a_failed_resume_keeps_a_newer_turn_filed_meanwhile(tmp_path):
    monitor = _monitor(tmp_path)
    ref: dict = {}
    newer = {"messages": [], "tools": None, "state": {}, "declared": [], "kwargs": {}}
    adapter = _ReplacingAdapter(ref, newer, tag="w0")
    worker = _seated(tmp_path, adapter, monitor)
    ref["w"] = worker
    asyncio.run(worker.analyze(AnalysisTask(question="q")))

    adapter.armed = True
    worker.run_control.add_steer(worker.agent_key, "one more thing")
    with pytest.raises(ProviderTurnFailed):
        asyncio.run(worker.resume())
    assert worker._kept_turn is newer


def test_cancellation_is_never_contained(tmp_path):
    monitor = _monitor(tmp_path)
    worker = _worker(tmp_path, _BrokenAdapter(asyncio.CancelledError()), 0, monitor)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert not _events(monitor, "analyst_worker.analyze.provider_error")


def test_a_google_error_records_its_status_from_code(tmp_path):
    monitor = _monitor(tmp_path)
    worker = _worker(
        tmp_path,
        _BrokenAdapter(genai_errors.ClientError(400, {"error": {"message": "too long"}})),
        0, monitor,
    )

    with pytest.raises(ProviderTurnFailed) as caught:
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert caught.value.status_code == 400
    assert _events(monitor, "analyst_worker.analyze.provider_error")[0].data["status_code"] == 400


def test_the_leads_own_provider_failure_ends_the_analysis_recorded(tmp_path):
    """The lead is the only unit that files the analysis result: no peer carries on in its place,
    so its failure leaves the debate (and ends the run in `run_research`), recorded first."""
    monitor = _monitor(tmp_path)
    workers = [_worker(tmp_path, _FilingAdapter(tag="w0"), 0, monitor)]
    lead = _real_lead(tmp_path, _LeadAdapter(fail=_too_long()), monitor)

    with pytest.raises(ProviderTurnFailed) as caught:
        asyncio.run(DebateToConverge(max_rounds=3, monitor=monitor).run(
            workers, lead, AnalysisTask(question="q")
        ))
    assert caught.value.agent == "analyst_lead" and caught.value.action == "run_cycle"
    (event,) = _events(monitor, "analyst_lead.run_cycle.provider_error")
    assert event.level == 1 and event.data["status_code"] == 400
