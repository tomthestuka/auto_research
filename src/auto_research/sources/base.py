"""Shared source interface."""

from typing import Protocol

from auto_research.schemas import SearchResult


class QuerySource(Protocol):
    """Async search interface for librarian sources."""

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Return normalized search results."""
