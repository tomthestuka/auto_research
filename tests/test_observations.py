"""Tests for observation-location configuration and the catalog health check.

The catalog READ (parsing/listing) moved to research_toolkit's ``catalog_utils`` (its
tests live there, ``catalog_utils/tests/test_catalog_read.py``); this module now covers
the orchestration-side pieces: obs-db-location config, the launch-time health check
(delegating to ``obsdb_catalog.healthcheck``), and the ``data_catalog`` tool wiring
(sourced from ``catalog_utils`` and run in-process for the lead).
"""

import json

import pytest
import yaml

from auto_research.observations import (
    _toolkit_root,
    check_catalog_health,
    load_observations_config,
)

# obsdb_catalog / catalog_utils ship with the research_toolkit checkout (pure stdlib); their
# location is config-driven (orchestration.yaml: toolkit_root), resolved the same way the
# check + the data_catalog tool do.
TOOLKIT_ROOT = _toolkit_root()


def _toolkit_pkg_available(pkg: str) -> bool:
    """Whether ``pkg`` is importable from ``toolkit_root`` (a provisioned machine).

    On an unprovisioned checkout (no toolkit, or a scaffolded one) the package is absent,
    so the tests that need it skip rather than fail — keeping the suite portable.
    """
    import importlib
    import sys

    root = str(TOOLKIT_ROOT)
    if not root or not (TOOLKIT_ROOT / pkg).exists():
        return False
    inserted = root not in sys.path
    if inserted:
        sys.path.insert(0, root)
    try:
        importlib.import_module(pkg)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if inserted and root in sys.path:
            sys.path.remove(root)


_needs_obsdb = pytest.mark.skipif(
    not _toolkit_pkg_available("obsdb_catalog"),
    reason="obsdb_catalog not in toolkit_root (unprovisioned machine)",
)
_needs_catalog_utils = pytest.mark.skipif(
    not _toolkit_pkg_available("catalog_utils"),
    reason="catalog_utils not in toolkit_root (unprovisioned machine)",
)


def test_load_observations_config_discovers_missions(tmp_path):
    """Discovery from a hermetic tmp obs-db (a `<root>/IXPE` subdir with a catalog.json marker)."""
    root = tmp_path / "obsdb"
    (root / "IXPE").mkdir(parents=True)
    (root / "IXPE" / "catalog.json").write_text("{}", encoding="utf-8")
    cfg = tmp_path / "observations.yaml"
    cfg.write_text(yaml.safe_dump({"obs_db_root": str(root)}), encoding="utf-8")

    config = load_observations_config(cfg)

    assert config.missions
    assert config.mission("ixpe").obs_db_path.name == "IXPE"


def test_load_observations_config_rejects_unknown_mission(tmp_path):
    root = tmp_path / "obsdb"
    root.mkdir()
    path = tmp_path / "observations.yaml"
    path.write_text(yaml.safe_dump({"obs_db_root": str(root)}), encoding="utf-8")

    config = load_observations_config(path)

    with pytest.raises(KeyError, match="unknown mission"):
        config.mission("missing")


# ── catalog health check (preflight) ────────────────────────────────────────


def _health_config(tmp_path, mission="probe"):
    """A single-root observations.yaml with one mission subdir whose basename has no obsdb
    scanner.

    ``healthcheck`` derives the mission from the obs_db basename; using one with no
    registered disk scanner keeps the test isolated to the catalog.json we control (no
    on-disk ``uncatalogued`` false positives).
    """
    root = tmp_path / "obsdb"
    obs_db = root / mission
    obs_db.mkdir(parents=True)
    cfg = tmp_path / "observations.yaml"
    cfg.write_text(yaml.safe_dump({"obs_db_root": str(root)}))
    return cfg, obs_db


@_needs_obsdb
def test_check_catalog_health_absent_catalog_passes(tmp_path):
    cfg, _ = _health_config(tmp_path)
    check_catalog_health(config_path=cfg, toolkit_root=TOOLKIT_ROOT)  # no raise


@_needs_obsdb
def test_check_catalog_health_good_catalog_passes(tmp_path):
    cfg, obs_db = _health_config(tmp_path)
    (obs_db / "01" / "evt").mkdir(parents=True)
    (obs_db / "01" / "evt" / "f.evt").write_text("x")
    (obs_db / "catalog.json").write_text(json.dumps({
        "entries": {
            "01": {
                "obsid": "01", "max_level": 2, "levels_present": [1, 2],
                "products": [{"kind": "level2_evt", "path": "01/evt/f.evt"}],
            },
        }
    }))
    check_catalog_health(config_path=cfg, toolkit_root=TOOLKIT_ROOT)  # no raise


@_needs_obsdb
def test_check_catalog_health_malformed_raises(tmp_path):
    cfg, obs_db = _health_config(tmp_path)
    (obs_db / "catalog.json").write_text("{ not valid json")
    with pytest.raises(RuntimeError, match="catalog drift"):
        check_catalog_health(config_path=cfg, toolkit_root=TOOLKIT_ROOT)


@_needs_obsdb
def test_check_catalog_health_missing_product_raises(tmp_path):
    cfg, obs_db = _health_config(tmp_path)
    (obs_db / "catalog.json").write_text(json.dumps({
        "entries": {
            "01": {
                "obsid": "01", "max_level": 2, "levels_present": [1, 2],
                "products": [{"kind": "level2_evt", "path": "01/evt/gone.evt"}],
            },
        }
    }))
    with pytest.raises(RuntimeError) as ei:
        check_catalog_health(config_path=cfg, toolkit_root=TOOLKIT_ROOT)
    # Human-friendly: the repair command + a one-line drift summary (not a raw dict dump).
    msg = str(ei.value)
    assert "repair_all" in msg
    assert "what drifted:" in msg
    assert "catalogued but missing on disk" in msg and "01 level2_evt" in msg
    assert "{'" not in msg and "[{" not in msg  # no raw dict/list dump


# ── data_catalog ReAct tool (now sourced from research_toolkit.catalog_utils) ──


def test_execute_data_catalog_delegates_to_catalog_utils(monkeypatch):
    """The in-process executor passes obs_db_path to the catalog_utils read and wraps the text.

    Monkeypatches the memoized read fn so the wiring is tested without a provisioned toolkit.
    """
    import asyncio

    from auto_research.agents import react

    captured = {}

    def _fake_read(obs_db_path, min_level=None):
        captured["obs_db_path"] = obs_db_path
        captured["min_level"] = min_level
        return "Local ixpe data (1 ObsIDs):\n- 01002901: max_level=3, products=[level2_evt, pcube]"

    monkeypatch.setattr(react, "_toolkit_clean_head", lambda: "head1")
    monkeypatch.setattr(react, "_catalog_read_fn", ("head1", _fake_read))

    out = asyncio.run(react.execute_data_catalog({"obs_db_path": "/data/x/IXPE", "min_level": 2}))
    assert not out.is_error
    assert "01002901" in out.text and "pcube" in out.text
    assert captured == {"obs_db_path": "/data/x/IXPE", "min_level": 2}


def test_execute_data_catalog_requires_obs_db_path(monkeypatch):
    import asyncio

    from auto_research.agents import react

    out = asyncio.run(react.execute_data_catalog({}))
    assert out.is_error and "obs_db_path" in out.text


def test_data_catalog_reviewer_excluded():
    """The reviewer's read-only provider must not carry the data_catalog tool (lead-only)."""
    from auto_research.agents.react import DATA_CATALOG_TOOL, ReadOnlyToolProvider

    reviewer = ReadOnlyToolProvider(roots=[])  # default: include_data_catalog=False
    assert DATA_CATALOG_TOOL not in {d["name"] for d in reviewer.tool_definitions()}


@_needs_catalog_utils
def test_data_catalog_lead_sources_def_from_catalog_utils():
    """The lead gets the data_catalog def sourced from research_toolkit.catalog_utils, with the
    registry-sanitized name and the obs_db_path arg (not the old mission arg)."""
    from auto_research.agents.react import DATA_CATALOG_TOOL, ReadOnlyToolProvider

    lead = ReadOnlyToolProvider(roots=[], include_data_catalog=True)
    defs = {d["name"]: d for d in lead.tool_definitions()}
    assert DATA_CATALOG_TOOL in defs
    props = defs[DATA_CATALOG_TOOL]["input_schema"]["properties"]
    assert "obs_db_path" in props and "mission" not in props


def test_data_catalog_runs_committed_toolkit_code_only(tmp_path, monkeypatch, request):
    """The lead's in-process read never runs an uncommitted edit, and a new commit is re-imported."""
    import asyncio
    import os
    import subprocess

    from auto_research import env as app_env
    from auto_research.agents import react

    root = tmp_path / "toolkit"
    lib = root / "catalog_utils" / "lib"
    lib.mkdir(parents=True)
    (root / "catalog_utils" / "__init__.py").write_text("")
    (lib / "__init__.py").write_text("")
    (lib / "catalog_read.py").write_text("def data_catalog(path, min_level=None):\n    return 'v1'\n")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")  # as the real toolkit's
    git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    git("add", "-A"); git("commit", "-qm", "v1")
    monkeypatch.setattr(app_env, "toolkit_root", lambda: root)
    monkeypatch.setattr(react, "_catalog_read_fn", None)
    import sys

    def forget_scratch_modules():  # the scratch toolkit's catalog_utils must not outlive this test
        for name in [m for m in sys.modules if m.split(".")[0] in ("catalog_utils", "obsdb_catalog")]:
            del sys.modules[name]

    forget_scratch_modules()
    request.addfinalizer(forget_scratch_modules)
    run = lambda: asyncio.run(react.execute_data_catalog({"obs_db_path": "/x"}))  # noqa: E731
    assert run().text == "v1"
    (lib / "catalog_read.py").write_text("def data_catalog(path, min_level=None):\n    return 'EDITED'\n")
    edited = run()
    assert edited.is_error and "uncommitted" in edited.text
    git("commit", "-qam", "v2")
    assert run().text == "EDITED"
    # A forged bytecode file (git-ignored, so the clean check cannot see it) is never what runs.
    import importlib.util
    import py_compile

    source = lib / "catalog_read.py"
    forged_src = tmp_path / "forged.py"
    forged_src.write_text("def data_catalog(path, min_level=None):\n    return 'FORGED'\n")
    st = source.stat()
    os.utime(forged_src, ns=(st.st_atime_ns, st.st_mtime_ns))
    forged_src.write_text("def data_catalog(path, min_level=None):\n    return 'FORGED'\n".ljust(st.st_size))
    os.utime(forged_src, ns=(st.st_atime_ns, st.st_mtime_ns))
    pyc = importlib.util.cache_from_source(str(source))
    py_compile.compile(str(forged_src), cfile=pyc)
    git("commit", "-q", "--allow-empty", "-m", "v3")  # a new head: the next call re-imports
    assert run().text == "EDITED"

