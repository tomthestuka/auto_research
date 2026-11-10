"""Live: the librarian researches a real request and files a report bound to what it read.

Opt-in (``--live``) because it spends provider tokens and downloads real
papers. The librarian is built the way ``graph/runtime.py`` builds it — no model passed, so its
seat resolves through the configuration pointer (``--seats`` / ``AUTO_RESEARCH_SEATS``), the run's
workspace (``create_run_workspace``), ``attach_librarian_runtime`` (key, report folder, the run's one
receipt store, stop channel) and the quick channel (``attach_channel``) — with its real prompt, real
sources, the real Docling parse.

What is asserted is the harness's side of the contract, per actor capability rather than stage
coverage: the loop left through ``turn_end`` and its report was persisted; it actually read at least
one paper in full; every support handle is a receipt it opened, whose bytes still hash to the
record; and production's own ``turn_end`` validator (``LibrarianToolProvider.turn_problems``) still
accepts the report when run over a *fresh* store replayed from disk (the resume path). What the
librarian concluded is recorded beside the run, never asserted.

The monitor ledger, the report and a summary land under ``logs/dev/librarian/<timestamp>/`` before
any assertion runs, so a failure leaves its evidence; the summary names the run folder and the
ledger folder (report JSONL, receipt store) in the configured runtime dir. The knowledge base is a
private SQLite file in that ``logs/dev`` folder: a test never writes the human's knowledge base.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.live

load_dotenv()

QUESTION = (
    "What X-ray polarization degree and angle has IXPE reported for the black-hole X-ray binary "
    "Cygnus X-1 in its hard state, and how do the published analyses compare?"
)


def test_live_librarian_researches_and_files_a_bound_report():
    from auto_research.agents.librarian_tools import LibrarianToolProvider
    from auto_research.agents.roles import LibrarianIOAgent
    from auto_research.graph.runtime import attach_channel, attach_librarian_runtime
    from auto_research.monitor import RunMonitor
    from auto_research.observability.costs import install_cost_meter
    from auto_research.provenance import ReceiptStore
    from auto_research.run_control import RunControl
    from auto_research.sandbox.workspace import create_run_workspace, ledger_dir, report_record_path, run_dir
    from auto_research.sources.downloads import papers_dir
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
        # Wired by the run path's own seams, never restated here.
        control = RunControl()
        create_run_workspace(monitor.run_id, ["librarian_io"])
        attach_librarian_runtime(agent, run_id=monitor.run_id, run_control=control)
        attach_channel((agent,), run_id=monitor.run_id, run_control=control, monitor=monitor)
        report = await agent.research(ResearchRequest(question=QUESTION))
        return agent, report, meter, monitor.run_id, storage

    agent, report, meter, run_id, storage = asyncio.run(run())

    # Evidence first: what the librarian said, what it read, what it cost.
    (folder / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    (folder / "summary.md").write_text(
        "\n".join(
            [
                f"# Librarian live run {stamp}",
                f"- model: {getattr(getattr(agent.model, 'config', None), 'model', '?')}",
                f"- run dir: {run_dir(run_id)}",
                f"- ledger dir (report JSONL, receipt store): {ledger_dir(run_id)}",
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
    assert report_record_path(agent.report_dir, "literature_pass1.jsonl").is_file()

    # Capability: it read at least one paper in full (a receipt exists only after a real open).
    papers = [r for r in agent.last_receipts.values() if r.kind == "paper"]
    assert papers, "the librarian filed without reading a single paper in full"

    # Every cited handle is a receipt it minted, and the bytes still match the record.
    # A second instance over the same root, replayed from receipts.jsonl: the resume path.
    fresh = ReceiptStore(agent.receipts.root)
    for finding in report.findings:
        for handle in finding.support:
            assert handle in agent.last_receipts, f"{handle} was never opened"
            assert fresh.verify(handle) is None

    # The resume path: production's turn_end validator, over the store replayed from disk, with
    # the documents this pass opened, still accepts the filed report.
    check = LibrarianToolProvider(agent.sources, storage=storage, store=fresh,
                                  report_path=folder / "recheck.jsonl", papers_dir=papers_dir())
    check.captures = {handle: fresh.get(handle) for handle in agent.last_receipts}
    check.written_report = report
    assert check.turn_problems() == [], check.turn_problems()
