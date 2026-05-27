"""Tavily web source adapter."""

import os

from tavily import TavilyClient

from auto_research.schemas import SearchResult


class WebSource:
    """Query Tavily web search and normalize results."""

    def __init__(self, client: TavilyClient | None = None):
        self.client = client or TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Search the web via Tavily."""

        return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        response = self.client.search(
            query=query,
            search_depth="basic",
            max_results=limit,
            include_raw_content=False,
        )
        return [_result_from_tavily(item) for item in response.get("results", [])]


def _result_from_tavily(item: dict) -> SearchResult:
    return SearchResult(
        source="web",
        title=(item.get("title") or item.get("url") or "Untitled web result").strip(),
        url=item.get("url"),
        abstract=item.get("content"),
    )
