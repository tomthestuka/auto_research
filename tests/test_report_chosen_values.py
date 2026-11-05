"""A value a unit chose for a tool call, on the page a human reads (no LLM).

The chosen value lives in the call's hashed arguments with its reason; a report writes it as
``{{handle#/$args/<name>}}``. Drives the real worker loop (`AgentRole._react`), the real
`SandboxToolProvider`, `ReceiptStore`, `RunMonitor`, the reviewer's `turn_end` check with its HTML
render, and the page's copyable reproduce code run as a human would; only the model and the
sandbox's tool process are fakes.
"""

import asyncio
import json
import re
import sys
from html import escape
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.agents.roles import AnalystWorkerAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.monitor.events import RunMonitor
from auto_research.provenance import ReceiptStore
from auto_research.schemas.review import ReviewerSection
from tests.test_report_html import _Scan, _render, _report as _html_report
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

MAKE, FIT = "research_toolkit.tk.make", "research_toolkit.tk.fit"
ME = "analyst_worker_0"
REASON = "the request asks for a <b>99%</b> limit"
SCHEMA = {"properties": {"d": {"kind": "data"}, "cl": {"kind": "parameter"}, "f": {"kind": "path"},
                         "o": {"kind": "output", "shape": "folder"}, "save_path": {"kind": "path"}},
          "required": ["cl"]}


def _report(**overrides):
    """A report to the human whose every number is one of this run's references."""
    fields = dict(rationale=f"The scan converged {{{{{ME}:r2}}}}.", key_findings=[f"The limit is {{{{{ME}:r2#/limit}}}}."],
                  sections=[ReviewerSection(heading="Scan", body=f"The limit is {{{{{ME}:r3#/limit}}}}.")])
    return _html_report(**{**fields, **overrides})


class _Model:
    def __init__(self, calls):
        self._calls = list(calls)

    async def call(self, messages, **kwargs):
        if self._calls:
            return ModelResponse(tool_calls=[self._calls.pop(0)])
        return ModelResponse(content="done")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """make → r1 (+ its file d1); fit with a chosen cl and the file → r2; fit taking r2's choice
    by reference and a typed path → r3."""
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    monkeypatch.setattr(react, "_observation_path_args", lambda: {})
    def tool(module, fn, args):
        if "d" not in args:  # make: writes into its own folder and declares the file
            made = Path(args["o"]) / "spec.pha"
            made.write_text("PHA\n")
            payload = {"bins": [{"q": 0.021510398015379906}], "file": str(made)}
            return {"exit_code": 0, "stdout": json.dumps(payload), **payload,
                    "products": [{"path": str(made), "caption": "spectrum"}]}
        return {"exit_code": 0, "stdout": "limit = 0.37959418926306543", "limit": 0.37959418926306543}

    store = ReceiptStore(tmp_path / "receipts")
    ledger = tmp_path / "monitor.jsonl"
    sandbox = _FakeSandbox(tool_fn=tool)
    tools = SandboxToolProvider(_FakeRegistry(known={MAKE, FIT}, mcp=set(), schema=SCHEMA), sandbox,
                                cwd=tmp_path / "w0", receipts=store, receipt_source=ME)
    worker = AnalystWorkerAgent(model=_Model([
        ToolCall(id="c1", name=MAKE, arguments={"cl": {"value": 1.0, "reason": "the make step's scale"}}),
        ToolCall(id="c2", name=FIT, arguments={"d": {"$ref": f"{ME}:r1#/bins"},
                                               "cl": {"value": 0.99, "reason": REASON},
                                               "f": {"$ref": f"{ME}:d1"}}),
        ToolCall(id="c3", name=FIT, arguments={"d": {"$ref": f"{ME}:r1#/bins/0"},
                                               "cl": {"$ref": f"{ME}:r2#/$args/cl"},
                                               "f": "/typed/path.pha"}),
    ]), prompt="p", monitor=RunMonitor(chatter=10, jsonl_path=ledger))
    worker.agent_key = ME
    asyncio.run(worker._react(seed_messages=[], tools=tools, action="analyze", cap=10))
    received = [args for _, _, args in sandbox.tool_calls]
    return store, ledger, received


def test_the_run_is_what_the_page_describes(run):
    store, _, received = run
    assert [r.handle for r in store.receipts(source=ME, kind="result")] == [f"{ME}:r1", f"{ME}:r2", f"{ME}:r3"]
    assert received[1]["cl"] == 0.99 and received[2]["cl"] == 0.99
    assert received[1]["f"].endswith("spec.pha") and received[2]["f"] == "/typed/path.pha"


def test_a_chosen_value_on_the_page_says_who_chose_it_and_why(tmp_path, run):
    store, ledger, _ = run
    problems, render = _render(tmp_path, store, ledger, _report(
        abstract=f"The scan used {{{{{ME}:r2#/$args/cl}}}} and then {{{{{ME}:r3#/$args/cl}}}} again."),
        admitted=(f"{ME}:r2", f"{ME}:r3"))
    assert problems == [] and render.html_errors == []
    page = render.html.html_path.read_text()
    assert ">0.990</button>" in page
    # The reason, escaped (model-written text), named with its chooser; the chained one too.
    assert f"chosen by {ME}: {escape(REASON)}" in page and REASON not in page
    assert f"passed from {ME}:r2#/$args/cl, chosen by {ME}: {escape(REASON)}" in page
    # The call's own arguments row shows the chosen value with its reason, not the wrapper.
    assert f'<code>0.99</code> <span class="muted">chosen: {escape(REASON)}</span>' in page


def test_the_reproduce_code_feeds_each_call_what_its_tool_received(tmp_path, run, monkeypatch):
    store, ledger, received = run
    _, render = _render(tmp_path, store, ledger, _report(
        abstract=f"Used {{{{{ME}:r2#/$args/cl}}}} and {{{{{ME}:r3#/$args/cl}}}}."), admitted=(f"{ME}:r2", f"{ME}:r3"))
    scan = _Scan()
    scan.feed(render.html.html_path.read_text())
    by_receipt = {c.splitlines()[0]: c for c in scan.codes}  # a call's block appears in each chain holding it
    fits = [by_receipt[f"# Reproduce {ME}:{r}: {FIT}"] for r in ("r2", "r3")]
    (tmp_path / "mod.py").write_text("def fn(**kwargs):\n    return kwargs\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    import tempfile

    rerun = tmp_path / "rerun"
    rerun.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(rerun))  # the reproduce code's fresh folders land here
    for code, want in zip(fits, (received[1], received[2])):
        monkeypatch.delitem(sys.modules, "mod", raising=False)
        namespace: dict = {}
        exec(code, namespace)  # noqa: S102 — the generated notebook lines, run as a human would
        out = dict(namespace["out"])
        # The output folder is a fresh one, so the rerun leaves the run's products as they are (B2).
        rerun_folder = Path(out.pop("o"))
        assert rerun_folder.is_dir() and rerun_folder.parent == rerun
        # A figure path the runtime placed goes to the same fresh folder, its file name kept.
        assert Path(out.pop("save_path")) == rerun_folder / Path(want["save_path"]).name == rerun_folder / "fn.png"
        assert out == {k: v for k, v in want.items() if k not in ("o", "save_path")}
        args_line = next(line for line in code.splitlines() if line.startswith("args = "))
        assert "reason" not in args_line and "$args" not in args_line  # a parameter arrives as its value


def test_a_choice_whose_record_changed_fails_the_notebook_loudly(tmp_path, run, monkeypatch):
    from auto_research.provenance.calls import calls_by_receipt, ledger_calls
    from auto_research.report.html import reproduce_code

    store, ledger, _ = run
    r2, r3 = store.get(f"{ME}:r2"), store.get(f"{ME}:r3")
    blob = store.blob_path(r2.args_sha256)
    blob.chmod(0o644)
    blob.write_text(blob.read_text().replace("0.99", "0.5"))  # r2's recorded arguments tampered
    code, _ = reproduce_code(r3, calls_by_receipt(ledger_calls(ledger), [r3])[r3.handle], store)
    (tmp_path / "mod.py").write_text("def fn(**kwargs):\n    return kwargs\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "mod", raising=False)
    with pytest.raises(RuntimeError, match=re.escape("recorded arguments could not be read")):
        exec(code, {})  # noqa: S102
