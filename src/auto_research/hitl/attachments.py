"""Files on the human channel, by receipt: resolve outbound handles, mint the human's files.

The two runtime ends of an attachment. A role names receipts by handle; the runtime resolves each
one inside that role's citable view and re-checks its bytes before any connector sees a path, so
the human is shown exactly what a receipt vouches for. On the way back, the files a connector saved
from the human's reply become receipts, so a role can view them; they ground nothing, because the
human gives verdicts, not materials.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Iterable

from auto_research.schemas import HumanAttachment, HumanResponse, Receipt

#: Image types a human's file is minted as a figure for; everything else is a data receipt.
FIGURE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def _file_for(receipt: Receipt) -> tuple[Path, str]:
    """The bytes a human should receive for ``receipt``, and the file name to show."""
    if receipt.source_path is not None:  # a paper or page: the original PDF/HTML, not its text
        path = Path(receipt.source_path)
        return path, path.name
    path = Path(receipt.path)
    if receipt.kind in ("figure", "data"):
        return path, path.name
    # A text receipt's blob is named by its hash; name it by its handle instead.
    return path, receipt.handle.replace(":", "_") + ".txt"


def resolve_attachments(
    lookup: Any, handles: Iterable[str]
) -> tuple[list[HumanAttachment], list[str]]:
    """Resolve receipt handles to attachments inside ``lookup`` (the sender's ``ScopedLookup``).

    Returns the attachments and one model-readable sentence per handle that could not be attached:
    not a receipt the sender may cite, or bytes that no longer match the receipt's hash.
    """
    attachments: list[HumanAttachment] = []
    problems: list[str] = []
    for handle in dict.fromkeys(h.strip() for h in handles if h and h.strip()):
        receipt = lookup.get(handle)
        if receipt is None:
            problems.append(f"{handle} is not a receipt you can cite, so it was not attached.")
            continue
        problem = lookup.verify(handle)
        if problem:
            problems.append(f"{problem} It was not attached.")
            continue
        path, filename = _file_for(receipt)
        mime = (
            receipt.mime_type if receipt.source_path is None and receipt.mime_type else None
        ) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        attachments.append(HumanAttachment(
            handle=handle, path=path, filename=filename, mime_type=mime,
            caption=receipt.caption or receipt.title or "",
        ))
    return attachments, problems


def mint_reply_attachments(
    store: Any, *, source: str, response: HumanResponse, parent: str | None
) -> list[Receipt]:
    """Mint one receipt per file the human sent: a figure for an image, data for anything else.

    Each receipt hashes the file where the connector saved it and is parented on ``parent`` (the
    reply's own receipt), or on nothing when the reply text is not itself a receipt.
    ``response.attachments`` is updated in place with the minted handles.
    """
    minted: list[Receipt] = []
    parents = [parent] if parent else []
    for i, attachment in enumerate(response.attachments):
        path = Path(attachment.path)
        if not path.is_file():
            continue
        if attachment.mime_type in FIGURE_MIME_TYPES:
            receipt = store.mint(
                source=source, kind="figure", produced_by="human_reply", path=path,
                parents=parents, caption=attachment.caption or f"from the human: {attachment.filename}",
                mime_type=attachment.mime_type,
            )
        else:
            receipt = store.mint(
                source=source, kind="data", produced_by="human_reply", path=path, parents=parents,
                mime_type=attachment.mime_type, caption=attachment.caption or None,
            )
        response.attachments[i] = attachment.model_copy(update={"handle": receipt.handle})
        minted.append(receipt)
    return minted
