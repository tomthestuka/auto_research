"""Observation-location configuration and the per-mission local data catalog.

The catalog (``<obs_db_path>/catalog.json``) is the data-side analogue of the
``FigureRef`` provenance pattern (CLAUDE.md §7) and the marked-synth-ObsID rule (§9.5):
it records which ObsIDs are local, their processing level (HEASARC supplies up to
**level 2**; **level 3** products carry the parameters used to obtain them), and the
provenance of each product. The **writers** are the runtime's authored toolkit tools
(``heasarc_utils`` acquire, reduction, pcube) — they cannot import ``auto_research``
(the trust boundary, §2), so the JSON shape is the contract, documented for the author
in ``prompts/coder.md``. This module owns the **observation-location config** (the obs-db
root + per-mission discovery) and the **launch-time catalog health check** only. The
catalog **read** (parsing/listing "what's already local") lives in ``research_toolkit``'s
``catalog_utils`` package (CLAUDE.md §2: domain reads belong to the toolkit, alongside the
``obsdb_catalog`` writer); orchestration reaches it through the tool layer, and preflight
delegates health to ``obsdb_catalog.healthcheck``.

The obs database is **one folder** (``obs_db_root`` in ``config/observations.yaml``); each
mission lives in a subdirectory ``<obs_db_root>/<MISSION>`` and is discovered from disk (a
subdir is a mission iff it holds a ``catalog.json``), so mission names stay data, never
special cases in code.
"""

from __future__ import annotations

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

    path = Path(path)
    if not path.exists():
        raise RuntimeError(
            f"Config not found: {path}. Run `bash install.sh` from the repo root, or copy "
            f"{path.name}.example to {path.name} and edit it."
        )
    with path.open("r", encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    return ObservationsConfig.model_validate(data)


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
    tk_root = Path(toolkit_root).expanduser() if toolkit_root is not None else _toolkit_root()
    healthcheck = _import_healthcheck(tk_root)
    cfg_all = load_observations_config(config_path)
    problems: dict[str, object] = {}
    for mission, cfg in cfg_all.missions.items():
        result = healthcheck(str(cfg.obs_db_path))
        if result is not True:
            problems[mission] = result
    if problems:
        raise RuntimeError(
            "data catalog drift — the catalog and the files on disk disagree. The runtime never "
            "repairs the catalog (that is a deliberate human step). Fix it, then rerun:\n\n"
            f"  PYTHONPATH={tk_root} python -m obsdb_catalog repair_all {cfg_all.obs_db_root}\n\n"
            "what drifted:\n" + _summarize_drift(problems)
        )


# Friendly labels for the `obsdb_catalog.healthcheck` drift categories (see that module).
_DRIFT_LABEL = {
    "missing": "catalogued but missing on disk",
    "uncatalogued": "on disk but not in the catalog",
    "level_inconsistent": "levels_present / max_level inconsistent",
    "regressed": "max_level regressed below a kept level-3 product",
    "malformed": "catalog.json malformed",
}


def _summarize_drift(problems: dict) -> str:
    """Render the healthcheck drift report as a few human-readable lines — one per
    mission/category with a count and a couple of examples — instead of dumping every path.
    """
    lines: list[str] = []
    for mission in sorted(problems):
        detail = problems[mission]
        if not isinstance(detail, dict):  # defensive: unexpected shape
            lines.append(f"  {mission}: {detail}")
            continue
        for category in sorted(detail):
            items = detail[category]
            label = _DRIFT_LABEL.get(category, category)
            if isinstance(items, str):  # malformed → the parse error
                lines.append(f"  {mission}: {label} — {items}")
                continue
            if not isinstance(items, list):
                lines.append(f"  {mission}: {label}: {items}")
                continue
            examples: list[str] = []
            for item in items:
                if isinstance(item, dict):
                    examples.append(" ".join(str(item[k]) for k in ("obsid", "kind") if item.get(k)))
                else:  # a bare path / obsid string
                    examples.append(Path(str(item)).name)
            unique = sorted(dict.fromkeys(e for e in examples if e))
            shown = unique[:3]
            more = f" (+{len(unique) - len(shown)} more)" if len(unique) > len(shown) else ""
            eg = f" — e.g. {'; '.join(shown)}{more}" if shown else ""
            lines.append(f"  {mission}: {len(items)} {label}{eg}")
    return "\n".join(lines)
