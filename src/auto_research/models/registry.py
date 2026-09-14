"""Config-driven role-to-model registry."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from auto_research.config_paths import models_yaml
from auto_research.models.providers import provider_for
from auto_research.models.types import ModelGroupHandle, ModelHandle, ModelsConfig


REQUIRED_ROLES = frozenset(
    {
        "reviewer",
        "analyst_lead",
        "analyst_worker",
        "librarian_io",
        # Phase-2 tool-authoring roles — required now that the gated author→review flow is
        # built. Non-required roles will be re-introduced only as the system grows.
        "coder",
        "analyst_colead",
        # The `/status` reporter. Required because it is the only way a human sees inside a
        # live run: with no deterministic fallback left, a config missing this role must fail
        # at launch rather than at the moment someone reaches for it.
        "status_monitor",
        # Summarises a finished authoring link for the lead. Required because a link's outcome
        # is what the next brief is written against: a config without it would degrade every
        # link to the deterministic facts alone, silently and for the whole run.
        "compactor",
    }
)


class ModelRegistry:
    """Validated model registry loaded from YAML."""

    def __init__(self, config: ModelsConfig):
        self.config = config

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "ModelRegistry":
        """Load and validate a model registry from YAML.

        ``path=None`` reads the configured ``models.yaml``, resolved per call: a default argument
        would be evaluated at import and pin the answer before a process could be pointed at
        another configuration.
        """

        path = Path(path) if path is not None else models_yaml()
        with Path(path).open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
        config = ModelsConfig.model_validate(data)
        missing = REQUIRED_ROLES - set(config.roles)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(f"models config missing required roles: {missing_list}")
        return cls(config)

    def resolve(self, role: str) -> ModelGroupHandle:
        """Resolve a role to its configured model group."""

        try:
            group = self.config.roles[role]
        except KeyError as exc:
            raise KeyError(f"unknown model role: {role}") from exc
        handles = tuple(
            ModelHandle(role=role, config=member, provider=provider_for(member.provider))
            for member in group.members
        )
        return ModelGroupHandle(role=role, handles=handles)


def load_registry(path: str | Path | None = None) -> ModelRegistry:
    """Load the configured model registry (``None`` = whatever this process is configured with)."""

    return ModelRegistry.from_file(path)


def resolve(role: str, path: str | Path | None = None) -> ModelGroupHandle:
    """Resolve `role` from a model config file (``None`` = the configured one)."""

    try:
        return load_registry(path).resolve(role)
    except ValidationError:
        raise
