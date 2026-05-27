"""Figure references carried by agent messages."""

import hashlib
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]


class FigureRef(MessageModel):
    """Serializable reference to an artifact image, never the image bytes."""

    path: Path
    caption: NonEmptyStr
    mime_type: NonEmptyStr
    produced_by: NonEmptyStr
    created_at: datetime
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    thumbnail_b64: str | None = None


class DocumentRef(MessageModel):
    """Serializable reference to a document attachment, never the bytes."""

    path: Path
    title: NonEmptyStr
    mime_type: NonEmptyStr
    created_at: datetime
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def document_from_file(path: str | Path, *, title: str | None = None) -> DocumentRef:
    """Build a document reference for a local attachment."""

    file_path = Path(path)
    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return DocumentRef(
        path=file_path,
        title=title or file_path.stem,
        mime_type=mime_type,
        created_at=datetime.fromtimestamp(file_path.stat().st_mtime).astimezone(),
        sha256=digest,
    )
