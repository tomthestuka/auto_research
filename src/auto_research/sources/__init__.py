"""Librarian source modules behind a shared `QuerySource` interface."""

from auto_research.sources.arxiv_source import ArxivSource
from auto_research.sources.base import QuerySource
from auto_research.sources.downloads import download_and_ingest_result, download_pdf, papers_dir
from auto_research.sources.nasa_ads_source import NasaAdsSource
from auto_research.sources.web_source import WebSource

__all__ = [
    "ArxivSource",
    "NasaAdsSource",
    "QuerySource",
    "WebSource",
    "download_and_ingest_result",
    "download_pdf",
    "papers_dir",
]
