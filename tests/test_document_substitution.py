"""A vendor with no usable file channel receives a document as text plus figure crops (A2).

Anthropic and OpenAI both ingest a PDF as text *and* a rendered image per page, per their own
docs. Alibaba publishes no route we can use — `input_file` takes a public `file_url` only, and PDF
input is restricted to `qwen3.5-ocr` — so the same two channels are assembled locally for that
seat. These tests pin the properties that make the substitution honest rather than merely present:

* every other vendor's wire bytes are untouched (OpenAI shares this adapter, and a science-sweep
  slot has already run on it — a change reaching its request would cost that block);
* the substituted figures pass through the **media plan**, so they are counted, budgeted and
  evicted like any other image rather than bypassing the guard that exists because run `8c75990e`
  died on an unbudgeted image request;
* the substituted text counts in the request's non-media **reserve**, since it is request weight;
* a parse failure names the document unread, so it reads as unread rather than empty.

The substitution is the model-input sanitiser's (`models.sanitise`, inside `ModelHandle.call`), so
every case drives `ModelHandle.call` over the real adapter with the vendor's client faked. Docling
itself is not exercised here — the parse is stubbed, so these run without the dependency and
without a 40-second conversion. The live end-to-end parse has its own gated test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_research.models.providers import _doc_parse, _responses
from auto_research.models.providers._doc_parse import ParsedDocument
from auto_research.models.types import ModelHandle, ModelMessage, RoleModelConfig
from auto_research.schemas import DocumentRef, FigureRef
from tests.media_files import pdf_bytes


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
    path.write_bytes(pdf_bytes("pretend"))
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


def _call(provider_cls, provider_name, messages, monkeypatch, **kwargs) -> None:
    """One `ModelHandle.call` over the real adapter, the vendor's client faked."""
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", _CaptureResponses)
    cfg = RoleModelConfig(name="m", provider=provider_name, model="fake")
    handle = ModelHandle(role="reviewer", config=cfg, provider=provider_cls())
    asyncio.run(handle.call(messages, **kwargs))


def _parts(provider_cls, provider_name, document, monkeypatch) -> list[dict]:
    """Drive one document through `ModelHandle.call` and return the content parts emitted."""
    _call(provider_cls, provider_name, [ModelMessage(role="user", content="review this")],
          monkeypatch, documents=[document])
    return [
        part
        for item in _CaptureResponses.store["request"]["input"]
        for part in (item.get("content") or [] if isinstance(item, dict) else [])
        if isinstance(part, dict)
    ]


def _stub_parse(monkeypatch, parsed: ParsedDocument | None):
    monkeypatch.setattr(_doc_parse, "parse_document", lambda document: parsed)


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


def test_the_vendors_without_a_documented_file_channel_decline_inline_documents():
    """Each declaration cites its vendor's reference: Alibaba takes `file_url` only, DeepSeek
    states file input is unsupported, Z.AI documents no route over this dialect (A1/A2)."""
    from auto_research.models.providers.dashscope import DASHSCOPE_VENDOR
    from auto_research.models.providers.deepseek import DEEPSEEK_VENDOR
    from auto_research.models.providers.openai import OPENAI_VENDOR
    from auto_research.models.providers.zai import ZAI_VENDOR

    for vendor in (DASHSCOPE_VENDOR, DEEPSEEK_VENDOR, ZAI_VENDOR):
        assert vendor.accepts_inline_documents is False, vendor.name
    assert OPENAI_VENDOR.accepts_inline_documents is True


# ── the substitution stays inside the guards ──────────────────────────────────────


def test_substituted_figures_pass_through_the_media_plan(tmp_path, monkeypatch):
    """A crop that the plan refuses is withheld with a note instead of being sent.

    This is the property worth pinning: the sanitiser adds the crops to the request *before* the
    adapter plans it, so an image that bypasses the plan cannot exist. A figure carrying a mime this vendor does not accept is
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
    assert "treat it as unread" in text
    assert not [p for p in parts if p.get("type") in {"input_file", "input_image"}]


def test_a_document_with_no_recoverable_figures_says_so(tmp_path, monkeypatch):
    """Silence about missing figures invites the model to reason about them anyway."""
    from auto_research.models.providers.dashscope import DashscopeProvider

    document = _document(tmp_path / "paper.pdf")
    _stub_parse(monkeypatch, ParsedDocument(text="body", figures=(), truncated=False))

    parts = _parts(DashscopeProvider, "dashscope", document, monkeypatch)
    text = "\n".join(p.get("text") or "" for p in parts)

    assert "It yielded no figure crops" in text


# ── R2: a document nested in the tool result that opened it ───────────────────────


def _opened(document: DocumentRef) -> list:
    from auto_research.models.types import ModelResponse, ToolCall, ToolResultMessage, ToolResultPart

    return [
        ModelMessage(role="user", content="read the paper"),
        ModelResponse(tool_calls=[ToolCall(id="c1", name="read_paper", arguments={"hit": 1})]),
        ToolResultMessage(results=[ToolResultPart(
            tool_call_id="c1", name="read_paper", content="the PDF is attached",
            documents=[document],
        )]),
    ]


def _request(provider_cls, provider_name, messages, monkeypatch) -> list:
    _call(provider_cls, provider_name, messages, monkeypatch)
    return _CaptureResponses.store["request"]["input"]


def test_a_nested_document_follows_its_result_on_openai(tmp_path, monkeypatch):
    from auto_research.models.providers.openai import OpenAIProvider

    document = _document(tmp_path / "paper.pdf")
    items = _request(OpenAIProvider, "openai", _opened(document), monkeypatch)
    kinds = [i.get("type") or i.get("role") for i in items]
    at = kinds.index("function_call_output")
    after = items[at + 1]
    assert after["role"] == "user" and [p["type"] for p in after["content"]] == ["input_file"]
    assert at + 2 == len(items), "nothing re-attached at the end of the request"


def test_alibaba_reads_a_nested_document_through_the_substitution(tmp_path, monkeypatch):
    """A nested document is read as extracted text at the result that opened it (A2: the R2
    residual of re-sending it at the end of every call is gone), never sent as a file."""
    from auto_research.models.providers.dashscope import DashscopeProvider

    document = _document(tmp_path / "paper.pdf")
    _stub_parse(monkeypatch, ParsedDocument(text="PD = 4.01 %", figures=(), truncated=False))
    items = _request(DashscopeProvider, "dashscope", _opened(document), monkeypatch)
    parts = [p for i in items if isinstance(i, dict) for p in (i.get("content") or [])
             if isinstance(p, dict)]
    assert not [p for p in parts if p.get("type") == "input_file"]
    (output,) = [i for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert "PD = 4.01 %" in json.dumps(output), "the parse sits at the result that opened it"
