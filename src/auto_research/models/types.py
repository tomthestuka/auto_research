"""Shared model-provider types."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from auto_research.schemas import DocumentRef, FigureRef


# Who serves the model. Every name here backs a chat adapter in ``providers/``; a name absent
# from CHAT_PROVIDERS would be reachable only through a coder harness (CLAUDE.md §6/§9.3), and
# ``provider_for`` hands such a vendor a raising placeholder instead of an adapter.
#
# The last three joined the chat set on 2026-08-24, when each was given an adapter over the
# OpenAI Responses dialect it already publishes. Membership here says a chat call *reaches* the
# vendor; it says nothing about what the vendor honours. Two of the three accept a strict schema
# and ignore it, so a role that emits one is refused by its adapter rather than by this set
# (``_responses.ResponsesVendor.supports_strict_schema``).
ProviderName = Literal["anthropic", "google", "openai", "deepseek", "zai", "dashscope"]
CHAT_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "google", "openai", "deepseek", "zai", "dashscope"}
)

# Which author harness drives a ``coder`` member: the Claude Agent SDK, or the Codex app-server.
# A harness speaks exactly one wire dialect, so the legal (provider, harness) pairs are a table
# checked at launch (``sandbox.coder.harness``), never inferred at the call site.
HarnessName = Literal["claude_sdk", "codex"]
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
    """Figures the tool produced, carried at their own turn rather than through the
    call-time ``images=`` side-channel. Living at their turn, they slide into the
    stable cached prefix as the ReAct conversation grows, and every later turn (and
    the terminal report call that replays the trajectory) keeps seeing them.

    **The wire shape is each adapter's decision, not a contract of this layer.** These
    are lightweight references — no pixels — and nothing here says whether an adapter
    nests them inside the tool result or emits them as a following turn. The two are
    not interchangeable and the right answer differs per vendor: Anthropic nests,
    because a turn with an unresolved server-tool call rejects anything beside the
    ``tool_result`` blocks; Alibaba cannot, because it types its tool output as a
    required string. This docstring used to assert "an image turn adjacent to this
    tool-result turn", which read as a design invariant when it was one adapter's
    rendering choice."""


class ToolResultMessage(BaseModel):
    """A tool-result conversation turn fed back to the model (for ReAct replay)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["tool"] = "tool"
    results: list[ToolResultPart] = Field(default_factory=list)


StopClass = Literal[
    "end_turn",      # the model finished naturally — the only clean end
    "tool_use",      # it wants a tool run (Anthropic `tool_use`, everyone else `tool_calls`)
    "truncated",     # the output budget or the context window ran out mid-answer
    "refusal",       # a safety classifier declined; arrives as a normal 200, not an error
    "paused",        # a server-tool loop hit its iteration limit and wants continuing
    "transient",     # an infrastructure failure the vendor reported IN the finish-reason field
    "malformed",     # the vendor rejected its own tool call (Google MALFORMED_FUNCTION_CALL etc.)
    "unknown",       # nothing usable reported; treated like a silent turn
]
"""How a turn ended, normalised. Read per vendor from that vendor's own documented field.

The six vendors do not agree, which is why this exists rather than a shared field read:

| vendor | field | notes |
|---|---|---|
| Anthropic | `stop_reason` | `end_turn`/`max_tokens`/`stop_sequence`/`tool_use`/`pause_turn`/`refusal` — the only vendor that also **prescribes** branching on it |
| OpenAI | `status` + `incomplete_details.reason` | `incomplete` + `max_output_tokens`/`content_filter`; there is no `tool_use` value, tool intent is read off the output items |
| Google | `finishReason` | 17 values, including `MALFORMED_FUNCTION_CALL` and `UNEXPECTED_TOOL_CALL` that nobody else has |
| DeepSeek | `finish_reason` | adds **`insufficient_system_resource`** — a transient, in the terminal field |
| Z.AI | `finish_reason` | adds **`network_error`** and `sensitive`; the ecosystem maps `network_error` to `stop`, so a dropped inference reads as success |
| Alibaba | `finish_reason` | `stop`/`length`/`tool_calls` |

Sources: platform.claude.com/docs/en/build-with-claude/handling-stop-reasons;
ai.google.dev/api/generate-content#FinishReason; api-docs.deepseek.com/api/create-chat-completion;
docs.z.ai/api-reference/api-code; alibabacloud.com/help/en/model-studio/qwen-api-via-openai-responses.
"""


_OPENAI_INCOMPLETE_STOPS: dict[str, StopClass] = {
    "max_output_tokens": "truncated",
    "content_filter": "refusal",
}

_ANTHROPIC_STOPS: dict[str, StopClass] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "stop_sequence": "end_turn",
    "max_tokens": "truncated",
    "refusal": "refusal",
    "pause_turn": "paused",
    "model_context_window_exceeded": "truncated",
}

#: Every OpenAI-dialect vendor's `finish_reason`, plus the two that put an infrastructure failure
#: in it. `network_error` is Z.AI's and is deliberately classed `transient`, NOT `end_turn`, even
#: though the wider ecosystem maps it to `stop`.
_CHAT_FINISH_STOPS: dict[str, StopClass] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "truncated",
    "content_filter": "refusal",
    "sensitive": "refusal",
    "insufficient_system_resource": "transient",
    "network_error": "transient",
}

_GOOGLE_STOPS: dict[str, StopClass] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "truncated",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
    "IMAGE_SAFETY": "refusal",
    "LANGUAGE": "refusal",
    "MALFORMED_FUNCTION_CALL": "malformed",
    "UNEXPECTED_TOOL_CALL": "malformed",
    "OTHER": "unknown",
    "FINISH_REASON_UNSPECIFIED": "unknown",
}


def stop_from_anthropic(raw: Any) -> StopClass:
    """Anthropic's ``Message.stop_reason`` → :data:`StopClass`.

    The only vendor that also prescribes the loop rule: *"Check this field to decide whether to
    use the response as-is, continue the conversation, retry, or fall back to another model."*
    (platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)
    """
    return _ANTHROPIC_STOPS.get(str(getattr(raw, "stop_reason", "") or ""), "unknown")


def stop_from_openai_responses(raw: Any) -> StopClass:
    """OpenAI Responses ``status`` + ``incomplete_details.reason`` → :data:`StopClass`.

    There is no `tool_use` status: tool intent is read off the output items, so a turn carrying
    tool calls is classified by the caller, not here. `status="completed"` is the clean end.
    """
    status = str(getattr(raw, "status", "") or "")
    if status == "incomplete":
        details = getattr(raw, "incomplete_details", None)
        reason = str(getattr(details, "reason", "") or "")
        return _OPENAI_INCOMPLETE_STOPS.get(reason, "truncated")
    if status in {"failed", "cancelled"}:
        return "transient"
    if status == "completed":
        return "end_turn"
    return "unknown"


def stop_from_chat_finish_reason(reason: Any) -> StopClass:
    """A `finish_reason` from any OpenAI-dialect vendor → :data:`StopClass`.

    Covers DeepSeek's `insufficient_system_resource` and Z.AI's `network_error`, both of which
    are **infrastructure failures reported in the terminal field**. Classifying either as
    `end_turn` is what makes a dropped inference look like a finished answer.
    """
    return _CHAT_FINISH_STOPS.get(str(reason or "").strip().lower(), "unknown")


def stop_from_google(raw: Any) -> StopClass:
    """Google's candidate ``finishReason`` → :data:`StopClass` (17 documented values)."""
    candidates = getattr(raw, "candidates", None) or []
    if not candidates:
        return "unknown"
    reason = getattr(candidates[0], "finish_reason", None)
    name = getattr(reason, "name", None) or getattr(reason, "value", None) or reason
    return _GOOGLE_STOPS.get(str(name or "").strip().upper(), "unknown")


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
    stop: StopClass = "unknown"
    """Why the provider stopped generating, normalised across the six vendors (F5a).

    **Not** a terminator. The ReAct loop ends on a role's terminal tool, because a tool call means
    the same thing on every vendor while this field does not: DeepSeek reports an overloaded
    cluster as `insufficient_system_resource` and Z.AI reports a dropped inference as
    `network_error`, both **in the finish-reason field**, and the ecosystem maps the latter to
    `stop` — so on GLM a transient failure reads as a clean turn end. This field's job is to
    decide *what to say next* when a turn arrives with neither a terminal tool nor a tool call.

    Before F5a nothing read a stop reason at all: `_react` broke on `not tool_calls`, collapsing
    five outcomes into "done". Slot 3's lead returned `output_tokens: 2` with no content after
    nine consecutive tool-result turns — Anthropic documents exactly that as *"an empty response
    (exactly 2-3 tokens with no content) with `stop_reason: "end_turn"` ... particularly after
    tool results"* — and the run died because the loop read it as a finished turn.
    """
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
    # --- coder-role fields (ignored by chat roles) ---
    harness: HarnessName | None = None
    """Author harness driving this member; required for the ``coder`` role."""
    base_url: str | None = None
    """Endpoint the harness is pointed at; required when the provider is not the harness's
    native one (e.g. a DeepSeek model driven by the Codex app-server)."""
    key_env: str | None = None
    """Environment variable holding this vendor's API key. Absent means the harness
    authenticates the way it does by default (the Claude SDK's stored credentials)."""


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
        # Additive, and only when asked: with the flag unset this is one `os.environ.get` and the
        # request below is byte-identical. See `_dump_prompt` for why the dump lives HERE.
        _dump_prompt(
            role=self.role, config=self.config, messages=normalized, schema=schema,
            tools=tools, tool_choice=tool_choice, images=images, documents=documents,
        )

        from auto_research.models.keys import make_rotator
        from auto_research.models.retry import call_with_transient_retry
        from auto_research.observability import traced_model_call

        # A rotator exists only for a provider with a second key configured; a key-scoped
        # failure (rate limit, spent balance) then retries on the other key immediately
        # instead of cooling down against a quota that waiting cannot refill.
        rotate_key = make_rotator(self.config.provider)
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
            provider=self.config.provider,
            rotate_key=rotate_key,
            on_key_rotation=self._record_key_rotation if rotate_key is not None else None,
        )

    async def _record_key_rotation(self, label: str, exc: BaseException) -> None:
        """Record a key switch on the run ledger — the monitor JSONL is the sole record [J1].

        Reached through the run-scoped ``CostMeter``'s monitor for the same reason cost
        accounting is: this sits far below any run object, and a ``ContextVar`` is the only
        handle a provider call has on the run it belongs to. Silent when no run is installed
        (a bare library call, a test), and the key **value** is never recorded — only which
        slot it came from.
        """

        from auto_research.observability import current_cost_meter

        meter = current_cost_meter()
        monitor = getattr(meter, "monitor", None) if meter is not None else None
        if monitor is None:
            return
        await monitor.record(
            "run",
            "provider.key_rotation",
            level=3,
            summary=(
                f"{self.config.provider} key-scoped error on {self.role}/{self.config.name} "
                f"— switched to the {label} API key"
            ),
            data={
                "provider": self.config.provider,
                "role": self.role,
                "member": self.config.name,
                "switched_to": label,
                "error": str(exc)[:500],
            },
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


# ---------------------------------------------------------------------------
# Verbatim prompt dump — the leak scan a benchmark must pass before it spends
# ---------------------------------------------------------------------------

#: Set to a file path to append one JSON object per model call, carrying the COMPLETE input that
#: call sends. Unset (the default) it costs one dict lookup and changes nothing.
PROMPT_DUMP_ENV = "AUTO_RESEARCH_PROMPT_DUMP"


def _dump_prompt(
    *,
    role: str,
    config: "RoleModelConfig",
    messages: list,
    schema: Any | None,
    tools: Any | None,
    tool_choice: Any | None,
    images: Any,
    documents: Any,
) -> None:
    """Append the verbatim input of one model call, when ``AUTO_RESEARCH_PROMPT_DUMP`` is set.

    **Why here.** This is the single chokepoint every actor's prompt passes through, so a dump
    taken at this line is the real thing rather than a reconstruction — system prompt, user
    payload, tool definitions and schema together, for every role, with no per-caller wiring to
    keep in sync. That distinction is not academic: on 2026-08-27 a leak scan built by
    hand-reassembling the co-lead's payload proved the brief was clean and missed that the
    worktree path named the planted defect, because a hand-built approximation contains only the
    channels its author remembered. Both benchmarks had shipped a leak through a channel nobody
    reconstructed (`benchmarks/gate_bench/briefs.py`, `benchmarks/author_bench/trial.py`).

    **Why additive rather than a separate debug path.** The scan is only evidence if it reads what
    the production pipeline actually sends, so the flag must not change the request, the control
    flow, or the harness in use. Everything here happens after the request is assembled and before
    it is dispatched, writes to its own file, and swallows its own failures: a diagnostic that can
    fail a run is worse than no diagnostic.

    Serialisation is best-effort and lossy on purpose — ``default=str`` rather than a schema — since
    the reader is a human or a regex hunting for a leaked string, not a replayer.
    """
    import os

    path = os.environ.get(PROMPT_DUMP_ENV)
    if not path:
        return
    try:
        import json
        from pathlib import Path

        def _render(message: Any) -> Any:
            if hasattr(message, "model_dump"):
                return message.model_dump(mode="json")
            return message

        record = {
            "role": role,
            "member": config.name,
            "provider": config.provider,
            "model": config.model,
            # The whole conversation, in order, exactly as handed to the adapter.
            "messages": [_render(m) for m in messages],
            # Tool definitions matter as much as the payload: a provider renders their
            # descriptions into what the model reads, which is how a filesystem path becomes
            # prompt content.
            "tools": tools,
            "tool_choice": tool_choice,
            "schema": getattr(schema, "__name__", None) if schema is not None else None,
            "n_images": len(images or ()),
            "n_documents": len(documents or ()),
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001 — a diagnostic must never break a run
        pass
