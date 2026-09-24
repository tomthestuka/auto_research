"""Live: the librarian researches a real request and files a report bound to what it read.

Env-gated (``AUTO_RESEARCH_LIVE_LIBRARIAN=1``) because it spends provider tokens and downloads real
papers. The librarian is built the way ``graph/runtime.py`` builds it — no model passed, so its
seat resolves through the configuration pointer (``--seats`` / ``AUTO_RESEARCH_SEATS``) — with its
real prompt, real sources, the real Docling parse and a real receipt store.

What is asserted is the harness's side of the contract, per actor capability rather than stage
coverage: the loop left through ``turn_end``; it actually read at least one paper in full; every
handle the report cites is a receipt it minted, whose bytes still hash to the record; and the
binding check passes when re-run from a *fresh* store over the same directory (the resume path).
What the librarian concluded is recorded beside the run, never asserted.

Artifacts (monitor ledger, report JSONL, the receipt store with its text blobs) land under
``logs/dev/librarian/<timestamp>/`` before any assertion runs, so a failure leaves its evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_LIBRARIAN") != "1",
    reason="set AUTO_RESEARCH_LIVE_LIBRARIAN=1 to spend provider tokens on a librarian research loop",
)

load_dotenv()

QUESTION = (
    "What X-ray polarization degree and angle has IXPE reported for the black-hole X-ray binary "
    "Cygnus X-1 in its hard state, and how do the published analyses compare?"
)


def test_live_librarian_researches_and_files_a_bound_report():
    from auto_research.agents.roles import LibrarianIOAgent
    from auto_research.monitor import RunMonitor
    from auto_research.observability.costs import install_cost_meter
    from auto_research.provenance import ReceiptStore, check_bindings
    from auto_research.schemas import ResearchRequest
    from auto_research.storage import Storage

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = Path("logs") / "dev" / "librarian" / stamp
    folder.mkdir(parents=True, exist_ok=True)

    async def run():
        monitor = RunMonitor(chatter=5, jsonl_path=folder / "monitor.jsonl")
        meter = install_cost_meter(run_id=monitor.run_id, monitor=monitor)
        monitor.cost_meter = meter  # as run_research does: every ledger line carries the running cost
        storage = Storage(folder / "knowledge.sqlite3")
        await storage.initialize()
        agent = LibrarianIOAgent(storage=storage, monitor=monitor)
        agent.agent_key = "librarian_io"
        agent.report_dir = folder / "librarian_io"
        agent.receipts = ReceiptStore.for_run(folder)
        report = await agent.research(ResearchRequest(question=QUESTION))
        return agent, report, meter

    agent, report, meter = asyncio.run(run())

    # Evidence first: what the librarian said, what it read, what it cost.
    (folder / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (folder / "summary.md").write_text(
        "\n".join(
            [
                f"# Librarian live run {stamp}",
                f"- model: {getattr(getattr(agent.model, 'config', None), 'model', '?')}",
                f"- cost: ${meter.total_usd:.2f}",
                f"- status: {report.status}",
                f"- documents read: {len(agent.last_receipts)}",
                "",
                "## Summary",
                report.summary,
                "",
                "## Findings",
                *(f"- {f.claim}  ({', '.join(f.support)})" for f in report.findings),
                "",
                "## Gaps",
                *(f"- {g}" for g in report.gaps),
                "",
                "## Documents",
                *(f"- [{h}] {r.title} {json.dumps(r.ids)}" for h, r in agent.last_receipts.items()),
            ]
        ),
        encoding="utf-8",
    )
    print(f"librarian live artifacts: {folder}  cost ${meter.total_usd:.2f}", flush=True)

    # The loop left through turn_end: a report was written by the librarian and persisted.
    assert (folder / "librarian_io" / "literature_pass1.jsonl").is_file()
    assert report.status in ("succeeded", "failed")

    # Capability: it read at least one paper in full (a receipt exists only after a real open).
    papers = [r for r in agent.last_receipts.values() if r.kind == "paper"]
    assert papers, "the librarian filed without reading a single paper in full"

    # Every cited handle is a receipt it minted, and the bytes still match the record.
    # A second instance over the same root, replayed from receipts.jsonl: the resume path.
    fresh = ReceiptStore(ReceiptStore.for_run(folder).root)
    for finding in report.findings:
        for handle in finding.support:
            assert handle in agent.last_receipts, f"{handle} was never opened"
            assert fresh.verify(handle) is None
        opened = [h for h in finding.support if h in agent.last_receipts]
        problems = check_bindings(
            finding.claim, fresh, own_source="lib", scope=opened, label="finding"
        ).problems
        assert not problems, problems
