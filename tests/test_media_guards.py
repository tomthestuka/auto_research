"""The multimodal request guards ([V-5]).

Live run `8c75990e` died at $116.10 — after all five of its tools had committed — on
``400 … image dimensions exceed max allowed size for many-image requests: 2000 pixels``.
Anthropic tightens its per-image dimension cap once a request carries more than 20 image blocks;
a plotter returning 3 PNGs per call, each replayed inline at its tool-result turn, crossed 20 with
a 2677x1554 figure aboard. Nothing in any adapter checked count, dimensions, bytes, payload or
mime.

These tests pin the guard at the boundary that failed, and the properties that keep it honest: an
under-limit request is byte-identical to before, a breach is never silent, and the budget is
computed over the WHOLE request rather than per item — the property being stateless buys us.
"""

from __future__ import annotations

import asyncio
import base64
import json
import io
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_research.models.providers._media import (
    MediaLimits,
    iter_media,
    nonmedia_weight,
    plan_for_call,
    plan_media,
)
from auto_research.models.types import (
    ModelMessage,
    RoleModelConfig,
    ToolResultMessage,
    ToolResultPart,
)
from auto_research.schemas import DocumentRef, FigureRef

_ANTHROPIC_LIKE = MediaLimits(
    image_mimes=frozenset({"image/png", "image/jpeg"}),
    doc_mimes=frozenset({"application/pdf"}),
    max_images=100,
    strict_block_threshold=20,
    strict_max_edge=2000,
    max_edge=8000,
    max_image_bytes=10 * 1024 * 1024,
    max_request_bytes=32 * 1024 * 1024,
    max_pdf_pages=100,
)


def _png(path: Path, size: tuple[int, int], *, noisy: bool = False) -> Path:
    """A PNG of exactly ``size``. ``noisy`` fills it with incompressible pixels, which is what a
    real plot is — a flat fill compresses to a few hundred bytes and cannot exercise a budget."""
    import os

    from PIL import Image

    if noisy:
        image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        image = Image.new("RGB", size, (30, 60, 90))
    image.save(path, format="PNG")
    return path


def _figure(path: Path, mime: str = "image/png") -> FigureRef:
    return FigureRef(
        path=path,
        caption=f"figure {path.name}",
        mime_type=mime,
        produced_by="test",
        created_at=datetime.now(timezone.utc),
        sha256=sha256(path.read_bytes()).hexdigest(),
    )


def _document(path: Path, mime: str = "application/pdf") -> DocumentRef:
    return DocumentRef(
        path=path,
        title=path.name,
        mime_type=mime,
        created_at=datetime.now(timezone.utc),
        sha256=sha256(path.read_bytes()).hexdigest(),
    )


def _turn_with(figures: list[FigureRef]) -> ToolResultMessage:
    return ToolResultMessage(
        results=[ToolResultPart(tool_call_id="c1", name="plot", content="ok", figures=figures)]
    )


def _anthropic_image_blocks(messages: list[dict]) -> list[dict]:
    """Every ``image`` block in an outgoing Anthropic request, at any depth.

    A tool's figures ride NESTED inside their ``tool_result``'s ``content`` list, so a walker that
    reads only a message's top-level blocks finds none of them — and an assertion written on that
    walker reports a working guard as having dropped everything.
    """
    found: list[dict] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                found.append(block)
            elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                found.extend(
                    nested
                    for nested in block["content"]
                    if isinstance(nested, dict) and nested.get("type") == "image"
                )
    return found


def _dimensions(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


# ── the boundary that killed the run ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [1, 20, 21])
def test_the_strict_dimension_cap_applies_from_the_first_send(tmp_path, count):
    """Every image is fitted to 2000 px from its first send, whatever the request's count.

    21 image blocks is the boundary that killed run 8c75990e (a 2677x1554 figure, legal alone).
    Fitting only past it changed bytes already sent once a turn crossed it; within a turn a
    conversation only grows at its end (human ruling B6, 2026-10-18), so the cap applies always.
    """
    figures = [
        _figure(_png(tmp_path / f"f{i}.png", (2677, 1554))) for i in range(count)
    ]
    plan = plan_media(iter_media([_turn_with(figures)]), _ANTHROPIC_LIKE)

    resolved = plan.resolve(figures[0])
    assert not resolved.dropped
    width, height = _dimensions(resolved.data)
    assert (width, height) == (2000, 1161)  # aspect ratio preserved
    assert "downscaled" in (resolved.note or "")


def test_under_limit_bytes_are_identical_to_no_guard(tmp_path):
    """A request inside every limit is byte-for-byte what it was before this module existed.

    The guard must be inert on the common path; a gratuitous re-encode would change every
    cached prefix and pay a resize cost for nothing.
    """
    path = _png(tmp_path / "small.png", (800, 600))
    figure = _figure(path)
    plan = plan_media(iter_media([_turn_with([figure])]), _ANTHROPIC_LIKE)

    resolved = plan.resolve(figure)
    assert resolved.data == path.read_bytes()
    assert resolved.note is None


def test_the_budget_spans_the_whole_request_not_one_turn(tmp_path):
    """History figures and this call's `images=` are counted together.

    Being stateless is what makes this possible: the adapter holds the entire conversation plus
    the call's attachments before it sends, so the count the provider will apply is knowable.
    """
    history = [_figure(_png(tmp_path / f"h{i}.png", (2677, 1554))) for i in range(15)]
    call_time = [_figure(_png(tmp_path / f"c{i}.png", (2677, 1554))) for i in range(6)]

    items = iter_media([_turn_with(history)], images=call_time)
    assert len(items) == 21

    plan = plan_media(items, _ANTHROPIC_LIKE)
    # 15 + 6 crosses 20, so even the first history figure is resized.
    assert max(_dimensions(plan.resolve(history[0]).data)) == 2000
    assert max(_dimensions(plan.resolve(call_time[-1]).data)) == 2000


# ── never silent ──────────────────────────────────────────────────────────────────────────────


def test_over_budget_drops_oldest_first_with_a_note(tmp_path):
    """The payload ceiling evicts the oldest media; each dropped block leaves an explanation.

    Oldest-first because the current turn's evidence is what the model is reasoning about now.
    """
    figures = [
        _figure(_png(tmp_path / f"b{i}.png", (400, 400), noisy=True)) for i in range(4)
    ]
    # Budget derived from the real encoded sizes so the test pins the *policy* (which items go)
    # rather than a guess about PNG compression: room for the newest two, not all four.
    encoded = [len(f.path.read_bytes()) * 4 // 3 for f in figures]
    budget = MediaLimits(
        image_mimes=frozenset({"image/png"}),
        max_request_bytes=encoded[-1] + encoded[-2] + 1024,
    )
    plan = plan_media(iter_media([_turn_with(figures)]), budget)

    kept = [f for f in figures if not plan.resolve(f).dropped]
    dropped = [f for f in figures if plan.resolve(f).dropped]

    assert dropped, "the budget must bite"
    assert kept, "the newest media survives"
    assert figures.index(dropped[-1]) < figures.index(kept[0]), "oldest went first"
    for figure in dropped:
        note = plan.resolve(figure).note or ""
        assert figure.path.name in note and "withheld" in note


def test_unsupported_image_mime_never_reaches_the_image_channel(tmp_path):
    """Closes [V-3]: a PDF handed over as a figure is withheld, not routed to `image`.

    Every provider 400s on a non-image mime in an image block, and `FigureRef.mime_type` is an
    unconstrained string, so the check has to live where the block is built.
    """
    pdf = tmp_path / "not-an-image.pdf"
    pdf.write_bytes(b"%PDF-1.5\n" + b"0" * 1024)
    figure = _figure(pdf, mime="application/pdf")

    resolved = plan_media(iter_media([_turn_with([figure])]), _ANTHROPIC_LIKE).resolve(figure)

    assert resolved.dropped
    assert "not an image type" in (resolved.note or "")


def test_pdf_over_the_page_limit_is_withheld_with_its_page_count(tmp_path):
    """Anthropic caps PDFs at 100 pages for our sub-1M-context models."""
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(101):
        writer.add_blank_page(width=200, height=200)
    path = tmp_path / "long.pdf"
    writer.write(path)
    document = _document(path)

    resolved = plan_media(iter_media([], documents=[document]), _ANTHROPIC_LIKE).resolve(document)

    assert resolved.dropped
    assert "101 pages" in (resolved.note or "")


def test_a_short_pdf_passes_the_page_check(tmp_path):
    """The counterpart: the page gate must not refuse an ordinary paper."""
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    for _ in range(12):
        writer.add_blank_page(width=200, height=200)
    path = tmp_path / "paper.pdf"
    writer.write(path)
    document = _document(path)

    resolved = plan_media(iter_media([], documents=[document]), _ANTHROPIC_LIKE).resolve(document)

    assert not resolved.dropped and resolved.note is None


# ── the adapters actually apply it ────────────────────────────────────────────────────────────


class _CaptureAnthropic:
    """Records the request Anthropic would have received."""

    store: dict = {}

    def __init__(self, *a, **k):
        pass

    @property
    def messages(self):
        return self

    @property
    def beta(self):
        return SimpleNamespace(messages=self)

    async def create(self, **request):
        _CaptureAnthropic.store["request"] = request
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def test_anthropic_adapter_resizes_a_many_image_request(tmp_path, monkeypatch):
    """End to end through the adapter: >20 oversized figures leave as ≤2000 px image blocks.

    Had this existed on 2026-08-06, run `8c75990e` would have finished its census instead of
    dying one step after its fifth commit.
    """
    import anthropic

    from auto_research.models.providers.anthropic import AnthropicProvider

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _CaptureAnthropic)
    figures = [_figure(_png(tmp_path / f"a{i}.png", (2677, 1554))) for i in range(21)]
    convo = [ModelMessage(role="user", content="review these"), _turn_with(figures)]
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake", vision=True)

    asyncio.run(AnthropicProvider().call(convo, cfg))

    blocks = _anthropic_image_blocks(_CaptureAnthropic.store["request"]["messages"])
    assert len(blocks) == 21
    for block in blocks:
        data = base64.b64decode(block["source"]["data"])
        assert max(_dimensions(data)) <= 2000


# ── the ceiling covers the whole payload, not just the media ──────────────────────────────────


def test_the_media_budget_reserves_the_non_media_request_weight(tmp_path):
    """Media that fits the ceiling *alone* still has to leave room for the rest of the request.

    Anthropic states the rule for both document limits: "Both limits are on the entire request
    payload, including any other content sent alongside PDFs". Budgeting media against the full
    ceiling is what killed run `fbb93f26` at its final node with a 413 and no media-guard event:
    two PDFs came to 33,512,296 encoded bytes under a 33,554,432 ceiling, leaving 42 KB for a
    conversation orders of magnitude larger.
    """
    figures = [_figure(_png(tmp_path / f"r{i}.png", (400, 400), noisy=True)) for i in range(3)]
    encoded = [len(f.path.read_bytes()) * 4 // 3 for f in figures]
    conversation = [ModelMessage(role="user", content="c" * 20_000)]
    reserve = nonmedia_weight(conversation)
    # A ceiling the media fits exactly, and does not fit once the conversation is counted.
    limits = MediaLimits(
        image_mimes=frozenset({"image/png"}),
        max_request_bytes=sum(encoded) + 1024,
    )
    turn = _turn_with(figures)

    unreserved = plan_media(iter_media([turn]), limits)
    assert not any(unreserved.resolve(f).dropped for f in figures), "fits on media alone"

    reserved = plan_media(iter_media([turn]), limits, reserve)
    dropped = [f for f in figures if reserved.resolve(f).dropped]
    assert dropped, "the reserve must bite where the bare ceiling did not"
    assert figures.index(dropped[-1]) < figures.index(figures[-1]), "oldest went first"
    note = reserved.resolve(dropped[0]).note or ""
    assert "left for media" in note and dropped[0].path.name in note


def test_nonmedia_weight_counts_text_tool_defs_and_provider_parts(tmp_path):
    """Every non-media channel that rides the request is counted, because the reserve is only
    safe if it is not systematically low."""
    from auto_research.models.types import ModelResponse, ToolCall

    text = ModelMessage(role="user", content="u" * 5_000)
    assert nonmedia_weight([text]) > 5_000

    result = ToolResultMessage(
        results=[ToolResultPart(tool_call_id="t1", name="read_file", content="r" * 8_000)]
    )
    assert nonmedia_weight([result]) > 8_000

    turn = ModelResponse(
        content="a" * 100,
        tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "p" * 3_000})],
        provider_parts=[{"type": "thinking", "thinking": "z" * 4_000}],
    )
    assert nonmedia_weight([turn]) > 7_000

    tools = [{"name": "read_file", "description": "d" * 6_000, "input_schema": {}}]
    assert nonmedia_weight([], tools=tools) > 6_000

    # Every part of one request adds up rather than any single one dominating.
    total = nonmedia_weight([text, result, turn], tools=tools)
    assert total > 5_000 + 8_000 + 7_000 + 6_000


def test_the_reserve_is_never_below_what_the_adapter_actually_sends():
    """The reserve must round **up** against the real on-the-wire non-media payload.

    Under-reserving is the failure mode that costs a whole request, so the property worth
    pinning is an inequality against the translator itself rather than a hand-picked constant —
    which keeps it true as the adapter's message shapes change. Measured against the verbatim
    record of the reviewer call that actually 413'd (12 messages, 6 tools, a five-round ReAct
    trajectory): the true payload was 72,124 bytes and the reserve 81,787, i.e. **1.13x**. This
    reconstructs that shape from our own types at the same scale.
    """
    import json as _json

    from auto_research.models.providers._media import MediaPlan
    from auto_research.models.providers.anthropic import _to_anthropic_messages
    from auto_research.models.types import ModelResponse, ToolCall

    messages: list = [
        ModelMessage(role="system", content="S" * 9_200),
        ModelMessage(role="user", content="U" * 12_000),
    ]
    for i in range(5):
        messages.append(
            ModelResponse(
                content=f"turn {i} narration " + "a" * 400,
                tool_calls=[ToolCall(id=f"t{i}", name="read_file",
                                     arguments={"path": f"/x/{i}", "offset": i, "limit": 200})],
                provider_parts=[{"type": "thinking", "thinking": "z" * 1_500}],
            )
        )
        messages.append(
            ToolResultMessage(
                results=[ToolResultPart(tool_call_id=f"t{i}", name="read_file",
                                        content="r" * 6_000)]
            )
        )
    tools = [
        {"name": n, "description": "d" * 600, "input_schema": {"type": "object",
         "properties": {"path": {"type": "string"}}, "required": ["path"]}}
        for n in ("read_file", "list_dir", "text_search", "open_paper", "web_search", "web_fetch")
    ]

    system, provider_messages = _to_anthropic_messages(messages, MediaPlan())
    wire = len(_json.dumps(
        {"system": system, "messages": provider_messages, "tools": tools}, default=str
    ).encode())
    reserve = nonmedia_weight(messages, tools=tools)

    assert reserve >= wire, f"reserve {reserve} under-reserves the {wire}-byte payload"


def test_an_unserializable_tool_call_argument_still_contributes_weight():
    """The reserve never silently reads as zero because a value would not JSON-encode: an
    under-reserve is the failure that costs a request."""
    from auto_research.models.types import ModelResponse, ToolCall

    class _Opaque:
        def __repr__(self) -> str:
            return "o" * 4_000

    turn = ModelResponse(
        tool_calls=[ToolCall(id="t1", name="tool", arguments={"blob": _Opaque()})]
    )
    assert nonmedia_weight([turn]) > 3_000


def test_a_document_changed_after_its_receipt_is_sent_with_a_note_in_the_request(tmp_path, monkeypatch):
    """F26 end to end through the adapter: the PDF goes out, and the model is told it changed."""
    import anthropic

    from auto_research.models.providers.anthropic import AnthropicProvider

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _CaptureAnthropic)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 original")
    document = _document(pdf)
    pdf.write_bytes(b"%PDF-1.4 swapped after the receipt")
    convo = [ModelMessage(role="user", content="read this")]
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake", vision=True)

    asyncio.run(AnthropicProvider().call(convo, cfg, documents=[document]))

    request = json.dumps(_CaptureAnthropic.store["request"]["messages"])
    assert "differs from its receipt" in request
    assert '"document"' in request  # still sent


def test_anthropic_nests_a_document_in_the_tool_result_that_opened_it(tmp_path, monkeypatch):
    """R2: the document block sits inside its ``tool_result`` content ("Example of tool result
    with documents"), and the request carries no front-of-conversation document message."""
    import anthropic

    from auto_research.models.providers.anthropic import AnthropicProvider
    from auto_research.models.types import ModelResponse, ToolCall, ToolResultPart

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _CaptureAnthropic)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 paper")
    document = _document(pdf)
    convo = [
        ModelMessage(role="user", content="read it"),
        ModelResponse(tool_calls=[ToolCall(id="c1", name="read_paper", arguments={})]),
        ToolResultMessage(results=[ToolResultPart(
            tool_call_id="c1", name="read_paper", content="attached", documents=[document])]),
    ]
    cfg = RoleModelConfig(name="m", provider="anthropic", model="fake", vision=True,
                          documents=True)
    asyncio.run(AnthropicProvider().call(convo, cfg))
    messages = _CaptureAnthropic.store["request"]["messages"]
    assert messages[0]["content"] == "read it" or messages[0]["content"][0]["type"] == "text", (
        "no document message in front")
    (result,) = messages[-1]["content"]
    assert result["type"] == "tool_result"
    assert [b["type"] for b in result["content"]] == ["text", "document"]


def test_nested_and_message_documents_count_against_the_request_ceiling(tmp_path):
    """R2: a document nested in a tool result, or carried by a message, is walked by the plan in
    conversation order, so it counts against the request ceiling like a call-time one (the 413
    class: an unwalked document is an unbudgeted one)."""
    from auto_research.models.types import ToolResultPart

    small = tmp_path / "small.pdf"
    small.write_bytes(b"%PDF-1.4 " + b"s" * 1024)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4 " + b"b" * 25_164_990)  # 33,553,333 bytes as base64
    first, second = _document(small), _document(big)
    convo = [
        ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="read",
                                                  content="ok", documents=[first])]),
        ModelMessage(role="user", content="carried", documents=[second]),
    ]
    items = iter_media(convo)
    assert [(i.ref, i.is_image) for i in items] == [(first, False), (second, False)]
    plan = plan_media(items, _ANTHROPIC_LIKE)
    assert not plan.resolve(second).dropped, "the newest fits the ceiling alone"
    assert plan.resolve(first).dropped, "the nested one counted: together they pass 32 MB"


def test_a_member_without_documents_refuses_a_nested_or_carried_document(tmp_path, monkeypatch):
    from auto_research.models.types import ModelHandle, ToolResultPart
    from tests.test_models import FakeProvider

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4 p")
    document = _document(pdf)
    handle = ModelHandle(
        role="analyst_worker",
        config=RoleModelConfig(name="fake", provider="openai", model="fake", vision=True),
        provider=FakeProvider(),
    )
    nested = ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="read",
                                                       content="ok", documents=[document])])
    for convo in ([nested], [ModelMessage(role="user", content="x", documents=[document])]):
        with pytest.raises(ValueError, match="not configured for document"):
            asyncio.run(handle.call(convo))


def test_google_renders_a_nested_document_after_the_function_response(tmp_path):
    from google.genai import types

    from auto_research.models.providers.google import _to_gemini_contents
    from auto_research.models.types import ModelResponse, ToolCall, ToolResultPart

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.4 paper")
    document = _document(pdf)
    convo = [
        ModelMessage(role="user", content="read"),
        ModelResponse(tool_calls=[ToolCall(id="c1", name="read", arguments={})]),
        ToolResultMessage(results=[ToolResultPart(tool_call_id="c1", name="read",
                                                  content="ok", documents=[document])]),
    ]
    _system, contents = _to_gemini_contents(convo, types, plan_media(iter_media(convo),
                                                                      _ANTHROPIC_LIKE))
    assert contents[-2].parts[0].function_response is not None
    (part,) = contents[-1].parts
    assert part.inline_data.mime_type == "application/pdf"
