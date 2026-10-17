"""The librarian's tools: search, read in full, write the report, end the turn.

The librarian is one ReAct loop that ends only through ``turn_end``: the same shape as every other
graph role. This provider is its whole tool surface:

- ``search_literature`` — one query against one source; hits come back numbered and are **leads,
  not evidence**: an abstract teaser is never what a finding rests on.
- ``read_paper`` — fetch a hit's PDF (or open a knowledge-base paper) and attach it to the model's
  context, where every vendor reads it natively and it stays for the rest of the loop. The open
  mints a paper receipt in the run's store (:mod:`auto_research.sources.receipts`) that cites the
  PDF itself and assigns the handle (``lib:p1``, ``lib:p2`` …). A paper is cited, never text-matched.
- ``web_fetch`` — the same for a web page (``lib:w1`` …).
- ``text_search`` — search the local knowledge base of papers earlier runs already read.
- ``edit_report`` / ``review_report`` — write and read back the ``LiteratureReport``.
- ``turn_end`` — submit it; the validator rejects with a reason and the loop continues.

The model never names a file it writes and never types a reference: the report cites handles,
the handles resolve to receipts this loop minted, and ``turn_end`` runs the shared binding check
(:func:`auto_research.provenance.check_bindings`) over every prose field.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from auto_research.agents.react import (
    EDIT_REPORT_TOOL,
    REVIEW_REPORT_TOOL,
    TURN_END_TOOL,
    ToolOutcome,
    execute_turn_end,
    turn_end_tool_def,
)
from auto_research.provenance import ReceiptStore, ScopedLookup, check_bindings, paper_attributions
from auto_research.schemas.artifacts import Receipt
from auto_research.agents.paper_tools import READ_PAPER_TOOL, PaperReader, format_ids
from auto_research.schemas.artifacts import PaperAttribution
from auto_research.schemas.literature import LiteratureFinding, LiteratureReport
from auto_research.schemas.search import SearchResult
from auto_research.sources.receipts import mint_paper_receipt, mint_web_receipt

SEARCH_LITERATURE_TOOL = "search_literature"
WEB_FETCH_TOOL = "web_fetch"
TEXT_SEARCH_TOOL = "text_search"

#: Tools that write or read back the librarian's own report, or submit it: report authoring, not
#: evidence gathering, so they cost no tool budget (the same ruling as the analysts').
LIBRARIAN_FREE_TOOLS = frozenset({EDIT_REPORT_TOOL, REVIEW_REPORT_TOOL, TURN_END_TOOL})

#: Characters of a web page shown per read. A page ends at a line break, so a number is never cut
#: in two across pages; the quote check runs against the whole text regardless.
READ_WINDOW_CHARS = 24_000

#: The identifier keys a search result carries, mapped to a receipt's persistent-id keys.
_ID_KEYS = {"bibcode": "bibcode", "arxiv_id": "arxiv", "doi": "doi"}

Recorder = Callable[..., Awaitable[None]]


class LibrarianToolProvider:
    """Tool surface for one librarian loop, and the catalog of what that loop has read.

    ``captures`` maps each handle this loop opened to its receipt; it is the only thing a report may
    cite, and what the reviewer receives beside the report. The receipts themselves live in the
    run's shared ``store``. ``hits`` are the numbered search results the model reads from.
    ``paper_ids`` links a paper receipt to its knowledge-base row, so the report's findings can be
    laid down as memory on the papers they cite.
    """

    def __init__(
        self,
        sources: Mapping[str, Any],
        *,
        storage: Any,
        store: ReceiptStore,
        report_path: Path,
        papers_dir: Path,
        web_source: Any | None = None,
        retrieval: Any | None = None,
        source_key: str = "lib",
        record: Recorder | None = None,
        downloader: Callable[[str, Path], Path | None] | None = None,
        paper_minter: Callable[..., Receipt | None] | None = None,
    ) -> None:
        self.sources = dict(sources)
        self.storage = storage
        self.store = store
        self.report_path = report_path
        self.papers_dir = papers_dir
        self.web_source = web_source if web_source is not None else self.sources.get("web")
        self.retrieval = retrieval
        self.source_key = source_key
        self._record_event = record
        if downloader is None:
            from auto_research.sources.downloads import download_pdf

            downloader = download_pdf
        self._download = downloader
        # What this librarian may cite: its own receipts, plus papers another role receipted first
        # and the reader admitted by reuse. Never a receipt from elsewhere in the run.
        self.scope = ScopedLookup(store, own_source=source_key)
        # The shared paper reader (agents/paper_tools.py): native PDF, one receipt per paper for the
        # whole run, admitted into this scope.
        self.reader = PaperReader(
            store, self.scope, source=source_key, storage=storage,
            minter=paper_minter or mint_paper_receipt,
            unreadable_action="record it among the report's gaps",
        )
        self.hits: list[SearchResult] = []
        self.captures: dict[str, Receipt] = {}
        self.paper_ids: dict[str, int] = {}
        self.written_report: LiteratureReport | None = None

    # ------------------------------------------------------------------ definitions

    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": SEARCH_LITERATURE_TOOL,
                "description": (
                    "Search one literature source and get back numbered hits (title, year, "
                    "identifiers, whether a PDF is available, an abstract teaser). Hits are leads "
                    "for what to read: open one with read_paper(hit=N) before you rely on it. "
                    "Sources: " + ", ".join(self.sources) + "."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": list(self.sources),
                            "description": "The source to query.",
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "A short keyword query of 3–6 distinctive terms. arXiv returns "
                                "papers matching all of them when any do. Each source also takes "
                                "its own field syntax, to find a known paper: arXiv "
                                "'au:Smith AND abs:\"quasar lag\"'; NASA ADS "
                                "'author:\"Smith\" year:2021 abs:\"quasar lag\"'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of hits (default 10).",
                        },
                    },
                    "required": ["source", "query"],
                },
            },
            PaperReader.definition(
                extra_properties={"hit": {"type": "integer", "description": "A search hit number."}},
                lead="Open a search hit with hit=N (its PDF is downloaded). ",
            ),
            {
                "name": WEB_FETCH_TOOL,
                "description": (
                    "Read one web page's full text, one page at a time: pass url to open it, or "
                    "handle='lib:wK' with the offset the previous page named to continue. The "
                    "first open assigns the page its handle; list it in a finding's support for "
                    "what the page says in words — a method, a convention, a tool's behaviour, a "
                    "known issue. A value on a page points to the paper that published it: open "
                    "that paper and attribute the value there. Use it for documentation, code, "
                    "data-product notes, discussions, and a paper whose PDF is not downloadable."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The page to open."},
                        "handle": {"type": "string", "description": "A page you already opened."},
                        "offset": {
                            "type": "integer",
                            "description": "Character offset to read from (default 0).",
                        },
                    },
                },
            },
            {
                "name": TEXT_SEARCH_TOOL,
                "description": (
                    "Search the local knowledge base of papers earlier runs already read; returns "
                    "their titles and paths. Open one with read_paper(path=...)."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keywords to match."},
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of papers (default 5).",
                        },
                    },
                    "required": ["query"],
                },
            },
            _edit_report_tool_def(),
            {
                "name": REVIEW_REPORT_TOOL,
                "description": (
                    "Read back your current report draft and the list of documents you can cite "
                    "(every paper and page you opened, by handle). Free: costs no tool budget."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            turn_end_tool_def(),
        ]

    # ------------------------------------------------------------------ dispatch

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == SEARCH_LITERATURE_TOOL:
            return await self._search(arguments)
        if name == READ_PAPER_TOOL:
            return await self._read_paper(arguments)
        if name == WEB_FETCH_TOOL:
            return await self._web_fetch(arguments)
        if name == TEXT_SEARCH_TOOL:
            return await self._text_search(arguments)
        if name == EDIT_REPORT_TOOL:
            return self._edit_report(arguments)
        if name == REVIEW_REPORT_TOOL:
            return ToolOutcome(text=self._review_text())
        if name == TURN_END_TOOL:
            return await execute_turn_end(self.turn_problems, arguments)
        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)

    # ------------------------------------------------------------------ search

    async def _search(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        source_name = str(arguments.get("source", ""))
        source = self.sources.get(source_name)
        if source is None:
            return ToolOutcome(text=f"unknown source: {source_name}", is_error=True)
        query = str(arguments.get("query", ""))
        limit = _int(arguments.get("limit"), 10)
        match = None
        try:
            if hasattr(source, "search_with_match"):
                results, match = await source.search_with_match(query, limit=limit)
            else:
                results = await source.search(query, limit=limit)
        except Exception as exc:  # noqa: BLE001 — one source failing is a note, never a dead loop
            # [I9]: a failing source is recorded, not swallowed, so a spent key or quota shows up
            # in the run record instead of silently zeroing that source.
            await self._record(
                "librarian.source.error", level=3,
                summary=f"source '{source_name}' failed",
                data={"source": source_name, "query": query, "error": repr(exc)},
            )
            return ToolOutcome(
                text=f"source '{source_name}' failed: {type(exc).__name__}: {exc}", is_error=True
            )
        if not results:
            return ToolOutcome(text="(no results)")
        lines = []
        if match == "any":
            lines.append(
                "(No paper matched all of your terms; these match some of them. Use fewer, more "
                "distinctive terms, or field syntax, for a tighter match.)"
            )
        for result in results:
            self.hits.append(result)
            lines.append(_hit_line(len(self.hits), result))
        return ToolOutcome(text="\n".join(lines))

    async def _text_search(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        from auto_research.retrieval.backend import SqliteRetrievalBackend

        backend = self.retrieval or SqliteRetrievalBackend(self.storage)
        try:
            hits = await backend.text_search(
                str(arguments.get("query", "")),
                limit=_int(arguments.get("limit"), 5),
                require_local_path=True,
            )
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            return ToolOutcome(
                text=f"local text_search failed: {type(exc).__name__}: {exc}", is_error=True
            )
        lines = [f"- {hit.title} — path: {hit.local_path}" for hit in hits if hit.local_path]
        return ToolOutcome(text="\n".join(lines) or "(no local matches)")

    # ------------------------------------------------------------------ reading

    async def _read_paper(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        """``hit=N`` downloads a search hit; ``path=``/``handle=`` are the shared reader's own."""
        if arguments.get("hit") is None:
            if arguments.get("handle"):
                outcome, receipt = self.reader.open_handle(str(arguments["handle"]).strip())
            elif arguments.get("path"):
                pdf = Path(str(arguments["path"])).expanduser()
                # A paper receipt is evidence: only a PDF in the papers folder (where downloads land,
                # outside every role's write reach) becomes one, never a file a run wrote.
                if not pdf.resolve().is_relative_to(Path(self.papers_dir).resolve()):
                    return ToolOutcome(text=f"read_paper: open papers by hit= or handle=; {pdf} is not in the "
                                            "papers folder.", is_error=True)
                if not pdf.is_file():
                    return ToolOutcome(text=f"read_paper: {pdf} is not a file.", is_error=True)
                outcome, receipt = await self.reader.open_pdf(pdf)
                if receipt is not None and receipt.handle not in self.paper_ids:
                    await self._store_paper(receipt.handle, pdf, None)
            else:
                return ToolOutcome(text="read_paper: pass hit=N, path=... or handle=....", is_error=True)
            return self._opened(outcome, receipt)

        number = _int(arguments.get("hit"), 0)
        if not 1 <= number <= len(self.hits):
            return ToolOutcome(
                text=f"read_paper: no hit {number} (hits so far: 1–{len(self.hits)}).", is_error=True
            )
        result = self.hits[number - 1]
        pdf = await asyncio.to_thread(self._fetch_pdf, result)
        if pdf is None:
            return ToolOutcome(
                text=(
                    f"read_paper: no downloadable PDF for hit {number} ({result.title}). "
                    + (f"Try web_fetch on {result.url}, or " if result.url else "")
                    + "record it among the report's gaps."
                ),
                is_error=True,
            )
        outcome, receipt = await self.reader.open_pdf(pdf, title=result.title, ids=_ids_of(result))
        if receipt is not None and receipt.handle not in self.paper_ids:
            await self._store_paper(receipt.handle, pdf, result)
        return self._opened(outcome, receipt)

    def _opened(self, outcome: ToolOutcome, receipt: Receipt | None) -> ToolOutcome:
        if receipt is not None:
            self.captures.setdefault(receipt.handle, receipt)
        return outcome

    async def _web_fetch(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        offset = _int(arguments.get("offset"), 0)
        handle = str(arguments.get("handle") or "").strip()
        if handle:
            receipt = self.captures.get(handle)
            if receipt is None or receipt.kind != "web":
                return ToolOutcome(text=f"web_fetch: no page opened as '{handle}'.", is_error=True)
            return ToolOutcome(text=self._page(receipt, offset, WEB_FETCH_TOOL))
        url = str(arguments.get("url") or "").strip()
        if not url:
            return ToolOutcome(text="web_fetch: pass url=... or handle='lib:wK'.", is_error=True)
        if self.web_source is None or not hasattr(self.web_source, "fetch"):
            return ToolOutcome(text="web_fetch is not available in this run.", is_error=True)
        try:
            text = await self.web_source.fetch(url)
        except Exception as exc:  # noqa: BLE001 — a page that will not load is a note
            await self._record(
                "librarian.web_fetch.error", level=3, summary=f"fetch failed: {url[:80]}",
                data={"url": url, "error": repr(exc)},
            )
            return ToolOutcome(text=f"web_fetch failed for {url}: {exc}", is_error=True)
        if not text.strip():
            return ToolOutcome(text=f"web_fetch: {url} returned no text.", is_error=True)
        receipt = await asyncio.to_thread(
            mint_web_receipt, self.store, url, text, source=self.source_key
        )
        self.captures.setdefault(receipt.handle, receipt)
        return ToolOutcome(text=self._page(receipt, offset, WEB_FETCH_TOOL))

    def _fetch_pdf(self, result: SearchResult) -> Path | None:
        """Download the result's PDF, trying its PDF link first and then its full-text links."""
        from auto_research.storage.database import safe_filename

        source_id = next(
            (result.identifiers[k] for k in ("arxiv_id", "bibcode", "doi") if result.identifiers.get(k)),
            result.title,
        )
        destination = self.papers_dir / safe_filename(f"{result.source}_{source_id}")
        if destination.is_file():
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        urls = [str(result.pdf_url)] if result.pdf_url else []
        urls += [str(link.url) for link in result.full_text_links if str(link.url) not in urls]
        for url in urls:
            path = self._download(url, destination)
            if path is not None:
                return path
        return None

    async def _store_paper(self, handle: str, pdf: Path, result: SearchResult | None) -> None:
        """Lay the paper down in the knowledge base so later runs can find it."""
        from auto_research.storage.database import paper_from_local_file, paper_from_search_result

        try:
            record = (
                paper_from_search_result(result, pdf)
                if result is not None
                else paper_from_local_file(pdf)
            )
            self.paper_ids[handle] = await self.storage.upsert_paper(record)
        except Exception as exc:  # noqa: BLE001 — the knowledge base is a convenience, not the record
            await self._record(
                "librarian.store_paper.error", level=3, summary=f"could not store {handle}",
                data={"handle": handle, "error": repr(exc)},
            )

    # ------------------------------------------------------------------ report

    def _edit_report(self, arguments: Mapping[str, Any]) -> ToolOutcome:
        # Only what the librarian writes; `attributed_values` is the runtime's, set at filing.
        fields = {k: arguments[k] for k in ("summary", "findings", "gaps", "status") if k in arguments}
        try:
            report = LiteratureReport.model_validate(fields)
        except ValidationError as exc:
            return ToolOutcome(
                text=f"edit_report: the report did not validate — fix and call again: {exc}",
                is_error=True,
            )
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(report.model_dump_json() + "\n", encoding="utf-8")
        self.written_report = report
        unknown = sorted(
            {h for f in report.findings for h in f.support if h not in self.captures}
        )
        text = f"Report saved: {len(report.findings)} finding(s), {len(report.gaps)} gap(s)."
        if unknown:
            text += (
                f" These handles are not documents you opened: {', '.join(unknown)}. "
                "Call review_report to see what you can cite."
            )
        return ToolOutcome(text=text)

    def _review_text(self) -> str:
        parts = []
        report = self.written_report
        if report is None:
            parts.append("You have not written a report yet — call edit_report to write one.")
        else:
            parts.append(f"Your current report draft:\n\nsummary: {report.summary}")
            for n, finding in enumerate(report.findings, 1):
                parts.append(f"finding {n}: {finding.claim}\n  support: {', '.join(finding.support)}")
            for gap in report.gaps:
                parts.append(f"gap: {gap}")
            parts.append(f"status: {report.status}")
        parts.append("--- documents you can cite ---")
        if self.captures:
            parts.extend(f"[{h}] {r.title}  ({_ids_text(r.ids)})" for h, r in self.captures.items())
        else:
            parts.append("(none opened yet)")
        return "\n".join(parts)

    def turn_problems(self) -> list[str]:
        """What stops this librarian's turn from ending, each stated so the model can fix it."""
        report = self.written_report
        if report is None:
            return [
                "No report has been written. Call edit_report to write it — that call is the only "
                "way your report is saved, and nothing is composed on your behalf."
            ]
        problems: list[str] = []
        if not report.summary.strip():
            problems.append("The report's summary is empty.")
        if report.status == "succeeded" and not report.findings:
            problems.append(
                "The report declares success but lists no findings. State what the literature "
                "establishes, or declare 'failed' and say in the summary and gaps what blocked you."
            )
        for n, finding in enumerate(report.findings, 1):
            if not finding.claim.strip():
                problems.append(f"Finding {n} has no claim.")
            if not finding.support:
                problems.append(
                    f"Finding {n} cites no document. Name the handle(s) of what you read that "
                    "supports it, or remove it."
                )
            unopened = [h for h in finding.support if h not in self.captures]
            for handle in unopened:
                problems.append(
                    f"Finding {n} cites '{handle}', which is not a document you opened. "
                    "Call review_report to see the handles you can cite."
                )
        problems.extend(self._binding_problems(report))
        return problems

    def _binding_problems(self, report: LiteratureReport) -> list[str]:
        """The shared provenance check over every prose field (P0/P2).

        Every handle must resolve in this librarian's scope and its bytes must still match. A paper
        is cited, never text-matched (human ruling 2026-09-24), so a value taken from a paper is
        written as an **attribution** with its place (``{{lib:p3 "12.5 ± 3.1 days" Table 2}}``),
        unverified here and collected for a person to check (:meth:`attributed_values`). Every
        number outside a binding is checked as usual against the web extracts cited — a paper
        grounds nothing, so a paper citation never exempts the numbers beside it. Gaps state what
        was not found, so their citations are checked and their numbers are not.
        """
        problems: list[str] = []
        for n, finding in enumerate(report.findings, 1):
            opened = [h for h in finding.support if h in self.captures]
            problems.extend(check_bindings(
                finding.claim, self.scope, own_source=self.source_key, support=opened,
                label=f"Finding {n}",
            ).problems)
        cited_anywhere = [h for f in report.findings for h in f.support if h in self.captures]
        problems.extend(check_bindings(
            report.summary, self.scope, own_source=self.source_key,
            scope=list(dict.fromkeys(cited_anywhere)), label="The summary",
        ).problems)
        for n, gap in enumerate(report.gaps, 1):
            problems.extend(check_bindings(
                gap, self.scope, own_source=self.source_key, exempt_spans=[(0, len(gap))],
                label=f"Gap {n}",
            ).problems)
        return problems

    def attributed_values(self, report: LiteratureReport) -> list[PaperAttribution]:
        """Every value the report attributes to a paper, once each, for the verify-against-the-PDF list."""
        values: list[PaperAttribution] = []
        for text in (report.summary, *(f.claim for f in report.findings)):
            for item in paper_attributions(text, self.scope, own_source=self.source_key):
                if item not in values:
                    values.append(item)
        return values

    def _page(self, receipt: Receipt, offset: int, tool: str) -> str:
        """One page of a receipt's reading copy, ending at a line break, with where to continue."""
        text, changed = self.store.text_with_note(receipt.handle)
        text = (f"{changed}\n" if changed else "") + (text or "")
        total = len(text)
        start = max(0, min(offset, total))
        end = min(total, start + READ_WINDOW_CHARS)
        if end < total:
            cut = text.rfind("\n", start, end)
            if cut > start:
                end = cut + 1
        header = f"[{receipt.handle}] {receipt.title} — characters {start}–{end} of {total}"
        if end < total:
            footer = f"\n[continue with {tool}(handle='{receipt.handle}', offset={end})]"
        else:
            footer = "\n[end of document]"
        return f"{header}\n\n{text[start:end]}{footer}"

    async def _record(self, event: str, **kwargs: Any) -> None:
        if self._record_event is not None:
            await self._record_event(event, **kwargs)


# ---------------------------------------------------------------------------- helpers


def _edit_report_tool_def() -> dict:
    """``edit_report`` over a ``LiteratureReport``, every description read from the schema itself.

    Written out rather than ``model_json_schema()``: that emits ``$ref``/``$defs`` for the nested
    finding, a shape vendors resolve inconsistently, and defaults the model has no use for. The
    prose still has one source — the ``Field(description=...)`` the handler validates back into —
    so what the model is told and what is checked cannot drift.
    """
    report, finding = LiteratureReport.model_fields, LiteratureFinding.model_fields
    return {
        "name": EDIT_REPORT_TOOL,
        "description": (
            "Write (or rewrite) your report — this is the ONLY way it is saved. Provide the whole "
            "report each call; a call overwrites the last, so refine across calls. Each finding "
            "cites the handles of the documents you read that support it. Free: costs no tool budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": report["summary"].description},
                "findings": {
                    "type": "array",
                    "description": report["findings"].description,
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string", "description": finding["claim"].description},
                            "support": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": finding["support"].description,
                            },
                        },
                        "required": ["claim", "support"],
                    },
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": report["gaps"].description,
                },
                "status": {
                    "type": "string",
                    "enum": ["succeeded", "failed"],
                    "description": report["status"].description,
                },
            },
            "required": ["summary", "status"],
        },
    }


def _hit_line(number: int, result: SearchResult) -> str:
    year = result.published.year if result.published else "n.d."
    ids = _ids_text(_ids_of(result))
    pdf = "PDF available" if (result.pdf_url or result.full_text_links) else "no PDF link"
    teaser = (result.abstract or "").strip().replace("\n", " ")[:300]
    return f"[hit {number}] {result.title} ({result.source}, {year}; {ids}; {pdf})\n  {teaser}"


def _ids_of(result: SearchResult) -> dict[str, str]:
    ids = {_ID_KEYS[k]: v for k, v in result.identifiers.items() if k in _ID_KEYS and v}
    if result.url:
        ids["url"] = str(result.url)
    return ids


def _ids_text(ids: Mapping[str, str]) -> str:
    return format_ids(ids) or "no identifiers"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
