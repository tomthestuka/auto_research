"""Pure-Python text retrieval tool: returns local PDF paths from the SQLite DB."""

from pathlib import Path

from auto_research.retrieval.backend import RetrievalBackend, SqliteRetrievalBackend


async def text_search_paths(
    query: str,
    *,
    limit: int = 10,
    backend: RetrievalBackend | None = None,
) -> list[Path]:
    """Return local PDF paths for papers matching *query* by text search.

    Each matching paper is one item (keyed by paper id) whose PDF is an
    attribute, so ``require_local_path`` filters to PDF-backed papers before the
    limit — returning up to *limit* distinct local PDFs, top-scored first.
    No LLM call is made — the result is a direct SQLite text-match.
    """
    b = backend if backend is not None else SqliteRetrievalBackend()
    hits = await b.text_search(query, limit=limit, require_local_path=True)
    return [hit.local_path for hit in hits if hit.local_path is not None]
