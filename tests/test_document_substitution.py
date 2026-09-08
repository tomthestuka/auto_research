"""A vendor with no usable file channel receives a document as text plus figure crops.

Anthropic and OpenAI both ingest a PDF as text *and* a rendered image per page, per their own
docs. Alibaba publishes no route we can use — `input_file` takes a public `file_url` only, and PDF
input is restricted to `qwen3.5-ocr` — so the same two channels are assembled locally for that
seat. These tests pin the properties that make the substitution honest rather than merely present:

* every other vendor's wire bytes are untouched (OpenAI shares this adapter, and a science-sweep
  slot has already run on it — a change reaching its request would cost that block);
* the substituted figures pass through the **media plan**, so they are counted, budgeted and
  evicted like any other image rather than bypassing the guard that exists because run `8c75990e`
  died on an unbudgeted image request;
* the substituted text is added to the request's non-media **reserve**, since it is request weight
  the plan cannot see;
* a parse failure says the document is UNREAD, never lets it look empty.

Docling itself is not exercised here — the parse is stubbed, so these run without the dependency
and without a 40-second conversion. The live end-to-end parse has its own gated test.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_research.models.providers import _responses
from auto_research.models.providers._doc_parse import ParsedDocument
from auto_research.models.types import ModelMessage, RoleModelConfig
from auto_research.schemas import DocumentRef, FigureRef


def _png(path: Path, size=(40, 30)) -> Path:
    Image = pytest.importorskip("PIL.Image", reason="pillow is a hard dependency")
    Image.new("RGB", size, (10, 120, 200)).save(path, "PNG")
    return path


def _figure(path: Path, caption: str = "Fig. 1. Polarization vs energy.") -> FigureRef:
    return FigureRef(
        path=path,
        caption=caption,
        mime_type="image/png",
        produced_by="docling:paper.pdf",
        created_at=datetime.now(UTC),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _document(path: Path) -> DocumentRef:
    path.write_bytes(b"%PDF-1.4 pretend")
    return DocumentRef(
        path=path,
        title="Krawczynski et al. 2022",
        mime_type="application/pdf",
        created_at=datetime.now(UTC),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


class _CaptureResponses:
    """Records the request a Responses-dialect vendor would have received."""

    store: dict = {}

    def __init__(self, *a, **k):
        pass

    @property
    def responses(self):
        return self

    async def create(self, **request):
        _CaptureResponses.store["request"] = request
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    content=[SimpleNamespace(type="output_text", text="ok")],
                )
            ],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def _parts(provider_cls, provider_name, document, monkeypatch) -> list[dict]:
    """Drive one document through a real adapter and return the content parts it emitted."""
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _CaptureResponses)
    cfg = RoleModelConfig(
        name="m", provider=provider_name, model="fake", vision=True, documents=True
    )
    asyncio.run(
        provider_cls().call(
            [ModelMessage(role="user", content="review this")], cfg, documents=[document]
        )
    )
    return [
        part
        for item in _CaptureResponses.store["request"]["input"]
        for part in (item.get("content") or [] if isinstance(item, dict) else [])
        if isinstance(part, dict)
    ]


def _stub_parse(monkeypatch, parsed: ParsedDocument | None):
    monkeypatch.setattr(_responses, "parse_document", lambda document: parsed)


# ── the fork ──────────────────────────────────────────────────────────────────────


def test_alibaba_gets_text_and_figures_where_openai_gets_the_pdf(tmp_path, monkeypatch):
    """The claim is not that a helper works — it is that Alibaba's reviewer stops dying on a
    paper while OpenAI's request does not move."""
    from auto_research.models.providers.dashscope import DashscopeProvider
    from auto_research.models.providers.openai import OpenAIProvider

    document = _document(tmp_path / "paper.pdf")
    figure = _figure(_png(tmp_path / "fig1.png"))
    _stub_parse(
        monkeypatch,
        ParsedDocument(text="PD = 4.01 +/- 0.20 %", figures=(figure,), truncated=False),
    )

    alibaba = _parts(DashscopeProvider, "dashscope", document, monkeypatch)
    assert not [p for p in alibaba if p.get("type") == "input_file"]
    text = "\n".join(p.get("text") or "" for p in alibaba)
    assert "extracted document" in text and "PD = 4.01 +/- 0.20 %" in text
    assert len([p for p in alibaba if p.get("type") == "input_image"]) == 1

    served = _parts(OpenAIProvider, "openai", document, monkeypatch)
    files = [p for p in served if p.get("type") == "input_file"]
    assert len(files) == 1, "OpenAI still receives the PDF itself"
    assert files[0]["file_data"].startswith("data:application/pdf;base64,")
    assert not [p for p in served if "extracted document" in (p.get("text") or "")]


def test_only_alibaba_declines_inline_documents():
    """The declaration is the whole blast radius. Every other vendor keeps today's behaviour,
    because a behaviour change on a vendor whose reference we have not read is a guess."""
    from auto_research.models.providers.dashscope import DASHSCOPE_VENDOR
    from auto_research.models.providers.deepseek import DEEPSEEK_VENDOR
    from auto_research.models.providers.openai import OPENAI_VENDOR
    from auto_research.models.providers.zai import ZAI_VENDOR

    assert DASHSCOPE_VENDOR.accepts_inline_documents is False
    for vendor in (OPENAI_VENDOR, DEEPSEEK_VENDOR, ZAI_VENDOR):
        assert vendor.accepts_inline_documents is True, vendor.name


# ── the substitution stays inside the guards ──────────────────────────────────────


def test_substituted_figures_pass_through_the_media_plan(tmp_path, monkeypatch):
    """A crop that the plan refuses is withheld with a note instead of being sent.

    This is the property worth pinning: the figures are added *before* planning, so an image
    that bypasses the plan cannot exist. A figure carrying a mime this vendor does not accept is
    the cheapest way to prove the plan actually ran on them.
    """
    from auto_research.models.providers.dashscope import DashscopeProvider

    document = _document(tmp_path / "paper.pdf")
    odd = _png(tmp_path / "fig1.tiff")
    figure = _figure(odd)
    figure = figure.model_copy(update={"mime_type": "image/tiff"})
    _stub_parse(monkeypatch, ParsedDocument(text="body", figures=(figure,), truncated=False))

    parts = _parts(DashscopeProvider, "dashscope", document, monkeypatch)

    assert not [p for p in parts if p.get("type") == "input_image"]
    assert any("not an image type" in (p.get("text") or "") for p in parts)


def test_substituted_text_is_added_to_the_non_media_reserve(tmp_path, monkeypatch):
    """Extracted text is request weight the media plan cannot see. Leaving it out of the reserve
    is how a request passes the guard and is then refused by the provider."""
    from auto_research.models.providers.dashscope import DashscopeProvider

    document = _document(tmp_path / "paper.pdf")
    body = "x" * 50_000
    _stub_parse(monkeypatch, ParsedDocument(text=body, figures=(), truncated=False))

    seen: dict[str, int] = {}
    real_plan = _responses.plan_for_call

    def spy(messages, images, documents, limits, *, reserved_bytes=0):
        seen["reserved"] = reserved_bytes
        return real_plan(messages, images, documents, limits, reserved_bytes=reserved_bytes)

    monkeypatch.setattr(_responses, "plan_for_call", spy)
    _parts(DashscopeProvider, "dashscope", document, monkeypatch)

    assert seen["reserved"] >= len(body), "the extracted text must count against the ceiling"


# ── failure is never allowed to look like emptiness ───────────────────────────────


def test_a_failed_parse_reports_the_document_unread_not_empty(tmp_path, monkeypatch):
    from auto_research.models.providers.dashscope import DashscopeProvider

    document = _document(tmp_path / "paper.pdf")
    _stub_parse(monkeypatch, None)

    parts = _parts(DashscopeProvider, "dashscope", document, monkeypatch)
    text = "\n".join(p.get("text") or "" for p in parts)

    assert "unavailable" in text
    assert "UNREAD" in text
    assert not [p for p in parts if p.get("type") in {"input_file", "input_image"}]


def test_a_document_with_no_recoverable_figures_says_so(tmp_path, monkeypatch):
    """Silence about missing figures invites the model to reason about them anyway."""
    from auto_research.models.providers.dashscope import DashscopeProvider

    document = _document(tmp_path / "paper.pdf")
    _stub_parse(monkeypatch, ParsedDocument(text="body", figures=(), truncated=False))

    parts = _parts(DashscopeProvider, "dashscope", document, monkeypatch)
    text = "\n".join(p.get("text") or "" for p in parts)

    assert "No figures could be recovered" in text
