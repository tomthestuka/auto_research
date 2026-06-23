"""Anthropic provider adapter."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

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

        # Figure references are lightweight text; leave them at the end of the conversation.
        if images:
            provider_messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Figure reference: {figure.path}"}
                        for figure in images
                    ],
                }
            )

        system_param = _system_param(system, cache_prefix)
        if cache_prefix:
            _mark_last_block_cacheable(provider_messages)
        client = AsyncAnthropic()

        if schema is not None:
            response = await client.messages.parse(
                model=config.model,
                max_tokens=max_tokens,
                messages=provider_messages,
                output_format=schema,
                **({"system": system_param} if system is not None else {}),
                **params,
            )
            parsed_obj = response.parsed_output
            text_parts = [
                block.text for block in response.content if getattr(block, "type", None) == "text"
            ]
            usage = getattr(response, "usage", None)
            return ModelResponse(
                content="".join(text_parts),
                parsed=parsed_obj.model_dump(mode="json") if parsed_obj is not None else None,
                usage=ModelUsage(
                    input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                    output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
                ),
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
            tool_defs = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
                for tool in tools
            ]
            # When there is no system block to carry the breakpoint, cache the tool
            # defs directly (they render first, so this caches the whole prefix).
            if cache_prefix and system is None and tool_defs:
                tool_defs[-1]["cache_control"] = {"type": "ephemeral"}
            request["tools"] = tool_defs
            request["tool_choice"] = {"type": "any"} if tool_choice == "required" else {"type": "auto"}

        response = await client.messages.create(**request)

        text_parts = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )

        usage = getattr(response, "usage", None)
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=ModelUsage(
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            ),
            raw=response,
        )


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
            for call in message.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            provider_messages.append({"role": "assistant", "content": blocks})
        elif isinstance(message, ToolResultMessage):
            provider_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": part.tool_call_id,
                            "content": part.content,
                            "is_error": part.is_error,
                        }
                        for part in message.results
                    ],
                }
            )
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
