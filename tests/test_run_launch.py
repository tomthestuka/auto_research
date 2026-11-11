"""``graph.runtime.RunLaunch``: the run's launch step and its end, one seam for ``run_research`` and
for cells that drive roles outside the graph (``tests/live_store.run_stores``)."""

from __future__ import annotations

import asyncio

import pytest

from auto_research.graph import runtime
from auto_research.run_control import RunControl
from auto_research.store import keys
from tests.test_store_servers import SECRETS, _no_dotenv, make_store  # noqa: F401 - fixtures

needs_servers = pytest.mark.skipif(
    not all(__import__("shutil").which(tool) for tool in ("versitygw", "initdb", "pg_ctl")),
    reason="versitygw and Postgres are not installed here")

RUN = "run-launch"


def _declare_store(make_store, tmp_path):
    """A throwaway store declared beside the fixture toolkit (the reconciler judges products against
    the declared toolkits), its buckets made, as the migration makes the real one's."""
    from tests.fixtures import toolkit as fixture

    root = fixture.build_toolkit(tmp_path / "toolkit", env_base="astropy")
    declared = {"name": fixture.TOOLKIT_NAME, "root": str(root), "package_glob": "*_utils",
                "template_package": fixture.PKG_NAME, "authoring_branch": fixture.BRANCH,
                "default_env": fixture.ENV_ID}
    made = make_store(extra_config={"toolkits": [declared]}, roles={"producer": ["analyst_worker_*"]})
    from auto_research.store import launch

    launch.start(made)
    s3 = made.admin_client("s3")
    for bucket in made.buckets:
        s3.create_bucket(Bucket=bucket)
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    return made


@needs_servers
def test_a_launch_binds_starts_holds_and_ends(make_store, monkeypatch, tmp_path):
    """Entered: the run's process is recorded, the contract and the stores are bound to the run, the
    store is reconciled under the run's reconciler login, and ``hold`` gives the stop channel the
    keys. Left: every key revoked, contract and stores released."""
    from auto_research.monitor import RunMonitor

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    _declare_store(make_store, tmp_path)
    monitor = RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
    control = RunControl()
    seen: dict = {}

    async def go():
        async with runtime.RunLaunch(RUN, monitor) as launch:
            launch.hold(control)
            seen["process"] = runtime.run_process_record(RUN)
            seen["held"] = [(c.unit, c.role_class) for c in launch.keys.held()]
            seen["stores_bound"] = RUN in runtime._STORE_RUNS
            seen["contract_bound"] = RUN in runtime._CONTRACT_RUNS
            seen["credentials"] = control.credentials is launch.keys
            seen["keys"] = launch.keys

    asyncio.run(go())
    assert seen["process"] and seen["process"]["pid"]
    assert seen["held"] == [(runtime.RECONCILER_UNIT, "reconciler")]
    assert seen["stores_bound"] and seen["contract_bound"] and seen["credentials"]
    assert not seen["keys"].pending()
    assert RUN not in runtime._STORE_RUNS and RUN not in runtime._CONTRACT_RUNS


def test_a_failed_start_ends_the_launch_at_once(monkeypatch, tmp_path):
    """A start that raises (a store not ready) revokes what was issued and releases the contract and
    the stores before the error leaves the block; the block's body never runs."""
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    revoked: list[str] = []

    async def failing_start(run_id, held, monitor):
        assert RUN in runtime._STORE_RUNS  # bound before the store is started
        raise RuntimeError("store db is not ready: store_servers: no answer")

    async def revoke_all(self):
        revoked.append(self.run)

    monkeypatch.setattr(runtime, "start_run_stores", failing_start)
    monkeypatch.setattr(keys.RunKeys, "revoke_all", revoke_all)
    body: list[bool] = []

    async def go():
        async with runtime.RunLaunch(RUN, None):
            body.append(True)

    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(go())
    assert body == [] and revoked == [RUN]
    assert RUN not in runtime._STORE_RUNS and RUN not in runtime._CONTRACT_RUNS


def test_run_research_takes_its_launch_through_the_seam(monkeypatch, tmp_path):
    """``run_research`` starts its launch with ``RunLaunch.start`` and, when the preflight refuses,
    ends it (keys revoked, contract and stores released) before the refusal leaves the run."""
    from auto_research import preflight

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls: list[str] = []
    bound: list[bool] = []

    async def started(run_id, held, monitor):  # the store servers are not under test here
        bound.append(RUN in runtime._STORE_RUNS and RUN in runtime._CONTRACT_RUNS)

    class Recorded(runtime.RunLaunch):
        async def start(self):
            calls.append("start")
            return await super().start()

        async def end(self):
            calls.append("end")
            await super().end()

    monkeypatch.setattr(runtime, "start_run_stores", started)

    async def refuse(**_):
        calls.append("preflight")
        raise preflight.PreflightError("refused for the test")

    monkeypatch.setattr(runtime, "RunLaunch", Recorded)
    monkeypatch.setattr(runtime, "run_preflight", refuse)
    with pytest.raises(preflight.PreflightError, match="refused for the test"):
        asyncio.run(runtime.run_research("a question", run_id=RUN, human_channel="none"))
    assert calls == ["start", "preflight", "end"]
    assert bound == [True], "the launch did not bind the run's contract and stores before starting its stores"
    assert RUN not in runtime._STORE_RUNS and RUN not in runtime._CONTRACT_RUNS


def test_the_live_cells_take_their_launch_through_the_seam():
    """``tests/live_store.run_stores`` calls ``RunLaunch`` and restates none of its steps (user rule
    2026-10-28: tests use module endpoints, never a hand-rolled equivalent)."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).with_name("live_store.py")).read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_stores")
    called = {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
              for c in ast.walk(node) if isinstance(c, ast.Call)}
    assert "RunLaunch" in called
    restated = called & {"record_run_process", "load_orchestration_config", "bind_run_contract", "bind_run_stores",
                         "start_run_stores", "revoke_all", "release_run_contract", "release_run_stores"}
    assert not restated, f"run_stores restates the launch: {sorted(restated)}"


@needs_servers
def test_a_store_that_cannot_serve_is_store_not_ready_and_a_reconciler_fault_is_itself(make_store, monkeypatch,
                                                                                       tmp_path):
    """``start_run_stores`` raises ``StoreNotReady`` only for servers that do not answer (the
    preflight's ``store_servers`` words). A running store without its buckets is a plain
    ``RuntimeError`` (``store_buckets``), and so is a fault of the launch step's own code (the
    reconciler), which comes through as itself; neither is ever a store refusal."""
    import yaml

    from auto_research.config_paths import orchestration_yaml
    from auto_research.monitor import RunMonitor
    from auto_research.store import launch, reconcile
    from tests.fixtures import toolkit as fixture

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    root = fixture.build_toolkit(tmp_path / "toolkit", env_base="astropy")
    declared = {"name": fixture.TOOLKIT_NAME, "root": str(root), "package_glob": "*_utils",
                "template_package": fixture.PKG_NAME, "authoring_branch": fixture.BRANCH,
                "default_env": fixture.ENV_ID}
    made = make_store(extra_config={"toolkits": [declared]}, roles={"producer": ["analyst_worker_*"]})
    orch = yaml.safe_load(orchestration_yaml().read_text())
    monitor = RunMonitor(jsonl_path=tmp_path / "ledger" / "monitor.jsonl")
    held = keys.RunKeys(RUN)
    runtime.bind_run_stores(orch, RUN)
    try:
        with pytest.raises(RuntimeError, match="store db is not ready: store_buckets") as raised:
            asyncio.run(runtime.start_run_stores(RUN, held, monitor))
        assert not isinstance(raised.value, runtime.StoreNotReady)
        real_down = launch.down
        monkeypatch.setattr(launch, "down", lambda store: ["gateway"])  # the S3 gateway stopped answering
        with pytest.raises(runtime.StoreNotReady, match="answer.*gateway"):  # launch.start or store_servers
            asyncio.run(runtime.start_run_stores(RUN, held, monitor))
        monkeypatch.setattr(launch, "down", real_down)
        s3 = made.admin_client("s3")
        for bucket in made.buckets:
            s3.create_bucket(Bucket=bucket)
            s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

        async def broken(*args, **kwargs):
            raise TypeError("a reconciler fault")

        monkeypatch.setattr(reconcile, "reconcile", broken)
        with pytest.raises(TypeError, match="a reconciler fault") as raised:
            asyncio.run(runtime.start_run_stores(RUN, held, monitor))
        assert not isinstance(raised.value, runtime.StoreNotReady)
    finally:
        asyncio.run(held.revoke_all())
        runtime.release_run_stores(RUN)
        launch.stop(made)


def _must_not_skip(fn, *args):
    """``fn(*args)``, with a skip it raises turned into a failure: a skip escaping a ``pytest.raises``
    would read as a skipped test, hiding the property it breaks."""
    try:
        return fn(*args)
    except pytest.skip.Exception as exc:
        pytest.fail(f"skipped where the cell must fail or run: {exc}")


def test_the_live_cells_skip_on_a_store_that_is_not_ready_and_fail_on_anything_else(monkeypatch, tmp_path):
    """``tests/live_store`` (main's rulings): a store absent from this host (admin variables unset,
    its root missing) and servers that do not start or answer (``StoreNotReady``) skip the cell with
    the store's own message before the body runs. A configuration defect (``health.config_defects``,
    checked first, even for an absent store; a block that does not parse), any other failure of the
    launch, and any failure of the body fail it."""
    from types import SimpleNamespace

    from auto_research.store import config as store_config
    from auto_research.store import health
    from tests import live_store

    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    body: list[bool] = []

    async def cell(fail_in_body=False):
        async with live_store.run_stores(RUN, None):
            body.append(True)
            if fail_in_body:
                raise runtime.StoreNotReady("raised by the body, not by the launch")

    def unset():
        raise RuntimeError("set AR_DB_S3_KEY_ID in .env (the store's admin secrets)")

    absent = SimpleNamespace(name="db", admin_env=SimpleNamespace(values=unset))
    present = SimpleNamespace(name="db", admin_env=SimpleNamespace(values=lambda: {}))
    real_stores = store_config.stores

    def declared(*stores):  # what the store checks read; a run still binds the configuration's
        return lambda config=None: list(stores) if config is None else real_stores(config)

    def declare(*stores):  # store.config's reader and the one health imported
        monkeypatch.setattr(store_config, "stores", declared(*stores))
        monkeypatch.setattr(health, "stores", declared(*stores))

    # Absent: skipped, with the store's own words.
    declare(absent)
    with pytest.raises(pytest.skip.Exception, match="absent from this host.*AR_DB_S3_KEY_ID"):
        asyncio.run(cell())
    with pytest.raises(pytest.skip.Exception, match="absent from this host"):
        live_store.require_store()
    missing = store_config.LocalStore(
        name="db", kind="local", root=tmp_path / "no_root", state_dir=tmp_path / "no_state",
        socket_dir=tmp_path / "s", ports={"s3": 18970, "iam": 18976, "catalog": 18932}, buckets=["ixpe"],
        admin_env={"s3_access_key": "T_S3_KEY", "s3_secret_key": "T_S3_SECRET", "catalog_password": "T_PG_PASSWORD"})
    declare(missing)
    with pytest.raises(pytest.skip.Exception, match="root .*no_root does not exist"):
        live_store.require_store()

    # A defect fails, even when the store is absent too: the real check, no default store declared.
    other = SimpleNamespace(name="elsewhere", admin_env=SimpleNamespace(values=unset))
    declare(other)
    with pytest.raises(pytest.fail.Exception, match="defective.*no store is named 'db'"):
        _must_not_skip(asyncio.run, cell())
    with pytest.raises(pytest.fail.Exception, match="defective.*no store is named 'db'"):
        _must_not_skip(live_store.require_store)

    def unparsable(config=None):
        raise ValueError("stores: entry 1 lacks 'kind'")

    monkeypatch.setattr(store_config, "stores", unparsable)
    monkeypatch.setattr(health, "stores", unparsable)
    with pytest.raises(ValueError, match="lacks 'kind'"):
        _must_not_skip(live_store.require_store)

    # Present and well-formed from here on; store_config itself is not under test below.
    declare(present)
    monkeypatch.setattr(health, "check_store_config", lambda: None)

    async def not_ready(run_id, held, monitor):
        raise runtime.StoreNotReady("store db is not ready: store_servers: not answering: gateway")

    monkeypatch.setattr(runtime, "start_run_stores", not_ready)
    with pytest.raises(pytest.skip.Exception, match="launch step.*store_servers: not answering"):
        asyncio.run(cell())
    with pytest.raises(pytest.skip.Exception, match="launch step.*store_servers: not answering"):
        live_store.require_store()
    assert body == []

    async def faulty(run_id, held, monitor):  # a running store whose catalog has another schema
        raise RuntimeError("store db is not ready: store_catalog: schema version 3, this code reads 4")

    monkeypatch.setattr(runtime, "start_run_stores", faulty)
    with pytest.raises(RuntimeError, match="store_catalog"):
        _must_not_skip(asyncio.run, cell())
    with pytest.raises(RuntimeError, match="store_catalog"):
        _must_not_skip(live_store.require_store)

    async def ready(run_id, held, monitor):
        return None

    monkeypatch.setattr(runtime, "start_run_stores", ready)
    with pytest.raises(runtime.StoreNotReady, match="raised by the body"):
        _must_not_skip(asyncio.run, cell(fail_in_body=True))
    assert body == [True]
    _must_not_skip(live_store.require_store)  # ready: no skip
    assert RUN not in runtime._STORE_RUNS and RUN not in runtime._CONTRACT_RUNS


def test_wire_lead_is_run_researchs_lead_wiring(monkeypatch, tmp_path):
    """``wire_lead`` issues the lead's own login into the run's keys and hands it to
    ``attach_lead_runtime`` with production's web source and retrieval; ``run_research`` wires its
    lead through ``wire_lead`` only."""
    import ast
    from pathlib import Path

    from auto_research.agents.react import CATALOG_QUERY_TOOL
    from auto_research.retrieval import SqliteRetrievalBackend
    from auto_research.sources import WebSource

    class _Login:
        store = "db"

    issued: list[tuple] = []

    async def issue(keys, units, monitor):
        issued.append((keys, tuple(units)))
        return {units[0]: _Login()}

    monkeypatch.setattr(runtime, "issue_unit_keys", issue)
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(tmp_path / "runtime"))

    class _Lead:
        tools = None

    lead, held = _Lead(), object()
    asyncio.run(runtime.wire_lead(lead, run_id="r1", toolkit_root=tmp_path, keys=held))
    assert issued == [(held, (runtime.LEAD_KEY,))]
    assert lead.agent_key == runtime.LEAD_KEY
    assert isinstance(lead.tools.catalog["db"], _Login)
    assert isinstance(lead.tools.web_source, WebSource)
    assert isinstance(lead.tools.retrieval, SqliteRetrievalBackend)
    assert CATALOG_QUERY_TOOL in {d["name"] for d in lead.tools.tool_definitions()}

    module = ast.parse(Path(runtime.__file__).read_text(encoding="utf-8"))
    tree = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_research")
    called = {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
              for c in ast.walk(tree) if isinstance(c, ast.Call)}
    assert "wire_lead" in called and "attach_lead_runtime" not in called


#: Live modules that build an `AnalystLeadAgent` for something other than a lead's turn, with why:
#: they drive `_react` itself over a tool provider of their own, or only read the built prompt.
_LEAD_SHELLS = {
    "test_live_read_file.py": "drives the lead's `_react` paging over a recording read provider",
    "test_live_channel.py": "the quick channel's ping matrix: every seat on one channel with ping tools",
    "test_live_pressure_cells.py:test_the_pressure_reaches_the_lead_and_the_coder_only":
        "reads the built prompt; no model is called",
}
_SEAM_FIELDS = {"agent_key", "report_dir", "tools", "receipts", "receipt_source", "monitor", "run_control"}


def test_every_live_lead_is_wired_by_wire_lead():
    """Main's ruling (S13a): a live cell that drives a real analyst lead gets exactly run_research's
    lead wiring, through `graph.runtime.wire_lead`, and restates none of it (no `attach_lead_runtime`,
    `readonly_tools_for` or `issue_unit_keys` of its own, no seam-owned field set on the lead)."""
    import ast
    from pathlib import Path

    problems: list[str] = []
    seen = 0
    for path in sorted(Path(__file__).parent.glob("test_live_*.py")):
        if path.name in _LEAD_SHELLS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or \
                    f"{path.name}:{fn.name}" in _LEAD_SHELLS:
                continue
            calls = {c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
                     for c in ast.walk(fn) if isinstance(c, ast.Call)}
            if "AnalystLeadAgent" not in calls:
                continue
            seen += 1
            where = f"{path.name}:{fn.name}"
            if "wire_lead" not in calls:
                problems.append(f"{where} builds a lead and never calls wire_lead")
            problems += [f"{where} calls {name}" for name in
                         sorted(calls & {"attach_lead_runtime", "readonly_tools_for", "issue_unit_keys"})]
            for node in ast.walk(fn):
                targets = node.targets if isinstance(node, ast.Assign) else \
                    [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
                problems += [f"{where} sets lead.{t.attr} (line {node.lineno})" for t in targets
                             if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                             and t.value.id == "lead" and t.attr in _SEAM_FIELDS]
    assert seen >= 9, f"the guard found only {seen} lead builders: it no longer reads the live cells"
    assert not problems, problems
