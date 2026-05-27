"""Figure references carried by agent messages."""

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
