"""Shared model-provider types."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from auto_research.schemas import FigureRef


ProviderName = Literal["anthropic", "google", "openai"]
MessageRole = Literal["system", "user", "assistant"]


class ModelMessage(BaseModel):
    """One text message sent to an LLM provider."""

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = Field(min_length=1)


class ModelUsage(BaseModel):
    """Provider-neutral token usage for one model call."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelResponse(BaseModel):
    """Provider-neutral text response and usage."""

    model_config = ConfigDict(extra="forbid")

    content: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
    raw: Any = None


class RoleModelConfig(BaseModel):
    """Config for one model member in an agent role group."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    provider: ProviderName
    model: str = Field(min_length=1)
    vision: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    pricing_usd_per_million_tokens: dict[str, float] = Field(default_factory=dict)


class RoleModelGroup(BaseModel):
    """Config binding an agent role to one or more concrete models."""

    model_config = ConfigDict(extra="forbid")

    members: list[RoleModelConfig] = Field(min_length=1)


class ModelsConfig(BaseModel):
    """Validated contents of `config/models.yaml`."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, RoleModelGroup]


class ModelProvider(Protocol):
    """Minimal async interface implemented by provider adapters."""

    async def call(
        self,
        messages: Sequence[ModelMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
    ) -> ModelResponse:
        """Call a provider model and return normalized output."""


@dataclass(frozen=True)
class ModelHandle:
    """Resolved role member + config + provider adapter."""

    role: str
    config: RoleModelConfig
    provider: ModelProvider

    async def call(
        self,
        messages: Sequence[ModelMessage | Mapping[str, str]],
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
    ) -> ModelResponse:
        """Call the configured provider for this role."""

        normalized = [
            message if isinstance(message, ModelMessage) else ModelMessage.model_validate(message)
            for message in messages
        ]
        if images and not self.config.vision:
            raise ValueError(
                f"role {self.role!r} member {self.config.name!r} is not configured for vision inputs"
            )
        from auto_research.observability import traced_model_call

        return await traced_model_call(
            role=self.role,
            config=self.config,
            provider_call=lambda: self.provider.call(
                normalized,
                self.config,
                schema=schema,
                images=images,
            ),
        )


@dataclass(frozen=True)
class ModelGroupHandle:
    """Resolved group of model handles for one agent role."""

    role: str
    handles: tuple[ModelHandle, ...]

    def member(self, name: str) -> ModelHandle:
        """Return a named model handle from the group."""

        for handle in self.handles:
            if handle.config.name == name:
                return handle
        raise KeyError(f"unknown model member {name!r} for role {self.role!r}")
