"""Receipts name the observations a tool located itself, and the toolkit they ran against (no LLM).

Drives the real `SandboxToolProvider` and `ReceiptStore`; only the tool process is a fake.
"""

import asyncio
import json
import subprocess

import pytest

import auto_research.agents.react as react
from auto_research.agents.react import SandboxToolProvider
from auto_research.provenance import ReceiptStore
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

TOOL = "research_toolkit.tk.fit"


@pytest.fixture
def mission(tmp_path, monkeypatch):
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    root = tmp_path / "IXPE"
    for obsid in ("01002901", "02001901"):
        (root / obsid / "event_l2").mkdir(parents=True)
        (root / obsid / "event_l2" / "evt2.fits").write_bytes(b"events " + obsid.encode())
    (root / "catalog.json").write_text(json.dumps({"mission": "ixpe", "entries": {
        "01002901": {"obsid": "01002901", "max_level": 2},
        "02001901": {"obsid": "02001901", "max_level": 3},
    }}))
    monkeypatch.setattr(react, "_observation_path_args", lambda: {"ixpe_path": str(root)})
    return root


def _toolkit(tmp_path):
    root = tmp_path / "toolkit"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return root, head


def _call(tmp_path, store, args, toolkit_root=None, kind=TOOL):
    registry = _FakeRegistry(known={TOOL, "mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"},
                             schema={"properties": {"obsid": {"kind": "setting"}, "obsids": {"kind": "setting"},
                                                    "epoch": {"kind": "setting"}, "ixpe_path": {"kind": "path"},
                                                    "obs_db_path": {"kind": "path"}, "infile": {"kind": "path"}}})
    registry._root = toolkit_root
    provider = SandboxToolProvider(registry, _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "ok"}),
                                   cwd=tmp_path, receipts=store, receipt_source="analyst_worker_0")
    asyncio.run(provider.execute(kind, args))
    return store.receipts(kind="result")[-1]


def test_an_obsid_the_catalog_lists_is_an_obsdb_input_with_a_hash(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    receipt = _call(tmp_path, store, {"obsid": "01002901"})
    (entry,) = receipt.inputs
    assert entry.basis == "obsdb" and entry.path == (mission / "01002901").resolve()

    both = _call(tmp_path, store, {"obsids": ["01002901", "02001901", "09999999"]})
    assert [i.path.name for i in both.inputs] == ["01002901", "02001901"]
    assert both.inputs[0].sha256 == entry.sha256


def test_the_hash_follows_the_folder_and_the_catalog_entry(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    first = _call(tmp_path, store, {"obsid": "01002901"}).inputs[0].sha256
    (mission / "01002901" / "event_l2" / "pcube.fits").write_bytes(b"new product")
    second = _call(tmp_path, store, {"obsid": "01002901"}).inputs[0].sha256
    catalog = json.loads((mission / "catalog.json").read_text())
    catalog["entries"]["01002901"]["max_level"] = 3
    (mission / "catalog.json").write_text(json.dumps(catalog))
    third = _call(tmp_path, store, {"obsid": "01002901"}).inputs[0].sha256
    assert len({first, second, third}) == 3


def test_the_models_own_mission_folder_is_the_one_recorded(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _call(tmp_path, store, {"obsid": "01002901", "ixpe_path": str(other)}).inputs == []
    copy = tmp_path / "copy" / "IXPE"
    import shutil
    shutil.copytree(mission, copy)
    (entry,) = _call(tmp_path, store, {"obsid": "01002901", "ixpe_path": str(copy)}).inputs
    assert entry.path == (copy / "01002901").resolve()
    (relative,) = _call(tmp_path, store, {"obsid": "01002901", "ixpe_path": "copy/IXPE"}).inputs
    assert relative.path == entry.path  # resolved against the worker's folder, as the tool does


def test_obsids_nested_in_a_dict_are_found(tmp_path, mission):
    """`load_data` takes its ObsIDs as `epoch={"IXPE": [...], "NICER": [...], "STATE": "HSS"}`."""
    store = ReceiptStore(tmp_path / "receipts")
    receipt = _call(tmp_path, store, {"epoch": {"IXPE": ["02001901"], "NICER": ["5100"], "STATE": "HSS"}})
    assert [(i.basis, i.path.name) for i in receipt.inputs] == [("obsdb", "02001901")]


def test_obs_db_path_is_a_mission_folder_never_hashed_whole(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    receipt = _call(tmp_path, store, {"obsid": "01002901", "obs_db_path": str(mission)})
    assert [(i.basis, i.path.name) for i in receipt.inputs] == [("obsdb", "01002901")]


def test_an_uncatalogued_observation_folder_is_still_an_input_and_flags_synthetic(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    synth = mission / "synth261017-abc123"
    synth.mkdir()
    (synth / "evt.fits").write_bytes(b"made up")
    receipt = _call(tmp_path, store, {"obsid": "synth261017-abc123"})
    assert [(i.basis, i.path.name) for i in receipt.inputs] == [("listing", "synth261017-abc123")]
    assert "synthetic" in receipt.flags


def test_a_toolkit_receipt_names_its_root_and_commit(tmp_path, mission):
    root, head = _toolkit(tmp_path)
    store = ReceiptStore(tmp_path / "receipts")
    receipt = _call(tmp_path, store, {"obsid": "01002901"}, toolkit_root=root)
    assert (receipt.toolkit_root, receipt.toolkit_commit) == (root, head)
    unknown = _call(tmp_path, store, {"obsid": "01002901"}, toolkit_root=None)
    assert (unknown.toolkit_root, unknown.toolkit_commit) == (None, None)
    shell = _call(tmp_path, store, {"command": "true"}, toolkit_root=root, kind="mcp.builtin.bash_exec")
    assert shell.toolkit_root is None  # a harness call names no toolkit tool


def test_the_leads_data_catalog_names_its_toolkit_root_and_commit(tmp_path, monkeypatch):
    """Through the lead's production tool loop (`readonly_tools_for`), not the helper."""
    import auto_research.env as env
    from auto_research.graph.runtime import readonly_tools_for

    root, _ = _toolkit(tmp_path)
    (root / "catalog_utils" / "lib").mkdir(parents=True)
    (root / "catalog_utils" / "__init__.py").write_text(
        "TOOL_MANIFEST = {'research_toolkit.catalog_utils.data_catalog': {'args_schema': {'properties': "
        "{'obs_db_path': {'kind': 'path'}, 'min_level': {'kind': 'setting'}}}}}\n")
    (root / "catalog_utils" / "lib" / "__init__.py").write_text("")
    (root / "catalog_utils" / "lib" / "catalog_read.py").write_text(
        "def data_catalog(obs_db_path, min_level=None):\n    return f'2 observations in {obs_db_path}'\n")
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "catalog"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    monkeypatch.setattr(env, "toolkit_root", lambda: root)
    monkeypatch.setattr(react, "_catalog_read_fn", None)
    import sys

    for name in [m for m in sys.modules if m.split(".")[0] == "catalog_utils"]:
        monkeypatch.delitem(sys.modules, name)  # the real one comes back after this test

    store = ReceiptStore(tmp_path / "receipts")
    tools = readonly_tools_for("analyst_lead", roots=[tmp_path])
    tools.receipts, tools.receipt_source = store, "analyst_lead"
    mission = tmp_path / "IXPE"
    mission.mkdir()
    (mission / "catalog.json").write_text('{"entries": {}}')
    out = asyncio.run(tools.execute(react.DATA_CATALOG_TOOL, {"obs_db_path": str(mission)}))
    assert not out.is_error, out.text
    (receipt,) = store.receipts()
    assert receipt.produced_by == "research_toolkit.catalog_utils.data_catalog"
    assert (receipt.toolkit_root, receipt.toolkit_commit) == (root.resolve(), head)
    assert [i.path.name for i in receipt.inputs] == ["catalog.json"]
    for name in [m for m in sys.modules if m.split(".")[0] == "catalog_utils"]:
        del sys.modules[name]  # this fake toolkit's modules, imported by the call


def test_hand_authored_fixtures_flag_their_results_synthetic(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    catalog = json.loads((mission / "catalog.json").read_text())
    catalog["entries"]["synth_pa_pd_constant"] = {"produced_by": "validation_fixture: hand-authored"}
    (mission / "catalog.json").write_text(json.dumps(catalog))
    (mission / "synth_pa_pd_constant").mkdir()
    receipt = _call(tmp_path, store, {"obsid": "synth_pa_pd_constant"})
    assert receipt.inputs[0].basis == "obsdb" and "synthetic" in receipt.flags


def test_number_obsids_trailing_slashes_and_nul_paths(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    (mission / "80902318002").mkdir()
    numbered = _call(tmp_path, store, {"epoch": {"NuSTAR": [80902318002]}, "obsid": "01002901/"})
    assert sorted(i.path.name for i in numbered.inputs) == ["01002901", "80902318002"]
    poisoned = _call(tmp_path, store, {"obsid": "01002901", "ixpe_path": "/x\u0000y", "infile": "a/b\u0000c"})
    assert poisoned.inputs == []  # recorded nothing; the call itself ran


def test_an_uncatalogued_folder_is_hashed_fresh_and_names_without_digits_are_not_observations(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    nested = mission / "01002901_picorr" / "event_l2"
    nested.mkdir(parents=True)
    first = _call(tmp_path, store, {"obsid": "01002901_picorr"}).inputs[0].sha256
    (nested / "pcube.fits").write_bytes(b"new product")  # the top folder's mtime does not change
    assert _call(tmp_path, store, {"obsid": "01002901_picorr"}).inputs[0].sha256 != first
    (mission / "figures").mkdir()
    # No observation named, and the mission folder was filled in by the runtime: nothing was read.
    assert _call(tmp_path, store, {"epoch": {"STATE": "figures"}}).inputs == []


def test_a_call_naming_no_observation_records_the_catalog_it_reads(tmp_path, mission):
    store = ReceiptStore(tmp_path / "receipts")
    (entry,) = _call(tmp_path, store, {"obs_db_path": str(mission)}).inputs
    assert entry.basis == "content" and entry.path == (mission / "catalog.json").resolve()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A mission folder laid out as HEASARC's archive (H2): ``ixpe/data/obs/<prefix>/<obsid>``."""
    from auto_research.sandbox import fs_guard

    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [tmp_path.resolve()])
    root = tmp_path / "ixpe"
    (root / "reduced" / "01002901").mkdir(parents=True)  # a product kept elsewhere; the level-2 event wins
    for prefix, obsid in (("01", "01002901"), ("02", "02001901")):
        (root / "data" / "obs" / prefix / obsid / "event_l2").mkdir(parents=True)
        (root / "data" / "obs" / prefix / obsid / "event_l2" / "evt2.fits").write_bytes(obsid.encode())
    (root / "catalog.json").write_text(json.dumps({"mission": "ixpe", "entries": {
        # Recorded: the folder is read from the product path. Recorded outside the mission folder
        # (a `..` or an absolute path): the tree rule finds it.
        "01002901": {"obsid": "01002901", "max_level": 2, "products": [
            {"kind": "pha", "path": "reduced/01002901/x.pha"},
            {"kind": "pha", "path": "../elsewhere/01002901/x.pha"},
            {"kind": "level2_evt", "path": "reduced/01002901/old_evt2.fits", "created_at": "2025-01-01"},
            {"kind": "level2_evt", "path": "data/obs/01/01002901/event_l2/evt2.fits",
             "created_at": "2026-11-04"}]},
        "02001901": {"obsid": "02001901", "max_level": 2, "products": [
            {"kind": "level2_evt", "path": "../elsewhere/02001901/evt2.fits"},
            {"kind": "level2_evt", "path": str(tmp_path / "elsewhere/02001901/evt2.fits")}]},
    }}))
    (tmp_path / "elsewhere" / "02001901").mkdir(parents=True)
    monkeypatch.setattr(react, "_observation_path_args", lambda: {"ixpe_path": str(root)})
    return root


def test_an_obsid_under_heasarcs_tree_is_found_by_its_record_and_by_the_tree_rule(tmp_path, tree):
    store = ReceiptStore(tmp_path / "receipts")
    receipt = _call(tmp_path, store, {"obsids": ["01002901", "02001901"]})
    assert [(i.basis, i.path) for i in receipt.inputs] == [
        ("obsdb", (tree / "data/obs/01/01002901").resolve()),
        ("obsdb", (tree / "data/obs/02/02001901").resolve())]
    first = receipt.inputs[0].sha256
    (tree / "data/obs/01/01002901/event_l2/pcube.fits").write_bytes(b"new product")
    assert _call(tmp_path, store, {"obsid": "01002901"}).inputs[0].sha256 != first  # the tree folder's listing


def test_an_uncatalogued_tree_folder_is_a_listing_and_a_doubled_obsid_records_nothing(tmp_path, tree):
    store = ReceiptStore(tmp_path / "receipts")
    (tree / "data/obs/03/03002599/event_l2").mkdir(parents=True)
    (entry,) = _call(tmp_path, store, {"obsid": "03002599"}).inputs
    assert entry.basis == "listing" and entry.path == (tree / "data/obs/03/03002599").resolve()
    (tree / "data/obs/99/02001901").mkdir(parents=True)  # the tree rule now finds it twice
    assert _call(tmp_path, store, {"obsid": "02001901"}).inputs == []
    (tree / "data/obs/99/01002901").mkdir(parents=True)  # twice too, but its catalog records where
    (entry,) = _call(tmp_path, store, {"obsid": "01002901"}).inputs
    assert entry.path == (tree / "data/obs/01/01002901").resolve()


def test_an_obsid_folder_names_its_mission_under_either_layout(tmp_path):
    from auto_research.observations import obsid_mission

    assert obsid_mission(tmp_path / "ixpe/data/obs/01/01002901") == "ixpe"
    assert obsid_mission(tmp_path / "nustar/data/obs/80/9/80902318002") == "nustar"
    assert obsid_mission(tmp_path / "chandra/data/byobsid/2/12345") == "chandra"
    assert obsid_mission(tmp_path / "IXPE/01002901") == "IXPE"
    assert obsid_mission(tmp_path / "XMM/0123456789") == "XMM"


def test_a_symlinked_mission_folder_keeps_its_missions_tree_rule(tmp_path, tree, monkeypatch):
    """The mission is the folder's name as configured; resolving the link must not lose it."""
    store_dir = tmp_path / "IXPE_store"
    tree.rename(store_dir)
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "ixpe").symlink_to(store_dir)
    monkeypatch.setattr(react, "_observation_path_args", lambda: {"ixpe_path": str(tmp_path / "root" / "ixpe")})
    (store_dir / "data/obs/03/03002599").mkdir(parents=True)
    (entry,) = _call(tmp_path, ReceiptStore(tmp_path / "receipts"), {"obsid": "03002599"}).inputs
    assert entry.basis == "listing" and entry.path == store_dir.resolve() / "data/obs/03/03002599"


def test_a_folder_a_record_names_outside_the_tree_names_its_mission(tmp_path, tree):
    from auto_research.observations import obsid_mission

    assert obsid_mission(tree / "reduced" / "01002901") == "ixpe"


@pytest.mark.skipif(not __import__("tests.test_observations", fromlist=["x"])._toolkit_pkg_available("obsdb_catalog"),
                    reason="obsdb_catalog not in toolkit_root (unprovisioned machine)")
def test_the_runtime_finds_each_obsid_where_the_toolkits_locate_does(tmp_path, tree, monkeypatch):
    """`observations.OBSID_TREES` / `obsid_folder` mirror `obsdb_catalog`: the same table, the same folder."""
    import sys

    from auto_research.observations import OBSID_TREES, obsid_folder
    from auto_research.provenance.store import _catalog_entries
    from tests.test_observations import TOOLKIT_ROOT

    monkeypatch.syspath_prepend(str(TOOLKIT_ROOT))
    for name in [m for m in sys.modules if m.split(".")[0] == "obsdb_catalog"]:
        monkeypatch.delitem(sys.modules, name)
    from obsdb_catalog import _domain
    from obsdb_catalog.locate import locate

    assert OBSID_TREES == _domain.TREES
    (tree / "data/obs/03/03002599").mkdir(parents=True)
    entries = _catalog_entries(tree / "catalog.json")
    for obsid in ("01002901", "02001901", "03002599"):
        assert obsid_folder(tree, obsid, entries.get(obsid)) == locate(tmp_path, "ixpe", obsid), obsid
    (tree / "data/obs/99/02001901").mkdir(parents=True)
    with pytest.raises(LookupError):
        locate(tmp_path, "ixpe", "02001901")
    with pytest.raises(LookupError):
        obsid_folder(tree, "02001901", entries.get("02001901"))
