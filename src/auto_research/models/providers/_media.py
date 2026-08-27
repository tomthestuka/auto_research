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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from auto_research.schemas import DocumentRef, FigureRef

from ..types import ConversationMessage, ToolResultMessage

_MB = 1024 * 1024


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


def plan_media(items: Sequence[MediaItem], limits: MediaLimits) -> MediaPlan:
    """Decide, for the request as a whole, what to send, shrink, or drop.

    Order of application: mime routing, then the per-item caps (pages, bytes, dimensions — the
    dimension cap tightens once the block count crosses the provider's threshold), then the
    whole-request payload budget, which evicts oldest-first until the request fits.
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

    _apply_request_budget(kept, limits)
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
    kept: list[tuple[MediaItem, Resolved]], limits: MediaLimits
) -> None:
    """Evict oldest-first, in place, until the encoded payload fits the request ceiling.

    Base64 inflates by 4/3, and that inflated form is what the request carries, so the budget is
    measured on the encoded size. The current turn's evidence is the most relevant, so the oldest
    media is what goes.
    """
    if not limits.max_request_bytes:
        return
    encoded = [(item, res, len(res.data or b"") * 4 // 3) for item, res in kept]
    total = sum(size for _, _, size in encoded)
    for item, res, size in encoded:
        if total <= limits.max_request_bytes:
            return
        res.note = (
            f"[withheld] {item.path.name}: {size / _MB:.1f} MB would put this request over the "
            f"provider's {limits.max_request_bytes / _MB:.0f} MB ceiling; the most recent media "
            "was kept."
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


def plan_for_call(
    messages: Sequence[ConversationMessage],
    images: Sequence[FigureRef],
    documents: Sequence[DocumentRef],
    limits: MediaLimits,
) -> MediaPlan:
    """One call: walk the whole request, then decide. What each adapter invokes."""
    return plan_media(iter_media(messages, images, documents), limits)


__all__ = [
    "MediaItem",
    "MediaLimits",
    "MediaPlan",
    "Resolved",
    "caption_with_note",
    "iter_media",
    "plan_for_call",
    "plan_media",
]
