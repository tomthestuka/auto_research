"""Deterministic tests for the inter-LLM report-authoring path (V-4).

Covers the figure-citation contract that replaces figure dedup: an append-only per-source
catalog, ``{source, index}`` citations resolved to ordered ``report.figures`` (Figure 1 =
figures[0]), the disciplined ``edit_report`` write (validates + persists a per-round JSONL,
degrades on bad input), and ``review_report`` (own draft + citable catalog menu, or a
subordinate's numbered report). No live models — just the resolver + tool executes.
"""

import asyncio
import json
from datetime import UTC, datetime

import pytest

from auto_research.agents.react import (
    EDIT_REPORT_TOOL,
    REVIEW_REPORT_TOOL,
    ReportWorkspace,
    SandboxToolProvider,
    execute_edit_report,
    execute_review_report,
    resolve_figure_citations,
)
from auto_research.sandbox.workspace import round_report_path
from auto_research.schemas import AnalystReport, FigureCitation, FigureRef


def _fig(name: str, caption: str, produced_by: str = "xspec_utils.lib.plot.plot_x") -> FigureRef:
    return FigureRef(
        path=f"/data/run/{name}.png",
        caption=caption,
        mime_type="image/png",
        produced_by=produced_by,
        created_at=datetime(2026, 7, 14, tzinfo=UTC),
        sha256="a" * 64,
    )


def _run(coro):
    return asyncio.run(coro)


# ── schema ───────────────────────────────────────────────────────────────────


def test_figure_citation_requires_positive_index():
    FigureCitation(source="self", index=1)
    with pytest.raises(Exception):
        FigureCitation(source="self", index=0)


def test_analyst_report_carries_cited_figures_and_figures():
    r = AnalystReport(summary="s", cited_figures=[{"source": "self", "index": 2}])
    assert r.cited_figures[0].index == 2
    assert r.figures == []  # runtime-resolved, empty until placed


# ── citation resolution (the dedup replacement) ───────────────────────────────


def test_resolve_orders_by_citation_and_drops_invalid():
    catalog = {"analyst_worker_0": [_fig("a", "PCUBE"), _fig("b", "QU")], "analyst_worker_1": [_fig("c", "bench")]}
    cited = [
        FigureCitation(source="analyst_worker_1", index=1),  # -> Figure 1
        FigureCitation(source="analyst_worker_0", index=2),  # -> Figure 2
        FigureCitation(source="analyst_worker_0", index=9),  # out of range -> dropped
        FigureCitation(source="ghost", index=1),             # unknown source -> dropped
    ]
    figures, notes = resolve_figure_citations(cited, catalog)
    assert [f.caption for f in figures] == ["bench", "QU"]  # order = citation order
    assert len(notes) == 2 and any("out of range" in n for n in notes) and any("unknown source" in n for n in notes)


def test_resolve_dedups_first_wins():
    catalog = {"self": [_fig("a", "PCUBE")]}
    cited = [FigureCitation(source="self", index=1), FigureCitation(source="self", index=1)]
    figures, _ = resolve_figure_citations(cited, catalog)
    assert len(figures) == 1


# ── edit_report: the disciplined write ─────────────────────────────────────────


def test_edit_report_persists_jsonl_and_resolves_citations(tmp_path):
    catalog = {"self": [_fig("a", "PCUBE PD/PA"), _fig("b", "QU plane"), _fig("c", "raw diag")]}
    path = tmp_path / "r.jsonl"
    ws = ReportWorkspace(report_path=path, report_model=AnalystReport, catalog=catalog)
    out = _run(
        execute_edit_report(
            ws,
            {
                "summary": "PD rises with energy (Figure 1); Q-U consistent (Figure 2).",
                "findings": ["fit ok"],
                "cited_figures": [{"source": "self", "index": 1}, {"source": "self", "index": 2}],
            },
        )
    )
    assert not out.is_error
    # persisted report resolves the two cited figures in order; the third (uncited) is dropped
    assert ws.written_report is not None
    assert [f.caption for f in ws.written_report.figures] == ["PCUBE PD/PA", "QU plane"]
    saved = AnalystReport.model_validate_json(path.read_text().strip())
    assert [f.caption for f in saved.figures] == ["PCUBE PD/PA", "QU plane"]
    assert "Figure 1: PCUBE PD/PA" in out.text
    assert "Not cited" in out.text and "raw diag" in out.text  # uncited surfaced, not silent


def test_referenced_figure_numbers_parses_and_ignores_words():
    from auto_research.agents.react import _referenced_figure_numbers

    # each figure-prefixed number is caught (a range like "3-4" only yields its prefixed first
    # number — best-effort; the guard still fires on the max, which is all it needs).
    assert _referenced_figure_numbers("PD rises (Figure 1); Q-U in Fig. 2; see figure 3") == {1, 2, 3}
    assert _referenced_figure_numbers("we reconfigure the fit") == set()  # 'fig' inside a word
    assert _referenced_figure_numbers(None, "", "no figures here") == set()


def test_edit_report_warns_when_prose_references_uncited_figures(tmp_path):
    """The §13 guard: prose cites 'Figure 1/2' but cited_figures is empty → warning (not error)."""
    catalog = {"self": [_fig("a", "PCUBE"), _fig("b", "QU")]}
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog=catalog)
    out = _run(
        execute_edit_report(
            ws,
            {"summary": "PD rises with energy (Figure 1); the Q-U plane is in Figure 2.",
             "cited_figures": []},  # the Sonnet failure: references figures, cites none
        )
    )
    assert not out.is_error  # a soft note, not a rejection — the report is still saved
    assert ws.written_report is not None and ws.written_report.figures == []
    assert "Did you mean that?" in out.text and "[1, 2]" in out.text
    assert "cited_figures" in out.text and "review_report" in out.text  # gentle nudge, not a directive


def test_edit_report_no_warning_when_references_match_placement(tmp_path):
    catalog = {"self": [_fig("a", "PCUBE"), _fig("b", "QU")]}
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog=catalog)
    out = _run(
        execute_edit_report(
            ws,
            {"summary": "PD rises (Figure 1); Q-U in Figure 2.",
             "cited_figures": [{"source": "self", "index": 1}, {"source": "self", "index": 2}]},
        )
    )
    assert not out.is_error and "Did you mean that?" not in out.text  # references match placed figures


def test_edit_report_invalid_report_degrades_not_raises(tmp_path):
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog={"self": []})
    out = _run(execute_edit_report(ws, {"findings": ["no summary"]}))  # summary is required
    assert out.is_error and "did not validate" in out.text
    assert not (tmp_path / "r.jsonl").exists()  # nothing persisted on a bad report


def test_edit_report_carries_tool_request(tmp_path):
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog={"self": []})
    out = _run(
        execute_edit_report(
            ws,
            {
                "summary": "capability missing",
                "status": "failed",
                "tool_request": {
                    "science_context": "need XSPEC polconst fit",
                    "expected_behavior": "fit tbabs*polconst*powerlaw, return PD/PA",
                    "proposed_tool_name": "fit_tbabs_polconst_powerlaw",
                    "validation_data": [{"mission": "ixpe", "obsid": "01002901"}],
                },
            },
        )
    )
    assert not out.is_error
    assert ws.written_report.tool_request is not None
    assert ws.written_report.tool_request.proposed_tool_name == "fit_tbabs_polconst_powerlaw"
    assert ws.written_report.status == "failed"


# ── review_report: read-back with figures attached ─────────────────────────────


def test_review_report_self_shows_catalog_menu_labeled_by_handle(tmp_path):
    catalog = {"self": [_fig("a", "PCUBE"), _fig("b", "QU")]}
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog=catalog)
    out = _run(execute_review_report(ws, {}))
    assert not out.is_error
    assert "[self:1] PCUBE" in out.text and "[self:2] QU" in out.text
    # every catalog figure attached, each display-copy captioned by its [source:index] handle
    assert [f.caption for f in out.figures] == ["[self:1] PCUBE", "[self:2] QU"]


def test_review_report_source_shows_numbered_report(tmp_path):
    sub = AnalystReport(summary="worker view", figures=[_fig("a", "PCUBE"), _fig("b", "QU")])
    ws = ReportWorkspace(
        report_path=tmp_path / "r.jsonl",
        report_model=AnalystReport,
        catalog={"analyst_worker_0": [_fig("a", "PCUBE")]},
        source_reports={"analyst_worker_0": sub},
    )
    out = _run(execute_review_report(ws, {"source": "analyst_worker_0"}))
    assert not out.is_error
    assert "Figure 1: PCUBE" in out.text and "Figure 2: QU" in out.text
    # a finished report's images are numbered "Figure N — …" (report-local, not [source:index])
    assert [f.caption for f in out.figures] == ["Figure 1 — PCUBE", "Figure 2 — QU"]


def test_review_report_unknown_source_errors(tmp_path):
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog={})
    out = _run(execute_review_report(ws, {"source": "ghost"}))
    assert out.is_error and "no report for source" in out.text


# ── provider wiring ────────────────────────────────────────────────────────────


class _StubRegistry:
    def tool_definitions(self):
        return []


def test_sandbox_provider_exposes_report_tools_only_with_workspace(tmp_path):
    ws = ReportWorkspace(report_path=tmp_path / "r.jsonl", report_model=AnalystReport, catalog={"self": []})
    with_ws = SandboxToolProvider(_StubRegistry(), object(), report_workspace=ws)
    names = {d["name"] for d in with_ws.tool_definitions()}
    assert {EDIT_REPORT_TOOL, REVIEW_REPORT_TOOL} <= names

    without = SandboxToolProvider(_StubRegistry(), object())
    names2 = {d["name"] for d in without.tool_definitions()}
    assert EDIT_REPORT_TOOL not in names2 and REVIEW_REPORT_TOOL not in names2


# ── workspace path helper ──────────────────────────────────────────────────────


def test_round_report_path_names_role_and_round_but_never_the_model(tmp_path):
    """A filename is readable by any agent whose allow-list covers the run directory (a coder's
    `grep -rl` returned `analyst_lead_claude-opus-5_round1_report.jsonl`, 2026-09-03), so the
    model stays out of the name; `model` remains in the signature so callers need no change."""
    p = round_report_path(tmp_path, role="analyst_worker", model="claude-opus-4-8", round_n=3)
    assert p.name == "analyst_worker_round3_report.jsonl"
    assert p.parent == tmp_path
    other = round_report_path(tmp_path, role="analyst_lead", model="gpt-5.5/high", round_n=1)
    assert other.name == "analyst_lead_round1_report.jsonl"


# ── lead path: per-worker catalog + citation + cross-iteration accumulation ────

from auto_research.agents.react import ReadOnlyToolProvider  # noqa: E402
from auto_research.agents.roles import AnalystLeadAgent  # noqa: E402
from auto_research.models import ModelResponse, ToolCall  # noqa: E402
from auto_research.schemas import AnalysisTask, AnalystReport as _AR  # noqa: E402


class _LeadModel:
    """Fake model for a lead adjudication ReAct loop: emit one edit_report (armed cited), then done."""

    def __init__(self):
        self.next_cited: list = []
        self.emitted = True

    def arm(self, cited):
        self.next_cited = cited
        self.emitted = False
        self.filed = False

    async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                   images=(), documents=(), cache_prefix=False):
        if not self.emitted:
            self.emitted = True
            return ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="e1",
                        name="edit_report",
                        arguments={"summary": "lead synthesis", "cited_figures": self.next_cited},
                    )
                ]
            )
        if not self.filed:
            self.filed = True
            return ModelResponse(
                tool_calls=[ToolCall(id="f1", name="submit_adjudication", arguments={})]
            )
        return ModelResponse(content="done")


def _lead(tmp_path):
    model = _LeadModel()
    lead = AnalystLeadAgent(model=model, prompt="p")
    lead.report_dir = tmp_path
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    return lead, model


def test_lead_adjudicate_cites_across_worker_sources(tmp_path):
    lead, model = _lead(tmp_path)
    r0 = _AR(summary="w0", figures=[_fig("a", "PCUBE")])
    r1 = _AR(summary="w1", figures=[_fig("b", "bench")])
    model.arm([{"source": "analyst_worker_1", "index": 1}, {"source": "analyst_worker_0", "index": 1}])
    outcome = _run(
        lead.run_cycle(
            AnalysisTask(question="x"), [r0, r1], sources=["analyst_worker_0", "analyst_worker_1"]
        )
    )
    final = outcome.report
    # citation order defines the report figure order (Figure 1 = the first cited = worker_1's bench)
    assert [f.caption for f in final.figures] == ["bench", "PCUBE"]
    # the report tools and the form desk were cleared off the shared provider after the cycle
    assert lead.tools.report_workspace is None
    assert lead.tools.cycle is None


def test_lead_catalog_accumulates_across_iterations(tmp_path):
    """V-4 second leak: a figure produced in iteration 1 is still citable in iteration 2, even
    though iteration-2's worker report does not carry it (append-only per-worker catalog)."""
    lead, model = _lead(tmp_path)
    # iteration 1 — worker_0 produced OLD; the lead cites it
    model.arm([{"source": "analyst_worker_0", "index": 1}])
    _run(lead.run_cycle(AnalysisTask(question="x"), [_AR(summary="w0", figures=[_fig("old", "OLD")])],
                        sources=["analyst_worker_0"]))
    # iteration 2 — worker_0's new report carries only NEW; OLD is gone from the report...
    model.arm([{"source": "analyst_worker_0", "index": 1}, {"source": "analyst_worker_0", "index": 2}])
    final = _run(lead.run_cycle(AnalysisTask(question="x"), [_AR(summary="w0", figures=[_fig("new", "NEW")])],
                                sources=["analyst_worker_0"])).report
    # ...but the lead's catalog kept OLD (slot 1) and appended NEW (slot 2), so both are citable
    assert [f.caption for f in final.figures] == ["OLD", "NEW"]


def test_lead_cycle_without_tools_reports_nothing_rather_than_composing_one(tmp_path):
    """The compose safety net is gone, and its absence is the contract.

    With no tool surface there is no form to file and nothing to investigate with, so the cycle
    returns the failure rather than a report the runtime wrote. A runtime-synthesized report sits
    closer to fabrication than the absence it papers over ([F6], the same ruling that dropped the
    terminal-compose guard).
    """

    class _StructModel:
        def __init__(self):
            self.calls = 0

        async def call(self, messages, *, schema=None, tools=None, tool_choice="auto",
                       images=(), documents=(), cache_prefix=False):
            self.calls += 1
            return ModelResponse(parsed={"summary": "plain synthesis", "findings": []})

    model = _StructModel()
    lead = AnalystLeadAgent(model=model, prompt="p")  # no tools attached
    outcome = _run(
        lead.run_cycle(AnalysisTask(question="x"), [_AR(summary="w0", figures=[_fig("a", "PCUBE")])],
                       sources=["analyst_worker_0"])
    )
    assert outcome.filed is None
    assert outcome.report is None
    assert model.calls == 0  # no model was asked to compose a stand-in
