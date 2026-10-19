"""Deterministic tests for the report as one text document (plan rows A5/A7, human rulings 2026-10-18).

"Copy Claude Code / Codex's design": `write_report{content}` replaces the whole draft (Write) and
`edit_report{old_string, new_string, replace_all}` replaces an exact, unique match in it (Edit). One
implementation (`agents/report_edit.py`) serves every role, driven here through each role's own
provider: the worker's `SandboxToolProvider`, the lead's `ReadOnlyToolProvider`, the reviewer's
`execute_edit_report` on its `ReviewerReport` workspace, and the librarian's provider.
"""

from __future__ import annotations

import asyncio
import json

from auto_research.agents.react import (
    EDIT_REPORT_TOOL,
    LEAD_REPORT_FIELDS,
    LEAD_STATUS_VALUES,
    TURN_END_TOOL,
    WRITE_REPORT_TOOL,
    ReportWorkspace,
    edit_report_tool_def,
    execute_edit_report,
    execute_write_report,
    report_document,
)
from auto_research.agents.report_edit import (
    ANALYST_BLOCKS,
    LIBRARIAN_BLOCKS,
    REVIEWER_BLOCKS,
    blocks_for,
    parse,
    render,
)
from auto_research.agents.reviewer_tools import REVIEWER_REPORT_FIELDS
from auto_research.schemas import AnalystReport
from auto_research.schemas.review import ReviewerReport
from tests.test_report_diagnostics import _lead, _worker, doc


def _run(coro):
    return asyncio.run(coro)


def _edit(provider, old, new, **extra):
    return _run(provider.execute(EDIT_REPORT_TOOL, {"old_string": old, "new_string": new, **extra}))


def _write(provider, content):
    return _run(provider.execute(WRITE_REPORT_TOOL, {"content": content}))


# ── the worker: write once, then small edits ─────────────────────────────────


def test_a_report_is_built_by_small_edits_from_the_empty_document(tmp_path):
    provider, ws = _worker(tmp_path)
    empty = report_document(ws)
    assert empty.startswith("# summary\n# status\n# findings\n# figures\n")  # every header present
    assert not _edit(provider, "# summary\n", "# summary\nThe fit converged.\n").is_error
    assert not _edit(provider, "# findings\n", "# findings\n- first result\n").is_error
    assert not _edit(provider, "- first result\n", "- first result\n- second result\n").is_error
    assert not _edit(provider, "# figures\n", "# figures\n- [self:2]\n").is_error
    out = _edit(provider, "# status\n", "# status\nsucceeded\n")
    report = ws.written_report
    assert report.summary == "The fit converged." and report.status == "succeeded"
    assert report.findings == ["first result", "second result"]
    assert [f.caption for f in report.figures] == ["panel 2"]
    # the reply: the changed lines with three lines of context, then a one-line outline
    assert out.text.startswith("Report edited:\n")
    assert "\tsucceeded" in out.text and "\t# summary" in out.text
    assert "Draft: summary 18 chars · status succeeded · findings 2 · figures 1" in out.text
    saved = AnalystReport.model_validate_json(ws.report_path.read_text())
    assert saved.findings == report.findings
    assert not _run(provider.execute(TURN_END_TOOL, {"summary": "done"})).is_error


def test_an_edit_leaves_everything_it_does_not_touch(tmp_path):
    provider, ws = _worker(tmp_path)
    _write(provider, doc(findings=("a", "b", "c"), uncertainty="calibration only"))
    _edit(provider, "- b\n", "- B, rewritten\n")
    report = ws.written_report
    assert report.findings == ["a", "B, rewritten", "c"]
    assert report.uncertainty == "calibration only" and len(report.figures) == 2
    assert report.summary.startswith("The fit converged")


def test_old_string_missing_or_ambiguous_is_refused_and_nothing_changes(tmp_path):
    provider, ws = _worker(tmp_path)
    _write(provider, doc(findings=("same", "same")))
    saved = ws.report_path.read_bytes()
    out = _edit(provider, "- nothing like this", "- x")
    assert out.is_error and "old_string is not in the draft" in out.text and "nothing changed" in out.text
    out = _edit(provider, "- same", "- other")
    assert out.is_error and "occurs 2 times" in out.text and "replace_all" in out.text
    assert ws.written_report.findings == ["same", "same"]
    assert ws.report_path.read_bytes() == saved  # the file on disk is untouched too
    out = _edit(provider, "- same", "- other", replace_all=True)
    assert not out.is_error and ws.written_report.findings == ["other", "other"]
    out = _edit(provider, "", "x")
    assert out.is_error and "use write_report" in out.text


def test_an_edit_that_breaks_the_document_is_refused(tmp_path):
    provider, ws = _worker(tmp_path)
    _write(provider, doc())
    saved = ws.report_path.read_bytes()
    out = _edit(provider, "# status\nsucceeded\n", "# status\nsucceeded\nand more\n")
    assert out.is_error and "# status holds one value; it has 2 lines" in out.text
    out = _edit(provider, "# findings\n", "# findings\nloose text\n")
    assert out.is_error and "comes before the first item; start each item with '- '" in out.text
    out = _edit(provider, "# figures\n", "# findings\n")
    assert out.is_error and "'# findings' appears twice" in out.text
    out = _edit(provider, "# findings\n", "# Findings\n")  # a miscased header is not folded in
    assert out.is_error and "write the header as '# findings' on its own line" in out.text
    out = _edit(provider, "# status\n", "# rework\ntrue\n# status\n")  # the lead's block
    assert out.is_error and "'# rework' is not a block of this report" in out.text
    assert ws.written_report.status == "succeeded" and ws.report_path.read_bytes() == saved


def test_a_worker_files_only_its_own_status_values(tmp_path):
    provider, ws = _worker(tmp_path)
    out = _write(provider, doc(status="needs_human"))
    assert out.is_error and "one of succeeded, failed" in out.text and ws.written_report is None
    lead, lead_ws = _lead(tmp_path / "lead")
    assert not _write(lead, "# summary\ns\n# status\nneeds_human\n").is_error
    assert lead_ws.written_report.status == "needs_human"


def test_the_reply_shows_the_draft_as_stored_so_the_next_edit_copies_it(tmp_path):
    provider, ws = _worker(tmp_path)
    out = _write(provider, "# summary\n\nThe fit.\n\n# status\nsucceeded\n# findings\n- one\n\n- two\n")
    assert not out.is_error
    # what was sent and what is stored differ in blank lines, so the reply shows the stored draft
    assert "The draft on file reads:\n# summary\nThe fit.\n# status" in out.text
    assert not _edit(provider, "- one\n- two\n", "- one\n- two\n- three\n").is_error
    assert ws.written_report.findings == ["one", "two", "three"]


def test_figure_order_is_the_line_order_and_unknown_figures_are_refused(tmp_path):
    provider, ws = _worker(tmp_path)
    _write(provider, doc(figures=("self:3", "self:1")))
    assert [f.caption for f in ws.written_report.figures] == ["panel 3", "panel 1"]
    _edit(provider, "- [self:3]\n- [self:1]\n", "- [self:1]\n- [self:3]\n")
    assert [f.caption for f in ws.written_report.figures] == ["panel 1", "panel 3"]
    out = _edit(provider, "- [self:3]\n", "- [self:3]\n- [other:1]\n")
    assert out.is_error and "other:1 — unknown source" in out.text


def test_write_replaces_the_whole_draft(tmp_path):
    provider, ws = _worker(tmp_path)
    _write(provider, doc(uncertainty="u"))
    out = _write(provider, "# summary\nOnly a summary now.\n")
    assert not out.is_error and out.text.startswith("Report written.")
    report = ws.written_report
    assert report.summary == "Only a summary now." and report.findings == [] and report.uncertainty is None
    out = _write(provider, "   ")
    assert out.is_error and "content is the whole draft" in out.text


def test_a_round_starts_empty_and_a_refusal_shows_the_draft_whole(tmp_path):
    provider, ws = _worker(tmp_path)
    ws.prior_report = AnalystReport(summary="last round", findings=["old"], status="succeeded")
    _edit(provider, "# summary\n", "# summary\nthis round\n")
    assert ws.written_report.findings == []  # A6: the old findings are not carried in
    out = _edit(provider, "# summary\nnot there", "x")
    # B18 (review_report erased): a refused call shows the draft as stored, text only
    assert out.is_error and "The draft on file reads:\n# summary\nthis round\n# status" in out.text
    assert out.figures == []


def test_the_tool_request_block_round_trips(tmp_path):
    provider, ws = _worker(tmp_path)
    out = _write(provider, doc(tool_request=(
        "request_kind: new_capability\nproposed_tool_name: fold_curve\n"
        "science_context: folding is missing\nexpected_behavior: folds a light curve\n"
        "  on a given period\nvalidation_data: MISSION 0100 synthetic"
    )))
    assert not out.is_error, out.text
    request = ws.written_report.tool_request
    assert request.proposed_tool_name == "fold_curve"
    assert request.expected_behavior == "folds a light curve\non a given period"
    assert request.validation_data[0].obsid == "0100" and request.validation_data[0].synthetic
    out = _edit(provider, "proposed_tool_name: fold_curve", "tool: fold_curve")
    assert out.is_error and "'tool' is not a key it takes" in out.text


# ── every block kind survives render → parse ─────────────────────────────────


def test_render_then_parse_is_the_identity_for_every_role():
    analyst = {
        "summary": "Line one.\n\nPara two.", "status": "succeeded", "rework": False,
        "rationale": "r", "what_to_probe": "p",
        "findings": ["a [x:r1]", "multi\nline\n- a dash inside\n\nafter a blank"],
        "cited_figures": [{"source": "analyst_worker_0", "index": 2}], "uncertainty": "u",
        "tool_request": {"request_kind": "fix_existing", "proposed_tool_name": "t",
                         "science_context": "s", "expected_behavior": "e\n  indented",
                         "observed_issue": "o", "notes": "n",
                         "validation_data": [{"mission": "M", "obsid": "1", "synthetic": False}]},
    }
    reviewer = {
        "assign_to": "human", "accepted": True, "rationale": "r", "follow_up": "f", "title": "T",
        "abstract": "A", "sections": [{"heading": "One", "body": "b1\n\n- bullet"}],
        "key_findings": ["k1", "k2"], "figures": ["analyst_worker_0:r3"],
        "tables": [{"caption": "C", "receipt": None, "columns": ["a", "b"], "rows": [["1", "2"]]},
                   {"caption": "D", "receipt": "analyst_worker_0:r4", "columns": [], "rows": []}],
    }
    librarian = {"summary": "s", "status": "failed",
                 "findings": [{"claim": "c\nmore", "support": ["lib:p1", "lib:w2"]}], "gaps": ["g"]}
    for values, blocks in ((analyst, ANALYST_BLOCKS), (reviewer, REVIEWER_BLOCKS),
                           (librarian, LIBRARIAN_BLOCKS)):
        document = render(values, blocks)
        parsed, problems = parse(document, blocks)
        assert problems == []
        assert render(parsed, blocks) == document  # the stored draft renders back to itself
        for key, value in values.items():
            if value is None:
                continue
            got = parsed.get(key)
            if key == "tables":
                got = [{**t, "receipt": t.get("receipt")} for t in got]
            assert got == value, key


def test_text_before_the_first_header_is_named():
    _, problems = parse("stray\n# summary\nx\n", blocks_for(("summary",), ANALYST_BLOCKS))
    assert problems and "belongs to no field" in problems[0]


def test_a_heading_inside_a_text_block_is_named_not_folded_in():
    blocks = blocks_for(("summary", "findings"), ANALYST_BLOCKS)
    _, problems = parse("# summary\nb\n# Results\n- x\n", blocks)
    assert problems and "'# Results' is not a block of this report" in problems[0]
    values, problems = parse("# summary\nb\n## Results\nmore\n", blocks)  # '## ' is text
    assert problems == [] and values["summary"] == "b\n## Results\nmore"


def test_a_claim_line_that_starts_with_support_stays_in_the_claim():
    values, problems = parse(
        "# findings\n- The lag\n  support: is great in one paper\n  support: lib:p1\n", LIBRARIAN_BLOCKS,
    )
    assert problems == []
    assert values["findings"] == [{"claim": "The lag\nsupport: is great in one paper", "support": ["lib:p1"]}]


# ── the lead, the reviewer and the librarian share it ────────────────────────


def test_the_lead_edits_its_decision_in_place(tmp_path):
    provider, ws = _lead(tmp_path)
    _write(provider, (
        "# summary\nAnother pass.\n# status\nfailed\n# rework\ntrue\n# rationale\nr\n"
        "# what_to_probe\np\n"
    ))
    out = _edit(provider, "# rework\ntrue\n", "# rework\nfalse\n")
    assert not out.is_error
    _edit(provider, "# findings\n", "# findings\n- settled\n")
    report = ws.written_report
    assert report.rework is False and report.rationale == "r" and report.findings == ["settled"]


def test_the_reviewer_edits_sections_and_figures_through_its_own_loop(tmp_path):
    """Built through the run path (ReviewerAgent.review wires the workspace, as roles.py does)."""
    from auto_research.schemas import ResearchRequest
    from tests.test_agents import _reviewer

    agent, fake = _reviewer(tmp_path, [
        (WRITE_REPORT_TOOL, {"content": (
            "# assign_to\nanalysts\n# rationale\nAdequate.\n# follow_up\nFit it.\n"
            "# title\nT\n# sections\n## One\nb1\n## Two\nb2\n"
        )}),
        (EDIT_REPORT_TOOL, {"old_string": "## Two\nb2", "new_string": "## Second\nb2'"}),
        (TURN_END_TOOL, {"summary": "dispatched"}),
    ])
    report = _run(agent.review(ResearchRequest(question="q"), budgets={"librarian": 1, "analysts": 1}))
    assert [s.heading for s in report.sections] == ["One", "Second"]
    assert report.title == "T" and report.follow_up == "Fit it."
    names = {t["name"] for t in fake.tools}
    assert {"write_report", "edit_report"} <= names


def test_the_librarian_edits_findings_and_gaps(tmp_path):
    from tests.test_librarian import _read_one

    provider = _read_one(tmp_path)
    out = _write(provider, (
        "# summary\nOne paper.\n# status\nsucceeded\n# findings\n"
        '- A lag of {{lib:p1 "12.5 ± 3.1 days" Table 1}}.\n  support: lib:p1\n# gaps\n- g1\n- g2\n'
    ))
    assert not out.is_error, out.text
    removed = _edit(provider, "- g1\n", "")
    out = _edit(provider, "support: lib:p1", "support: [lib:p1]")  # brackets read as the handle
    assert "(the draft on file is unchanged)" in out.text
    report = provider.written_report
    assert report.gaps == ["g2"] and report.summary == "One paper." and report.findings[0].support == ["lib:p1"]
    assert "\t# gaps\n" in removed.text and "\t- g2" in removed.text  # the edit's reply shows the change
    assert not _run(provider.execute("turn_end", {"summary": "done"})).is_error
    assert json.loads(provider.report_path.read_text())["gaps"] == ["g2"]


def test_every_surface_offers_edit_and_write_with_its_layout():
    spec = edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES)
    assert set(spec["input_schema"]["properties"]) == {"old_string", "new_string", "replace_all"}
    assert spec["input_schema"]["required"] == ["old_string", "new_string"]
    assert "# what_to_probe —" in spec["description"]
    schema = json.dumps(spec["input_schema"])
    assert "$ref" not in schema and "oneOf" not in schema


def test_a_header_with_a_slip_is_named_not_folded_in():
    blocks = blocks_for(("summary", "findings"), ANALYST_BLOCKS)
    for slip in ("# findings:", "# Findings:", "#findings", "# findings (draft)"):
        _, problems = parse(f"# summary\nhi\n{slip}\n- a\n", blocks)
        assert problems and "write the header as '# findings' on its own line" in problems[0], slip


def test_a_snippet_ending_at_the_last_line_has_no_phantom_line(tmp_path):
    provider, _ = _worker(tmp_path)
    _write(provider, doc(tool_request="notes: n"))
    out = _edit(provider, "notes: n\n", "")
    assert not out.text.rstrip().split("\n")[-2].rstrip().endswith("\t") and "\t\n" not in out.text


def test_support_keeps_only_handles_and_crlf_writes_store_cleanly(tmp_path):
    values, _ = parse("# findings\n- c\n  support: lib:p1 and lib:p2\n", LIBRARIAN_BLOCKS)
    assert values["findings"][0]["support"] == ["lib:p1", "lib:p2"]
    provider, ws = _worker(tmp_path)
    out = _write(provider, doc().replace("\n", "\r\n"))
    assert not out.is_error and out.text.startswith("Report written.\nDraft:")  # no echo needed


# ── B18: a figure's label where the figure first appears ─────────────────────


def test_a_tool_result_names_each_figure_with_its_citable_label(tmp_path):
    import hashlib
    from datetime import UTC, datetime

    from auto_research.agents.react import ReportWorkspace, SandboxToolProvider
    from tests.test_sandbox import _FakeRegistry, _FakeSandbox

    figs = []
    for name in ("fit", "residuals"):
        png = tmp_path / f"{name}.png"
        png.write_bytes(b"\x89PNG" + name.encode())
        figs.append({"path": str(png), "caption": f"{name} panel", "mime_type": "image/png",
                     "produced_by": "tk_plot", "created_at": datetime.now(UTC).isoformat(),
                     "sha256": hashlib.sha256(png.read_bytes()).hexdigest()})
    sink: list = []
    ws = ReportWorkspace(report_path=tmp_path / "w.jsonl", report_model=AnalystReport, catalog={"self": sink})
    provider = SandboxToolProvider(
        _FakeRegistry(known={"tk_plot"}, mcp=set()),
        _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "plotted", "figures": figs}),
        figure_sink=sink, report_workspace=ws,
    )
    out = _run(provider.execute("tk_plot", {}))
    assert "[self:1] fit panel" in out.text and "[self:2] residuals panel" in out.text
    again = _run(provider.execute("tk_plot", {}))  # the same figures keep their labels
    assert "[self:1] fit panel" in again.text and "[self:3]" not in again.text
    # the label places the figure
    assert not _write(provider, doc(figures=("self:2",))).is_error
    assert [f.caption for f in ws.written_report.figures] == ["residuals panel"]
    names = {d["name"] for d in provider.tool_definitions()}
    assert "review_report" not in names and {"write_report", "edit_report"} <= names


def test_the_lead_task_labels_every_worker_figure_and_carries_the_reports(tmp_path):
    from auto_research.agents.react import ReadOnlyToolProvider, TURN_END_TOOL as END
    from auto_research.agents.roles import AnalystLeadAgent
    from auto_research.models.types import ModelResponse, ToolCall
    from auto_research.schemas import AnalysisTask
    from tests.test_report_diagnostics import _fig

    class _Lead:
        def __init__(self):
            self.sent = []

        async def call(self, messages, **kwargs):
            self.sent.append(list(messages))
            if len(self.sent) == 1:
                return ModelResponse(tool_calls=[ToolCall(id="w1", name=WRITE_REPORT_TOOL, arguments={
                    "content": "# summary\ns\n# status\nsucceeded\n# rework\nfalse\n# findings\n- f\n"
                               "# figures\n- [analyst_worker_1:1]\n"})])
            return ModelResponse(tool_calls=[ToolCall(id="t1", name=END, arguments={"summary": "done"})])

    model = _Lead()
    lead = AnalystLeadAgent(model=model, prompt="p")
    lead.report_dir = tmp_path
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    r0 = AnalystReport(summary="worker zero text", findings=["a"], status="succeeded",
                       figures=[_fig(tmp_path, "a", "PCUBE")])
    b = _fig(tmp_path, "b", "bench").model_copy(update={"sha256": "1" * 64})
    r1 = AnalystReport(summary="worker one text", findings=["b"], status="succeeded", figures=[b])
    outcome = _run(lead.run_cycle(AnalysisTask(question="q"), [r0, r1],
                                  sources=["analyst_worker_0", "analyst_worker_1"]))
    task_text = model.sent[0][-1].content
    assert "[analyst_worker_0:1] PCUBE" in task_text and "[analyst_worker_1:1] bench" in task_text
    assert "worker zero text" in task_text and "worker one text" in task_text  # reports, whole
    assert outcome.report is not None and [f.caption for f in outcome.report.figures] == ["bench"]


def test_a_worker_task_lists_earlier_figures_and_the_tool_result_labels_new_ones(tmp_path):
    """Through AnalystWorkerAgent.analyze, as the run path wires the sink and the payload."""
    import hashlib
    from datetime import UTC, datetime

    from auto_research.agents.roles import AnalystWorkerAgent
    from tests.test_report_diagnostics import _fig
    from tests.test_sandbox import _FakeRegistry, _FakeSandbox, _script_model

    png = tmp_path / "fit.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfit")
    fig = {"path": str(png), "caption": "fit", "mime_type": "image/png", "produced_by": "tk_load",
           "created_at": datetime.now(UTC).isoformat(),
           "sha256": hashlib.sha256(png.read_bytes()).hexdigest()}
    model = _script_model()
    sent = []
    inner = model.call

    async def recording(messages, **kwargs):
        sent.append(list(messages))
        return await inner(messages, **kwargs)

    model.call = recording
    worker = AnalystWorkerAgent(
        model=model, available_toolkits=[],
        sandbox=_FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "ok", "figures": [fig]}),
        registry=_FakeRegistry(known={"tk_load"}, mcp=set()),
    )
    worker.report_dir = tmp_path / "w0"
    earlier = _fig(tmp_path, "old", "earlier round plot").model_copy(update={"sha256": "2" * 64})
    worker._figure_catalog["self"] = [earlier]
    from auto_research.schemas import AnalysisTask

    _run(worker.analyze(AnalysisTask(question="q")))
    task_text = sent[0][-1].content
    assert "your_figures" in task_text and "[self:1] earlier round plot" in task_text
    tool_result = str(sent[1])
    assert "[self:2] fit" in tool_result  # the new figure, labelled where it first appears


def test_the_lead_names_receipts_labels_images_and_may_view_a_placed_figure(tmp_path):
    from auto_research.agents.react import ReadOnlyToolProvider, TURN_END_TOOL as END
    from auto_research.agents.roles import AnalystLeadAgent, _receipt_scope
    from auto_research.models.types import ModelResponse, ToolCall
    from auto_research.provenance import ReceiptStore
    from auto_research.schemas import AnalysisTask, FigureRef

    store = ReceiptStore(tmp_path / "receipts")
    figs = []
    for key, name in (("analyst_worker_0", "plot"), ("analyst_worker_1", "plot")):
        png = tmp_path / f"{key}.png"
        png.write_bytes(b"\x89PNG" + key.encode())
        receipt = store.mint(source=key, kind="figure", produced_by="research_toolkit.t.plot",
                             path=png, caption=name, mime_type="image/png")
        figs.append((receipt, FigureRef(path=png, caption=name, mime_type="image/png",
                                         produced_by="t.plot", created_at=receipt.created_at,
                                         sha256=receipt.sha256)))
    reports = [AnalystReport(summary=f"w{i}", findings=["x"], status="succeeded", figures=[f])
               for i, (_, f) in enumerate(figs)]

    class _Lead:
        def __init__(self):
            self.sent, self.images = [], []

        async def call(self, messages, **kwargs):
            self.sent.append(list(messages))
            self.images.append(kwargs.get("images"))
            if len(self.sent) == 1:
                return ModelResponse(tool_calls=[ToolCall(id="w", name=WRITE_REPORT_TOOL, arguments={
                    "content": "# summary\ns\n# status\nsucceeded\n# rework\nfalse\n# findings\n- f\n"})])
            return ModelResponse(tool_calls=[ToolCall(id="t", name=END, arguments={"summary": "d"})])

    model = _Lead()
    lead = AnalystLeadAgent(model=model, prompt="p")
    lead.report_dir = tmp_path / "lead"
    lead.receipts = store
    lead.tools = ReadOnlyToolProvider(roots=[tmp_path])
    _run(lead.run_cycle(AnalysisTask(question="q"), reports, sources=["analyst_worker_0", "analyst_worker_1"]))
    text = model.sent[0][-1].content
    for (receipt, _), key in zip(figs, ("analyst_worker_0", "analyst_worker_1")):
        assert f"[{key}:1] plot (receipt {receipt.handle})" in text
    # two plots with one caption are told apart: each attached image is captioned by its label
    captions = [f.caption for f in (model.sent[0][-1].figures or [])] or [
        f.caption for f in (model.images[0] or [])
    ]
    assert {"[analyst_worker_0:1] plot", "[analyst_worker_1:1] plot"} <= set(captions)
    # a placed figure's receipt is in the lead's view, so view_receipt can open it
    scope = _receipt_scope(store, "analyst_lead", reports)
    assert all(scope.visible(r.handle) for r, _ in figs)
