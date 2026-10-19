"""Opt-in LIVE cells for the report form and its diagnostics (plan rows A1–A4, run 32814459).

    AUTO_RESEARCH_LIVE_REPORT_FORM=1 pytest tests/test_live_report_form.py -s
    AUTO_RESEARCH_LIVE_REPORT_FORM=1 pytest tests/test_live_report_form.py -s -k blue_worker

**Blue team** (honest seats, production prompts, no hint about the form): each seat files a report
whose results sit in `findings`, whose `summary` is a short briefing and whose figures are cited —
on the first `turn_end`, with no `edit_report` refused (A1; A4: an honest report is unaffected).

**Red team** (seats told to exploit the report system freely, CLAUDE.md §2b adversarial cells): the
model tries the malformed shapes run 32814459 produced — parameter markup inside an argument
(saved as text by ruling 2026-10-18; whether it reaches the filed report is printed, not asserted),
a string `cited_figures`, an edit that drops placed figures, results in `summary` — and the cell
asserts each refusal was a diagnostic the model could act on, that it ended on a valid report, and
that no run of consecutive refusals ran longer than a few calls (A2, A3). Blue cells also fail on a
filed summary longer than its findings and record the summary's sentence count against the 3–5 form.

**Seats ("shuffle").** Each cell runs once per seat in `AUTO_RESEARCH_REPORT_FORM_SEATS`
(comma-separated `provider:model`, default the configured worker vendors). The lead cell runs on
`AUTO_RESEARCH_REPORT_FORM_LEAD_SEAT`, by default a vendor the configuration does not seat there.
Roles are built through the run path's own classes and prompts; the model resolves through the
configuration pointer (`tests.seats.seated`); the cost meter is installed in the task; every cell
writes its artifact under `logs/dev/report-form/` before asserting.

The analysis itself is not re-run: the toolkit results are minted as real receipts in a real store
and the figures are real PNGs in the worker's catalog, so the cell spends only on report writing.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

from tests.dev_logs import dev_log_path

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_REPORT_FORM") != "1",
    reason="set AUTO_RESEARCH_LIVE_REPORT_FORM=1 to spend provider tokens on the report-form cells",
)

load_dotenv()

SEATS = [
    s.strip() for s in os.getenv(
        "AUTO_RESEARCH_REPORT_FORM_SEATS", "anthropic:claude-sonnet-5,openai:gpt-5.6-terra"
    ).split(",") if s.strip()
]
LEAD_SEAT = os.getenv("AUTO_RESEARCH_REPORT_FORM_LEAD_SEAT", "openai:gpt-5.6-terra")
#: Independent draws per seat: one draw is not validation (`-k blue` with DRAWS=3 for a check).
DRAWS = list(range(int(os.getenv("AUTO_RESEARCH_REPORT_FORM_DRAWS", "1"))))

#: A briefing is 3–5 sentences; one or two over is a style slip, past this it carries results.
MAX_SUMMARY_SENTENCES = 6
#: "No refusal loop longer than a few calls" (plan A3), counted as consecutive refused report calls.
MAX_REFUSAL_RUN = 4

RED_TEAM = (
    "You are also red-teaming this harness's report system, with permission. Before you file, "
    "exploit `write_report`, `edit_report` and `turn_end` freely and read every reply: put "
    "`</parameter>` and `<parameter name=\"…\">` markup inside an argument; send an old_string "
    "that is not in the draft, and one that occurs twice; make an edit that breaks the document "
    "layout (a duplicate header, a figure line that is not a handle, a figure the catalog does "
    "not hold); put every result in the summary with no findings and status succeeded; call "
    "`turn_end` before the report is complete. Then file a correct report and end your turn with "
    "`turn_end`. In the uncertainty block, say which replies helped you fix the report and which "
    "did not."
)


# ── the world each cell writes from ──────────────────────────────────────────


def _png(path: Path, kind: str, title: str) -> None:
    """A plot that shows what its caption says, so a reviewing model can judge it as evidence."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    rng = np.random.default_rng(7)
    fig, ax = plt.subplots(figsize=(4.5, 3))
    if kind == "periodogram":
        period = np.linspace(0.5, 8.0, 2000)
        power = 2 + rng.gamma(1.5, 1.0, period.size)
        power += 38.2 * np.exp(-0.5 * ((period - 3.4172) / 0.02) ** 2)
        power += 8.8 * np.exp(-0.5 * ((period - 1.4126) / 0.01) ** 2)
        ax.plot(period, power, lw=0.7)
        ax.axvline(3.4172, color="r", ls="--", lw=0.8)
        ax.set_xlabel("period (d)")
        ax.set_ylabel("power")
    else:
        phase = rng.uniform(0, 1, 400)
        model = 0.083 * np.cos(2 * np.pi * (phase - 0.47))  # faintest at phase 0.47
        noise = rng.normal(0, 0.01, phase.size)
        if kind == "folded":
            ax.plot(phase, model + noise, ".", ms=2)
            ax.set_ylabel("relative magnitude (mag)")
        else:
            ax.plot(phase, noise, ".", ms=2)
            ax.axhline(0, color="k", lw=0.6)
            ax.set_ylabel("residual (mag)")
        ax.invert_yaxis()
        ax.set_xlabel("phase")
    ax.set_title(title, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=80)
    plt.close(fig)


def _seed_worker_world(root: Path, store, key: str):
    """Minted toolkit results and catalog figures for one worker, as a finished analysis leaves them."""
    from auto_research.provenance import sha256_file
    from auto_research.schemas import FigureRef

    figdir = root / key / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    results = [
        store.mint(source=key, kind="result", produced_by="research_toolkit.timing_utils.period_search",
                   data="best period = 3.4172 +/- 0.0021 d; peak power = 41.7; false-alarm probability = 1.2e-06"),
        store.mint(source=key, kind="result", produced_by="research_toolkit.timing_utils.fold_curve",
                   data="folded amplitude = 0.083 +/- 0.006 mag; phase of minimum = 0.47 +/- 0.02"),
        store.mint(source=key, kind="result", produced_by="research_toolkit.timing_utils.alias_check",
                   data="strongest alias at 1.4126 d has power 12.3 (ratio to peak 0.29)"),
    ]
    figures = []
    for stem, caption in (
        ("periodogram", "Periodogram with the best period marked"),
        ("folded", "Light curve folded on the best period"),
        ("residuals", "Residuals after subtracting the folded model"),
    ):
        path = figdir / f"{stem}.png"
        _png(path, stem, caption)
        figures.append(FigureRef(
            path=path, caption=caption, mime_type="image/png",
            produced_by="research_toolkit.timing_utils.plot", created_at=datetime.now().astimezone(),
            sha256=sha256_file(path),
        ))
    return results, figures


def _results_text(store, results) -> str:
    out = []
    for r in results:
        text = store.text(r.handle) or ""
        out.append(f"[receipt {r.handle}] {r.produced_by.rsplit('.', 1)[-1]}:\n{text}")
    return "\n\n".join(out)


QUESTION = "Find the period of the variable source in the supplied light curve and say how significant it is."


# ── recording what the harness answered ──────────────────────────────────────


def _record_report_calls(monkeypatch, log: list[dict]) -> None:
    """Wrap the report tools at the module seam the providers call, recording every reply."""
    import auto_research.agents.react as react

    edit, end = react.execute_edit_report, react.execute_turn_end

    async def recording_edit(workspace, arguments, tool="edit_report"):
        # write_report reaches this seam too (execute_write_report calls it with its tool name).
        out = await edit(workspace, arguments, tool)
        log.append({"tool": tool, "args": dict(arguments), "is_error": out.is_error, "text": out.text,
                    "arg_chars": len(json.dumps(dict(arguments), ensure_ascii=False))})
        return out

    async def recording_end(validator, arguments, warnings=None):
        out = await end(validator, arguments, warnings)
        log.append({"tool": "turn_end", "args": dict(arguments), "is_error": out.is_error, "text": out.text})
        return out

    monkeypatch.setattr(react, "execute_edit_report", recording_edit)
    monkeypatch.setattr(react, "execute_turn_end", recording_end)


def _longest_refusal_run(log: list[dict]) -> int:
    run = best = 0
    for entry in log:
        run = run + 1 if entry["is_error"] else 0
        best = max(best, run)
    return best


def _sentences(text: str) -> int:
    import re

    text = (text or "").strip()
    return len(re.split(r"(?<=[.!?])\s+(?=[A-Z\[(\"'])", text)) if text else 0


def _seat(spec: str):
    from tests.seats import seat

    provider, model = spec.split(":", 1)
    return seat(provider, model)


def _write_artifact(cell: str, spec: str, report, log, cost: float | None) -> Path:
    path = dev_log_path("report-form", f"{cell}_{spec.replace(':', '_')}", ".json")
    path.write_text(json.dumps({
        "cell": cell, "seat": spec, "cost_usd": cost,
        "report": report.model_dump(mode="json") if report is not None else None,
        "calls": log,
    }, indent=2, default=str) + "\n", encoding="utf-8")
    sizes = [f"{e['tool'][0]}{e['arg_chars']}" for e in log if "arg_chars" in e]
    print(f"\n[{cell} {spec}] calls={len(log)} refusals={sum(e['is_error'] for e in log)} "
          f"report-call chars (w=write, e=edit)={sizes} "
          f"cost=${cost if cost is not None else '?'} → {path}")
    return path


# ── the worker cell ──────────────────────────────────────────────────────────


def _run_worker(tmp_path, spec: str, monkeypatch, *, red: bool):
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.models import load_registry
    from auto_research.monitor.events import RunMonitor
    from auto_research.observability.costs import install_cost_meter
    from auto_research.provenance import ReceiptStore
    from auto_research.schemas import AnalysisTask
    from tests.seats import seated

    log: list[dict] = []
    _record_report_calls(monkeypatch, log)
    store = ReceiptStore(tmp_path / "receipts")
    key = "analyst_worker_0"
    results, figures = _seed_worker_world(tmp_path, store, key)

    class _NoSandbox:  # the analysis already ran; the cell measures report writing only
        is_alive = True

        async def start(self):
            return None

    class _NoToolkit:
        def tool_definitions(self):
            return []

    meter = None
    with seated(analyst_worker=_seat(spec)):
        handle = load_registry().resolve("analyst_worker").handles[0]
        monitor = RunMonitor(jsonl_path=tmp_path / "monitor.jsonl")
        worker = AnalystWorkerAgent(
            model=handle, prompt=load_prompt("analyst_worker"), sandbox=_NoSandbox(),
            registry=_NoToolkit(), monitor=monitor,
        )
        worker.agent_key = key
        worker.report_dir = tmp_path / key
        worker.receipts = store
        worker._figure_catalog["self"] = list(figures)
        instructions = (
            "Your toolkit calls for this task have run; their results and the figures they wrote are "
            "below and in your figure catalog. Write your report.\n\n" + _results_text(store, results)
        )
        if red:
            instructions += "\n\n" + RED_TEAM
        task = AnalysisTask(question=QUESTION, instructions=instructions)

        async def scenario():
            nonlocal meter
            meter = install_cost_meter(run_id=monitor.run_id, monitor=monitor)
            return await worker.analyze(task)

        report = None
        try:
            report = asyncio.run(scenario())
        finally:
            cost = meter.total_usd if meter is not None else None
            _write_artifact("red_worker" if red else "blue_worker", spec, report, log, cost)
    return report, log


@pytest.mark.parametrize("draw", DRAWS)
@pytest.mark.parametrize("spec", SEATS)
def test_blue_worker_files_the_form_first_try(tmp_path, spec, draw, monkeypatch):
    report, log = _run_worker(tmp_path, spec, monkeypatch, red=False)
    problems = []
    if report is None:
        problems.append("no report filed")
    else:
        if not report.findings:
            problems.append("findings empty")
        if _sentences(report.summary) > MAX_SUMMARY_SENTENCES:
            problems.append(f"summary of {_sentences(report.summary)} sentences")
        # The warning may fire mid-turn (it is there to be acted on); the filed report is judged.
        if len(report.summary.strip()) > sum(len(f.strip()) for f in report.findings):
            problems.append("the filed summary is longer than its findings")
        if not report.figures:
            problems.append("no figure cited")
        if report.status != "succeeded":
            problems.append(f"status {report.status}")
    refused = [e for e in log if e["is_error"]]
    if refused:
        problems.append(f"{len(refused)} refused call(s), first: {refused[0]['text'][:200]}")
    assert not problems, f"{spec}: " + "; ".join(problems)


@pytest.mark.parametrize("draw", DRAWS)
@pytest.mark.parametrize("spec", SEATS)
def test_red_worker_is_nudged_to_a_valid_report(tmp_path, spec, draw, monkeypatch):
    report, log = _run_worker(tmp_path, spec, monkeypatch, red=True)
    problems = []
    if report is None:
        problems.append("no report filed")
    else:
        if report.status == "succeeded" and not report.findings:
            problems.append("a 'succeeded' report with no findings was filed")
        if not report.figures:
            problems.append("no figure cited")
        if any("</parameter>" in (t or "") for t in [report.summary, *report.findings]):
            # Permitted by ruling (2026-10-18, no special case); recorded, not asserted.
            print(f"[red {spec}] parameter markup reached the filed report")
    run = _longest_refusal_run(log)
    if run > MAX_REFUSAL_RUN:
        problems.append(f"{run} consecutive refusals")
    for entry in log:
        if entry["is_error"] and not (
            "nothing changed" in entry["text"] or "cannot be ended yet" in entry["text"]
        ):
            problems.append(f"an undiagnosed refusal: {entry['text'][:200]}")
    assert not problems, f"{spec}: " + "; ".join(problems)


# ── the lead cell ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("draw", DRAWS)
def test_blue_lead_files_the_form_first_try(tmp_path, draw, monkeypatch):
    from auto_research.agents.prompts import load_prompt
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.env import toolkit_root as configured_toolkit_root
    from auto_research.graph.runtime import readonly_tools_for
    from auto_research.models import load_registry
    from auto_research.monitor.events import RunMonitor
    from auto_research.observability.costs import install_cost_meter
    from auto_research.provenance import ReceiptStore
    from auto_research.schemas import AnalysisTask, AnalystReport
    from tests.seats import seated

    log: list[dict] = []
    _record_report_calls(monkeypatch, log)
    store = ReceiptStore(tmp_path / "receipts")
    reports = []
    for key in ("analyst_worker_0", "analyst_worker_1"):
        results, figures = _seed_worker_world(tmp_path, store, key)
        r0, r1, r2 = (r.handle for r in results)
        reports.append(AnalystReport(
            summary=f"The light curve has a significant period of 3.4172 d [{r0}]. Figure 1 shows the "
                    "periodogram and Figure 2 the folded curve.",
            findings=[
                f"best period = 3.4172 +/- 0.0021 d with false-alarm probability = 1.2e-06 [{r0}]",
                f"folded amplitude = 0.083 +/- 0.006 mag [{r1}]",
                f"the strongest alias at 1.4126 d has power 12.3 [{r2}]",
            ],
            status="succeeded",
            cited_figures=[{"source": "self", "index": 1}, {"source": "self", "index": 2}],
            figures=figures[:2],
        ))

    meter = None
    with seated(analyst_lead=_seat(LEAD_SEAT)):
        handle = load_registry().resolve("analyst_lead").handles[0]
        monitor = RunMonitor(jsonl_path=tmp_path / "monitor.jsonl")
        lead = AnalystLeadAgent(model=handle, prompt=load_prompt("analyst_lead"), monitor=monitor)
        lead.agent_key = "analyst_lead"
        lead.report_dir = tmp_path / "analyst_lead"
        lead.report_dir.mkdir(parents=True, exist_ok=True)
        lead.tools = readonly_tools_for("analyst_lead", roots=[tmp_path, configured_toolkit_root()])
        lead.receipts = store
        lead.tools.receipts = store
        lead.tools.receipt_source = "analyst_lead"
        task = AnalysisTask(question=QUESTION, instructions="Adjudicate the two workers' reports.")

        async def scenario():
            nonlocal meter
            meter = install_cost_meter(run_id=monitor.run_id, monitor=monitor)
            return await lead.run_cycle(
                task, reports, sources=["analyst_worker_0", "analyst_worker_1"], rounds_left=0,
            )

        outcome = None
        try:
            outcome = asyncio.run(scenario())
        finally:
            cost = meter.total_usd if meter is not None else None
            _write_artifact("blue_lead", LEAD_SEAT, getattr(outcome, "report", None), log, cost)

    report = outcome.report
    problems = []
    if report is None:
        problems.append("no report filed")
    else:
        if not report.findings:
            problems.append("findings empty")
        if _sentences(report.summary) > MAX_SUMMARY_SENTENCES:
            problems.append(f"summary of {_sentences(report.summary)} sentences")
        # The warning may fire mid-turn (it is there to be acted on); the filed report is judged.
        if len(report.summary.strip()) > sum(len(f.strip()) for f in report.findings):
            problems.append("the filed summary is longer than its findings")
        if not report.figures:
            problems.append("no figure cited")
    refused = [e for e in log if e["is_error"]]
    if refused:
        problems.append(f"{len(refused)} refused call(s), first: {refused[0]['text'][:200]}")
    assert not problems, f"{LEAD_SEAT}: " + "; ".join(problems)
