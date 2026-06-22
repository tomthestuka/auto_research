"""Download source papers into the runtime paper directory."""

import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from auto_research.env import papers_dir
from auto_research.schemas import SearchResult
from auto_research.storage import Storage, StoredPaper, paper_from_search_result, safe_filename

_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF = 2.0


def _is_pdf_bytes(data: bytes) -> bool:
    """True when ``data`` is a real PDF (the ``%PDF-`` signature near the start).

    Source resolvers (notably NASA ADS ``esource`` links) sometimes return an
    HTML landing/paywall page instead of the paper; such a page saved as ``.pdf``
    is later rejected by the model document API ("The PDF specified was not
    valid"), so the signature is checked before a file is trusted or stored.
    """

    return b"%PDF-" in data[:1024]


def download_pdf(url: str, destination: Path) -> Path | None:
    """Download a PDF URL to a destination path if it is not already present.

    Transient fetch failures (a momentary arxiv/ADS 404, a network blip, a
    timeout) are retried with linear backoff. A response whose bytes are not a
    PDF (e.g. an HTML landing/paywall page) is discarded — ``None`` is returned
    so callers degrade to metadata-only ingest instead of storing a file the
    model API will reject. A previously stored file is reused only when it is
    itself a valid PDF; a stale non-PDF is removed and re-fetched.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        with open(destination, "rb") as handle:
            if _is_pdf_bytes(handle.read(1024)):
                return destination
        destination.unlink()  # stale non-PDF (e.g. HTML saved as .pdf) — re-fetch
    request = Request(url, headers={"User-Agent": "auto_research/0.0"})
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            with urlopen(request, timeout=60) as response:
                data = response.read()
            if not _is_pdf_bytes(data):
                return None  # not a PDF (e.g. an HTML landing page) — degrade to metadata
            destination.write_bytes(data)
            return destination
        except (HTTPError, URLError, TimeoutError):
            if attempt == _DOWNLOAD_ATTEMPTS:
                return None
            time.sleep(_DOWNLOAD_BACKOFF * attempt)
    return None


async def download_and_ingest_result(
    storage: Storage,
    result: SearchResult,
    *,
    target_dir: str | Path | None = None,
) -> StoredPaper | None:
    """Download a source PDF, ingest it, and return the stored paper."""

    if result.pdf_url is None:
        return None
    root = Path(target_dir).expanduser() if target_dir is not None else papers_dir()
    source_id = (
        result.identifiers.get("arxiv_id")
        or result.identifiers.get("bibcode")
        or result.identifiers.get("doi")
        or result.title
    )
    destination = root / safe_filename(f"{result.source}_{source_id}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    path = download_pdf(str(result.pdf_url), destination)
    if path is None:
        return None
    paper_id = await storage.upsert_paper(paper_from_search_result(result, path))
    return await storage.get_paper(paper_id)
