"""arXiv source adapter."""

import re
from datetime import date
from typing import Literal

import arxiv

from auto_research.schemas import SearchResult


class ArxivSource:
    """Query arXiv and normalize results."""

    def __init__(self, client: arxiv.Client | None = None):
        self.client = client or arxiv.Client(page_size=100)

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Search arXiv for papers."""

        return (await self.search_with_match(query, limit=limit))[0]

    async def search_with_match(
        self, query: str, *, limit: int = 10
    ) -> tuple[list[SearchResult], Literal["all", "any", "as_written"]]:
        """Search, preferring papers that match every term; say which match was used.

        The arXiv API does not require every bare term to match, so a keyword query ranks papers
        that share only some words (measured 2026-09-24: a query naming the author, target and
        topic of a known paper never returned it, while the same terms joined with AND put it in
        the top five). Bare keywords are therefore joined with AND first; when no paper matches
        them all, the loose query runs and the result says so. A query already written in arXiv's
        field syntax (``au:``, ``ti:``, ``abs:``, ``AND``/``OR``) is sent as written.
        """
        if _FIELD_SYNTAX.search(query):
            return self._search(query, limit), "as_written"
        strict = self._search(_all_terms(query), limit)
        if strict:
            return strict, "all"
        return self._search(query, limit), "any"

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        search = arxiv.Search(
            query=query,
            max_results=limit,
            sort_by=arxiv.SortCriterion.Relevance,
            sort_order=arxiv.SortOrder.Descending,
        )
        return [_result_from_arxiv(item) for item in self.client.results(search)]


#: arXiv field prefixes and boolean operators: a query using them is sent as written.
_FIELD_SYNTAX = re.compile(r"\b(?:ti|au|abs|co|jr|cat|rn|id|all):|\b(?:AND|OR|ANDNOT)\b")


def _all_terms(query: str) -> str:
    """``a b "c d"`` → ``all:a AND all:b AND all:"c d"``."""
    return " AND ".join(f"all:{term}" for term in re.findall(r'"[^"]+"|\S+', query))


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
