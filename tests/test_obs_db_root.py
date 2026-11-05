"""H2: a fresh, empty obs-db root.

The runtime fills a toolkit argument named ``obs_db_root`` with the configured root (as it fills
``<mission>_path`` from the mission folders that exist), so ``acquire`` works on an empty root;
``data_catalog`` and the analysts' task say the root and that ``acquire`` creates a mission's
folder on its first observation; the catalog health check passes an empty root that exists and
is writable. Driven through the worker's call path (``SandboxToolProvider.execute``), the lead's
``data_catalog`` (``readonly_tools_for``), the graph's task builder and the preflight check, over a temporary
``observations.yaml`` (the config's location is the only thing pointed elsewhere).
"""

from __future__ import annotations

import asyncio
import os

import pytest
import yaml

from auto_research.agents.react import DATA_CATALOG_TOOL, SandboxToolProvider
from auto_research.observations import check_catalog_health, load_observations_config
from tests.test_observations import TOOLKIT_ROOT, _needs_obsdb
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

ACQUIRE = "research_toolkit.heasarc_utils.acquire"
LOAD = "research_toolkit.xspec_utils.load_data"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """An empty obs-db root, configured as the run's (observations.yaml in a temp place)."""
    from auto_research.sandbox import fs_guard

    obsdb = tmp_path / "obsdb"
    obsdb.mkdir()
    config = tmp_path / "observations.yaml"
    config.write_text(yaml.safe_dump({"obs_db_root": str(obsdb)}))
    monkeypatch.setattr("auto_research.observations.observations_yaml", lambda: config)
    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    return obsdb


class _Registry(_FakeRegistry):
    def spec(self, k):
        schemas = {
            ACQUIRE: {"properties": {"obs_db_root": {"kind": "path"}, "obsid": {"kind": "setting"}},
                      "required": ["obsid"]},
            LOAD: {"properties": {"ixpe_path": {"kind": "path"}, "obsid": {"kind": "setting"}},
                   "required": ["obsid"]},
        }
        return {"module": "tk", "fn": k.rsplit(".", 1)[1], "args_schema": schemas[k]}


def _provider(tmp_path, seen, receipts=None):
    def tool(module, fn, args):
        seen.append((fn, dict(args)))
        if fn == "acquire":  # what acquire does on its first observation: the mission's folder
            mission = os.path.join(args["obs_db_root"], "IXPE")
            os.makedirs(mission, exist_ok=True)
            with open(os.path.join(mission, "catalog.json"), "w") as handle:
                handle.write('{"entries": {}}')
        return {"exit_code": 0, "stdout": "ok"}

    return SandboxToolProvider(_Registry(known={ACQUIRE, LOAD}, mcp=set()),
                               _FakeSandbox(tool_fn=tool), cwd=tmp_path / "w0",
                               receipts=receipts, receipt_source="analyst_worker_0" if receipts else None)


def test_acquire_gets_the_root_and_the_mission_path_follows(root, tmp_path):
    """The model leaves obs_db_root out; acquire receives the configured root and creates IXPE.
    A loader's ixpe_path then resolves to the new folder (case-insensitive basename)."""
    from auto_research.provenance import ReceiptStore

    seen: list = []
    store = ReceiptStore(tmp_path / "receipts")
    provider = _provider(tmp_path, seen, receipts=store)
    (root / "01002901").mkdir()  # a stray folder at the root: the root is no mission folder
    out = asyncio.run(provider.execute(ACQUIRE, {"obsid": "01002901"}))
    assert not out.is_error, out.text
    assert seen[-1] == ("acquire", {"obs_db_root": str(root), "obsid": "01002901"})
    assert store.receipts(kind="result")[-1].inputs == []  # nothing hashed at the root, no ObsID looked up there
    assert asyncio.run(provider.execute(LOAD, {"obsid": "01002901"})).is_error is False
    assert seen[-1][1]["ixpe_path"] == str(root / "IXPE")


def test_data_catalog_on_an_empty_root_says_where_data_will_go(root, tmp_path):
    """Through the lead's tool loop: the note is an error result, minted as no toolkit receipt
    (no toolkit code ran)."""
    from auto_research.graph.runtime import readonly_tools_for
    from auto_research.provenance import ReceiptStore

    store = ReceiptStore(tmp_path / "receipts")
    tools = readonly_tools_for("analyst_lead", roots=[tmp_path])
    tools.receipts, tools.receipt_source = store, "analyst_lead"
    out = asyncio.run(tools.execute(DATA_CATALOG_TOOL, {}))
    assert out.is_error and store.receipts() == []
    assert f"The local observation database at {root} holds no mission yet" in out.text
    assert "acquire creates a mission's folder, with its catalog, on its first observation" in out.text


def test_the_analysts_task_names_an_empty_root(root):
    from auto_research.graph.builder import _build_task
    from auto_research.schemas import ResearchRequest

    task = _build_task({"request": ResearchRequest(question="q")},
                       obs_config=load_observations_config())
    assert f"Local observation database: {root}, which holds no mission yet." in task.instructions
    assert "acquire creates a mission's folder, with its catalog, on its first observation" in task.instructions


@_needs_obsdb
def test_an_empty_root_is_healthy_and_a_missing_one_is_named(root, tmp_path):
    check_catalog_health(toolkit_root=TOOLKIT_ROOT)  # an empty, writable root: no raise
    root.rmdir()
    with pytest.raises(RuntimeError, match="is not a directory. Create it"):
        check_catalog_health(toolkit_root=TOOLKIT_ROOT)


@_needs_obsdb
@pytest.mark.skipif(os.geteuid() == 0, reason="root writes everywhere")
def test_a_root_that_takes_no_writes_is_named(root):
    root.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="is not writable by this user"):
            check_catalog_health(toolkit_root=TOOLKIT_ROOT)
    finally:
        root.chmod(0o700)
