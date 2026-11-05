"""Opt-in live retrieval check for step 6.

Run with:
    pytest tests/test_live_retrieval.py --live -s
"""

import asyncio
import json

import pytest
from dotenv import load_dotenv

from auto_research.retrieval import SqliteRetrievalBackend
from tests.dev_logs import dev_log_path, summarize_retrieval_log


pytestmark = pytest.mark.live


def test_live_retrieval_gx339_4():
    load_dotenv()
    query = "GX339-4"
    log_path = dev_log_path("step-6", "live_retrieval_gx339_4", ".jsonl")
    backend = SqliteRetrievalBackend()

    hits = asyncio.run(backend.text_search(query, limit=10))

    with log_path.open("w", encoding="utf-8") as stream:
        for hit in hits:
            stream.write(json.dumps(hit.model_dump(mode="json"), sort_keys=True))
            stream.write("\n")

    for hit in hits:
        print(f"{hit.kind} score={hit.score}: {hit.title} {hit.local_path or ''}", flush=True)

    assert hits
    assert any("GX339" in hit.title or (hit.local_path and "GX339" in str(hit.local_path)) for hit in hits)
    summary_path = summarize_retrieval_log(log_path, query=query)
    print(f"wrote dev log {log_path}", flush=True)
    print(f"wrote summary {summary_path}", flush=True)
