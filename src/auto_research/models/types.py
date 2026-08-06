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
    """One provider-native tool/function call extracted from a model response.

    ``provider_fields`` carries the provider's own record of the call, so a later turn can hand
    it back unchanged. The three fields above are what *we* need to execute a call; a provider
    may attach more that *it* needs to resolve one — and re-typing only our three silently
    discarded the rest. Two live cases: OpenAI's ``namespace`` (a tool discovered through tool
    search does not live in the default namespace, so a replay without it names a tool the API
    cannot find), and Gemini's ``thought_signature``, which rides on the same part as the call
    and must be echoed back or the second call of any loop hard-400s ([I8]). Adapters that set
    nothing here are unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    provider_fields: dict[str, Any] = Field(default_factory=dict)


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


class ToolResultMessage(BaseModel):
    """A tool-result conversation turn fed back to the model (for ReAct replay)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["tool"] = "tool"
    results: list[ToolResultPart] = Field(default_factory=list)


class ModelResponse(BaseModel):
    """One **provider-authored assistant turn** — the neutral analogue of Anthropic
    ``Message.content``, OpenAI ``response.output`` and Google ``Content.parts``.

    The type flows in both directions: an adapter returns it from ``call``, and
    ``AgentRole._react`` appends it straight back onto the conversation, where the same adapter
    translates it into that provider's assistant-turn shape. So **the turn we replay is the turn
    we received**, not a re-typed copy of it — both 2026-08-03 run-killers were fields lost in
    such a copy, and the hand-copy this replaced had already dropped one part silently.

    The neutral conversation is split by **authorship**, not by content: ``ModelMessage`` and
    ``ToolResultMessage`` are ours and we own their shape; this one is the provider's, so we
    carry it whole. Interpreted parts, which the harness acts on: ``content`` (text/message
    parts, joined), ``tool_calls`` (tool_use/function_call parts, each keeping its own verbatim
    record), and ``parsed`` (provider-parsed structured output, when a ``schema`` was declared).
    Everything else rides verbatim on ``provider_parts``.

    **Carry on ingest; the adapter decides what is legal to send.** Capturing a part never
    obliges an adapter to replay it into every request: a part that references a tool cannot go
    into a request that declares none (a live 400 on 2026-07-29), and OpenAI projects each item
    into its accepted *input* shape at capture time.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["assistant"] = "assistant"
    content: str = ""
    parsed: Any | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider_parts: list[dict[str, Any]] = Field(default_factory=list)
    """Every part of this turn we do **not** interpret, verbatim and in the order the provider
    emitted them, so a later request can hand them back.

    Anthropic calls these content *blocks*, OpenAI *items*, Google *parts*. The set is open by
    construction because it is never complete: ``anthropic 0.104.1``'s ``ContentBlock`` union
    already lacks the documented ``fallback`` block, so any allowlist would be stale on arrival —
    an unrecognised part rides along instead of going missing.

    Carrying them is a documented requirement, not an optimization. Anthropic: *"when you return a
    tool result, the thinking blocks from the assistant message must come back with it"*, and
    *"rebuilding the message or filtering out `redacted_thinking` blocks triggers a 400 error"*
    (platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows). OpenAI rejects a
    ``tool_search_call`` whose ``reasoning`` item does not precede it. Both providers re-expand a
    discovered tool's definition from the tool-search parts in history, so dropping those
    de-activates the tool mid-run.

    Order is preserved for the same reason. How these sit relative to the *interpreted* parts is a
    per-provider rule and belongs to the adapter — for Anthropic it does not arise, because the
    adapter echoes the whole array rather than reassembling it."""
    usage: ModelUsage = Field(default_factory=ModelUsage)
    context_edits: list[dict[str, Any]] = Field(default_factory=list)
    """Provider-native context-management edits applied to THIS call (Anthropic context
    editing ``applied_edits``: each a plain dict with e.g. ``cleared_tool_uses`` /
    ``cleared_input_tokens``); empty when none applied. Surfaced so the run monitor can
    record when stale tool context was cleared. Live-verified 2026-07-28."""
    raw: Any = None


# Any turn that may appear in a ReAct conversation passed to ``call(messages=…)``.
ConversationMessage = ModelMessage | ModelResponse | ToolResultMessage


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
        """Call a provider model and return one normalized :class:`ModelResponse`.

        ``schema`` and ``tools`` **may be declared together** — all three providers
        accept it (live-verified 2026-08-04, and end-to-end through these adapters
        2026-08-06), and the model then either calls a tool (``tool_calls``) or emits the
        structured answer (``parsed``). Each tool is a neutral dict
        ``{"name", "description", "input_schema"}``; ``tool_choice`` is ``"auto"``,
        ``"required"`` or ``"none"``. ``"none"`` keeps the declarations in the request while
        forbidding a call — what a terminal compose over a tool trajectory declares, so the
        items it replays still resolve against declared tools.
        ``messages`` may include prior ``ModelResponse``
        / ``ToolResultMessage`` turns for ReAct-loop replay. ``cache_prefix`` asks the
        adapter to cache the stable prefix (system + tool defs) using its
        provider-native mechanism.
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

        ``schema`` (structured output) and ``tools`` (function calling) may be declared
        together; the model then either calls a tool or emits the structured answer.
        ``tool_choice`` is ``"auto"``, ``"required"`` or ``"none"`` (declare the tools,
        forbid calling them — the terminal-compose shape).
        ``messages`` may include prior ``ModelResponse`` / ``ToolResultMessage`` turns
        for ReAct-loop replay; ``cache_prefix`` requests provider-native caching of the
        stable prefix.
        """

        normalized = [
            message
            if isinstance(message, (ModelMessage, ModelResponse, ToolResultMessage))
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
