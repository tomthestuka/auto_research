"""The model-input half of the input validator (CLAUDE.md §9.3.9; plan item A2, ruling 2026-11-04).

Every request a member sends passes through :func:`sanitise` inside ``ModelHandle.call``, over the
whole normalised request (task figures, peer figures, the history seed's figures, every tool
result's figures and documents, and the call-time ``images=`` / ``documents=``), before any
adapter sees it. Each attachment is judged against the member's capability, its model's entry in
``config/model_capabilities.yaml`` (``models.providers.media_capability``, Z1d):

- a medium the member takes natively passes untouched;
- a document for a member with no native document channel becomes the local parse
  (``_doc_parse.parse_document``): its text, plus its figure crops when the member takes images;
- a medium the member cannot take and that has no patch (an image for a text-only member, a
  document the parser cannot read) is withheld, replaced by a note the model reads naming it and
  the peers who can view it.

The adapter keeps only the volume limits (count, bytes, eviction), which only it sees whole.

**Same input, same output.** Notes are fixed text, a parse is read from its content-keyed cache,
and an untouched message is passed on as the same object, so a replayed turn produces a
byte-identical request and the provider's prompt cache holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from auto_research.schemas import DocumentRef, FigureRef

from .types import ConversationMessage, ModelMessage, ToolResultMessage

if TYPE_CHECKING:
    from .providers import MediaCapability


def sanitise(
    messages: Sequence[ConversationMessage],
    images: Sequence[FigureRef],
    documents: Sequence[DocumentRef],
    capability: MediaCapability,
    *,
    viewers: Sequence[str] = (),
) -> tuple[list[ConversationMessage], Sequence[FigureRef], Sequence[DocumentRef]]:
    """``(messages, images, documents)`` as this member can receive them (unchanged ones as given).

    ``viewers`` names the peers whose members take images; a withheld medium's note asks them.
    Call-time media the member cannot take natively leave the call-time channel and arrive as one
    trailing user message carrying the parsed text, the crops and the notes; what stays on that
    channel is unchanged.
    """
    rewrite = _Rewrite(capability, tuple(viewers))
    out: list[ConversationMessage] = []
    for message in messages:
        if isinstance(message, ModelMessage) and (message.figures or message.documents):
            message = rewrite.carrier(message)
        elif isinstance(message, ToolResultMessage) and any(
            p.figures or p.documents for p in message.results
        ):
            parts = [rewrite.carrier(part) for part in message.results]
            if any(new is not old for new, old in zip(parts, message.results)):
                message = message.model_copy(update={"results": parts})
        out.append(message)
    kept_images, notes = rewrite.figures(images)
    kept_documents, crops, document_notes = rewrite.documents(documents)
    notes += document_notes
    if notes:
        out.append(ModelMessage(role="user", content="\n\n".join(notes), figures=crops))
    return out, kept_images, kept_documents


class _Rewrite:
    def __init__(self, capability: MediaCapability, viewers: tuple[str, ...]):
        self.capability = capability
        self.viewers = viewers

    def carrier(self, carrier):
        """A message or tool-result part, rewritten only when one of its media changes."""
        figures, notes = self.figures(carrier.figures)
        documents, crops, document_notes = self.documents(carrier.documents)
        notes += document_notes
        if not notes:
            return carrier
        content = "\n\n".join([carrier.content, *notes]) if carrier.content else "\n\n".join(notes)
        return carrier.model_copy(
            update={"content": content, "figures": [*figures, *crops], "documents": list(documents)}
        )

    def figures(self, figures: Sequence[FigureRef]) -> tuple[Sequence[FigureRef], list[str]]:
        if self.capability.images:
            return figures, []
        return [], [self._withheld(f"the figure {_figure_name(f)}") for f in figures]

    def documents(
        self, documents: Sequence[DocumentRef]
    ) -> tuple[Sequence[DocumentRef], list[FigureRef], list[str]]:
        """``(documents kept native, crops to attach, notes with each parsed text)``."""
        if self.capability.native_documents:
            return documents, [], []
        from .providers._doc_parse import parse_document

        crops: list[FigureRef] = []
        notes: list[str] = []
        for document in documents:
            parsed = parse_document(document)
            if parsed is None:
                notes.append(
                    f"[unavailable] {_document_name(document)} could not be converted to text "
                    "here, so treat it as unread and cite it from its search metadata."
                    + self._ask(f"to read {_document_name(document)}")
                )
                continue
            with_crops = self.capability.images and bool(parsed.figures)
            if with_crops:
                crops.extend(parsed.figures)
            notes.append(self._extracted(document, len(parsed.figures), parsed.truncated,
                                         with_crops=with_crops) + "\n\n" + parsed.text)
        return [], crops, notes

    def _ask(self, what: str) -> str:
        """Who to ask, when the caller named peers that read images; else nothing to add."""
        if self.viewers:
            return (f" Ask {', '.join(self.viewers)} (message; list_peers names each peer's model) "
                    f"{what}.")
        return ""

    def _withheld(self, what: str) -> str:
        return (f"[withheld] Your model reads text, so {what} is named here rather than shown."
                + self._ask("to look at it (view_receipt) and describe it"))

    def _extracted(self, document: DocumentRef, figures: int, truncated: bool, *,
                   with_crops: bool) -> str:
        name = _document_name(document)
        if not figures:
            figure_line = "It yielded no figure crops, so read its figures from the text alone."
        elif with_crops:
            figure_line = f"Its {figures} figure(s) are attached as images, in document order."
        else:
            figure_line = (f"Your model reads text, so its {figures} figure(s) are described by the "
                           "text only." + self._ask(f"to look at the figures of {name}"))
        truncation = " The text was truncated; a marker names where." if truncated else ""
        return (f"[extracted document] {name}: its text was extracted locally and is reproduced "
                f"below. {figure_line}{truncation}")


def _figure_name(figure: FigureRef) -> str:
    """The figure as a peer can find it: its receipt handle when it has one, caption, file."""
    handle = f"{figure.receipt} " if figure.receipt else ""
    caption = f"'{figure.caption}' " if figure.caption else ""
    return f"{handle}{caption}({Path(figure.path).name})"


def _document_name(document: DocumentRef) -> str:
    return f"{Path(document.path).name} ({document.title})"
