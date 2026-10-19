"""Deterministic tests for the report system's diagnostics (plan rows A1–A4, run 32814459).

Each shape the run produced is fed through the providers' own `execute` (the path a model's call
takes: `SandboxToolProvider` for a worker, `ReadOnlyToolProvider` for the lead, the librarian's
provider), and the reply is asserted to name the problem and its fix. Since A7 the draft is one
text document written with `write_report` and edited with `edit_report` (tests/test_report_edit.py):

* parameter markup inside one argument (`</parameter>\\n<parameter name="findings">…`): 49 of
  worker 0's 51 calls and 21 of the lead's — saved as the text it is (ruling 2026-10-18: no special
  case), the outline naming what it left empty;
* a figure line that is not a handle, or names a figure the catalog does not hold;
* results written into `summary` (a 22,033-character summary with no findings was filed) — a
  warning whenever the summary is longer than the findings combined, at `edit_report` /
  `write_report` and at `turn_end`, which files the report anyway (ruling 2026-10-18).

And `turn_end` names the field and the fix: a worker's `succeeded` report needs findings (A4), and
the figure refusal offers only the action that exists.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from auto_research.agents.react import (
    EDIT_REPORT_TOOL,
    LEAD_REPORT_FIELDS,
    LEAD_STATUS_VALUES,
    TURN_END_TOOL,
    WORKER_REPORT_FIELDS,
    WORKER_STATUS_VALUES,
    WRITE_REPORT_TOOL,
    LeadCycle,
    ReadOnlyToolProvider,
    ReportWorkspace,
    SandboxToolProvider,
    edit_report_tool_def,
    lead_turn_problems,
    write_report_tool_def,
)
from auto_research.schemas import AnalystReport, FigureRef


def _run(coro):
    return asyncio.run(coro)


def _fig(tmp_path, name: str, caption: str) -> FigureRef:
    path = tmp_path / f"{name}.png"
    path.write_bytes(b"\x89PNG" + name.encode())
    return FigureRef(
        path=path, caption=caption, mime_type="image/png", produced_by="pkg.lib.plot.plot_x",
        created_at=datetime(2026, 10, 18, tzinfo=UTC), sha256="0" * 64,
    )


class _StubRegistry:
    def tool_definitions(self):
        return []


def _worker(tmp_path, n_figures: int = 3):
    figs = [_fig(tmp_path, f"f{i}", f"panel {i}") for i in range(1, n_figures + 1)]
    # distinct hashes: placement keys on (path, sha256)
    figs = [f.model_copy(update={"sha256": f"{i:064x}"}) for i, f in enumerate(figs, 1)]
    ws = ReportWorkspace(
        report_path=tmp_path / "worker.jsonl", report_model=AnalystReport, catalog={"self": figs},
    )
    return SandboxToolProvider(_StubRegistry(), object(), report_workspace=ws), ws


def _lead(tmp_path, catalog=None, rounds_left=1):
    ws = ReportWorkspace(
        report_path=tmp_path / "lead.jsonl", report_model=AnalystReport,
        catalog=catalog if catalog is not None else {"analyst_worker_0": [], "analyst_worker_1": []},
        prose_fields=LEAD_REPORT_FIELDS, status_values=LEAD_STATUS_VALUES,
    )
    cycle = LeadCycle(rounds_left=rounds_left)
    provider = ReadOnlyToolProvider(
        roots=[tmp_path], cycle=cycle, report_workspace=ws,
        turn_end_validator=lambda: lead_turn_problems(cycle, ws),
    )
    return provider, ws


def doc(summary="The fit converged. Figure 1 shows it.", status="succeeded",
        findings=("The fit converged on the first try.",), figures=("self:1", "self:2"), **blocks):
    """A worker's draft document, as a model writes it with write_report."""
    lines = ["# summary", summary or "", "# status", status or "", "# findings"]
    lines += [f"- {f}" for f in findings]
    lines += ["# figures"] + [f"- [{h}]" for h in figures]
    for name, body in blocks.items():
        lines += [f"# {name}", body]
    return "\n".join(lines) + "\n"


def _write(provider, content):
    return _run(provider.execute(WRITE_REPORT_TOOL, {"content": content}))


# The leaked shape, as seq 7294 carried it: every field inside `summary`.
_LEAKED_SUMMARY = (
    "Theory pass: Task A verification and Task B verdicts.</parameter>\n"
    '<parameter name="findings">["TASK A1 -- ratio evaluates to 1."]</parameter>\n'
    '<parameter name="cited_figures">[{"source": "self", "index": 17}]</parameter>\n'
    '<parameter name="status">succeeded'
)


# ── A2: replies name each problem ────────────────────────────────────────────


def test_a_figure_line_that_is_not_a_handle_is_named(tmp_path):
    provider, ws = _worker(tmp_path)
    out = _write(provider, doc(figures=("self 2",)))
    assert out.is_error and "nothing changed" in out.text
    assert "is not a figure handle; write one '- [source:index]' line per figure" in out.text
    assert ws.written_report is None


def test_a_figure_the_catalog_does_not_hold_is_refused(tmp_path):
    provider, ws = _worker(tmp_path)
    _write(provider, doc())
    out = _write(provider, doc(figures=("self:9",)))
    assert out.is_error and "# figures line 1: self:9 — out of range (source 'self' has 3)" in out.text
    assert len(ws.written_report.figures) == 2  # the draft on file is unchanged


def test_a_draft_without_status_shows_it_reads_as_failed(tmp_path):
    provider, ws = _worker(tmp_path)
    out = _write(provider, doc(status=None))
    assert not out.is_error and "status empty (reads as 'failed')" in out.text
    assert ws.written_report.status == "failed"


def test_a_failed_report_with_no_findings_is_not_told_to_add_findings(tmp_path):
    provider, _ = _worker(tmp_path)
    out = _write(provider, doc(findings=(), status="failed"))
    assert "findings is empty" not in out.text
    assert "Warning:" not in out.text  # an honest failure has no results to move
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "blocked"}))
    assert not end.is_error and "Note for your next report" not in end.text


def test_markup_in_a_text_field_is_saved_as_text_and_the_outline_shows_what_it_left_out(tmp_path):
    """Ruling 2026-10-18: no special case for merged parameters; the outline carries it."""
    provider, ws = _worker(tmp_path)
    out = _write(provider, f"# summary\n{_LEAKED_SUMMARY}\n")
    assert not out.is_error
    assert "</parameter>" in ws.written_report.summary  # saved as the text it is
    assert "status empty (reads as 'failed')" in out.text and "findings empty" in out.text
    # Residual of the ruling: read as an honest 'failed', it carries no results, so turn_end
    # files it once no other check refuses (here the catalog's figures still do).
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert end.is_error and "places none" in end.text


def test_a_summary_longer_than_the_findings_is_warned_at_write_edit_and_turn_end(tmp_path):
    provider, ws = _worker(tmp_path)
    long_summary = " ".join(f"Row {i} gave a value of {i}.5 in the table." for i in range(40))
    out = _write(provider, doc(summary=long_summary, findings=("Short.",)))
    assert not out.is_error
    assert f"Warning: summary ({len(long_summary)} characters) is longer than findings (6 characters" in out.text
    out = _run(provider.execute(EDIT_REPORT_TOOL, {"old_string": "- Short.", "new_string": "- Short!"}))
    assert not out.is_error and "Warning: summary (" in out.text
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert not end.is_error  # a warning; the report is filed as it stands
    assert end.text.startswith("Turn ended.")
    assert "Your report is filed as it stands. Note for your next report: Warning: summary (" in end.text


def test_a_briefing_shorter_than_its_findings_draws_no_warning(tmp_path):
    provider, _ = _worker(tmp_path)
    out = _write(provider, doc(
        findings=("The fit converged on the first try with the default settings and a clean residual.",),
    ))
    assert "Warning:" not in out.text and "findings is empty" not in out.text
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert not end.is_error and "Warning:" not in end.text


def test_a_succeeded_report_with_no_findings_is_told_the_turn_end_rule(tmp_path):
    provider, _ = _worker(tmp_path)
    out = _write(provider, doc(findings=()))
    assert "findings is empty" in out.text and "refuses this report while findings is empty" in out.text
    assert "Warning:" not in out.text  # one note for one problem


def test_the_warning_rides_beside_a_refusal(tmp_path):
    provider, _ = _worker(tmp_path, n_figures=2)
    long_summary = "A long briefing that holds the results. " * 10
    _write(provider, doc(summary=long_summary, findings=("Short.",), figures=()))
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert end.is_error and "places none" in end.text
    fix_at = end.text.index("Fix these with your tools")
    assert end.text.index("Also noted (a warning; turn_end files the report with it): Warning: summary (") > fix_at


def test_a_lead_rework_request_draws_no_summary_warning(tmp_path):
    provider, _ = _lead(tmp_path)
    out = _write(provider, (
        "# summary\nThe fits disagree and one more pass would settle which holds.\n# status\nfailed\n"
        "# rework\ntrue\n# rationale\nThe fits disagree.\n# what_to_probe\nRefit both.\n"
    ))
    assert not out.is_error and "Warning:" not in out.text
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "again"}))
    assert not end.is_error and "Warning:" not in end.text


def test_a_lead_conclusion_is_warned_at_turn_end(tmp_path):
    provider, _ = _lead(tmp_path)
    _write(provider, (
        "# summary\n" + "A long synthesis of everything the workers established. " * 5
        + "\n# status\nsucceeded\n# rework\nfalse\n# findings\n- One result.\n"
    ))
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert not end.is_error and "Warning: summary (" in end.text


def lib_doc(support="lib:p1", status="succeeded", summary="One paper measures the lag."):
    return (
        f"# summary\n{summary}\n# status\n{status}\n# findings\n"
        '- A lag of {{lib:p1 "12.5 ± 3.1 days" Table 1}}.\n'
        f"  support: {support}\n"
    )


def test_librarian_summary_warning_and_per_pass_handle_message(tmp_path):
    from tests.test_librarian import _read_one

    provider = _read_one(tmp_path)
    out = _run(provider.execute("write_report", {"content": lib_doc(
        summary="One paper was searched for, opened and read in full for this request. " * 4,
    )}))
    assert not out.is_error and "Warning: summary (" in out.text
    assert "naming in support the documents behind it" in out.text
    end = _run(provider.execute("turn_end", {"summary": "done"}))
    assert not end.is_error and "Warning: summary (" in end.text

    provider = _read_one(tmp_path / "second")
    out = _run(provider.execute("write_report", {"content": lib_doc(support="lib:p1, lib:p7")}))
    assert "not documents you opened in this pass: lib:p7" in out.text
    end = _run(provider.execute("turn_end", {"summary": "done"}))
    assert end.is_error and "not a document you opened in this pass" in end.text
    assert "Open it in this pass with read_paper (or web_fetch) to cite it" in end.text


# ── A3/A4: turn_end names the field and the fix ──────────────────────────────


def test_a_succeeded_worker_report_with_no_findings_is_refused(tmp_path):
    provider, _ = _worker(tmp_path)
    _write(provider, doc(findings=()))
    out = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert out.is_error
    assert "status 'succeeded' and lists no findings" in out.text
    assert "under # findings" in out.text and "status 'failed'" in out.text


def test_a_failed_worker_report_with_no_findings_is_accepted(tmp_path):
    provider, _ = _worker(tmp_path)
    _write(provider, doc(findings=(), status="failed"))
    out = _run(provider.execute(TURN_END_TOOL, {"summary": "blocked"}))
    assert not out.is_error, out.text


def test_a_complete_worker_report_is_accepted(tmp_path):
    provider, _ = _worker(tmp_path)
    _write(provider, doc())
    out = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert not out.is_error, out.text


def test_the_figure_refusal_names_the_action_that_exists(tmp_path):
    provider, _ = _worker(tmp_path, n_figures=19)
    _write(provider, doc(figures=()))
    out = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert out.is_error and "19 figure(s) are in your catalog and the report places none" in out.text
    assert "'- [self:1]'" in out.text and "# figures" in out.text
    assert "say why none apply" not in out.text


def test_an_empty_summary_refusal_says_what_to_write(tmp_path):
    provider, _ = _worker(tmp_path)
    assert not _write(provider, doc(summary=None)).is_error
    out = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert "summary is empty. Write a 3–5-sentence briefing" in out.text


def test_lead_conclusion_without_findings_names_findings_and_the_rework_route(tmp_path):
    provider, ws = _lead(tmp_path)
    out = _write(provider, "# summary\nDone.\n# status\nfailed\n# rework\nfalse\n")
    # the lead's conclusion needs findings whatever its status, and the save note says so
    assert "findings is empty" in out.text and "refuses this report while findings is empty" in out.text
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert end.is_error and "lists no findings" in end.text and "set rework to true" in end.text
    _run(provider.execute(EDIT_REPORT_TOOL, {
        "old_string": "# rework\nfalse\n# rationale\n# what_to_probe\n",
        "new_string": "# rework\ntrue\n# rationale\nThe fits disagree.\n# what_to_probe\nRefit both.\n",
    }))
    assert not _run(provider.execute(TURN_END_TOOL, {"summary": "again"})).is_error


def test_lead_at_the_round_cap_is_not_offered_rework(tmp_path):
    provider, _ = _lead(tmp_path, rounds_left=0)
    _write(provider, "# summary\nDone.\n# status\nfailed\n# rework\nfalse\n")
    end = _run(provider.execute(TURN_END_TOOL, {"summary": "done"}))
    assert "lists no findings" in end.text and "set rework to true" not in end.text
    assert "a finding may state what is not settled" in end.text


# ── A1: the form, as the model reads it ─────────────────────────────────────


def test_the_worker_places_self_figures_and_the_lead_places_workers():
    worker = edit_report_tool_def(WORKER_REPORT_FIELDS, WORKER_STATUS_VALUES)["description"]
    lead = edit_report_tool_def(LEAD_REPORT_FIELDS, LEAD_STATUS_VALUES)["description"]
    assert "'- [self:index]'" in worker and "analyst_worker_0" not in worker
    assert "'- [analyst_worker_0:3]'" in lead
    assert "# rework" in lead and "# rework" not in worker
    assert "# tool_request" in worker and "# tool_request" not in lead
    assert "succeeded or failed or needs_human" in lead and "needs_human" not in worker


def test_the_form_defines_summary_and_findings_everywhere_the_model_reads_it():
    for tool in (edit_report_tool_def(WORKER_REPORT_FIELDS, WORKER_STATUS_VALUES),
                 write_report_tool_def(WORKER_REPORT_FIELDS, WORKER_STATUS_VALUES)):
        assert "# summary — a briefing of 3–5 sentences" in tool["description"]
        assert "# findings — the results: one '- ' item per result" in tool["description"]
    assert "3–5 sentences" in AnalystReport.model_fields["summary"].description
    assert "one item per result" in AnalystReport.model_fields["findings"].description
