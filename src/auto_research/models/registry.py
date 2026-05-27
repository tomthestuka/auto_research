"""Config-driven role-to-model registry."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from auto_research.models.providers import provider_for
from auto_research.models.types import ModelGroupHandle, ModelHandle, ModelsConfig


REQUIRED_ROLES = frozenset(
    {
        "reviewer",
        "analyst_lead",
        "analyst_worker",
        "librarian_io",
        "librarian_searcher",
    }
)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "models.yaml"


class ModelRegistry:
    """Validated model registry loaded from YAML."""

    def __init__(self, config: ModelsConfig):
        self.config = config

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "ModelRegistry":
        """Load and validate a model registry from YAML."""

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


def load_registry(path: str | Path = DEFAULT_CONFIG_PATH) -> ModelRegistry:
    """Load the default model registry."""

    return ModelRegistry.from_file(path)


def resolve(role: str, path: str | Path = DEFAULT_CONFIG_PATH) -> ModelGroupHandle:
    """Resolve `role` from a model config file."""

    try:
        return load_registry(path).resolve(role)
    except ValidationError:
        raise
