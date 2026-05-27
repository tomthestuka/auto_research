"""Opt-in live source checks for step 5.

Run with:
    AUTO_RESEARCH_LIVE_SOURCE_TESTS=1 pytest tests/test_live_sources.py -s
"""

import asyncio
import json
import os

import pytest
from dotenv import load_dotenv

from auto_research.sources import ArxivSource, NasaAdsSource, WebSource
from auto_research.sources.downloads import download_and_ingest_result, papers_dir
from auto_research.storage import Storage, database_path
from tests.dev_logs import dev_log_path, summarize_source_log


pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_SOURCE_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_SOURCE_TESTS=1 to query external literature sources",
)


def test_live_sources_black_hole_binary():
    load_dotenv()
    query = "black hole binary"
    sources = [
        ("arxiv", ArxivSource()),
        ("nasa_ads", NasaAdsSource()),
        ("web", WebSource()),
    ]
    log_path = dev_log_path("step-5", "live_sources_black_hole_binary", ".jsonl")
    storage = Storage(database_path())

    async def run_all():
        await storage.initialize()
        failures = []
        for name, source in sources:
            print(f"query {name}: {query}", flush=True)
            try:
                results = await asyncio.wait_for(source.search(query, limit=3), timeout=60)
            except Exception as exc:
                failures.append((name, repr(exc)))
                continue
            print(f"{name} returned {len(results)} results", flush=True)
            ingested = []
            if name in {"arxiv", "nasa_ads"}:
                for result in results:
                    paper = await download_and_ingest_result(storage, result, target_dir=papers_dir())
                    if paper is not None:
                        ingested.append(paper.model_dump(mode="json"))
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "source": name,
                            "query": query,
                            "n_results": len(results),
                            "results": [result.model_dump(mode="json") for result in results],
                            "ingested": ingested,
                        },
                        sort_keys=True,
                    )
                )
                stream.write("\n")
            for result in results:
                print(f"- {name}: {result.title}", flush=True)
        return failures

    failures = asyncio.run(run_all())

    assert not failures, "\n".join(f"{name} failed with {error}" for name, error in failures)
    assert log_path.exists()
    summary_path = summarize_source_log(log_path)
    print(f"wrote dev log {log_path}", flush=True)
    print(f"wrote summary {summary_path}", flush=True)
