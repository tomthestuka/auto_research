"""The run ledger rebuilds the arguments every worker call ran with (no LLM).

Drives the real worker loop (`AgentRole._react`), the real `SandboxToolProvider`, the real
`RunMonitor` and `ReceiptStore`; only the model and the sandbox's tool process are fakes.
"""

import asyncio
import json
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.agents.roles import AnalystWorkerAgent
from auto_research.models import ModelResponse, ToolCall
from auto_research.monitor.events import RunMonitor
from auto_research.provenance import ReceiptStore
from auto_research.provenance.calls import calls_by_receipt, ledger_calls, resolve_call_args
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

TOOL = "research_toolkit.tk.fit"
BASH = "mcp.builtin.bash_exec"
LONG = "x" * 300_000


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    monkeypatch.setattr(react, "_observation_path_args",
                        lambda: {"ixpe_path": "/obs/IXPE", "nicer_path": "/obs/NICER"})


class _ScriptedModel:
    """Issues the scripted tool calls one per turn, then ends the loop."""

    def __init__(self, calls):
        self._calls = list(calls)

    async def call(self, messages, **kwargs):
        if self._calls:
            return ModelResponse(tool_calls=[self._calls.pop(0)])
        return ModelResponse(content="done")


def _run(tmp_path, calls, sandbox=None, known=(TOOL, BASH), action="analyze", store=None):
    store = store or ReceiptStore(tmp_path / "receipts")
    ledger = tmp_path / "monitor.jsonl"
    sandbox = sandbox or _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "PD=3.4", "stderr": ""})
    registry = _FakeRegistry(known=set(known), mcp={BASH},
                             schema={"properties": {"obsid": {}, "ixpe_path": {}, "save_path": {}, "note": {}}})
    tools = SandboxToolProvider(registry, sandbox, cwd=tmp_path / "w0", receipts=store,
                                receipt_source="analyst_worker_0")
    worker = AnalystWorkerAgent(model=_ScriptedModel(calls), prompt="p",
                                monitor=RunMonitor(chatter=10, jsonl_path=ledger))
    worker.agent_key = "analyst_worker_0"
    asyncio.run(worker._react(seed_messages=[], tools=tools, action=action, cap=10))
    return store, ledger, sandbox


def test_every_call_rebuilds_from_the_ledger_to_its_receipt_and_to_what_the_tool_received(tmp_path):
    calls = [
        ToolCall(id="c1", name=TOOL, arguments={"obsid": "01002901", "save_path": "/tmp/q.png", "note": LONG}),
        ToolCall(id="c2", name=TOOL, arguments={"obsid": "01002901"}),  # unnamed figure: stamped
        ToolCall(id="c3", name=TOOL, arguments={"obsid": "01002901", "save_path": "/tmp/q.png", "note": LONG}),
        ToolCall(id="c4", name=TOOL, arguments={"obsid": "02001", "ixpe_path": "/mine", "env": "ar-x"}),
        ToolCall(id="c5", name=BASH, arguments={"command": "echo hi", "shell_env": "planted"}),
    ]
    store, ledger, sandbox = _run(tmp_path, calls)

    rebuilt = ledger_calls(ledger)
    assert [c.tool_call_id for c in rebuilt] == ["c1", "c2", "c3", "c4", "c5"]
    # What the tool process actually received, call by call, is what the ledger rebuilds.
    received = [args for _, _, args in sandbox.tool_calls] + [args for _, args in sandbox.mcp_calls]
    assert [c.sent_args for c in rebuilt] == received
    assert rebuilt[3].ran_args["env"] == "ar-x" and "env" not in rebuilt[3].sent_args
    assert rebuilt[0].ran_args["save_path"] == str(tmp_path / "w0" / "figures" / "q.png")
    assert rebuilt[0].ran_args["ixpe_path"] == "/obs/IXPE"
    assert rebuilt[1].ran_args["save_path"].startswith(str(tmp_path / "w0" / "figures" / "fn_"))
    assert rebuilt[3].ran_args["ixpe_path"] == "/mine"
    assert "shell_env" not in rebuilt[4].ran_args and rebuilt[4].ran_args["cwd"] == str(tmp_path / "w0")

    # Every receipt finds its call; identical calls pair in order.
    results = store.receipts(source="analyst_worker_0", kind="result")
    matched = calls_by_receipt(rebuilt, store.receipts())
    assert [matched[r.handle].tool_call_id for r in results] == ["c1", "c2", "c3", "c4", "c5"]
    assert all(matched[r.handle].args_sha256 == r.args_sha256 for r in results)


def test_the_ledger_records_every_field_whole(tmp_path):
    store, ledger, _ = _run(tmp_path, [ToolCall(id="c1", name=TOOL, arguments={"note": LONG})])
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    issued = next(e for e in events if e["event"].endswith(".tool_call"))
    assert json.loads(issued["data"]["args"])["note"] == LONG
    returned = next(e for e in events if e["event"].endswith(".tool_result"))
    assert returned["data"]["text"].endswith("PD=3.4") and "chars]" not in returned["data"]["text"]


def test_a_receipt_whose_call_does_not_rebuild_is_left_unmatched(tmp_path):
    """An edited ledger (or a rewrite the context does not capture) never pairs a receipt with
    the wrong call: the hash refuses it."""
    store, ledger, _ = _run(tmp_path, [ToolCall(id="c1", name=TOOL, arguments={"obsid": "01002901"})])
    lines = ledger.read_text().splitlines()
    edited = []
    for line in lines:
        event = json.loads(line)
        if event["event"].endswith(".tool_call"):
            event["data"]["args"] = json.dumps({"obsid": "09999999"})
        edited.append(json.dumps(event))
    ledger.write_text("\n".join(edited) + "\n")
    assert calls_by_receipt(ledger_calls(ledger), store.receipts()) == {}


def test_the_figure_rule_keeps_the_basename_and_owns_the_folder_and_extension():
    context = {"figure_args": ["save_path", "save_path_prefix"], "figures_dir": "/run/w0/figures",
               "fn": "plot", "stamp_ms": 17}
    args = resolve_call_args({"save_path": "../../etc/x.PDF", "save_path_prefix": " "}, context)
    assert args == {"save_path": "/run/w0/figures/x.png", "save_path_prefix": "/run/w0/figures/plot_17"}


def test_a_call_that_never_ran_is_never_paired_with_its_identical_twin(tmp_path):
    """A garbled call is answered, not run; the next call with the same arguments to another tool
    did run. The receipt pairs with the call that minted it, by identity."""
    other = "research_toolkit.tk.other"
    calls = [
        ToolCall(id="c1", name=other, arguments={"obsid": "X"}, arguments_error="unterminated string"),
        ToolCall(id="c2", name=TOOL, arguments={"obsid": "X"}),
    ]
    store, ledger, _ = _run(tmp_path, calls, known=(TOOL, other, BASH))
    rebuilt = ledger_calls(ledger)
    assert [c.tool_call_id for c in rebuilt] == ["c2"]
    (result,) = store.receipts(kind="result")
    assert calls_by_receipt(rebuilt, [result])[result.handle].tool == TOOL


def test_any_subset_of_receipts_pairs_with_its_own_call(tmp_path):
    same = {"obsid": "X"}
    calls = [ToolCall(id=f"c{i}", name=TOOL, arguments=dict(same)) for i in range(1, 4)]
    store, ledger, _ = _run(tmp_path, calls)
    results = store.receipts(kind="result")
    rebuilt = ledger_calls(ledger)
    for receipt, expected in zip(results, ["c1", "c2", "c3"]):
        assert calls_by_receipt(rebuilt, [receipt])[receipt.handle].tool_call_id == expected
    reversed_match = calls_by_receipt(rebuilt, list(reversed(results)))
    assert [reversed_match[r.handle].tool_call_id for r in results] == ["c1", "c2", "c3"]


def test_a_shell_call_in_a_chosen_environment_rebuilds_and_names_what_was_sent(tmp_path):
    from auto_research.sandbox.envs import Environment

    own, chosen = Environment(name="own", prefix=tmp_path / "own"), Environment(name="ar-x", prefix=tmp_path / "x")
    sandbox = _FakeSandbox(mcp_fn=lambda k, a: {"exit_code": 0, "stdout": "hi", "stderr": ""})
    sandbox.environment, sandbox.shell_environment = own, "ar-x"
    sandbox.environments = lambda: [own, chosen]
    store, ledger, sandbox = _run(tmp_path, [ToolCall(id="c1", name=BASH, arguments={"command": "echo hi"})],
                                  sandbox=sandbox)
    (call,) = ledger_calls(ledger)
    assert call.ran_args["shell_env"] == "ar-x" and "shell_env" not in call.sent_args
    (_, received), = sandbox.mcp_calls
    assert received["cwd"] == call.sent_args["cwd"] and "echo hi" in received["command"]
    (result,) = store.receipts(kind="result")
    assert calls_by_receipt([call], [result]) == {result.handle: call}


def test_a_malformed_ledger_line_is_skipped_not_fatal(tmp_path):
    store, ledger, _ = _run(tmp_path, [ToolCall(id="c1", name=TOOL, arguments={"obsid": "X"})])
    with ledger.open("a") as handle:
        handle.write("[1, 2]\n{\"event\": \"x.tool_result\", \"actor\": \"analyst_worker_0\", \"data\": 3}\n{torn")
    assert [c.tool_call_id for c in ledger_calls(ledger)] == ["c1"]


def test_every_receipt_of_a_call_pairs_with_it_figures_and_data_too(tmp_path):
    """One call minting a result, a figure and a data file: all three join the same call."""
    from datetime import datetime, timezone

    def tool(m, f, a):
        Path(a["save_path"]).write_bytes(b"\x89PNG fig")
        Path(a["out_path"]).write_text("t,pd\n0,3.4\n")
        return {"exit_code": 0, "stdout": "wrote", "figures": [{
            "path": a["save_path"], "caption": "PD", "mime_type": "image/png", "produced_by": TOOL,
            "created_at": datetime.now(timezone.utc).isoformat(), "sha256": "0" * 64}]}

    (tmp_path / "w0").mkdir()
    call = ToolCall(id="c1", name=TOOL, arguments={"save_path": "pd", "out_path": str(tmp_path / "w0" / "s.csv")})
    store, ledger, _ = _run(tmp_path, [call], sandbox=_FakeSandbox(tool_fn=tool))
    assert {r.kind for r in store.receipts()} == {"result", "figure", "data"}
    matched = calls_by_receipt(ledger_calls(ledger), store.receipts())
    assert {matched[r.handle].tool_call_id for r in store.receipts()} == {"c1"}


def test_an_in_process_tool_pairs_and_an_unrun_call_mints_nothing(tmp_path):
    """`_receipted` tools carry their call id too; a call the guard refuses mints nothing."""
    from auto_research.sandbox.envs import Environment

    own = Environment(name="own", prefix=tmp_path / "own")
    sandbox = _FakeSandbox()
    sandbox.environment = own
    calls = [ToolCall(id="c1", name="use_env", arguments={}),
             ToolCall(id="c2", name="read_file", arguments={"path": "/etc/passwd"})]
    store, ledger, _ = _run(tmp_path, calls, sandbox=sandbox)
    rebuilt = ledger_calls(ledger)
    assert [c.tool_call_id for c in rebuilt] == ["c1"]
    (result,) = store.receipts(kind="result")
    assert calls_by_receipt(rebuilt, [result])[result.handle].tool == "use_env"


def test_two_loops_of_one_actor_reusing_a_call_id_never_cross(tmp_path):
    """Two loops on one actor (a resumed reply turn beside a new round) can each issue `c1`;
    with their lines interleaved, each receipt still joins its own call."""
    other = "research_toolkit.tk.other"
    store = ReceiptStore(tmp_path / "receipts")
    _run(tmp_path, [ToolCall(id="c1", name=TOOL, arguments={"obsid": "X"})], store=store,
         known=(TOOL, other, BASH), action="analyze")
    _, ledger, _ = _run(tmp_path, [ToolCall(id="c1", name=other, arguments={"obsid": "X"})], store=store,
                        known=(TOOL, other, BASH), action="resume")
    lines = ledger.read_text().splitlines()
    kinds = [json.loads(line)["event"].rsplit(".", 1)[-1] for line in lines]
    calls_at = [i for i, k in enumerate(kinds) if k == "tool_call"]
    results_at = [i for i, k in enumerate(kinds) if k == "tool_result"]
    order = [calls_at[0], calls_at[1], results_at[0], results_at[1]]
    ledger.write_text("\n".join(lines[i] for i in order) + "\n")
    first, second = store.receipts(kind="result")
    matched = calls_by_receipt(ledger_calls(ledger), [first, second])
    assert (matched[first.handle].tool, matched[second.handle].tool) == (TOOL, other)
