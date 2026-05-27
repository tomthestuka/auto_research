"""Google Gemini provider adapter."""

from collections.abc import Sequence

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig
from auto_research.schemas import DocumentRef, FigureRef


class GoogleProvider:
    """Thin adapter around Google Gen AI models."""

    async def call(
        self,
        messages: Sequence[ModelMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
    ) -> ModelResponse:
        """Call Google and normalize the response."""

        from google import genai
        from google.genai import types

        if schema is not None:
            raise NotImplementedError("structured output is wired in the agent layer later")

        contents: list[str | object] = [
            "\n\n".join(f"{message.role}: {message.content}" for message in messages)
        ]
        for document in documents:
            contents.append(types.Part.from_bytes(data=document.path.read_bytes(), mime_type=document.mime_type))
        if images:
            contents.append("\n".join(f"Figure reference: {figure.path}" for figure in images))

        params = dict(config.parameters)
        if "thinking_config" in params:
            params["thinking_config"] = types.ThinkingConfig(**params["thinking_config"])
        generation_config = types.GenerateContentConfig(**params) if params else None

        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=config.model,
            contents=contents,
            config=generation_config,
        )
        usage = getattr(response, "usage_metadata", None)
        return ModelResponse(
            content=getattr(response, "text", ""),
            usage=ModelUsage(
                input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
            ),
            raw=response,
        )
