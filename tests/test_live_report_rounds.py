"""Env-gated LIVE test: the V-4 two-round report structure with real models.

Simulates the two-round shape of the Cyg X-1 reproduction run
(``…/runs/bf1e39e079…/monitor.jsonl``): a **pre-authoring** round (PCUBE analysis, figures =
PD(E)/PA(E) + Q-U plane; the XSPEC polconst cross-check is missing → a tool_request), then a
**post-authoring** round (the XSPEC fitter + benchmark tools now exist; figures = the polconst
fit + the this-work-vs-paper comparison). Two analyst workers + the lead run on real models over
the run's *memory* (no live science sandbox — the analysis is replayed from memory, the figures
are pre-placed).

What it checks (the V-4 fix):
  * round 1 — each worker writes a report (to its working folder via ``edit_report``) and the lead
    adjudicates one; reports are logged for human review of "reasonableness".
  * round 2 — figures from round 1 are **still citable** (append-only catalog): each worker's
    catalog spans both rounds, and the lead's per-worker catalog — accumulated across the two
    adjudications — contains a round-1 AND a round-2 figure. This is the leak V-4 closes: nothing
    is cleared at round start, so the lead → reviewer report can carry figures from any round.

The figures are timestamped (round 1 = T1, round 2 = T2), so the catalog at each round reflects
the state at that time. The lead's *authoring decision* (round 1 → round 2 transition) is
simulated here; the full decide_authoring→coder flow is exercised by the notebook end-to-end runs
(§9.10 step 7), not this test. Spends real tokens across the analyst_worker + analyst_lead models.

Activate:
    AUTO_RESEARCH_LIVE_REPORT_ROUNDS_TESTS=1 pytest tests/test_live_report_rounds.py -q -s
"""

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dotenv import load_dotenv

from auto_research.agents.roles import AnalystLeadAgent, AnalystWorkerAgent
from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.models import load_registry
from auto_research.schemas import AnalysisTask, AnalystReport, FigureRef

pytestmark = pytest.mark.skipif(
    os.getenv("AUTO_RESEARCH_LIVE_REPORT_ROUNDS_TESTS") != "1",
    reason="set AUTO_RESEARCH_LIVE_REPORT_ROUNDS_TESTS=1 to run the V-4 two-round live test (spends tokens)",
)

_T1 = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)   # round 1 (pre-authoring)
_T2 = datetime(2026, 7, 14, 10, 30, tzinfo=UTC)  # round 2 (post-authoring)

_QUESTION = (
    "Independent reproduction of the Krawczynski+2022 (Science) Cyg X-1 IXPE polarization for "
    "ObsIDs 01002901 (2022 May) and 01250101 (2022 June ToO): report 2-8 keV PD/PA, the "
    "energy-resolved PD(E) trend, an XSPEC tbabs*polconst*powerlaw cross-check, and a this-work-"
    "vs-paper comparison. Published values are a benchmark only; never tune to match them."
)


def _make_fig(dirpath: Path, stem: str, caption: str, produced_by: str, created: datetime) -> FigureRef:
    """Render a real (small) labelled plot and build a FigureRef whose created_at marks its round.

    A genuine PNG (not a degenerate 1x1, which providers reject as "Could not process image"), so
    the figure attaches at the multimodal boundary exactly as a toolkit plotter's would.
    """
    import hashlib

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = dirpath / f"{stem}.png"
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.plot([2, 3, 4, 6, 8], [2.6, 2.9, 3.8, 4.5, 5.0], "o-")
    ax.set_title(caption, fontsize=7)
    ax.set_xlabel("energy (keV)")
    ax.set_ylabel("value")
    fig.tight_layout()
    fig.savefig(path, dpi=80)
    plt.close(fig)
    return FigureRef(
        path=path,
        caption=caption,
        mime_type="image/png",
        produced_by=produced_by,
        created_at=created,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


_ROUND1_MEMORY = (
    "TOOL RESULTS SO FAR (round 1 — PCUBE analysis, already executed):\n"
    "- ixpe_pcube on 01002901 (2-8 keV, DU-combined, NEFF weighting): PD=3.37+/-0.20%, "
    "PA=-20.45+/-1.66 deg, SIGNIF=17.3; 01250101: PD=3.73+/-0.30%, PA=-24.92+/-2.33 deg; "
    "May+June combined PD=3.46+/-0.16%, PA=-21.73+/-1.36 deg.\n"
    "- Energy-resolved PCUBE (4 bands): PD rises 2.58->4.99% across 2-8 keV; a linear fit gives "
    "slope B=0.53+/-0.11 %/keV (paper 0.58+/-0.15).\n"
    "- ixpe_pcube_plot produced two figures: the PD(E)/PA(E) time/energy series and the Stokes "
    "Q-U plane (both in your figure catalog).\n"
    "- GAP: the XSPEC tbabs*polconst*powerlaw spectro-polarimetric cross-check could NOT be run — "
    "no toolkit tool performs that fit, and inline fitting is off-limits."
)

_ROUND2_MEMORY = _ROUND1_MEMORY + (
    "\n\nTOOL RESULTS (round 2 — the XSPEC fitter + benchmark tools were just authored and are now "
    "available):\n"
    "- fit_tbabs_polconst_powerlaw on 01002901: PD=3.40+/-0.14%, PA=-20.3+/-1.2 deg, Gamma~1.6, "
    "PD/PA tied across DUs — consistent with the PCUBE result; plot_fit produced the I-spectrum + "
    "residuals figure (in your catalog).\n"
    "- compare_to_benchmark: 14 records, 0 flagged at |z|>3; plot_benchmark_compare produced the "
    "this-work-vs-paper forest plot (in your catalog).\n"
    "The reproduction is now complete: PCUBE, the XSPEC cross-check, and the benchmark all agree "
    "with the paper within stated uncertainties."
)


def _worker(handle, folder: Path, key: str) -> AnalystWorkerAgent:
    class _StubSandbox:
        is_alive = True

        async def start(self):  # noqa: D401
            pass

    class _StubRegistry:
        def tool_definitions(self):
            return []

    w = AnalystWorkerAgent(model=handle, prompt=_worker_prompt(), sandbox=_StubSandbox(), registry=_StubRegistry())
    w.memory_path = folder / "memory.md"
    w.report_dir = folder
    w.agent_key = key
    return w


def _worker_prompt() -> str:
    # Compact, replay-framed worker prompt (the real prompt file is loaded in production; here we
    # keep the test self-contained and cheap): the analysis is done, compose the report via tools.
    return (
        "You are an analyst worker. The data analysis has already been executed — its results are "
        "in your `memory`, and the figures it produced are in your figure catalog. Do NOT call "
        "analysis tools. Call `review_report` to see your figures (each labelled [self:index]), "
        "then call `edit_report` to write your report: a `summary`, `findings`, and `cited_figures` "
        "citing every figure that supports a finding, in order, referring to the k-th as 'Figure k'. "
        "If a needed capability is missing (per memory), set the `tool_request` field. Set `status`."
    )


def _lead(handle, folder: Path) -> AnalystLeadAgent:
    lead = AnalystLeadAgent(model=handle, prompt=_lead_prompt())
    lead.report_dir = folder
    lead.agent_key = "analyst_lead"
    lead.tools = ReadOnlyToolProvider(roots=[folder.parent])
    return lead


def _lead_prompt() -> str:
    return (
        "You are the analyst lead. Synthesize the workers' reports into one final analysis report. "
        "Call `review_report` (no argument) to see every figure you can cite, grouped by worker and "
        "labelled [analyst_worker_i:index]; call review_report('analyst_worker_i') to re-read a "
        "worker's report. Then call `edit_report` to write the final report, citing in "
        "`cited_figures` the figures that best evidence the synthesis (across workers and rounds), "
        "in order, referring to the k-th as 'Figure k'. Set `status`."
    )


def _has_round(figs, when: datetime) -> bool:
    return any(f.created_at == when for f in figs)


def test_live_two_round_report_accumulates_figures(tmp_path):
    load_dotenv()
    registry = load_registry()
    wh = registry.resolve("analyst_worker").handles
    lh = registry.resolve("analyst_lead").handles[0]
    w_handles = [wh[0], wh[1] if len(wh) > 1 else wh[0]]

    folders = [tmp_path / "analyst_worker_0", tmp_path / "analyst_worker_1", tmp_path / "analyst_lead"]
    for f in folders:
        (f / "figures").mkdir(parents=True, exist_ok=True)
    workers = [
        _worker(w_handles[0], folders[0], "analyst_worker_0"),
        _worker(w_handles[1], folders[1], "analyst_worker_1"),
    ]
    lead = _lead(lh, folders[2])

    # Round-1 figures (T1) — seed each worker's catalog + write round-1 memory.
    for w, folder in zip(workers, folders[:2]):
        figdir = folder / "figures"
        w._figure_catalog["self"] = [
            _make_fig(figdir, "ixpe_pcube_plot_pdpa", "PCUBE PD(E)/PA(E), Cyg X-1 01002901",
                      "xspec_utils.lib.ixpe_pcube_plot.ixpe_pcube_plot", _T1),
            _make_fig(figdir, "ixpe_pcube_plot_qu", "Stokes Q-U plane, Cyg X-1",
                      "xspec_utils.lib.ixpe_pcube_plot.ixpe_pcube_plot", _T1),
        ]
        w.memory_path.write_text(_ROUND1_MEMORY, encoding="utf-8")

    task = AnalysisTask(question=_QUESTION, instructions="Compose your report from memory + your figure catalog.")

    async def run():
        # ── ROUND 1 (pre-authoring) ──
        r1_w = [await w.analyze(task) for w in workers]
        r1_lead = await lead.adjudicate(task, r1_w, sources=["analyst_worker_0", "analyst_worker_1"])

        # ── simulate the authoring decision → ROUND 2 (post-authoring): append T2 figures ──
        for w, folder in zip(workers, folders[:2]):
            figdir = folder / "figures"
            w._figure_catalog["self"] += [
                _make_fig(figdir, "plot_fit_polconst", "tbabs*polconst*powerlaw fit: I-spectrum + residuals",
                          "xspec_utils.lib.plot_fit.plot_fit", _T2),
                _make_fig(figdir, "plot_benchmark", "This-work vs Krawczynski+2022 benchmark (0 flags >3sigma)",
                          "xspec_utils.lib.plot_benchmark_compare.plot_benchmark_compare", _T2),
            ]
            w.memory_path.write_text(_ROUND2_MEMORY, encoding="utf-8")

        r2_w = [await w.analyze(task) for w in workers]
        r2_lead = await lead.adjudicate(task, r2_w, sources=["analyst_worker_0", "analyst_worker_1"])
        return r1_w, r1_lead, r2_w, r2_lead

    # Instrument every edit_report call so we can see whether the source-side figure-reference
    # note ("Did you mean that?") fires in the live loop — and whether the model re-cites after.
    import auto_research.agents.react as _react

    _orig_edit = _react.execute_edit_report
    edit_calls: list[dict] = []

    async def _recording_edit(workspace, arguments):
        out = await _orig_edit(workspace, arguments)
        edit_calls.append({
            "agent": workspace.report_path.parent.name if workspace else "?",
            "n_cited": len(arguments.get("cited_figures") or []),
            "note_fired": "Did you mean that?" in out.text,
            "note": next((ln for ln in out.text.splitlines() if ln.startswith("Note:")), ""),
        })
        return out

    _react.execute_edit_report = _recording_edit
    try:
        r1_w, r1_lead, r2_w, r2_lead = asyncio.run(run())
    finally:
        _react.execute_edit_report = _orig_edit

    # ── log everything for human review of reasonableness ──
    logdir = Path("logs/dev/report_rounds")
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")

    def _fig_line(f):
        return f"{f.caption}  [{'T1' if f.created_at == _T1 else 'T2'}]"

    md = [f"# V-4 two-round report live check ({stamp})\n", f"Question: {_QUESTION}\n"]
    for label, reps in (("ROUND 1 workers", r1_w), ("ROUND 2 workers", r2_w)):
        md.append(f"\n## {label}\n")
        for i, r in enumerate(reps):
            md.append(f"### worker {i} (status={r.status})\n")
            md.append(f"- summary: {r.summary}\n")
            md.append("- cited figures: " + "; ".join(_fig_line(f) for f in r.figures) + "\n")
            if r.tool_request:
                md.append(f"- tool_request: {r.tool_request.proposed_tool_name}\n")
    for label, r in (("ROUND 1 lead", r1_lead), ("ROUND 2 lead", r2_lead)):
        md.append(f"\n## {label} (status={r.status})\n- summary: {r.summary}\n")
        md.append("- cited figures: " + "; ".join(_fig_line(f) for f in r.figures) + "\n")
    md.append("\n## lead per-worker catalog after round 2 (accumulated across both adjudications)\n")
    for src, figs in lead._figure_catalog.items():
        md.append(f"- {src}: " + "; ".join(_fig_line(f) for f in figs) + "\n")
    md.append("\n## edit_report calls (figure-reference note = the §13 guard)\n")
    for c in edit_calls:
        md.append(f"- {c['agent']}: cited {c['n_cited']}; note_fired={c['note_fired']}"
                  + (f"\n    {c['note']}" if c["note"] else "") + "\n")
    (logdir / f"two_round_{stamp}.md").write_text("".join(md), encoding="utf-8")
    (logdir / f"two_round_{stamp}.jsonl").write_text(
        "\n".join(
            json.dumps({"role": lbl, "summary": r.summary, "status": r.status,
                        "figures": [_fig_line(f) for f in r.figures]})
            for lbl, r in [("r1_lead", r1_lead), ("r2_lead", r2_lead)]
        ) + "\n",
        encoding="utf-8",
    )
    fired = [c for c in edit_calls if c["note_fired"]]
    print(f"\n[report-rounds] transcripts → {logdir}/two_round_{stamp}.md")
    print(f"[report-rounds] edit_report calls: {len(edit_calls)}; figure-reference note fired on "
          f"{len(fired)} — {[c['agent'] for c in fired] or 'none this run (no model under-cited)'}")

    # ── assertions: reports composed, and the V-4 accumulation guarantee ──
    for r in (*r1_w, r1_lead, *r2_w, r2_lead):
        assert r.summary.strip(), "every round produced a non-empty report"

    # append-only worker catalog: after round 2 each worker can still see its round-1 figures
    for w in workers:
        cat = w._figure_catalog["self"]
        assert _has_round(cat, _T1) and _has_round(cat, _T2), "worker catalog spans both rounds"

    # THE V-4 FIX: the lead's per-worker catalog, accumulated across the two adjudications, carries
    # a round-1 figure AND a round-2 figure — a figure born in round 1 is never cleared, so the
    # lead → reviewer report is no longer last-round-only.
    lead_figs = [f for figs in lead._figure_catalog.values() for f in figs]
    assert _has_round(lead_figs, _T1), "lead retained a round-1 figure into round 2 (no clearing)"
    assert _has_round(lead_figs, _T2), "lead has the round-2 figures"
