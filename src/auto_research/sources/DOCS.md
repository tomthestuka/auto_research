# `auto_research.sources` — external literature sources

## Purpose
Provides the librarian I/O sub-team with read access to external literature and
the web, behind a single shared interface so new sources drop in cleanly
([CLAUDE.md §8](../../../CLAUDE.md#8-what-phase-1-built-the-system-as-it-stands)).

## Architecture
- `base.py` — the `QuerySource` Protocol/ABC.
- `arxiv_source.py` — backed by the `arxiv` Python library.
- `nasa_ads_source.py` — backed by the `ads` client; token from `ADS_DEV_KEY`.
- `web_source.py` — generic web search via Tavily; also reused by analysts for
  reading authoritative documentation.

Each module is independent; no source imports another.

## Interfaces / Contracts
- `QuerySource` protocol: `async search(query: str, *, limit: int = 10) ->
  list[SearchResult]`.
- `ArxivSource(client: arxiv.Client | None = None)` searches arXiv and returns
  `SearchResult(source="arxiv")`.
- `NasaAdsSource(token: str | None = None)` searches NASA ADS and returns
  `SearchResult(source="nasa_ads")`. If `token` is not provided, the ADS client
  reads `ADS_DEV_KEY` or `ADS_API_TOKEN` from the environment.
- `WebSource(client: TavilyClient | None = None)` searches Tavily and returns
  `SearchResult(source="web")`. If `client` is not provided, it reads
  `TAVILY_API_KEY` from the environment.
- Live source verification is opt-in:
  `AUTO_RESEARCH_LIVE_SOURCE_TESTS=1 pytest tests/test_live_sources.py -s`.
  It queries `black hole binary` against arXiv, NASA ADS, and Tavily, and writes
  timestamped JSONL and Markdown summary logs under `logs/dev/step-5/`.
- arXiv results include `pdf_url` and can be downloaded/ingested into storage.
  NASA ADS results call the ADS resolver service for `esource` links, the API
  equivalent of the UI's "Full Text Sources" buttons. Resolved source buttons are
  stored in `SearchResult.full_text_links`; the best PDF-like link is selected
  from `eprint_pdf`, `pub_pdf`, `ads_pdf`, or `author_pdf` candidates.
- Source PDF downloads default to `AUTO_RESEARCH_PAPERS_DIR` or
  `/data/fzhou/ar_runtime/papers`.

## Dependencies
- External: `arxiv`, `ads`, `tavily-python`.
- Internal: `auto_research.schemas`.

## Dictionary-level reference
- `QuerySource` — protocol for async source adapters. Method:
  `search(query, *, limit=10)`.
- `ArxivSource.search(query: str, *, limit: int = 10) -> list[SearchResult]` —
  returns normalized arXiv paper results.
- `NasaAdsSource.search(query: str, *, limit: int = 10) ->
  list[SearchResult]` — returns normalized NASA ADS paper results.
- `resolve_ads_full_text(bibcode: str | None, token: str | None) ->
  list[FullTextLink]` — calls
  `https://api.adsabs.harvard.edu/v1/resolver/<bibcode>/esource` and returns
  the available full-text source buttons for a record.
- `WebSource.search(query: str, *, limit: int = 10) -> list[SearchResult]` —
  returns normalized Tavily web results.
- `papers_dir() -> Path` — returns the configured source-paper artifact
  directory.
- `download_pdf(url: str, destination: Path) -> Path` — downloads a PDF unless
  the target already exists.
- `download_and_ingest_result(storage: Storage, result: SearchResult, *,
  target_dir: str | Path | None = None) -> StoredPaper | None` — downloads a
  source PDF when `result.pdf_url` exists, upserts it into storage with source
  metadata, and returns the stored row.

## Changelog
- 2026-05-26 -> 2026-06-12 — Source layer built and stabilized: `QuerySource`,
  arXiv/NASA ADS/Tavily adapters, PDF download/ingest helpers, ADS full-text resolver, and
  ADS 404 degradation for normal no-esource records.
- 2026-06-12 — Compacted changelog and refreshed CLAUDE section link.
