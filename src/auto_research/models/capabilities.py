"""What each model takes, read from ``config/model_capabilities.yaml`` (Z1d, user ruling 2026-11-04).

The table is the one source of a model's media capability: the builders write each entry from the
vendor's own model page (citations in ``config/DOCS.md``), and the adapters trust it. The adapters
keep only vendor-wide wire behaviour and documented API limits. The table describes models; a
member only names one, so no member carries a capability flag of its own (A1's retirement stands).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import yaml

from auto_research.config_paths import model_capabilities_yaml


@dataclass(frozen=True)
class ModelCapability:
    """One table entry: ``images`` (the model reads image input), ``documents`` (it reads a PDF
    natively)."""

    images: bool
    documents: bool


class MissingModelCapability(ValueError):
    """A member names a model the capability table does not describe."""


_FIELDS = ("images", "documents")


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a key given twice (a second entry would silently win)."""


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    keys = [loader.construct_object(key, deep=deep) for key, _ in node.value]
    duplicate = next((key for key in keys if keys.count(key) > 1), None)
    if duplicate is not None:
        raise ValueError(f"{duplicate!r} is given twice")
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)
_CACHE: dict[tuple[str, int, int], dict[tuple[str, str], ModelCapability]] = {}


def load_capabilities(path: Path | None = None) -> dict[tuple[str, str], ModelCapability]:
    """Every entry of the table, keyed by ``(provider, model)``; re-read when the file changes.

    A malformed entry raises ``ValueError`` naming it: an entry is exactly ``images`` and
    ``documents``, both booleans."""
    from auto_research.models.types import ProviderName

    path = Path(path) if path is not None else model_capabilities_yaml()
    if not path.is_file():
        raise MissingModelCapability(
            f"{path}: the model capability table is not there; copy config/model_capabilities.yaml "
            "into this configuration directory (it describes models and is the same for every run)."
        )
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader) or {}
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a mapping of provider -> model -> capability")
    providers = set(get_args(ProviderName))
    table: dict[tuple[str, str], ModelCapability] = {}
    for provider, models in data.items():
        if provider not in providers:
            raise ValueError(f"{path}: {provider!r} is not a provider; use one of {sorted(providers)}")
        if not isinstance(models, dict):
            raise ValueError(f"{path}: {provider!r} maps model ids to capability entries")
        for model, entry in models.items():
            if (not isinstance(entry, dict) or set(entry) != set(_FIELDS)
                    or not all(isinstance(entry[name], bool) for name in _FIELDS)):
                raise ValueError(
                    f"{path}: {provider}/{model} needs exactly `images` and `documents`, each true "
                    f"or false; it reads {entry!r}"
                )
            table[(str(provider), str(model))] = ModelCapability(**entry)
    _CACHE.clear()
    _CACHE[key] = table
    return table


def capability_for(provider: str, model: str) -> ModelCapability:
    """The table's entry for ``provider``/``model``; :class:`MissingModelCapability` names the file
    and what to add when there is none."""
    entry = load_capabilities().get((provider, model))
    if entry is None:
        raise MissingModelCapability(
            f"{provider}/{model}: add `{model}: {{images: ..., documents: ...}}` under `{provider}:` "
            f"in {model_capabilities_yaml()}, from the vendor's own model page, with its citation "
            "in config/DOCS.md (Model capabilities)."
        )
    return entry


def check_member_capabilities() -> None:
    """Launch preflight: every member ``models.yaml`` configures names a model the capability
    table describes, so no request is judged against a guess."""
    from auto_research.models.registry import load_registry

    registry = load_registry()
    problems = []
    for role, group in sorted(registry.config.roles.items()):
        for member in group.members:
            try:
                capability_for(member.provider, member.model)
            except MissingModelCapability as exc:
                problems.append(f"role {role}, member {member.name}: {exc}")
    if problems:
        raise MissingModelCapability("; ".join(problems))


__all__ = [
    "MissingModelCapability",
    "ModelCapability",
    "capability_for",
    "check_member_capabilities",
    "load_capabilities",
]
