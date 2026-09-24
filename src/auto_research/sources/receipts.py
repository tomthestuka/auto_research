"""Minting paper and web receipts: what a role that reads literature cites.

Every role that reads literature mints through these two functions (the librarian under ``lib``,
the reviewer under ``rev``) into the run's :class:`~auto_research.provenance.ReceiptStore`, so a
cited document means the same thing whoever checks it.

- **Paper — cited, never text-matched** (human ruling 2026-09-24, "option A"). The model reads the
  downloaded PDF natively, and the receipt's stored bytes ARE that PDF (``path`` = ``source_path``,
  ``sha256`` = ``source_sha256``), so ``verify()`` proves the cited file is the one that was read.
  No text is extracted: no extractor is faithful to a PDF (live, a text layer dropped the minus sign
  of a correct "−22 ± 2"), so a quote or a number cannot be checked against a paper at all, and the
  roles cite papers by handle. Values taken from a paper are verified by a person against the PDF.
- **Web page:** the Tavily extract is both what the model reads and the stored text, so quotes and
  numbers from a page ARE checked. Tavily returns no raw HTML, so the source hash is the extract's.

Reopening the same document returns its existing receipt (matched on ``source_sha256``), so a
document keeps one handle for the whole run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from auto_research.provenance import ReceiptStore, sha256_file
from auto_research.schemas.artifacts import Receipt

#: The text extractor recorded on a web receipt.
WEB_PARSER = "tavily-extract"


def existing_receipt(
    store: ReceiptStore, *, source: str, kind: str, source_sha256: str
) -> Receipt | None:
    """The receipt ``source`` already minted for these source bytes, if any (also after a resume)."""
    for receipt in store.receipts(source=source, kind=kind):
        if receipt.source_sha256 == source_sha256:
            return receipt
    return None


def mint_paper_receipt(
    store: ReceiptStore,
    pdf: Path,
    *,
    source: str,
    title: str,
    ids: dict[str, str] | None = None,
    produced_by: str = "read_paper",
) -> Receipt | None:
    """Mint (or return) the paper receipt citing a local PDF, or ``None`` when the file is no PDF."""
    pdf = Path(pdf).resolve()
    with pdf.open("rb") as handle:
        if b"%PDF-" not in handle.read(1024):
            return None
    pdf_sha = sha256_file(pdf)
    known = existing_receipt(store, source=source, kind="paper", source_sha256=pdf_sha)
    if known is not None:
        return known
    return store.mint(
        source=source,
        kind="paper",
        produced_by=produced_by,
        path=pdf,
        title=title,
        ids=dict(ids or {}),
        source_sha256=pdf_sha,
        source_path=pdf,
    )


def mint_web_receipt(
    store: ReceiptStore,
    url: str,
    text: str,
    *,
    source: str,
    title: str | None = None,
    produced_by: str = "web_fetch",
) -> Receipt:
    """Mint (or return) the web receipt for a fetched page's extract."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    known = existing_receipt(store, source=source, kind="web", source_sha256=digest)
    if known is not None:
        return known
    return store.mint(
        source=source,
        kind="web",
        produced_by=produced_by,
        data=text,
        title=title or url,
        ids={"url": url},
        source_sha256=digest,
        parser=WEB_PARSER,
    )
