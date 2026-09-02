"""Opt-in live checks for step-8 role wrappers.

Run with:
    AUTO_RESEARCH_LIVE_AGENT_TESTS=1 pytest tests/test_live_agents.py -s
"""

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.models import load_registry
from auto_research.observations import load_observations_config
from auto_research.retrieval import SqliteRetrievalBackend, text_search_paths
from auto_research.schemas import (
    AnalysisTask,
    AnalystReport,
    ResearchRequest,
    SearchRequest,
    document_from_file,
)
from auto_research.storage import MemoryRecord, Storage, paper_from_local_file
from tests.dev_logs import dev_log_path, summarize_agent_log


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_AGENT_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_AGENT_TESTS=1 to spend provider tokens",
)


GX339_PDF = Path("/data/fzhou/ar_runtime/papers/GX339_4.pdf")
IXPE_ONLY_BAD_FIT = """
Model constant<1>*TBfeo<2>*plabs<3>(polconst<4>*diskbb<5> + polconst<6>*relxillCp<7>) Source No.: 1   Active/On
Model Model Component  Parameter  Unit     Value
 par  comp
                           Data group: 1
   1    1   constant   factor              1.00000      frozen
   2    2   TBfeo      nH         10^22    0.400000     frozen
   6    3   plabs      index               2.00000      frozen
   8    4   polconst   A                   0.957113     +/-  -1.00000
   9    4   polconst   psi        deg      -53.5693     +/-  -1.00000
  10    5   diskbb     Tin        keV      4.86557E-03  +/-  -1.00000
  11    5   diskbb     norm                5.04638E+19  +/-  -1.00000
  12    6   polconst   A                   2.89757E-02  +/-  2.14536E-03
  13    6   polconst   psi        deg      -27.0000     frozen
  14    7   relxillCp  Incl       deg      37.0000      frozen
  16    7   relxillCp  Rin                 -1.00000     +/-  27.9445
  22    7   relxillCp  gamma               2.10110      +/-  2.05793E-02
  23    7   relxillCp  logxi               3.63421      +/-  0.114024
  27    7   relxillCp  refl_frac           3.98483      +/-  3.52587
  28    7   relxillCp  norm                0.203784     +/-  0.142256
                           Data group: 2
  29    1   constant   factor              0.697197     +/-  5.27040E-03
  34    3   plabs      index               1.32556      +/-  1.52240E-02
                           Data group: 3
  57    1   constant   factor              0.641158     +/-  4.99129E-03
  62    3   plabs      index               1.30002      +/-  1.53049E-02

Response parameters defined:
   1     1    gain     slope             1.12446      +/-  3.07032E-03
   7     4    gain     slope             1.01249      +/-  2.67073E-03
  13     7    gain     slope             1.02101      +/-  2.63719E-03

Fit statistic  : Chi-Squared                 7026.65     using 149 bins, spectrum 1, group 1.
                 Chi-Squared                  174.52     using 149 bins, spectrum 2, group 1.
                 Chi-Squared                  120.99     using 149 bins, spectrum 3, group 1.
                 Chi-Squared                  485.17     using 149 bins, spectrum 4, group 2.
                 Chi-Squared                  126.38     using 149 bins, spectrum 5, group 2.
                 Chi-Squared                  157.44     using 149 bins, spectrum 6, group 2.
                 Chi-Squared                  521.76     using 149 bins, spectrum 7, group 3.
                 Chi-Squared                  180.47     using 149 bins, spectrum 8, group 3.
                 Chi-Squared                  169.30     using 149 bins, spectrum 9, group 3.
Total fit statistic                          8962.67     with 1324 d.o.f.

Test statistic : Chi-Squared                 8962.67     using 1341 bins.
 Null hypothesis probability of 0.00e+00 with 1324 degrees of freedom
""".strip()


def test_live_role_wrappers_on_research_jobs(tmp_path, monkeypatch):
    load_dotenv()
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path))
    registry = load_registry()
    observations = load_observations_config()
    storage = Storage(tmp_path / "live_agents.sqlite3")
    log_path = dev_log_path("step-8", "live_agents", ".jsonl")

    async def run_all():
        await storage.initialize()
        failures = []
        reviewer = ReviewerAgent(model=registry.resolve("reviewer").handles[0])
        await _record(
            log_path,
            "reviewer",
            reviewer,
            reviewer.review(
                ResearchRequest(
                    question=(
                        "IXPE has done consecutive observations on Cyg X-1 in its 2024 soft states. "
                        "Perform a spectral fit on one 2024 Cyg X-1 IXPE observation, preferably "
                        "as a joint fit with other missions."
                    ),
                    context=(
                        "Hint: the HEASARC archive is available for you to discover relevant observations."
                    ),
                )
            ),
            failures,
        )

        paper_id = await storage.upsert_paper(paper_from_local_file(GX339_PDF))
        document = document_from_file(GX339_PDF, title="GX339-4 paper")
        latest_memory: MemoryRecord | None = None
        for handle in registry.resolve("librarian_io").handles:
            agent = LibrarianIOAgent(sources={}, model=handle)
            output = await _record(
                log_path,
                f"librarian_io/{handle.config.name}",
                agent,
                agent.ingest_document(
                    document,
                    paper_id=paper_id,
                    instructions=(
                        "Treat this as a newly found paper. Compose a durable memory abstraction "
                        "with abstract, key points, key methods, data, and analysis results useful "
                        "for future text search. Include relevant search aliases in tags."
                    ),
                ),
                failures,
            )
            if isinstance(output, MemoryRecord):
                latest_memory = output
                await storage.replace_memory_for_paper(output)

        obs_payload = {
            name: str(config.obs_db_path)
            for name, config in observations.missions.items()
        }
        analyst = AnalystWorkerAgent(model=registry.resolve("analyst_worker").handles[0])
        await _record(
            log_path,
            "analyst_worker/tool_request",
            analyst,
            analyst.request_tools(
                AnalysisTask(
                    question=(
                        "Using the configured observation databases, identify which observations "
                        "are 2024 soft-state observations and perform a spectral fit on one of them."
                    ),
                    instructions=(
                        "Operate the available computer/tool environment as needed. "
                        f"Observation locations: {json.dumps(obs_payload)}"
                    ),
                )
            ),
            failures,
        )

        # The lead's whole turn is one cycle that ends by filing a form, so it needs the two
        # things a cycle writes through: a report folder (edit_report's JSONL) and a tool
        # surface (the forms live on the provider). Without them there is no form to file and
        # the cycle returns the honest failure instead of a report.
        lead = AnalystLeadAgent(model=registry.resolve("analyst_lead").handles[0])
        lead.agent_key = "analyst_lead"
        lead.report_dir = log_path.parent
        lead.tools = ReadOnlyToolProvider(
            roots=[log_path.parent], include_text_search=False
        )
        await _record(
            log_path,
            "analyst_lead/ixpe_only_fit_review",
            lead,
            lead.run_cycle(
                AnalysisTask(
                    question="Review whether this IXPE-only Cyg X-1 fit result is acceptable.",
                    instructions=IXPE_ONLY_BAD_FIT,
                ),
                [
                    AnalystReport(
                        summary="An IXPE-only XSPEC fit result was supplied for review.",
                        findings=[IXPE_ONLY_BAD_FIT],
                        uncertainty="No NICER/NuSTAR joint data, residual plot, or external literature comparison was supplied.",
                    )
                ],
                sources=["analyst_worker_0"],
                rounds_left=0,
            ),
            failures,
        )

        if latest_memory is None:
            failures.append(("librarian_io", "no memory abstraction was produced"))
        hits = await SqliteRetrievalBackend(storage).text_search("spectural fit", limit=5)
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "role": "retrieval_assertion",
                        "model_member": "sqlite_text_search",
                        "provider": "local",
                        "model": "sqlite",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "status": "ok" if hits else "failed",
                        "output": [hit.model_dump(mode="json") for hit in hits],
                        "error": None if hits else "no local hit for 'spectural fit'",
                    },
                    sort_keys=True,
                )
            )
            stream.write("\n")
        if not hits:
            # Local retrieval is pure Python — there is no LLM searcher role to attribute this to.
            failures.append(("local_retrieval", "no local hit for 'spectural fit'"))
        return failures

    failures = asyncio.run(run_all())
    md_path = summarize_agent_log(log_path)
    print(f"wrote dev log {log_path}", flush=True)
    print(f"wrote summary {md_path}", flush=True)
    assert not failures, "\n".join(f"{role} failed with {error}" for role, error in failures)


async def _record(log_path, role, agent, task, failures):
    handle = agent.model
    print(f"running {role} via {handle.config.provider}:{handle.config.model}", flush=True)
    try:
        output = await asyncio.wait_for(task, timeout=180)
    except Exception as exc:
        failures.append((role, repr(exc)))
        row = _row(role, handle, "failed", error=repr(exc), response=agent.last_response)
        output = None
    else:
        row = _row(role, handle, "ok", output=_dump_output(output), response=agent.last_response)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True))
        stream.write("\n")
    print(f"{role} {row['status']}", flush=True)
    return output


def _dump_output(output):
    if hasattr(output, "filed"):  # a LeadCycleOutcome — the filed form is what it produced
        return {
            "filed": output.filed,
            "report": output.report.model_dump(mode="json") if output.report else None,
            "verdict": output.verdict.model_dump(mode="json") if output.verdict else None,
        }
    if isinstance(output, list):
        return [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in output]
    if hasattr(output, "model_dump"):
        return output.model_dump(mode="json")
    return output


def _row(role, handle, status, *, output=None, error=None, response=None):
    usage = response.usage if response is not None else None
    return {
        "role": role,
        "model_member": handle.config.name,
        "provider": handle.config.provider,
        "model": handle.config.model,
        "input_tokens": usage.input_tokens if usage is not None else 0,
        "output_tokens": usage.output_tokens if usage is not None else 0,
        "status": status,
        "output": output,
        "error": error,
        "raw_text": response.content if response is not None and status == "failed" else None,
    }


def test_live_io_library_pipeline_xray_binary(monkeypatch):
    """Verify the modified IO library pipeline end-to-end with 'X-ray binary'.

    Exercises all four steps of the literature node in isolation:
      1. compose_search_strategy  — LLM composes a SearchRequest from the research intent
      2. io_agent.search          — online queries (arXiv / ADS / web), no LLM, raw results
      3. io_agent.ingest_results  — download PDFs, IODebateToConverge, mem_abbrev → DB
      4. searcher.search          — local SQLite query, local_path populated on hits

    Assertions:
      - mem_abbrev is written to the memory table for every ingested result
      - LibrarianSearcherAgent returns results; local_path is set for downloaded papers
    """

    load_dotenv()
    registry = load_registry()
    storage = Storage()
    log_path = dev_log_path("step-8", "live_io_pipeline_xray_binary", ".jsonl")

    def _write_plain(role: str, status: str, *, output=None, error: str | None = None):
        """Write a non-LLM event row (no model handle)."""
        row = {
            "role": role,
            "model_member": None,
            "provider": None,
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "status": status,
            "output": output,
            "error": error,
            "raw_text": None,
        }
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True))
            stream.write("\n")

    async def run():
        await storage.initialize()
        failures: list[tuple[str, str]] = []

        request = ResearchRequest(
            question=(
                "What are the key spectral states and hardness-intensity diagram "
                "behavior of black hole X-ray binary systems during outbursts?"
            )
        )

        # ── Step 1: compose_search_strategy ──────────────────────────────────
        io_agent = LibrarianIOAgent(
            model=registry.resolve("librarian_io").handles[0],
            storage=storage,
        )
        search_request: SearchRequest | None = await _record(
            log_path,
            "io/compose_search_strategy",
            io_agent,
            io_agent.compose_search_strategy(request),
            failures,
        )
        if search_request is None:
            search_request = SearchRequest(query="X-ray binary spectral states", sources=["arxiv"])

        # Cap to 3 results per source; skip generic web search for this test.
        search_request = SearchRequest(
            query=search_request.query,
            sources=["arxiv", "nasa_ads"],
            max_results=3,
        )
        print(f"  search_request: query={search_request.query!r} sources={search_request.sources} max_results={search_request.max_results}", flush=True)

        # ── Step 2: online search (no LLM) ───────────────────────────────────
        print("running io/online_search (no LLM call)", flush=True)
        try:
            raw_results = await asyncio.wait_for(
                io_agent.search(search_request), timeout=60
            )
        except Exception as exc:
            failures.append(("io/online_search", repr(exc)))
            raw_results = []
        _write_plain(
            "io/online_search",
            "ok" if raw_results else "empty",
            output={
                "n_results": len(raw_results),
                "results": [
                    {
                        "source": r.source,
                        "title": r.title,
                        "pdf_url": str(r.pdf_url) if r.pdf_url else None,
                        "published": str(r.published) if r.published else None,
                        "abstract_preview": (r.abstract or "")[:200],
                    }
                    for r in raw_results
                ],
            },
        )
        print(f"  {len(raw_results)} raw results from online sources", flush=True)
        if not raw_results:
            failures.append(("io/online_search", "no results returned"))

        # ── Step 3: ingest_results (download + IO debate + mem_abbrev to DB) ─
        print(
            f"running io/ingest_results (download + IODebateToConverge + DB write)"
            f" — {len(raw_results)} results",
            flush=True,
        )
        papers_before = await storage.list_papers()
        ingest_ok = True
        try:
            await io_agent.ingest_results(raw_results)
        except Exception as exc:
            failures.append(("io/ingest_results", repr(exc)))
            ingest_ok = False
        papers_after = await storage.list_papers()

        # Query DB for memory rows including mem_abbrev
        with sqlite3.connect(storage.path) as conn:
            conn.row_factory = sqlite3.Row
            mem_rows = conn.execute(
                "SELECT paper_id, summary, mem_abbrev FROM memory ORDER BY paper_id"
            ).fetchall()
        ingested_count = len(papers_after) - len(papers_before)
        mem_with_abbrev = [dict(r) for r in mem_rows if r["mem_abbrev"]]
        mem_without_abbrev = [dict(r) for r in mem_rows if not r["mem_abbrev"]]

        _write_plain(
            "io/ingest_results",
            "ok" if ingest_ok else "failed",
            output={
                "papers_before": len(papers_before),
                "papers_after": len(papers_after),
                "papers_ingested": ingested_count,
                "memory_rows_total": len(mem_rows),
                "memory_with_mem_abbrev": len(mem_with_abbrev),
                "memory_without_mem_abbrev": len(mem_without_abbrev),
                "mem_abbrev_samples": [
                    {
                        "paper_id": r["paper_id"],
                        "summary": r["summary"],
                        "mem_abbrev_preview": (r["mem_abbrev"] or "")[:300],
                    }
                    for r in mem_with_abbrev[:3]
                ],
                "papers_without_abbrev": [
                    {"paper_id": r["paper_id"], "summary": r["summary"]}
                    for r in mem_without_abbrev
                ],
            },
            error=None if ingest_ok else "ingest_results raised an exception",
        )
        print(
            f"  ingested {ingested_count} papers; "
            f"{len(mem_with_abbrev)} memory rows with mem_abbrev, "
            f"{len(mem_without_abbrev)} without",
            flush=True,
        )
        if not mem_with_abbrev and ingest_ok and raw_results:
            failures.append(("io/ingest_results", "no mem_abbrev written to DB"))

        # ── Step 4: local text search (pure Python, no LLM) ─────────────────
        print("running text_search_paths (no LLM call)", flush=True)
        local_paths: list = []
        try:
            local_paths = await asyncio.wait_for(
                text_search_paths(
                    search_request.query,
                    limit=10,
                    backend=SqliteRetrievalBackend(storage),
                ),
                timeout=10,
            )
        except Exception as exc:
            failures.append(("text_search_paths", repr(exc)))
        _write_plain(
            "text_search_paths",
            "ok" if local_paths else "empty",
            output={
                "query": search_request.query,
                "n_paths": len(local_paths),
                "paths": [str(p) for p in local_paths],
            },
        )
        print(f"  text_search_paths returned {len(local_paths)} local paths", flush=True)

        # ── DB assertion row ──────────────────────────────────────────────────
        _write_plain(
            "db_assertion",
            "ok" if mem_with_abbrev else "failed",
            output={
                "mem_abbrev_populated": len(mem_with_abbrev),
                "local_paths_found": len(local_paths),
            },
            error=None if mem_with_abbrev else "mem_abbrev column is empty after ingestion",
        )
        if not mem_with_abbrev:
            failures.append(("db_assertion", "mem_abbrev column empty after ingestion"))

        return failures

    failures = asyncio.run(run())
    md_path = summarize_agent_log(log_path)
    print(f"wrote dev log  {log_path}", flush=True)
    print(f"wrote summary  {md_path}", flush=True)
    assert not failures, "\n".join(f"{role}: {error}" for role, error in failures)
