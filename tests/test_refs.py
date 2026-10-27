"""Receipt references in toolkit arguments: one tool's output passed on by handle (no LLM).

Drives the real worker loop (`AgentRole._react`), the real `SandboxToolProvider`, `ReceiptStore`,
`RunMonitor`, ledger rebuild and HTML reproduce code; only the model and the sandbox's tool process
are fakes.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.agents.roles import AnalystWorkerAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.monitor.events import RunMonitor
from auto_research.provenance import ReceiptStore
from auto_research.provenance.calls import calls_by_receipt, ledger_calls
from auto_research.provenance.refs import CONSOLE_SEPARATOR, handles_in_brief, returned_value
from auto_research.sandbox.runner import normalize_tool_raw
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

EXTRACT = "research_toolkit.tk.extract"
FIT = "research_toolkit.tk.fit"
BASH = "mcp.builtin.bash_exec"
BINS = [{"e_lo": 2.0, "q": 0.021510398015379906, "q_err": 0.0020782946477872952},
        {"e_lo": 3.0, "q": -0.023023469373583794, "q_err": 0.002075791505773522}]


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    monkeypatch.setattr(react, "_observation_path_args", lambda: {})


def _tool(module, fn, args):
    """The fake tool process, in the toolkit's real shape (contract rule 5): summary lines and the
    returned dict printed as its last line in ``stdout``, the dict's keys returned beside it, and
    console chatter folded under it. Fit echoes what it received."""
    if "bins" in args:
        payload = {"received": args}
        return {"exit_code": 0, "stdout": "fit: done\n" + json.dumps(payload), **payload}
    payload = {"bins": BINS, "n_bins": 2}
    return {"exit_code": 0, "stdout": "extract: 2 bins\n" + json.dumps(payload), **payload,
            "console_tail": "XSPEC chatter 1.0"}


class _ScriptedModel:
    def __init__(self, calls):
        self._calls = list(calls)

    async def call(self, messages, **kwargs):
        if self._calls:
            return ModelResponse(tool_calls=[self._calls.pop(0)])
        return ModelResponse(content="done")


def _provider(tmp_path, store, *, ref_handles=None, source="analyst_worker_0"):
    registry = _FakeRegistry(known={EXTRACT, FIT, BASH}, mcp={BASH},
                             schema={"properties": {"bins": {}, "cl": {}, "obsid": {}}})
    return SandboxToolProvider(registry, _FakeSandbox(tool_fn=_tool, mcp_fn=lambda k, a: {
        "exit_code": 0, "stdout": json.dumps({"bins": BINS}), "stderr": ""}),
        cwd=tmp_path / source, receipts=store, receipt_source=source, ref_handles=ref_handles)


def _run(tmp_path, calls, store):
    ledger = tmp_path / "monitor.jsonl"
    tools = _provider(tmp_path, store)
    worker = AnalystWorkerAgent(model=_ScriptedModel(calls), prompt="p",
                                monitor=RunMonitor(chatter=10, jsonl_path=ledger))
    worker.agent_key = "analyst_worker_0"
    asyncio.run(worker._react(seed_messages=[], tools=tools, action="analyze", cap=10))
    return tools, ledger


def _texts(ledger: Path) -> dict[str, str]:
    out = {}
    for line in ledger.read_text().splitlines():
        event = json.loads(line)
        if event["event"].endswith(".tool_result"):
            out[event["data"]["tool_call_id"]] = str(event["data"].get("text") or "")
    return out


def test_a_chained_call_receives_the_exact_values_and_records_its_parent(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    calls = [
        ToolCall(id="c1", name=EXTRACT, arguments={"obsid": "01"}),
        ToolCall(id="c2", name=FIT, arguments={"bins": {"$ref": "analyst_worker_0:r1#/bins"}, "cl": 0.99}),
        ToolCall(id="c3", name=FIT, arguments={"bins": [{"$ref": "analyst_worker_0:r1#/bins/1"}],
                                               "cl": {"$ref": "analyst_worker_0:r2#/received/cl"}}),
    ]
    tools, ledger = _run(tmp_path, calls, store)

    received = [args for _, _, args in tools.sandbox.tool_calls]
    assert received[1] == {"bins": BINS, "cl": 0.99}  # every digit, the producer's
    assert received[2] == {"bins": [BINS[1]], "cl": 0.99}
    r1, r2, r3 = store.receipts(source="analyst_worker_0", kind="result")
    assert r1.parents == [] and r2.parents == ["analyst_worker_0:r1"]
    assert r3.parents == ["analyst_worker_0:r1", "analyst_worker_0:r2"]
    assert "[receipt analyst_worker_0:r2] ← analyst_worker_0:r1" in _texts(ledger)["c2"]

    # The ledger keeps the references; the receipt hashes them; the call still rebuilds.
    rebuilt = ledger_calls(ledger)
    assert rebuilt[1].sent_args["bins"] == {"$ref": "analyst_worker_0:r1#/bins"}
    matched = calls_by_receipt(rebuilt, store.receipts())
    assert matched[r2.handle].tool_call_id == "c2" and matched[r3.handle].tool_call_id == "c3"


def test_the_console_tail_is_set_aside_exactly_where_the_runner_puts_it(tmp_path):
    raw = normalize_tool_raw({"exit_code": 0, "bins": BINS, "console_tail": "noise"})
    assert CONSOLE_SEPARATOR in raw["stdout"]
    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_0", kind="result", produced_by=EXTRACT, data=raw["stdout"])
    assert returned_value(store, "analyst_worker_0:r1") == {"bins": BINS}


def test_a_reference_to_what_a_role_read_is_refused_and_nothing_runs(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(BASH, {"command": "cat bins.json"}))  # r1: shell output
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}}))
    assert out.is_error and "shell output, a place to look" in out.text
    assert tools.sandbox.tool_calls == []
    assert len(store.receipts(source="analyst_worker_0")) == 1


def test_a_peer_result_resolves_only_once_it_was_handed_over(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    peer = _provider(tmp_path, store, source="analyst_worker_1")
    asyncio.run(peer.execute(EXTRACT, {"obsid": "01"}))  # analyst_worker_1:r1
    ref = {"bins": {"$ref": "analyst_worker_1:r1#/bins"}}

    alone = _provider(tmp_path, store)
    out = asyncio.run(alone.execute(FIT, ref))
    assert out.is_error and "not a receipt you can pass on" in out.text

    handed: set[str] = set()
    worker = _provider(tmp_path, store, ref_handles=lambda: handed)
    handed.add("analyst_worker_1:r1")  # read at call time: a message naming it mid-turn counts
    out = asyncio.run(worker.execute(FIT, ref))
    assert not out.is_error
    assert store.get("analyst_worker_0:r1").parents == ["analyst_worker_1:r1"]


def test_a_pointer_token_a_model_could_write_never_raises_past_the_provider(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    for token in ("²", "9" * 5000, "-1", "01", ""):
        out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": f"analyst_worker_0:r1#/bins/{token}"}}))
        assert out.is_error, token
    assert tools.sandbox.tool_calls[1:] == []


def test_a_bad_pointer_names_the_keys_that_are_there(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bns"}}))
    assert out.is_error and "no key 'bns'" in out.text and "bins, n_bins" in out.text
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins/7"}}))
    assert out.is_error and "not an index below 2" in out.text


def test_a_failed_or_changed_result_is_refused(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_0", kind="result", produced_by=EXTRACT,
               data=json.dumps({"bins": BINS}), failed=True)
    good = store.mint(source="analyst_worker_0", kind="result", produced_by=EXTRACT,
                      data=json.dumps({"bins": BINS, "v": 2}))
    tools = _provider(tmp_path, store)
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}}))
    assert out.is_error and "flagged failed" in out.text

    Path(good.path).chmod(0o644)
    Path(good.path).write_text(json.dumps({"bins": [], "v": 2}))
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r2#/bins"}}))
    assert out.is_error and "no longer matches its receipt" in out.text
    assert tools.sandbox.tool_calls == []


def test_a_synthetic_flag_travels_through_a_reference(tmp_path):
    from auto_research.schemas.artifacts import InputFile

    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_0", kind="result", produced_by=EXTRACT, data=json.dumps({"bins": BINS}),
               inputs=[InputFile(path=tmp_path / "synth_injected", sha256="0" * 64)])
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}}))
    assert "synthetic" in store.get("analyst_worker_0:r2").flags


def test_values_copied_by_hand_are_refused_with_the_reference_to_write(tmp_path):
    """User ruling 2026-10-26 (a note after the call was ignored live): the call does not run."""
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    out = asyncio.run(tools.execute(FIT, {"bins": BINS, "cl": 0.99}))
    assert out.is_error and "This call was not run" in out.text
    assert f'argument bins/0/q = {BINS[0]["q"]!r}: pass {{"$ref": "analyst_worker_0:r1#/bins/0/q"}}' in out.text
    assert len(tools.sandbox.tool_calls) == 1  # the copy never ran

    # Parameters a model chooses (short floats, integers) and the reference itself run.
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}, "cl": 0.99, "n": 20000}))
    assert not out.is_error and store.get("analyst_worker_0:r2").parents == ["analyst_worker_0:r1"]


def test_a_brief_names_only_toolkit_results(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))  # r1, a toolkit result
    asyncio.run(tools.execute(BASH, {"command": "ls"}))   # r2, shell output
    text = "Refit with the bins of [analyst_worker_0:r1] and analyst_worker_0:r2; see analyst_worker_0:r9."
    assert handles_in_brief(store, text) == ["analyst_worker_0:r1"]


def test_the_reproduce_code_feeds_the_tool_the_values_the_call_received(tmp_path, monkeypatch):
    from auto_research.report.html import reproduce_code

    store = ReceiptStore(tmp_path / "receipts")
    calls = [ToolCall(id="c1", name=EXTRACT, arguments={"obsid": "01"}),
             ToolCall(id="c2", name=FIT, arguments={"bins": {"$ref": "analyst_worker_0:r1#/bins/0"}})]
    tools, ledger = _run(tmp_path, calls, store)
    r2 = store.get("analyst_worker_0:r2")
    call = calls_by_receipt(ledger_calls(ledger), [r2])[r2.handle]
    code, _ = reproduce_code(r2, call, store)

    (tmp_path / "mod.py").write_text("def fn(**kwargs):\n    return kwargs\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "mod", raising=False)
    namespace: dict = {}
    exec(code, namespace)  # noqa: S102 — the generated notebook lines, run as a human would
    assert namespace["out"] == tools.sandbox.tool_calls[1][2] == {"bins": BINS[0]}


def test_a_worker_passes_on_a_result_its_brief_names_and_nothing_else(tmp_path):
    """The role's own wiring (`AnalystWorkerAgent.analyze`): a result the task names is passable."""
    from types import SimpleNamespace

    from auto_research.provenance.capture import receipt_call
    from auto_research.schemas import AnalysisTask

    store = ReceiptStore(tmp_path / "receipts")
    peer = receipt_call(store, source="analyst_worker_0", produced_by=EXTRACT, args={"obsid": "01"},
                        text="extract: 2 bins", failed=False, returned={"bins": BINS}).result

    class _Model:
        config = SimpleNamespace(model="fake")

        def __init__(self):
            self.turn = 0

        async def call(self, messages, **kwargs):
            self.turn += 1
            if self.turn == 1:
                return ModelResponse(tool_calls=[ToolCall(id="f", name=FIT, arguments={
                    "bins": {"$ref": f"{peer.handle}#/bins"}})])
            if self.turn == 2:
                return ModelResponse(tool_calls=[ToolCall(id="w", name="write_report", arguments={
                    "content": "# summary\nrefit the briefed bins\n# status\nfailed\n"})])
            return ModelResponse(tool_calls=[ToolCall(id="t", name="turn_end", arguments={})])

    def worker(name: str):
        agent = AnalystWorkerAgent(model=_Model(), available_toolkits=[],
                                   sandbox=_FakeSandbox(tool_fn=_tool), registry=_FakeRegistry(known={FIT}, mcp=set()))
        agent.agent_key, agent.report_dir, agent.receipts = name, tmp_path / name, store
        return agent

    briefed = worker("analyst_worker_1")
    asyncio.run(briefed.analyze(AnalysisTask(question="refit", instructions=f"Use the bins in [{peer.handle}].")))
    assert briefed.sandbox.tool_calls[0][2] == {"bins": BINS}
    assert store.get("analyst_worker_1:r1").parents == [peer.handle]

    unbriefed = worker("analyst_worker_2")
    asyncio.run(unbriefed.analyze(AnalysisTask(question="refit")))
    assert unbriefed.sandbox.tool_calls == []  # the reference was refused; nothing ran


def test_a_brief_naming_a_result_its_author_cannot_open_is_refused_at_turn_end(tmp_path):
    from auto_research.agents.reviewer_tools import reviewer_turn_problems
    from auto_research.schemas.review import ReviewerReport

    store = ReceiptStore(tmp_path / "receipts")
    for source in ("analyst_worker_0", "analyst_worker_1"):
        store.mint(source=source, kind="result", produced_by=EXTRACT, data=json.dumps({"bins": BINS}))
    report = ReviewerReport(rationale="r", assign_to="analysts",
                            follow_up="Rerun the scan with the bounds in [analyst_worker_1:r1].")
    problems = reviewer_turn_problems(report, lookup=store, scope=["analyst_worker_0:r1"],
                                      budgets={"analysts": 1})
    assert any("analyst_worker_1:r1, which you cannot open" in p for p in problems)
    report.follow_up = "Rerun the scan with the bounds in [analyst_worker_0:r1]."
    problems = reviewer_turn_problems(report, lookup=store, scope=["analyst_worker_0:r1"],
                                      budgets={"analysts": 1})
    assert not any("cannot open" in p for p in problems)


def test_a_reference_reads_the_returned_value_and_refuses_it_once_tampered(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    extract = store.get("analyst_worker_0:r1")
    assert extract.returned_sha256 and "extract: 2 bins" in store.text(extract.handle)
    blob = store.blob_path(extract.returned_sha256)
    assert json.loads(blob.read_text()) == {"bins": BINS, "n_bins": 2}

    blob.chmod(0o644)
    blob.write_text(json.dumps({"bins": [], "n_bins": 2}))
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}}))
    assert out.is_error and "no longer matches its receipt" in out.text
    assert tools.sandbox.tool_calls[1:] == []


def test_a_reference_written_as_a_json_string_resolves_and_reproduces(tmp_path, monkeypatch):
    """Measured live 2026-10-26: a model filled a number-typed argument with the reference as text."""
    from auto_research.report.html import reproduce_code

    store = ReceiptStore(tmp_path / "receipts")
    calls = [ToolCall(id="c1", name=EXTRACT, arguments={"obsid": "01"}),
             ToolCall(id="c2", name=FIT, arguments={"bins": '{"$ref": "analyst_worker_0:r1#/bins/1"}',
                                                    "cl": {"$ref": "analyst_worker_0:r1#/"}})]
    tools, ledger = _run(tmp_path, calls, store)
    assert tools.sandbox.tool_calls[1][2] == {"bins": BINS[1], "cl": {"bins": BINS, "n_bins": 2}}
    r2 = store.get("analyst_worker_0:r2")
    assert r2.parents == ["analyst_worker_0:r1"]

    code, _ = reproduce_code(r2, calls_by_receipt(ledger_calls(ledger), [r2])[r2.handle], store)
    (tmp_path / "mod.py").write_text("def fn(**kwargs):\n    return kwargs\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "mod", raising=False)
    namespace: dict = {}
    exec(code, namespace)  # noqa: S102
    assert namespace["out"] == tools.sandbox.tool_calls[1][2]


def test_the_page_check_accepts_reproduce_code_that_resolves_references(tmp_path):
    from html import escape

    from auto_research.report.html import check_page, reproduce_code

    store = ReceiptStore(tmp_path / "receipts")
    calls = [ToolCall(id="c1", name=EXTRACT, arguments={"obsid": "01"}),
             ToolCall(id="c2", name=FIT, arguments={"bins": {"$ref": "analyst_worker_0:r1#/bins"}})]
    _, ledger = _run(tmp_path, calls, store)
    r2 = store.get("analyst_worker_0:r2")
    code, args_json = reproduce_code(r2, calls_by_receipt(ledger_calls(ledger), [r2])[r2.handle], store)
    page = tmp_path / "report.html"
    page.write_text(f'<html><body><button type="button" class="copy" data-code="{escape(code)}" '
                    f'data-args="{escape(args_json)}">copy</button><script></script></body></html>')
    assert not [e for e in check_page(page) if "reproduce block" in e]


def _echoing(module, fn, args):
    """A tool that echoes its own parameters (as `cl` and `e_edges` are in the real toolkit)."""
    payload = {"cl": args.get("cl"), "e_edges": args.get("e_edges"), "bins": BINS,
               "ordered": {"2-3 keV": 1.0, "3-5 keV": 2.0, "10-12 keV": 3.0}}
    return {"exit_code": 0, "stdout": json.dumps(payload), **payload}


def test_a_parameter_the_caller_chose_and_the_tool_echoes_is_never_a_copy(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    tools.sandbox._tool_fn = _echoing
    grid = {"cl": 0.6827, "e_edges": [2.0, 2.828, 4.0, 5.657, 8.0]}
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01", **grid}))
    out = asyncio.run(tools.execute(EXTRACT, {"obsid": "02", **grid}))  # the same chosen grid
    assert not out.is_error, out.text
    # ...while a value the tool computed is still a copy when typed.
    out = asyncio.run(tools.execute(FIT, {"bins": [{"q": BINS[0]["q"]}]}))
    assert out.is_error and "bins/0/q" in out.text


def test_a_refusal_never_names_a_reference_that_would_itself_be_refused(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_0", kind="result", produced_by=EXTRACT,
               data=json.dumps({"bins": BINS}), failed=True)
    tools = _provider(tmp_path, store)
    out = asyncio.run(tools.execute(FIT, {"bins": BINS}))
    assert not out.is_error, out.text  # the only holder is failed: nothing to point at, so it runs


def test_a_reference_keeps_the_key_order_the_tool_returned(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    tools.sandbox._tool_fn = _echoing
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    tools.sandbox._tool_fn = _tool
    asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/ordered"}}))
    assert list(tools.sandbox.tool_calls[1][2]["bins"]) == ["2-3 keV", "3-5 keV", "10-12 keV"]


def test_the_brief_check_reads_bracketed_handles_and_accepts_what_a_message_showed(tmp_path):
    from auto_research.agents.reviewer_tools import reviewer_turn_problems
    from auto_research.schemas.review import ReviewerReport

    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_1", kind="result", produced_by=EXTRACT, data=json.dumps({"bins": BINS}))
    report = ReviewerReport(rationale="r", assign_to="analysts",
                            follow_up="Fit powerlaw:p1 with gauss:r2 using [analyst_worker_1:r1].")
    refused = reviewer_turn_problems(report, lookup=store, scope=[], budgets={"analysts": 1})
    assert [p for p in refused if "cannot open" in p] == [
        "follow_up names analyst_worker_1:r1, which you cannot open. Name the results you can open "
        "(your own, and those the reports you received cite), copying each handle exactly."]
    shown = reviewer_turn_problems(report, lookup=store, scope=[], budgets={"analysts": 1},
                                   viewable={"analyst_worker_1:r1"}.__contains__)
    assert not [p for p in shown if "cannot open" in p]


def test_the_page_check_refuses_a_reproduce_block_of_the_wrong_shape(tmp_path):
    from html import escape

    from auto_research.report.html import check_page

    args = json.dumps({"bins": {"$ref": "analyst_worker_0:r1#/bins"}})
    for code in (f"args = json.loads({args!r})", f"args = _ref(json.loads({args!r})"):
        page = tmp_path / "report.html"
        page.write_text(f'<html><body><button class="copy" data-code="{escape(code)}" '
                        f'data-args="{escape(args)}">c</button><script></script></body></html>')
        assert [e for e in check_page(page) if "reproduce block" in e], code
