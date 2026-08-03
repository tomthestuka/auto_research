"""NASA ADS source adapter."""

import os
import json
from datetime import date
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import ads
from pydantic import ValidationError

from auto_research.schemas import FullTextLink, SearchResult


PDF_LINK_PRIORITY = ("EPRINT_PDF", "PUB_PDF", "ADS_PDF", "AUTHOR_PDF")


class NasaAdsSource:
    """Query NASA ADS and normalize paper records."""

    def __init__(self, token: str | None = None, resolve_full_text: bool = True):
        self.token = token or os.getenv("ADS_DEV_KEY") or os.getenv("ADS_API_TOKEN")
        self.resolve_full_text = resolve_full_text

    async def search(self, query: str, *, limit: int = 10) -> list[SearchResult]:
        """Search NASA ADS for papers."""

        return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        ads_query = ads.SearchQuery(
            q=query,
            rows=limit,
            max_pages=1,
            sort="score desc",
            fl=[
                "author",
                "bibcode",
                "doi",
                "keyword",
                "pubdate",
                "title",
                "abstract",
                "year",
            ],
            token=self.token,
        )
        return [
            _result_from_ads(
                item,
                full_text_links=resolve_ads_full_text(getattr(item, "bibcode", None), self.token)
                if self.resolve_full_text
                else [],
            )
            for item in ads_query
        ]


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _ads_date(item) -> date | None:
    pubdate = _first(getattr(item, "pubdate", None))
    if isinstance(pubdate, str) and len(pubdate) >= 7:
        try:
            return date.fromisoformat(f"{pubdate[:7]}-01")
        except ValueError:
            return None
    year = _first(getattr(item, "year", None))
    if year:
        try:
            return date(int(year), 1, 1)
        except (TypeError, ValueError):
            return None
    return None


def resolve_ads_full_text(bibcode: str | None, token: str | None) -> list[FullTextLink]:
    """Resolve ADS full-text source buttons for a bibcode.

    A bibcode with no electronic-source links is a normal outcome, and ADS
    reports it inconsistently: usually ``200`` with an empty ``records`` list,
    but sometimes ``404`` (``{"error": "did not find any records"}``). Treat the
    404 the same as the empty case — return ``[]`` rather than propagating, so a
    single link-less paper cannot crash the literature node.

    Individual records are also served malformed on occasion — a relative URL
    where ``FullTextLink.url`` wants an absolute one, or a missing
    ``link_type``. Such a record is skipped and every well-formed link beside it
    is kept, so one bad link costs one link rather than the whole source.
    """

    if not bibcode or not token:
        return []
    url = f"https://api.adsabs.harvard.edu/v1/resolver/{quote(bibcode, safe='')}/esource"
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    records = payload.get("links", {}).get("records", [])
    links = []
    for record in records:
        record_url = record.get("url")
        if not record_url:
            continue
        try:
            link = FullTextLink(
                link_type=str(record.get("link_type", "")),
                url=record_url,
                title=record.get("title"),
            )
        except ValidationError:
            continue
        links.append(link)
    return links


def _best_pdf_url(links: list[FullTextLink]) -> str | None:
    for preferred in PDF_LINK_PRIORITY:
        for link in links:
            if preferred in link.link_type.upper():
                return str(link.url)
    return None


def _result_from_ads(item, *, full_text_links: list[FullTextLink] | None = None) -> SearchResult:
    title = _first(getattr(item, "title", None)) or getattr(item, "bibcode", "Untitled ADS result")
    bibcode = getattr(item, "bibcode", None)
    doi = _first(getattr(item, "doi", None))
    identifiers = {}
    if bibcode:
        identifiers["bibcode"] = bibcode
    if doi:
        identifiers["doi"] = doi
    links = full_text_links or []
    pdf_url = _best_pdf_url(links)
    if pdf_url is None and getattr(item, "arxiv_id", None):
        pdf_url = f"https://arxiv.org/pdf/{getattr(item, 'arxiv_id')}"
    return SearchResult(
        source="nasa_ads",
        title=title.strip(),
        url=f"https://ui.adsabs.harvard.edu/abs/{bibcode}/abstract" if bibcode else None,
        abstract=getattr(item, "abstract", None),
        authors=list(getattr(item, "author", []) or []),
        published=_ads_date(item),
        identifiers=identifiers,
        keywords=list(getattr(item, "keyword", []) or []),
        pdf_url=pdf_url,
        full_text_links=links,
    )
