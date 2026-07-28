"""Anthropic provider adapter."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.env import context_editing_config, tool_search_config
from auto_research.models.providers._tool_search import (
    TOMBSTONE_DESCRIPTION,
    missing_tool_names,
)
from auto_research.models.types import (
    AssistantToolUse,
    ConversationMessage,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
)
from auto_research.schemas import DocumentRef, FigureRef


_DISCOVERY_BLOCK_TYPES = frozenset({"server_tool_use", "tool_search_tool_result"})
"""Tool-search blocks the server produces and that must be replayed verbatim, so the API can
re-expand the referenced tool definitions from the conversation history on every later turn."""


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Best-effort plain-dict view of an SDK content block (for verbatim replay)."""
    for attr in ("model_dump", "to_dict", "dict"):
        dumper = getattr(block, attr, None)
        if callable(dumper):
            try:
                return dict(dumper(mode="json") if attr == "model_dump" else dumper())
            except TypeError:
                try:
                    return dict(dumper())
                except Exception:  # noqa: BLE001 — fall through to the generic path
                    pass
            except Exception:  # noqa: BLE001
                pass
    if isinstance(block, dict):
        return dict(block)
    return {"type": getattr(block, "type", None)}


class AnthropicProvider:
    """Thin adapter around Anthropic messages.

    A ``schema`` request uses Anthropic's **native structured output** via
    ``messages.parse(output_format=schema)``: the SDK transforms the Pydantic
    model into a strict ``json_schema`` output format (dropping unsupported
    keywords, inlining ``$defs``, forcing ``additionalProperties:false``) and
    constrains the model to emit conforming JSON, then validates it into an
    instance. We return that instance as a plain dict in ``ModelResponse.parsed``
    for parity with the OpenAI/Google adapters.

    A ``tools`` request is native function calling via ``messages.create``.
    ``messages`` may interleave ``AssistantToolUse`` / ``ToolResultMessage`` turns
    (translated to native ``tool_use`` / ``tool_result`` content blocks) so a
    ReAct loop can replay its trajectory. ``cache_prefix`` sets Anthropic-native
    ``cache_control: ephemeral`` breakpoints on the stable prefix (the last system
    block, which also caches the tool defs that render before it; or the last tool
    when there is no system) and on the rolling conversation tail.
    """

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
        """Call Anthropic and normalize the response."""

        from anthropic import AsyncAnthropic

        if schema is not None and tools is not None:
            raise ValueError("schema and tools are mutually exclusive")

        params = dict(config.parameters)
        max_tokens = params.pop("max_tokens", 1024)
        system, provider_messages = _to_anthropic_messages(messages)

        # Documents (e.g. PDFs the reviewer opens) go at the FRONT — a stable position
        # right after the system prompt — and grow append-only across a ReAct loop, so
        # each document is cache-written once and cache-read (~0.1x) on every later turn.
        # Re-sending them at the END (after a conversation that grows in the middle) breaks
        # the cache prefix right after `system` and re-bills every document each turn.
        # The cache breakpoint on the last document block, plus the stable [system + docs]
        # prefix, is what lets Anthropic serve the prior documents from cache as new ones
        # accumulate. (Anthropic prompt-caching: prefix match, ≤4 breakpoints, 20-block
        # lookback — the front position also keeps docs inside that lookback window.)
        if documents:
            document_blocks: list[dict[str, Any]] = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": document.mime_type,
                        "data": base64.b64encode(document.path.read_bytes()).decode("ascii"),
                    },
                }
                for document in documents
            ]
            if cache_prefix:
                document_blocks[-1]["cache_control"] = {"type": "ephemeral"}
            provider_messages.insert(0, {"role": "user", "content": document_blocks})

        # Cross-agent figures (peer plots, a worker's figures the reviewer must see) have no
        # tool-turn in *this* conversation to attach to, so they ride this call-time channel —
        # a caption block then the image block, at the end. (Figures a tool produced *this*
        # loop instead live inline at their tool-result turn; see `_to_anthropic_messages`.)
        if images:
            image_content: list[dict[str, Any]] = []
            for figure in images:
                image_content.extend(_figure_blocks(figure))
            provider_messages.append({"role": "user", "content": image_content})

        system_param = _system_param(system, cache_prefix)
        if cache_prefix:
            _mark_last_block_cacheable(provider_messages)

        # Provider-native context editing (Anthropic beta): on the cache_prefix paths — the
        # ReAct tool loop and the terminal report call — clear the oldest tool RESULTS
        # server-side once the request crosses the token trigger, keeping the last N. This is
        # a context-window pressure valve for long loops (with prompt caching, raw history is
        # cheap and any clear breaks the cached prefix, so clear_at_least amortizes it). Routed
        # through the beta.messages namespace only when active, so every other call stays
        # byte-identical to before. NOT LIVE TESTED (2026-07-22 — pending API credits).
        ctx_cfg = context_editing_config()
        use_ctx_edit = cache_prefix and ctx_cfg.enabled
        ctx_extra: dict[str, Any] = {}
        if use_ctx_edit:
            ctx_extra["betas"] = [ctx_cfg.beta_header]
            ctx_extra["context_management"] = {
                "edits": [
                    {
                        "type": ctx_cfg.strategy,
                        "trigger": {"type": "input_tokens", "value": ctx_cfg.trigger_tokens},
                        "keep": {"type": "tool_uses", "value": ctx_cfg.keep_tool_uses},
                        "clear_at_least": {
                            "type": "input_tokens",
                            "value": ctx_cfg.clear_at_least_tokens,
                        },
                    }
                ]
            }

        client = AsyncAnthropic()
        # beta.messages accepts betas + context_management; its response carries the same
        # content/usage shape, and ParsedBetaMessage.parsed_output is a property mirroring the
        # non-beta parse — so the extraction below is identical on either namespace.
        messages_api = client.beta.messages if use_ctx_edit else client.messages

        if schema is not None:
            response = await messages_api.parse(
                model=config.model,
                max_tokens=max_tokens,
                messages=provider_messages,
                output_format=schema,
                **({"system": system_param} if system is not None else {}),
                **params,
                **ctx_extra,
            )
            parsed_obj = response.parsed_output
            text_parts = [
                block.text for block in response.content if getattr(block, "type", None) == "text"
            ]
            usage = getattr(response, "usage", None)
            return ModelResponse(
                content="".join(text_parts),
                parsed=parsed_obj.model_dump(mode="json") if parsed_obj is not None else None,
                usage=_anthropic_usage(usage),
                context_edits=_context_edits(response),
                raw=response,
            )

        request: dict[str, Any] = {
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": provider_messages,
            **params,
        }
        if system is not None:
            request["system"] = system_param
        if tools is not None:
            ts_cfg = tool_search_config()
            tool_defs = []
            for tool in tools:
                entry: dict[str, Any] = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
                # Deferred tools stay out of the system-prompt prefix; Claude pulls one in
                # via the search tool, which appends a ``tool_reference`` inline in the
                # conversation, so the cached prefix is untouched. NOT LIVE TESTED (2026-07-27).
                if ts_cfg.enabled and tool.get("defer_loading"):
                    entry["defer_loading"] = True
                tool_defs.append(entry)
            if ts_cfg.enabled:
                # Tombstones for tools this conversation already discovered that have since
                # left the catalog — without them the API 400s on the dangling reference.
                # Checked independently of whether any *current* tool is deferred: the
                # vanished tool may have been the only deferred one (exactly the failed-
                # authoring case), and the stale reference in history still needs resolving.
                gone_names = missing_tool_names(
                    messages, [t["name"] for t in tool_defs if "name" in t]
                )
                for gone in gone_names:
                    tool_defs.append(
                        {
                            "name": gone,
                            "description": TOMBSTONE_DESCRIPTION,
                            "input_schema": {"type": "object", "properties": {}},
                            "defer_loading": True,
                        }
                    )
                if any(t.get("defer_loading") for t in tool_defs):
                    # Anthropic requires ≥1 non-deferred tool; the harness tools are it.
                    tool_defs.append({"type": ts_cfg.anthropic_tool, "name": "tool_search"})
            # When there is no system block to carry the breakpoint, cache the tool
            # defs directly (they render first, so this caches the whole prefix). The
            # breakpoint must land on a NON-deferred tool: a deferred tool carrying
            # ``cache_control`` is a 400, since it is not part of the prefix being cached.
            if cache_prefix and system is None and tool_defs:
                resident = [t for t in tool_defs if not t.get("defer_loading")]
                if resident:
                    resident[-1]["cache_control"] = {"type": "ephemeral"}
            request["tools"] = tool_defs
            request["tool_choice"] = {"type": "any"} if tool_choice == "required" else {"type": "auto"}

        response = await messages_api.create(**request, **ctx_extra)

        text_parts = []
        tool_calls: list[ToolCall] = []
        provider_blocks: list[dict[str, Any]] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )
            elif btype in _DISCOVERY_BLOCK_TYPES:
                # Tool-search discovery: the server ran the search and returned
                # ``tool_reference``s. These must be replayed verbatim on every later
                # request — the API re-expands the referenced definitions from history, so
                # dropping them de-activates the discovered tool. Never answer the
                # ``srvtoolu_…`` id with a tool_result; the API rejects that.
                provider_blocks.append(_block_to_dict(block))

        usage = getattr(response, "usage", None)
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=_anthropic_usage(usage),
            context_edits=_context_edits(response),
            provider_blocks=provider_blocks,
            raw=response,
        )


def _anthropic_usage(usage: Any) -> ModelUsage:
    """Map an Anthropic ``usage`` object into the neutral non-overlapping buckets ([I5]).

    Anthropic's ``input_tokens`` is already the *uncached* remainder; cached volume is reported
    separately as ``cache_read_input_tokens`` (~0.1x) and ``cache_creation_input_tokens`` (~1.25x),
    so no subtraction is needed. Thinking is billed as output and already counted in
    ``output_tokens``, so ``reasoning_tokens`` stays 0.
    """
    if usage is None:
        return ModelUsage()
    return ModelUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _context_edits(response: Any) -> list[dict[str, Any]]:
    """Extract the provider-native context-management edits applied to ``response``.

    Anthropic reports cleared context under ``response.context_management.applied_edits``
    (present only on the beta context-management path; each edit e.g.
    ``{"type": "clear_tool_uses_20250919", "cleared_tool_uses": N, "cleared_input_tokens": M}``).
    Returns ``[]`` when the field is absent (every non-beta call). NOT LIVE TESTED (2026-07-22).
    """
    cm = getattr(response, "context_management", None)
    applied = getattr(cm, "applied_edits", None) if cm is not None else None
    if not applied:
        return []
    edits: list[dict[str, Any]] = []
    for edit in applied:
        try:
            edits.append(edit.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 — a report we cannot serialize must never break the call
            edits.append({"type": getattr(edit, "type", "unknown")})
    return edits


def _figure_blocks(figure: FigureRef) -> list[dict[str, Any]]:
    """Render one figure as Anthropic content blocks: a caption then the image bytes.

    Loading bytes here is the multimodal boundary — the only place a figure's pixels
    enter a request (elsewhere figures travel as lightweight ``FigureRef``). The caption is
    rendered verbatim: the presentation layer may prefix it with the report-local number
    ("Figure 1 — …") or the catalog handle ("[analyst_worker_0:2] …"), so prose references
    line up with the images (V-4).
    """
    return [
        {"type": "text", "text": f"{figure.caption} (produced by {figure.produced_by})"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": figure.mime_type,
                "data": base64.b64encode(figure.path.read_bytes()).decode("ascii"),
            },
        },
    ]


def _to_anthropic_messages(
    messages: Sequence[ConversationMessage],
) -> tuple[str | None, list[dict]]:
    """Split system text out and translate the rest into Anthropic message dicts.

    ``AssistantToolUse`` → an assistant message with ``tool_use`` blocks;
    ``ToolResultMessage`` → a user message with ``tool_result`` blocks; plain
    ``ModelMessage`` → a text message (system text is joined and returned).
    """
    system_texts: list[str] = []
    provider_messages: list[dict] = []
    for message in messages:
        if isinstance(message, AssistantToolUse):
            blocks: list[dict] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            # Tool-search discovery blocks replay verbatim, positioned after the assistant
            # text and before the tool_use blocks — the order the server produced them in.
            blocks.extend(
                b for b in message.provider_blocks
                if b.get("type") in _DISCOVERY_BLOCK_TYPES
            )
            for call in message.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            provider_messages.append({"role": "assistant", "content": blocks})
        elif isinstance(message, ToolResultMessage):
            content: list[dict] = [
                {
                    "type": "tool_result",
                    "tool_use_id": part.tool_call_id,
                    "content": part.content,
                    "is_error": part.is_error,
                }
                for part in message.results
            ]
            # A tool's figures live inline at this turn: caption+image blocks appended after
            # the tool_result blocks in the SAME user message (Anthropic allows mixed content
            # there), so they replay in place every round and cache with the growing prefix.
            for part in message.results:
                for figure in part.figures:
                    content.extend(_figure_blocks(figure))
            provider_messages.append({"role": "user", "content": content})
        elif message.role == "system":
            system_texts.append(message.content)
        else:
            provider_messages.append({"role": message.role, "content": message.content})
    return ("\n".join(system_texts) or None), provider_messages


def _system_param(system: str | None, cache_prefix: bool) -> Any:
    """Return the ``system`` request value, marking it cacheable when requested."""
    if system is None:
        return None
    if cache_prefix:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def _mark_last_block_cacheable(provider_messages: list[dict]) -> None:
    """Mark the current Anthropic conversation tail as a prompt-cache breakpoint."""
    if not provider_messages:
        return
    content = provider_messages[-1].get("content")
    cache_control = {"type": "ephemeral"}
    if isinstance(content, str):
        provider_messages[-1]["content"] = [
            {"type": "text", "text": content, "cache_control": cache_control}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = cache_control
