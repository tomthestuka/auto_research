"""The input validator over every argument case the ledger studies found, and every kind × shape ×
reference target (user order 2026-11-03: the tests exhaust the cases; no LLM).

Two tables drive ``SandboxToolProvider.execute`` (the worker's production call path):

* ``LEDGER`` — argument values as models wrote them in real runs, from the three ledger studies of
  2026-11-03 (session scratch ``handles_only_A`` = run faraday-refs-20261026, ``_B`` = run 32814459,
  ``_C`` = the September PI #12 runs). Each case names the study, the ledger sequence or slice, the
  study's origin class and the kind the argument would declare. Values are kept as written.
* ``MATRIX`` — each kind against each value shape and each reference target, so no combination
  is left to chance.

A case expects ``RUNS`` (the tool receives plain values: no reference and no reason left) or a
phrase of the refusal the model reads.
"""

import asyncio
import json
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.provenance import ReceiptStore
from auto_research.provenance.refs import refs_in
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

TOOL = "research_toolkit.tk.call"
MAKE = "research_toolkit.tk.make"
ME, PEER = "analyst_worker_0", "analyst_worker_1"
RUNS = None

#: One argument of each kind (two parameters: required, and optional with a default).
SCHEMA = {
    "properties": {
        "d": {"kind": "data"},
        "p": {"kind": "parameter"},
        "q": {"kind": "parameter", "default": 0.99},
        "path": {"kind": "path"},
        "s": {"kind": "setting"},
    },
    "required": ["p"],
}
RESULT = {"M2": {"ci_lo_rad": -0.3222161424653368, "chi2": 1.0035928731}, "bins": [{"q": 0.0215103980, "q_err": 0.0020782946},
                                                                                    {"q": -0.0230234694, "q_err": 0.0020757915}],
          "n_bins": 2, "file": "spec.pha"}
OK = {"p": {"value": 1.0, "reason": "r"}}  # the required parameter, satisfied


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    monkeypatch.setattr(react, "_observation_path_args", lambda: {"path": str(tmp_path / "IXPE")})


def _tool(tmp_path):
    made = []

    def run(module, fn, args):
        if fn == "make":  # writes a file of its own and names it, as a reduction tool does
            out = tmp_path / f"spec_{len(made)}.pha"
            out.write_text(f"PHA {len(made)}\n")
            made.append(out)
            payload = {**RESULT, "file": str(out)}
            return {"exit_code": 0, "stdout": json.dumps(payload), **payload}
        return {"exit_code": 0, "stdout": "ok", "seen": 1}
    return run


class _Registry(_FakeRegistry):
    def spec(self, k):
        return {"module": "tk", "fn": k.rsplit(".", 1)[1], "args_schema": SCHEMA}


@pytest.fixture
def run(tmp_path):
    """The run state every case may point at: own and peer results and files, a brief, the
    request, a file read, shell output, a failed result, a figure; then one call."""
    store = ReceiptStore(tmp_path / "receipts")
    tool = _tool(tmp_path)  # one counter: each worker's file has its own name

    def provider(source):
        registry = _Registry(known={TOOL, MAKE}, mcp=set())
        return SandboxToolProvider(registry, _FakeSandbox(tool_fn=tool), cwd=tmp_path / source,
                                   receipts=store, receipt_source=source)

    mine = provider(ME)
    asyncio.run(provider(PEER).execute(MAKE, OK))  # analyst_worker_1:r1, :d1
    asyncio.run(mine.execute(MAKE, OK))  # analyst_worker_0:r1, :d1
    # analyst_worker_0:r2 — a call with a chosen parameter, a setting and a passed default.
    asyncio.run(mine.execute(TOOL, {"p": {"value": [2.0, 4.0, 8.0], "reason": "the brief's bands"}, "s": "0.5"}))
    store.mint_input("Fit the bands; theta_e = 0.2.")  # input:r1, a brief
    store.mint_input("Reproduce PD = 0.0215 for ObsID 01002901.", kind="human", produced_by="research_request")  # input:h1
    store.mint(source=ME, kind="result", produced_by="read_file", data="E,pa\n2.5,12.3456789\n")  # r3
    store.mint(source=ME, kind="result", produced_by="mcp.builtin.bash_exec", data='{"b": 2482606.1575153912}')  # r4
    store.mint(source=ME, kind="result", produced_by=MAKE, data=json.dumps(RESULT), failed=True)  # r5
    png = tmp_path / "fig.png"
    png.write_bytes(b"\x89PNG\r\n")
    store.mint(source=ME, kind="figure", produced_by=MAKE, path=png, caption="PD", mime_type="image/png")  # f1
    calls_before = len(mine.sandbox.tool_calls)

    def call(args):
        out = asyncio.run(mine.execute(TOOL, args))
        ran = len(mine.sandbox.tool_calls) > calls_before
        return out, ran, (mine.sandbox.tool_calls[-1][2] if ran else None), store

    return call


def _check(run, args, expect):
    out, ran, received, store = run(args)
    if expect is RUNS:
        assert not out.is_error and ran, out.text
        assert not refs_in(received), received
        assert not any(isinstance(v, dict) and set(v) == {"value", "reason"} for v in received.values()), received
    else:
        assert out.is_error and not ran, (args, out.text)
        assert expect in out.text, (expect, out.text)
    return received, store


def ref(text):
    return {"$ref": text}


def chosen(value, reason="why"):
    return {"value": value, "reason": reason}


# -- what the ledgers hold ------------------------------------------------------------------------

LEDGER = [
    # study, place, origin class (the study's), the case: (arguments, expected)
    ("A seq-4381, retyped exactly from a result", {"p": 0.3222161424653368}, "argument p is a parameter"),
    ("A, retyped sign-flipped (|ci_lo|)", {"p": 0.3222161424653368 * -1}, "argument p is a parameter"),
    ("A, already a $ref into a peer-of-run field", {"p": ref(f"{ME}:r1#/M2/ci_lo_rad")}, RUNS),
    ("A, a $ref to a whole result with no '#'", {"d": ref(f"{ME}:r1"), **OK}, RUNS),
    ("A, a $ref ending in a bare '#'", {"d": ref(f"{ME}:r1#"), **OK}, RUNS),
    ("A seq-1210, a $ref written as a JSON string", {"d": json.dumps(ref(f"{ME}:r1#/bins")), **OK}, RUNS),
    ("A, a flag the tool's refusal asked for (overwrite)", {"s": True, **OK}, RUNS),
    ("A, theta_e 0.2 stated in the reviewer's prose brief, typed bare", {"p": 0.2}, "argument p is a parameter"),
    ("A, the same value by reference to the brief (a brief is never a source)", {"p": ref("input:r1#")}, "a task brief"),
    ("A, the same value typed with where it is stated", {"p": chosen(0.2, "the reviewer's brief sets theta_e")}, RUNS),
    ("A, selection 'rej' spelled by the lead", {"s": "rej", **OK}, RUNS),
    ("A, a free label 'TEST'", {"s": "TEST", **OK}, RUNS),
    ("A, a tool default passed explicitly as a setting", {"s": "ALL", **OK}, RUNS),
    ("A, an ObsID string", {"s": "01002901", **OK}, RUNS),
    ("B, ObsIDs as a JSON-encoded list string", {"s": "[\"02008401\"]", **OK}, RUNS),
    ("A, the data directory typed from the brief", {"path": "/data/x/IXPE", **OK}, RUNS),
    ("A seq-291, b_parallel_g copied from shell output", {"p": 2482606.1575153912}, "argument p is a parameter"),
    ("A, b_parallel_g by reference to the shell output", {"p": ref(f"{ME}:r4#/b")}, "shell output, a place to look"),
    ("A, e_edges equal to a native grid read in the shell", {"p": [2, 3, 4, 5, 6, 8]}, "argument p is a parameter"),
    ("A, projection_factor 1/sqrt(3) by hand", {"p": 0.5773502691896258}, "argument p is a parameter"),
    ("A, 1/sqrt(3) typed with its reason (accepted; shown as chosen)", {"p": chosen(0.5773502691896258, "isotropic projection")}, RUNS),
    ("B seq-4552, a hand-built fit block (20 leaves)", {"d": {"M1": {"chi0_deg": 164.04921192224566, "dof": 7}}, **OK}, "argument d is a tool's output"),
    ("B, plot bins copied from a CSV the worker read", {"d": {"bins": [{"pa_err_deg": 1.9338049498942111, "e_lo": 6.0}]}, **OK}, "argument d is a tool's output"),
    ("B, error bars rounded from another field", {"d": [{"pd_err": 0.0040666}, {"pd_err": 0.0049}], **OK}, "argument d is a tool's output"),
    ("B, a bins dict mixing a reference and a retyped count", {"d": {"bins": ref(f"{ME}:r1#/bins"), "n_bins": 2}, **OK}, "argument d is a tool's output"),
    ("B, cl 0.99 from the brief, equal to the default, left out", {**OK}, RUNS),
    ("B, cl 0.99 typed bare", {"q": 0.99, **OK}, "argument q is a parameter"),
    ("B, an integer grid the model chose", {"p": [1, 2, 5, 10, 20, 50]}, "argument p is a parameter"),
    ("B, the same grid with its reason", {"p": chosen([1, 2, 5, 10, 20, 50], "a 1-2-5 sequence")}, RUNS),
    ("B, min_level 1 chosen", {"s": 1, **OK}, RUNS),
    ("B, a figure title", {"s": "Cyg X-1 2023 soft-state campaign: PA(E) Faraday fit", **OK}, RUNS),
    ("B, an epoch dict with a STATE label (a setting)", {"s": {"IXPE": ["02008401"], "STATE": "HSS"}, **OK}, RUNS),
    ("C ev600, RA from a shell header print", {"p": 299.59032}, "argument p is a parameter"),
    ("C ev7166, load_data indices retyped by hand", {"d": {"IXPE": {"01002901": {"DU3": [7, 8]}}}, **OK}, "argument d is a tool's output"),
    ("C ev2988, y values from a CSV read", {"d": [4.986339062452316, 5.1], **OK}, "argument d is a tool's output"),
    ("C ev590, bkg_outer_rad 5.1667 from the brief", {"p": 5.1667}, "argument p is a parameter"),
    ("C, 310/60 computed by hand", {"p": 5.166667}, "argument p is a parameter"),
    ("C, an output CSV path chosen", {"path": "/data/x/out.csv", **OK}, RUNS),
    ("C, a flag 'resume false'", {"s": False, **OK}, RUNS),
    ("C, a column name seen in a file read", {"s": "PA_deg", **OK}, RUNS),
    ("A seq-760, a $ref to the file a peer wrote", {"path": ref(f"{PEER}:d1"), **OK}, "a file analyst_worker_1's tool wrote"),
    ("A, a $ref to a peer's result", {"d": ref(f"{PEER}:r1#/bins"), **OK}, "is analyst_worker_1's result"),
    ("A, a $ref to the request", {"p": ref("input:h1#")}, "is the request"),
    ("A, a $ref to a file read", {"p": ref(f"{ME}:r3#")}, "a file you read"),
]


@pytest.mark.parametrize("args, expect", [c[1:] for c in LEDGER], ids=[c[0] for c in LEDGER])
def test_every_case_the_ledgers_hold(run, args, expect):
    _check(run, args, expect)


# -- every kind × shape × target -----------------------------------------------------------------

#: (id, arguments, expected): each kind against each value shape.
SHAPES = [
    # data: only references into what the caller's own tools returned
    ("data: a reference", {"d": ref(f"{ME}:r1#/bins")}, RUNS),
    ("data: a list of references", {"d": [ref(f"{ME}:r1#/bins/0"), ref(f"{ME}:r1#/bins/1")]}, RUNS),
    ("data: a dict of references", {"d": {"a": ref(f"{ME}:r1#/bins/0"), "b": ref(f"{ME}:r1#/M2")}}, RUNS),
    ("data: a reference as a JSON string inside a list", {"d": [json.dumps(ref(f"{ME}:r1#/bins/0"))]}, RUNS),
    ("data: a number", {"d": 0.0215103980}, "argument d is a tool's output"),
    ("data: a number as a string", {"d": "0.0215103980"}, "argument d is a tool's output"),
    ("data: a JSON list as a string", {"d": "[0.0215103980]"}, "argument d is a tool's output"),
    ("data: an empty list", {"d": []}, "argument d is a tool's output"),
    ("data: null", {"d": None}, "argument d is a tool's output"),
    ("data: a list mixing a reference and a number", {"d": [ref(f"{ME}:r1#/bins/0"), 0.5]}, "argument d is a tool's output"),
    ("data: a value with a reason", {"d": chosen([1.0])}, "argument d is a tool's output"),
    ("data: a brief", {"d": ref("input:r1#")}, "argument d is a tool's output"),
    ("data: the request", {"d": ref("input:h1#")}, "comes from the tool that produces it"),
    ("data: a call's chosen argument", {"d": ref(f"{ME}:r2#/$args/p")}, "argument d is a tool's output"),
    ("data: a file the caller wrote", {"d": ref(f"{ME}:d1")}, RUNS),
    # parameter: a reference, a value with its reason, or left out (the default)
    ("parameter: a reference", {"p": ref(f"{ME}:r1#/M2/chi2")}, RUNS),
    ("parameter: a list of references", {"p": [ref(f"{ME}:r1#/bins/0/q"), ref(f"{ME}:r1#/bins/1/q")]}, RUNS),
    ("parameter: a value with a reason", {"p": chosen(0.5)}, RUNS),
    ("parameter: a value with a reason, as a JSON string", {"p": json.dumps(chosen(0.5))}, RUNS),
    ("parameter: a list with a reason", {"p": chosen([2.0, 8.0])}, RUNS),
    ("parameter: the caller's own earlier choice", {"p": ref(f"{ME}:r2#/$args/p")}, RUNS),
    ("parameter: a number", {"p": 0.5}, "argument p is a parameter"),
    ("parameter: an integer", {"p": 3}, "argument p is a parameter"),
    ("parameter: a number as a string", {"p": "0.5"}, "argument p is a parameter"),
    ("parameter: a list of numbers", {"p": [2.0, 8.0]}, "argument p is a parameter"),
    ("parameter: null", {"p": None}, "argument p is a parameter"),
    ("parameter: a boolean", {"p": True}, "argument p is a parameter"),
    ("parameter: an empty reason", {"p": chosen(0.5, "")}, "argument p is a parameter"),
    ("parameter: a blank reason", {"p": chosen(0.5, "   ")}, "argument p is a parameter"),
    ("parameter: a reason that is not text", {"p": {"value": 0.5, "reason": 7}}, "argument p is a parameter"),
    ("parameter: a value object with an extra key", {"p": {"value": 0.5, "reason": "r", "x": 1}}, "argument p is a parameter"),
    ("parameter: a value object with no reason", {"p": {"value": 0.5}}, "argument p is a parameter"),
    ("parameter: a value holding a reference", {"p": chosen(ref(f"{ME}:r1#/M2/chi2"))}, "argument p is a parameter"),
    ("parameter: a value holding a nested reference", {"p": chosen([ref(f"{ME}:r1#/M2/chi2")])}, "argument p is a parameter"),
    ("parameter: a list mixing a reference and a number", {"p": [ref(f"{ME}:r1#/M2/chi2"), 0.5]}, "argument p is a parameter"),
    ("parameter: the optional one typed bare", {"q": 0.95, **OK}, "argument q is a parameter"),
    ("parameter: the optional one as null", {"q": None, **OK}, "argument q is a parameter"),
    ("parameter: a call's setting by $args", {"p": ref(f"{ME}:r2#/$args/s")}, "typed with no reason and no reference"),
    ("parameter: a call's default by $args", {"p": ref(f"{ME}:r2#/$args/q")}, "typed with no reason and no reference"),
    ("parameter: an argument a call never took", {"p": ref(f"{ME}:r2#/$args/zz")}, "took no argument 'zz'"),
    ("parameter: $args with no argument named", {"p": ref(f"{ME}:r2#/$args")}, "name an argument"),
    # path: typed, or a file the caller's own tool wrote
    ("path: typed", {"path": "/data/x/file.pha", **OK}, RUNS),
    ("path: a file the caller wrote", {"path": ref(f"{ME}:d1"), **OK}, RUNS),
    ("path: a pointer into a file", {"path": ref(f"{ME}:d1#/x"), **OK}, "passes as its path"),
    ("path: a path a result returned", {"path": ref(f"{ME}:r1#/file"), **OK}, RUNS),
    ("path: a value with a reason", {"path": chosen("/x"), **OK}, "which takes no reason"),
    # setting: typed, any JSON
    ("setting: text", {"s": "soft", **OK}, RUNS),
    ("setting: a number", {"s": 300, **OK}, RUNS),
    ("setting: a dict", {"s": {"IXPE": ["01"]}, **OK}, RUNS),
    ("setting: null", {"s": None, **OK}, RUNS),
    ("setting: a value with a reason", {"s": chosen("soft"), **OK}, "which takes no reason"),
    ("setting: a reason object as a JSON string", {"s": json.dumps(chosen("soft")), **OK}, "which takes no reason"),
    # the call as a whole
    ("undeclared: an argument the tool does not take", {"norm": 0.379, **OK}, "argument norm is not one this tool takes"),
    ("runtime: env picks the environment and never reaches the tool", {"env": "ar-x", **OK}, RUNS),
]

#: (id, reference, expected): each kind of receipt a reference can name, as a parameter.
TARGETS = [
    ("own result", f"{ME}:r1#/n_bins", RUNS),
    ("own failed result", f"{ME}:r5#/n_bins", "flagged failed"),
    ("own figure", f"{ME}:f1", "a figure receipt, a place to look"),
    ("own file read", f"{ME}:r3", "a file you read"),
    ("own shell output", f"{ME}:r4", "shell output"),
    ("peer result", f"{PEER}:r1#/n_bins", "is analyst_worker_1's result"),
    ("peer file", f"{PEER}:d1", "a file analyst_worker_1's tool wrote"),
    ("brief whole", "input:r1#", "a task brief"),
    ("the request", "input:h1#", "is the request"),
    ("not in the run", f"{ME}:r99", "not a receipt of this run"),
    ("a mistyped handle", "worker0-r1", "not a receipt of this run"),
    ("a missing key", f"{ME}:r1#/nbins", "no key 'nbins'"),
    ("an index past the end", f"{ME}:r1#/bins/9", "not an index below 2"),
    ("past a single value", f"{ME}:r1#/n_bins/0", "a single value"),
]


@pytest.mark.parametrize("args, expect", [c[1:] for c in SHAPES], ids=[c[0] for c in SHAPES])
def test_every_kind_against_every_shape(run, args, expect):
    _check(run, args if "p" in args else {**OK, **args}, expect)


@pytest.mark.parametrize("target, expect", [c[1:] for c in TARGETS], ids=[c[0] for c in TARGETS])
def test_every_reference_target(run, target, expect):
    _check(run, {"p": ref(target)}, expect)


def test_what_the_tool_receives_and_what_the_call_records(run, tmp_path):
    """The plain values the tool gets, and the record that keeps the reason, the reference and the
    filled values behind the call's handle."""
    from auto_research.provenance.refs import call_arguments

    received, store = _check(run, {"d": ref(f"{ME}:r1#/bins/0"), "p": chosen([2.0, 8.0], "the band"),
                                   "path": ref(f"{ME}:d1"), "s": "soft"}, RUNS)
    assert received == {"d": RESULT["bins"][0], "p": [2.0, 8.0], "q": 0.99,
                        "path": str(tmp_path / "spec_1.pha"), "s": "soft"}
    handle = store.receipts(source=ME, kind="result")[-1].handle
    assert call_arguments(store, handle) == {"d": ref(f"{ME}:r1#/bins/0"), "p": chosen([2.0, 8.0], "the band"),
                                             "q": 0.99, "path": ref(f"{ME}:d1"), "s": "soft"}
    # The mission folder is filled when the model leaves a path out.
    received, _ = _check(run, {**OK}, RUNS)
    assert received["path"] == str(tmp_path / "IXPE")


def test_ledger_cases_cover_every_origin_class_of_the_studies():
    """Every origin class the studies used is represented by at least one real case."""
    labels = " ".join(c[0] for c in LEDGER)
    for origin in ("retyped", "$ref", "brief", "chosen", "label", "ObsID", "directory", "shell",
                   "by hand", "CSV", "default", "peer", "request", "file read"):
        assert origin in labels, origin
