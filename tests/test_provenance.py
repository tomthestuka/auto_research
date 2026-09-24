"""Receipts and the scoped-grounding check (provenance P0)."""

import json

import pytest

from auto_research.provenance import (
    ReceiptStore,
    bounded_find,
    check_bindings,
    check_report_fields,
    normalise,
    render_bindings,
)
from auto_research.schemas.artifacts import Receipt


@pytest.fixture
def store(tmp_path):
    return ReceiptStore(tmp_path / "receipts")


def _result(store, text, source="analyst_worker_0", **kw):
    return store.mint(source=source, kind="result", produced_by="xspec_utils.fit", data=text, **kw)


# -- store ------------------------------------------------------------------------------------

def test_handles_count_per_source_and_kind(store, tmp_path):
    a = _result(store, "one")
    b = _result(store, "two")
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG")
    f = store.mint(source="analyst_worker_0", kind="figure", produced_by="plot", path=png,
                   caption="c", mime_type="image/png")
    other = _result(store, "three", source="lib")
    assert [a.handle, b.handle, f.handle, other.handle] == [
        "analyst_worker_0:r1", "analyst_worker_0:r2", "analyst_worker_0:f1", "lib:r1"]


def test_store_replays_its_ledger_so_handles_survive_a_resume(store):
    _result(store, "one")
    again = ReceiptStore(store.root)
    assert again.get("analyst_worker_0:r1") is not None
    assert _result(again, "two").handle == "analyst_worker_0:r2"


def test_sha_is_runtime_computed_and_verify_catches_tampering(store, tmp_path):
    data = tmp_path / "t.csv"
    data.write_text("a,b\n1,2\n")
    r = store.mint(source="w", kind="data", produced_by="tool", path=data)
    assert store.verify(r.handle) is None
    data.write_text("a,b\n1,3\n")
    assert "no longer match" in store.verify(r.handle)
    data.unlink()
    assert "not a file" in store.verify(r.handle)


def test_input_hash_links_parent_and_flags_propagate(store, tmp_path):
    produced = tmp_path / "prod.csv"
    produced.write_text("x\n")
    parent = store.mint(source="w", kind="data", produced_by="reduce", path=produced, failed=True)
    child = _result(store, "derived", inputs=[store.hash_input(produced)])
    assert child.parents == [parent.handle]
    assert child.flags == ["failed"]
    assert [r.handle for r in store.ancestors(child.handle)] == [parent.handle]


def test_synthetic_marked_input_flags_the_result(store, tmp_path):
    synth = tmp_path / "IXPE" / "synth260924-abc123" / "evt.fits"
    synth.parent.mkdir(parents=True)
    synth.write_bytes(b"x")
    r = _result(store, "PD = 1.0", inputs=[store.hash_input(synth)])
    assert r.flags == ["synthetic"]


def test_directory_inputs_hash_by_listing(store, tmp_path):
    d = tmp_path / "obs"
    d.mkdir()
    (d / "a").write_text("1")
    entry = store.hash_input(d)
    assert entry.basis == "listing"


def test_receipt_rejects_a_handle_naming_another_kind(tmp_path):
    with pytest.raises(ValueError):
        Receipt(handle="w:f1", kind="result", path=tmp_path, sha256="0" * 64, produced_by="t",
                created_at="2026-09-24T00:00:00Z")


# -- boundaries -------------------------------------------------------------------------------

@pytest.mark.parametrize("hay,needle,ok", [
    ("PD = -0.51 %", "0.51", False),      # sign flip
    ("PD = 10.51 %", "0.51", False),      # digit glued before
    ("PD = 0.5123", "0.51", False),       # digit glued after
    ("x = 1.2e-3", "1.2", False),         # exponent follows
    ("range 10-20 keV", "-20", False),    # range dash is not a minus
    ("PD = 4.0.", "4.0", True),           # sentence full stop
    ("3.453 +/-0.194", "0.194", True),    # "+/-" is an error bar
    ("PA = -20.67", "-20.67", True),
])
def test_bounded_find(hay, needle, ok):
    assert bounded_find(normalise(hay, "result"), normalise(needle, "result")) is ok


def test_nfc_not_nfkc_superscripts_stay_distinct():
    assert not bounded_find(normalise("flux 10²³", "result"), "1023")


def test_paper_normalisation_rejoins_spaced_number_tokens():
    hay = normalise("PD of 4 . 0 ± 0 . 2 % and PA - 20 . ◦ 7", "paper")
    assert bounded_find(hay, normalise("4.0 ± 0.2 %", "paper"))
    assert not bounded_find(normalise("counts 4 02", "paper"), "402")


# -- scoped grounding -------------------------------------------------------------------------

def test_a_cited_number_grounds_and_an_uncited_one_is_rejected(store):
    r = _result(store, "PD=3.453 +/- 0.194 %, PA=-20.67 +/- 1.61 deg")
    ok = check_bindings(f"PD is 3.453 ± 0.194 % [{r.handle}].", store)
    assert ok.problems == []
    bad = check_bindings(f"PD is 3.45 % [{r.handle}].", store)
    assert len(bad.problems) == 1 and '"3.45"' in bad.problems[0]


def test_unicode_minus_in_prose_grounds_against_ascii_output(store):
    r = _result(store, "PA=-20.67")
    assert check_bindings(f"PA = −20.67° [{r.handle}]", store).problems == []


def test_sign_flip_in_prose_is_rejected(store):
    r = _result(store, "PA=-20.67")
    assert check_bindings(f"PA = 20.67° [{r.handle}]", store).problems


def test_no_citation_means_nothing_grounds(store):
    _result(store, "PD=3.453")
    res = check_bindings("PD is 3.453 %.", store)
    assert "no receipt at all" in res.problems[0]


def test_pinned_quote_must_be_exact(store):
    r = _result(store, "PD = 4.02 +/- 0.21 %")
    assert check_bindings(f'{{{{{r.handle} "PD = 4.02 +/- 0.21 %"}}}}', store).problems == []
    assert check_bindings(f'{{{{{r.handle} "PD = 4.2 +/- 0.21 %"}}}}', store).problems


def test_flagged_receipt_grounds_nothing(store):
    r = _result(store, "PD=3.4", failed=True)
    assert check_bindings(f"PD 3.4 [{r.handle}]", store).problems
    assert "marked failed" in check_bindings(f'{{{{{r.handle} "PD=3.4"}}}}', store).problems[0]


def test_unknown_handle_and_self_alias(store):
    assert "not a receipt" in check_bindings("see [w:r9]", store).problems[0]
    r = _result(store, "N=12", source="analyst_worker_0")
    assert check_bindings("N=12 [self:r1]", store, own_source="analyst_worker_0").problems == []
    assert r.handle == "analyst_worker_0:r1"


def test_exemptions_figures_years_and_source_names(store):
    res = check_bindings("Figure 3 and Figs. 2–5 show GRS 1915+105 (Krawczynski et al. 2022).", store)
    assert res.problems == []


def test_exempt_spans_from_a_markup_caller(store):
    prose = "$\\chi^2$ is small"
    assert check_bindings(prose, store).problems
    i = prose.index("2")
    assert check_bindings(prose, store, exempt_spans=[(i, i + 1)]).problems == []


def test_findings_are_scoped_per_item_and_summary_by_the_whole_report(store):
    a = _result(store, "PD=3.453")
    b = _result(store, "PA=-20.67")
    res = check_report_fields({
        "summary": "PD 3.453 and PA -20.67.",
        "findings": [f"PD 3.453 [{a.handle}]", f"PA -20.67 [{a.handle}]", f"PA -20.67 [{b.handle}]"],
    }, store)
    assert len(res.problems) == 1 and res.problems[0].startswith("finding 2")


def test_render_replaces_quotes_and_cites(store):
    r = _result(store, "PD = 4.02 %")
    prose = f'We find {{{{{r.handle} "PD = 4.02 %"}}}} [{r.handle}].'
    out = render_bindings(prose, quote=lambda q: q.replace("%", r"\%"), cite=lambda h: "")
    assert out == r"We find PD = 4.02 \% ."


def test_a_broken_scope_receipt_grounds_nothing_and_raises_nothing(store, tmp_path):
    data = tmp_path / "gone.csv"
    data.write_text("PD 9.9\n")
    stale = store.mint(source="w", kind="data", produced_by="t", path=data)
    data.unlink()
    good = _result(store, "PD=3.453")
    res = check_report_fields({"summary": "PD 3.453."}, store, itemised=(),
                              scope=[stale.handle, good.handle])
    assert res.problems == []


def test_normalised_texts_are_computed_once_per_report(store):
    r = _result(store, "PD=3.453")
    calls = []

    class Counting:
        def get(self, h):
            return store.get(h)

        def verify(self, h):
            calls.append(h)
            return store.verify(h)

        def text(self, h):
            return store.text(h)

    check_report_fields({"summary": f"PD 3.453 [{r.handle}]",
                         "findings": [f"PD 3.453 [{r.handle}]"] * 5}, Counting())
    assert calls == [r.handle]


def test_source_names_with_a_hyphenated_number_are_not_numbers(store):
    assert check_bindings("Cyg X-1, Sco X-1 and DU-3 were observed.", store).problems == []


def test_a_range_grounds_against_the_same_range(store):
    r = _result(store, "fit in the 2-8 keV band; also 1.2e-3")
    assert check_bindings(f"Fit the 2-8 keV band [{r.handle}].", store).problems == []
    assert check_bindings(f"The value 3 [{r.handle}].", store).problems  # exponent is not "3"


def test_edit_report_serves_a_report_that_is_not_figured(tmp_path, store):
    import asyncio

    from pydantic import Field

    from auto_research.agents.react import (
        ReportWorkspace, edit_report_tool_def, execute_edit_report,
    )
    from auto_research.schemas.base import MessageModel

    class Plain(MessageModel):
        abstract: str = Field(description="What the review concluded.")
        rationale: str | None = Field(default=None, description="Why, in the reviewer's words.")

    spec = edit_report_tool_def(("abstract", "rationale"), (), Plain, "Write your review.")
    props = spec["input_schema"]["properties"]
    assert set(props) == {"abstract", "rationale"} and "cited_figures" not in props
    assert props["rationale"]["description"] == "Why, in the reviewer's words."
    assert spec["input_schema"]["required"] == ["abstract"] and spec["description"] == "Write your review."

    r = _result(store, "PD=3.453")
    ws = ReportWorkspace(report_path=tmp_path / "rev.jsonl", report_model=Plain, catalog={},
                         prose_fields=("abstract", "rationale"), receipts=store,
                         provenance_check=lambda rep: check_bindings(rep.abstract, store).problems)
    out = asyncio.run(execute_edit_report(ws, {"abstract": f"PD 3.453 [{r.handle}]"}))
    assert not out.is_error and "Provenance" not in out.text
    out = asyncio.run(execute_edit_report(ws, {"abstract": "PD 3.45"}))
    assert "Provenance" in out.text


def test_inputs_are_citable_by_every_unit_but_ground_only_when_cited(store):
    from auto_research.provenance import ScopedLookup

    ask = store.mint_input("Is PD stable across 2-8 keV in ObsID 02008401?", kind="human",
                           produced_by="research_request")
    assert store.mint_input("Is PD stable across 2-8 keV in ObsID 02008401?", kind="human",
                            produced_by="research_request").handle == ask.handle
    view = ScopedLookup(store, own_source="analyst_worker_0")
    assert ask.handle in view.handles()
    # Visible is not in scope: a number the task stated is repeatable only with the task cited.
    assert check_bindings("We analysed ObsID 02008401.", view).problems
    assert check_bindings(f"We analysed ObsID 02008401 [{ask.handle}].", view).problems == []


def test_math_alphanumerics_fold_but_superscripts_do_not():
    assert normalise("𝜎 = 𝟑.𝟐", "paper") == normalise("σ = 3.2", "paper")
    assert not bounded_find(normalise("flux 10²³", "paper"), "1023")


def test_a_failed_quote_names_the_closest_passage(store):
    r = _result(store, "The fit gives PD = 4.01 ± 0.20 % in the 2-8 keV band.")
    res = check_bindings(f'{{{{{r.handle} "PD = 4.1 ± 0.20 %"}}}}', store)
    assert "closest passage" in res.problems[0] and "PD = 4.01 ± 0.20 %" in res.problems[0]


def test_a_changed_original_file_fails_verify(store, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 original")
    from auto_research.provenance import sha256_file

    r = store.mint(source="lib", kind="paper", produced_by="read_paper", data="PD = 4.0",
                   title="T", source_sha256=sha256_file(pdf), source_path=pdf)
    assert store.verify(r.handle) is None
    pdf.write_bytes(b"%PDF-1.7 altered")
    assert "has changed" in store.verify(r.handle)


def test_ordinals_that_name_roles_and_steps_are_not_numbers(store):
    text = "Worker 0 and worker 1 agree; DU 2 in round 3 at step 4 of link 1."
    assert check_bindings(text, store).problems == []


def test_the_closest_passage_is_itself_quotable(store):
    import re as _re

    text = ("The PA is -20.7 ◦ ± 1.4 ◦ in the soft state, and -20 ◦.7 ± 1◦.4 in the hard state, respec-\n"
            "tively, with PD = 4.01 ± 0.20% overall.")
    r = store.mint(source="lib", kind="web", produced_by="read_paper", data=text, title="T")
    for bad in ('-20.7 ± 1.4 in the hard state, respectively', 'PD = 4.1 ± 0.20%', 'soft state, and -21'):
        res = check_bindings(f'{{{{{r.handle} "{bad}"}}}}', store)
        hint = _re.search(r'closest passage in \S+ reads: "(.*)"\.', res.problems[0])
        if hint:
            again = check_bindings(f'{{{{{r.handle} "{hint.group(1)}"}}}}', store)
            assert again.problems == [], (bad, hint.group(1), again.problems)


def test_a_line_broken_word_matches_in_raw_and_collapsed_form(store):
    r = store.mint(source="lib", kind="web", produced_by="read_paper",
                   data="in the hard state, respec-\ntively, with PD", title="T")
    for q in ("hard state, respec- tively, with", "hard state, respectively, with"):
        assert check_bindings(f'{{{{{r.handle} "{q}"}}}}', store).problems == []


def test_a_signed_value_in_a_table_row_is_not_read_as_a_range(store):
    r = store.mint(source="lib", kind="web", produced_by="read_paper",
                   data="Cyg X-1 4.1 ± 0.2 −22 ± 2 [31]\nband 2 - 8 keV", title="T")
    assert check_bindings(f'{{{{{r.handle} "−22 ± 2"}}}}', store).problems == []
    assert check_bindings(f"PA = −22 ± 2 [{r.handle}]", store).problems == []
    for q in ("2 - 8 keV", "2-8 keV"):
        assert check_bindings(f'{{{{{r.handle} "{q}"}}}}', store).problems == [], q


def test_enumerators_identifiers_and_status_codes_are_not_numbers(store):
    text = ("Found via search: (1) the first paper, (2) arXiv:2101.00001v2 and astro-ph/0601001, "
            "doi 10.3847/1538-4357/ac1234, bibcode 2022Sci...378..650K; arxiv returned "
            '"401 Unauthorized" and nasa_ads HTTP 429.')
    assert check_bindings(text, store).problems == []
    # a bare 4.5-digit value is still a number
    assert check_bindings("the flux is 3456.12345 counts", store).problems


def test_quote_hint_names_the_receipt_that_holds_it(store):
    from auto_research.provenance import ScopedLookup

    a = store.mint(source="lib", kind="web", produced_by="r", data="lag of 12.5 ± 3.1 days", title="A")
    b = store.mint(source="lib", kind="web", produced_by="r", data="virial factor 4.3 ± 1.1 here", title="B")
    view = ScopedLookup(store, own_source="lib")
    res = check_bindings(f'{{{{{a.handle} "4.3 ± 1.1"}}}}', view)
    assert f"It does appear in {b.handle}" in res.problems[0]


def test_quote_hint_never_offers_a_text_that_lost_the_sign(store):
    r = store.mint(source="rev", kind="web", produced_by="r",
                   data="Cyg X-3 8.3 ± 0.5 22 ± 2 [44]", title="T")
    res = check_bindings(f'{{{{{r.handle} "−22 ± 2"}}}}', store)
    assert "without the sign" in res.problems[0] and "closest passage" not in res.problems[0]


def test_sexagesimal_coordinates_are_names(store):
    assert check_bindings("any source at right ascension 00h00m or 12h34m56.7s", store).problems == []


def test_an_input_grounds_only_the_text_that_cites_it(store):
    ask = store.mint_input("Analyse NuSTAR ObsID 30001011002.", kind="human", produced_by="research_request")
    res = check_report_fields({
        "title": "Triage of NuSTAR ObsID 30001011002",
        "follow_up": [f"Re-reduce ObsID 30001011002 [{ask.handle}]"],
    }, store, itemised=(), scope=[ask.handle])
    assert len(res.problems) == 1 and res.problems[0].startswith("title")


def _paper(store, tmp_path):
    from auto_research.provenance import sha256_file

    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.7\nPD = 4.0 plain ascii body")
    return store.mint(source="lib", kind="paper", produced_by="read_paper", path=pdf, title="T",
                      source_path=pdf, source_sha256=sha256_file(pdf))


def test_a_pdf_receipt_has_no_checkable_text(store, tmp_path):
    p = _paper(store, tmp_path)
    assert store.text(p.handle) is None


def test_a_paper_value_must_be_attributed_with_a_locator(store, tmp_path):
    from auto_research.provenance import paper_attributions

    p = _paper(store, tmp_path)
    # one paper citation never exempts the numbers beside it
    res = check_bindings(f"The published PD is 4.0 ± 0.2 % [{p.handle}].", store)
    assert res.problems and "attribute it" in res.problems[0]
    # attributed without a locator: refused, naming what to add
    res = check_bindings(f'PD {{{{{p.handle} "4.0 ± 0.2 %"}}}}.', store)
    assert "without saying where" in res.problems[0]
    # attributed with a locator: accepted, and listed for verification against the PDF
    ok = f'PD {{{{{p.handle} "4.0 ± 0.2 %" Table 2}}}} while we measured 3.9 here.'
    res = check_bindings(ok, store)
    assert len(res.problems) == 1 and '"3.9"' in res.problems[0]
    got = paper_attributions(ok, store)
    assert [(a.handle, a.value, a.locator) for a in got] == [(p.handle, "4.0 ± 0.2 %", "Table 2")]


def test_a_locator_on_a_text_quote_is_allowed_and_ignored(store):
    r = _result(store, "PD = 4.02 +/- 0.21 %")
    assert check_bindings(f'{{{{{r.handle} "PD = 4.02 +/- 0.21 %" line 1}}}}', store).problems == []


def test_the_run_store_lives_outside_the_readable_workspace(tmp_path):
    run = tmp_path / "runtime" / "runs" / "abc"
    run.mkdir(parents=True)
    store = ReceiptStore.for_run(run)
    assert run not in store.root.parents and "ledgers" in store.root.parts


def test_a_shell_call_cannot_ground_a_number_the_role_typed(store):
    from auto_research.provenance.capture import receipt_call

    shell = "mcp.builtin.bash_exec"
    echo = receipt_call(store, source="analyst_worker_0", produced_by=shell,
                        args={"command": "echo 'PD = 3.7'"}, text="PD = 3.7", failed=False).result
    assert check_bindings(f"PD is 3.7 [{echo.handle}]", store).problems
    assert check_bindings(f'{{{{{echo.handle} "PD = 3.7"}}}}', store).problems
    # write-then-read in two calls: the number was typed in the first, so the second cannot ground it
    receipt_call(store, source="analyst_worker_0", produced_by=shell,
                 args={"command": "echo 4.9 > /tmp/x"}, text="", failed=False)
    cat = receipt_call(store, source="analyst_worker_0", produced_by=shell,
                       args={"command": "cat /tmp/x"}, text="4.9", failed=False).result
    assert check_bindings(f"value 4.9 [{cat.handle}]", store).problems
    # arithmetic the shell computed does ground
    calc = receipt_call(store, source="analyst_worker_0", produced_by=shell,
                        args={"command": "python -c 'print(4.0-3.4)'"}, text="0.6", failed=False).result
    assert check_bindings(f"the difference is 0.6 [{calc.handle}]", store).problems == []
    # a toolkit tool echoing its own parameter (a band edge) is not affected
    fit = receipt_call(store, source="analyst_worker_0", produced_by="xspec_utils.fit",
                       args={"emin": 2, "emax": 8}, text="fit 2-8 keV: PD=4.0", failed=False).result
    assert check_bindings(f"PD 4.0 in 2-8 keV [{fit.handle}]", store).problems == []
