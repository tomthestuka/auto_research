"""Deterministic tests for the quick channel between seats (E12): no LLM, no network.

``message`` never blocks. A text to a live seat lands in its inbox, to a finished seat it resumes
that seat's normal loop beside the sender, to a seat that has not started it waits in the inbox,
and to the human it is answered later through the sender's inbox as a nudge — never a receipt.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from auto_research.agents.react import ToolOutcome, execute_view_receipt
from auto_research.agents.roles import AgentRole, ChannelTools
from auto_research.hitl.router import HUMAN, SeatRouter
from auto_research.models.types import ModelMessage, ModelResponse, ToolCall
from auto_research.provenance.store import ReceiptStore, ScopedLookup
from auto_research.run_control import RunControl
from auto_research.schemas import HumanAttachment, HumanResponse


def _run(coro):
    return asyncio.run(coro)


def _call(name: str, **arguments) -> ModelResponse:
    return ModelResponse(tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=arguments)])


class _ScriptedModel:
    """Returns queued responses; ``gate`` (if set) holds each call until released."""

    def __init__(self, responses=(), gate: asyncio.Event | None = None):
        self.responses = list(responses)
        self.calls: list[list] = []
        self.gate = gate

    async def call(self, messages, **kwargs):
        self.calls.append(list(messages))
        if self.gate is not None:
            await self.gate.wait()
        return self.responses.pop(0) if self.responses else _call("turn_end")


class _SeatTools:
    """A seat's provider: a report it can edit and a turn_end that accepts."""

    def __init__(self):
        self.report = "filed"
        self.edits = 0

    def tool_definitions(self):
        return [
            {"name": "edit_report", "description": "", "input_schema": {"type": "object"}},
            {"name": "turn_end", "description": "", "input_schema": {"type": "object"}},
        ]

    async def execute(self, name, args):
        if name == "edit_report":
            self.edits += 1
            self.report = str(args.get("summary", ""))
            return ToolOutcome(text="saved")
        if name == "turn_end":
            return ToolOutcome(text="accepted")
        return ToolOutcome(text=f"unknown {name}", is_error=True)


def _seat(key: str, model, control: RunControl, router: SeatRouter | None) -> AgentRole:
    inst = object.__new__(AgentRole)
    inst.role = "analyst_worker"
    inst.model = model
    inst.monitor = None
    inst.tools = None
    inst.last_response = None
    inst.run_control = control
    inst.agent_key = key
    inst.channel = router
    inst._kept_turn = None
    if router is not None:
        router.register(key, inst)
    return inst


def _seed():
    return [ModelMessage(role="system", content="sys"), ModelMessage(role="user", content="task")]


async def _turn(seat: AgentRole, tools) -> None:
    await seat._react(
        seed_messages=_seed(), tools=tools, action="analyze", cap=5,
        require_terminal=True, terminal_tools=frozenset({"turn_end"}),
    )


# ── the directory ─────────────────────────────────────────────────────────────


def test_the_directory_is_every_other_seat_then_the_human_only_when_attended():
    control = RunControl()
    unattended = SeatRouter(control)
    for key in ("reviewer", "analyst_lead", "analyst_worker_0"):
        unattended.register(key, SimpleNamespace())
    assert unattended.directory("analyst_lead") == ["reviewer", "analyst_worker_0"]
    attended = SeatRouter(control, human_handler=lambda q: "ok")
    attended.register("analyst_lead", SimpleNamespace())
    attended.register("reviewer", SimpleNamespace())
    assert attended.directory("analyst_lead") == ["reviewer", HUMAN]


def test_an_unknown_or_self_address_is_refused_and_names_who_is_reachable():
    control = RunControl()
    router = SeatRouter(control)
    router.register("reviewer", SimpleNamespace())
    router.register("analyst_lead", SimpleNamespace())
    for to in ("analyst_colead", "analyst_lead", HUMAN):
        text, is_error = _run(router.send("analyst_lead", to, "hi"))
        assert is_error and "reviewer" in text, (to, text)
    assert control.drain_inbox("analyst_lead") == []


# ── routes ────────────────────────────────────────────────────────────────────


def test_a_text_to_a_live_seat_lands_in_its_inbox_and_returns_at_once():
    control = RunControl()
    router = SeatRouter(control)
    router.register("analyst_lead", SimpleNamespace())
    router.register("analyst_worker_0", SimpleNamespace(has_kept_turn=lambda: True))
    control.set_live("analyst_worker_0", True)
    text, is_error = _run(router.send("analyst_lead", "analyst_worker_0", "which band?"))
    assert not is_error and "working now" in text
    [entry] = control.drain_inbox("analyst_worker_0")
    assert entry.sender == "analyst_lead" and entry.kind == "message"
    assert "[Message from analyst_lead] which band?" in entry.render()


def test_a_text_to_a_seat_that_has_not_started_waits_in_its_inbox():
    control = RunControl()
    router = SeatRouter(control)
    router.register("analyst_worker_0", SimpleNamespace())
    router.register("analyst_lead", SimpleNamespace(has_kept_turn=lambda: False))
    text, is_error = _run(router.send("analyst_worker_0", "analyst_lead", "note the DU3 gap"))
    assert not is_error and "has not started" in text
    assert control.drain_steer("analyst_lead") == ["note the DU3 gap"]
    assert control.resumed_by("analyst_worker_0") == []


def test_a_text_mints_no_receipt_lists_the_handles_it_names_and_widens_no_citable_scope(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    figure = tmp_path / "fig.png"
    figure.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    minted = store.mint(
        source="analyst_worker_0", kind="figure", produced_by="plot", path=figure,
        caption="light curve", mime_type="image/png",
    )
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake paper")
    paper = store.mint(source="lib", kind="paper", produced_by="read_paper", path=pdf, title="A paper")
    recipient_scope = ScopedLookup(store, own_source="analyst_lead")
    before = len(store.receipts())

    control = RunControl()
    router = SeatRouter(control, store=store)
    router.register("analyst_worker_0", SimpleNamespace())
    router.register("analyst_lead", SimpleNamespace(has_kept_turn=lambda: False))
    text, is_error = _run(router.send(
        "analyst_worker_0", "analyst_lead", f"see the dip; the value is in [{paper.handle}]",
        handles=[minted.handle, "analyst_worker_1:f9"],
    ))
    assert not is_error and "analyst_worker_1:f9 is not a receipt of this run" in text
    assert len(store.receipts()) == before, "a text is informal: nothing is minted for it"
    assert not recipient_scope.visible(minted.handle), "a text widens no citable scope"
    (entry,) = control.drain_inbox("analyst_lead")
    assert entry.handles == (minted.handle, paper.handle)  # attached, then named in the prose
    rendered = entry.render()
    assert rendered.startswith("[Message from analyst_worker_0] see the dip")
    assert f"open each with view_receipt): {minted.handle}, {paper.handle}" in rendered
    assert 'message(to="analyst_worker_0")' in rendered


def test_any_seat_opens_any_receipt_of_the_run_and_a_paper_arrives_as_its_pdf(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    figure = tmp_path / "fig.png"
    figure.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    minted = store.mint(source="analyst_worker_0", kind="figure", produced_by="plot", path=figure,
                        caption="light curve", mime_type="image/png")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake paper")
    paper = store.mint(source="lib", kind="paper", produced_by="read_paper", path=pdf, title="A paper")

    shown = _run(execute_view_receipt(store, {"handle": minted.handle}))  # never sent to anyone
    assert not shown.is_error and shown.figures and shown.figures[0].sha256 == minted.sha256
    opened = _run(execute_view_receipt(store, {"handle": paper.handle}))
    assert not opened.is_error and opened.documents and opened.documents[0].sha256 == paper.sha256
    refused = _run(execute_view_receipt(store, {"handle": "analyst_worker_1:f9"}))
    assert refused.is_error and "not a receipt of this run" in refused.text


def test_a_steer_and_a_reply_name_the_human_as_the_address_and_a_notice_names_none():
    from auto_research.run_control import InboxEntry

    steer = InboxEntry("human", "check the background", kind="steer").render()
    assert steer.startswith("[Human steer] check the background") and 'message(to="human")' in steer
    reply = InboxEntry("human", "use 2-8 keV", kind="reply").render()
    assert reply.startswith("[Reply from the human] use 2-8 keV") and 'message(to="human")' in reply
    assert InboxEntry("runtime", "w0 ended its turn.", kind="notice").render() == "[Notice] w0 ended its turn."


# ── the human ─────────────────────────────────────────────────────────────────


def test_a_text_to_the_human_returns_at_once_and_the_reply_arrives_as_a_nudge(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    release = asyncio.Event()

    async def human(query):
        await release.wait()
        return HumanResponse(query_id=query.id, answer="use 2-8 keV")

    async def scenario():
        control = RunControl()
        router = SeatRouter(control, human_handler=human, store=store)
        router.register("analyst_lead", SimpleNamespace(has_kept_turn=lambda: False))
        control.set_live("analyst_lead", True)
        text, is_error = await router.send("analyst_lead", HUMAN, "which band?")
        assert not is_error and text.startswith("Sent to the human")
        assert router.outstanding("analyst_lead") == ["the human"]
        release.set()
        for _ in range(100):
            if control.has_inbox("analyst_lead"):
                break
            await asyncio.sleep(0.01)
        [entry] = control.drain_inbox("analyst_lead")
        assert entry.kind == "reply" and entry.render().startswith("[Reply from the human] use 2-8 keV")
        assert router.outstanding("analyst_lead") == []

    before = len(store.receipts())
    _run(scenario())
    assert len(store.receipts()) == before, "the human's text is a nudge, never a receipt"


def test_the_files_a_human_sends_become_the_senders_receipts(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    photo = tmp_path / "sketch.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\nsketch")

    async def human(query):
        return HumanResponse(
            query_id=query.id, answer="like this",
            attachments=[HumanAttachment(path=photo, filename="sketch.png", mime_type="image/png")],
        )

    async def scenario():
        control = RunControl()
        router = SeatRouter(control, human_handler=human, store=store)
        router.register("reviewer", SimpleNamespace(has_kept_turn=lambda: False))
        control.set_live("reviewer", True)
        await router.send("reviewer", HUMAN, "sketch it?", receipt_source="rev")
        for _ in range(100):
            if control.has_inbox("reviewer"):
                break
            await asyncio.sleep(0.01)
        return control.drain_inbox("reviewer")

    [entry] = _run(scenario())
    [handle] = entry.handles
    assert handle.startswith("rev:f") and store.get(handle).kind == "figure"
    assert handle in entry.text and "view_receipt" in entry.text


def test_an_unanswered_human_question_becomes_a_notice_not_an_answer():
    async def human(query):
        raise TimeoutError

    async def scenario():
        control = RunControl()
        router = SeatRouter(control, human_handler=human)
        router.register("analyst_lead", SimpleNamespace(has_kept_turn=lambda: False))
        control.set_live("analyst_lead", True)
        await router.send("analyst_lead", HUMAN, "which band?")
        for _ in range(100):
            if control.has_inbox("analyst_lead"):
                break
            await asyncio.sleep(0.01)
        return control.drain_inbox("analyst_lead")

    [entry] = _run(scenario())
    assert entry.kind == "notice" and "did not reply" in entry.text


# ── resuming a finished seat ──────────────────────────────────────────────────


def test_a_text_resumes_a_finished_seat_beside_the_sender_and_its_report_stays_filed():
    """B filed; A's text resumes B's NORMAL loop concurrently. B tries to re-file (refused),
    answers A, ends its turn; A's turn_end waits while B runs; A hears B, then a notice."""

    async def scenario():
        control = RunControl()
        router = SeatRouter(control)
        gate = asyncio.Event()
        b_model = _ScriptedModel([_call("turn_end")])
        a = _seat("analyst_lead", _ScriptedModel(), control, router)
        b = _seat("analyst_worker_0", b_model, control, router)
        b_tools = _SeatTools()
        await _turn(b, b_tools)
        assert b.has_kept_turn() and not control.is_live("analyst_worker_0")

        # Resumed turn: try to re-file, answer, end — held at each model call by `gate`.
        b_model.responses = [
            _call("edit_report", summary="rewritten"),
            _call("message", to="analyst_lead", text="the background was 3 arcmin"),
            _call("turn_end"),
        ]
        b_model.gate = gate
        a_tools = ChannelTools(_SeatTools(), role=a)
        text = (await a_tools.execute("message", {"to": "analyst_worker_0", "text": "bg?"})).text
        assert "resumed" in text
        await asyncio.sleep(0.05)
        assert control.is_live("analyst_worker_0"), "B runs beside A"
        held = await a_tools.execute("turn_end", {})
        assert held.is_error and "analyst_worker_0" in held.text
        gate.set()
        for _ in range(200):
            if not control.resumed_by("analyst_lead"):
                break
            await asyncio.sleep(0.01)
        assert control.resumed_by("analyst_lead") == []
        assert b_tools.report == "filed" and b_tools.edits == 0, "a filed report is final"
        # The injected text reached B's resumed loop.
        assert any(
            isinstance(m, ModelMessage) and "[Message from analyst_lead] bg?" in m.content
            for m in b_model.calls[1]
        )
        texts = [e.render() for e in control.drain_inbox("analyst_lead")]
        assert texts[0].startswith("[Message from analyst_worker_0] the background was 3 arcmin")
        assert texts[1] == "[Notice] analyst_worker_0 ended its resumed turn."
        assert not (await a_tools.execute("turn_end", {})).is_error

    _run(scenario())


def test_stop_cancels_a_resumed_seat():
    async def scenario():
        control = RunControl()
        router = SeatRouter(control)
        b_model = _ScriptedModel([_call("turn_end")])
        _seat("analyst_lead", _ScriptedModel(), control, router)
        b = _seat("analyst_worker_0", b_model, control, router)
        await _turn(b, _SeatTools())
        b_model.gate = asyncio.Event()  # never released: the resumed turn hangs on its model
        await router.send("analyst_lead", "analyst_worker_0", "still there?")
        await asyncio.sleep(0.05)
        assert control.resumed_by("analyst_lead") == ["analyst_worker_0"]
        assert await control.cancel_resumed() == 1
        assert control.resumed_by("analyst_lead") == []
        assert not control.is_live("analyst_worker_0")

    _run(scenario())


def test_wait_for_reply_wakes_on_the_inbox_and_says_when_nothing_is_outstanding():
    async def scenario():
        control = RunControl()
        router = SeatRouter(control)
        a = _seat("analyst_lead", _ScriptedModel(), control, router)
        tools = ChannelTools(_SeatTools(), role=a)
        idle = await tools.execute("wait_for_reply", {"timeout_s": 60})
        assert "Nothing is outstanding" in idle.text

        router._open_human["analyst_lead"] = 1  # a question to the human is open
        asyncio.get_running_loop().call_later(
            0.05, lambda: control.add_steer("analyst_lead", "use PI", kind="reply")
        )
        woke = await tools.execute("wait_for_reply", {"timeout_s": 60})
        assert "A message for you has arrived" in woke.text

    _run(scenario())


def test_a_seat_without_a_router_runs_the_bare_loop():
    """No channel ⇒ `_react` is exactly the loop: no channel tools declared, nothing kept."""
    model = _ScriptedModel([_call("turn_end")])
    control = RunControl()
    seat = _seat("analyst_colead", model, control, None)
    _run(_turn(seat, _SeatTools()))
    declared = {tool["name"] for tool in _SeatTools().tool_definitions()}
    assert declared == {"edit_report", "turn_end"}
    assert not seat.has_kept_turn()


def test_list_peers_names_each_address_with_its_role_model_and_state():
    """Free; the directory with what each peer does and whether a text reaches it live, resumes
    it, or waits for its turn — and the human only when one is attending."""
    async def scenario():
        control = RunControl()
        router = SeatRouter(control, human_handler=lambda q: "ok")
        lead = _seat("analyst_lead", _ScriptedModel(), control, router)
        lead.role = "analyst_lead"
        worker = _seat("analyst_worker_0", _ScriptedModel([_call("turn_end")]), control, router)
        _seat("analyst_worker_1", _ScriptedModel(), control, router)
        await _turn(worker, _SeatTools())
        out = await ChannelTools(_SeatTools(), role=lead).execute("list_peers", {})
        text = out.text
        assert not out.is_error
        assert "analyst_worker_0 — runs the analysis" in text and "a text resumes it" in text
        assert "analyst_worker_1" in text and "not started" in text
        assert "- human" in text and "analyst_lead —" not in text, "a seat does not list itself"

    _run(scenario())


def test_a_lead_built_without_a_channel_can_still_reach_its_coder():
    """The coder is a `message` address, so a seat whose loop can author gets a one-seat channel
    from `_react` itself when nothing put it on the team's (a lead built by attach_lead_runtime)."""
    seen: dict = {}

    class _AuthoringTools(_SeatTools):
        authoring_session = object()

    class _Model(_ScriptedModel):
        async def call(self, messages, **kwargs):
            seen["tools"] = [t["name"] for t in kwargs.get("tools") or []]
            message = next(t for t in kwargs["tools"] if t["name"] == "message")
            seen["to"] = message["input_schema"]["properties"]["to"]["enum"]
            return _call("turn_end")

    control = RunControl()
    lead = _seat("analyst_lead", _Model(), control, None)
    _run(_turn(lead, _AuthoringTools()))
    assert lead.channel is not None and "message" in seen["tools"]
    assert lead.channel.monitor is lead.monitor, "the one-seat channel ledgers to the role's monitor"
    assert seen["to"] == ["coder"], "no human and no peers on a one-seat channel"


def test_seat_to_seat_texts_are_mirrored_to_the_human_and_human_texts_are_not():
    posted: list[str] = []

    class _Handler:
        async def __call__(self, query):
            return HumanResponse(query_id=query.id, answer="ok")

        async def notify(self, text):
            posted.append(text)

    async def scenario():
        control = RunControl()
        router = SeatRouter(control, human_handler=_Handler())
        router.register("analyst_lead", SimpleNamespace(has_kept_turn=lambda: False))
        router.register("analyst_worker_0", SimpleNamespace(has_kept_turn=lambda: False))
        await router.send("analyst_lead", "analyst_worker_0", "which band?")
        await router.send("analyst_lead", HUMAN, "a question for you")
        for _ in range(50):
            if posted:
                break
            await asyncio.sleep(0.01)
        off = SeatRouter(control, human_handler=_Handler(), mirror=False)
        off.register("a", SimpleNamespace()), off.register("b", SimpleNamespace())
        await off.send("a", "b", "unmirrored")
        await asyncio.sleep(0.05)

    _run(scenario())
    assert posted == ["💬 analyst_lead → analyst_worker_0\n\nwhich band?"]


def test_a_viewed_result_says_what_its_call_was_given(tmp_path):
    """User rule 2026-11-03: a reader holding a handle can see whether a result's values were passed
    by reference; only a toolkit result carries the line, and it states what the runtime recorded."""
    from auto_research.schemas.artifacts import InputFile

    store = ReceiptStore(tmp_path / "receipts")
    tool = "research_toolkit.tk.fit"
    typed = store.mint(source="analyst_worker_1", kind="result", produced_by=tool, data="x = 1.2345")
    data = store.mint(source="analyst_worker_1", kind="data", produced_by=tool, data="E,q")
    chained = store.mint(source="analyst_worker_1", kind="result", produced_by=tool, data="y = 2.5",
                         parents=[typed.handle, data.handle],
                         inputs=[InputFile(path=tmp_path / "bands.csv", sha256="0" * 64, receipt=data.handle)])
    shell = store.mint(source="analyst_worker_1", kind="result", produced_by="mcp.builtin.bash_exec", data="ls")
    first = lambda h: _run(execute_view_receipt(store, {"handle": h})).text.splitlines()[0]
    assert first(typed.handle) == (f"{typed.handle} ({tool}; its call took no earlier result by "
                                   "reference and read no recorded file):")
    assert first(chained.handle) == f"{chained.handle} ({tool}; its call took {typed.handle} by reference and read bands.csv):"
    assert first(shell.handle) == f"{shell.handle} (mcp.builtin.bash_exec):"


def test_view_receipt_hands_every_seat_the_figure_with_its_handle(tmp_path):
    """A3: `view_receipt` hands any medium on, whatever the seat's member takes; the model-input
    sanitiser decides what the model receives (`tests/test_model_inputs.py`). The ref carries its
    receipt handle, which the sanitiser's note names for a text-only member."""
    from auto_research.models.types import ModelHandle, RoleModelConfig

    store = ReceiptStore(tmp_path / "receipts")
    figure = tmp_path / "fig.png"
    figure.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    minted = store.mint(source="analyst_worker_0", kind="figure", produced_by="plot", path=figure,
                        caption="light curve", mime_type="image/png")

    def view(provider: str, model: str) -> ToolOutcome:
        control = RunControl()
        router = SeatRouter(control, store=store)
        handle = ModelHandle(role="analyst_worker",
                             config=RoleModelConfig(name="m", provider=provider, model=model),
                             provider=_ScriptedModel())
        seat = _seat("analyst_worker_1", handle, control, router)
        return _run(ChannelTools(_SeatTools(), role=seat).execute(
            "view_receipt", {"handle": minted.handle}))

    for provider, model in (("deepseek", "deepseek-v4-pro"), ("zai", "glm-5.3"),
                            ("zai", "glm-5.3-flash")):
        shown = view(provider, model)
        (ref,) = shown.figures
        assert ref.sha256 == minted.sha256 and ref.receipt == minted.handle, provider
        assert shown.text == f"{minted.handle}: light curve"


def test_a_pdf_the_human_sent_is_handed_on_like_a_paper(tmp_path):
    """A human's file is minted as a `data` receipt; when its bytes are a PDF, `view_receipt`
    hands it on as a document for the sanitiser, and any other data receipt stays named."""
    from auto_research.hitl.attachments import mint_reply_attachments
    from tests.media_files import pdf_bytes

    store = ReceiptStore(tmp_path / "receipts")
    pdf = tmp_path / "draft.pdf"
    pdf.write_bytes(pdf_bytes("a draft"))
    table = tmp_path / "values.csv"
    table.write_text("a,b\n1,2\n", encoding="utf-8")
    response = HumanResponse(query_id="q1", answer="see these", attachments=[
        HumanAttachment(path=pdf, filename="draft.pdf", mime_type="application/pdf"),
        HumanAttachment(path=table, filename="values.csv", mime_type="text/csv"),
    ])
    paper, data = mint_reply_attachments(store, source="input", response=response, parent=None)
    assert paper.kind == data.kind == "data"

    opened = _run(execute_view_receipt(store, {"handle": paper.handle}))
    (doc,) = opened.documents
    assert not opened.is_error and doc.sha256 == paper.sha256
    assert opened.text.startswith(f"[{paper.handle}] ") and opened.text.endswith("the paper follows.")
    named = _run(execute_view_receipt(store, {"handle": data.handle}))
    assert not named.documents and "not text to show here" in named.text
