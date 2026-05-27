"""Observation-location configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_OBSERVATIONS_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "observations.yaml"


class MissionObservationConfig(BaseModel):
    """Configuration for one mission's observation database or archive location."""

    model_config = ConfigDict(extra="forbid")

    obs_db_path: Path = Field(description="Path to the mission observation database or archive root.")


class ObservationsConfig(BaseModel):
    """Validated observation configuration for all configured missions."""

    model_config = ConfigDict(extra="forbid")

    missions: dict[str, MissionObservationConfig] = Field(default_factory=dict)

    def mission(self, name: str) -> MissionObservationConfig:
        """Return one mission config by name."""

        try:
            return self.missions[name]
        except KeyError as exc:
            raise KeyError(f"unknown mission observation config: {name}") from exc


def load_observations_config(
    path: str | Path = DEFAULT_OBSERVATIONS_CONFIG_PATH,
) -> ObservationsConfig:
    """Load and validate observation locations from YAML."""

    with Path(path).open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    return ObservationsConfig.model_validate(data)
