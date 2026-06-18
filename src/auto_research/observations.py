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

The obs database is **one folder** (``obs_db_root`` in ``config/observations.yaml``); each
mission lives in a subdirectory ``<obs_db_root>/<MISSION>`` and is discovered from disk (a
subdir is a mission iff it holds a ``catalog.json``), so mission names stay data, never
special cases in code.
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
    """Validated observation configuration: a **single obs-db root** whose per-mission
    subdirectories are discovered from disk (no per-mission map in config anymore).

    Each immediate subdirectory of ``obs_db_root`` that holds a ``catalog.json`` is a
    mission, keyed by ``basename.lower()`` (``research_toolkit`` derives the mission name the
    same way). This marker is what separates real mission folders from unrelated working
    directories that may share the root. ``research_toolkit`` tools are unchanged — they
    still take a per-mission directory, which is ``<obs_db_root>/<MISSION>``.
    """

    model_config = ConfigDict(extra="forbid")

    obs_db_root: Path = Field(description="Root folder holding one subdirectory per mission.")

    @property
    def missions(self) -> dict[str, MissionObservationConfig]:
        """Discover missions from disk: subdirectories of ``obs_db_root`` with a
        ``catalog.json``, keyed by ``basename.lower()``. Empty when the root is absent."""
        result: dict[str, MissionObservationConfig] = {}
        root = self.obs_db_root
        if root.is_dir():
            for sub in sorted(root.iterdir()):
                if sub.is_dir() and (sub / CATALOG_FILENAME).exists():
                    result[sub.name.lower()] = MissionObservationConfig(obs_db_path=sub)
        return result

    def mission(self, name: str) -> MissionObservationConfig:
        """Resolve one mission's ``obs_db_path`` (``<obs_db_root>/<MISSION>``) by name.

        Matches the on-disk subdirectory case-insensitively (``basename.lower() == name``),
        so the path resolves even before the mission's ``catalog.json`` exists (e.g. a first
        acquire). Raises ``KeyError`` if no such subdirectory is present.
        """
        root = self.obs_db_root
        if root.is_dir():
            for sub in sorted(root.iterdir()):
                if sub.is_dir() and sub.name.lower() == name.lower():
                    return MissionObservationConfig(obs_db_path=sub)
        raise KeyError(f"unknown mission observation config: {name}")


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


# ---------------------------------------------------------------------------
# Launch-time catalog health check (preflight; detect-only, never writes).
# ---------------------------------------------------------------------------


def _toolkit_root() -> Path:
    """``toolkit_root`` from ``config/orchestration.yaml`` — the ``research_toolkit`` checkout."""
    path = DEFAULT_OBSERVATIONS_CONFIG_PATH.parent / "orchestration.yaml"
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except OSError:
        data = {}
    return Path(data.get("toolkit_root", "")).expanduser()


def _import_healthcheck(toolkit_root: Path):
    """Import ``research_toolkit``'s detect-only ``healthcheck`` (pure stdlib, dependency-free).

    ``obsdb_catalog`` ships with ``research_toolkit`` at ``toolkit_root`` and is *not*
    ``*_utils``-named, so the tool registry ignores it — we import it directly, making
    ``toolkit_root`` importable the same transient way ``ToolkitRegistry`` does (prepend to
    ``sys.path`` for the import, then remove; the module stays cached in ``sys.modules``).
    """
    import importlib
    import sys

    root_str = str(toolkit_root)
    inserted = root_str not in sys.path
    if inserted:
        sys.path.insert(0, root_str)
    try:
        module = importlib.import_module("obsdb_catalog.healthcheck")
    finally:
        if inserted:
            sys.path.remove(root_str)
    return module.healthcheck


def check_catalog_health(
    *,
    config_path: str | Path = DEFAULT_OBSERVATIONS_CONFIG_PATH,
    toolkit_root: str | Path | None = None,
) -> None:
    """Launch-time preflight (no LLM): assert every mission's local data catalog is healthy.

    Delegates to ``research_toolkit``'s detect-only ``obsdb_catalog.healthcheck`` (pure
    standard library, never writes) over each configured mission's ``obs_db_path``. It flags
    a malformed/unparseable ``catalog.json``, catalogued-but-missing files,
    ``levels_present``/``max_level`` inconsistency, a regressed ``max_level``, and
    on-disk-but-uncatalogued files (CLAUDE.md §9.5 catalog provenance). Every mission's drift
    report is aggregated so one launch surfaces everything wrong, then raised — this check
    **never repairs**. On drift the human runs ``obsdb_catalog.repair(obs_db_path)`` out of
    band and reruns. An absent catalog with no local data is healthy (``healthcheck`` returns
    ``True``). Registered in ``auto_research.preflight``.
    """
    root = Path(toolkit_root).expanduser() if toolkit_root is not None else _toolkit_root()
    healthcheck = _import_healthcheck(root)
    problems: dict[str, object] = {}
    for mission, cfg in load_observations_config(config_path).missions.items():
        result = healthcheck(str(cfg.obs_db_path))
        if result is not True:
            problems[mission] = result
    if problems:
        raise RuntimeError(
            "data catalog health drift — run obsdb_catalog.repair(obs_db_path) on each, then "
            f"rerun:\n{problems}"
        )
