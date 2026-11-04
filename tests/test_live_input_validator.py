"""Opt-in LIVE cells for the input validator's tool-input job (plan items 2 and 3, 2026-11-03).

    AUTO_RESEARCH_LIVE_INPUT_VALIDATOR=1 pytest tests/test_live_input_validator.py -s

* ``test_every_configured_vendor_accepts_the_kind_schemas`` — each configured worker and lead member is sent
  the real toolkit's tool definitions, as the registry rewrites them (``provider_schema``: data and
  parameter arguments untyped, ``kind``/``default`` removed), and must answer without a refusal.
* ``test_a_worker_passes_data_by_reference_and_types_a_parameter_with_its_reason`` — a worker,
  wired by ``graph.runtime.attach_worker_runtime`` over a throwaway toolkit with declared kinds,
  makes bins and fits a limit at a confidence level of its choosing, and files its report. The
  receipts must show: the fit took the bins by reference (a parent), the confidence level arrived
  with a reason (recorded behind the fit's handle), and the report states the limit as a reference.

Assertions stay on our side of the boundary (CLAUDE.md §10): what the receipts and the ledger
record. Each cell writes its record under ``logs/dev/input-validator/``.
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_INPUT_VALIDATOR") != "1",
    reason="set AUTO_RESEARCH_LIVE_INPUT_VALIDATOR=1 to run the live input-validator cells",
)
load_dotenv()

LOG = Path(__file__).resolve().parents[1] / "logs" / "dev" / "input-validator"
#: The roles a toolkit's tool definitions reach: the workers (every tool) and the lead (data_catalog).
#: (Gemini, seated only in the reviewer and librarian groups, refuses `load_data`'s `epoch` object
#: with `additionalProperties`, which predates argument kinds: Google is not an active seat, §13.)
ROLES = ("analyst_lead", "analyst_worker")


def _record(cell: str, payload: dict) -> Path:
    LOG.mkdir(parents=True, exist_ok=True)
    path = LOG / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{cell}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[{cell}] → {path}")
    return path


def test_every_configured_vendor_accepts_the_kind_schemas():
    from auto_research.env import toolkit_root
    from auto_research.models import ModelMessage
    from auto_research.models.registry import resolve
    from auto_research.sandbox.registry import ToolkitRegistry

    registry = ToolkitRegistry(toolkit_root())
    tools = registry.tool_definitions()
    assert tools, "the configured toolkit exposes no tools"
    seen: dict[tuple[str, str], object] = {}
    for role in ROLES:
        for handle in resolve(role).handles:
            seen.setdefault((handle.config.provider, handle.config.model), handle)
    results: dict[str, str] = {}

    async def probe(key, handle):
        try:
            response = await handle.call(
                [ModelMessage(role="system", content="You are testing tool definitions."),
                 ModelMessage(role="user", content="Reply with the single word OK. Call no tool.")],
                tools=tools, tool_choice="auto",
            )
            results[f"{key[0]}:{key[1]}"] = "ok: " + str(getattr(response, "content", "") or "")[:40]
        except Exception as exc:  # noqa: BLE001 — the refusal is the finding
            results[f"{key[0]}:{key[1]}"] = f"REFUSED {type(exc).__name__}: {exc}"[:400]

    async def main():
        await asyncio.gather(*(probe(k, h) for k, h in seen.items()))

    asyncio.run(main())
    _record("vendor_schemas", {"tools": [t["name"] for t in tools], "results": results})
    refused = {k: v for k, v in results.items() if v.startswith("REFUSED")}
    assert not refused, refused


_MANIFEST = '''
TOOL_MANIFEST = {
    "research_toolkit.demo_utils.make_bins": {
        "module": "demo_utils.lib.demo", "fn": "make_bins",
        "description": "Make energy bins with a polarization fraction q per bin (a synthetic data tool).",
        "args_schema": {"type": "object", "properties": {
            "n_bins": {"kind": "setting", "type": "integer", "description": "How many bins."}},
            "required": ["n_bins"]},
    },
    "research_toolkit.demo_utils.upper_limit": {
        "module": "demo_utils.lib.demo", "fn": "upper_limit",
        "description": "Upper limit on the mean polarization fraction of the bins at a confidence level.",
        "args_schema": {"type": "object", "properties": {
            "bins": {"kind": "data", "description": "The bins make_bins returned."},
            "cl": {"kind": "parameter", "type": "number",
                   "description": "Confidence level, a fraction between 0 and 1."}},
            "required": ["bins", "cl"]},
    },
}
'''

_LIB = '''
import math


def make_bins(n_bins):
    bins = [{"e_kev": 2.0 + i, "q": round(0.0123 * (i + 1), 6), "q_err": 0.0041} for i in range(int(n_bins))]
    print(f"made {len(bins)} bins")
    return {"bins": bins, "n_bins": len(bins)}


def upper_limit(bins, cl):
    mean = sum(b["q"] for b in bins) / len(bins)
    err = math.sqrt(sum(b["q_err"] ** 2 for b in bins)) / len(bins)
    z = {0.68: 1.0, 0.9: 1.645, 0.95: 1.96, 0.99: 2.576}.get(round(float(cl), 2), 2.0)
    limit = mean + z * err
    print(f"upper limit {limit:.6f} at cl {cl}")
    return {"limit": limit, "mean": mean, "cl": cl}
'''


def _toolkit(root: Path) -> Path:
    pkg = root / "demo_utils"
    (pkg / "skills").mkdir(parents=True)
    (pkg / "lib").mkdir()
    (pkg / "__init__.py").write_text("from demo_utils.skills import TOOL_MANIFEST\n")
    (pkg / "skills" / "__init__.py").write_text(textwrap.dedent(_MANIFEST))
    (pkg / "lib" / "__init__.py").write_text("")
    (pkg / "lib" / "demo.py").write_text(textwrap.dedent(_LIB))
    return root


def test_a_worker_passes_data_by_reference_and_types_a_parameter_with_its_reason(tmp_path):
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.graph.runtime import attach_channel, attach_worker_runtime
    from auto_research.monitor.events import RunMonitor
    from auto_research.observability.costs import install_cost_meter
    from auto_research.provenance.refs import call_arguments
    from auto_research.run_control import RunControl
    from auto_research.sandbox.registry import ToolkitRegistry, check_toolkit_manifests
    from auto_research.sandbox.workspace import create_run_workspace
    from auto_research.schemas import AnalysisTask
    from tests.dev_logs import dev_log_path
    from tests.seats import seat as seat_override, seated

    root = _toolkit(tmp_path / "toolkit")
    check_toolkit_manifests(root)  # the launch check the run path runs
    provider, model = os.getenv("AUTO_RESEARCH_SHAPE_SEAT", "anthropic:claude-sonnet-5").split(":", 1)
    # The ledger lives under logs/dev, not tmp_path: pytest prunes its numbered tmpdirs.
    monitor = RunMonitor(chatter=3, jsonl_path=dev_log_path("input-validator", "worker_monitor", ".jsonl"))
    ledger = monitor.jsonl_path
    with seated(analyst_worker=seat_override(provider, model)):
        worker = AnalystWorkerAgent(monitor=monitor)  # its model resolves through the seat
        create_run_workspace(monitor.run_id, ["analyst_worker_0"])
        control = RunControl()
        sandboxes = attach_worker_runtime([worker], run_id=monitor.run_id, registry=ToolkitRegistry(root),
                                          toolkit_root=root, run_control=control)
        attach_channel((worker,), run_id=monitor.run_id, run_control=control, monitor=monitor)

        async def main():
            monitor.cost_meter = install_cost_meter(run_id=monitor.run_id, monitor=monitor)
            try:
                return await worker.analyze(AnalysisTask(
                    question="Make 4 energy bins, then compute the upper limit on their mean "
                             "polarization fraction at a 99% confidence level. Report the limit."))
            finally:
                for sandbox in sandboxes:
                    await sandbox.shutdown()

        report = asyncio.run(main())
    store = worker.receipts
    results = store.receipts(source="analyst_worker_0", kind="result")
    limits = [r for r in results if r.produced_by.endswith("upper_limit")]
    makes = [r for r in results if r.produced_by.endswith("make_bins")]
    refusals = [json.loads(line)["data"].get("text", "")[:300] for line in ledger.read_text().splitlines()
                if '"tool_result"' in line and "This call was not run" in line]
    record = {
        "seat": f"{provider}:{model}", "status": getattr(report, "status", None),
        "findings": getattr(report, "findings", None), "refusals": refusals,
        "limit_calls": [{"handle": r.handle, "parents": r.parents, "args": call_arguments(store, r.handle)}
                        for r in limits],
    }
    _record("worker_kinds", record)
    assert makes and limits, record
    final = limits[-1]
    assert any(p in final.parents for p in (m.handle for m in makes)), "the fit took no bins by reference"
    cl = call_arguments(store, final.handle)["cl"]
    assert isinstance(cl, dict) and (set(cl) == {"value", "reason"} or "$ref" in cl), cl
    assert report is not None and report.status == "succeeded", record
    assert any(final.handle in f or "$args" in f for f in report.findings), record
