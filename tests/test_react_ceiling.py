"""Ruling D4 (2026-10-18): the absolute ceiling is a cap with the same grace window as the budget,
and only an accepted ``turn_end`` advances a draft to a report.

Reproduced first on a real worker: a ``turn_end`` refused on every call (all calls budget-free)
ran to the absolute ceiling, the loop returned unfiled, and ``analyze()`` handed the unfiled draft
on as the report, status ``succeeded``. Every test drives a real role through ``ModelHandle`` with
only the provider adapter faked; the caps come from the module's own config seams, set small.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime

import pytest

import auto_research.agents.roles as roles_mod
import auto_research.models.retry as retry_mod
from auto_research.agents.react import TURN_END_TOOL, ReadOnlyToolProvider
from auto_research.agents.roles import AnalystLeadAgent, AnalystWorkerAgent, ReactBudgetExhausted
from auto_research.env import ProviderRetryPolicy
from auto_research.models.types import ModelHandle, ModelResponse, RoleModelConfig, ToolCall
from auto_research.monitor import RunMonitor
from auto_research.schemas import AnalysisTask
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

# CEILING sits above the floor `_react_loop` enforces (CAP + GRACE + 1 = 8), so the configured
# ceiling is the one under test.
CAP, CEILING, GRACE = 3, 12, 4


@pytest.fixture(autouse=True)
def _small_caps(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setattr(roles_mod, "_react_loop_cap", lambda: CAP)
    monkeypatch.setattr(roles_mod, "_react_loop_absolute_cap", lambda: CEILING)
    monkeypatch.setattr(roles_mod, "react_grace_rounds", lambda: GRACE)
    monkeypatch.setattr(
        retry_mod, "provider_retry_policy",
        lambda: ProviderRetryPolicy(max_retries=0, base_cooldown_s=0.0, max_cooldown_s=0.0),
    )


def _text(message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    results = getattr(message, "results", None)
    return " ".join(p.content for p in results) if results else ""


def _saw_grace(messages) -> bool:
    return any("run out of rounds" in _text(m) for m in messages)


class _RefusedForever:
    """Writes a report citing a figure that is not on disk, then calls ``turn_end`` forever.

    ``turn_end`` refuses (the figure is missing) and every call is budget-free: the exact shape
    that reached the absolute ceiling. With ``fix_in_grace`` it writes nothing at first (so
    ``turn_end`` refuses an unwritten report), and once the grace notice arrives it writes a
    report with no figure and files.
    """

    def __init__(self, *, fix_in_grace: bool = False):
        self.calls = 0
        self.fix_in_grace = fix_in_grace
        self.grace_seen_at: int | None = None
        self.countdowns: list[str] = []
        self._fixed = False

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        last = _text(messages[-1]) if messages else ""
        if "grace round(s) left" in last:
            self.countdowns.append(last.rsplit("[", 1)[-1])
        if _saw_grace(messages) and self.grace_seen_at is None:
            self.grace_seen_at = self.calls
        if self.calls == 1 and not self.fix_in_grace:
            return ModelResponse(content="writing", stop="tool_use", tool_calls=[
                ToolCall(id="e1", name="write_report", arguments={"content": (
                    "# summary\nfit done\n# status\nsucceeded\n# findings\n- the fit converged\n"
                    "# figures\n- [self:1]\n"
                )}),
            ])
        if self.fix_in_grace and self.grace_seen_at is not None and not self._fixed:
            self._fixed = True
            return ModelResponse(content="writing it now", stop="tool_use", tool_calls=[
                ToolCall(id="e2", name="write_report", arguments={"content": (
                    "# summary\nfit done\n# status\nsucceeded\n# findings\n- the fit converged\n"
                )}),
            ])
        return ModelResponse(content="done", stop="tool_use", tool_calls=[
            ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={"summary": "filed"}),
        ])


def _worker(tmp_path, adapter, monitor, *, missing_figure: bool = True) -> AnalystWorkerAgent:
    missing = {
        "path": str(tmp_path / "gone.png"), "caption": "fit", "mime_type": "image/png",
        "produced_by": "tk_load", "created_at": datetime.now(UTC).isoformat(), "sha256": "a" * 64,
    }
    worker = AnalystWorkerAgent(
        model=ModelHandle(
            role="analyst_worker",
            config=RoleModelConfig(name="fake", provider="anthropic", model="fake", vision=True),
            provider=adapter,
        ),
        prompt="p",
        available_toolkits=[],
        sandbox=_FakeSandbox(tool_fn=lambda m, f, a: {
            "exit_code": 0, "stdout": "loaded", "figures": [missing]}),
        registry=_FakeRegistry(known={"tk_load"}, mcp=set()),
        monitor=monitor,
    )
    worker.agent_key = "analyst_worker_0"
    worker.report_dir = tmp_path / "w0"
    # The figure the report cites is in the worker's catalog but not on disk.
    from auto_research.schemas.figures import FigureRef

    if missing_figure:
        worker._figure_catalog["self"].append(FigureRef.model_validate(missing))
    return worker


def _monitor(tmp_path) -> RunMonitor:
    return RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")


def _events(monitor, name):
    return [e for e in monitor.events if e.event == name]


def test_free_calls_to_the_absolute_ceiling_get_grace_then_fail_loudly(tmp_path):
    """The reproduction: refused turn_end on every call. It used to return the draft as a report."""
    monitor = _monitor(tmp_path)
    adapter = _RefusedForever()
    worker = _worker(tmp_path, adapter, monitor)

    with pytest.raises(ReactBudgetExhausted, match="absolute ceiling"):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))

    # The draft existed: write_report was accepted, only turn_end was refused.
    writes = [e for e in _events(monitor, "analyst_worker.analyze.tool_result")
              if e.data.get("tool") in ("edit_report", "write_report")]
    assert writes and all(e.data["is_error"] is False for e in writes)
    # The ceiling, then exactly GRACE more model turns, then the failure.
    assert adapter.calls == CEILING + GRACE
    assert adapter.grace_seen_at == CEILING + 1
    opened = [e for e in _events(monitor, "analyst_worker.analyze.grace_opened")
              if e.data.get("at") == "ceiling"]
    assert len(opened) == 1
    (exhausted,) = _events(monitor, "analyst_worker.analyze.grace_exhausted")
    assert exhausted.level == 1 and exhausted.data["at"] == "ceiling"
    # The countdown rides the tool results, one per grace turn the model reads.
    assert [c.split(" ", 1)[0] for c in adapter.countdowns] == [
        str(n) for n in range(GRACE - 1, 0, -1)
    ]


def test_a_role_that_files_inside_the_ceiling_grace_is_accepted(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _RefusedForever(fix_in_grace=True)
    worker = _worker(tmp_path, adapter, monitor, missing_figure=False)

    report = asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert report.summary == "fit done" and report.status == "succeeded"
    assert CEILING < adapter.calls <= CEILING + GRACE
    assert _events(monitor, "analyst_worker.analyze.filed")


class _WritesThenRefuses:
    """Writes a complete report, then answers with a refusal stop: the loop ends unfiled."""

    def __init__(self, *, lead: bool = False):
        self.calls = 0
        self.lead = lead

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        if self.calls == 1:
            args = {"content": "# summary\na full draft\n# status\nsucceeded\n# findings\n- a finding\n"}
            return ModelResponse(content="writing", stop="tool_use", tool_calls=[
                ToolCall(id="e1", name="write_report", arguments=args),
            ])
        return ModelResponse(content="I will stop here.", stop="refusal")


def test_an_unfiled_draft_is_never_the_workers_report(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _WritesThenRefuses()
    worker = _worker(tmp_path, adapter, monitor, missing_figure=False)

    report = asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert report.status == "failed" and report.summary != "a full draft"
    (event,) = _events(monitor, "analyst_worker.analyze.no_report")
    assert event.data["unfiled_draft"] is True


def test_an_unfiled_draft_is_never_the_leads_decision(tmp_path):
    monitor = _monitor(tmp_path)
    lead = AnalystLeadAgent(
        model=ModelHandle(
            role="analyst_lead",
            config=RoleModelConfig(name="fake", provider="anthropic", model="fake", vision=True),
            provider=_WritesThenRefuses(lead=True),
        ),
        prompt="p",
        monitor=monitor,
    )
    lead.report_dir = tmp_path / "lead"
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])

    outcome = asyncio.run(lead.run_cycle(AnalysisTask(question="q"), []))

    # The draft was written (edit_report accepted) and still is not the decision.
    (edit,) = [e for e in _events(monitor, "analyst_lead.run_cycle.tool_result")
               if e.data.get("tool") in ("edit_report", "write_report")]
    assert edit.data["is_error"] is False
    assert outcome.report is None and outcome.filed is None


class _SpendsThenRefused(_RefusedForever):
    """Spends its budget on a toolkit call, then writes a report and is refused forever."""

    async def call(self, messages, config, **kwargs):
        if self.calls < CAP:
            self.calls += 1
            return ModelResponse(content="loading", stop="tool_use", tool_calls=[
                ToolCall(id=f"k{self.calls}", name="tk_load", arguments={}),
            ])
        self.calls += 1
        self.last_request = messages
        if self.calls == CAP + 1:
            return ModelResponse(content="writing", stop="tool_use", tool_calls=[
                ToolCall(id="e1", name="write_report", arguments={"content": (
                    "# summary\nfit done\n# status\nsucceeded\n# findings\n- the fit converged\n"
                    "# figures\n- [self:1]\n"
                )}),
            ])
        return ModelResponse(content="done", stop="tool_use", tool_calls=[
            ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={"summary": "filed"}),
        ])


def test_budget_grace_then_the_ceiling_is_one_window_announced_once(tmp_path):
    """Budget grace opens at the cap; free refusals never spend it; the ceiling's window then
    bounds the loop, with no second announcement."""
    monitor = _monitor(tmp_path)
    adapter = _SpendsThenRefused()
    worker = _worker(tmp_path, adapter, monitor)

    with pytest.raises(ReactBudgetExhausted, match="absolute ceiling"):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert adapter.calls == CEILING + GRACE
    opened = _events(monitor, "analyst_worker.analyze.grace_opened")
    assert [e.data.get("at") for e in opened] == [None, "ceiling"]
    notices = [m for m in adapter.last_request if "run out of rounds" in _text(m)]
    assert len(notices) == 1


def test_no_grace_configured_still_fails_loudly_at_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(roles_mod, "react_grace_rounds", lambda: 0)
    monitor = _monitor(tmp_path)
    adapter = _SpendsThenRefused()
    worker = _worker(tmp_path, adapter, monitor)

    with pytest.raises(ReactBudgetExhausted, match="no grace rounds"):
        asyncio.run(worker.analyze(AnalysisTask(question="q")))
    assert adapter.calls == CAP
    (event,) = _events(monitor, "analyst_worker.analyze.grace_exhausted")
    assert event.data["at"] == "budget" and event.level == 1


class _ResettingMonitor(RunMonitor):
    """Interleaves a background resume of the same seat: its loop start clears the shared
    `last_filed_form` while this turn is still recording its filing."""

    seat: AnalystWorkerAgent | None = None

    async def record(self, team, event, **kwargs):
        await super().record(team, event, **kwargs)
        if event == "analyst_worker.analyze.filed" and self.seat is not None:
            self.seat.last_filed_form = None


def test_a_filed_report_survives_a_concurrent_turn_of_the_same_seat(tmp_path):
    monitor = _ResettingMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
    adapter = _RefusedForever(fix_in_grace=True)
    worker = _worker(tmp_path, adapter, monitor, missing_figure=False)
    monitor.seat = worker
    # File at once: no grace needed for this one.
    adapter.grace_seen_at = 0

    report = asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert report.summary == "fit done" and report.status == "succeeded"
    assert worker.last_filed_form is None  # the interleaving really happened


REPORT = "# summary\nfit done\n# status\nsucceeded\n# findings\n- the fit converged\n"


class _FilesWhileAMessageArrives:
    """First turn, one batch: write the report, run a tool (a text arrives meanwhile), turn_end.
    Every later turn (a resume or a fresh turn) records its request and files at once."""

    def __init__(self):
        self.calls = 0
        self.later_requests: list[list] = []

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.calls == 1:
            return ModelResponse(content="writing and filing", stop="tool_use", tool_calls=[
                ToolCall(id="w1", name="write_report", arguments={"content": REPORT}),
                ToolCall(id="k1", name="tk_load", arguments={}),
                ToolCall(id="t1", name=TURN_END_TOOL, arguments={"summary": "filed"}),
            ])
        self.later_requests.append(list(messages))
        return ModelResponse(content="read it", stop="tool_use", tool_calls=[
            ToolCall(id=f"w{self.calls}", name="write_report", arguments={"content": REPORT}),
            ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={"summary": "read"}),
        ])


def _seated_pair(tmp_path, adapter, monitor, *, arrives):
    """A worker on a router beside a registered peer; ``arrives(control, key)`` runs inside the
    tool call of its filing batch — the moment a text reaches the live seat."""
    from auto_research.hitl.router import SeatRouter
    from auto_research.run_control import RunControl

    worker = _worker(tmp_path, adapter, monitor, missing_figure=False)
    control = RunControl()
    worker.run_control = control
    router = SeatRouter(control, monitor=monitor)
    router.register(worker.agent_key, worker)
    router.register("analyst_worker_1", object())  # the peer seat

    def _tool(module, fn, args):
        arrives(control, worker.agent_key)
        return {"exit_code": 0, "stdout": "loaded", "stderr": ""}

    worker.sandbox = _FakeSandbox(tool_fn=_tool)
    worker.channel = router
    return worker, control, router


async def _drain_tasks():
    for _ in range(50):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _peer_text(control, key):
    control.add_steer(key, "look at the hard band", sender="analyst_worker_1", kind="message")


def test_a_message_arriving_in_the_filing_batch_resumes_the_seat_once(tmp_path):
    """D5: the text was drained during the batch that filed, never shown; it goes back to the
    inbox and wakes the idle seat, which reads it exactly once. The peer has finished its own
    turn, so it is told when the resume ends and never made to wait for it."""
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()
    worker, control, _ = _seated_pair(tmp_path, adapter, monitor, arrives=_peer_text)

    async def _main():
        report = await worker.analyze(AnalysisTask(question="q"))
        assert control.resumed_by("analyst_worker_1") == []  # nobody waits on it
        await _drain_tasks()
        return report

    report = asyncio.run(_main())

    assert report.summary == "fit done"
    (first_resume,) = adapter.later_requests
    assert sum("look at the hard band" in _text(m) for m in first_resume) == 1
    assert not control.has_inbox(worker.agent_key)
    (filed,) = _events(monitor, "analyst_worker.analyze.filed")
    assert filed.data["requeued_texts"] == 1
    assert len(_events(monitor, "channel.resume")) == 1
    notes = control.drain_inbox("analyst_worker_1")
    assert any("ended its resumed turn" in e.text for e in notes)


def test_a_sender_still_in_its_turn_waits_for_the_woken_seat(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()

    def _live_peer_text(control, key):
        control.set_live("analyst_worker_1", True)  # the peer is mid-turn as it sends
        _peer_text(control, key)

    worker, control, _ = _seated_pair(tmp_path, adapter, monitor, arrives=_live_peer_text)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        assert control.resumed_by("analyst_worker_1") == [worker.agent_key]
        await _drain_tasks()

    asyncio.run(_main())
    assert len(adapter.later_requests) == 1


def test_a_human_steer_in_the_filing_batch_wakes_the_seat(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()

    def _steer(control, key):
        control.add_steer(key, "check the background region")  # a human /steer

    worker, control, _ = _seated_pair(tmp_path, adapter, monitor, arrives=_steer)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        await _drain_tasks()

    asyncio.run(_main())
    (resume,) = adapter.later_requests
    texts = [_text(m) for m in resume]
    assert texts.count("[Human steer] check the background region") == 1


def test_a_notice_in_the_filing_batch_waits_for_the_next_turn(tmp_path):
    """A runtime notice is re-queued but wakes nothing: the next turn reads it."""
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()

    def _notice(control, key):
        control.add_steer(key, "analyst_worker_1 ended its resumed turn.",
                          sender="analyst_worker_1", kind="notice")

    worker, control, _ = _seated_pair(tmp_path, adapter, monitor, arrives=_notice)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        await _drain_tasks()

    asyncio.run(_main())
    assert adapter.later_requests == [] and not _events(monitor, "channel.resume")
    assert control.has_inbox(worker.agent_key)


def test_wake_does_nothing_while_the_seat_is_working(tmp_path):
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()
    worker, control, router = _seated_pair(tmp_path, adapter, monitor, arrives=lambda c, k: None)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        control.add_steer(worker.agent_key, "look at the hard band",
                          sender="analyst_worker_1", kind="message")
        control.set_live(worker.agent_key, True)
        router.wake(worker.agent_key, [("analyst_worker_1", "message")])
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
        control.set_live(worker.agent_key, False)

    asyncio.run(_main())
    assert adapter.later_requests == []


def test_two_texts_to_an_idle_seat_start_one_resume(tmp_path):
    """The second text arrives after the first started a resume but before its loop is live:
    it joins the inbox that resume reads instead of starting another."""
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()
    worker, control, router = _seated_pair(tmp_path, adapter, monitor, arrives=lambda c, k: None)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        first, _ = await router.send("analyst_worker_1", worker.agent_key, "first text")
        second, _ = await router.send("analyst_worker_1", worker.agent_key, "second text")
        assert "resumed" in first and "working now" in second
        await _drain_tasks()

    asyncio.run(_main())
    assert len(_events(monitor, "channel.resume")) == 1
    (resume,) = adapter.later_requests
    joined = " ".join(_text(m) for m in resume)
    assert "first text" in joined and "second text" in joined


def test_the_next_fresh_turn_carries_the_woken_text_once(tmp_path):
    """After the woken resume read it, the seat's next fresh turn sees the text exactly once —
    through its session history, never again through the inbox."""
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()
    worker, control, _ = _seated_pair(tmp_path, adapter, monitor, arrives=_peer_text)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        await _drain_tasks()
        worker.sandbox = _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "ok"})
        await worker.analyze(AnalysisTask(question="q2"))
        await _drain_tasks()

    asyncio.run(_main())
    assert len(adapter.later_requests) == 2
    fresh = adapter.later_requests[-1]
    assert sum("look at the hard band" in _text(m) for m in fresh) == 1


class _AnswersAndAsks:
    """A seat that files at once; resumed, it answers the peer by `message` and files."""

    def __init__(self, peer: str, *, ask_in_batch: bool):
        self.peer, self.ask_in_batch = peer, ask_in_batch
        self.calls = 0
        self.seen: list[str] = []

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        self.seen.extend(_text(m) for m in messages)
        await asyncio.sleep(0)
        calls = [ToolCall(id=f"w{self.calls}", name="write_report", arguments={"content": REPORT})]
        if self.calls == 1 and self.ask_in_batch:
            calls.append(ToolCall(id="k1", name="tk_load", arguments={}))
        if self.calls == 2:
            calls.append(ToolCall(id="m2", name="message",
                                  arguments={"to": self.peer, "text": "an answer"}))
        calls.append(ToolCall(id=f"t{self.calls}", name=TURN_END_TOOL, arguments={"summary": "x"}))
        return ModelResponse(content="", stop="tool_use", tool_calls=calls)


def test_two_idle_seats_never_wait_on_each_other(tmp_path):
    """The review's deadlock: B files while A's text arrives; B's woken resume answers A, which
    resumes A. Each used to be registered as waiting for the other, so each turn_end was refused
    to the ceiling. Neither is in its turn, so neither waits."""
    from auto_research.hitl.router import SeatRouter
    from auto_research.run_control import RunControl

    monitor = _monitor(tmp_path)
    control = RunControl()
    router = SeatRouter(control, monitor=monitor)
    a_model = _AnswersAndAsks("analyst_worker_1", ask_in_batch=False)
    b_model = _AnswersAndAsks("analyst_worker_0", ask_in_batch=True)
    a = _worker(tmp_path / "a", a_model, monitor, missing_figure=False)
    b = _worker(tmp_path / "b", b_model, monitor, missing_figure=False)
    a.agent_key, b.agent_key = "analyst_worker_0", "analyst_worker_1"
    for seat in (a, b):
        seat.run_control, seat.channel = control, router
        router.register(seat.agent_key, seat)

    def _a_asks(module, fn, args):
        control.add_steer(b.agent_key, "a question", sender=a.agent_key, kind="message")
        return {"exit_code": 0, "stdout": "loaded", "stderr": ""}

    b.sandbox = _FakeSandbox(tool_fn=_a_asks)

    async def _main():
        await a.analyze(AnalysisTask(question="q"))
        await b.analyze(AnalysisTask(question="q"))
        await _drain_tasks()

    asyncio.run(_main())
    assert not _events(monitor, "channel.resume.error")
    # Both resumed turns ended: B's (woken for A's question) and A's (resumed by B's answer).
    # B, still in its resumed turn when it answered, waits for A — a few refused turn_ends, far
    # from the ceiling the deadlock reached.
    ended = {e.actor for e in _events(monitor, "channel.resume.end")}
    assert ended == {"analyst_worker_0", "analyst_worker_1"}
    assert a_model.calls < CEILING and b_model.calls < CEILING
    # A's answer reached B: if it arrived as B's resumed turn filed, B is resumed again for it
    # once that resume ends, and nothing that wakes a seat is left unread.
    assert any("an answer" in t for t in b_model.seen)
    from auto_research.run_control import WAKING_KINDS

    for seat in (a, b):
        assert not [e for e in control.peek_inbox(seat.agent_key) if e.kind in WAKING_KINDS]


def test_a_woken_resume_waits_behind_the_seats_next_fresh_turn(tmp_path):
    """The seat's next fresh turn starts before the woken resume: turns run one at a time, the
    fresh turn reads the text, and the resume then finds nothing to read and runs no model call."""
    monitor = _monitor(tmp_path)
    adapter = _FilesWhileAMessageArrives()
    worker, control, _ = _seated_pair(tmp_path, adapter, monitor, arrives=_peer_text)

    async def _main():
        await worker.analyze(AnalysisTask(question="q"))
        worker.sandbox = _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "ok"})
        await worker.analyze(AnalysisTask(question="q2"))  # no yield in between
        await _drain_tasks()

    asyncio.run(_main())
    (fresh,) = adapter.later_requests
    assert sum("look at the hard band" in _text(m) for m in fresh) == 1
    assert _events(monitor, "channel.resume") and not _events(monitor, "analyst_worker.resume.filed")


# ── ruling D6: a resumed turn's ReactBudgetExhausted ends the run ─────────────────────────────


class _SpendsForever:
    """Files its first turn at once; resumed, it calls a toolkit tool forever and never files."""

    def __init__(self):
        self.calls = 0

    async def call(self, messages, config, **kwargs):
        self.calls += 1
        await asyncio.sleep(0)
        if self.calls == 1:
            return ModelResponse(content="", stop="tool_use", tool_calls=[
                ToolCall(id="w1", name="write_report", arguments={"content": REPORT}),
                ToolCall(id="t1", name=TURN_END_TOOL, arguments={"summary": "filed"}),
            ])
        return ModelResponse(content="", stop="tool_use", tool_calls=[
            ToolCall(id=f"k{self.calls}", name="tk_load", arguments={}),
        ])


def test_a_resumed_turn_that_exhausts_its_caps_fails_the_run(tmp_path):
    """The resumed seat runs its budget, then its grace, and raises; the router records it, keeps
    nothing for the seat, and fails the run (the run task is cancelled with it as `fatal`)."""
    monitor = _monitor(tmp_path)
    adapter = _SpendsForever()
    worker, control, router = _seated_pair(tmp_path, adapter, monitor, arrives=lambda c, k: None)
    worker.sandbox = _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "ok"})

    async def _main():
        run_task = asyncio.create_task(asyncio.sleep(60))  # stands for the run's task
        control.register_run_task(run_task)
        await worker.analyze(AnalysisTask(question="q"))
        await router.send("analyst_worker_1", worker.agent_key, "one more fit")
        await _drain_tasks()
        return run_task

    run_task = asyncio.run(_main())

    assert isinstance(control.fatal, ReactBudgetExhausted)
    assert run_task.cancelled()
    assert adapter.calls == 1 + CAP + GRACE
    (event,) = _events(monitor, "channel.resume.exhausted")
    assert event.level == 1 and event.actor == worker.agent_key
    assert not _events(monitor, "channel.resume.error")
    assert not control.has_inbox(worker.agent_key)


def _hermetic_run(tmp_path, monkeypatch):
    """`run_research`'s own wiring on a temp config: no preflight, an empty toolkit, no env."""

    async def _noop_preflight(*, human_handler=None, monitor=None):
        return None

    monkeypatch.setattr("auto_research.graph.runtime.run_preflight", _noop_preflight)
    ini = tmp_path / "env.sh"
    ini.write_text("#!/bin/bash\n", encoding="utf-8")
    toolkit = tmp_path / "toolkit"
    toolkit.mkdir()
    orch = {"toolkit_root": str(toolkit), "analyze_env": None, "analyze_ini_sh": str(ini),
            "disabled_tools": [], "mcp_servers": []}
    monkeypatch.setattr("auto_research.graph.runtime.load_orchestration_config",
                        lambda *a, **k: orch)
    monkeypatch.setattr("auto_research.env.agent_conda_dir", lambda: tmp_path / "conda")
    monkeypatch.setattr("auto_research.env.env_lock_dir", lambda: tmp_path / "conda" / "locks")


class _ExhaustedLibrarian:
    """A seat with a kept turn whose resumed turn runs out of its caps."""

    last_receipts: dict = {}

    async def research(self, request, *, follow_up=None):
        raise AssertionError("never routed to")

    def has_kept_turn(self):
        return True

    async def resume(self):
        raise ReactBudgetExhausted("librarian_io.resume: budget and grace spent")


def _research(tmp_path, reviewer):
    from auto_research import run_research
    from auto_research.schemas import ResearchRequest

    class Consensus:
        async def run(self, workers, lead, task):
            raise AssertionError("never routed to")

    return asyncio.run(asyncio.wait_for(run_research(
        ResearchRequest(question="q"), reviewer=reviewer, io_agent=_ExhaustedLibrarian(),
        retrieval=None, workers=[], lead=object(), consensus=Consensus(),
        max_iterations=1, chatter=10, monitor_jsonl=tmp_path / "monitor.jsonl",
        report_formats=[],
    ), timeout=20))


def _ledger(tmp_path):
    import json

    return [json.loads(line) for line in (tmp_path / "monitor.jsonl").read_text().splitlines()]


def test_a_resumed_seats_exhaustion_ends_run_research(tmp_path, monkeypatch):
    """Through `run_research`: a seat resumed mid-run exhausts its caps; the run ends with
    `ReactBudgetExhausted`, recorded as `run.error`, instead of carrying on."""
    from auto_research.schemas import ReviewerReport

    _hermetic_run(tmp_path, monkeypatch)

    class Reviewer:
        async def review(self, request, **_):
            await self.channel.send("reviewer", "librarian_io", "one more source, please")
            await asyncio.sleep(30)  # the failure ends the run long before this
            return ReviewerReport(rationale="r", assign_to="human", title="T", abstract="A")

    with pytest.raises(ReactBudgetExhausted, match="budget and grace spent"):
        _research(tmp_path, Reviewer())

    events = _ledger(tmp_path)
    names = [e["event"] for e in events]
    assert "channel.resume.exhausted" in names
    (error,) = [e for e in events if e["event"] == "run.error"]
    assert "budget and grace spent" in error["data"]["error"]
    # The interrupt's kill-and-repair ran, recorded as the run's failure, not as a stop.
    assert "run.interrupted" not in names and names[-1] == "run.summary"


def test_a_seat_exhausted_as_the_graph_ends_still_fails_the_run(tmp_path, monkeypatch):
    """The resume fails while the graph is finishing: the run raises the failure after its last
    node, never a stray cancel in its teardown."""
    from auto_research.schemas import ReviewerReport

    _hermetic_run(tmp_path, monkeypatch)

    class Reviewer:
        async def review(self, request, **_):
            await self.channel.send("reviewer", "librarian_io", "one more source, please")
            return ReviewerReport(rationale="r", assign_to="human", title="T", abstract="A")

    with pytest.raises(ReactBudgetExhausted):
        _research(tmp_path, Reviewer())
    assert "run.error" in [e["event"] for e in _ledger(tmp_path)]


def test_only_the_first_failure_ends_the_run_and_a_stop_that_came_first_wins():
    from auto_research.run_control import RunControl

    async def _main():
        control = RunControl()
        run = asyncio.create_task(asyncio.sleep(60))
        control.register_run_task(run)
        first, second = ReactBudgetExhausted("first"), ReactBudgetExhausted("second")
        assert control.fail(first) is True
        assert control.fail(second) is False
        assert control.interrupt("human stop") is False  # the failure already ends the run
        await asyncio.sleep(0)
        assert control.fatal is first and not control.stop_requested
        assert run.cancelled() or run.cancelling() == 1

        stopped = RunControl()
        run2 = asyncio.create_task(asyncio.sleep(60))
        stopped.register_run_task(run2)
        assert stopped.interrupt("human stop") is True
        assert stopped.fail(first) is False  # the stop came first and wins
        assert stopped.fatal is None
        await asyncio.sleep(0)

        late = RunControl()
        run3 = asyncio.create_task(asyncio.sleep(60))
        late.register_run_task(run3)
        assert late.fail(first) is True
        late.register_run_task(None)  # the graph returned before the cancel fired
        await asyncio.sleep(0)
        assert not run3.cancelled() and run3.cancelling() == 0
        run3.cancel()

    asyncio.run(_main())

