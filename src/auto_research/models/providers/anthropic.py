"""Anthropic provider adapter."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig, ToolCall
from auto_research.schemas import DocumentRef, FigureRef


class AnthropicProvider:
    """Thin adapter around Anthropic messages.

    Native structured output is not a first-class Anthropic feature, so a
    ``schema`` request is served by **forced single-tool use**: a synthetic tool
    whose ``input_schema`` is the Pydantic model schema, with ``tool_choice``
    pinned to it. The returned ``tool_use.input`` is the structured object
    (validated by the agent layer — Anthropic is tier-2, schema-as-hint).
    A ``tools`` request is native function calling.
    """

    async def call(
        self,
        messages: Sequence[ModelMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str = "auto",
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
    ) -> ModelResponse:
        """Call Anthropic and normalize the response."""

        from anthropic import AsyncAnthropic

        if schema is not None and tools is not None:
            raise ValueError("schema and tools are mutually exclusive")

        params = dict(config.parameters)
        max_tokens = params.pop("max_tokens", 1024)
        system = "\n".join(message.content for message in messages if message.role == "system") or None
        provider_messages = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role != "system"
        ]
        attachment_content = []
        for document in documents:
            attachment_content.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": document.mime_type,
                        "data": base64.b64encode(document.path.read_bytes()).decode("ascii"),
                    },
                }
            )
        for figure in images:
            attachment_content.append({"type": "text", "text": f"Figure reference: {figure.path}"})
        if attachment_content:
            provider_messages.append({"role": "user", "content": attachment_content})

        request: dict[str, Any] = {
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": provider_messages,
            **params,
        }
        if system is not None:
            request["system"] = system

        emit_tool_name: str | None = None
        if schema is not None:
            emit_tool_name = f"emit_{schema.__name__}"
            request["tools"] = [
                {
                    "name": emit_tool_name,
                    "description": f"Emit the result as a {schema.__name__} object.",
                    "input_schema": schema.model_json_schema(),
                }
            ]
            request["tool_choice"] = {"type": "tool", "name": emit_tool_name}
        elif tools is not None:
            request["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
                for tool in tools
            ]
            request["tool_choice"] = {"type": "any"} if tool_choice == "required" else {"type": "auto"}

        response = await AsyncAnthropic().messages.create(**request)

        text_parts: list[str] = []
        tool_uses: list[tuple[str, dict]] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_uses.append((block.name, dict(block.input or {})))

        parsed: Any | None = None
        tool_calls: list[ToolCall] = []
        if schema is not None:
            for name, inp in tool_uses:
                if name == emit_tool_name:
                    parsed = inp
                    break
        elif tools is not None:
            tool_calls = [ToolCall(name=name, arguments=inp) for name, inp in tool_uses]

        usage = getattr(response, "usage", None)
        return ModelResponse(
            content="".join(text_parts),
            parsed=parsed,
            tool_calls=tool_calls,
            usage=ModelUsage(
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            ),
            raw=response,
        )
