"""The reviewer's one report: its schema, the renderer that turns it into LaTeX, and its turn_end check."""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_research.agents.reviewer_tools import (
    REVIEWER_REPORT_FIELDS,
    ReviewerRender,
    reviewer_turn_problems,
)
from auto_research.provenance import ReceiptStore
from auto_research.report.render import (
    _glyphs,
    compile_report,
    latex_escape,
    parse_delimited,
    render_report,
)
from auto_research.schemas.review import ReviewerReport, ReviewerSection, ReviewerTable


def _png(path: Path, rgb: bytes = b"\xff\x00\x00") -> Path:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + rgb))
        + chunk(b"IEND", b"")
    )
    return path


@pytest.fixture
def store(tmp_path: Path) -> ReceiptStore:
    s = ReceiptStore(tmp_path / "receipts")
    s.mint(source="analyst_lead", kind="result", produced_by="research_toolkit.xspec_utils.fit",
           data="PD = 4.02 ± 0.21 %\nPA = −20.7 ± 1.4 deg\nchi2/dof = 1.8")
    s.mint(source="analyst_lead", kind="figure", produced_by="research_toolkit.t.plotter",
           path=_png(tmp_path / "a" / "plot.png"), caption="PD vs energy", mime_type="image/png")
    s.mint(source="analyst_lead", kind="figure", produced_by="research_toolkit.t.plotter",
           path=_png(tmp_path / "b" / "plot.png", b"\x00\x00\xff"), caption="PA vs energy",
           mime_type="image/png")
    csv_path = tmp_path / "cmp.csv"
    csv_path.write_text("# produced by compare_to_benchmark\nquantity,this_work,paper\nPD,4.02,4.0\nPA,-20.7,-20.7\n")
    s.mint(source="analyst_lead", kind="data", produced_by="research_toolkit.xspec_utils.compare_to_benchmark", path=csv_path)
    s.mint(source="lib", kind="paper", produced_by="read_paper",
           data="We measure PD = 4.0 ± 0.2 % in the 2-8 keV band.",
           title="Polarized X-rays from Cyg X-1", ids={"arxiv": "2206.09972", "bibcode": "2020A&A...1K"})
    return s


def _final(**overrides) -> ReviewerReport:
    fields = dict(
        accepted=True,
        assign_to="human",
        rationale='Agrees with the literature [lib:p1].',
        title="Cyg X-1 polarization",
        abstract='We find {{analyst_lead:r1 "PD = 4.02 ± 0.21 %"}}, consistent with {{lib:p1 "PD = 4.0 ± 0.2 %" Table 2}}.',
        sections=[ReviewerSection(heading="Results", body='Figure 1 shows it [analyst_lead:f1]. Table 1 compares.')],
        key_findings=['The fit gives {{analyst_lead:r1 "chi2/dof = 1.8"}}.'],
        figures=["analyst_lead:f1"],
        tables=[ReviewerTable(caption="Benchmark", receipt="analyst_lead:d1")],
    )
    fields.update(overrides)
    return ReviewerReport(**fields)


def _scope(store: ReceiptStore) -> list[str]:
    return [r.handle for r in store.receipts()]


# -- schema -----------------------------------------------------------------------------------


def test_the_schema_is_permissive_so_a_partial_write_is_legal():
    report = ReviewerReport()
    assert report.assign_to is None and report.prose_fields() == {}


def test_every_declared_field_is_a_real_field():
    assert set(REVIEWER_REPORT_FIELDS) <= set(ReviewerReport.model_fields)


def test_prose_fields_name_every_piece_of_prose_in_reading_order():
    report = _final(
        follow_up="go",
        tables=[ReviewerTable(caption="Built", columns=["q", "v"], rows=[["PD", "x"]])],
    )
    assert list(report.prose_fields()) == [
        "rationale", "follow_up", "title", "abstract", "section 1 heading", "section 1",
        "key_findings", "table 1 caption", "table 1 columns", "table 1 row 1",
    ]


# -- parsing and escaping ---------------------------------------------------------------------


def test_parse_delimited_reads_csv_tsv_and_pipe_rows_and_refuses_the_rest():
    assert parse_delimited("# header\na,b\n1,2\n") == [["a", "b"], ["1", "2"]]
    assert parse_delimited("a\tb\n1\t2\n") == [["a", "b"], ["1", "2"]]
    assert parse_delimited("| a | b |\n|---|---|\n| 1 | 2 |\n") == [["a", "b"], ["1", "2"]]
    assert parse_delimited("PD   4.02\nPA   -20.7\n") is None  # whitespace-aligned: not guessed
    assert parse_delimited("a,b\n1,2,3\n") is None
    assert parse_delimited("only a header\n") is None


def test_latex_escape_covers_the_specials_and_leaves_unicode_alone():
    assert latex_escape("50% of a_b & #1 {x} ~ ^ $ \\ ± χ²") == (
        r"50\% of a\_b \& \#1 \{x\} \textasciitilde{} \textasciicircum{} \$ \textbackslash{} ± χ²"
    )


# -- rendering --------------------------------------------------------------------------------


def test_a_quote_is_typeset_as_the_receipt_wrote_it(store, tmp_path):
    result = render_report(_final(), store, tmp_path / "report", run_id="r1")
    assert result.problems == []
    tex = result.tex_path.read_text()
    assert "PD = 4.02 ± 0.21 \\%" in tex
    assert "{{" not in tex  # no binding survives into the LaTeX


def test_figures_are_staged_by_content_hash_so_shared_basenames_never_collide(store, tmp_path):
    report = _final(figures=["analyst_lead:f1", "analyst_lead:f2"])
    result = render_report(report, store, tmp_path / "report", run_id="r1")
    relpaths = [rel for _, rel in result.figures]
    assert len(set(relpaths)) == 2
    for receipt, rel in result.figures:
        assert rel == f"figures/{receipt.sha256[:12]}.png"
        assert (tmp_path / "report" / rel).read_bytes() == Path(receipt.path).read_bytes()


def test_a_figure_whose_bytes_changed_is_staged_as_it_is_now_with_an_alert(store, tmp_path):
    """F26, human ruling 2026-10-06: changed after its receipt → still staged, alerted."""
    from auto_research.provenance import store as S

    alerts = []
    token = S._DRIFT_SINK.set(lambda summary, data: alerts.append(summary))
    try:
        Path(store.get("analyst_lead:f1").path).write_bytes(b"not the plot")
        result = render_report(_final(), store, tmp_path / "report", run_id="r1")
    finally:
        S._DRIFT_SINK.reset(token)
    assert not any("analyst_lead:f1" in p for p in result.problems), result.problems
    assert alerts and any("analyst_lead:f1" in a for a in alerts)
    staged = list((tmp_path / "report" / "figures").glob("*"))
    assert any(f.read_bytes() == b"not the plot" for f in staged)


def test_a_non_figure_in_figures_is_refused(store, tmp_path):
    result = render_report(_final(figures=["analyst_lead:r1"]), store, tmp_path / "report", run_id="r1")
    assert any("not a figure" in p for p in result.problems)


def test_a_tool_computed_table_is_typeset_from_its_receipt(store, tmp_path):
    tex = render_report(_final(), store, tmp_path / "report", run_id="r1").tex_path.read_text()
    assert "PD & 4.02 & 4.0" in tex and "produced by compare_to_benchmark" not in tex


def test_a_table_receipt_that_is_not_delimited_is_refused(store, tmp_path):
    report = _final(tables=[ReviewerTable(caption="Fit", receipt="analyst_lead:r1")])
    result = render_report(report, store, tmp_path / "report", run_id="r1")
    assert any("delimited table" in p for p in result.problems)


def test_a_table_filling_both_forms_or_neither_is_refused(store, tmp_path):
    both = ReviewerTable(caption="x", receipt="analyst_lead:d1", columns=["a", "b"], rows=[["1", "2"]])
    neither = ReviewerTable(caption="y")
    result = render_report(_final(tables=[both, neither]), store, tmp_path / "report", run_id="r1")
    assert any("Fill one form" in p for p in result.problems)
    assert any("neither a receipt" in p for p in result.problems)


def test_a_built_table_with_a_ragged_row_is_refused(store, tmp_path):
    table = ReviewerTable(caption="Built", columns=["q", "v"], rows=[["PD"]])
    result = render_report(_final(tables=[table]), store, tmp_path / "report", run_id="r1")
    assert any("different number of cells" in p for p in result.problems)


def test_a_paper_value_is_typeset_with_its_reference_and_listed_for_verification(store, tmp_path):
    tex = render_report(_final(), store, tmp_path / "report", run_id="r1").tex_path.read_text()
    assert "PD = 4.0 ± 0.2 \\%~\\protect\\hyperlink{ref-1}{[1]}" in tex
    assert "Values to verify against the PDFs" in tex and "Table 2" in tex


def test_a_bare_paper_number_is_refused_with_a_hint_to_attribute_it(store):
    report = _final(key_findings=["The paper reports 7.7 % [lib:p1]."])
    assert any("lib:p1" in p for p in _problems(report, store))


def test_the_text_view_shows_the_draft_and_its_figures_when_no_tex_is_configured(store):
    from auto_research.agents.reviewer_tools import reviewer_text_view

    text, figures = reviewer_text_view(store)(_final())
    assert "abstract:" in text and "Figure 1: [analyst_lead:f1]" in text
    assert [f.caption for f in figures] == ["Figure 1 — PD vs energy"]


def test_references_come_from_the_cited_paper_receipts(store, tmp_path):
    tex = render_report(_final(), store, tmp_path / "report", run_id="r1").tex_path.read_text()
    assert "Polarized X-rays from Cyg X-1" in tex
    assert "https://arxiv.org/abs/2206.09972" in tex
    # "&" in a bibcode is percent-encoded in the URL and escaped in the display text.
    assert r"abs/2020A\%26A...1K" in tex and r"2020A\&A...1K" in tex


def test_the_appendix_lists_every_cited_receipt_with_its_hash(store, tmp_path):
    result = render_report(_final(), store, tmp_path / "report", run_id="r1")
    tex = result.tex_path.read_text()
    assert set(result.cited) == {"analyst_lead:r1", "lib:p1", "analyst_lead:f1", "analyst_lead:d1"}
    for handle in result.cited:
        assert store.get(handle).sha256[:12] in tex


def test_missing_glyphs_are_named_for_the_model():
    log = "Missing character: There is no ☃ (U+2603) in font [DejaVuSerif]:mode=harf;!\n" * 2
    [problem] = _glyphs(log)
    assert "☃ (U+2603)" in problem


@pytest.mark.skipif(not shutil.which("lualatex"), reason="lualatex not installed")
def test_a_rendered_report_compiles_to_a_pdf(store, tmp_path):
    report = _final(abstract=_final().abstract + " Symbols: χ² ≲ σ.")
    render_report(report, store, tmp_path / "report", run_id="run_1")
    result = compile_report(tmp_path / "report")
    assert result.errors == [] and result.glyph_problems == []
    assert result.pdf_path is not None and result.pdf_path.is_file()


# -- turn_end ---------------------------------------------------------------------------------


def _problems(report, store, *, lit=1, ana=1, render=None):
    return reviewer_turn_problems(
        report, lookup=store, scope=_scope(store), budgets={"librarian": lit, "analysts": ana},
        render=render,
    )


def test_no_report_is_refused(store):
    assert "No report has been written" in _problems(None, store)[0]


def test_a_complete_report_to_the_human_passes(store):
    assert _problems(_final(), store) == []


def test_a_report_with_no_assignee_is_refused(store):
    problems = _problems(_final(assign_to=None), store)
    assert any("assign_to is empty" in p for p in problems)


def test_a_report_to_the_human_needs_its_parts(store):
    problems = _problems(ReviewerReport(rationale="Holds [lib:p1].", assign_to="human"), store)
    for name in ("title", "abstract", "sections", "key_findings"):
        assert any(f"and {name} is empty" in p for p in problems)


@pytest.mark.parametrize("team,cap_words", [("librarian", "literature passes"),
                                            ("analysts", "analysis passes")])
def test_an_assignment_needs_a_follow_up_and_a_pass_left(store, team, cap_words):
    report = ReviewerReport(rationale="Short [lib:p1].", assign_to=team)
    problems = _problems(report, store, lit=0, ana=0)
    assert any("follow_up is empty" in p and team in p for p in problems)
    assert any(f"No {cap_words} remain" in p for p in problems)


def test_the_other_teams_cap_does_not_block_an_assignment(store):
    report = ReviewerReport(rationale="Short [lib:p1].", assign_to="analysts", follow_up="Refit.")
    assert _problems(report, store, lit=0, ana=1) == []


def test_an_assignment_does_not_need_the_report_parts(store):
    report = ReviewerReport(rationale="Short [lib:p1].", assign_to="librarian", follow_up="Search.")
    assert _problems(report, store) == []


def test_an_unknown_assignee_is_refused_by_the_schema():
    with pytest.raises(ValidationError):
        ReviewerReport(rationale="r", assign_to="END")


def test_a_brief_prescribes_numbers_without_grounding_but_a_claim_does_not(store):
    # Human ruling 2026-09-25: follow_up is a prescription, not a claim.
    brief = ReviewerReport(rationale="r [lib:p1]", assign_to="analysts",
                           follow_up="Fit the 3-7 keV band from starting spins 0, 0.5 and 0.9.")
    assert _problems(brief, store) == []
    claim = ReviewerReport(rationale="The fit used 3-7 keV [lib:p1].", assign_to="analysts",
                           follow_up="Refit.")
    assert any("rationale states numbers" in p for p in _problems(claim, store))


def test_any_readable_receipt_grounds_any_field(store):
    # The abstract cites nothing, yet its number is in a receipt the reviewer can read.
    assert _problems(_final(abstract="PD is 4.02 here."), store) == []


def test_a_broken_receipt_the_report_never_cites_is_not_the_models_problem(store):
    store.mint(source="analyst_lead", kind="result", produced_by="research_toolkit.t.x", data="unrelated 99.9")
    Path(store.get("analyst_lead:r2").path).write_text("tampered")
    assert _problems(_final(), store) == []


def test_render_runs_only_for_a_report_to_the_human_with_nothing_else_to_fix(store, tmp_path):
    render = ReviewerRender(report_dir=tmp_path / "report", run_id="r1", formats=("jsonl",))
    assert _problems(_final(), store, render=render) == [] and render.attempts == 0
    render = ReviewerRender(report_dir=tmp_path / "report", run_id="r1")
    _problems(_final(title=""), store, render=render)
    assert render.attempts == 0  # a missing title is fixed before anything is rendered
    _problems(_final(assign_to="analysts", follow_up="Refit."), store, render=render)
    assert render.attempts == 0  # a report to a team is never rendered


@pytest.mark.skipif(not shutil.which("lualatex"), reason="lualatex not installed")
def test_a_turn_end_to_the_human_renders_and_compiles(store, tmp_path):
    render = ReviewerRender(report_dir=tmp_path / "report", run_id="r1")
    assert _problems(_final(), store, render=render) == []
    assert render.compile.pdf_path.is_file() and render.renderer_errors == []


# -- the reviewer's own tools -----------------------------------------------------------------


from auto_research.agents.reviewer_tools import reviewer_draft_view  # noqa: E402


@pytest.mark.skipif(not (shutil.which("lualatex") and shutil.which("pdftoppm")), reason="needs lualatex + pdftoppm")
def test_the_draft_view_shows_rendered_pages(store, tmp_path):
    view = reviewer_draft_view(tmp_path / "report", "r1", store)
    text, pages = view(_final())
    assert pages and pages[0].caption == "Draft page 1" and "page image" in text
    assert not (tmp_path / "report" / "report.tex").exists()  # the final report is untouched


def test_the_draft_view_shows_render_problems_instead_of_failing(store, tmp_path):
    view = reviewer_draft_view(tmp_path / "report", "r1", store)
    text, pages = view(_final(figures=["analyst_lead:r1"]))
    assert pages == [] and "cannot be rendered yet" in text and "not a figure" in text


def test_a_number_from_the_request_grounds_only_where_the_request_is_cited(store):
    from auto_research.agents.reviewer_tools import reviewer_scope
    from auto_research.provenance import ScopedLookup

    request = store.mint_input("Reproduce PD = 7.7 % from ObsID 01002901.", kind="human",
                               produced_by="research_request")
    lookup = ScopedLookup(store, own_source="rev")
    assert request.handle not in reviewer_scope(lookup)
    budgets = {"librarian": 1, "analysts": 1}
    uncited = ReviewerReport(rationale="Check the 7.7 value.", assign_to="librarian", follow_up="Search.")
    assert any("7.7" in p for p in reviewer_turn_problems(
        uncited, lookup=lookup, scope=reviewer_scope(lookup), budgets=budgets))
    cited = ReviewerReport(rationale=f"Check the 7.7 value [{request.handle}].", assign_to="librarian",
                           follow_up="Search.")
    assert reviewer_turn_problems(
        cited, lookup=lookup, scope=reviewer_scope(lookup), budgets=budgets) == []


def test_an_earlier_published_report_neither_grounds_nor_can_be_cited(store, tmp_path):
    from auto_research.agents.reviewer_tools import reviewer_scope
    from auto_research.provenance import ScopedLookup

    earlier = tmp_path / "report.jsonl"
    earlier.write_text('{"request_question": "Reproduce PD = 7.7 %", "created_at": "17.918273"}')
    published = store.mint(source="rev", kind="data", produced_by="report_renderer", path=earlier)
    lookup = ScopedLookup(store, own_source="rev")
    budgets = {"librarian": 1, "analysts": 1}
    uncited = ReviewerReport(rationale="Check the 7.7 value.", assign_to="analysts", follow_up="Refit.")
    assert any("7.7" in p for p in reviewer_turn_problems(
        uncited, lookup=lookup, scope=reviewer_scope(lookup), budgets=budgets))
    cited = ReviewerReport(rationale=f"Check the 7.7 value [{published.handle}].", assign_to="analysts",
                           follow_up="Refit.")
    assert any("earlier published report" in p for p in reviewer_turn_problems(
        cited, lookup=lookup, scope=reviewer_scope(lookup), budgets=budgets))


def test_every_turn_end_clause_has_a_live_attack_cell(store, tmp_path):
    """Free: each live attack cell's refusal text is one this check really returns, and every
    refusal the check can return has a cell (tests/test_live_reviewer_paths.py::CLAUSES)."""
    import inspect

    from auto_research.agents import reviewer_tools
    from tests.test_live_reviewer_paths import CLAUSES

    request = store.mint_input("Fit ObsID 30001011002.", kind="human", produced_by="research_request")
    published_file = tmp_path / "earlier.jsonl"
    published_file.write_text('{"spin": "0.973"}')
    published = store.mint(source="rev", kind="data", produced_by="report_renderer", path=published_file)
    task = dict(rationale="r [lib:p1]", assign_to="analysts", follow_up="Refit.")
    triggers = {
        "no_report": (None, {}),
        "empty_rationale": (ReviewerReport(**{**task, "rationale": ""}), {}),
        "no_assignee": (_final(assign_to=None), {}),
        "empty_follow_up": (ReviewerReport(**{**task, "follow_up": ""}), {}),
        "cap_spent": (ReviewerReport(**task), {"ana": 0}),
        "human_missing_parts": (ReviewerReport(rationale="r [lib:p1]", assign_to="human", title="T"), {}),
        "cites_published_report": (ReviewerReport(**{**task, "rationale": f"Check [{published.handle}]."}), {}),
        "ungrounded_number": (_final(abstract="The spin is 0.8127."), {}),
        "uncited_input": (ReviewerReport(**{**task, "rationale": "Fit ObsID 30001011002."}), {}),
        "render_refused": (_final(figures=["analyst_lead:r1"]),
                           {"render": ReviewerRender(report_dir=tmp_path / "r", run_id="r1")}),
    }
    assert set(triggers) == set(CLAUSES), "a live attack cell has no deterministic trigger here"
    for clause, (report, kw) in triggers.items():
        needle = CLAUSES[clause][0]
        problems = _problems(report, store, **kw)
        assert any(needle in p for p in problems), f"{clause}: '{needle}' not in {problems}"
    # Every refusal site in the check has a cell: a new clause must add one.
    source = inspect.getsource(reviewer_tools.reviewer_turn_problems)
    assert source.count("problems.append(") == 5, "a turn_end clause was added: give it a live attack cell"
