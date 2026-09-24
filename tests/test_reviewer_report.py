"""The reviewer's one report: its schema, the renderer that turns it into LaTeX, and its turn_end check."""

from __future__ import annotations

import shutil
import struct
import zlib
from pathlib import Path

import pytest

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
    s.mint(source="analyst_lead", kind="result", produced_by="xspec_fit",
           data="PD = 4.02 ± 0.21 %\nPA = −20.7 ± 1.4 deg\nchi2/dof = 1.8")
    s.mint(source="analyst_lead", kind="figure", produced_by="plotter",
           path=_png(tmp_path / "a" / "plot.png"), caption="PD vs energy", mime_type="image/png")
    s.mint(source="analyst_lead", kind="figure", produced_by="plotter",
           path=_png(tmp_path / "b" / "plot.png", b"\x00\x00\xff"), caption="PA vs energy",
           mime_type="image/png")
    csv_path = tmp_path / "cmp.csv"
    csv_path.write_text("# produced by compare_to_benchmark\nquantity,this_work,paper\nPD,4.02,4.0\nPA,-20.7,-20.7\n")
    s.mint(source="analyst_lead", kind="data", produced_by="compare_to_benchmark", path=csv_path)
    s.mint(source="lib", kind="paper", produced_by="read_paper",
           data="We measure PD = 4.0 ± 0.2 % in the 2-8 keV band.",
           title="Polarized X-rays from Cyg X-1", ids={"arxiv": "2206.09972", "bibcode": "2020A&A...1K"})
    return s


def _final(**overrides) -> ReviewerReport:
    fields = dict(
        accepted=True,
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
    assert report.rework is False and report.prose_fields() == {}


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


def test_a_figure_whose_bytes_changed_is_refused_before_staging(store, tmp_path):
    Path(store.get("analyst_lead:f1").path).write_bytes(b"not the plot")
    result = render_report(_final(), store, tmp_path / "report", run_id="r1")
    assert result.tex_path is None
    assert any("analyst_lead:f1" in p and "no longer match" in p for p in result.problems)


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


def _problems(report, store, *, stage="analysis", left=1, render=None):
    return reviewer_turn_problems(
        report, lookup=store, scope=_scope(store), stage=stage, iterations_left=left, render=render,
    )


def test_no_report_is_refused(store):
    assert "No report has been written" in _problems(None, store)[0]


def test_a_complete_final_report_passes(store):
    assert _problems(_final(), store) == []


def test_a_final_report_needs_its_parts(store):
    problems = _problems(ReviewerReport(rationale="Holds [lib:p1]."), store)
    for name in ("title", "abstract", "sections", "key_findings"):
        assert any(f"and {name} is empty" in p for p in problems)


def test_rework_needs_a_follow_up_and_an_iteration_left(store):
    report = ReviewerReport(rationale="Short [lib:p1].", needs_analysis_rework=True)
    problems = _problems(report, store, left=0)
    assert any("follow_up is empty" in p for p in problems)
    assert any("No analysis iterations remain" in p for p in problems)


def test_a_rework_report_does_not_need_the_final_parts(store):
    report = ReviewerReport(rationale="Short [lib:p1].", needs_analysis_rework=True, follow_up="Refit.")
    assert _problems(report, store) == []


def test_a_switch_set_at_the_wrong_stage_is_refused_rather_than_ignored(store):
    at_analysis = _final(needs_more_literature=True)
    assert any("applies at the literature review" in p for p in _problems(at_analysis, store))
    at_literature = ReviewerReport(rationale="r [lib:p1]", follow_up="Fit.", needs_analysis_rework=True)
    assert any("applies at the analysis review" in p for p in _problems(at_literature, store, stage="literature"))


def test_the_literature_review_needs_a_dispatch_and_respects_its_cap(store):
    report = ReviewerReport(rationale="Gap [lib:p1].", needs_more_literature=True)
    problems = _problems(report, store, stage="literature", left=0)
    assert any("follow_up is empty" in p for p in problems)
    assert any("No literature passes remain" in p for p in problems)


def test_a_number_in_follow_up_must_be_grounded_too(store):
    ungrounded = ReviewerReport(rationale="r [lib:p1]", follow_up="Fit the 3-7 keV band.")
    assert any("follow_up states numbers" in p for p in _problems(ungrounded, store, stage="literature"))
    grounded = ReviewerReport(rationale="r [lib:p1]", follow_up="Fit the 2-8 keV band.")
    assert _problems(grounded, store, stage="literature") == []


def test_any_readable_receipt_grounds_any_field(store):
    # The abstract cites nothing, yet its number is in a receipt the reviewer can read.
    assert _problems(_final(abstract="PD is 4.02 here."), store) == []


def test_a_broken_receipt_the_report_never_cites_is_not_the_models_problem(store):
    store.mint(source="analyst_lead", kind="result", produced_by="x", data="unrelated 99.9")
    Path(store.get("analyst_lead:r2").path).write_text("tampered")
    assert _problems(_final(), store) == []


def test_render_runs_only_for_a_final_report_with_nothing_else_to_fix(store, tmp_path):
    render = ReviewerRender(report_dir=tmp_path / "report", run_id="r1", formats=("jsonl",))
    assert _problems(_final(), store, render=render) == [] and render.attempts == 0
    render = ReviewerRender(report_dir=tmp_path / "report", run_id="r1")
    _problems(_final(title=""), store, render=render)
    assert render.attempts == 0  # a missing title is fixed before anything is rendered


@pytest.mark.skipif(not shutil.which("lualatex"), reason="lualatex not installed")
def test_a_final_turn_end_renders_and_compiles(store, tmp_path):
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
    uncited = ReviewerReport(rationale="r", follow_up="Check the 7.7 value.")
    assert any("7.7" in p for p in reviewer_turn_problems(
        uncited, lookup=lookup, scope=reviewer_scope(lookup), stage="literature", iterations_left=1))
    cited = ReviewerReport(rationale="r", follow_up=f"Check the 7.7 value [{request.handle}].")
    assert reviewer_turn_problems(
        cited, lookup=lookup, scope=reviewer_scope(lookup), stage="literature", iterations_left=1) == []
