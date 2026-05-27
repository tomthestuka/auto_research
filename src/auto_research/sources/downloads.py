"""Download source papers into the runtime paper directory."""

import os
from pathlib import Path
from urllib.request import Request, urlopen

from auto_research.schemas import SearchResult
from auto_research.storage import Storage, StoredPaper, paper_from_search_result, safe_filename


def papers_dir() -> Path:
    """Return the local paper artifact directory."""

    return Path(os.getenv("AUTO_RESEARCH_PAPERS_DIR", "/data/fzhou/ar_runtime/papers")).expanduser()


def download_pdf(url: str, destination: Path) -> Path:
    """Download a PDF URL to a destination path if it is not already present."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        return destination
    request = Request(url, headers={"User-Agent": "auto_research/0.0"})
    with urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())
    return destination


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
    paper_id = await storage.upsert_paper(paper_from_search_result(result, path))
    return await storage.get_paper(paper_id)
