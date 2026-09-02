"""Multimodal request guards — mechanical half, shared by the provider adapters.

**Why this exists.** Live run `8c75990e` died at $116.10, after all five of its tools had
committed, on ``400 … image dimensions exceed max allowed size for many-image requests: 2000
pixels``. Anthropic applies a stricter per-image dimension cap once a request carries more than 20
image blocks; a worker's plotter returned 3 PNGs per call, each replays inline at its tool-result
turn on every later request, the count crossed 20, and one figure was 2677x1554. An audit then
found **no guard of any kind** in any adapter: we base64'd whatever we were handed, checking
neither count, dimensions, bytes, total payload, nor mime.

**Why the adapter is the right home.** We are stateless: an adapter receives the whole conversation
plus this call's ``images=``/``documents=`` before it sends anything. It is the only place that can
see *all* media in one request, which is exactly what volume limits are defined over. Each provider
publishes different limits, so the *policy* (a :class:`MediaLimits` instance) lives in each adapter
beside its citations; only the mechanics live here.

**What a breach does.** An image is resized to fit; a document, which cannot be resized, is dropped.
Either way the block is replaced by a model-visible note naming the file and the reason — the [F4]
"malformed figure dropped with a note" precedent. A resize touches only the *transmitted* copy: the
artifact on disk and its ``FigureRef.sha256`` are untouched, so a human still reproduces the figure
the provenance chain records.

**What it deliberately does not do.** It does not downscale unless a limit would otherwise be
breached. Providers already resize server-side to their processing tier and bill the smaller
version, so client-side downscaling saves no tokens; it would only shrink the upload, and would
lose real fidelity on a hi-res-tier model for nothing.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from auto_research.schemas import DocumentRef, FigureRef

from ..types import ConversationMessage, ModelMessage, ToolResultMessage

_MB = 1024 * 1024

# Flat per-part allowance for the JSON envelope around one block (keys, quoting, commas,
# role wrappers). Rounded generously: the reserve exists to be safe, not exact.
_PART_OVERHEAD = 256


@dataclass(frozen=True)
class MediaLimits:
    """One provider's documented media limits. Every field is cited where it is instantiated.

    ``0``/``None`` means "not documented for this provider" and disables that check — we enforce
    what a provider publishes and no more, rather than inventing a rule from a 400 we never saw.
    """

    image_mimes: frozenset[str]
    doc_mimes: frozenset[str] = frozenset()
    images_accepted: bool = True
    """Whether *this model* accepts image input at all. Unlike the fields below, this is not a
    volume limit but a per-model capability: a vendor may serve vision on only part of its
    lineup, and an image sent to the wrong model is refused outright rather than ignored. An
    adapter narrows its vendor limits per call (``ResponsesVendor.limits_for``)."""
    max_images: int = 0
    """Image blocks per request (0 = undocumented)."""
    strict_block_threshold: int = 0
    """Image+document blocks above which ``strict_max_edge`` applies (0 = no such rule)."""
    strict_max_edge: int = 0
    max_edge: int = 0
    """Long-edge cap that applies regardless of block count (0 = undocumented)."""
    max_image_bytes: int = 0
    max_doc_bytes: int = 0
    max_request_bytes: int = 0
    max_pdf_pages: int = 0


@dataclass
class MediaItem:
    """One figure or document the request will render, in emission order."""

    ref: FigureRef | DocumentRef
    is_image: bool
    path: Path
    mime: str


@dataclass
class Resolved:
    """What the adapter should emit for one item."""

    data: bytes | None
    mime: str
    note: str | None = None
    """Model-visible explanation, set whenever the item was altered or dropped."""

    @property
    def dropped(self) -> bool:
        return self.data is None

    def b64(self) -> str:
        return base64.b64encode(self.data or b"").decode("ascii")


@dataclass
class MediaPlan:
    """Per-item decisions for one request, keyed by the ref's identity.

    Keyed on ``id(ref)`` rather than ``sha256``: the same figure can legitimately appear twice in
    one request (cited by two workers), and each occurrence is its own block for counting.
    """

    _by_ref: dict[int, Resolved] = field(default_factory=dict)

    def resolve(self, ref: FigureRef | DocumentRef) -> Resolved:
        """The decision for ``ref``; falls back to reading the file when unplanned.

        The fallback keeps a caller that renders an unplanned ref (a test fake, a path the walk
        does not know) working exactly as it did before this module existed.
        """
        found = self._by_ref.get(id(ref))
        if found is not None:
            return found
        return Resolved(data=Path(ref.path).read_bytes(), mime=ref.mime_type)


def iter_media(
    messages: Sequence[ConversationMessage],
    images: Sequence[FigureRef] = (),
    documents: Sequence[DocumentRef] = (),
) -> list[MediaItem]:
    """Every figure/document this request will render, oldest first.

    Order mirrors what the translators emit — tool-result figures inline through the conversation,
    then call-time ``images=``, then ``documents=`` — so "drop the oldest" drops the least relevant
    evidence, and the count is the count the provider will see.
    """
    items: list[MediaItem] = []
    for message in messages:
        if isinstance(message, ToolResultMessage):
            for part in message.results:
                for figure in part.figures:
                    items.append(
                        MediaItem(figure, True, Path(figure.path), figure.mime_type)
                    )
    for figure in images:
        items.append(MediaItem(figure, True, Path(figure.path), figure.mime_type))
    for document in documents:
        items.append(MediaItem(document, False, Path(document.path), document.mime_type))
    return items


def plan_media(
    items: Sequence[MediaItem], limits: MediaLimits, reserved_bytes: int = 0
) -> MediaPlan:
    """Decide, for the request as a whole, what to send, shrink, or drop.

    Order of application: mime routing, then the per-item caps (pages, bytes, dimensions — the
    dimension cap tightens once the block count crosses the provider's threshold), then the
    whole-request payload budget, which evicts oldest-first until the request fits alongside
    ``reserved_bytes`` of non-media content (see :func:`_apply_request_budget`).
    """
    plan = MediaPlan()
    kept: list[tuple[MediaItem, Resolved]] = []

    block_count = len(items)
    max_edge = limits.max_edge
    if limits.strict_block_threshold and block_count > limits.strict_block_threshold:
        # Anthropic: past this many image+document blocks, every image must fit the stricter cap.
        max_edge = min(max_edge or limits.strict_max_edge, limits.strict_max_edge)

    images_seen = 0
    for item in items:
        if item.is_image:
            images_seen += 1
        resolved = _resolve_one(item, limits, max_edge, images_seen)
        plan._by_ref[id(item.ref)] = resolved
        if not resolved.dropped:
            kept.append((item, resolved))

    _apply_request_budget(kept, limits, reserved_bytes)
    return plan


def _resolve_one(
    item: MediaItem, limits: MediaLimits, max_edge: int, image_index: int
) -> Resolved:
    """Per-item checks: mime, count, pages, bytes, dimensions."""
    name = item.path.name
    if item.is_image:
        if not limits.images_accepted:
            # Some vendors serve vision on only part of their lineup, and sending an image to
            # the wrong model is a 400 rather than a degraded answer (DeepSeek: "Only vision
            # models accept images; other models return a 400 error"). Withholding here keeps
            # that a model-visible note instead of a dead request.
            return Resolved(
                None,
                item.mime,
                f"[withheld] {name}: this model does not accept image input.",
            )
        if limits.image_mimes and item.mime not in limits.image_mimes:
            # Closes [V-3]: an unsupported mime must never enter the image channel, where every
            # provider 400s on it.
            return Resolved(
                None,
                item.mime,
                f"[withheld] {name}: {item.mime} is not an image type this provider accepts "
                f"({', '.join(sorted(limits.image_mimes))}).",
            )
        if limits.max_images and image_index > limits.max_images:
            return Resolved(
                None,
                item.mime,
                f"[withheld] {name}: this request already carries the provider's maximum of "
                f"{limits.max_images} images.",
            )
        return _resolve_image(item, limits, max_edge)

    if limits.doc_mimes and item.mime not in limits.doc_mimes:
        return Resolved(
            None,
            item.mime,
            f"[withheld] {name}: {item.mime} is not a document type this provider accepts.",
        )
    return _resolve_document(item, limits)


def _resolve_image(item: MediaItem, limits: MediaLimits, max_edge: int) -> Resolved:
    try:
        data = item.path.read_bytes()
    except OSError as exc:
        return Resolved(None, item.mime, f"[withheld] {item.path.name}: unreadable ({exc}).")

    width, height = _dimensions(data)
    too_wide = bool(max_edge) and max(width, height) > max_edge
    too_big = bool(limits.max_image_bytes) and len(data) > limits.max_image_bytes
    if not (too_wide or too_big):
        return Resolved(data, item.mime)

    target = max_edge or max(width, height)
    shrunk, new_size = _resize(data, item.mime, target)
    if shrunk is None:
        return Resolved(
            None,
            item.mime,
            f"[withheld] {item.path.name}: {width}x{height} exceeds this provider's limit and "
            "could not be resized.",
        )
    if limits.max_image_bytes and len(shrunk) > limits.max_image_bytes:
        return Resolved(
            None,
            item.mime,
            f"[withheld] {item.path.name}: {len(shrunk) / _MB:.1f} MB after resizing, over the "
            f"provider's {limits.max_image_bytes / _MB:.0f} MB per-image limit.",
        )
    return Resolved(
        shrunk,
        item.mime,
        f"[downscaled] {item.path.name}: {width}x{height} -> {new_size[0]}x{new_size[1]} to fit "
        "this provider's limit for a request carrying this many images. The stored artifact is "
        "unchanged.",
    )


def _resolve_document(item: MediaItem, limits: MediaLimits) -> Resolved:
    try:
        data = item.path.read_bytes()
    except OSError as exc:
        return Resolved(None, item.mime, f"[withheld] {item.path.name}: unreadable ({exc}).")

    if limits.max_doc_bytes and len(data) > limits.max_doc_bytes:
        return Resolved(
            None,
            item.mime,
            f"[withheld] {item.path.name}: {len(data) / _MB:.1f} MB, over this provider's "
            f"{limits.max_doc_bytes / _MB:.0f} MB per-document limit. Cite it from its "
            "text_search metadata instead of attaching it.",
        )
    if limits.max_pdf_pages and item.mime == "application/pdf":
        pages = _pdf_page_count(data)
        if pages and pages > limits.max_pdf_pages:
            return Resolved(
                None,
                item.mime,
                f"[withheld] {item.path.name}: {pages} pages, over this provider's "
                f"{limits.max_pdf_pages}-page limit. Cite it from its text_search metadata "
                "instead of attaching it.",
            )
    return Resolved(data, item.mime)


def _apply_request_budget(
    kept: list[tuple[MediaItem, Resolved]], limits: MediaLimits, reserved: int = 0
) -> None:
    """Evict oldest-first, in place, until the encoded payload fits the request ceiling.

    Base64 inflates by 4/3, and that inflated form is what the request carries, so the budget is
    measured on the encoded size. The current turn's evidence is the most relevant, so the oldest
    media is what goes.

    ``reserved`` is the weight of everything in the request that is **not** media — the system
    prompt, the tool definitions, the whole conversation's text, the JSON scaffolding — so the
    media budget is what is left after that, never the whole ceiling. Anthropic states the rule
    for both of its document limits: *"Both limits are on the entire request payload, including
    any other content sent alongside PDFs"*
    (platform.claude.com/docs/en/build-with-claude/pdf-support). Budgeting media against the
    full ceiling therefore admits a request the provider refuses: measured on the 413 that killed
    run ``fbb93f26`` at its final node, two PDFs came to 33,512,296 encoded bytes against a
    33,554,432 ceiling — the guard stopped with **42 KB** of headroom for a conversation orders
    of magnitude larger, and no media-guard event fired.
    """
    if not limits.max_request_bytes:
        return
    ceiling = max(0, limits.max_request_bytes - max(0, reserved))
    encoded = [(item, res, len(res.data or b"") * 4 // 3) for item, res in kept]
    total = sum(size for _, _, size in encoded)
    for item, res, size in encoded:
        if total <= ceiling:
            return
        res.note = (
            f"[withheld] {item.path.name}: {size / _MB:.1f} MB does not fit the "
            f"{ceiling / _MB:.1f} MB left for media under the provider's "
            f"{limits.max_request_bytes / _MB:.0f} MB request ceiling once the rest of this "
            "request is counted; the most recent media was kept."
        )
        res.data = None
        total -= size


def _dimensions(data: bytes) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:  # noqa: BLE001 — an unreadable image is handled by the size checks
        return (0, 0)


def _resize(data: bytes, mime: str, max_edge: int) -> tuple[bytes | None, tuple[int, int]]:
    """Scale the long edge down to ``max_edge``, preserving aspect ratio.

    LANCZOS because the content is charts: axis labels and thin error bars have to stay legible.
    """
    from PIL import Image

    fmt = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            width, height = image.size
            if max(width, height) <= max_edge:
                return data, (width, height)
            scale = max_edge / max(width, height)
            size = (max(1, int(width * scale)), max(1, int(height * scale)))
            out = io.BytesIO()
            image.resize(size, Image.LANCZOS).save(out, format=fmt.get(mime, "PNG"))
            return out.getvalue(), size
    except Exception:  # noqa: BLE001 — caller turns a failed resize into a withheld note
        return None, (0, 0)


def _pdf_page_count(data: bytes) -> int:
    """Page count, or 0 when the file cannot be parsed (the byte checks still apply)."""
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001
        return 0


def caption_with_note(caption: str, resolved: Resolved) -> str:
    """The text block that accompanies (or replaces) a media block."""
    return f"{caption}\n{resolved.note}" if resolved.note else caption


def nonmedia_weight(
    messages: Sequence[ConversationMessage],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    extra: Sequence[str] = (),
) -> int:
    """Bytes this request will carry that are **not** media, for the payload reserve.

    What each adapter passes as ``reserved_bytes`` to :func:`plan_for_call`. The whole-request
    ceiling covers everything, so the media budget is the ceiling minus this; without it the
    guard fills the ceiling and the conversation pushes the request over it (the 413 in
    :func:`_apply_request_budget`).

    Deliberately an estimate over the **neutral** conversation rather than a provider's assembled
    payload: the payload cannot be built until the media plan exists, so an exact figure would be
    circular. It counts what dominates — message text, tool-call arguments, tool-result text,
    every verbatim provider part, and the declared tool schemas — with a flat per-part allowance
    for JSON scaffolding, and rounds **up** wherever it is unsure, because under-reserving is the
    failure mode that costs a request and over-reserving only withholds a figure with a note.
    """
    total = 0
    for message in messages:
        if isinstance(message, ModelMessage):
            total += len(message.content.encode("utf-8")) + _PART_OVERHEAD
        elif isinstance(message, ToolResultMessage):
            for part in message.results:
                total += len(part.content.encode("utf-8")) + len(part.tool_call_id)
                total += len(part.name) + _PART_OVERHEAD
        else:  # ModelResponse — the provider's own turn, replayed verbatim
            total += len(message.content.encode("utf-8")) + _PART_OVERHEAD
            for call in message.tool_calls:
                total += _json_bytes(call.arguments) + _json_bytes(call.provider_fields)
                total += len(call.name) + len(call.id) + _PART_OVERHEAD
            for part in message.provider_parts:
                total += _json_bytes(part) + _PART_OVERHEAD
    for tool in tools or ():
        total += _json_bytes(tool) + _PART_OVERHEAD
    for text in extra:
        total += len(text.encode("utf-8")) + _PART_OVERHEAD
    return total


def _json_bytes(value: Any) -> int:
    """Serialized size of one JSON-ish payload; an unserializable value falls back to ``repr``."""
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(repr(value).encode("utf-8"))


def plan_for_call(
    messages: Sequence[ConversationMessage],
    images: Sequence[FigureRef],
    documents: Sequence[DocumentRef],
    limits: MediaLimits,
    reserved_bytes: int = 0,
) -> MediaPlan:
    """One call: walk the whole request, then decide. What each adapter invokes.

    ``reserved_bytes`` is the non-media weight of this same request
    (:func:`nonmedia_weight`); the media budget is the provider's ceiling minus it.
    """
    return plan_media(iter_media(messages, images, documents), limits, reserved_bytes)


__all__ = [
    "MediaItem",
    "MediaLimits",
    "MediaPlan",
    "Resolved",
    "caption_with_note",
    "iter_media",
    "nonmedia_weight",
    "plan_for_call",
    "plan_media",
]
