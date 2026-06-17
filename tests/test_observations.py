"""Tests for observation-location configuration and the local data catalog."""

import json

import pytest
import yaml

from auto_research.observations import (
    DataCatalog,
    load_observations_config,
    local_obsids,
    read_catalog,
)


def test_load_observations_config_from_project_file():
    config = load_observations_config()

    assert config.missions
    assert config.mission("ixpe").obs_db_path.name == "IXPE"


def test_load_observations_config_rejects_unknown_mission(tmp_path):
    path = tmp_path / "observations.yaml"
    path.write_text(yaml.safe_dump({"missions": {}}), encoding="utf-8")

    config = load_observations_config(path)

    with pytest.raises(KeyError, match="unknown mission"):
        config.mission("missing")


# ── data catalog (read side) ────────────────────────────────────────────────


def _config_with_obs_db(tmp_path):
    obs_db = tmp_path / "IXPE"
    obs_db.mkdir()
    cfg = tmp_path / "observations.yaml"
    cfg.write_text(yaml.safe_dump({"missions": {"ixpe": {"obs_db_path": str(obs_db)}}}))
    return cfg, obs_db


def test_read_catalog_absent_returns_empty(tmp_path):
    cfg, _ = _config_with_obs_db(tmp_path)
    catalog = read_catalog("ixpe", config_path=cfg)
    assert isinstance(catalog, DataCatalog)
    assert catalog.obsids() == []


def test_read_catalog_parses_levels_and_products(tmp_path):
    cfg, obs_db = _config_with_obs_db(tmp_path)
    (obs_db / "catalog.json").write_text(json.dumps({
        "entries": {
            "01002901": {
                "obsid": "01002901", "max_level": 3, "levels_present": [1, 2, 3],
                "products": [
                    {"kind": "level2_evt", "path": "01002901/evt2", "produced_by": "heasarc"},
                    {"kind": "pcube", "path": "01002901/pcube", "produced_by": "pcube_tool",
                     "params": {"ebinning": [2, 5, 8], "src_rad": 60}},
                ],
            },
            "02001901": {"obsid": "02001901", "max_level": 2, "levels_present": [1, 2]},
        }
    }))

    catalog = read_catalog("ixpe", config_path=cfg)
    assert catalog.obsids() == ["01002901", "02001901"]
    assert catalog.has("01002901", min_level=3)
    assert not catalog.has("02001901", min_level=3)
    pcube = [p for p in catalog.entries["01002901"].products if p.kind == "pcube"][0]
    assert pcube.params["src_rad"] == 60

    assert local_obsids("ixpe", config_path=cfg) == ["01002901", "02001901"]
    assert local_obsids("ixpe", min_level=3, config_path=cfg) == ["01002901"]


def test_read_catalog_malformed_raises(tmp_path):
    cfg, obs_db = _config_with_obs_db(tmp_path)
    (obs_db / "catalog.json").write_text("{ not valid json")
    with pytest.raises(ValueError, match="malformed data catalog"):
        read_catalog("ixpe", config_path=cfg)
