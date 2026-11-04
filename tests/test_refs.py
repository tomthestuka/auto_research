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
from auto_research.provenance.refs import CONSOLE_SEPARATOR, handles_named, returned_value
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


def _provider(tmp_path, store, *, source="analyst_worker_0"):
    registry = _FakeRegistry(known={EXTRACT, FIT, BASH}, mcp={BASH},
                             schema={"properties": {"bins": {"kind": "data"}, "cl": {"kind": "parameter"},
                                                    "obsid": {"kind": "setting"}},
                                     "required": ["cl"]})
    return SandboxToolProvider(registry, _FakeSandbox(tool_fn=_tool, mcp_fn=lambda k, a: {
        "exit_code": 0, "stdout": json.dumps({"bins": BINS}), "stderr": ""}),
        cwd=tmp_path / source, receipts=store, receipt_source=source)


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
        ToolCall(id="c2", name=FIT, arguments={"bins": {"$ref": "analyst_worker_0:r1#/bins"},
                                               "cl": {"value": 0.99, "reason": "the brief's 99% limit"}}),
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

    # The ledger keeps the references and the reason; the receipt hashes them; the call rebuilds.
    rebuilt = ledger_calls(ledger)
    assert rebuilt[1].sent_args["bins"] == {"$ref": "analyst_worker_0:r1#/bins"}
    assert rebuilt[1].sent_args["cl"] == {"value": 0.99, "reason": "the brief's 99% limit"}
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


def test_a_call_passes_on_only_its_own_results_and_a_peer_value_is_reproduced(tmp_path):
    """User ruling 2026-11-03: a $ref resolves the caller's own results only, so a tool echoing a
    peer's value cannot make it the caller's evidence; a typed parameter needs its reason."""
    store = ReceiptStore(tmp_path / "receipts")
    peer = _provider(tmp_path, store, source="analyst_worker_1")
    asyncio.run(peer.execute(EXTRACT, {"obsid": "01"}))  # analyst_worker_1:r1

    worker = _provider(tmp_path, store)
    out = asyncio.run(worker.execute(FIT, {"bins": {"$ref": "analyst_worker_1:r1#/bins"}}))
    assert out.is_error and "is analyst_worker_1's result" in out.text and "reproduce" in out.text
    out = asyncio.run(worker.execute(FIT, {"bins": {"$ref": "analyst_worker_1:r1#/bins"}, "cl": BINS[0]["q"]}))
    assert out.is_error and "argument cl is a parameter" in out.text
    out = asyncio.run(worker.execute(FIT, {"bins": {"$ref": "analyst_worker_1:r9#/bins"}}))
    assert out.is_error and "not a receipt of this run" in out.text
    assert worker.sandbox.tool_calls == []

    asyncio.run(worker.execute(EXTRACT, {"obsid": "01"}))  # analyst_worker_0:r1, its own run
    out = asyncio.run(worker.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}}))
    assert not out.is_error and store.get("analyst_worker_0:r2").parents == ["analyst_worker_0:r1"]


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


def test_typed_data_and_a_parameter_without_its_reason_never_run(tmp_path):
    """User rulings 2026-11-03: a tool's output passes by reference, and a typed parameter carries
    why it was chosen; the call does not run otherwise (a note after the call was ignored live)."""
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    for args in ({"bins": BINS, "cl": {"value": 0.99, "reason": "r"}},
                 {"bins": json.dumps(BINS), "cl": {"value": 0.99, "reason": "r"}},  # the REPL parses it
                 {"bins": {"$ref": "analyst_worker_0:r1#/bins"}, "cl": 0.99},
                 {"bins": {"$ref": "analyst_worker_0:r1#/bins"}, "cl": {"value": 0.99, "reason": " "}},
                 {"bins": [{"$ref": "analyst_worker_0:r1#/bins/0"}, BINS[1]], "cl": {"value": 0.99, "reason": "r"}}):
        out = asyncio.run(tools.execute(FIT, args))
        assert out.is_error and "This call was not run" in out.text, args
    assert len(tools.sandbox.tool_calls) == 1  # none of them ran
    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"}, "cl": 0.99}))
    assert 'pass {"$ref": "<handle>#/<key>"}' in out.text and '{"value": <the value>, "reason": "<why this value>"}' in out.text

    out = asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"},
                                          "cl": '{"value": 0.99, "reason": "the brief asks for 99%"}'}))
    assert not out.is_error and store.get("analyst_worker_0:r2").parents == ["analyst_worker_0:r1"]
    assert tools.sandbox.tool_calls[1][2] == {"bins": BINS, "cl": 0.99}


def test_a_text_names_its_handles_bracketed_or_bare_once_each():
    text = "Refit with [analyst_worker_0:r1] and analyst_worker_0:r2, then [analyst_worker_0:r1]; see lib:p3."
    assert handles_named(text) == ["analyst_worker_0:r1", "analyst_worker_0:r2", "lib:p3"]


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


def test_a_reference_keeps_the_key_order_the_tool_returned(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    tools.sandbox._tool_fn = _echoing
    asyncio.run(tools.execute(EXTRACT, {"obsid": "01"}))
    tools.sandbox._tool_fn = _tool
    asyncio.run(tools.execute(FIT, {"bins": {"$ref": "analyst_worker_0:r1#/ordered"}}))
    assert list(tools.sandbox.tool_calls[1][2]["bins"]) == ["2-3 keV", "3-5 keV", "10-12 keV"]


def test_the_brief_check_reads_cited_handles_and_refuses_one_not_in_the_run(tmp_path):
    from auto_research.agents.reviewer_tools import reviewer_turn_problems
    from auto_research.schemas.review import ReviewerReport

    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_1", kind="result", produced_by=EXTRACT, data=json.dumps({"bins": BINS}))
    report = ReviewerReport(rationale="r", assign_to="analysts",
                            follow_up="Fit powerlaw:p1 with gauss:r2 using {{analyst_worker_1:r1}}.")
    in_run = lambda h: store.get(h) is not None  # the runtime's predicate (roles._seat_viewable)
    ok = reviewer_turn_problems(report, lookup=store, scope=[], budgets={"analysts": 1}, viewable=in_run)
    assert not [p for p in ok if "of this run" in p]
    mistyped = report.model_copy(update={"follow_up": "Fit using {{analyst_worker_1:r7}}."})
    refused = reviewer_turn_problems(mistyped, lookup=store, scope=[], budgets={"analysts": 1},
                                     viewable=in_run)
    assert [p for p in refused if "of this run" in p] == [
        "follow_up names analyst_worker_1:r7, which is not a receipt of this run. Copy each handle "
        "exactly as a tool result or report shows it."]


def test_the_page_check_refuses_a_reproduce_block_of_the_wrong_shape(tmp_path):
    from html import escape

    from auto_research.report.html import check_page

    args = json.dumps({"bins": {"$ref": "analyst_worker_0:r1#/bins"}})
    for code in (f"args = json.loads({args!r})", f"args = _ref(json.loads({args!r})"):
        page = tmp_path / "report.html"
        page.write_text(f'<html><body><button class="copy" data-code="{escape(code)}" '
                        f'data-args="{escape(args)}">c</button><script></script></body></html>')
        assert [e for e in check_page(page) if "reproduce block" in e], code


def test_a_brief_without_parameters_is_typed_from_with_its_reason(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    brief = store.mint_input("Start the fit at norm = 0.0123456789.")
    worker = _provider(tmp_path, store)
    out = asyncio.run(worker.execute(FIT, {"bins": {"$ref": f"{brief.handle}#"}, "cl": 0.5}))
    assert out.is_error and "is a parameter" in out.text
    out = asyncio.run(worker.execute(FIT, {"cl": {"$ref": f"{brief.handle}#"}}))
    assert out.is_error and 'is a task brief: type a value it states as {"value": …' in out.text


def test_a_reference_to_the_request_or_a_place_to_look_is_named_by_what_it_is(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    request = store.mint_input("Fit ObsID 01.", kind="human", produced_by="research_request")
    shell = store.mint(source="analyst_worker_1", kind="result", produced_by="mcp.builtin.bash_exec", data="{}")
    worker = _provider(tmp_path, store)
    out = asyncio.run(worker.execute(FIT, {"cl": {"$ref": f"{request.handle}#"}}))
    assert out.is_error and 'is the request: type a value it states as {"value"' in out.text
    out = asyncio.run(worker.execute(FIT, {"bins": {"$ref": f"{shell.handle}#"}}))
    assert out.is_error and "shell output" in out.text and "'s result" not in out.text


def test_a_brief_naming_a_mistyped_handle_in_the_retired_brackets_is_refused_too(tmp_path):
    from auto_research.provenance.refs import brief_handle_problems

    store = ReceiptStore(tmp_path / "receipts")
    store.mint(source="analyst_worker_1", kind="result", produced_by=EXTRACT, data="{}")
    viewable = lambda h: store.get(h) is not None  # noqa: E731
    assert brief_handle_problems("what_to_probe", "Use [analyst_worker_1:r1] and {{analyst_worker_1:r1}}.", viewable) == []
    (problem,) = brief_handle_problems("what_to_probe", "Use [analyst_worker_1:r99].", viewable)
    assert "analyst_worker_1:r99" in problem
