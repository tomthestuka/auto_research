"""Literature search request and result messages."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, HttpUrl

from auto_research.schemas.base import MessageModel


NonEmptyStr = Annotated[str, Field(min_length=1)]
SearchSource = Literal["arxiv", "nasa_ads", "web", "local"]


class FullTextLink(MessageModel):
    """Candidate full-text link for a search result."""

    link_type: NonEmptyStr
    url: HttpUrl
    title: str | None = None


class SearchRequest(MessageModel):
    """Request from a reviewer or librarian searcher to a literature source."""

    query: NonEmptyStr
    sources: list[SearchSource] = Field(default_factory=list)
    max_results: int = Field(default=10, ge=1, le=100)
    since: date | None = None


class SearchResult(MessageModel):
    """Normalized metadata for one paper or authoritative web result."""

    source: SearchSource
    title: NonEmptyStr
    url: HttpUrl | None = None
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    published: date | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    pdf_url: HttpUrl | None = None
    full_text_links: list[FullTextLink] = Field(default_factory=list)
