"""Anthropic provider adapter."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig, ToolCall
from auto_research.schemas import DocumentRef, FigureRef


class AnthropicProvider:
    """Thin adapter around Anthropic messages.

    A ``schema`` request uses Anthropic's **native structured output** via
    ``messages.parse(output_format=schema)``: the SDK transforms the Pydantic
    model into a strict ``json_schema`` output format (dropping unsupported
    keywords, inlining ``$defs``, forcing ``additionalProperties:false``) and
    constrains the model to emit conforming JSON, then validates it into an
    instance. We return that instance as a plain dict in ``ModelResponse.parsed``
    for parity with the OpenAI/Google adapters. This replaces the older
    forced-single-tool workaround, which did not enforce the schema and let the
    model nest the result one level deep (``{"parameter": {...}}``).
    A ``tools`` request is native function calling via ``messages.create``.
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

        client = AsyncAnthropic()

        if schema is not None:
            response = await client.messages.parse(
                model=config.model,
                max_tokens=max_tokens,
                messages=provider_messages,
                output_format=schema,
                **({"system": system} if system is not None else {}),
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
            request["system"] = system
        if tools is not None:
            request["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
                for tool in tools
            ]
            request["tool_choice"] = {"type": "any"} if tool_choice == "required" else {"type": "auto"}

        response = await client.messages.create(**request)

        text_parts = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(ToolCall(name=block.name, arguments=dict(block.input or {})))

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
