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

    Only papers with a downloaded PDF (non-null local_path) are returned.
    No LLM call is made — the result is a direct SQLite text-match.
    """
    b = backend if backend is not None else SqliteRetrievalBackend()
    hits = await b.text_search(query, limit=limit)
    return [hit.local_path for hit in hits if hit.local_path is not None]
