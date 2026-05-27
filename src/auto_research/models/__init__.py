"""Provider abstraction + role-to-model registry."""

from auto_research.models.registry import DEFAULT_CONFIG_PATH, REQUIRED_ROLES, ModelRegistry, load_registry, resolve
from auto_research.models.types import (
    ModelGroupHandle,
    ModelHandle,
    ModelMessage,
    ModelProvider,
    ModelResponse,
    ModelUsage,
    ModelsConfig,
    ProviderName,
    RoleModelConfig,
    RoleModelGroup,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "REQUIRED_ROLES",
    "ModelHandle",
    "ModelGroupHandle",
    "ModelMessage",
    "ModelProvider",
    "ModelRegistry",
    "ModelResponse",
    "ModelUsage",
    "ModelsConfig",
    "ProviderName",
    "RoleModelConfig",
    "RoleModelGroup",
    "load_registry",
    "resolve",
]
