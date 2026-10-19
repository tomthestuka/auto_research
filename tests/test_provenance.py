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
    return store.mint(source=source, kind="result", produced_by="research_toolkit.xspec_utils.fit", data=text, **kw)


# -- store ------------------------------------------------------------------------------------

def test_handles_count_per_source_and_kind(store, tmp_path):
    a = _result(store, "one")
    b = _result(store, "two")
    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG")
    f = store.mint(source="analyst_worker_0", kind="figure", produced_by="research_toolkit.t.plot", path=png,
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
    r = store.mint(source="w", kind="data", produced_by="research_toolkit.t.tool", path=data)
    assert store.verify(r.handle) is None
    data.write_text("a,b\n1,3\n")
    assert "no longer match" in store.verify(r.handle)
    data.unlink()
    assert "not a file" in store.verify(r.handle)


def test_input_hash_links_parent_and_flags_propagate(store, tmp_path):
    produced = tmp_path / "prod.csv"
    produced.write_text("x\n")
    parent = store.mint(source="w", kind="data", produced_by="research_toolkit.t.reduce", path=produced, failed=True)
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
        Receipt(handle="w:f1", kind="result", path=tmp_path, sha256="0" * 64, produced_by="research_toolkit.t.t",
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
    stale = store.mint(source="w", kind="data", produced_by="research_toolkit.t.t", path=data)
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
        ReportWorkspace, edit_report_tool_def, execute_edit_report, execute_write_report,
    )
    from auto_research.schemas.base import MessageModel

    class Plain(MessageModel):
        abstract: str = Field(description="What the review concluded.")
        rationale: str | None = Field(default=None, description="Why, in the reviewer's words.")

    spec = edit_report_tool_def(("abstract", "rationale"), (), Plain, "Write your review.")
    # A7: Edit over the draft document; the role's own text follows the layout's edit text.
    assert set(spec["input_schema"]["properties"]) == {"old_string", "new_string", "replace_all"}
    assert "Write your review." in spec["description"] and "# abstract —" in spec["description"]
    assert "# figures" not in spec["description"]

    r = _result(store, "PD=3.453")
    ws = ReportWorkspace(report_path=tmp_path / "rev.jsonl", report_model=Plain, catalog={},
                         prose_fields=("abstract", "rationale"), receipts=store,
                         provenance_check=lambda rep: check_bindings(rep.abstract, store).problems)
    out = asyncio.run(execute_write_report(ws, {"content": f"# abstract\nPD 3.453 [{r.handle}]\n"}))
    assert not out.is_error and "Provenance" not in out.text
    out = asyncio.run(execute_edit_report(ws, {"old_string": f"PD 3.453 [{r.handle}]", "new_string": "PD 3.45"}))
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
    r = store.mint(source="lib", kind="result", produced_by="research_toolkit.t.extract", data=text, title="T")
    for bad in ('-20.7 ± 1.4 in the hard state, respectively', 'PD = 4.1 ± 0.20%', 'soft state, and -21'):
        res = check_bindings(f'{{{{{r.handle} "{bad}"}}}}', store)
        hint = _re.search(r'closest passage in \S+ reads: "(.*)"\.', res.problems[0])
        if hint:
            again = check_bindings(f'{{{{{r.handle} "{hint.group(1)}"}}}}', store)
            assert again.problems == [], (bad, hint.group(1), again.problems)


def test_a_line_broken_word_matches_in_raw_and_collapsed_form():
    # Page/PDF text normalisation, kept at the function level: since 2026-09-25 no web or paper
    # text grounds a number (``is_evidence``), so the check itself never reaches this path.
    from auto_research.provenance.bindings import bounded_find, normalise

    hay = normalise("in the hard state, respec-\ntively, with PD", "web")
    for q in ("hard state, respec- tively, with", "hard state, respectively, with"):
        assert bounded_find(hay, normalise(q, "web")), q


def test_a_signed_value_in_a_table_row_is_not_read_as_a_range(store):
    from auto_research.provenance.bindings import bounded_find, normalise

    r = store.mint(source="lib", kind="result", produced_by="research_toolkit.t.extract",
                   data="Cyg X-1 4.1 ± 0.2 −22 ± 2 [31]\nband 2 - 8 keV")
    assert check_bindings(f'{{{{{r.handle} "−22 ± 2"}}}}', store).problems == []
    assert check_bindings(f"PA = −22 ± 2 [{r.handle}]", store).problems == []
    hay = normalise("band 2 - 8 keV", "web")  # page text spaces a range; normalised both ways
    for q in ("2 - 8 keV", "2-8 keV"):
        assert bounded_find(hay, normalise(q, "web")), q


def test_enumerators_identifiers_and_status_codes_are_not_numbers(store):
    text = ("Found via search: (1) the first paper, (2) arXiv:2101.00001v2 and astro-ph/0601001, "
            "doi 10.3847/1538-4357/ac1234, bibcode 2022Sci...378..650K; arxiv returned "
            '"401 Unauthorized" and nasa_ads HTTP 429.')
    assert check_bindings(text, store).problems == []
    # a bare 4.5-digit value is still a number
    assert check_bindings("the flux is 3456.12345 counts", store).problems


def test_quote_hint_names_the_receipt_that_holds_it(store):
    from auto_research.provenance import ScopedLookup

    a = store.mint(source="lib", kind="result", produced_by="research_toolkit.t.r", data="lag of 12.5 ± 3.1 days")
    b = store.mint(source="lib", kind="result", produced_by="research_toolkit.t.r", data="virial factor 4.3 ± 1.1 here")
    view = ScopedLookup(store, own_source="lib")
    res = check_bindings(f'{{{{{a.handle} "4.3 ± 1.1"}}}}', view)
    assert f"It does appear in {b.handle}" in res.problems[0]


def test_quote_hint_never_offers_a_text_that_lost_the_sign(store):
    r = store.mint(source="rev", kind="result", produced_by="research_toolkit.t.r",
                   data="Cyg X-3 8.3 ± 0.5 22 ± 2 [44]")
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


def test_the_shell_grounds_nothing_even_arithmetic(store):
    """Human ruling 2026-09-25: bash_exec is not evidence at all — a derived number comes from a
    toolkit tool the analysts request. Echo, write-then-read and ``python -c`` are all refused."""
    from auto_research.provenance.capture import receipt_call

    shell = "mcp.builtin.bash_exec"
    for command, out, prose in (
        ("echo 'PD = 3.7'", "PD = 3.7", "PD is 3.7"),
        ("cat /tmp/x", "4.9", "value 4.9"),
        ("python -c 'print(4.0-3.4)'", "0.6", "the difference is 0.6"),
    ):
        r = receipt_call(store, source="analyst_worker_0", produced_by=shell,
                         args={"command": command}, text=out, failed=False).result
        problems = check_bindings(f"{prose} [{r.handle}]", store).problems
        assert any("shell output" in p for p in problems), (command, problems)
    # a toolkit tool echoing its own parameter (a band edge) grounds as ever
    fit = receipt_call(store, source="analyst_worker_0", produced_by="research_toolkit.xspec_utils.fit",
                       args={"emin": 2, "emax": 8}, text="fit 2-8 keV: PD=4.0", failed=False).result
    assert check_bindings(f"PD 4.0 in 2-8 keV [{fit.handle}]", store).problems == []


@pytest.mark.parametrize("producer,kind,what", [
    ("read_file", "result", "a file you read"), ("list_dir", "result", "a directory listing"),
    ("text_search", "result", "a search result"), ("web_search", "result", "a search result"),
    ("web_fetch", "web", "a web page"), ("report_renderer", "data", "an earlier published report"),
])
def test_what_a_role_read_is_recorded_but_never_evidence(store, tmp_path, producer, kind, what):
    """Human ruling 2026-09-25: only what the analysis produced backs a number."""
    from auto_research.provenance.store import is_evidence

    if kind == "data":
        f = tmp_path / "x.jsonl"
        f.write_text("PD = 4.4")
        r = store.mint(source="rev", kind=kind, produced_by=producer, path=f)
    else:
        r = store.mint(source="rev", kind=kind, produced_by=producer, data="    31\tPD = 4.4", title="T")
    assert not is_evidence(r)
    cited = check_bindings(f"PD = 4.4 [{r.handle}]", store).problems
    assert any(what in p and "not evidence" in p for p in cited), cited
    # In scope it grounds nothing and raises nothing — including a line-number prefix (R7).
    assert check_bindings("inclination 31 deg", store, scope=[r.handle]).problems


def test_what_counts_as_evidence(store):
    from auto_research.provenance.store import is_evidence

    tool = store.mint(source="w", kind="result", produced_by="research_toolkit.xspec_utils.fit", data="x")
    commit = store.mint(source="analyst_lead", kind="commit", produced_by="approve_edit", data="{}")
    request = store.mint_input("Fit ObsID 1.", kind="human", produced_by="research_request")
    brief = store.mint_input("Fit 2-8 keV.")
    human_file = store.mint(source="input", kind="result", produced_by="human_reply", data="3.3")
    assert is_evidence(tool) and is_evidence(commit) and is_evidence(request)
    assert not is_evidence(brief) and not is_evidence(human_file)


def test_a_file_resolving_outside_the_allow_list_is_never_receipted(store, tmp_path, monkeypatch):
    """F22: a tool's output -- or a symlink planted in a writable folder -- can name any host file;
    receipting it would put its bytes behind a handle any role can view."""
    from auto_research.provenance import capture
    from auto_research.sandbox import fs_guard

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "outside" / "credentials.json"
    secret.parent.mkdir()
    secret.write_text("{}")
    planted = allowed / "result.csv"
    planted.symlink_to(secret)
    honest = allowed / "honest.csv"
    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [allowed.resolve()])

    state = capture.CallInputs(started_ns=0)
    honest.write_text("a,b\n")
    out = capture.receipt_call(
        store, source="analyst_worker_0", produced_by="research_toolkit.x_utils.t",
        args={}, text=f"wrote {planted} and {honest}", failed=False, state=state,
    )
    receipted = {str(r.path) for r in out.products}
    assert str(secret.resolve()) not in receipted
    assert str(honest.resolve()) in receipted
    assert any("outside the allow-list" in note for note in out.notes)


def test_a_figure_that_is_a_symlink_is_dropped_not_receipted(store, tmp_path, monkeypatch):
    from auto_research.provenance import capture
    from auto_research.sandbox import fs_guard
    from auto_research.schemas.figures import FigureRef

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    real = tmp_path / "real.png"
    real.write_bytes(b"\x89PNG")
    link = tmp_path / "plot.png"
    link.symlink_to(real)
    fig = FigureRef.model_validate({"path": str(link), "caption": "c", "mime_type": "image/png",
                                    "produced_by": "t", "created_at": "2026-10-06T00:00:00Z",
                                    "sha256": "0" * 64})
    out = capture.receipt_call(store, source="w", produced_by="research_toolkit.x_utils.t",
                               args={}, text="ok", failed=False, figures=[fig])
    assert out.figures == [] and any("allow-list" in n for n in out.notes)


def test_a_receipted_file_is_read_once_checked_and_a_change_is_sent_with_an_alert(tmp_path):
    """F26 (human ruling 2026-10-06): read the bytes once, hash those; changed → still used,
    with a model-visible note and an alert; a symlink → not read at all, alerted."""
    from auto_research.provenance import store as S

    alerts: list[tuple[str, dict]] = []
    token = S._DRIFT_SINK.set(lambda summary, data: alerts.append((summary, data)))
    try:
        fig = tmp_path / "plot.png"
        fig.write_bytes(b"original")
        receipted = S.sha256_bytes(b"original")
        same = S.read_receipted(fig, receipted)
        assert same.data == b"original" and same.note is None and not alerts

        fig.write_bytes(b"swapped after minting")
        changed = S.read_receipted(fig, receipted, label="w0:f1")
        assert changed.data == b"swapped after minting" and changed.changed
        assert "w0:f1" in changed.note and len(alerts) == 1

        secret = tmp_path / "secret"
        secret.write_bytes(b"token")
        fig.unlink()
        fig.symlink_to(secret)
        linked = S.read_receipted(fig, receipted)
        assert linked.data is None and "symlink" in linked.note and len(alerts) == 2
    finally:
        S._DRIFT_SINK.reset(token)


def test_a_figure_changed_after_its_receipt_reaches_the_model_with_a_note(tmp_path):
    from auto_research.models.providers import _media
    from auto_research.provenance.store import sha256_bytes
    from auto_research.schemas.figures import FigureRef

    png = tmp_path / "p.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nAAAA")
    ref = FigureRef.model_validate({"path": str(png), "caption": "c", "mime_type": "image/png",
                                    "produced_by": "t", "created_at": "2026-10-06T00:00:00Z",
                                    "sha256": sha256_bytes(b"something else")})
    data, note = _media._read_item(_media.MediaItem(ref, True, png, "image/png"))
    assert data == png.read_bytes() and note and "differs from its receipt" in note


def test_a_folder_on_the_path_swapped_for_a_symlink_is_not_followed(tmp_path):
    """O_NOFOLLOW alone checks only the last component; every folder on the path is checked."""
    from auto_research.provenance import store as S

    figs = tmp_path / "w" / "figs"
    figs.mkdir(parents=True)
    (figs / "a.png").write_bytes(b"plot")
    receipted = S.sha256_bytes(b"plot")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.png").write_bytes(b"SECRET")
    (figs / "a.png").unlink()
    figs.rmdir()
    figs.symlink_to(elsewhere)
    read = S.read_receipted(figs / "a.png", receipted)
    assert read.data is None and "symlink" in read.note


def test_the_drift_alert_reaches_the_run_monitor_once_even_from_a_thread(tmp_path):
    import asyncio
    import json as _json

    from auto_research.monitor import RunMonitor
    from auto_research.provenance import store as S

    ledger = tmp_path / "monitor.jsonl"
    fig = tmp_path / "f.png"
    fig.write_bytes(b"new")

    async def run() -> None:
        monitor = RunMonitor(run_id="r", jsonl_path=ledger)
        S.install_drift_alert(monitor)
        try:
            S.read_receipted(fig, S.sha256_bytes(b"old"))  # on the loop
            await asyncio.to_thread(S.read_receipted, fig, S.sha256_bytes(b"old"))  # off it
            fig.write_bytes(b"newer")
            await asyncio.to_thread(S.read_receipted, fig, S.sha256_bytes(b"old"))
            await asyncio.sleep(0.05)
        finally:
            S.install_drift_alert(None)

    asyncio.run(run())
    events = [_json.loads(l) for l in ledger.read_text().splitlines() if "receipt.drift" in l]
    assert len(events) == 2  # one per (file, bytes): the repeat of the same change is not re-alerted


def test_a_citation_whose_bytes_changed_is_refused(store, tmp_path):
    from auto_research.provenance import check_bindings

    data = tmp_path / "t.csv"
    data.write_text("PD,4.0\n")
    r = store.mint(source="analyst_worker_0", kind="data",
                   produced_by="research_toolkit.x_utils.t", path=data)
    data.write_text("PD,9.9\n")
    problems = check_bindings(f"PD is 9.9 [{r.handle}]", store).problems
    assert any("no longer match" in p for p in problems), problems


def test_a_citation_check_reads_the_receipt_once(store, tmp_path, monkeypatch):
    """The bytes checked against the receipt are the bytes the quote is matched in (F26)."""
    from auto_research.provenance import check_bindings

    data = tmp_path / "t.csv"
    data.write_text("PD,4.0\n")
    r = store.mint(source="analyst_worker_0", kind="data",
                   produced_by="research_toolkit.x_utils.t", path=data)
    calls = []
    real_read = store.read
    monkeypatch.setattr(store, "read", lambda h: calls.append(h) or real_read(h))
    monkeypatch.setattr(store, "verify", lambda h: pytest.fail("a second, separate check"))
    monkeypatch.setattr(store, "text", lambda h: pytest.fail("a second, separate read"))
    assert check_bindings(f"PD is 4.0 [{r.handle}]", store).problems == []
    assert calls == [r.handle]


def test_a_parsed_document_cache_under_a_symlinked_root_keeps_its_figures(tmp_path, monkeypatch):
    """Crops cached under a configured spelling through a link (/data -> /nfsdata1) are read back
    component by component; the cache root is resolved so they are not refused."""
    from auto_research.models.providers import _doc_parse, _media
    from auto_research.provenance.store import sha256_bytes
    from auto_research.schemas import DocumentRef
    from auto_research.schemas.figures import FigureRef

    real = tmp_path / "real_cache"
    real.mkdir()
    link = tmp_path / "linked_cache"
    link.symlink_to(real)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    doc = DocumentRef(path=pdf, title="p", mime_type="application/pdf",
                      created_at="2026-10-06T00:00:00Z", sha256=sha256_bytes(pdf.read_bytes()))

    def fake_docling(document, base, *, data):
        base.mkdir(parents=True, exist_ok=True)
        crop = base / "fig_001.png"
        crop.write_bytes(b"\x89PNG crop")
        ref = FigureRef(path=crop, caption="c", mime_type="image/png", produced_by="docling",
                        created_at="2026-10-06T00:00:00Z", sha256=sha256_bytes(crop.read_bytes()))
        return _doc_parse.ParsedDocument(text="t", figures=(ref,), truncated=False)

    monkeypatch.setattr(_doc_parse, "_run_docling", fake_docling)
    parsed = _doc_parse.parse_document(doc, root=link)
    (crop,) = parsed.figures
    data, note = _media._read_item(_media.MediaItem(crop, True, crop.path, "image/png"))
    assert data == b"\x89PNG crop" and note is None
