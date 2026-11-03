"""report.html: a reported number opens the chain that produced it (no LLM).

A worker's real loop runs two toolkit calls (the first writes a file the second reads) through the
real provider, ledger and receipt store; the reviewer's real `turn_end` check then renders the
page. Only the model and the tool process are fakes.
"""

import asyncio
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.agents.reviewer_tools import ReviewerRender, reviewer_scope, reviewer_turn_problems
from auto_research.agents.roles import AnalystWorkerAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.monitor.events import RunMonitor
from auto_research.provenance import ReceiptStore, ScopedLookup
from auto_research.report.html import HTML_NAME, _pid, check_page
from auto_research.schemas.review import ReviewerReport, ReviewerSection, ReviewerTable
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

LOAD, FIT = "research_toolkit.tk.load", "research_toolkit.tk.fit"


class _Model:
    def __init__(self, calls):
        self._calls = list(calls)

    async def call(self, messages, **kwargs):
        return ModelResponse(tool_calls=[self._calls.pop(0)]) if self._calls else ModelResponse(content="done")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Two chained toolkit calls by a worker: the store, the ledger, the toolkit checkout."""
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    mission = tmp_path / "IXPE"
    (mission / "01002901").mkdir(parents=True)
    (mission / "01002901" / "evt2.fits").write_bytes(b"events")
    (mission / "catalog.json").write_text(json.dumps({"entries": {"01002901": {"max_level": 2}}}))
    monkeypatch.setattr(react, "_observation_path_args", lambda: {"ixpe_path": str(mission)})
    root = tmp_path / "toolkit"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (root / "tk.py").write_text("# the toolkit as committed\n")
    subprocess.run([*git, "add", "tk.py"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)

    work = tmp_path / "w0"
    work.mkdir()
    csv = work / "spectrum.csv"

    def tool(module, fn, args):
        if "out_path" in args:
            Path(args["out_path"]).write_text("E,counts\n2,10\n")
            return {"exit_code": 0, "stdout": f"wrote {args['out_path']}"}
        return {"exit_code": 0, "stdout": "PD = 4.02 ± 0.21 %\nchi2/dof = 1.8"}

    store = ReceiptStore(tmp_path / "receipts")
    ledger = tmp_path / "monitor.jsonl"
    registry = _FakeRegistry(known={LOAD, FIT}, mcp=set(),
                             schema={"properties": {"obsid": {}, "ixpe_path": {}, "out_path": {}, "csv_path": {}}})
    registry._root = root
    sandbox = _FakeSandbox(tool_fn=tool)
    tools = SandboxToolProvider(registry, sandbox, cwd=work, receipts=store,
                                receipt_source="analyst_worker_0")
    worker = AnalystWorkerAgent(model=_Model([
        ToolCall(id="c1", name=LOAD, arguments={"obsid": "01002901", "out_path": str(csv)}),
        ToolCall(id="c2", name=FIT, arguments={"csv_path": str(csv), "model": "<b>tbabs</b>"}),
    ]), prompt="p", monitor=RunMonitor(chatter=10, jsonl_path=ledger))
    worker.agent_key = "analyst_worker_0"
    asyncio.run(worker._react(seed_messages=[], tools=tools, action="analyze", cap=10))

    class Received:
        received = [args for _, _, args in sandbox.tool_calls]

    return store, ledger, root, Received


def run_received(run):
    return run[3].received


def _report(**overrides) -> ReviewerReport:
    fields = dict(
        accepted=True, assign_to="human", rationale="The fit converged [analyst_worker_0:r2].",
        title="Cyg X-1 polarization",
        abstract='We find {{analyst_worker_0:r2 "PD = 4.02 ± 0.21 %"}}.',
        sections=[ReviewerSection(heading="Fit", body="The fit gives chi2/dof of 1.8 [analyst_worker_0:r2].\n\n- PD is 4.02 [analyst_worker_0:r2]")],
        key_findings=["PD 4.02 ± 0.21 % [analyst_worker_0:r2]."],
    )
    fields.update(overrides)
    return ReviewerReport(**fields)


def _render(tmp_path, store, ledger, report, formats=("html",), admitted=("analyst_worker_0:r2",)):
    lookup = ScopedLookup(store, own_source="rev", admitted=list(admitted))
    render = ReviewerRender(report_dir=tmp_path / "report", run_id="run1", formats=formats, ledger=ledger)
    problems = reviewer_turn_problems(report, lookup=lookup, scope=reviewer_scope(lookup),
                                      budgets={"librarian": 1, "analysts": 1}, render=render)
    return problems, render


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons, self.ids, self.codes = [], set(), []
        self._button = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"])
        if tag == "button" and (a.get("class") or "").startswith("num"):
            self._button = [a["popovertarget"], ""]
        if a.get("data-code"):
            self.codes.append(a["data-code"])

    def handle_data(self, data):
        if self._button is not None:
            self._button[1] += data

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None:
            self.buttons.append(tuple(self._button))
            self._button = None


def test_every_number_opens_the_chain_that_produced_it(tmp_path, run):
    store, ledger, root, _ = run
    problems, render = _render(tmp_path, store, ledger, _report())
    assert problems == [] and render.html_errors == []
    page = render.html.html_path.read_text()
    scan = _Scan()
    scan.feed(page)
    numbers = {text for _, text in scan.buttons}
    assert {"PD = 4.02 ± 0.21 %", "4.02", "0.21", "1.8"} <= numbers
    r2 = _pid("analyst_worker_0:r2")
    assert {target for target, _ in scan.buttons} == {r2} <= scan.ids
    chain = page[page.index(f'id="{r2}"'):]
    # Two steps, oldest first: the load (its obs-db input, its file output), then the fit reading it.
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    assert chain.index(f"1. {root.resolve()}/tk.load") < chain.index(f"2. {root.resolve()}/tk.fit")
    assert "observation IXPE 01002901" in chain and "[analyst_worker_0:d1, step 1]" in chain
    assert f"toolkit commit <code>{head.strip()}</code>" in chain
    assert "← the number is here" in chain


def test_a_numbered_list_keeps_its_page(tmp_path, run):
    store, ledger, _, _ = run
    problems, render = _render(tmp_path, store, ledger, _report(sections=[ReviewerSection(
        heading="Steps", body="Steps:\n1. The fit converged [analyst_worker_0:r2].\n2. The model is adequate.")]))
    assert problems == [] and render.html_errors == [] and render.html.html_path.is_file()


def test_text_after_a_result_quote_is_refused_before_anything_renders(tmp_path, run):
    store, ledger, _, _ = run
    problems, _ = _render(tmp_path, store, ledger, _report(
        abstract='We find {{analyst_worker_0:r2 "4.02" so the PA is 99.9 deg}}.'))
    assert any("has text after its quote" in p for p in problems)


def test_a_value_reference_shows_its_display_value_and_the_full_value_one_click_away(tmp_path, run):
    store, ledger, _, _ = run
    result = store.mint(source="analyst_worker_0", kind="result", produced_by=FIT,
                        data=json.dumps({"pd_pct": 4.021534, "b_g": 24826061.57515391}))
    problems, render = _render(tmp_path, store, ledger, _report(
        abstract=f"We find PD = {{{{{result.handle}#/pd_pct}}}} % and B = {{{{{result.handle}#/b_g}}}} G."),
        admitted=("analyst_worker_0:r2", result.handle))
    assert problems == [] and render.html_errors == []
    page = render.html.html_path.read_text()
    assert ">4.02</button>" in page and "2.48×10<sup>7</sup></button>" in page
    assert "<code>4.021534</code>" in page and "<code>/pd_pct</code>" in page
    assert "pd_pct" not in page.split("<main", 1)[-1].split("popover", 1)[0]  # no key name in prose


def test_copy_reproduce_code_carries_the_exact_arguments(tmp_path, run):
    store, ledger, root, _ = run
    _, render = _render(tmp_path, store, ledger, _report())
    scan = _Scan()
    scan.feed(render.html.html_path.read_text())
    fit = next(c for c in scan.codes if "research_toolkit.tk.fit" in c)
    assert "from mod import fn" in fit and f"_root, _commit = {str(root.resolve())!r}" in fit
    assert "sys.path.insert(0, _root)" in fit
    args = json.loads(eval(re.search(r"^args = json\.loads\((.*)\)$", fit, re.M).group(1)))  # noqa: S307 — our literal
    fit_receipt = store.get("analyst_worker_0:r2")
    assert args["csv_path"] == str(tmp_path / "w0" / "spectrum.csv") and args["ixpe_path"] == str(tmp_path / "IXPE")
    assert args["model"] == "<b>tbabs</b>" and fit_receipt.args_sha256  # rebuilt, and proven by the hash
    assert args == run_received(run)[-1]  # exactly what the tool process received


def test_model_text_and_tool_arguments_stay_inert(tmp_path, run):
    store, ledger, _, _ = run
    # Numberless, so the grounding check accepts it and the page is rendered.
    attack = '<script>alert("x")</script><img src=x onerror=alert("y")> </div><div popover id="chain-x">'
    problems, render = _render(tmp_path, store, ledger, _report(
        title=f"Report {attack}", sections=[ReviewerSection(heading=attack, body=f"{attack} PD is 4.02 [analyst_worker_0:r2].")],
    ))
    assert problems == []
    page = render.html.html_path.read_text()
    assert "<script>alert" not in page and "<img src=x" not in page and "<b>tbabs</b>" not in page
    assert check_page(render.html.html_path) == []


def test_the_page_check_refuses_what_the_renderer_never_writes(tmp_path, run):
    store, ledger, _, _ = run
    _, render = _render(tmp_path, store, ledger, _report())
    path = render.html.html_path
    good = path.read_text()
    for tamper in ('<img src="https://evil/x.png">', '<p onclick="x()">', "<script>1</script>",
                   '<button popovertarget="chain-nowhere">', '<link rel="stylesheet" href="//cdn/x.css">'):
        path.write_text(good.replace("</main>", tamper + "</main>"))
        assert check_page(path), tamper
    path.write_text(good.replace("args = json.loads(", "args = json.loads(&#x27;{&#x27; + "))
    assert any("reproduce" in e for e in check_page(path))


def test_each_format_runs_only_when_listed(tmp_path, run):
    store, ledger, _, _ = run
    _, html_only = _render(tmp_path, store, ledger, _report(), formats=("html",))
    assert html_only.render is None and html_only.html.html_path.name == HTML_NAME
    _, none = _render(tmp_path, store, ledger, _report(), formats=("jsonl",))
    assert none.attempts == 0 and none.html is None
    (tmp_path / "report" / HTML_NAME).unlink()
    _, tex_only = _render(tmp_path, store, ledger, _report(), formats=("tex",))
    assert tex_only.html is None and not (tmp_path / "report" / HTML_NAME).exists()


def test_a_missing_ledger_shows_the_chain_without_arguments(tmp_path, run):
    store, _, _, _ = run
    problems, render = _render(tmp_path, store, tmp_path / "absent.jsonl", _report())
    assert problems == [] and render.html_errors == []
    page = render.html.html_path.read_text()
    assert "not rebuilt from the ledger" in page and "copy reproduce code" not in page


def _page_numbers(render):
    scan = _Scan()
    scan.feed(render.html.html_path.read_text())
    return [text for _, text in scan.buttons]


def test_whitespace_around_a_field_never_unlinks_its_numbers(tmp_path, run):
    store, ledger, _, _ = run
    problems, render = _render(tmp_path, store, ledger, _report(
        abstract="\nWe find PD of 4.02 [analyst_worker_0:r2].\n",
        sections=[ReviewerSection(heading=" Fit ", body="\n\nThe fit gives 1.8 [analyst_worker_0:r2].\n")],
    ))
    assert problems == [] and render.html_errors == []
    assert {"4.02", "1.8"} <= set(_page_numbers(render))


@pytest.mark.parametrize("table, expect", [
    (ReviewerTable(caption="T", receipt="analyst_worker_0:r9"), "not a receipt you can cite"),
    (ReviewerTable(caption="T", receipt="analyst_worker_0:r2", columns=["a"], rows=[["b"]]), "Fill one form"),
    (ReviewerTable(caption="T"), "neither a receipt nor columns"),
    (ReviewerTable(caption="T", columns=["a", "b"], rows=[["only one"]]), "different number of cells"),
])
def test_an_html_only_report_checks_its_tables_like_tex(tmp_path, run, table, expect):
    store, ledger, _, _ = run
    problems, render = _render(tmp_path, store, ledger, _report(tables=[table]))
    assert any(expect in p for p in problems) and not (tmp_path / "report" / HTML_NAME).exists()


def test_a_cell_holding_the_separator_keeps_its_rows_numbers_linked(tmp_path, run):
    store, ledger, _, _ = run
    table = ReviewerTable(caption="Fit", columns=["quantity | unit", "value"],
                          rows=[["PD | %", "4.02 [analyst_worker_0:r2]"]])
    problems, render = _render(tmp_path, store, ledger, _report(tables=[table]))
    assert problems == [] and render.html_errors == []
    assert "4.02" in _page_numbers(render)


def test_a_number_only_the_wider_scope_holds_lists_every_holder(tmp_path, run):
    """A title cites nothing: its number is grounded by the report's scope, and the page says so."""
    store, ledger, _, _ = run
    _, render = _render(tmp_path, store, ledger, _report(title="PD of 4.02 in Cyg X-1"))
    page = render.html.html_path.read_text()
    assert 'class="num scoped"' in page and "The text cites none of the receipts holding this value" in page


def test_a_renderer_fault_in_one_format_never_stops_the_other(tmp_path, run, monkeypatch):
    import auto_research.agents.reviewer_tools as reviewer_tools

    store, ledger, _, _ = run
    monkeypatch.setattr(reviewer_tools, "render_report", lambda *a, **k: 1 / 0)
    problems, render = _render(tmp_path, store, ledger, _report(), formats=("tex", "html"))
    assert problems == [] and "ZeroDivisionError" in render.renderer_errors[0]
    assert render.html.html_path.is_file()


def test_a_page_the_check_refuses_is_removed(tmp_path, run, monkeypatch):
    import auto_research.report.html as html

    store, ledger, _, _ = run
    monkeypatch.setattr(html, "check_page", lambda path: ["refused"])
    _, render = _render(tmp_path, store, ledger, _report())
    assert render.html.html_path is None and render.html_errors == ["refused"]
    assert not (tmp_path / "report" / HTML_NAME).exists()


def test_the_page_check_refuses_schemes_duplicates_and_loose_numbers(tmp_path, run):
    store, ledger, _, _ = run
    _, render = _render(tmp_path, store, ledger, _report())
    path = render.html.html_path
    good = path.read_text()
    for tamper in ('<a href="javascript:alert(1)">x</a>', '<iframe src="x.html"></iframe>',
                   '<p id="held-1"></p><p id="held-1"></p>', '<p style="background:url(x)">',
                   '<section data-prose="1"><p>PD is 9.99 here</p></section>'):
        path.write_text(good.replace("</main>", tamper + "</main>"))
        assert check_page(path), tamper


def test_a_quote_holding_braces_in_a_table_cell_is_accepted(tmp_path, run):
    from auto_research.report.render import check_table

    store, _, _, _ = run
    table = ReviewerTable(caption="T", columns=["fit", "value"],
                          rows=[["{{analyst_worker_0:r2 \"PD = 4.02\"}}", "x {'k': {'v': 1}}"]])
    assert check_table(table, 1, store)[2] == []
    split = ReviewerTable(caption="T", columns=["a", "b"], rows=[['{{analyst_worker_0:r2 "PD', '4.02"}}']])
    assert "split across cells" in check_table(split, 1, store)[2][0]


@pytest.mark.parametrize("formats", [("html",), ("tex",)])
def test_a_table_receipt_must_be_evidence(tmp_path, run, formats):
    """A CSV a read or the shell produced is not "a table a tool computed" (user ruling 2026-10-17)."""
    store, ledger, _, _ = run
    read = store.mint(source="analyst_worker_0", kind="result", produced_by="read_file", data="a,b\n1,2\n")
    problems, _ = _render(tmp_path, store, ledger, _report(tables=[ReviewerTable(caption="T", receipt=read.handle)]),
                          formats=formats, admitted=("analyst_worker_0:r2", read.handle))
    assert any("a file you read" in p and "toolkit result" in p for p in problems), problems


def test_figures_are_embedded_so_the_page_travels_alone(tmp_path, run):
    import struct
    import zlib

    store, ledger, _, _ = run

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = tmp_path / "w0" / "plot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b""))
    figure = store.mint(source="analyst_worker_0", kind="figure", produced_by="research_toolkit.tk.plot",
                        path=png, caption="PD vs E", mime_type="image/png")
    problems, render = _render(tmp_path, store, ledger, _report(figures=[figure.handle]),
                               admitted=("analyst_worker_0:r2", figure.handle))
    assert problems == [] and render.html_errors == []
    page = render.html.html_path.read_text()
    assert 'src="data:image/png;base64,' in page and 'src="figures/' not in page


def test_html_is_a_report_format_the_run_and_the_cli_accept():
    from auto_research.cli.main import build_parser
    from auto_research.graph.runtime import _normalize_report_formats

    assert _normalize_report_formats(["html", "jsonl"], ["tex"]) == ["html", "jsonl"]
    assert _normalize_report_formats(None, ["html"]) == ["html"]
    with pytest.raises(ValueError):
        _normalize_report_formats(None, ["htm"])  # a typo in the config is refused too
    args = build_parser().parse_args(["--report-format", "html", "question?"])
    assert args.report_formats == ["html"]


def test_reproduce_code_runs_the_toolkit_at_the_results_commit_when_the_checkout_moved_on(tmp_path, run):
    """Run faraday-refs-20261026: 218 of 249 blocks ran the live checkout, not their commit."""
    store, ledger, root, _ = run
    _, render = _render(tmp_path, store, ledger, _report())
    scan = _Scan()
    scan.feed(render.html.html_path.read_text())
    fit = next(c for c in scan.codes if "research_toolkit.tk.fit" in c)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    (root / "added_later.py").write_text("x = 1\n")
    subprocess.run([*git, "add", "added_later.py"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "later"], check=True)
    pin = fit[: fit.index("sys.path.insert(0, _root)")]
    scope: dict = {}
    exec(pin, scope)  # noqa: S102 — our own generated lines
    assert scope["_root"] != str(root.resolve()) and not (Path(scope["_root"]) / "added_later.py").exists()


def test_reproduce_code_imports_an_in_process_call_through_its_package_manifest(tmp_path, monkeypatch):
    from auto_research.provenance.calls import LedgerCall
    from auto_research.report.html import reproduce_code

    pkg = tmp_path / "zeta_utils"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "TOOL_MANIFEST = {'research_toolkit.zeta_utils.listing': {'module': 'zeta_utils.impl', 'fn': 'listing'}}\n")
    (pkg / "impl.py").write_text("def listing(obs_db_path):\n    return 'listed ' + obs_db_path\n")
    store = ReceiptStore(tmp_path / "receipts")
    receipt = store.mint(source="analyst_lead", kind="result", produced_by="research_toolkit.zeta_utils.listing",
                         data="listed /x", toolkit_root=str(tmp_path))
    call = LedgerCall(actor="analyst_lead", tool="data_catalog", tool_call_id="t", call_id="c",
                      model_args={}, context={}, ran_args={"obs_db_path": "/x"}, args_sha256="0")
    code, _ = reproduce_code(receipt, call, store)
    scope: dict = {}
    monkeypatch.syspath_prepend(str(tmp_path))
    exec(code, scope)  # noqa: S102 — our own generated lines
    assert scope["out"] == "listed /x"


def test_reproduce_blocks_run_their_own_commit_in_one_interpreter_and_ignore_uncommitted_edits(
    tmp_path, monkeypatch
):
    """Review 2026-10-27: a second block reused the first block's cached module, and a dirty
    checkout at the stamped commit ran the uncommitted code."""
    import sys

    from auto_research.provenance.calls import LedgerCall
    from auto_research.report.html import reproduce_code

    root = tmp_path / "toolkit"
    (root / "eta_utils").mkdir(parents=True)
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (root / "eta_utils" / "__init__.py").write_text("")
    impl = root / "eta_utils" / "impl.py"
    commits = []
    for tag in ("v1", "v2"):
        impl.write_text(f"def tag(x):\n    return '{tag}:' + x\n")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", tag], check=True)
        commits.append(subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip())
    impl.write_text("def tag(x):\n    return 'DIRTY:' + x\n")  # HEAD is v2, the tree is not

    store = ReceiptStore(tmp_path / "receipts")
    call = LedgerCall(actor="analyst_worker_0", tool="t", tool_call_id="t", call_id="c",
                      model_args={}, context={"module": "eta_utils.impl", "fn": "tag"},
                      ran_args={"x": "a"}, args_sha256="0")
    outs = []
    try:
        for commit in commits:
            receipt = store.mint(source="analyst_worker_0", kind="result", produced_by="research_toolkit.eta_utils.tag",
                                 data="x", toolkit_root=str(root), toolkit_commit=commit)
            scope: dict = {}
            exec(reproduce_code(receipt, call, store)[0], scope)  # noqa: S102 — our own generated lines
            outs.append(scope["out"])
    finally:
        for name in [m for m in sys.modules if m.startswith("eta_utils")]:
            del sys.modules[name]
        sys.path[:] = [p for p in sys.path if "toolkit" not in p]
    assert outs == ["v1:a", "v2:a"]


def test_a_text_value_is_shown_as_written_and_rounding_keeps_three_digits(tmp_path, run):
    from auto_research.provenance.bindings import display_value

    assert [display_value(v) for v in (9.9996, 999999.7, 0.00099996, 2.48e7, "source1")] == [
        "10.0", "1.00e6", "0.00100", "2.48e7", "source1"]
    store, ledger, _, _ = run
    result = store.mint(source="analyst_worker_0", kind="result", produced_by=FIT,
                        data=json.dumps({"region": "source1", "unit": "cm e²", "b_g": 2.48e7}))
    problems, render = _render(tmp_path, store, ledger, _report(
        abstract=f"Region {{{{{result.handle}#/region}}}} in {{{{{result.handle}#/unit}}}}, "
                 f"B {{{{{result.handle}#/b_g}}}} G."),
        admitted=("analyst_worker_0:r2", result.handle))
    assert problems == [] and render.html_errors == []
    page = render.html.html_path.read_text()
    assert ">source1</button>" in page and ">cm e²</button>" in page and "2.48×10<sup>7</sup>" in page


def test_a_nested_reference_argument_shows_the_value_it_carried(tmp_path):
    from auto_research.report.html import _arguments_html

    store = ReceiptStore(tmp_path / "receipts")
    peer = store.mint(source="analyst_worker_0", kind="result", produced_by=FIT,
                      data=json.dumps({"bins": [{"q": 0.25}, {"q": 0.5}]}))
    row = _arguments_html({"bins": [{"$ref": f"{peer.handle}#/bins/1"}]}, store)
    assert "[{&quot;q&quot;: 0.5}]" in row and f"← {peer.handle}#/bins/1" in row
