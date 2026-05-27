"""OpenAI provider adapter."""

import base64
from collections.abc import Sequence

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig
from auto_research.schemas import DocumentRef, FigureRef


class OpenAIProvider:
    """Thin adapter around the OpenAI Responses API."""

    async def call(
        self,
        messages: Sequence[ModelMessage],
        config: RoleModelConfig,
        *,
        schema: type[BaseModel] | None = None,
        images: Sequence[FigureRef] = (),
        documents: Sequence[DocumentRef] = (),
    ) -> ModelResponse:
        """Call OpenAI and normalize the response."""

        from openai import AsyncOpenAI

        if schema is not None:
            raise NotImplementedError("structured output is wired in the agent layer later")

        input_items = [
            {
                "role": message.role,
                "content": [{"type": "input_text", "text": message.content}],
            }
            for message in messages
        ]
        attachment_content = []
        for document in documents:
            data = base64.b64encode(document.path.read_bytes()).decode("ascii")
            attachment_content.append(
                {
                    "type": "input_file",
                    "filename": document.path.name,
                    "file_data": f"data:{document.mime_type};base64,{data}",
                }
            )
        for figure in images:
            attachment_content.append(
                {
                    "type": "input_text",
                    "text": f"Figure reference: {figure.path}",
                }
            )
        if attachment_content:
            input_items.append({"role": "user", "content": attachment_content})

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
