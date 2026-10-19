"""The image budget (B4 at the turn boundary, rulings B3/B6/B8) and the provider ceilings behind it.
No LLM, no network: driven through the real ``AgentRole._react_loop`` with a recording model."""

from __future__ import annotations

import asyncio
import hashlib
import math
from datetime import UTC, datetime

from auto_research.agents import history, images
from auto_research.agents.react import ToolOutcome
from auto_research.agents.roles import AnalystWorkerAgent
from auto_research.models.types import ModelMessage, ModelResponse, ToolCall, ToolResultMessage
from auto_research.monitor import RunMonitor
from auto_research.provenance.store import ReceiptStore
from auto_research.schemas import FigureRef

W, H = 1400, 1000
PER_IMAGE = math.ceil(W / 28) * math.ceil(H / 28)  # 50 x 36 = 1800 tokens


def _png(path, shade):
    from PIL import Image

    Image.new("RGB", (W, H), (shade % 256, 40, 90)).save(path)
    return path


class _Plotter:
    """`plot` returns three new figures per call, each minted as a figure receipt."""

    def __init__(self, tmp_path, store, catalog, per_call=3):
        self.tmp_path, self.store, self.catalog, self.per_call = tmp_path, store, catalog, per_call
        self.n = 0

    def tool_definitions(self):
        return [{"name": n, "description": n, "input_schema": {"type": "object"}}
                for n in ("plot", "turn_end")]

    async def execute(self, name, args):
        if name == "turn_end":
            return ToolOutcome(text="accepted")
        figures = []
        for _ in range(self.per_call):
            self.n += 1
            path = _png(self.tmp_path / f"fig{self.n}.png", self.n * 7)
            receipt = self.store.mint(source="analyst_worker_0", kind="figure",
                                      produced_by="plot", path=path,
                                      caption=f"plot {self.n}", mime_type="image/png")
            figure = FigureRef(path=path, caption=f"plot {self.n}", mime_type="image/png",
                               produced_by="plot", created_at=datetime.now(UTC),
                               sha256=receipt.sha256)
            self.catalog.append(figure)
            figures.append(figure)
        return ToolOutcome(text=f"plotted {len(figures)}", figures=figures)


class _Recorder:
    def __init__(self, calls):
        self.script = [ModelResponse(tool_calls=[ToolCall(id=f"p{i}", name="plot")])
                       for i in range(calls)]
        self.script.append(ModelResponse(tool_calls=[ToolCall(id="end", name="turn_end")]))
        self.sent: list[tuple[list, list]] = []

    async def call(self, messages, *, images=(), **kwargs):
        self.sent.append((list(messages), list(images)))
        return self.script.pop(0)


def _run(tmp_path, calls, per_call=3, peer=()):
    store = ReceiptStore.for_run(tmp_path / "run")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _Recorder(calls)
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = "analyst_worker_0"
    worker.receipts = store
    tools = _Plotter(tmp_path, store, worker._figure_catalog["self"], per_call)
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="plot")], tools=tools,
        action="analyze", cap=calls + 2, terminal_tools=frozenset({"turn_end"}),
        images=list(peer),
    ))
    return model, monitor, store


def _parts(messages):
    return [p for m in messages if isinstance(m, ToolResultMessage) for p in m.results
            if p.name == "plot"]


def test_within_a_turn_every_figure_rides_every_later_call(tmp_path):
    """Ruling B6: within a turn the conversation only grows at its end, so nothing is evicted
    mid-turn; each call sends every figure set so far, unchanged."""
    model, monitor, store = _run(tmp_path, calls=20)  # 20 sets x 3 = 60 images
    for k, (messages, _) in enumerate(model.sent):
        parts = _parts(messages)
        assert len(parts) == k and all(len(p.figures) == 3 for p in parts)
    rebuilt = list(history.turns_for(monitor.jsonl_path, "analyst_worker_0").values())[0]
    assert len(rebuilt.calls) == len(model.sent)


def test_at_the_boundary_the_newest_sets_within_the_budget_ride_the_transcript(tmp_path):
    """60 images in one turn: the next turn's transcript carries the newest whole sets, at most
    20k image tokens once the 40k budget is passed (ruling B8), the rest named by handle."""
    model, monitor, store = _run(tmp_path, calls=20)
    turn = list(history.turns_for(monitor.jsonl_path, "analyst_worker_0").values())[0]
    sets = history.turn_figure_sets(turn.messages)
    kept = images.keep_newest(sets)
    per_set = 3 * PER_IMAGE
    assert len(kept) % 3 == 0 and len(kept) // 3 * per_set <= images.IMAGE_LOW_WATER_TOKENS
    assert kept == [f for s_ in sets[-len(kept) // 3:] for f in s_], "the newest, whole"
    text = history.transcript(turn.messages, turn.handles.get("figures", {}))
    assert text.count("view_receipt shows each again") == 20, "every set named by handle"


def test_an_image_costs_what_the_provider_processes(tmp_path):
    fig = _png(tmp_path / "a.png", 1)
    assert images.image_tokens(fig) == PER_IMAGE
    assert images.image_tokens(tmp_path / "missing.png") == images.UNREADABLE_IMAGE_TOKENS
    from PIL import Image

    big = tmp_path / "huge.png"
    Image.new("RGB", (8000, 6000)).save(big)
    assert images.image_tokens(big) == images.MAX_IMAGE_TOKENS


def test_the_anthropic_ceilings_follow_the_declared_window():
    """Docs: 600 images per request above a 200k window (100 at 200k), 600 PDF pages at 1M."""
    from auto_research.models.providers.anthropic import _limits_for
    from auto_research.models.types import RoleModelConfig

    def member(window):
        return RoleModelConfig(name="m", provider="anthropic", model="x", context_tokens=window)

    assert (_limits_for(member(1_000_000)).max_images, _limits_for(member(1_000_000)).max_pdf_pages) == (600, 600)
    assert (_limits_for(member(200_000)).max_images, _limits_for(member(200_000)).max_pdf_pages) == (100, 100)
    assert _limits_for(member(None)).max_images == 100


def test_the_provider_count_backstop_withholds_the_oldest_images(tmp_path):
    from auto_research.models.providers._media import MediaItem, MediaLimits, plan_media

    paths = [_png(tmp_path / f"c{i}.png", i) for i in range(5)]
    refs = [FigureRef(path=p, caption=str(i), mime_type="image/png", produced_by="t",
                      created_at=datetime.now(UTC),
                      sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            for i, p in enumerate(paths)]
    items = [MediaItem(r, True, r.path, r.mime_type) for r in refs]
    plan = plan_media(items, MediaLimits(image_mimes=frozenset({"image/png"}), max_images=3))
    kept = [plan.resolve(r).dropped for r in refs]
    assert kept == [True, True, False, False, False], "the two oldest go, the newest three stay"


def test_the_window_comes_from_litellm_unless_overridden():
    from auto_research.models.providers.anthropic import _limits_for
    from auto_research.models.types import RoleModelConfig
    from auto_research.observability.costs import context_window_for

    sonnet = RoleModelConfig(name="w", provider="anthropic", model="claude-sonnet-5")
    assert context_window_for(sonnet) == 1_000_000 and _limits_for(sonnet).max_images == 600
    override = sonnet.model_copy(update={"context_tokens": 150_000})
    assert context_window_for(override) == 150_000 and _limits_for(override).max_images == 100


def _refs(tmp_path, n, prefix, size=(W, H)):
    from PIL import Image

    out = []
    for i in range(n):
        path = tmp_path / f"{prefix}{i}.png"
        Image.new("RGB", size, (i % 256, 9, 9)).save(path)
        out.append(FigureRef(path=path, caption=f"{prefix} {i}", mime_type="image/png",
                             produced_by="peer", created_at=datetime.now(UTC),
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return out


def test_the_adapter_applies_the_window_dependent_ceiling(monkeypatch):
    """Through `AnthropicProvider.call`, not only `_limits_for`: the plan sees 600 on a 1M member."""
    from types import SimpleNamespace

    from auto_research.models.providers import anthropic as adapter
    from auto_research.models.types import RoleModelConfig

    seen = {}
    real = adapter.plan_for_call

    def spy(messages, images, documents, limits, reserved_bytes=0):
        seen["max_images"] = limits.max_images
        return real(messages, images, documents, limits, reserved_bytes=reserved_bytes)

    class _Client:
        def __init__(self, *a, **k):
            async def create(**request):
                return SimpleNamespace(content=[], usage=None, stop_reason="end_turn")
            self.messages = SimpleNamespace(create=create)
            self.beta = SimpleNamespace(messages=self.messages)

    monkeypatch.setattr(adapter, "plan_for_call", spy)
    monkeypatch.setattr("anthropic.AsyncAnthropic", _Client)
    cfg = RoleModelConfig(name="w", provider="anthropic", model="claude-sonnet-5")
    asyncio.run(adapter.AnthropicProvider().call([ModelMessage(role="user", content="hi")], cfg))
    assert seen["max_images"] == 600


def test_the_next_turns_opening_carries_those_figures(tmp_path):
    """Through `AgentRole._history_seed`, the method every seat opens its next turn with."""
    store = ReceiptStore.for_run(tmp_path / "run")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _Recorder(20)
    worker = AnalystWorkerAgent(model=model, prompt="w", monitor=monitor)
    worker.agent_key = "analyst_worker_0"
    worker.receipts = store
    tools = _Plotter(tmp_path, store, worker._figure_catalog["self"])
    asyncio.run(worker._react_loop(
        seed_messages=[ModelMessage(role="user", content="plot")], tools=tools,
        action="analyze", cap=22, terminal_tools=frozenset({"turn_end"}),
    ))
    opening, base = asyncio.run(worker._history_seed())
    turn = list(history.turns_for(monitor.jsonl_path, "analyst_worker_0").values())[0]
    assert opening[1].figures == images.keep_newest(history.turn_figure_sets(turn.messages))
    assert opening[1].figures and base["view"] == "transcript"


# -- R7: a turn's task attaches each figure once per session, within the image budget ---------


def _peer_report(tmp_path, store, start, count):
    """A peer's report citing ``count`` figures, each minted as the peer's figure receipt."""
    from auto_research.schemas import AnalystReport

    figures = []
    for n in range(start, start + count):
        path = _png(tmp_path / f"peer{n}.png", n * 11)
        receipt = store.mint(source="analyst_worker_1", kind="figure", produced_by="plot",
                             path=path, caption=f"peer plot {n}", mime_type="image/png")
        figures.append(FigureRef(path=path, caption=f"peer plot {n}", mime_type="image/png",
                                 produced_by="plot", created_at=datetime.now(UTC),
                                 sha256=receipt.sha256))
    return AnalystReport(summary="peer", findings=["peer finding"], status="succeeded",
                         figures=figures), figures


def _task_message(sent):
    """The turn's task: the last user message of the turn's first request."""
    return [m for m in sent if isinstance(m, ModelMessage) and m.role == "user"][-1]


def test_a_continued_worker_attaches_only_peer_figures_it_has_not_carried(tmp_path):
    from auto_research.schemas import AnalysisTask
    from tests.test_session_history import _setup

    worker, model, store, monitor = _setup(tmp_path)
    report1, first = _peer_report(tmp_path, store, 0, 2)
    asyncio.run(worker.analyze(AnalysisTask(question="one"), peer_reports=[report1]))
    assert [f.sha256 for f in _task_message(model.sent[0]).figures] == [f.sha256 for f in first]

    report2, second = _peer_report(tmp_path, store, 2, 1)
    report2 = report2.model_copy(update={"figures": [*first, *second]})
    start = len(model.sent)
    asyncio.run(worker.analyze(AnalysisTask(question="two"), peer_reports=[report2]))
    task = _task_message(model.sent[start])
    assert [f.sha256 for f in task.figures] == [second[0].sha256], "only the new peer figure"
    carried = [f.sha256 for m in model.sent[start] if isinstance(m, ModelMessage)
               for f in m.figures]
    assert carried.count(first[0].sha256) == 1, "an earlier figure rides the transcript once"
    handles = {r.sha256: r.handle for r in store.receipts(kind="figure")}
    opening = next(m.content for m in model.sent[start] if isinstance(m, ModelMessage)
                   and m.content.startswith(history.TRANSCRIPT_MARK))
    assert all(handles[f.sha256] in opening for f in first), "the transcript names them"


def test_a_task_figure_set_past_the_budget_keeps_the_newest_and_names_the_rest(tmp_path):
    from auto_research.schemas import AnalysisTask
    from tests.test_session_history import _setup

    worker, model, store, monitor = _setup(tmp_path)
    count = images.IMAGE_BUDGET_TOKENS // PER_IMAGE + 2           # 24 x 1800 = 43,200 > 40k
    report, figures = _peer_report(tmp_path, store, 0, count)
    asyncio.run(worker.analyze(AnalysisTask(question="one"), peer_reports=[report]))
    task = _task_message(model.sent[0])
    kept = images.IMAGE_LOW_WATER_TOKENS // PER_IMAGE                # 11 within 20k
    assert [f.sha256 for f in task.figures] == [f.sha256 for f in figures[-kept:]]
    handles = {r.sha256: r.handle for r in store.receipts(kind="figure")}
    for figure in figures[:-kept]:
        assert handles[figure.sha256] in task.content, "a figure left out is named by handle"
    assert handles[figures[-1].sha256] not in task.content.split("not_attached", 1)[-1]


def test_a_fresh_session_attaches_the_whole_set_again(tmp_path):
    """Without a ledger nothing was carried, so each turn attaches every peer figure."""
    from auto_research.schemas import AnalysisTask
    from tests.test_session_history import _Model, _worker

    store = ReceiptStore.for_run(tmp_path / "runs" / "r1")
    model = _Model()
    worker = _worker(tmp_path, model, store, None)
    report, figures = _peer_report(tmp_path, store, 0, 2)
    asyncio.run(worker.analyze(AnalysisTask(question="one"), peer_reports=[report]))
    start = len(model.sent)
    asyncio.run(worker.analyze(AnalysisTask(question="two"), peer_reports=[report]))
    assert len(_task_message(model.sent[start]).figures) == 2


def test_the_leads_next_cycle_attaches_only_the_catalogs_new_figures(tmp_path):
    """The lead's figure catalog accumulates across cycles; a continued cycle attaches only the
    figures its session has not carried."""
    from types import SimpleNamespace

    from auto_research.agents.react import ReadOnlyToolProvider
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.models.types import ModelResponse, ToolCall
    from auto_research.schemas import AnalysisTask

    class _LeadModel:
        def __init__(self):
            self.config = SimpleNamespace(model="fake-lead")
            self.sent: list[list] = []
            self.n = 0

        async def call(self, messages, **kwargs):
            self.sent.append(list(messages))
            self.n += 1
            if self.n % 2 == 1:
                return ModelResponse(tool_calls=[ToolCall(
                    id=f"e{self.n}", name="write_report",
                    arguments={"content": "# summary\nthe workers agree\n# status\nsucceeded\n"
                                          "# rework\nfalse\n# rationale\nconsistent\n"
                                          "# findings\n- they agree\n"})])
            return ModelResponse(tool_calls=[ToolCall(id=f"t{self.n}", name="turn_end",
                                                      arguments={})])

    store = ReceiptStore.for_run(tmp_path / "runs" / "r1")
    monitor = RunMonitor(run_id="r1", jsonl_path=tmp_path / "monitor.jsonl", chatter=3)
    model = _LeadModel()
    lead = AnalystLeadAgent(model=model, prompt="lead", monitor=monitor)
    lead.agent_key = "analyst_lead"
    lead.receipts = store
    lead.report_dir = tmp_path / "runs" / "r1" / "analyst_lead"
    lead.report_dir.mkdir(parents=True)
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    report1, first = _peer_report(tmp_path, store, 0, 2)
    asyncio.run(lead.run_cycle(AnalysisTask(question="first"), [report1]))
    assert len(_task_message(model.sent[0]).figures) == 2
    report2, second = _peer_report(tmp_path, store, 2, 1)
    start = len(model.sent)
    asyncio.run(lead.run_cycle(AnalysisTask(question="second"), [report2]))
    task = _task_message(model.sent[start])
    assert [f.sha256 for f in task.figures] == [second[0].sha256]
