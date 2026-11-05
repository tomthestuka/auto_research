"""A2 — the model-input sanitiser inside ``ModelHandle.call`` (CLAUDE.md §9.3.9, ruling 2026-11-04).

Every case drives the real ``ModelHandle.call`` over the real adapter of each vendor; only the
vendor SDK's client (the network) is faked, recording the request it would have sent. A document's
local parse runs the real ``_doc_parse.parse_document`` over a parse seeded in its content-keyed
cache (Docling itself has its own gated live test), so the path is production's but for the
40-second conversion.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_research.models.types import (
    ModelHandle,
    ModelMessage,
    ModelResponse,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.schemas import DocumentRef, FigureRef
from tests.media_files import pdf_bytes, png_bytes

PARSED_TEXT = "The polarization degree rises with energy (Table 2)."


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


# ── media ──────────────────────────────────────────────────────────────────────


def _figure(path: Path, caption: str = "light curve", color=(10, 120, 200)) -> FigureRef:
    path.write_bytes(png_bytes((16, 12), color))
    return FigureRef(path=path, caption=caption, mime_type="image/png", produced_by="plot",
                     created_at=datetime(2026, 11, 4, tzinfo=UTC),
                     sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _paper(path: Path) -> DocumentRef:
    path.write_bytes(pdf_bytes("a paper about polarization"))
    return DocumentRef(path=path, title="Paper One", mime_type="application/pdf",
                       created_at=datetime(2026, 11, 4, tzinfo=UTC),
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def _seed_parse(document: DocumentRef, crop: FigureRef) -> None:
    """The parse ``_doc_parse`` would have cached for ``document``: its text and one crop."""
    from auto_research.models.providers._doc_parse import cache_root

    base = cache_root() / hashlib.sha256(Path(document.path).read_bytes()).hexdigest()
    base.mkdir(parents=True)
    (base / "fig1.png").write_bytes(Path(crop.path).read_bytes())
    (base / "text.md").write_text(PARSED_TEXT, encoding="utf-8")
    ref = crop.model_dump(mode="json", exclude={"path"})
    (base / "manifest.json").write_text(json.dumps(
        {"figures": [{"file": "fig1.png", "ref": ref}], "truncated": False}), encoding="utf-8")


def _b64(path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


# ── the vendors' networks, faked ───────────────────────────────────────────────


class _Wire:
    """The last request any faked vendor client received."""

    request: dict = {}


class _Anthropic:
    def __init__(self, *a, **k):
        pass

    @property
    def messages(self):
        return self

    @property
    def beta(self):
        return SimpleNamespace(messages=self)

    async def create(self, **request):
        _Wire.request = request
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))


class _Responses:
    def __init__(self, *a, **k):
        pass

    @property
    def responses(self):
        return self

    async def create(self, **request):
        _Wire.request = request
        return SimpleNamespace(
            output=[SimpleNamespace(type="message", role="assistant",
                                    content=[SimpleNamespace(type="output_text", text="ok")])],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


class _Gemini:
    def __init__(self, *a, **k):
        self.aio = SimpleNamespace(models=self)

    async def generate_content(self, *, model, contents, config):
        _Wire.request = {"contents": contents}
        from tests.test_providers import _g_part, _g_resp

        return _g_resp(_g_part(text="ok"))


@pytest.fixture
def wire(monkeypatch):
    import anthropic
    import openai

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Anthropic)
    monkeypatch.setattr(openai, "AsyncOpenAI", _Responses)
    monkeypatch.setattr("google.genai.Client", _Gemini)
    _Wire.request = {}
    return _Wire


def _sent(request) -> str:
    """Everything a request carries, as one string: text, and media as base64."""
    def walk(value):
        if isinstance(value, (bytes, bytearray)):
            yield base64.b64encode(bytes(value)).decode("ascii")
        elif isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from walk(item)
        elif hasattr(value, "__dict__"):
            yield from walk(vars(value))
    return "\n".join(walk(request))


def _handle(provider: str, model: str) -> ModelHandle:
    from auto_research.models.providers import provider_for

    config = RoleModelConfig(name="m", provider=provider, model=model,
                             parameters={"max_tokens": 1024} if provider == "anthropic" else {})
    return ModelHandle(role="analyst_worker", config=config, provider=provider_for(provider))


#: (provider, model, takes images, reads a PDF natively) — as each capability entry gives it (Z1d).
SEATS = [
    ("anthropic", "claude-sonnet-5", True, True),
    ("openai", "gpt-5.6-terra", True, True),
    ("google", "gemini-3.5-flash", True, True),
    ("dashscope", "qwen3.8-max", True, False),
    ("zai", "glm-5.3-flash", True, False),
    ("zai", "glm-5.3", False, False),
    ("deepseek", "deepseek-flash", True, False),
    ("deepseek", "deepseek-v4-pro", False, False),
]


def _carriers(figure: FigureRef, paper: DocumentRef) -> dict[str, list]:
    """Each place a medium rides a request: a user message (a task's or a peer's figures, the
    history seed's), a tool result (a tool's figure, a paper the tool opened)."""
    call = ModelResponse(tool_calls=[ToolCall(id="c1", name="view_receipt", arguments={})])
    return {
        "message figure": [ModelMessage(role="user", content="the task", figures=[figure])],
        "tool-result figure": [
            ModelMessage(role="user", content="the task"), call,
            ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="view_receipt",
                                                      content="w0:f1: light curve",
                                                      figures=[figure])]),
        ],
        "tool-result paper": [
            ModelMessage(role="user", content="the task"), call,
            ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="view_receipt",
                                                      content="lib:p1 — the paper follows.",
                                                      documents=[paper])]),
        ],
    }


@pytest.mark.parametrize(("provider", "model", "images", "native_pdf"), SEATS,
                         ids=[f"{p}:{m}" for p, m, *_ in SEATS])
def test_every_vendor_receives_each_medium_as_its_capability_allows(
    tmp_path, wire, provider, model, images, native_pdf,
):
    figure = _figure(tmp_path / "fig.png")
    crop = _figure(tmp_path / "crop.png", caption="Fig. 3", color=(200, 40, 40))
    paper = _paper(tmp_path / "paper.pdf")
    _seed_parse(paper, crop)
    handle = _handle(provider, model)

    for carrier, messages in _carriers(figure, paper).items():
        asyncio.run(handle.call(messages, viewers=["analyst_lead"]))
        sent = _sent(wire.request)
        if carrier.endswith("figure"):
            if images:
                assert _b64(figure.path) in sent, carrier
                assert "[withheld]" not in sent, carrier
            else:
                assert _b64(figure.path) not in sent, carrier
                assert ("[withheld] Your model reads text, so the figure 'light curve' (fig.png) "
                        "is named here rather than shown. Ask analyst_lead") in sent, carrier
        elif native_pdf:
            assert _b64(paper.path) in sent and PARSED_TEXT not in sent, carrier
        else:
            assert _b64(paper.path) not in sent, carrier
            assert "[extracted document] paper.pdf (Paper One)" in sent and PARSED_TEXT in sent
            assert (_b64(crop.path) in sent) is images, carrier
            if not images:
                assert "Ask analyst_lead" in sent and "figures of paper.pdf" in sent


@pytest.mark.parametrize(("provider", "model", "images", "native_pdf"), SEATS,
                         ids=[f"{p}:{m}" for p, m, *_ in SEATS])
def test_call_time_media_reach_the_member_the_same_way(
    tmp_path, wire, provider, model, images, native_pdf,
):
    """`images=` / `documents=` (a tool-less structured call's channel) are sanitised too."""
    figure = _figure(tmp_path / "fig.png")
    crop = _figure(tmp_path / "crop.png", caption="Fig. 3", color=(200, 40, 40))
    paper = _paper(tmp_path / "paper.pdf")
    _seed_parse(paper, crop)

    asyncio.run(_handle(provider, model).call(
        [ModelMessage(role="user", content="review")], images=[figure], documents=[paper]))
    sent = _sent(wire.request)
    assert (_b64(figure.path) in sent) is images
    assert ("[withheld]" in sent) is not images
    assert (_b64(paper.path) in sent) is native_pdf
    assert (PARSED_TEXT in sent) is not native_pdf


def test_the_same_request_is_sanitised_to_the_same_bytes(tmp_path, wire):
    """A replayed turn is byte-identical, so the provider's prompt cache holds."""
    figure = _figure(tmp_path / "fig.png")
    crop = _figure(tmp_path / "crop.png", caption="Fig. 3", color=(200, 40, 40))
    paper = _paper(tmp_path / "paper.pdf")
    _seed_parse(paper, crop)
    for provider, model in (("deepseek", "deepseek-v4-pro"), ("dashscope", "qwen3.8-max")):
        handle = _handle(provider, model)
        sent = []
        for _ in range(2):
            carriers = _carriers(figure, paper)
            messages = [*carriers["message figure"], *carriers["tool-result paper"][1:]]
            asyncio.run(handle.call(messages, viewers=["analyst_lead"]))
            sent.append(json.dumps(wire.request["input"], sort_keys=True))
        assert sent[0] == sent[1], provider


def test_a_document_the_parser_cannot_read_is_named_unread_and_parsed_once(
    tmp_path, wire, monkeypatch,
):
    """The failure is the same answer on every later call: byte-identical, and no second parse."""
    import auto_research.models.providers._doc_parse as doc_parse

    paper = _paper(tmp_path / "paper.pdf")  # no parse cached: the parser runs, and fails
    attempts = []

    def _fails(*a, **k):
        attempts.append(1)
        raise RuntimeError("parser unavailable")

    monkeypatch.setattr(doc_parse, "_run_docling", _fails)
    sent = []
    for _ in range(2):
        asyncio.run(_handle("deepseek", "deepseek-v4-pro").call(
            [ModelMessage(role="user", content="read", documents=[paper])]))
        sent.append(_sent(wire.request))
    assert "[unavailable] paper.pdf (Paper One) could not be converted to text here" in sent[0]
    assert _b64(paper.path) not in sent[0]
    assert sent[0] == sent[1] and len(attempts) == 1


def test_a_text_only_note_names_no_one_when_the_caller_names_no_viewer(tmp_path, wire):
    figure = _figure(tmp_path / "fig.png")
    asyncio.run(_handle("deepseek", "deepseek-v4-pro").call(
        [ModelMessage(role="user", content="look", figures=[figure])]))
    sent = _sent(wire.request)
    assert ("[withheld] Your model reads text, so the figure 'light curve' (fig.png) is named here "
            "rather than shown.") in sent
    assert "Ask " not in sent and "list_peers" not in sent


def test_the_viewers_are_the_seats_peers_that_take_images(tmp_path):
    """`_image_viewers`: the channel's other seats whose members take images, sorted; never the
    seat itself or the human, and nothing for a seat that takes images itself."""
    from auto_research.agents.roles import _image_viewers
    from auto_research.hitl.router import SeatRouter
    from auto_research.run_control import RunControl

    router = SeatRouter(RunControl(), human_handler=lambda q: "ok")
    seats = {
        "analyst_worker_0": _handle("deepseek", "deepseek-v4-pro"),
        "analyst_worker_1": _handle("zai", "glm-5.3"),
        "reviewer": _handle("anthropic", "claude-fable-5"),
        "analyst_lead": _handle("openai", "gpt-5.6-sol"),
    }
    for key, handle in seats.items():
        router.register(key, SimpleNamespace(model=handle, agent_key=key, channel=router))
    assert _image_viewers(router.seat("analyst_worker_0")) == {
        "viewers": ["analyst_lead", "reviewer"]}
    assert _image_viewers(router.seat("reviewer")) == {}


# ── a run-shaped worker on a text-only seat ─────────────────────────────────────


class _ViewsThePaper:
    """The worker's vendor, faked at the adapter: view `lib:p1` and a peer's figure in one batch,
    write the report, file it."""

    def __init__(self):
        self.requests: list[list] = []

    async def call(self, messages, config, **kwargs):
        self.requests.append(list(messages))
        step = len(self.requests)
        if step == 1:
            return ModelResponse(content="", stop="tool_use", tool_calls=[
                ToolCall(id="v1", name="view_receipt", arguments={"handle": "lib:p1"}),
                ToolCall(id="v1b", name="view_receipt", arguments={"handle": "analyst_worker_1:f1"}),
            ])
        if step == 2:
            return ModelResponse(content="", stop="tool_use", tool_calls=[
                ToolCall(id="v2", name="write_report", arguments={"content": (
                    "# summary\nthe paper was read\n# status\nsucceeded\n# findings\n"
                    "- the paper's trend is noted\n")})])
        return ModelResponse(content="", stop="tool_use", tool_calls=[
            ToolCall(id="v3", name="turn_end", arguments={"summary": "filed"})])


def test_a_worker_on_a_text_only_seat_views_a_paper_and_a_figure_and_files(tmp_path):
    """The run's crash (polevo-20261104): a worker viewed `lib:p1` and its next model call raised.
    Now, through the real `execute_view_receipt` and `ModelHandle.call`, the paper reaches it as
    the local parse in place at its tool result, a peer's figure as a note naming its handle and
    the peer that can view it, and it files (A2/A3)."""
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.hitl.router import SeatRouter
    from auto_research.monitor import RunMonitor
    from auto_research.provenance import ReceiptStore
    from auto_research.run_control import RunControl
    from auto_research.schemas import AnalysisTask
    from tests.test_sandbox import _FakeRegistry, _FakeSandbox

    store = ReceiptStore(tmp_path / "receipts")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(pdf_bytes("a paper about polarization"))
    receipt = store.mint(source="lib", kind="paper", produced_by="read_paper", path=pdf,
                         title="Paper One")
    assert receipt.handle == "lib:p1"
    plot = _figure(tmp_path / "plot.png")
    figure = store.mint(source="analyst_worker_1", kind="figure", produced_by="plot", path=plot.path,
                        caption="light curve", mime_type="image/png")
    assert figure.handle == "analyst_worker_1:f1"
    crop = _figure(tmp_path / "crop.png", caption="Fig. 3")
    _seed_parse(DocumentRef(path=pdf, title="Paper One", mime_type="application/pdf",
                            created_at=datetime.now(UTC), sha256=receipt.sha256), crop)

    adapter = _ViewsThePaper()
    monitor = RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
    worker = AnalystWorkerAgent(
        model=ModelHandle(role="analyst_worker",
                          config=RoleModelConfig(name="ds", provider="deepseek",
                                                 model="deepseek-v4-pro"),
                          provider=adapter),
        prompt="p", sandbox=_FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": ""}),
        registry=_FakeRegistry(known=set(), mcp=set()), monitor=monitor,
    )
    worker.agent_key = "analyst_worker_0"
    worker.report_dir = tmp_path / "w0"
    worker.receipts = store
    worker.run_control = RunControl()
    worker.channel = SeatRouter(worker.run_control, monitor=monitor, store=store)
    worker.channel.register(worker.agent_key, worker)
    peer = SimpleNamespace(model=_handle("anthropic", "claude-opus-5"),
                           has_kept_turn=lambda: False)
    worker.channel.register("analyst_lead", peer)

    report = asyncio.run(worker.analyze(AnalysisTask(question="q")))

    assert report.summary == "the paper was read"
    result = [m for m in adapter.requests[1] if isinstance(m, ToolResultMessage)][-1]
    paper_part, figure_part = result.results
    assert paper_part.documents == [] and paper_part.figures == []  # neither PDF nor crop
    assert paper_part.content.startswith("[lib:p1] Paper One — the paper follows.")
    assert "[extracted document] paper.pdf ([lib:p1] Paper One)" in paper_part.content
    assert PARSED_TEXT in paper_part.content and "Ask analyst_lead" in paper_part.content
    assert figure_part.figures == []
    assert ("[withheld] Your model reads text, so the figure analyst_worker_1:f1 'light curve' "
            "(plot.png) is named here rather than shown. Ask analyst_lead") in figure_part.content
    assert "view_receipt" in figure_part.content
    assert not [e for e in monitor.events if e.event.endswith(".model_call_failed")]


def _text_seat(adapter) -> ModelHandle:
    return ModelHandle(role="analyst_worker",
                       config=RoleModelConfig(name="ds", provider="deepseek",
                                              model="deepseek-v4-pro"),
                       provider=adapter)


class _Answers:
    async def call(self, messages, config, **kwargs):
        return ModelResponse(content="ok")


def test_a_first_parse_runs_off_the_event_loop(tmp_path, monkeypatch):
    """A3 ruling: Docling's first parse of a paper runs in a worker thread, so another coroutine (a
    peer seat, the /stop listener) progresses meanwhile. A handshake, not a clock: the parse waits
    until the peer has run; on the event loop it would wait in vain and fail."""
    import threading

    import auto_research.models.providers._doc_parse as doc_parse
    from auto_research.models.providers._doc_parse import ParsedDocument

    paper = _paper(tmp_path / "paper.pdf")  # not in the cache: the parse runs
    peer_ran = threading.Event()
    waited: list[bool] = []

    def _parse_once_the_peer_ran(document, base, *, data):
        waited.append(peer_ran.wait(5))  # False: the loop was blocked while the parse ran
        return ParsedDocument(text=PARSED_TEXT, figures=(), truncated=False)

    monkeypatch.setattr(doc_parse, "_run_docling", _parse_once_the_peer_ran)

    async def _peer():
        for _ in range(3):
            await asyncio.sleep(0)
        peer_ran.set()

    async def _main():
        await asyncio.gather(
            _text_seat(_Answers()).call(
                [ModelMessage(role="user", content="read", documents=[paper])]),
            _peer(),
        )

    asyncio.run(_main())
    assert waited == [True], "the peer progressed while the parse ran"


def test_two_seats_parsing_one_paper_parse_it_once(tmp_path, monkeypatch):
    """Two seats sanitise in worker threads at once; the parse is serial, so one Docling run
    serves both and a failure in one never deletes the other's cache folder."""
    import threading
    import time

    import auto_research.models.providers._doc_parse as doc_parse
    from auto_research.models.providers._doc_parse import ParsedDocument

    paper = _paper(tmp_path / "paper.pdf")
    runs: list[int] = []
    real = doc_parse._run_docling

    def _parse(document, base, *, data):
        runs.append(threading.get_ident())
        time.sleep(0.2)
        base.mkdir(parents=True, exist_ok=True)
        (base / "text.md").write_text(PARSED_TEXT, encoding="utf-8")
        (base / "manifest.json").write_text(json.dumps({"figures": [], "truncated": False}),
                                            encoding="utf-8")
        return ParsedDocument(text=PARSED_TEXT, figures=(), truncated=False)

    assert real is not _parse
    monkeypatch.setattr(doc_parse, "_run_docling", _parse)
    handle = _text_seat(_Answers())

    async def _main():
        await asyncio.gather(*(
            handle.call([ModelMessage(role="user", content="read", documents=[paper])])
            for _ in range(2)))

    asyncio.run(_main())
    assert len(runs) == 1, "the second seat read the first one's parse from the cache"
