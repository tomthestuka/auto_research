"""Compaction of a long turn (B6 Anthropic server-side, B7 the runtime's summariser elsewhere).
No LLM, no network: the model and the summariser are scripted fakes standing in for the provider;
the loop, the plan, the ledger rebuild and the Anthropic request builder are the production code.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from auto_research.agents import compaction, history
from auto_research.agents.react import ToolOutcome
from auto_research.agents.roles import AnalystWorkerAgent, _unanswered_call_ids
from auto_research.env import CompactionConfig
from auto_research.models.types import (
    ModelMessage,
    ModelResponse,
    ModelUsage,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.monitor import RunMonitor

KEY = "analyst_worker_0"


def _on(monkeypatch):
    monkeypatch.setattr("auto_research.env.compaction_config",
                        lambda: CompactionConfig(enabled=True, trigger_fraction=0.9))


class _Tools:
    def tool_definitions(self):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                for n in ("fit", "turn_end")]

    async def execute(self, name, args):
        if name == "turn_end":
            return ToolOutcome(text="accepted")
        return ToolOutcome(text=f"fit result {args.get('i')} " + "z" * 2000)


class _Long:
    """A member near its window: every call reports a 95k-token prompt against a 100k limit."""

    def __init__(self, provider, calls):
        self.config = RoleModelConfig(name="m", provider=provider, model="m",
                                      context_tokens=100_000)
        self.sent: list[list] = []
        self.script = [ModelResponse(tool_calls=[ToolCall(id=f"f{i}", name="fit",
                                                          arguments={"i": i})],
                                     usage=ModelUsage(input_tokens=95_000))
                       for i in range(calls)]
        self.script.append(ModelResponse(tool_calls=[ToolCall(id="end", name="turn_end")]))

    async def call(self, messages, **kwargs):
        self.sent.append(list(messages))
        return self.script.pop(0)


class _Summariser:
    def __init__(self):
        self.read: list[str] = []

    async def call(self, messages, **kwargs):
        self.read.append(messages[-1].content)
        return ModelResponse(content="<summary>Task: fit. Done so far: fits 0-3.</summary>")


def _wire_summariser(monkeypatch):
    summariser = _Summariser()
    import auto_research.models as models

    monkeypatch.setattr(models, "resolve",
                        lambda role: SimpleNamespace(handles=[summariser]))
    return summariser


@pytest.mark.parametrize("provider", ["deepseek", "zai", "dashscope", "google"])
def test_a_long_turn_is_condensed_at_the_trigger_on_every_client_path(tmp_path, monkeypatch, provider):
    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _Long(provider, calls=9)
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = KEY
    seed = [ModelMessage(role="system", content="w"), ModelMessage(role="user", content="TASK")]
    messages, _, _ = asyncio.run(worker._react_loop(
        seed_messages=seed, tools=_Tools(), action="analyze", cap=20,
        terminal_tools=frozenset({"turn_end"}), task_receipt="input:h1",
    ))
    assert summariser.read, "the summariser was asked once the prompt passed 90% of the window"
    after = next(m for m in model.sent if any(
        isinstance(x, ModelMessage) and x.content.startswith("[The middle of this turn") for x in m))
    texts = [x.content for x in after if isinstance(x, ModelMessage)]
    assert texts[0] == "w" and "TASK" in texts, "the system prompt and the task stay word for word"
    # Ruling B13 (b): the new segment sends no earlier assistant turn, so no earlier thinking.
    cut = next(i for i, x in enumerate(after)
               if isinstance(x, ModelMessage) and x.content.startswith("[The middle of this turn"))
    assert not any(isinstance(x, ModelResponse) for x in after[:cut])
    assert [type(x).__name__ for x in after[:cut + 2]] == ["ModelMessage"] * (cut + 2)
    assert any('view_receipt("input:h1")' in t for t in texts), "the pin names the task receipt"
    assert _unanswered_call_ids(after) == []
    # The ledger rebuild replays the cut: every call's input equals what was sent.
    (turn,) = history.turns_for(monitor.jsonl_path, KEY).values()
    assert [[history.encode_message(m) for m in c.messages] for c in turn.calls] == [
        [history.encode_message(m) for m in sent] for sent in model.sent
    ]


def test_the_summary_is_a_message_not_a_receipt(tmp_path, monkeypatch):
    """It grounds no number: nothing is minted for it, so nothing can cite it."""
    from auto_research.provenance.store import ReceiptStore

    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    store = ReceiptStore.for_run(tmp_path / "run")
    before = len(store.receipts())
    worker = AnalystWorkerAgent(model=_Long("deepseek", calls=8), prompt="w",
                                monitor=RunMonitor(run_id="r1", jsonl_path=tmp_path / "m.jsonl"))
    worker.agent_key = KEY
    worker.receipts = store
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="TASK")], tools=_Tools(),
        action="analyze", cap=20, terminal_tools=frozenset({"turn_end"}),
    ))
    assert summariser.read, "a segment boundary ran"
    assert len(store.receipts()) == before


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_a_provider_with_server_compaction_is_condensed_by_it_not_by_us(tmp_path, monkeypatch, provider):
    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    worker = AnalystWorkerAgent(model=_Long(provider, calls=8), prompt="w",
                                monitor=RunMonitor(run_id="r1", jsonl_path=tmp_path / "m.jsonl"))
    worker.agent_key = KEY
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="TASK")], tools=_Tools(),
        action="analyze", cap=20, terminal_tools=frozenset({"turn_end"}),
    ))
    assert summariser.read == []


def test_a_server_compaction_is_recorded_and_followed_by_the_pin(tmp_path, monkeypatch):
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "m.jsonl", chatter=3)

    class _Model:
        def __init__(self):
            self.sent = []
            self.script = [
                ModelResponse(
                    tool_calls=[ToolCall(id="f0", name="fit", arguments={"i": 0})],
                    provider_parts=[{"type": "compaction", "content": "S"},
                                    {"type": "tool_use", "id": "f0", "name": "fit",
                                     "input": {"i": 0}}],
                    context_edits=[{"type": "compaction", "input_tokens": 900_000,
                                    "output_tokens": 3000, "summary_chars": 1}],
                ),
                ModelResponse(tool_calls=[ToolCall(id="end", name="turn_end")]),
            ]

        async def call(self, messages, **kwargs):
            self.sent.append(list(messages))
            return self.script.pop(0)

    model = _Model()
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = KEY
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="TASK")], tools=_Tools(),
        action="analyze", cap=5, terminal_tools=frozenset({"turn_end"}), task_receipt="input:h2",
    ))
    last = model.sent[-1][-1]
    assert isinstance(last, ModelMessage) and 'view_receipt("input:h2")' in last.content
    assert "TASK" in last.content, "the provider's summary replaced the task, so code restates it"
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    assert any(r["event"].endswith(".compaction") and r["data"].get("server") for r in rows)


def test_the_plan_keeps_discovery_turns_and_leaves_every_call_answered():
    discovery = ModelResponse(
        tool_calls=[ToolCall(id="d1", name="fit")],
        provider_parts=[{"type": "tool_search_call", "id": "ts1"},
                        {"type": "tool_search_output", "id": "ts1"}],
    )
    plain = lambda i: ModelResponse(tool_calls=[ToolCall(id=f"p{i}", name="note")])
    answer = lambda cid: ToolResultMessage(results=[ToolResultPart(tool_call_id=cid, name="x",
                                                                   content="r")])
    messages = [ModelMessage(role="system", content="s"), ModelMessage(role="user", content="old"),
                plain(0), answer("p0"), ModelMessage(role="user", content="TASK"),
                discovery, answer("d1")]
    for i in range(1, 7):
        messages += [plain(i), answer(f"p{i}")]
    plan = compaction.plan_compaction(messages, task_index=4)
    out = compaction.apply_compaction(messages, plan.as_part("summary"))
    assert out[0].content == "s" and out[1] is discovery and out[3].content == "TASK"
    assert out[4].content.startswith("[The middle of this turn")
    assert _unanswered_call_ids(out) == []
    assert sum(isinstance(m, ModelResponse) for m in out) == 1 + compaction.KEEP_RECENT_TURNS


def test_the_anthropic_request_carries_the_compaction_edit(monkeypatch):
    from auto_research.models.providers import anthropic as adapter

    seen = {}

    class _Client:
        def __init__(self, *a, **k):
            async def create(**request):
                seen.update(request)
                return SimpleNamespace(content=[], usage=None, stop_reason="end_turn")
            self.messages = SimpleNamespace(create=create)
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    monkeypatch.setattr("anthropic.AsyncAnthropic", _Client)
    monkeypatch.setattr(adapter, "compaction_config",
                        lambda: CompactionConfig(enabled=True, trigger_fraction=0.9))
    monkeypatch.setattr("auto_research.env.compaction_config",
                        lambda: CompactionConfig(enabled=True, trigger_fraction=0.9))
    cfg = RoleModelConfig(name="w", provider="anthropic", model="claude-sonnet-5")
    asyncio.run(adapter.AnthropicProvider().call(
        [ModelMessage(role="user", content="hi")], cfg, cache_prefix=True))
    edits = seen["context_management"]["edits"]
    compact = next(e for e in edits if e["type"] == "compact_20260112")
    assert compact["trigger"] == {"type": "input_tokens", "value": 900_000}  # 90% of litellm's 1M
    assert "Respond with the summary text alone." in compact["instructions"]
    assert "compact-2026-01-12" in seen["betas"]


def test_compaction_iterations_are_billed():
    from auto_research.models.providers.anthropic import _anthropic_usage

    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0,
                            iterations=[{"type": "compaction", "input_tokens": 900_000,
                                         "output_tokens": 3_000},
                                        {"type": "message", "input_tokens": 10,
                                         "output_tokens": 5}])
    got = _anthropic_usage(usage)
    assert (got.input_tokens, got.output_tokens) == (900_010, 3_005)


def test_compaction_config_reads_its_keys(monkeypatch):
    from auto_research.env import compaction_config

    monkeypatch.setattr("auto_research.env._orch_config",
                        lambda: {"compaction_enabled": True, "compaction_trigger_fraction": 2})
    cfg = compaction_config()
    assert cfg.enabled is True and cfg.trigger_fraction == 0.99
    monkeypatch.setattr("auto_research.env._orch_config", lambda: {"compaction_enabled": "yes"})
    assert compaction_config().enabled is False


def test_a_null_server_compaction_is_a_failure_not_a_summary():
    """The SDK: a compaction block with null content means compaction failed (a no-op)."""
    assert not compaction.has_server_compaction(
        ModelResponse(provider_parts=[{"type": "compaction", "content": None}]))
    assert compaction.has_server_compaction(
        ModelResponse(provider_parts=[{"type": "compaction", "content": "S"}]))


def test_a_summariser_that_fails_backs_off_and_says_so(tmp_path, monkeypatch):
    _on(monkeypatch)
    import auto_research.models as models

    class _Broken:
        calls = 0

        async def call(self, messages, **kwargs):
            _Broken.calls += 1
            return ModelResponse(content="")

    monkeypatch.setattr(models, "resolve", lambda role: SimpleNamespace(handles=[_Broken()]))
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "m.jsonl", chatter=3)
    worker = AnalystWorkerAgent(model=_Long("google", calls=9), prompt="w", monitor=monitor)
    worker.agent_key = KEY
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="TASK")], tools=_Tools(),
        action="analyze", cap=20, terminal_tools=frozenset({"turn_end"}),
    ))
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    failed = [r for r in rows if r["event"].endswith(".compaction_failed")]
    assert failed and _Broken.calls <= 3, "recorded, and retried only after a few calls"


def test_a_thinking_binding_refusal_strips_and_retries_once(monkeypatch):
    """Ruling B6.5's safety net, through `AnthropicProvider.call`."""
    from auto_research.models.providers import anthropic as adapter

    sent = []

    class _Refusal(Exception):
        message = ("messages.1.content.0: Invalid `signature` in `thinking` block. The block is "
                   "bound to a different conversation.")

    class _Client:
        def __init__(self, *a, **k):
            async def create(**request):
                sent.append(json.loads(json.dumps(request["messages"])))
                if len(sent) == 1:
                    raise _Refusal(_Refusal.message)
                return SimpleNamespace(content=[], usage=None, stop_reason="end_turn")
            self.messages = SimpleNamespace(create=create)
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))

    monkeypatch.setattr("anthropic.AsyncAnthropic", _Client)
    turn = ModelResponse(
        tool_calls=[ToolCall(id="c1", name="fit")],
        provider_parts=[{"type": "thinking", "thinking": "t", "signature": "S"},
                        {"type": "tool_use", "id": "c1", "name": "fit", "input": {}}],
    )
    messages = [ModelMessage(role="user", content="TASK"), turn,
                ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="fit",
                                                          content="r")])]
    cfg = RoleModelConfig(name="w", provider="anthropic", model="claude-sonnet-5")
    out = asyncio.run(adapter.AnthropicProvider().call(messages, cfg))
    assert len(sent) == 2
    assert [b["type"] for b in sent[1][1]["content"]] == ["tool_use"]
    assert {"type": "thinking_dropped", "from_message": 1} in out.context_edits


def test_the_openai_request_carries_context_management_and_replays_the_item(monkeypatch):
    """Through `OpenAIProvider.call`: the compaction threshold rides the request for an OpenAI
    member (90% of litellm's 922k for gpt-5.6-terra), and a returned compaction item is carried
    and handed back on the next request (probe: tests/test_live_openai_compaction.py)."""
    from auto_research.models.providers.openai import OpenAIProvider

    requests = []

    class _Item:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def model_dump(self, mode=None):
            return dict(self.__dict__)

    class _Responses:
        async def create(self, **request):
            requests.append(request)
            return SimpleNamespace(
                output=[_Item(type="compaction", id="cmp_1", encrypted_content="ENC")],
                usage=None, status="completed", incomplete_details=None, output_text="",
            )

    class _Client:
        def __init__(self, *a, **k):
            self.responses = _Responses()

    monkeypatch.setattr("openai.AsyncOpenAI", _Client)
    monkeypatch.setattr("auto_research.env.compaction_config",
                        lambda: CompactionConfig(enabled=True, trigger_fraction=0.9))
    cfg = RoleModelConfig(name="w", provider="openai", model="gpt-5.6-terra")
    first = asyncio.run(OpenAIProvider().call([ModelMessage(role="user", content="hi")], cfg,
                                              cache_prefix=True))
    assert requests[0]["extra_body"]["context_management"] == [
        {"type": "compaction", "compact_threshold": int(922_000 * 0.9)}
    ]
    assert compaction.has_server_compaction(first)
    asyncio.run(OpenAIProvider().call([ModelMessage(role="user", content="hi"), first], cfg,
                                      cache_prefix=True))
    assert {"type": "compaction", "id": "cmp_1", "encrypted_content": "ENC"} in requests[1]["input"]


# -- R3: a segment boundary when the media nears the provider's ceilings ---------------------


class _Plots:
    """Each `plot` returns ten small figures."""

    def __init__(self, tmp_path):
        self.tmp_path, self.n = tmp_path, 0

    def tool_definitions(self):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                for n in ("plot", "turn_end")]

    async def execute(self, name, args):
        import hashlib
        from datetime import UTC, datetime

        from PIL import Image

        from auto_research.schemas import FigureRef

        if name == "turn_end":
            return ToolOutcome(text="accepted")
        figures = []
        for _ in range(10):
            self.n += 1
            path = self.tmp_path / f"p{self.n}.png"
            Image.new("RGB", (60, 40), (self.n % 256, 0, 0)).save(path)
            figures.append(FigureRef(path=path, caption=f"plot {self.n}", mime_type="image/png",
                                     produced_by="plot", created_at=datetime.now(UTC),
                                     sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        return ToolOutcome(text="plotted", figures=figures)


class _Plotter:
    """An Anthropic member with a 200k window (100 images per request), far from its tokens."""

    def __init__(self, calls):
        self.config = RoleModelConfig(name="m", provider="anthropic", model="m",
                                      context_tokens=200_000, vision=True)
        self.sent: list[list] = []
        self.script = [ModelResponse(tool_calls=[ToolCall(id=f"p{i}", name="plot")],
                                     usage=ModelUsage(input_tokens=5_000)) for i in range(calls)]
        self.script.append(ModelResponse(tool_calls=[ToolCall(id="end", name="turn_end")]))

    async def call(self, messages, **kwargs):
        self.sent.append(list(messages))
        return self.script.pop(0)


def _images(messages) -> int:
    from auto_research.models.providers._media import media_load

    return media_load(messages)[0]


@pytest.mark.parametrize("calls, condensed", [(9, True), (7, False)])
def test_media_near_the_image_ceiling_starts_a_segment_on_any_provider(
    tmp_path, monkeypatch, calls, condensed
):
    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "m.jsonl", chatter=3)
    model = _Plotter(calls)
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = KEY
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="TASK")], tools=_Plots(tmp_path),
        action="analyze", cap=20, terminal_tools=frozenset({"turn_end"}),
    ))
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    triggers = [r["data"].get("trigger") for r in rows if r["event"].endswith(".compaction")]
    if condensed:
        assert summariser.read and triggers == ["media"]
        assert _images(model.sent[-1]) < 80, "the request fell back under 80% of 100 images"
        (turn,) = history.turns_for(monitor.jsonl_path, KEY).values()
        assert [[history.encode_message(m) for m in c.messages] for c in turn.calls] == [
            [history.encode_message(m) for m in sent] for sent in model.sent
        ]
    else:
        assert not summariser.read and triggers == []
        assert _images(model.sent[-1]) == 70


# -- R10: a condensed turn-boundary seed pins the receipts of every earlier turn ---------------


def test_a_condensed_seed_pins_the_receipts_of_every_earlier_turn(tmp_path, monkeypatch):
    from auto_research.schemas import AnalysisTask
    from tests.test_session_history import _setup

    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    worker, model, store, monitor = _setup(tmp_path)
    # A tiny window, so every boundary transcript is condensed; Anthropic, so no in-turn segment.
    model.config = RoleModelConfig(name="m", provider="anthropic", model="fake",
                                   context_tokens=200)
    for question in ("one", "two", "three"):
        asyncio.run(worker.analyze(AnalysisTask(question=question)))
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    fits = [r["data"]["history"]["result_receipt"] for r in rows
            if r["data"].get("tool") == "tk_fit" and "history" in r["data"]]
    assert len(fits) == 3 and all(fits)
    assert len(summariser.read) == 2, "turns two and three opened on a condensed record"
    (turn3,) = [t for t in history.turns_for(monitor.jsonl_path, KEY).values()][-1:]
    opening = next(m.content for m in turn3.messages
                   if isinstance(m, ModelMessage) and m.content.startswith(history.TRANSCRIPT_MARK))
    pin = opening.split("The record stands in the receipts:", 1)[1]
    assert "Your earlier turns were condensed" in opening
    assert fits[0] in pin and fits[1] in pin, "turn one's receipt survives two condensations"
    assert fits[2] not in pin, "turn three's own output is not an earlier turn's"


# -- R2 x R3 review fixes: what a cut keeps, and when the media trigger fires ------------------


class _PlotsAndPaper(_Plots):
    """`plot` as above; `read` opens a real PDF through the shared paper reader (a receipt)."""

    def __init__(self, tmp_path, store):
        super().__init__(tmp_path)
        from auto_research.agents.paper_tools import PaperReader
        from tests.test_paper_tools import _text_pdf

        self.reader = PaperReader(store, store, source="analyst_worker_0")
        self.pdf = _text_pdf(tmp_path / "lag.pdf", "The lag is 12.5 days.")

    def tool_definitions(self):
        return [*super().tool_definitions(),
                {"name": "read", "description": "read", "input_schema": {"type": "object"}}]

    async def execute(self, name, args):
        if name == "read":
            outcome, _receipt = await self.reader.open_pdf(self.pdf, title="Lag")
            return outcome
        return await super().execute(name, args)


class _Script:
    """An Anthropic member (200k window, 100 images) following a fixed list of tool batches."""

    def __init__(self, batches):
        self.config = RoleModelConfig(name="m", provider="anthropic", model="m",
                                      context_tokens=200_000, vision=True, documents=True)
        self.sent: list[list] = []
        self.script = [ModelResponse(tool_calls=[ToolCall(id=f"c{i}_{k}", name=n)
                                                 for k, n in enumerate(batch)],
                                     usage=ModelUsage(input_tokens=5_000))
                       for i, batch in enumerate(batches)]
        self.script.append(ModelResponse(tool_calls=[ToolCall(id="end", name="turn_end")]))

    async def call(self, messages, **kwargs):
        self.sent.append(list(messages))
        return self.script.pop(0)


def _run_script(tmp_path, monkeypatch, batches, tools=None):
    from auto_research.provenance.store import ReceiptStore

    _on(monkeypatch)
    summariser = _wire_summariser(monkeypatch)
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "m.jsonl", chatter=3)
    store = ReceiptStore.for_run(tmp_path / "run")
    model = _Script(batches)
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = KEY
    worker.receipts = store
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="TASK")],
        tools=tools or _PlotsAndPaper(tmp_path, store), action="analyze", cap=30,
        terminal_tools=frozenset({"turn_end"}),
    ))
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    return model, summariser, store, monitor, rows


def test_a_cut_carries_the_latest_rounds_document_and_pins_its_paper(tmp_path, monkeypatch):
    """The round that tipped the media over the line opened a paper the model has not read yet:
    the cut keeps it, on the message that restates that round, and pins its receipt."""
    model, summariser, store, monitor, rows = _run_script(
        tmp_path, monkeypatch, [["plot"]] * 7 + [["plot", "read"]])
    assert summariser.read, "80 images: the boundary ran"
    after = model.sent[-1]
    restated = [m for m in after if isinstance(m, ModelMessage) and m.documents]
    assert len(restated) == 1 and restated[0].content.startswith("Your latest calls")
    (paper,) = store.receipts(kind="paper")
    pin = next(m.content for m in after if isinstance(m, ModelMessage)
               and "The record stands in the receipts" in m.content)
    assert paper.handle in pin
    (turn,) = history.turns_for(monitor.jsonl_path, KEY).values()
    assert [[history.encode_message(m) for m in c.messages] for c in turn.calls] == [
        [history.encode_message(m) for m in sent] for sent in model.sent
    ]


def test_media_the_cut_cannot_remove_never_refires_the_boundary(tmp_path, monkeypatch):
    """A latest round that alone holds 90 images stays word for word, so the boundary does not
    fire on it (it fired five times in twenty calls before the fix)."""
    class _Ninety(_Plots):
        async def execute(self, name, args):
            if name == "turn_end":
                return ToolOutcome(text="accepted")
            outcomes = [await super().execute(name, args) for _ in range(9)]
            return ToolOutcome(text="plotted",
                               figures=[f for o in outcomes for f in o.figures])

    model, summariser, store, monitor, rows = _run_script(
        tmp_path, monkeypatch, [["plot"]] * 6, tools=_Ninety(tmp_path))
    triggers = [r["data"].get("trigger") for r in rows if r["event"].endswith(".compaction")]
    assert triggers.count("media") <= 1


def test_the_bytes_trigger_fires_without_waiting_out_the_back_off(tmp_path, monkeypatch):
    """Two 9 MB documents (24 MB as base64) pass 70% of Anthropic's 32 MB request ceiling one
    call after a cut: the boundary runs at once rather than four calls later."""
    from datetime import UTC, datetime

    from auto_research.schemas import DocumentRef

    class _Big(_Plots):
        def tool_definitions(self):
            return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                    for n in ("open", "turn_end")]

        async def execute(self, name, args):
            if name == "turn_end":
                return ToolOutcome(text="accepted")
            self.n += 1
            path = self.tmp_path / f"big{self.n}.pdf"
            path.write_bytes(b"%PDF-1.4\n" + bytes([self.n]) * (9 * 1024 * 1024))
            import hashlib

            return ToolOutcome(text="opened", documents=[DocumentRef(
                path=path, title=f"big {self.n}", mime_type="application/pdf",
                created_at=datetime.now(UTC),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())])

    model, summariser, store, monitor, rows = _run_script(
        tmp_path, monkeypatch, [["open"], ["open"], ["open"]], tools=_Big(tmp_path))
    triggers = [r["data"].get("trigger") for r in rows if r["event"].endswith(".compaction")]
    assert triggers == ["media", "media"], "the second cut came one call after the first"
    from auto_research.models.providers._media import media_load

    assert all(media_load(sent)[1] < 0.7 * 32 * 1024 * 1024 for sent in model.sent), (
        "no request went past the line")


def test_an_indexed_output_names_its_documents_and_a_short_one_its_receipt(tmp_path):
    """The transcript keeps every earlier value citable: a short output whole with its receipt,
    a result that attached a paper names it (the PDF itself leaves the indexed view)."""
    from datetime import UTC, datetime

    from auto_research.schemas import DocumentRef

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4 p")
    import hashlib

    paper = DocumentRef(path=pdf, title="[lib:p1] Lag paper", mime_type="application/pdf",
                        created_at=datetime.now(UTC),
                        sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    messages = [
        ModelResponse(tool_calls=[ToolCall(id="c1", name="read"), ToolCall(id="c2", name="fit")]),
        ToolResultMessage(results=[
            ToolResultPart(tool_call_id="c1", name="read", content="attached", documents=[paper]),
            ToolResultPart(tool_call_id="c2", name="fit", content="chi2 = 41.5",
                           receipt="analyst_worker_0:r3"),
        ]),
    ]
    once = history.apply_view(messages, "indexed", receipts={})
    read, fit = once[1].results
    assert not read.documents and "It attached [lib:p1] Lag paper." in read.content
    assert fit.content == "chi2 = 41.5 [analyst_worker_0:r3]"
    assert history.apply_view(once, "indexed", receipts={})[1].results[1].content == fit.content


def test_the_pin_names_only_this_seats_receipts_never_a_handle_quoted_in_a_text(tmp_path, monkeypatch):
    """R10 reads the rebuilt turns, not the transcript text: a handle a task quotes is not pinned,
    a short output's receipt is."""
    from auto_research.schemas import AnalysisTask
    from tests.test_session_history import _setup

    _on(monkeypatch)
    _wire_summariser(monkeypatch)
    worker, model, store, monitor = _setup(tmp_path)
    model.config = RoleModelConfig(name="m", provider="anthropic", model="fake",
                                   context_tokens=200)
    asyncio.run(worker.analyze(AnalysisTask(
        question='compare with view_receipt("analyst_worker_9:r99") and [other:r7]')))
    asyncio.run(worker.analyze(AnalysisTask(question="two")))
    (_, turn2) = history.turns_for(monitor.jsonl_path, KEY).values()
    opening = next(m.content for m in turn2.messages
                   if isinstance(m, ModelMessage) and m.content.startswith(history.TRANSCRIPT_MARK))
    pin = opening.split("The record stands in the receipts:", 1)[1]
    assert "analyst_worker_9:r99" not in pin and "other:r7" not in pin
    rows = [json.loads(line) for line in monitor.jsonl_path.read_text().splitlines()]
    fit = next(r["data"]["history"]["result_receipt"] for r in rows
               if r["data"].get("tool") == "tk_fit" and "history" in r["data"])
    assert fit in pin
