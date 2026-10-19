"""The shared paper reader: one way for every role to open, cite and quote a paper.

Human rulings 2026-09-24: roles read the downloaded **PDF natively** (provider-agnostic, no layout
parser on this path), and the reviewer uses the **same** paper path as the librarian. So this is
the one ``read_paper`` implementation; the librarian adds ``hit=N`` on top, the reviewer uses it as
is (both through ``extra_tools`` or directly).

What opening a paper does:

- **Attach the PDF** to the role's context (``ToolOutcome.documents``); ``_react`` keeps it attached
  for the rest of the loop, and each vendor reads it as printed — text, tables, figures.
- **One paper, one receipt for the run.** A PDF whose bytes (``source_sha256``) already have a paper
  receipt from ANY source keeps that handle and is admitted into the reader's lookup, so the
  reviewer re-opening the librarian's paper cites ``lib:p4``, not a second ``rev:p2``. Only a PDF
  the run has never receipted is minted, under the reader's own ``source``
  (:func:`auto_research.sources.receipts.mint_paper_receipt`: the receipt cites the PDF itself; a
  paper is cited by handle, never text-matched — human ruling 2026-09-24).
- **Title and identifiers come from the knowledge base** (matched by path or content hash). A PDF
  it has no row for takes its file name as the title and its identifier from the downloader's own
  ``<source>_<id>.pdf`` naming.

Adapted from the reviewer's interim reader (5c), whose semantics this keeps.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from auto_research.agents.react import ExtraTool, ToolOutcome
from auto_research.provenance import sha256_file
from auto_research.schemas.artifacts import Receipt
from auto_research.schemas.figures import document_from_file
from auto_research.sources.receipts import mint_paper_receipt

READ_PAPER_TOOL = "read_paper"

#: Knowledge-base ``papers.source`` → the persistent-id key a receipt carries.
_KB_ID_KEYS = {"arxiv": "arxiv", "nasa_ads": "bibcode"}

#: The downloader names a file ``<source>_<id>.pdf`` (``storage.safe_filename``), so a PDF the
#: knowledge base has no row for still names its identifier.
_STEM_ID = re.compile(r"^(?P<source>arxiv|nasa_ads)_(?P<id>.+)$")


def format_ids(ids: Mapping[str, str]) -> str:
    """Identifiers written the way prose cites them: ``arXiv:2101.00001``, ``doi:10.…``, ``bibcode …``.

    The prefix is what lets the provenance check tell an identifier from a measured number.
    """
    parts = []
    for key, value in ids.items():
        if key == "arxiv":
            parts.append(f"arXiv:{value}")
        elif key == "doi":
            parts.append(f"doi:{value}")
        else:
            parts.append(f"{key} {value}")
    return ", ".join(parts)


def ids_from_stem(stem: str) -> dict[str, str]:
    """``nasa_ads_<bibcode>`` → ``{bibcode}``; ``arxiv_<id>`` → ``{arxiv}``; otherwise nothing."""
    match = _STEM_ID.match(stem)
    if match is None:
        return {}
    return {_KB_ID_KEYS[match["source"]]: match["id"]}


class PaperReader:
    """Open papers for one role: attach, receipt once per run, admit into the role's scope.

    ``lookup`` is the scope the role's report is checked against (a ``ScopedLookup``, or the store
    itself for a role that may cite any receipt), or a zero-argument callable returning the current
    one — for a role whose workspace is rebuilt each round. ``allowed`` gates ``path=`` (the role's
    filesystem guard); ``None`` allows any readable file.
    """

    def __init__(
        self,
        store: Any,
        lookup: Any,
        *,
        source: str,
        storage: Any | None = None,
        allowed: Callable[[Path], bool] | None = None,
        minter: Callable[..., Receipt | None] | None = None,
        unreadable_action: str = "state it among what you could not read",
    ) -> None:
        self.store = store
        self._lookup = lookup
        self.source = source
        self.storage = storage
        self.allowed = allowed
        self._mint = minter or mint_paper_receipt
        self.unreadable_action = unreadable_action

    # ------------------------------------------------------------------ the tool

    def tool(self) -> ExtraTool:
        return (self.definition(), self.execute)

    @staticmethod
    def definition(*, extra_properties: Mapping[str, dict] | None = None, lead: str = "") -> dict:
        properties = {
            **(extra_properties or {}),
            "path": {"type": "string", "description": "A local PDF path, e.g. one text_search returned."},
            "handle": {"type": "string", "description": "A paper receipt you can read, e.g. 'lib:p3'."},
        }
        return {
            "name": READ_PAPER_TOOL,
            "description": (
                lead
                + "Open a paper: its PDF is attached to your context, so you read the whole paper as "
                "printed — text, tables, equations and figures — and it stays attached for the rest "
                "of your work. Open a local PDF with path=..., or a paper you can read by handle. "
                "Cite it by the handle this returns, e.g. [lib:p3]. A paper is cited, not quoted: "
                "state what it reports in your own words beside its handle."
            ),
            "input_schema": {"type": "object", "properties": properties},
        }

    async def execute(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        handle = str(arguments.get("handle") or "").strip()
        if handle:
            outcome, _ = self.open_handle(handle)
            return outcome
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return ToolOutcome(text="read_paper: pass path=... or handle=....", is_error=True)
        pdf = Path(raw).expanduser()
        if (self.allowed is not None and not self.allowed(pdf)) or not pdf.is_file():
            return ToolOutcome(text=f"read_paper: {pdf} is not a readable local PDF.", is_error=True)
        outcome, _ = await self.open_pdf(pdf)
        return outcome

    # ------------------------------------------------------------------ opening

    def open_handle(self, handle: str) -> tuple[ToolOutcome, Receipt | None]:
        receipt = self.lookup.get(handle)
        if receipt is None or receipt.kind != "paper" or receipt.source_path is None:
            return ToolOutcome(text=f"read_paper: '{handle}' is not a paper you can open.", is_error=True), None
        return self._attach(receipt), receipt

    async def open_pdf(
        self, pdf: Path, *, title: str | None = None, ids: Mapping[str, str] | None = None
    ) -> tuple[ToolOutcome, Receipt | None]:
        """Open a local PDF: reuse its run-wide receipt or mint one, admit it, attach the file.

        ``title``/``ids`` are what the caller already knows (a search hit); otherwise they come from
        the knowledge base. Returns the outcome for the model and the receipt (``None`` on refusal).
        """
        digest = await asyncio.to_thread(sha256_file, pdf)
        receipt = next(
            (r for r in self.store.receipts(kind="paper") if r.source_sha256 == digest), None
        )
        if receipt is None:
            if title is None:
                title, known_ids = await self._metadata(pdf, digest)
                ids = {**known_ids, **(ids or {})}
            receipt = await asyncio.to_thread(
                self._mint, self.store, pdf, source=self.source, title=title, ids=dict(ids or {})
            )
        if receipt is None:
            return ToolOutcome(
                text=f"read_paper: {pdf.name} is not a PDF; {self.unreadable_action}.",
                is_error=True,
            ), None
        admit = getattr(self.lookup, "admit", None)
        if callable(admit):
            admit([receipt.handle])
        return self._attach(receipt), receipt

    @property
    def lookup(self) -> Any:
        return self._lookup() if callable(self._lookup) else self._lookup

    def _attach(self, receipt: Receipt) -> ToolOutcome:
        # The loop attaches a document once while the conversation carries it (R2), and again
        # after a segment boundary dropped it, so the reader returns it every time.
        ids = format_ids(receipt.ids or {}) or "no identifiers recorded"
        return ToolOutcome(
            text=(
                f"[{receipt.handle}] {receipt.title} — the PDF is attached ({ids}). Cite it as "
                f"[{receipt.handle}]."
            ),
            documents=[
                document_from_file(
                    Path(receipt.source_path), title=f"[{receipt.handle}] {receipt.title}",
                    sha256=receipt.source_sha256,  # the receipted hash, not the file's current one
                )
            ],
        )

    async def _metadata(self, pdf: Path, digest: str) -> tuple[str, dict[str, str]]:
        """Title and identifiers from the knowledge base; the file name only when it has none."""
        if self.storage is not None:
            try:
                resolved = str(pdf.resolve())
                for paper in await self.storage.list_papers():
                    same_file = (
                        paper.local_path is not None
                        and str(Path(paper.local_path).resolve()) == resolved
                    )
                    if same_file or paper.sha256 == digest:
                        key = _KB_ID_KEYS.get(paper.source)
                        return paper.title, ({key: paper.source_id} if key else {})
            except Exception:  # noqa: BLE001 — metadata is a convenience; the receipt is the record
                pass
        return pdf.stem, ids_from_stem(pdf.stem)


def paper_reader_tool(
    store: Any,
    lookup: Any,
    *,
    source: str,
    storage: Any | None = None,
    allowed: Callable[[Path], bool] | None = None,
) -> ExtraTool:
    """The shared ``read_paper`` as an ``(definition, executor)`` pair for ``extra_tools``."""
    return PaperReader(store, lookup, source=source, storage=storage, allowed=allowed).tool()
