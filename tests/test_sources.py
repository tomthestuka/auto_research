"""Deterministic tests for step-5 source normalization."""

import asyncio
from datetime import UTC, datetime

from auto_research.schemas import FullTextLink
from auto_research.sources import ArxivSource, NasaAdsSource, WebSource
from auto_research.sources.nasa_ads_source import _result_from_ads
from auto_research.sources.downloads import download_and_ingest_result
from auto_research.storage import Storage


class FakeArxivAuthor:
    def __init__(self, name):
        self.name = name


class FakeArxivResult:
    title = "  Binary Paper  "
    entry_id = "https://arxiv.org/abs/1234.5678"
    summary = "  Summary text.  "
    authors = [FakeArxivAuthor("A. Author")]
    categories = ["astro-ph.HE"]
    published = datetime(2026, 5, 27, tzinfo=UTC)
    pdf_url = "https://arxiv.org/pdf/1234.5678"

    def get_short_id(self):
        return "1234.5678"


class FakeArxivClient:
    def results(self, search):
        assert search.query == "black hole binary"
        yield FakeArxivResult()


def test_arxiv_source_normalizes_results():
    results = asyncio.run(ArxivSource(client=FakeArxivClient()).search("black hole binary", limit=1))

    assert results[0].source == "arxiv"
    assert results[0].title == "Binary Paper"
    assert results[0].authors == ["A. Author"]
    assert results[0].identifiers == {"arxiv_id": "1234.5678"}
    assert str(results[0].pdf_url) == "https://arxiv.org/pdf/1234.5678"


class FakeAdsPaper:
    title = ["ADS Paper"]
    bibcode = "2026ApJ...000..001A"
    doi = ["10.0000/example"]
    abstract = "ADS abstract."
    author = ["A. Astronomer"]
    keyword = ["black holes"]
    pubdate = "2026-05-00"


def test_ads_source_normalizes_results(monkeypatch):
    class FakeSearchQuery:
        def __init__(self, **kwargs):
            assert kwargs["q"] == "black hole binary"

        def __iter__(self):
            return iter([FakeAdsPaper()])

    monkeypatch.setattr("auto_research.sources.nasa_ads_source.ads.SearchQuery", FakeSearchQuery)

    results = asyncio.run(
        NasaAdsSource(token="token", resolve_full_text=False).search("black hole binary", limit=1)
    )

    assert results[0].source == "nasa_ads"
    assert results[0].title == "ADS Paper"
    assert results[0].identifiers["bibcode"] == "2026ApJ...000..001A"
    assert results[0].identifiers["doi"] == "10.0000/example"


def test_ads_source_prefers_resolved_pdf_link():
    result = _result_from_ads(
        FakeAdsPaper(),
        full_text_links=[
            FullTextLink(
                link_type="ESOURCE|EPRINT_HTML",
                url="https://arxiv.org/abs/astro-ph/0306213",
            ),
            FullTextLink(
                link_type="ESOURCE|EPRINT_PDF",
                url="https://arxiv.org/pdf/astro-ph/0306213",
            ),
        ],
    )

    assert str(result.pdf_url) == "https://arxiv.org/pdf/astro-ph/0306213"
    assert [link.link_type for link in result.full_text_links] == [
        "ESOURCE|EPRINT_HTML",
        "ESOURCE|EPRINT_PDF",
    ]


class FakeTavilyClient:
    def search(self, **kwargs):
        assert kwargs["query"] == "black hole binary"
        return {
            "results": [
                {
                    "title": "Web Paper",
                    "url": "https://example.org/paper",
                    "content": "Web abstract.",
                }
            ]
        }


def test_web_source_normalizes_results():
    results = asyncio.run(WebSource(client=FakeTavilyClient()).search("black hole binary", limit=1))

    assert results[0].source == "web"
    assert results[0].title == "Web Paper"
    assert str(results[0].url) == "https://example.org/paper"
    assert results[0].abstract == "Web abstract."


def test_download_and_ingest_result_uses_source_metadata(tmp_path, monkeypatch):
    from auto_research.sources import downloads

    def fake_download(url, destination):
        assert url == "https://arxiv.org/pdf/1234.5678"
        destination.write_bytes(b"pdf")
        return destination

    monkeypatch.setattr(downloads, "download_pdf", fake_download)
    db = Storage(tmp_path / "test.sqlite3")
    result = asyncio.run(ArxivSource(client=FakeArxivClient()).search("black hole binary", limit=1))[0]

    async def run():
        await db.initialize()
        return await download_and_ingest_result(db, result, target_dir=tmp_path / "papers")

    paper = asyncio.run(run())

    assert paper is not None
    assert paper.source == "arxiv"
    assert paper.source_id == "1234.5678"
    assert paper.title == "Binary Paper"
    assert paper.local_path is not None
    assert paper.local_path.name == "arxiv_1234.5678.pdf"


def test_download_and_ingest_result_returns_none_on_failed_download(tmp_path, monkeypatch):
    from auto_research.sources import downloads

    def failing_download(url, destination):
        return None

    monkeypatch.setattr(downloads, "download_pdf", failing_download)
    db = Storage(tmp_path / "test.sqlite3")
    result = asyncio.run(ArxivSource(client=FakeArxivClient()).search("black hole binary", limit=1))[0]

    async def run():
        await db.initialize()
        return await download_and_ingest_result(db, result, target_dir=tmp_path / "papers")

    assert asyncio.run(run()) is None
