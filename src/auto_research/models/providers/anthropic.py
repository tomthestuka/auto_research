"""Anthropic provider adapter."""

from collections.abc import Sequence

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig
from auto_research.schemas import FigureRef


class AnthropicProvider:
    """Thin adapter around Anthropic messages."""

    async def call(
        self,
        messages: Sequence[ModelMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
    ) -> ModelResponse:
        """Call Anthropic and normalize the response."""

        from anthropic import AsyncAnthropic

        if schema is not None:
            raise NotImplementedError("structured output is wired in the agent layer later")

        params = dict(config.parameters)
        max_tokens = params.pop("max_tokens", 1024)
        system = "\n".join(message.content for message in messages if message.role == "system") or None
        provider_messages = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role != "system"
        ]
        if images:
            provider_messages.append(
                {
                    "role": "user",
                    "content": "\n".join(f"Figure: {figure.path}" for figure in images),
                }
            )

        request = {
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": provider_messages,
            **params,
        }
        if system is not None:
            request["system"] = system

        response = await AsyncAnthropic().messages.create(**request)
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        usage = getattr(response, "usage", None)
        return ModelResponse(
            content=text,
            usage=ModelUsage(
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            ),
            raw=response,
        )
