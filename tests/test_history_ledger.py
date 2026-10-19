"""The run ledger is each agent's session history (B1): no LLM, no network.

Every test drives the real ``AgentRole._react`` / ``_react_loop`` (and, for the lead, the real
``_ChainDriver``) with a scripted model that records exactly what each call was sent. The history
is then rebuilt from the ledger file alone (``agents.history``) and compared, call by call, with
what the model actually received: the conversation, the call-time figures, the documents.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime

import pytest

from auto_research.agents import history
from auto_research.agents.react import ToolOutcome
from auto_research.agents.roles import AnalystLeadAgent, AnalystWorkerAgent, ChannelTools
from auto_research.models.types import ModelMessage, ModelResponse, ToolCall, ToolResultMessage
from auto_research.monitor import RunMonitor
from auto_research.provenance.store import ReceiptStore
from auto_research.run_control import RunControl
from auto_research.schemas import DocumentRef, FigureRef


def _png(path, color=(200, 30, 30)):
    from PIL import Image

    Image.new("RGB", (64, 48), color).save(path)
    return path


def _figure(path, caption="a plot") -> FigureRef:
    import hashlib

    return FigureRef(
        path=path, caption=caption, mime_type="image/png", produced_by="plot_tool",
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _call(name, call_id, **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


class _Recorder:
    """A model that answers from a script and keeps a deep copy of the very objects it was sent.

    The copy is of the objects, not of any encoding: comparing an encoder's output with itself
    would let a field the encoder drops cancel out on both sides.
    """

    def __init__(self, script, gate: asyncio.Event | None = None):
        self.script = list(script)
        self.sent: list[dict] = []
        self.gate = gate

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto", images=(),
                   documents=(), cache_prefix=False):
        self.sent.append(_snapshot(list(messages), list(images), list(documents), tools))
        if self.gate is not None:
            await self.gate.wait()
        return copy.deepcopy(self.script.pop(0))


def _snapshot(messages, images, documents, tools) -> dict:
    """Python-mode dumps (bytes, paths and datetimes kept as themselves) plus the key ORDER of
    every tool call's arguments and every provider part, which a sorted ledger line would lose."""
    order = [
        (list(c.arguments), [list(p) for p in m.provider_parts])
        for m in messages if isinstance(m, ModelResponse) for c in m.tool_calls
    ]
    return {
        "messages": [(type(m).__name__, m.model_dump(mode="python", exclude={"raw"}))
                     for m in copy.deepcopy(messages)],
        "order": order,
        "images": [f.model_dump(mode="python") for f in images],
        "documents": [d.model_dump(mode="python") for d in documents],
        "tools": copy.deepcopy(list(tools)) if tools is not None else None,
    }


class _Tools:
    """A provider with a plotting tool, a document tool, a free tool and a terminal."""

    def __init__(self, figure: FigureRef, document: DocumentRef):
        self.figure, self.document = figure, document

    def tool_definitions(self):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                for n in ("plot", "open_doc", "note", "turn_end")]

    async def execute(self, name, args):
        if name == "plot":
            return ToolOutcome(text="plotted 1 figure", figures=[self.figure])
        if name == "open_doc":
            return ToolOutcome(text="opened", documents=[self.document])
        if name == "turn_end":
            return ToolOutcome(text="accepted")
        return ToolOutcome(text=f"{name} done {json.dumps(args, sort_keys=True)}")


def _as_sent(call: history.ModelCall) -> dict:
    return _snapshot(call.messages, call.images, call.documents, call.tools)


def _worker(model, monitor, key="analyst_worker_0") -> AnalystWorkerAgent:
    worker = AnalystWorkerAgent(model=model, prompt="you are a worker", monitor=monitor)
    worker.agent_key = key
    return worker


@pytest.fixture
def media(tmp_path):
    fig = _figure(_png(tmp_path / "f1.png"), caption="[self:1] residuals")
    peer = _figure(_png(tmp_path / "peer.png", (10, 10, 200)), caption="peer figure")
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 tiny")
    import hashlib

    doc = DocumentRef(path=pdf, title="doc", mime_type="application/pdf",
                      created_at=datetime(2026, 10, 18, tzinfo=UTC),
                      sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    return fig, peer, doc


def _rich_script():
    """Thinking parts, a Gemini-style bytes field, a silent turn, a batch, grace, the terminal."""
    thinking = {"type": "thinking", "thinking": "let me plot", "signature": "sig=="}
    return [
        ModelResponse(
            content="Plotting first.",
            tool_calls=[_call("plot", "c1", n=3)],
            provider_parts=[thinking, {"type": "text", "text": "Plotting first."},
                            {"type": "tool_use", "id": "c1", "name": "plot", "input": {"n": 3}}],
            stop="tool_use",
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(id="c2", name="open_doc", arguments={},
                         provider_fields={"thought_signature": b"\x00\xffraw"}),
                _call("note", "c3", text="two in one batch"),
            ],
            stop="tool_use",
        ),
        ModelResponse(content="", stop="end_turn"),  # silent: the loop nudges
        ModelResponse(tool_calls=[_call("note", "c4", text="grace")]),
        ModelResponse(tool_calls=[_call("turn_end", "c5")]),
    ]


def test_the_rebuilt_history_is_exactly_what_each_call_was_sent(tmp_path, media):
    fig, peer, doc = media
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _Recorder(_rich_script())
    worker = _worker(model, monitor)
    control = RunControl()
    worker.run_control = control
    control.add_steer("analyst_worker_0", "look at the residuals")
    seed = [ModelMessage(role="system", content="you are a worker"),
            ModelMessage(role="user", content="the task")]
    messages, _, _ = asyncio.run(worker._react_loop(
        seed_messages=seed, tools=_Tools(fig, doc), action="analyze", cap=3,
        images=[peer], require_terminal=True, terminal_tools=frozenset({"turn_end"}),
        free_tools=frozenset({"turn_end"}), task_receipt="input:r1",
    ))
    # The ledger alone, read back from the file (chatter 3 keeps none of it in memory).
    turns = history.turns_for(monitor.jsonl_path, "analyst_worker_0")
    assert len(turns) == 1
    (turn,) = turns.values()
    assert turn.task_receipt == "input:r1"
    assert [_as_sent(c) for c in turn.calls] == model.sent
    assert _snapshot(turn.messages, [], [], None) == _snapshot(messages, [], [], None)
    # The parts that were missing before B1 are all there: the steer, the nudge, the grace
    # countdown, the bytes signature, the peer figure in the task message.
    texts = [m.content for m in turn.messages if isinstance(m, ModelMessage)]
    assert "look at the residuals" in texts[2]
    assert any("turn_end" in t or "report" in t for t in texts[3:]), texts
    sig = turn.messages[5].tool_calls[0].provider_fields["thought_signature"]
    assert sig == b"\x00\xffraw"
    # The peer figure rides the task message on EVERY call (ruling B6: within a turn the
    # conversation only grows at its end), never the call-time channel.
    assert all(not call["images"] for call in model.sent)
    assert all(call["messages"][1][1]["figures"] for call in model.sent)
    # The opened document rides the result that opened it (R2), never the call-time channel.
    assert all(not call["documents"] for call in model.sent)
    nested = [d for kind, m in model.sent[-1]["messages"] if kind == "ToolResultMessage"
              for p in m["results"] for d in p["documents"]]
    assert len(nested) == 1, "the document sits at its result, once"
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    parts = [r["data"]["history"] for r in rows if "history" in r["data"]]
    assert any(p["part"] == "results_end" and p["suffix"] for p in parts), "grace countdown"
    assert any(r["event"].endswith(".nudge") for r in rows), "the silent turn's nudge"
    assert any(r["event"].endswith(".grace") for r in rows), "the grace notice"


def test_figures_are_recorded_by_receipt_handle_and_full_reference(tmp_path, media):
    fig, peer, doc = media
    store = ReceiptStore.for_run(tmp_path / "run")
    receipt = store.mint(source="analyst_worker_0", kind="figure", produced_by="plot_tool",
                         path=fig.path, caption="residuals", mime_type="image/png")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    model = _Recorder([
        ModelResponse(tool_calls=[_call("plot", "c1")]),
        ModelResponse(tool_calls=[_call("turn_end", "c2")]),
    ])
    worker = _worker(model, monitor)
    worker.receipts = store
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="t")], tools=_Tools(fig, doc),
        action="analyze", cap=5, terminal_tools=frozenset({"turn_end"}),
    ))
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    (plot_row,) = [r for r in rows if r["event"].endswith(".tool_result")
                   and r["data"]["tool"] == "plot"]
    entry = plot_row["data"]["history"]["figures"][0]
    assert entry["handle"] == receipt.handle
    assert entry["ref"]["caption"] == "[self:1] residuals", "the presentation caption is kept"
    model_rows = [r for r in rows if r["event"].endswith(".model_turn")]
    assert len(model_rows) == 2
    assert json.loads(model_rows[0]["data"]["history"]["response_json"])["tool_calls"]


def test_a_resumed_turn_continues_its_kept_turn_in_the_ledger(tmp_path, media):
    """A resume records only what it adds; its base is the kept turn, rebuilt verbatim."""
    from auto_research.hitl.router import SeatRouter

    fig, peer, doc = media
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    model = _Recorder([
        ModelResponse(tool_calls=[_call("note", "c1", text="first")]),
        ModelResponse(tool_calls=[_call("turn_end", "c2")]),
        ModelResponse(tool_calls=[_call("note", "c3", text="after the message")]),
        ModelResponse(tool_calls=[_call("turn_end", "c4")]),
    ])
    worker = _worker(model, monitor)
    control = RunControl()
    worker.run_control = control
    worker.channel = SeatRouter(control)
    worker.channel.register("analyst_worker_0", worker)
    seed = [ModelMessage(role="system", content="s"), ModelMessage(role="user", content="t")]
    asyncio.run(worker._react(
        seed_messages=seed, tools=_Tools(fig, doc), action="analyze", cap=5,
        require_terminal=True, terminal_tools=frozenset({"turn_end"}),
    ))
    control.add_steer("analyst_worker_0", "the lead asks one more thing", sender="analyst_lead",
                      kind="message")
    asyncio.run(worker.resume())

    turns = list(history.turns_for(monitor.jsonl_path, "analyst_worker_0").values())
    assert len(turns) == 2
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    starts = [r["data"]["history"] for r in rows if r["event"].endswith(".turn_start")]
    assert starts[1]["base"]["through"] == turns[0].id and starts[1]["seed_json"] == "[]"
    assert [_as_sent(c) for t in turns for c in t.calls] == model.sent
    assert history.encode_message(turns[1].messages[-1]) == history.encode_message(
        worker._kept_turn["messages"][-1]
    )


def _lead_turn(tmp_path, monkeypatch, script, links=1):
    """Drive the real lead loop and chain driver over a scripted model (batches of calls)."""
    from tests.chain_stub import StubSession, stub_compactor
    from tests.test_lead_forms import GOOD_LINK

    from auto_research.agents.react import LeadCycle, LeadCycleOutcome
    from auto_research.agents.roles import _ChainDriver
    from auto_research.schemas import AuthoringDecision, ToolSpec

    stub_compactor(monkeypatch)

    async def _build(self, spec):
        return StubSession(spec, status="committed", sha="0123456789ab")

    monkeypatch.setattr(_ChainDriver, "_build_session", _build)
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    model = _Recorder([
        ModelResponse(tool_calls=[_call(name, f"{name[:2]}{i}_{k}", **args)
                                  for k, (name, args) in enumerate(batch)])
        for i, batch in enumerate(script)
    ])
    lead = AnalystLeadAgent(model=model, prompt="lead", monitor=monitor)
    lead.agent_key = "analyst_lead"
    cycle = LeadCycle(authoring_enabled=True, rounds_left=1)
    specs = [ToolSpec.model_validate({**GOOD_LINK, "proposed_tool_name": f"tool_{i}"})
             for i in range(links)]
    cycle.brief = AuthoringDecision(should_author=True, tool_specs=specs, rationale="r")
    messages = [ModelMessage(role="system", content="lead"), ModelMessage(role="user", content="t")]

    class _Provider:
        authoring_session = None

        def tool_definitions(self):
            return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                    for n in ("author_chain", "message", "note", "submit_for_review", "turn_end")]

        async def execute(self, name, args):
            if name in ("author_chain", "submit_for_review"):
                return ToolOutcome(text=await driver.advance())
            return ToolOutcome(text=f"{name} ok")

    provider = _Provider()
    driver = _ChainDriver(
        lead=lead, provider=provider, cycle=cycle,
        outcome=LeadCycleOutcome(filed=None, author_budget=3), messages=messages,
        toolkit_root=None, worktree_base=None, direct_branch=None, colead=None,
        human_handler=None, coder=None, allow_correlated_review=False,
    )
    # As `run_cycle` wires it: the driver edits the turn whose id the loop records.
    driver.turn_id = turn_id = history.new_turn_id("analyst_lead")

    async def run():
        try:
            await lead._react_loop(
                seed_messages=messages, share_messages=True, tools=provider, action="run_cycle",
                cap=8, terminal_tools=frozenset({"turn_end"}), turn_id=turn_id,
            )
        finally:
            await driver.close()

    asyncio.run(run())
    (turn,) = history.turns_for(monitor.jsonl_path, "analyst_lead").values()
    return turn, messages, model


def _names(messages):
    return [c.name for m in messages for c in (getattr(m, "tool_calls", None) or ())]


def test_the_leads_coding_link_cut_is_replayed(tmp_path, monkeypatch):
    """B14 (c): a link closes at the next complete tool round; the ledger records the cut and
    the rebuild replays it at the same point."""
    turn, messages, model = _lead_turn(tmp_path, monkeypatch, [
        [("author_chain", {})], [("message", {"to": "coder", "text": "go"})],
        [("note", {"text": "reading"})], [("submit_for_review", {})], [("turn_end", {})],
    ])
    assert [_as_sent(c) for c in turn.calls] == model.sent
    assert "message" not in _names(turn.messages), "the coding span was cut in the rebuild too"
    assert _snapshot(turn.messages, [], [], None) == _snapshot(messages, [], [], None)


def test_a_link_closed_in_the_filing_batch_is_left_as_filed(tmp_path, monkeypatch):
    """Found by review: `close()` after the loop had cut the batch that filed turn_end."""
    turn, messages, model = _lead_turn(tmp_path, monkeypatch, [
        [("author_chain", {})], [("message", {"to": "coder", "text": "go"})],
        [("submit_for_review", {}), ("turn_end", {})],
    ])
    assert "turn_end" in _names(messages) and "submit_for_review" in _names(messages)
    assert _snapshot(turn.messages, [], [], None) == _snapshot(messages, [], [], None)


def test_two_links_closed_in_one_batch_are_both_cut(tmp_path, monkeypatch):
    """Found by review: one pending slot let the second close overwrite the first."""
    turn, messages, model = _lead_turn(tmp_path, monkeypatch, [
        [("author_chain", {})], [("message", {"to": "coder", "text": "go"})],
        [("submit_for_review", {}), ("submit_for_review", {})], [("turn_end", {})],
    ], links=2)
    assert "message" not in _names(messages), "link 1's transcript left with the cut"
    answer = next(p for m in messages if isinstance(m, ToolResultMessage) for p in m.results
                  if p.name == "author_chain")
    assert answer.content.count("tool_0") >= 1 and answer.content.count("tool_1") >= 1
    assert _snapshot(turn.messages, [], [], None) == _snapshot(messages, [], [], None)


def test_a_turn_whose_base_is_missing_from_the_ledger_is_refused():
    rows = [{
        "actor": "a", "event": "x.resume.turn_start",
        "data": {"history": {"part": "turn_start", "turn": "a#2",
                             "base": {"through": "a#1", "view": "verbatim", "count": 3},
                             "seed_json": "[]"}},
    }]
    with pytest.raises(history.HistoryError, match="lacks"):
        history.rebuild(rows)


def test_without_a_monitor_the_loop_records_nothing_and_still_runs(media):
    fig, peer, doc = media
    model = _Recorder([ModelResponse(tool_calls=[_call("turn_end", "c1")])])
    worker = _worker(model, None)
    messages, _, _ = asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="t")], tools=_Tools(fig, doc),
        action="analyze", cap=2, terminal_tools=frozenset({"turn_end"}),
    ))
    assert len(messages) == 3


def _resumable_worker(tmp_path, script, media):
    from auto_research.hitl.router import SeatRouter

    fig, peer, doc = media
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl")
    model = _Recorder(script)
    worker = _worker(model, monitor)
    control = RunControl()
    worker.run_control = control
    worker.channel = SeatRouter(control)
    worker.channel.register("analyst_worker_0", worker)
    tools = _Tools(fig, doc)
    seed = [ModelMessage(role="system", content="s"), ModelMessage(role="user", content="t")]
    asyncio.run(worker._react(
        seed_messages=seed, tools=tools, action="analyze", cap=5,
        require_terminal=True, terminal_tools=frozenset({"turn_end"}),
    ))
    return worker, model, monitor, control


def test_a_resume_that_ends_unfiled_is_still_the_base_of_the_next(tmp_path, media):
    """Found by review: a resume that raised left the kept list grown and the kept id stale, so
    the next resume recorded nothing of what the failed one had added."""
    from auto_research.agents.roles import ReactBudgetExhausted

    worker, model, monitor, control = _resumable_worker(tmp_path, [
        ModelResponse(tool_calls=[_call("turn_end", "c1")]),
        # the first resume: two plain calls, then the budget is gone and it never files
        *[ModelResponse(tool_calls=[_call("note", f"r{i}")]) for i in range(40)],
    ], media)
    control.add_steer("analyst_worker_0", "one more", sender="analyst_lead", kind="message")
    worker._kept_turn["kwargs"]["cap"] = 1
    with pytest.raises(ReactBudgetExhausted):
        asyncio.run(worker.resume())
    model.script = [ModelResponse(tool_calls=[_call("turn_end", "z1")])]
    control.add_steer("analyst_worker_0", "and again", sender="analyst_lead", kind="message")
    asyncio.run(worker.resume())
    turns = list(history.turns_for(monitor.jsonl_path, "analyst_worker_0").values())
    assert len(turns) == 3
    assert [_as_sent(c) for t in turns for c in t.calls] == model.sent
    assert _snapshot(turns[-1].messages, [], [], None) == _snapshot(
        worker._kept_turn["messages"], [], [], None
    )


def test_two_loops_of_one_seat_at_once_keep_their_parts_apart(tmp_path, media):
    """A background resume beside a new turn of the same seat: every part names its own turn."""
    fig, peer, doc = media
    worker, model, monitor, control = _resumable_worker(tmp_path, [
        ModelResponse(tool_calls=[_call("turn_end", "c1")]),
    ], media)
    gate = asyncio.Event()

    resume_model = _Recorder([
        ModelResponse(tool_calls=[_call("note", "r1")]),
        ModelResponse(tool_calls=[_call("turn_end", "r2")]),
    ], gate=gate)
    fresh_model = _Recorder([
        ModelResponse(tool_calls=[_call("note", "f1")]),
        ModelResponse(tool_calls=[_call("turn_end", "f2")]),
    ])

    class _BySeed:
        """One seat, one model attribute: route each call to the loop it belongs to."""

        async def call(self, messages, **kwargs):
            fresh = any(getattr(m, "content", None) == "fresh" for m in messages)
            return await (fresh_model if fresh else resume_model).call(messages, **kwargs)

    async def _both():
        worker.model = _BySeed()
        control.add_steer("analyst_worker_0", "resume me", sender="analyst_lead", kind="message")
        resume = asyncio.create_task(worker.resume())
        await asyncio.sleep(0.01)          # the resume opens its turn and parks on its first call
        fresh_task = asyncio.create_task(worker._react_loop(
            seed_messages=[ModelMessage(role="user", content="fresh")], tools=_Tools(fig, doc),
            action="analyze", cap=5, terminal_tools=frozenset({"turn_end"}),
        ))
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(resume, fresh_task)

    asyncio.run(_both())
    turns = list(history.turns_for(monitor.jsonl_path, "analyst_worker_0").values())
    assert len(turns) == 3
    by_first = {t.calls[0].messages[-1].content if t.calls else None: t for t in turns}
    fresh_turn = by_first["fresh"]
    assert [_as_sent(c) for c in fresh_turn.calls] == fresh_model.sent
    resume_turn = turns[1] if turns[1] is not fresh_turn else turns[2]
    assert [_as_sent(c) for c in resume_turn.calls] == resume_model.sent


def test_an_in_memory_monitor_below_chatter_10_is_refused(media):
    monitor = RunMonitor(run_id="r1", chatter=8)
    with pytest.raises(history.HistoryError, match="chatter 8"):
        history.turns_for(monitor, "analyst_worker_0")


def test_a_results_batch_that_answers_other_calls_is_refused():
    seed = json.dumps([{"kind": "message", "role": "user", "content": "t"}])
    response = json.dumps({"tool_calls": [{"id": "c1", "name": "note", "arguments": {}}]})
    rows = [
        {"actor": "a", "event": "x.turn_start",
         "data": {"history": {"part": "turn_start", "turn": "a#1", "seed_json": seed}}},
        {"actor": "a", "event": "x.model_turn",
         "data": {"history": {"part": "model", "turn": "a#1", "response_json": response}}},
        {"actor": "a", "event": "x.tool_result",
         "data": {"tool": "note", "tool_call_id": "OTHER", "text": "",
                  "history": {"part": "tool_result", "turn": "a#1"}}},
        {"actor": "a", "event": "x.tools",
         "data": {"history": {"part": "results_end", "turn": "a#1", "suffix": ""}}},
    ]
    with pytest.raises(history.HistoryError, match="answers"):
        history.rebuild(rows)
