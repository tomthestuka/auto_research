"""OpenAI provider adapter."""

import base64
import json
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


class OpenAIProvider:
    """Thin adapter around the OpenAI Responses API.

    Native structured output uses ``responses.parse(text_format=schema)`` (the
    SDK converts the Pydantic model to a strict JSON schema and deserializes the
    result). Function calling passes per-tool function definitions and reads
    ``function_call`` items from the response output. ``AssistantToolUse`` /
    ``ToolResultMessage`` turns are replayed as ``function_call`` /
    ``function_call_output`` input items so a ReAct loop can carry its trajectory.
    Prefix caching is automatic on the Responses API; ``cache_prefix`` additionally
    sets a stable ``prompt_cache_key`` to improve hit routing.
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
        """Call OpenAI and normalize the response."""

        from openai import AsyncOpenAI

        if schema is not None and tools is not None:
            raise ValueError("schema and tools are mutually exclusive")

        input_items = _to_openai_input(messages)
        cache_kwargs = {"prompt_cache_key": config.name} if cache_prefix else {}
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
        # Cross-agent figures (from another agent's report) ride this call-time channel;
        # figures a tool produced *this* loop live inline at their tool-result turn instead
        # (see `_to_openai_input`).
        for figure in images:
            attachment_content.extend(_figure_items(figure))
        if attachment_content:
            input_items.append({"role": "user", "content": attachment_content})

        client = AsyncOpenAI()

        if schema is not None:
            response = await client.responses.create(
                model=config.model,
                input=input_items,
                text={"format": _strict_json_schema_format(schema)},
                **cache_kwargs,
                **config.parameters,
            )
            text = getattr(response, "output_text", "") or ""
            parsed = json.loads(text) if text else None
            return _response(content=text, parsed=parsed, raw=response)

        if tools is not None:
            function_tools = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    "strict": False,
                }
                for tool in tools
            ]
            response = await client.responses.create(
                model=config.model,
                input=input_items,
                tools=function_tools,
                tool_choice="required" if tool_choice == "required" else "auto",
                **cache_kwargs,
                **config.parameters,
            )
            tool_calls = []
            for item in getattr(response, "output", []) or []:
                if getattr(item, "type", None) == "function_call":
                    try:
                        args = json.loads(item.arguments) if item.arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_calls.append(
                        ToolCall(id=getattr(item, "call_id", "") or "", name=item.name, arguments=args)
                    )
            return _response(
                content=getattr(response, "output_text", ""), tool_calls=tool_calls, raw=response
            )

        response = await client.responses.create(
            model=config.model,
            input=input_items,
            **cache_kwargs,
            **config.parameters,
        )
        return _response(content=getattr(response, "output_text", ""), raw=response)


def _figure_items(figure: FigureRef) -> list[dict]:
    """Render one figure as Responses-API content items: a caption then the image bytes.

    Loading bytes here is the multimodal boundary — the only place a figure's pixels
    enter a request (elsewhere figures travel as lightweight ``FigureRef``). The caption is
    rendered verbatim: the presentation layer may prefix it with the report-local number
    ("Figure 1 — …") or the catalog handle ("[analyst_worker_0:2] …") so prose lines up
    with the images (V-4).
    """
    data = base64.b64encode(figure.path.read_bytes()).decode("ascii")
    return [
        {"type": "input_text", "text": f"{figure.caption} (produced by {figure.produced_by})"},
        {"type": "input_image", "image_url": f"data:{figure.mime_type};base64,{data}"},
    ]


def _to_openai_input(messages: Sequence[ConversationMessage]) -> list[dict]:
    """Translate neutral conversation turns into Responses-API input items.

    ``AssistantToolUse`` → ``function_call`` items; ``ToolResultMessage`` →
    ``function_call_output`` items; plain ``ModelMessage`` → a text message
    (assistant turns use ``output_text``, others ``input_text``).
    """
    items: list[dict] = []
    for message in messages:
        if isinstance(message, AssistantToolUse):
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    }
                )
        elif isinstance(message, ToolResultMessage):
            figure_content: list[dict] = []
            for part in message.results:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": part.tool_call_id,
                        "output": part.content,
                    }
                )
                for figure in part.figures:
                    figure_content.extend(_figure_items(figure))
            # A tool's figures live inline at this turn: a user message carrying the image(s)
            # placed right after the function_call_output item(s), so they replay in place
            # every round and fall inside the automatically-cached prefix as the loop grows.
            if figure_content:
                items.append({"role": "user", "content": figure_content})
        else:
            content_type = "output_text" if message.role == "assistant" else "input_text"
            items.append(
                {"role": message.role, "content": [{"type": content_type, "text": message.content}]}
            )
    return items


# JSON Schema string `format` values OpenAI structured outputs accepts. Pydantic
# emits others (e.g. `path` for `Path`, `uri` for `HttpUrl`) that the API rejects.
_OPENAI_SUPPORTED_FORMATS = {
    "date-time", "date", "time", "duration", "email", "hostname",
    "ipv4", "ipv6", "uuid",
}


def _strict_json_schema_format(schema: type[BaseModel]) -> dict:
    """Build a Responses-API `text.format` json_schema block for a Pydantic model.

    OpenAI structured outputs require strict-mode JSON Schema: every object sets
    ``additionalProperties: false`` and lists all properties in ``required``,
    ``default`` is disallowed, and only a small set of string ``format`` values is
    accepted. Pydantic's generated schema violates all three (optionals, defaults,
    ``format: path``/``uri``), so we sanitize a copy before sending it.
    """
    json_schema = schema.model_json_schema()
    _sanitize_strict(json_schema)
    return {
        "type": "json_schema",
        "name": schema.__name__,
        "strict": True,
        "schema": json_schema,
    }


def _sanitize_strict(node: object) -> None:
    """Recursively make a Pydantic JSON schema OpenAI-strict-compatible (in place).

    ``properties`` keys are field *names* (which may be ``default``/``format``),
    not schema keywords, so we recurse into the field subschemas without touching
    the field names.
    """
    if isinstance(node, dict):
        fmt = node.get("format")
        if isinstance(fmt, str) and fmt not in _OPENAI_SUPPORTED_FORMATS:
            node.pop("format")
        node.pop("default", None)
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for subschema in value.values():
                    _sanitize_strict(subschema)
            else:
                _sanitize_strict(value)
    elif isinstance(node, list):
        for item in node:
            _sanitize_strict(item)


def _response(*, content="", parsed=None, tool_calls=None, raw=None) -> ModelResponse:
    usage = getattr(raw, "usage", None)
    return ModelResponse(
        content=content or "",
        parsed=parsed,
        tool_calls=tool_calls or [],
        usage=ModelUsage(
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        ),
        raw=raw,
    )
