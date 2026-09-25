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
- `receipts.py` — mints the **paper** and **web** receipts every role that reads literature uses
  (P5 of the provenance generalization). A **paper** receipt IS the PDF the model reads natively
  (`path` = `source_path`, `sha256` = `source_sha256`) and is **cited, never text-matched** (human
  ruling 2026-09-24, option A): no text is extracted, a quote of a paper is refused ("no text to
  quote"), and a person checks paper values against the PDF. A **web** receipt's stored text is the Tavily extract, which is
  also what the model reads (`parser="tavily-extract"`; Tavily returns no raw HTML). Both carry
  `title` and persistent `ids`.

Each source module is independent; no source imports another.

## Interfaces / Contracts
- `QuerySource` protocol: `async search(query: str, *, limit: int = 10) ->
  list[SearchResult]`.
- `download_pdf(url, destination) -> Path | None` — cached, PDF-signature-checked download. An
  arXiv PDF URL follows **arXiv's discipline**, read on arXiv's own pages: fetched from
  `export.arxiv.org` ("specifically set aside for programmatic access"; the primary host, never a
  fallback, since switching hosts to escape a refusal is the circumvention arXiv's terms prohibit),
  one at a time and ≥3 s apart process-wide (API terms), a `403` stops arXiv downloads for the rest
  of the process (continued requests after a 403 "will be interpreted as an attack"), a `406`/`429`
  waits 15 s (`robots.txt` crawl delay) and retries once. Headers: `Accept: application/pdf` and a
  nameless `auto_research/<version>` User-Agent (shared repo; no personal contact, human ruling).
  `arxiv_download_url(url) -> str | None` gives the export form of an arXiv `/pdf/` URL.
- `mint_paper_receipt(store, pdf, *, source, title, ids=None, produced_by="read_paper") ->
  Receipt | None` — mint, or return the receipt `source` already minted for this PDF (matched on
  `source_sha256`, so a handle is stable for the run and across a resume). `None` when the file is
  not a PDF (no `%PDF-` signature). A scan with no text layer is as citable as any PDF.
- `mint_web_receipt(store, url, text, *, source, title=None, produced_by="web_fetch") -> Receipt`
  — the same for a page extract (`parser="tavily-extract"`).
- `existing_receipt(store, *, source, kind, source_sha256) -> Receipt | None`.
- `ArxivSource(client: arxiv.Client | None = None)` searches arXiv and returns
  `SearchResult(source="arxiv")`. `search_with_match(query, *, limit) -> (results, match)`:
  bare keywords are sent joined with AND (`all:a AND all:b …`); when no paper matches them all the
  loose query runs (`match="any"`); a query in arXiv field syntax (`au:`, `ti:`, `abs:`, `AND`/`OR`)
  is sent as written (`"as_written"`). `search()` returns just the results.
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
  the available full-text source buttons for a record. A record ADS serves
  malformed — a relative URL where `FullTextLink.url` wants an absolute one, or a
  missing `link_type` — is skipped, and every well-formed link beside it is kept.
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
- 2026-06-18 -> 2026-06-22 — `download_pdf` retries transient failures and returns `None` on ultimate
  failure or a non-`%PDF-` body (`_is_pdf_bytes`; a stale non-PDF cache file is re-fetched).
- 2026-07-13 — [I1] `WebSource`'s Tavily client is lazy (a `client` property); [I10] `WebSource.fetch`
  (Tavily Extract) backs `web_fetch`.
- 2026-08-03 — `resolve_ads_full_text` skips a malformed ADS record (relative URL, missing `link_type`)
  instead of failing the whole source.
- 2026-09-24 — `receipts.py` (P5): paper/web receipt minting, used by the librarian (`lib`) and the
  reviewer (`rev`). [I9] closed: a failing source is a `librarian.source.error` event and an error outcome.
- 2026-09-24 — arXiv search ANDs every term first, loose fallback only on zero results (reported to the
  librarian). arXiv PDFs come from `export.arxiv.org` with pacing, a stop on 403, one retry on 406/429.
- 2026-09-24 — Ruling: papers are cited, never text-matched — the paper receipt is the PDF itself.
  Docling (and `parse_root`) left the librarian's path, then the interim pypdf text layer.
