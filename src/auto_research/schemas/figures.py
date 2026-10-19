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


class FigureCitation(MessageModel):
    """A citation to one catalog figure: which source, which 1-based slot (V-4).

    The handle an authoring agent emits in ``edit_report`` to *place* a figure into its
    report. ``source`` names a figure catalog — ``"self"`` for an agent's own produced
    figures, or a worker key like ``"analyst_worker_0"`` for one of the lead's per-worker
    catalogs — and ``index`` is the 1-based position within that source's append-only list
    (the ``[source:index]`` label shown where the figure first appears). The runtime resolves each
    citation to a real :class:`FigureRef`; the *report-local* figure number is the
    citation's position in the ordered ``cited_figures`` list, so prose "Figure k" is the
    k-th cited. The two coordinate systems never mix in one message: ``{source, index}`` is
    the selection handle (menu), "Figure k" is the reference (finished report).
    """

    source: NonEmptyStr = Field(
        description=(
            "Catalog source: 'self' for an analyst worker (its own figures, its only source); for "
            "the analyst lead, the worker whose figure it is, e.g. 'analyst_worker_0'."
        )
    )
    index: Annotated[int, Field(ge=1)] = Field(
        description="1-based slot within that source (its [source:index] number)."
    )


class FiguredReport(MessageModel):
    """Base for inter-LLM reports that carry figures by citation (CLAUDE.md §7; V-4).

    ``cited_figures`` is what the authoring model emits (catalog handles, in citation
    order); ``figures`` is the runtime-resolved, ordered :class:`FigureRef` list
    (Figure 1 = ``figures[0]``). Cited figures are placed and ordered; uncited catalog
    figures are dropped — this *is* the de-duplication (the model picks one when a plot
    appears twice), so no separate dedup pass is needed.
    """

    cited_figures: list[FigureCitation] = Field(
        default_factory=list,
        description=(
            "The figures this report places: an array of {source, index} objects, one per figure, "
            "in the order they should appear. Cite every figure the prose refers to; an uncited "
            "catalog figure is dropped."
        ),
    )
    figures: list[FigureRef] = Field(
        default_factory=list,
        description="Resolved by the runtime from cited_figures; leave it alone.",
    )


class DocumentRef(MessageModel):
    """Serializable reference to a document attachment, never the bytes."""

    path: Path
    title: NonEmptyStr
    mime_type: NonEmptyStr
    created_at: datetime
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def document_from_file(
    path: str | Path, *, title: str | None = None, sha256: str | None = None,
) -> DocumentRef:
    """Build a document reference for a local attachment.

    The path is resolved here, so the reference names the canonical file a later reader checks
    component by component (``provenance.store.read_receipted``). ``sha256`` is the hash recorded
    in a receipt, when there is one: a reader then compares the bytes it sends against *that*, not
    against whatever the file holds when the reference is built.
    """

    file_path = Path(path).resolve()
    digest = sha256 or hashlib.sha256(file_path.read_bytes()).hexdigest()
    mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return DocumentRef(
        path=file_path,
        title=title or file_path.stem,
        mime_type=mime_type,
        created_at=datetime.fromtimestamp(file_path.stat().st_mtime).astimezone(),
        sha256=digest,
    )
