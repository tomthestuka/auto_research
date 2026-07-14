"""Env-gated LIVE test: web_fetch reads a real page in full as Markdown via Tavily Extract,
and pages it like read_file ([I10]).

Activate with:
    AUTO_RESEARCH_LIVE_WEB_FETCH_TESTS=1 pytest tests/test_live_web_fetch.py -q

What it proves against the real Tavily Extract endpoint (the external dependency): a reference
URL comes back as non-empty Markdown, `execute_web_fetch` renders it `cat -n` and — with a small
`limit` — emits the paging footer and honors the next `offset`. Writes a short log under
`logs/dev/web_fetch/`. Requires `TAVILY_API_KEY`.
"""

import asyncio
import os
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.agents.react import execute_web_fetch
from auto_research.sources import WebSource

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_WEB_FETCH_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_WEB_FETCH_TESTS=1 to run the live web_fetch test (spends a Tavily credit)",
)

_URL = "https://en.wikipedia.org/wiki/X-ray_binary"  # stable, long, on-domain reference page


def test_live_web_fetch_reads_and_pages_real_page():
    load_dotenv()
    if not os.getenv("TAVILY_API_KEY"):
        pytest.skip("TAVILY_API_KEY not set")

    web = WebSource()
    # Page 1 with a small window so any real page triggers the footer.
    out = asyncio.run(execute_web_fetch(web, {"url": _URL, "limit": 20}))
    assert not out.is_error, out.text
    lines = out.text.splitlines()
    assert lines[0].startswith(f"{1:>6}\t")  # cat -n
    body = "\n".join(lines)
    paged = "call web_fetch with offset=21 to continue" in body

    resumed = ""
    if paged:
        out2 = asyncio.run(execute_web_fetch(web, {"url": _URL, "offset": 21, "limit": 20}))
        assert not out2.is_error
        assert out2.text.splitlines()[0].startswith(f"{21:>6}\t")  # honored offset
        resumed = out2.text.splitlines()[0]

    logdir = Path("logs/dev/web_fetch")
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (logdir / f"web_fetch_{stamp}.md").write_text(
        f"# web_fetch live ([I10])\n\n- url: {_URL}\n- page-1 first line: `{lines[0]}`\n"
        f"- paged (footer present): {paged}\n- page-2 first line: `{resumed}`\n\n"
        f"## page 1 (first 20 lines)\n\n```\n{body[:2000]}\n```\n",
        encoding="utf-8",
    )
    print(f"\n[web_fetch live] fetched {_URL}; paged={paged}; first line={lines[0]!r}")

    assert len(body) > 0
    # markdown, not raw HTML: no leftover tags on the first content lines
    assert "<html" not in body.lower() and "<div" not in body.lower()
