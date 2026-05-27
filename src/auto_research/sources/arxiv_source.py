"""arXiv source adapter."""

from datetime import date

import arxiv

from auto_research.schemas import SearchResult


class ArxivSource:
    """Query arXiv and normalize results."""

    def __init__(self, client: arxiv.Client | None = None):
        self.client = client or arxiv.Client(page_size=100)

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Search arXiv for papers."""

        return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        search = arxiv.Search(
            query=query,
            max_results=limit,
            sort_by=arxiv.SortCriterion.Relevance,
            sort_order=arxiv.SortOrder.Descending,
        )
        # Use page_size=limit so the API request fetches only what we need.
        client = arxiv.Client(page_size=limit)
        return [_result_from_arxiv(item) for item in client.results(search)]


def _result_from_arxiv(item) -> SearchResult:
    published: date | None = item.published.date() if getattr(item, "published", None) else None
    return SearchResult(
        source="arxiv",
        title=item.title.strip(),
        url=item.entry_id,
        abstract=item.summary.strip() if item.summary else None,
        authors=[author.name for author in item.authors],
        published=published,
        identifiers={"arxiv_id": item.get_short_id()},
        keywords=[category for category in getattr(item, "categories", [])],
        pdf_url=item.pdf_url,
    )
