"""Provider abstraction + role-to-model registry."""

from auto_research.models.registry import DEFAULT_CONFIG_PATH, REQUIRED_ROLES, ModelRegistry, load_registry, resolve
from auto_research.models.types import (
    AssistantToolUse,
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
    "DEFAULT_CONFIG_PATH",
    "REQUIRED_ROLES",
    "AssistantToolUse",
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
