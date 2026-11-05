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

import os
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from auto_research.config_paths import observations_yaml, orchestration_yaml


#: Kept as a name because callers pass it as a default argument; it resolves through
#: :mod:`auto_research.config_paths`, so a process configured against another directory reads that
#: one. It is a function everywhere it matters and this alias only for the module's public API.
def default_observations_config_path() -> Path:
    """The ``observations.yaml`` this process reads."""
    return observations_yaml()

CATALOG_FILENAME = "catalog.json"

#: Where each mission keeps an ObsID's folder under its mission folder, as HEASARC's archive lays
#: it out (``ixpe/data/obs/01/01002901``), or flat. A mirror of ``research_toolkit``'s
#: ``obsdb_catalog._domain.TREES``, the rule the tools write by; the two change together. A mission
#: it does not list keeps ``<obsid>``.
OBSID_TREES: dict[str, tuple[str, ...]] = {
    "ixpe": ("data/obs/*/{obsid}", "{obsid}"),
    "nicer": ("data/obs/*/{obsid}", "{obsid}"),
    "nustar": ("data/obs/*/*/{obsid}", "{obsid}"),
    "chandra": ("data/byobsid/*/{obsid}",),
}


def obsid_index(mission_dir: Path, mission: str) -> dict[str, list[Path]]:
    """Every folder under ``mission_dir`` where ``mission``'s :data:`OBSID_TREES` rule keeps an
    ObsID, keyed by its name (the ObsID): one walk of the tree serves every name a call holds."""
    import glob

    found: dict[str, set[str]] = {}
    for pattern in OBSID_TREES.get(mission.lower(), ("{obsid}",)):
        for d in glob.glob(str(Path(mission_dir) / pattern.format(obsid="*"))):
            if os.path.isdir(d):
                found.setdefault(os.path.basename(d), set()).add(d)
    return {name: [Path(d) for d in sorted(dirs)] for name, dirs in found.items()}


def obsid_folder(
    mission_dir: Path, obsid: str, entry: dict | None = None, *, mission: str | None = None,
    index: Callable[[], Mapping[str, list[Path]]] | None = None,
) -> Path:
    """The folder holding ``obsid`` under ``mission_dir``, found as the toolkit's
    ``obsdb_catalog.locate`` finds it: the catalog ``entry``'s record first (the folder a recorded
    product lies in, up to the path part that is the ObsID; the newest level-2 event before other
    kinds; a path leaving the mission folder is passed over), then ``mission``'s
    :data:`OBSID_TREES` rule (default: the folder's own name). ``<mission_dir>/<obsid>`` when
    neither finds a folder; :class:`LookupError` when the tree rule finds the ObsID twice.
    ``index``: returns :func:`obsid_index` of the folder, for a caller looking up many names."""
    folder = Path(mission_dir)
    products = (entry or {}).get("products") if isinstance(entry, dict) else None
    recorded = [p for p in products if isinstance(p, dict)] if isinstance(products, list) else []
    level2 = sorted((p for p in recorded if p.get("kind") == "level2_evt"),
                    key=lambda p: str(p.get("created_at") or ""), reverse=True)
    for product in level2 + [p for p in recorded if p.get("kind") != "level2_evt"]:
        path = product.get("path")
        parts = Path(path).parts if isinstance(path, str) else ()
        if obsid in parts and ".." not in parts and not os.path.isabs(path):
            found = folder.joinpath(*parts[: parts.index(obsid) + 1])
            if found.is_dir():
                return found
    tree = index() if index is not None else obsid_index(folder, mission or folder.name)
    found = tree.get(obsid, [])
    if len(found) > 1:
        raise LookupError(f"ObsID {obsid} is in more than one folder under {folder}: {found}")
    return found[0] if found else folder / obsid


def obsid_mission(folder: Path) -> str:
    """The mission folder's name for an ObsID ``folder`` that :func:`obsid_folder` returned: the
    path part above the mission's :data:`OBSID_TREES` pattern, else the nearest folder above it
    holding a ``catalog.json``, else the folder's parent."""
    import fnmatch

    parts = Path(folder).parts
    for mission, patterns in OBSID_TREES.items():
        for pattern in patterns:
            tail = pattern.format(obsid=parts[-1] if parts else "").split("/")
            if (len(parts) > len(tail) and parts[-len(tail) - 1].lower() == mission
                    and all(fnmatch.fnmatchcase(p, t) for p, t in zip(parts[-len(tail):], tail))):
                return parts[-len(tail) - 1]
    for parent in Path(folder).parents:  # a folder a catalog record names outside the tree
        if (parent / CATALOG_FILENAME).is_file():
            return parent.name
    return Path(folder).parent.name


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
    path: str | Path | None = None,
) -> ObservationsConfig:
    """Load and validate observation locations from YAML (``None`` = the configured one)."""

    path = Path(path) if path is not None else observations_yaml()
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
    path = orchestration_yaml()
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
    config_path: str | Path | None = None,
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
    ``True``), and so is a root with no mission yet, provided the root exists and is writable
    (H2). Registered in ``auto_research.preflight``.
    """
    tk_root = Path(toolkit_root).expanduser() if toolkit_root is not None else _toolkit_root()
    healthcheck = _import_healthcheck(tk_root)
    cfg_all = load_observations_config(config_path)
    # An empty root is a healthy one (H2): `acquire` creates each mission's folder and catalog on
    # its first observation, so the root itself must exist and take writes.
    root = cfg_all.obs_db_root
    if not root.is_dir():
        raise RuntimeError(
            f"obs_db_root {root} (config/observations.yaml) is not a directory. Create it "
            "(an empty folder is a healthy observation database), or point obs_db_root at the "
            "folder that holds the missions."
        )
    if not os.access(root, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"obs_db_root {root} is not writable by this user. Grant write access to it: acquire "
            "creates each mission's folder and catalog there on its first observation."
        )
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
