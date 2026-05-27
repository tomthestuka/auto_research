"""Phase 2 stub: vector-embedding retrieval over the local DB."""

from pathlib import Path

from auto_research.retrieval.backend import RetrievalBackend


async def vector_search_paths(
    query: str,
    *,
    limit: int = 10,
    backend: RetrievalBackend | None = None,
) -> list[Path]:
    """Phase 2 stub — raises NotImplementedError. See CLAUDE.md §13."""
    _ = (query, limit, backend)
    raise NotImplementedError("vector_search_paths is a Phase 2 feature — see CLAUDE.md §13")
