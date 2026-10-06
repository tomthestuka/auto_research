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

from auto_research.provenance.store import read_receipted

#: Telegram's bot upload ceiling; a larger file is refused before it is read into memory.
MAX_ATTACHMENT_BYTES = 50 * 2**20

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
        path, filename = _file_for(receipt)
        # The bytes sent are the bytes checked (F26): read once without following a symlink, kept
        # beside the ledger for the connector, never re-read from the agent's folder. Changed since
        # it was receipted → still sent, with an alert (human ruling 2026-10-06).
        expected = receipt.source_sha256 if receipt.source_path is not None else receipt.sha256
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_ATTACHMENT_BYTES:
            problems.append(f"{handle} is {size / 2**20:.0f} MB, over the {MAX_ATTACHMENT_BYTES // 2**20} MB "
                            "a chat attachment can carry; it was not attached.")
            continue
        read = read_receipted(path, expected, label=handle)
        if read.data is None:
            problems.append(f"{handle}: {read.note} It was not attached.")
            continue
        path = _outbox_copy(lookup, read.data, filename)
        mime = (
            receipt.mime_type if receipt.source_path is None and receipt.mime_type else None
        ) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        caption = receipt.caption or receipt.title or ""
        attachments.append(HumanAttachment(
            handle=handle, path=path, filename=filename, mime_type=mime,
            # The human is told too when it changed since its receipt (F26).
            caption=f"{read.note} {caption}".strip() if read.note else caption,
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


def _outbox_copy(lookup: Any, data: bytes, filename: str) -> Path:
    """The verified bytes, written where no agent can reach (the run's receipt store)."""
    from auto_research.provenance.store import sha256_bytes

    store = getattr(lookup, "store", lookup)
    folder = Path(store.root) / "outbox" / sha256_bytes(data)[:16]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename
    if not target.is_file():
        target.write_bytes(data)
    return target
