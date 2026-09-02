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


def _dimensions(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


# ── the boundary that killed the run ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("count,expect_resized", [(20, False), (21, True)])
def test_strict_dimension_cap_applies_only_past_the_block_threshold(
    tmp_path, count, expect_resized
):
    """21 image blocks tightens the cap to 2000 px; 20 leaves the image untouched.

    This is the exact boundary that killed run 8c75990e — the figure was 2677x1554 and legal on
    its own, illegal only because of how many images rode with it.
    """
    figures = [
        _figure(_png(tmp_path / f"f{i}.png", (2677, 1554))) for i in range(count)
    ]
    plan = plan_media(iter_media([_turn_with(figures)]), _ANTHROPIC_LIKE)

    resolved = plan.resolve(figures[0])
    assert not resolved.dropped
    width, height = _dimensions(resolved.data)
    if expect_resized:
        assert max(width, height) == 2000
        assert (width, height) == (2000, 1161)  # aspect ratio preserved
        assert "downscaled" in (resolved.note or "")
    else:
        assert (width, height) == (2677, 1554)
        assert resolved.note is None


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

    blocks = [
        block
        for message in _CaptureAnthropic.store["request"]["messages"]
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if isinstance(block, dict) and block.get("type") == "image"
    ]
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
    safe if it is not systematically low. Rounded up: under-reserving costs the whole request,
    over-reserving withholds one figure with a note."""
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
