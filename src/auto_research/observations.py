"""Observation-location configuration and the per-mission local data catalog.

The catalog (``<obs_db_path>/catalog.json``) is the data-side analogue of the
``FigureRef`` provenance pattern (CLAUDE.md §7) and the marked-synth-ObsID rule (§9.5):
it records which ObsIDs are local, their processing level (HEASARC supplies up to
**level 2**; **level 3** products carry the parameters used to obtain them), and the
provenance of each product. The **writers** are the runtime's authored toolkit tools
(``heasarc_utils`` acquire, reduction, pcube) — they cannot import ``auto_research``
(the trust boundary, §2), so the JSON shape is the contract, documented for the author
in ``prompts/coder.md``. This module owns only the **read side**, so the orchestration
can surface "what's already local" to analysts (a worker requests *acquire* only when
data is genuinely missing) and consume provenance.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_OBSERVATIONS_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "observations.yaml"

CATALOG_FILENAME = "catalog.json"


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


# ---------------------------------------------------------------------------
# Local data catalog (read side) — written by authored toolkit tools (§9.5).
# ---------------------------------------------------------------------------


class DataProduct(BaseModel):
    """One produced data product for an ObsID, with the provenance to reproduce it.

    ``params`` records the parameters used to obtain the product (e.g. the energy
    binning + region radii for a ``pcube``), so a level-3 product is reproducible (§9.1).
    ``produced_by`` names the tool/commit that wrote it; ``sha256`` is optional integrity.
    """

    model_config = ConfigDict(extra="allow")

    kind: str = Field(description="Product kind, e.g. 'level2_evt', 'pcube', 'pha'.")
    path: str = Field(description="Path to the product (file or directory) under the mission obs_db.")
    params: dict = Field(default_factory=dict, description="Parameters used to obtain the product.")
    produced_by: str = Field(default="", description="Tool/commit that produced this product.")
    created_at: datetime | None = None
    sha256: str | None = None


class CatalogEntry(BaseModel):
    """The catalog record for one ObsID."""

    model_config = ConfigDict(extra="allow")

    obsid: str
    max_level: int | None = Field(default=None, description="Highest processing level present (1|2|3).")
    levels_present: list[int] = Field(default_factory=list)
    products: list[DataProduct] = Field(default_factory=list)


class DataCatalog(BaseModel):
    """A mission's local data inventory — the parsed ``<obs_db_path>/catalog.json``."""

    model_config = ConfigDict(extra="allow")

    mission: str = ""
    entries: dict[str, CatalogEntry] = Field(default_factory=dict)

    def obsids(self) -> list[str]:
        """All catalogued ObsIDs."""
        return sorted(self.entries)

    def has(self, obsid: str, *, min_level: int | None = None) -> bool:
        """Whether ``obsid`` is local (optionally at or above ``min_level``)."""
        entry = self.entries.get(obsid)
        if entry is None:
            return False
        if min_level is None:
            return True
        return entry.max_level is not None and entry.max_level >= min_level


def catalog_path(mission: str, *, config_path: str | Path = DEFAULT_OBSERVATIONS_CONFIG_PATH) -> Path:
    """Return the catalog.json path for ``mission`` (``<obs_db_path>/catalog.json``)."""
    cfg = load_observations_config(config_path)
    return Path(cfg.mission(mission).obs_db_path) / CATALOG_FILENAME


def read_catalog(mission: str, *, config_path: str | Path = DEFAULT_OBSERVATIONS_CONFIG_PATH) -> DataCatalog:
    """Read and validate a mission's local data catalog.

    Returns an empty ``DataCatalog`` when the file is absent (nothing acquired yet).
    Raises loudly on a present-but-malformed catalog so a corrupt inventory never passes
    silently.
    """
    path = catalog_path(mission, config_path=config_path)
    if not path.exists():
        return DataCatalog(mission=mission)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed data catalog at {path}: {exc}") from exc
    data.setdefault("mission", mission)
    return DataCatalog.model_validate(data)


def local_obsids(
    mission: str,
    *,
    min_level: int | None = None,
    config_path: str | Path = DEFAULT_OBSERVATIONS_CONFIG_PATH,
) -> list[str]:
    """Return the locally-available ObsIDs for ``mission`` (optionally ``>= min_level``)."""
    catalog = read_catalog(mission, config_path=config_path)
    return [o for o in catalog.obsids() if catalog.has(o, min_level=min_level)]
