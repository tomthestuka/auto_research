"""OpenAI provider adapter."""

from collections.abc import Sequence

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig
from auto_research.schemas import FigureRef


class OpenAIProvider:
    """Thin adapter around the OpenAI Responses API."""

    async def call(
        self,
        messages: Sequence[ModelMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
    ) -> ModelResponse:
        """Call OpenAI and normalize the response."""

        from openai import AsyncOpenAI

        if schema is not None:
            raise NotImplementedError("structured output is wired in the agent layer later")

        input_items = [{"role": message.role, "content": message.content} for message in messages]
        if images:
            input_items.append(
                {
                    "role": "user",
                    "content": "\n".join(f"Figure: {figure.path}" for figure in images),
                }
            )

        response = await AsyncOpenAI().responses.create(
            model=config.model,
            input=input_items,
            **config.parameters,
        )
        usage = getattr(response, "usage", None)
        return ModelResponse(
            content=getattr(response, "output_text", ""),
            usage=ModelUsage(
                input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
                output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            ),
            raw=response,
        )
