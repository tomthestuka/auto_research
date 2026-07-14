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
  `SearchResult(source="web")`. The `TavilyClient` is built **lazily** on first
  `search` (exposed via the `client` property), reading `TAVILY_API_KEY` then — so
  constructing a `WebSource` never touches the key. This lets every ReAct role carry a
  `web_search` tool unconditionally (`TAVILY_API_KEY` is a launch preflight requirement)
  and keeps the deterministic suite key-independent. Pass `client` to inject a fake.
- Live source verification is opt-in:
  `AUTO_RESEARCH_LIVE_SOURCE_TESTS=1 pytest tests/test_live_sources.py -s`.
  It queries `black hole binary` against arXiv, NASA ADS, and Tavily, and writes
  timestamped JSONL and Markdown summary logs under `logs/dev/step-5/`.
- arXiv results include `pdf_url` and can be downloaded/ingested into storage.
  NASA ADS results call the ADS resolver service for `esource` links, the API
  equivalent of the UI's "Full Text Sources" buttons. Resolved source buttons are
  stored in `SearchResult.full_text_links`; the best PDF-like link is selected
  from `eprint_pdf`, `pub_pdf`, `ads_pdf`, or `author_pdf` candidates.
- Source PDF downloads default to the `papers_dir` key in `config/orchestration.yaml`,
  which itself defaults to `<runtime_dir>/papers`.

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
  returns normalized Tavily web results (query-aware snippets; `search_depth="basic"`).
- `WebSource.fetch(url: str, *, depth: str = "advanced") -> str` — reads one page's full
  content as Markdown via the Tavily **Extract** endpoint (`format` defaults markdown;
  `depth` `"advanced"` pulls tables/embedded content, `"basic"` is faster). Raises when the
  URL yields no content (empty `results` / a `failed_results` entry) so the caller degrades
  to an honest error. Backs the agents' `web_fetch` tool.
- `papers_dir() -> Path` — re-exported from `auto_research.env`; returns the configured
  source-paper artifact directory (`orchestration.yaml: papers_dir`, defaulting to
  `<runtime_dir>/papers`).
- `download_pdf(url: str, destination: Path) -> Path | None` — downloads a PDF
  unless a valid one already exists. Transient fetch failures (a momentary 404, a
  network blip, a timeout) are retried with linear backoff (`_DOWNLOAD_ATTEMPTS`
  tries); returns `None` when the download ultimately fails so callers degrade to
  metadata-only ingest instead of raising. The fetched bytes are checked with
  `_is_pdf_bytes` (the `%PDF-` signature): a non-PDF response (e.g. an HTML
  landing/paywall page from an ADS `esource` link) returns `None` rather than being
  stored, and a previously stored file is reused only when it is itself a valid PDF
  (a stale non-PDF is removed and re-fetched).
- `_is_pdf_bytes(data: bytes) -> bool` — True when `b"%PDF-"` appears in the first
  1 KB; the guard that keeps HTML-saved-as-`.pdf` out of the document-ingest path.
- `download_and_ingest_result(storage: Storage, result: SearchResult, *,
  target_dir: str | Path | None = None) -> StoredPaper | None` — downloads a
  source PDF when `result.pdf_url` exists, upserts it into storage with source
  metadata, and returns the stored row. Returns `None` when `result.pdf_url` is
  absent **or** the download fails, so a single bad/transient PDF URL never aborts
  ingest (the caller falls back to metadata-only).

## Changelog
- 2026-05-26 -> 2026-06-12 — Source layer built: `QuerySource`, arXiv/NASA ADS/Tavily adapters,
  PDF download/ingest helpers, ADS full-text resolver, ADS 404 degradation for no-esource records.
- 2026-06-18 — `download_pdf` retries transient fetch failures and returns `None` (instead of
  raising) on ultimate failure, and `download_and_ingest_result` propagates it, so a single bad
  PDF URL no longer crashes the `literature` graph node.
- 2026-06-22 — `download_pdf` validates content via the `%PDF-` signature (`_is_pdf_bytes`):
  a non-PDF response returns `None` and a stale non-PDF cache file is discarded/re-fetched,
  keeping HTML-saved-as-`.pdf` out of ingest.
- 2026-07-13 — `WebSource` Tavily client is now lazy ([I1], built on first use behind a `client`
  property so construction no longer reads the key and `web_search` attaches to every role
  unconditionally) and gains `WebSource.fetch` ([I10], a page-read primitive over the Tavily
  Extract endpoint backing the agents' `web_fetch` tool).
