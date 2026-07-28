"""Shared model-provider types."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from auto_research.schemas import DocumentRef, FigureRef


ProviderName = Literal["anthropic", "google", "openai"]
MessageRole = Literal["system", "user", "assistant"]


class ModelMessage(BaseModel):
    """One text message sent to an LLM provider."""

    model_config = ConfigDict(extra="forbid")

    role: MessageRole
    content: str = Field(min_length=1)


class ModelUsage(BaseModel):
    """Provider-neutral token usage for one model call.

    Token classes are **non-overlapping** so cost is a plain weighted sum (no
    subtraction at pricing time). Each adapter normalizes its provider's usage
    object into these buckets ([I5]):

    - ``input_tokens`` — full-price *uncached* prompt tokens. Anthropic reports
      this directly; OpenAI/Google report a total that includes the cached
      subset, so those adapters subtract ``cache_read_tokens``.
    - ``cache_read_tokens`` — prompt tokens served from the provider cache
      (billed ~0.1x input).
    - ``cache_write_tokens`` — prompt tokens written to the cache (Anthropic
      cache-creation, billed ~1.25x input; 0 where the provider caches
      implicitly with no write premium).
    - ``output_tokens`` — all generated tokens, **including** reasoning/thinking
      (providers bill thinking as output and count it here).
    - ``reasoning_tokens`` — the reasoning/thinking subset of ``output_tokens``,
      informational only (already priced within ``output_tokens``).
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class ToolCall(BaseModel):
    """One provider-native tool/function call extracted from a model response."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultPart(BaseModel):
    """One tool result, correlated to a prior :class:`ToolCall`.

    Anthropic/OpenAI correlate by ``tool_call_id``; Google correlates by tool
    ``name``, so both are carried (the ReAct loop fills them from the originating
    ``ToolCall``).
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = ""
    name: str = ""
    content: str = ""
    is_error: bool = False
    figures: list[FigureRef] = Field(default_factory=list)
    """Figures the tool produced, carried so the adapter renders them inline in
    history — an image turn adjacent to this tool-result turn — instead of via the
    call-time ``images=`` side-channel. Living at their turn, they slide into the
    stable cached prefix as the ReAct conversation grows, and every later turn (and
    the terminal report call that replays the trajectory) keeps seeing them."""


class AssistantToolUse(BaseModel):
    """An assistant conversation turn carrying native tool calls (for ReAct replay)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["assistant"] = "assistant"
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider_blocks: list[dict[str, Any]] = Field(default_factory=list)
    """Opaque provider-native blocks from this turn that MUST be replayed verbatim on every
    later request. Today: tool-search discovery (Anthropic ``server_tool_use`` +
    ``tool_search_tool_result``; OpenAI ``tool_search_call`` + ``tool_search_output``), where
    the API re-expands the referenced tool definitions from the conversation history — so
    dropping these silently de-activates a discovered tool mid-run. Each adapter replays its
    own blocks positionally, after any assistant text and before the ``tool_use`` blocks, and
    ignores blocks it does not recognise. Empty for every non-discovery turn, which keeps the
    replay byte-identical to the pre-tool-search behaviour."""


class ToolResultMessage(BaseModel):
    """A tool-result conversation turn fed back to the model (for ReAct replay)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["tool"] = "tool"
    results: list[ToolResultPart] = Field(default_factory=list)


# Any turn that may appear in a ReAct conversation passed to ``call(messages=…)``.
ConversationMessage = ModelMessage | AssistantToolUse | ToolResultMessage


class ModelResponse(BaseModel):
    """Provider-neutral response: free text, structured output, and/or tool calls.

    A structured-output call (``schema=``) populates ``parsed`` with the
    provider-parsed object. A function-calling call (``tools=``) populates
    ``tool_calls``. A plain call populates ``content``.
    """

    model_config = ConfigDict(extra="forbid")

    content: str = ""
    parsed: Any | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    context_edits: list[dict[str, Any]] = Field(default_factory=list)
    """Provider-native context-management edits applied to THIS call (Anthropic context
    editing ``applied_edits``: each a plain dict with e.g. ``cleared_tool_uses`` /
    ``cleared_input_tokens``); empty when none applied. Surfaced so the run monitor can
    record when stale tool context was cleared. **NOT LIVE TESTED (2026-07-22).**"""
    provider_blocks: list[dict[str, Any]] = Field(default_factory=list)
    """Opaque provider-native blocks this response produced that must be carried into the
    next request — see ``AssistantToolUse.provider_blocks``. ``AgentRole._react`` copies
    these onto the assistant turn it appends, so tool-search discoveries survive replay."""
    raw: Any = None


class RoleModelConfig(BaseModel):
    """Config for one model member in an agent role group."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    provider: ProviderName
    model: str = Field(min_length=1)
    vision: bool = False
    documents: bool = False
    priceMTk: dict[str, float] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)


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
        messages: Sequence[ConversationMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str = "auto",
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
        cache_prefix: bool = False,
    ) -> ModelResponse:
        """Call a provider model and return normalized output.

        ``schema`` and ``tools`` are mutually exclusive. ``schema`` requests
        native structured output (result in ``ModelResponse.parsed``); ``tools``
        requests native function calling (result in ``ModelResponse.tool_calls``).
        Each tool is a neutral dict ``{"name", "description", "input_schema"}``.
        ``tool_choice`` is ``"auto"`` or ``"required"``. ``messages`` may include
        ``AssistantToolUse`` / ``ToolResultMessage`` turns for ReAct-loop replay.
        ``cache_prefix`` asks the adapter to cache the stable prefix (system +
        tool defs) using its provider-native mechanism.
        """


@dataclass(frozen=True)
class ModelHandle:
    """Resolved role member + config + provider adapter."""

    role: str
    config: RoleModelConfig
    provider: ModelProvider

    async def call(
        self,
        messages: Sequence[ConversationMessage | Mapping[str, str]],
        *,
        schema: type[BaseModel] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str = "auto",
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
        cache_prefix: bool = False,
    ) -> ModelResponse:
        """Call the configured provider for this role.

        ``schema`` (structured output) and ``tools`` (function calling) are
        mutually exclusive. ``messages`` may include ``AssistantToolUse`` /
        ``ToolResultMessage`` turns for ReAct-loop replay; ``cache_prefix``
        requests provider-native caching of the stable prefix.
        """

        if schema is not None and tools is not None:
            raise ValueError("schema and tools are mutually exclusive in one call")
        normalized = [
            message
            if isinstance(message, (ModelMessage, AssistantToolUse, ToolResultMessage))
            else ModelMessage.model_validate(message)
            for message in messages
        ]
        if images and not self.config.vision:
            raise ValueError(
                f"role {self.role!r} member {self.config.name!r} is not configured for vision inputs"
            )
        if documents and not self.config.documents:
            raise ValueError(
                f"role {self.role!r} member {self.config.name!r} is not configured for document inputs"
            )
        from auto_research.models.retry import call_with_transient_retry
        from auto_research.observability import traced_model_call

        return await call_with_transient_retry(
            lambda: traced_model_call(
                role=self.role,
                config=self.config,
                provider_call=lambda: self.provider.call(
                    normalized,
                    self.config,
                    schema=schema,
                    tools=tools,
                    tool_choice=tool_choice,
                    images=images,
                    documents=documents,
                    cache_prefix=cache_prefix,
                ),
            ),
            describe=f"{self.role}/{self.config.name}",
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
