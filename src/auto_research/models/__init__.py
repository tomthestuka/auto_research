"""Provider abstraction + role-to-model registry."""

from auto_research.config_paths import models_yaml
from auto_research.models.registry import REQUIRED_ROLES, ModelRegistry, load_registry, resolve
from auto_research.models.types import (
    ConversationMessage,
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
    ToolCall,
    ToolResultMessage,
    ToolResultPart,
)

__all__ = [
    "models_yaml",
    "REQUIRED_ROLES",
    "ConversationMessage",
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
    "ToolCall",
    "ToolResultMessage",
    "ToolResultPart",
    "load_registry",
    "resolve",
]
