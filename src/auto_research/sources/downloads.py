"""Download source papers into the runtime paper directory.

**arXiv discipline** (read on arXiv's own pages 2026-09-24). The bulk-data guidance asks tools to
"use the dedicated site ``export.arxiv.org`` … specifically set aside for programmatic access";
the robots policy says "Indiscriminate automated downloads from this site are not permitted" and
that continued rapid requests after a ``403`` "will be interpreted as an attack"; the API terms ask
for one request every three seconds on a single connection. So an arXiv PDF is always fetched from
``export.arxiv.org`` (the primary host, never a fallback — switching hosts to escape a refusal is the
rate-limit circumvention the terms prohibit), one at a time and at least three seconds apart for the
whole process; a ``403`` stops arXiv downloads for the rest of the process; a ``406``/``429`` waits
fifteen seconds (the ``robots.txt`` crawl delay) and tries once more. The User-Agent names the tool
and its version and no person: this repository is shared, and arXiv documents no contact field.
"""

import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from auto_research import __version__
from auto_research.env import papers_dir
from auto_research.schemas import SearchResult
from auto_research.storage import Storage, StoredPaper, paper_from_search_result, safe_filename

_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_BACKOFF = 2.0
_USER_AGENT = f"auto_research/{__version__}"

_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
_ARXIV_EXPORT_HOST = "export.arxiv.org"
_ARXIV_MIN_INTERVAL_S = 3.0
_ARXIV_REFUSAL_WAIT_S = 15.0


class _ArxivGate:
    """Process-wide arXiv pacing: one download at a time, spaced, and a stop after a 403."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last = float("-inf")
        self.blocked = False

    def wait_turn(self) -> None:
        """Call with ``lock`` held: sleep until the minimum interval since the last request."""
        delay = self.last + _ARXIV_MIN_INTERVAL_S - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


_ARXIV_GATE = _ArxivGate()


def arxiv_download_url(url: str) -> str | None:
    """The ``export.arxiv.org`` form of an arXiv PDF URL, or ``None`` for any other URL."""
    parts = urlsplit(url)
    if parts.hostname not in _ARXIV_HOSTS or not parts.path.startswith("/pdf/"):
        return None
    return urlunsplit(("https", _ARXIV_EXPORT_HOST, parts.path, parts.query, ""))


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
    arxiv_url = arxiv_download_url(url)
    if arxiv_url is not None:
        return _download_arxiv(arxiv_url, destination)
    request = Request(url, headers={"User-Agent": _USER_AGENT})
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


def _download_arxiv(url: str, destination: Path) -> Path | None:
    """One arXiv PDF under arXiv's discipline (module docstring); ``None`` when refused."""
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/pdf"})
    with _ARXIV_GATE.lock:
        for attempt in (1, 2):
            if _ARXIV_GATE.blocked:
                return None
            _ARXIV_GATE.wait_turn()
            try:
                with urlopen(request, timeout=60) as response:
                    data = response.read()
            except HTTPError as exc:
                if exc.code == 403:
                    _ARXIV_GATE.blocked = True  # arXiv's robot block: stop, never retry
                    return None
                if exc.code in (406, 429) and attempt == 1:
                    time.sleep(_ARXIV_REFUSAL_WAIT_S)
                    continue
                return None
            except (URLError, TimeoutError):
                if attempt == 1:
                    continue
                return None
            if not _is_pdf_bytes(data):
                return None
            destination.write_bytes(data)
            return destination
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
