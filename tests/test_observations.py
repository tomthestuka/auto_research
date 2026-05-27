"""Tests for observation-location configuration."""

import pytest
import yaml

from auto_research.observations import load_observations_config


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
