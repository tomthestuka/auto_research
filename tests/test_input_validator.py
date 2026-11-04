"""The input validator's tool-input job: argument kinds, defaults, files and
chosen values behind their call's handle (user rulings 2026-11-03; no LLM).

Drives the production entry points — ``ToolkitRegistry``, ``check_toolkit_manifests``,
``SandboxToolProvider.execute``, ``execute_data_catalog``, ``check_structural``, the report document
parser, ``check_bindings`` and ``render_html``'s value popover; only the sandbox's tool process is
a fake.
"""

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.provenance import ReceiptStore
from auto_research.provenance.refs import argument_source, argument_value, call_arguments
from auto_research.sandbox.registry import ToolkitRegistry, check_toolkit_manifests, provider_schema
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

FIT = "research_toolkit.tk.fit"
REDUCE = "research_toolkit.tk.reduce"
SCHEMA = {
    "properties": {
        "bins": {"kind": "data", "type": "array"},
        "cl": {"kind": "parameter", "type": "number", "default": 0.68},
        "grid": {"kind": "parameter", "type": "array", "items": {"type": "number"}},
        "infile": {"kind": "path", "type": "string"},
        "label": {"kind": "setting", "type": "string"},
    },
    "required": ["grid"],
}


@pytest.fixture(autouse=True)
def _pinned(monkeypatch, tmp_path):
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    monkeypatch.setattr(react, "_observation_path_args", lambda: {})


def _tool(tmp_path):
    def run(module, fn, args):
        if "grid" not in args:  # the reducer: writes a file and returns its numbers
            out = tmp_path / "spec.pha"
            out.write_text("PHA 1 2 3\n")
            payload = {"bins": [{"q": 0.0215103980}], "file": str(out)}
            return {"exit_code": 0, "stdout": json.dumps(payload), **payload}
        payload = {"received": args}
        return {"exit_code": 0, "stdout": json.dumps(payload, default=str), **payload}
    return run


def _provider(tmp_path, store, *, source="analyst_worker_0", schema=SCHEMA):
    registry = _FakeRegistry(known={FIT, REDUCE}, mcp=set(), schema=schema)
    return SandboxToolProvider(registry, _FakeSandbox(tool_fn=_tool(tmp_path)), cwd=tmp_path / source,
                               receipts=store, receipt_source=source)


def _run(tools, kind, args):
    return asyncio.run(tools.execute(kind, args))


# -- the registry --------------------------------------------------------------------------------


def _toolkit(root: Path, properties: dict, required=()) -> Path:
    pkg = root / "demo_utils"
    (pkg / "skills").mkdir(parents=True)
    manifest = {"research_toolkit.demo_utils.fit": {
        "module": "demo_utils.lib", "fn": "fit", "description": "Fit a demo model.",
        "args_schema": {"type": "object", "properties": properties, "required": list(required)}}}
    (pkg / "skills" / "__init__.py").write_text(f"TOOL_MANIFEST = {manifest!r}\n")
    (pkg / "__init__.py").write_text("from demo_utils.skills import TOOL_MANIFEST\n")
    return root


@pytest.fixture
def _fresh_demo_modules():
    yield
    for name in [m for m in sys.modules if m.split(".")[0] == "demo_utils"]:
        del sys.modules[name]


def test_the_real_toolkit_declares_every_kind():
    check_toolkit_manifests()  # the configured research_toolkit: raises on any undeclared kind


@pytest.mark.parametrize("prop, required, said", [
    ({"x": {"type": "number"}}, (), "argument x declares no kind"),
    ({"x": {"kind": "number"}}, (), "declares kind 'number'"),
    ({"x": {"kind": "parameter"}}, (), "optional parameter x declares no default"),
])
def test_launch_refuses_a_manifest_the_validator_cannot_check(tmp_path, prop, required, said, _fresh_demo_modules):
    root = _toolkit(tmp_path / "tk", prop, required)
    registry = ToolkitRegistry(root)
    registry.load()
    assert any(said in e for e in registry.manifest_errors), registry.manifest_errors
    with pytest.raises(RuntimeError, match="input validator cannot check"):
        check_toolkit_manifests(root)


def test_a_required_parameter_needs_no_default(tmp_path, _fresh_demo_modules):
    check_toolkit_manifests(_toolkit(tmp_path / "tk", {"x": {"kind": "parameter"}}, ["x"]))


def test_the_provider_schema_states_each_kind_and_carries_no_runtime_key(tmp_path, _fresh_demo_modules):
    root = _toolkit(tmp_path / "tk", SCHEMA["properties"], SCHEMA["required"])
    registry = ToolkitRegistry(root)
    [definition] = registry.tool_definitions()
    props = definition["input_schema"]["properties"]
    assert all("kind" not in p and "default" not in p for p in props.values())
    assert "type" not in props["bins"] and '{"$ref": "<handle>#/<key>"}' in props["bins"]["description"]
    assert props["cl"]["properties"]["value"] == {"type": "number"}
    assert "leave it out for the default, 0.68" in props["cl"]["description"]
    assert "leave it out" not in props["grid"]["description"]
    assert "type" not in props["infile"] and "a file a tool of yours wrote" in props["infile"]["description"]
    assert props["label"] == {"type": "string"}
    assert '"reason": "<why this value>"' in definition["description"]
    assert provider_schema(None) == {"type": "object", "properties": {}}
    # A default on any kind is said, never dropped silently; a typed-only path offers no reference.
    other = provider_schema({"properties": {"mode": {"kind": "setting", "type": "string", "default": "fast"},
                                            "where": {"kind": "path", "type": "string"}}}, references=False)
    assert other["properties"]["mode"] == {"type": "string", "description": 'Default: "fast".'}
    assert other["properties"]["where"] == {"type": "string", "description": "A path as text."}


@pytest.mark.parametrize("schema, said", [
    ({"properties": ["x"]}, "args_schema.properties is a list"),
    ("not a schema", "args_schema is a str"),
])
def test_a_malformed_manifest_is_a_problem_never_a_crash(tmp_path, schema, said, _fresh_demo_modules):
    from auto_research.provenance.inputs import manifest_problems

    assert any(said in p for p in manifest_problems("t", schema))
    pkg = tmp_path / "tk" / "demo_utils"
    (pkg / "skills").mkdir(parents=True)
    manifest = {"research_toolkit.demo_utils.fit": {"module": "m", "fn": "f", "description": "d",
                                                     "args_schema": schema}}
    (pkg / "skills" / "__init__.py").write_text(f"TOOL_MANIFEST = {manifest!r}\n")
    (pkg / "__init__.py").write_text("from demo_utils.skills import TOOL_MANIFEST\n")
    registry = ToolkitRegistry(tmp_path / "tk")
    registry.load()  # records, never raises
    assert any(said in e for e in registry.manifest_errors)


def test_a_disabled_tool_never_blocks_launch(tmp_path, monkeypatch, _fresh_demo_modules):
    import auto_research.graph.runtime as runtime
    import auto_research.sandbox.merge_flow as merge_flow

    root = _toolkit(tmp_path / "tk", {"x": {"type": "number"}})
    monkeypatch.setattr(merge_flow, "_resolve_toolkit_root", lambda: root)
    monkeypatch.setattr(runtime, "load_orchestration_config",
                        lambda path=None: {"disabled_tools": ["research_toolkit.demo_utils.fit"]})
    check_toolkit_manifests()


def test_the_leads_catalog_definition_carries_no_runtime_key_and_offers_no_reference():
    defn = react.data_catalog_tool_def()
    if defn is None:
        pytest.skip("catalog_utils is not importable from the configured toolkit")
    props = defn["input_schema"]["properties"]
    assert all("kind" not in p and "default" not in p for p in props.values())
    assert "$ref" not in json.dumps(defn)


# -- the call path -------------------------------------------------------------------------------


def test_each_kind_arrives_its_way_and_the_tool_receives_plain_values(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    _run(tools, REDUCE, {"label": "soft"})  # analyst_worker_0:r1 and its file d1
    out = _run(tools, FIT, {"bins": {"$ref": "analyst_worker_0:r1#/bins"},
                            "grid": {"value": [2.0, 4.0, 8.0], "reason": "the brief's three bands"},
                            "infile": {"$ref": "analyst_worker_0:d1"}, "label": "soft"})
    assert not out.is_error, out.text
    received = tools.sandbox.tool_calls[-1][2]
    assert received == {"bins": [{"q": 0.0215103980}], "grid": [2.0, 4.0, 8.0], "cl": 0.68,
                        "infile": str(tmp_path / "spec.pha"), "label": "soft"}
    fit = store.get("analyst_worker_0:r2")
    assert fit.parents == ["analyst_worker_0:r1", "analyst_worker_0:d1"]
    # The call's record keeps the reason, the references and the default the runtime passed.
    recorded = call_arguments(store, fit.handle)
    assert recorded["grid"] == {"value": [2.0, 4.0, 8.0], "reason": "the brief's three bands"}
    assert recorded["cl"] == 0.68 and recorded["bins"] == {"$ref": "analyst_worker_0:r1#/bins"}


def test_a_call_breaking_a_kind_never_runs_and_says_what_to_write(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    _run(tools, REDUCE, {})
    for args, said in (
        ({"bins": [{"q": 0.0215103980}], "grid": {"value": [2.0], "reason": "r"}}, "argument bins is a tool's output"),
        ({"grid": [2.0, 4.0]}, '{"value": <the value>, "reason": "<why this value>"}'),
        ({"grid": {"value": [2.0], "reason": ""}}, "argument grid is a parameter"),
        ({"grid": {"value": {"$ref": "analyst_worker_0:r1#/bins"}, "reason": "r"}}, "argument grid is a parameter"),
        ({"cl": 0.9, "grid": {"value": [2.0], "reason": "r"}}, "argument cl is a parameter"),
        ({"cl": None, "grid": {"value": [2.0], "reason": "r"}}, "argument cl is a parameter"),  # null is no default
        ({"norm": 0.379, "grid": {"value": [2.0], "reason": "r"}}, "argument norm is not one this tool takes"),
        ({"label": {"value": "soft", "reason": "r"}, "grid": {"value": [2.0], "reason": "r"}},
         "argument label is a setting, which takes no reason"),
    ):
        out = _run(tools, FIT, args)
        assert out.is_error and said in out.text, (args, out.text)
    assert len(tools.sandbox.tool_calls) == 1


def test_a_tool_whose_manifest_declares_no_kinds_cannot_run(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store, schema={"properties": {"grid": {"type": "array"}}})
    out = _run(tools, FIT, {"grid": [2.0]})
    assert out.is_error and "argument grid declares no kind" in out.text and "tool_request" in out.text
    assert tools.sandbox.tool_calls == []


def test_a_file_passes_as_its_path_only_while_it_matches_and_only_its_writers(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    _run(tools, REDUCE, {})  # d1: spec.pha
    peer = _provider(tmp_path, store, source="analyst_worker_1")
    out = _run(peer, FIT, {"infile": {"$ref": "analyst_worker_0:d1"}, "grid": {"value": [2.0], "reason": "r"}})
    assert out.is_error and "a file analyst_worker_0's tool wrote" in out.text and "reproduce" in out.text
    out = _run(tools, FIT, {"infile": {"$ref": "analyst_worker_0:d1#/x"}, "grid": {"value": [2.0], "reason": "r"}})
    assert out.is_error and "passes as its path" in out.text
    (tmp_path / "spec.pha").write_text("PHA rewritten by a later call\n")
    out = _run(tools, FIT, {"infile": {"$ref": "analyst_worker_0:d1"}, "grid": {"value": [2.0], "reason": "r"}})
    assert out.is_error and "no longer matches its receipt" in out.text
    # A typed path runs (ruling 4: paths stay typeable).
    out = _run(tools, FIT, {"infile": str(tmp_path / "spec.pha"), "grid": {"value": [2.0], "reason": "r"}})
    assert not out.is_error, out.text


def test_a_chosen_value_lives_behind_its_calls_handle(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    _run(tools, FIT, {"grid": {"value": [2.0, 8.0], "reason": "the full IXPE band"}})
    assert argument_value(store, "analyst_worker_0:r1", "/$args/grid/1") == 8.0
    assert argument_source(store, "analyst_worker_0:r1", "/$args/grid") == "chosen by analyst_worker_0: the full IXPE band"
    # A later call of the same unit passes its own choice on by reference: one source of truth.
    out = _run(tools, FIT, {"grid": {"$ref": "analyst_worker_0:r1#/$args/grid"}})
    assert not out.is_error and tools.sandbox.tool_calls[-1][2]["grid"] == [2.0, 8.0]
    out = _run(tools, FIT, {"grid": {"$ref": "analyst_worker_0:r1#/$args/nope"}})
    assert out.is_error and "took no argument 'nope'" in out.text and "grid" in out.text


def test_the_leads_catalog_call_passes_the_same_validator(tmp_path, monkeypatch):
    calls = []
    spec = {"args_schema": {"properties": {"obs_db_path": {"kind": "path"}, "min_level": {"kind": "setting"},
                                           "cut": {"kind": "parameter"}}, "required": ["cut"]}}
    monkeypatch.setattr(react, "_toolkit_clean_head", lambda: "head")
    monkeypatch.setattr(react, "_catalog_read_fn", ("head", lambda p, min_level=None: calls.append(p) or "ok", spec))
    out = asyncio.run(react.execute_data_catalog({"obs_db_path": "/x", "cut": 2.0}))
    assert out.is_error and "argument cut is a parameter" in out.text and calls == []
    out = asyncio.run(react.execute_data_catalog({"obs_db_path": "/x", "cut": {"value": 2.0, "reason": "r"}}))
    assert not out.is_error and calls == ["/x"]


# -- the report side -----------------------------------------------------------------------------


def test_a_report_writes_a_chosen_value_as_its_calls_argument(tmp_path):
    from auto_research.provenance import check_bindings
    from auto_research.provenance.bindings import render_values

    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    _run(tools, FIT, {"grid": {"value": [2.0, 8.0], "reason": "the full band"},
                      "cl": {"value": 0.99, "reason": "the brief's 99% limit"}})
    text = "The scan used a {{analyst_worker_0:r1#/$args/cl}} confidence level."
    result = check_bindings(text, store, own_source="analyst_worker_0")
    assert not result.problems, result.problems
    assert render_values(text, store) == "The scan used a 0.990 [analyst_worker_0:r1#/$args/cl] confidence level."
    whole = check_bindings("Bands {{analyst_worker_0:r1#/$args/grid}}.", store, own_source="analyst_worker_0")
    assert any("not one value" in p for p in whole.problems)


# -- the gate ------------------------------------------------------------------------------------


def test_the_gate_fails_a_tool_whose_argument_declares_no_kind(tmp_path):
    from tests.test_gate import _make_repo, _spec, collect_targets
    from auto_research.sandbox.gate import check_structural

    repo = _make_repo(tmp_path)
    pkg = repo / "demo_utils"
    for sub in ("lib", "skills"):
        (pkg / sub).mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "lib" / "demo_help.py").write_text("def my_tool(x):\n    return {'x': x}\n")
    (pkg / "lib" / "test_demo.py").write_text("def test_my_tool():\n    assert True\n")

    def manifest(prop):
        (pkg / "skills" / "__init__.py").write_text(textwrap.dedent(f'''\
            TOOL_MANIFEST = {{"research_toolkit.demo_utils.my_tool": {{
                "module": "demo_utils.lib.demo_help", "fn": "my_tool", "description": "d",
                "args_schema": {{"properties": {{"x": {prop!r}}}}}}}}}
            '''))
        return {c.name: c for c in check_structural(repo, _spec(), collect_targets(repo))}["structural.argument_kinds"]

    missing = manifest({"type": "number"})
    assert not missing.passed and "argument x declares no kind" in missing.detail
    assert manifest({"kind": "parameter", "default": 1.0}).passed


def test_data_takes_only_what_a_tool_returned_and_args_reach_only_chosen_or_passed_values(tmp_path):
    """Review 2026-11-03: a call's typed setting reached data arguments and report numbers by
    reference; it carries no tool output and is refused, and a brief is never a source."""
    from auto_research.provenance import check_bindings
    store = ReceiptStore(tmp_path / "receipts")
    brief = store.mint_input("Fit at q = 0.5.")
    tools = _provider(tmp_path, store)
    _run(tools, FIT, {"grid": {"value": [2.0], "reason": "the band"}, "label": "[0.37959418926306543]"})
    grid = {"grid": {"value": [2.0], "reason": "r"}}
    out = _run(tools, FIT, {"bins": {"$ref": "analyst_worker_0:r1#/$args/grid"}, **grid})
    assert out.is_error and "argument bins is a tool's output" in out.text
    out = _run(tools, FIT, {"bins": {"$ref": f"{brief.handle}#"}, **grid})
    assert out.is_error and "comes from the tool that produces it" in out.text  # a brief is never a source
    out = _run(tools, FIT, {"grid": {"$ref": "analyst_worker_0:r1#/$args/label"}})
    assert out.is_error and "typed with no reason and no reference" in out.text
    out = _run(tools, FIT, {"grid": {"$ref": "analyst_worker_0:r1#/$args/cl"}})  # the default passed
    assert out.is_error and "typed with no reason and no reference" in out.text
    problems = check_bindings("Value {{analyst_worker_0:r1#/$args/label}}.", store,
                              own_source="analyst_worker_0").problems
    assert any("typed with no reason" in p for p in problems), problems


def test_a_file_referenced_twice_is_read_once_while_it_stays_as_it_was(tmp_path, monkeypatch):
    import auto_research.provenance.store as store_module

    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)
    _run(tools, REDUCE, {})
    reads = []
    real = store_module.sha256_file
    monkeypatch.setattr(store_module, "sha256_file", lambda path: reads.append(path) or real(path))
    def call():
        out = _run(tools, FIT, {"infile": {"$ref": "analyst_worker_0:d1"}, "grid": {"value": [2.0], "reason": "r"}})
        assert not out.is_error, out.text
        return [p for p in reads if Path(p) == tmp_path / "spec.pha"]

    first = len(call())
    call(), call()
    assert len(call()) == first  # later references read nothing more while the file stays as it was


def test_a_chosen_value_is_stated_through_its_calls_argument_never_the_tools_echo(tmp_path):
    """User ruling 2026-11-03: a tool echoing a value its call was given is no source for it; the
    report writes `{{h#/$args/<name>}}`, the one form that carries who chose it and why."""
    from auto_research.provenance import check_bindings

    store = ReceiptStore(tmp_path / "receipts")
    tools = _provider(tmp_path, store)  # FIT echoes what it received under "received"
    _run(tools, FIT, {"grid": {"value": [2.0, 8.0], "reason": "the band"},
                      "cl": {"value": 0.9973, "reason": "3 sigma"}})
    echo = check_bindings("At {{analyst_worker_0:r1#/received/cl}} confidence.", store, own_source="analyst_worker_0")
    assert any("{{analyst_worker_0:r1#/$args/cl}}" in p and "echo" in p for p in echo.problems), echo.problems
    typed = check_bindings("At 0.9973 confidence {{analyst_worker_0:r1}}.", store, own_source="analyst_worker_0")
    assert any("{{analyst_worker_0:r1#/$args/cl}}" in p for p in typed.problems), typed.problems
    assert not check_bindings("At {{analyst_worker_0:r1#/$args/cl}} confidence.", store,
                              own_source="analyst_worker_0").problems
