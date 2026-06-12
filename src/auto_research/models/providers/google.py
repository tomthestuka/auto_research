"""Google Gemini provider adapter."""

import copy
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

# String `format` values Gemini's response schema accepts (others are stripped).
_GEMINI_SUPPORTED_FORMATS = {"date-time", "enum"}


def _to_gemini_schema(model: type[BaseModel]) -> dict:
    """Convert a Pydantic model to a Gemini-compatible response schema (dict).

    Gemini's structured-output schema dialect is a restricted OpenAPI subset: it
    rejects `additionalProperties`, `$ref`/`$defs` (refs must be inlined),
    `default`/`title`, and most string `format` values. We inline refs and strip
    the unsupported keywords from a copy of the Pydantic schema.
    """
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    resolved = _inline_refs(raw, defs)
    if isinstance(resolved, dict):
        resolved.pop("$defs", None)
    _clean_gemini(resolved)
    return resolved


def _inline_refs(node: Any, defs: dict) -> Any:
    """Return a deep copy of ``node`` with every ``$ref`` to ``$defs`` inlined."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.split("/")[-1]
            target = defs.get(name, {})
            merged = _inline_refs(copy.deepcopy(target), defs)
            # Preserve any sibling keys (e.g. description) alongside the inlined ref.
            if isinstance(merged, dict):
                for key, value in node.items():
                    if key != "$ref":
                        merged[key] = _inline_refs(value, defs)
            return merged
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def _clean_gemini(node: Any) -> None:
    """Strip Gemini-unsupported schema keywords in place.

    ``properties`` is handled specially: its keys are field *names* (which may
    legitimately be ``title``/``default``/``format``), not schema keywords, so we
    recurse into the field subschemas without stripping the field names.
    """
    if isinstance(node, dict):
        node.pop("additionalProperties", None)
        node.pop("default", None)
        node.pop("title", None)
        fmt = node.get("format")
        if isinstance(fmt, str) and fmt not in _GEMINI_SUPPORTED_FORMATS:
            node.pop("format")
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for subschema in value.values():
                    _clean_gemini(subschema)
            else:
                _clean_gemini(value)
    elif isinstance(node, list):
        for item in node:
            _clean_gemini(item)


def _to_gemini_contents(messages: Sequence[ConversationMessage], types: Any) -> tuple[str | None, list]:
    """Split system text out and translate the rest into Gemini ``Content`` turns.

    ``AssistantToolUse`` → a ``model`` turn with ``function_call`` parts;
    ``ToolResultMessage`` → a ``user`` turn with ``function_response`` parts
    (keyed by tool name); plain ``ModelMessage`` → a text turn.
    """
    system_texts: list[str] = []
    contents: list = []
    for message in messages:
        if isinstance(message, AssistantToolUse):
            parts = []
            if message.content:
                parts.append(types.Part.from_text(text=message.content))
            for call in message.tool_calls:
                parts.append(types.Part.from_function_call(name=call.name, args=call.arguments))
            contents.append(types.Content(role="model", parts=parts))
        elif isinstance(message, ToolResultMessage):
            parts = [
                types.Part.from_function_response(name=part.name, response={"result": part.content})
                for part in message.results
            ]
            contents.append(types.Content(role="user", parts=parts))
        elif message.role == "system":
            system_texts.append(message.content)
        else:
            role = "model" if message.role == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=message.content)]))
    return ("\n".join(system_texts) or None), contents


class GoogleProvider:
    """Thin adapter around Google Gen AI models.

    Native structured output uses ``response_mime_type="application/json"`` plus
    a ``response_schema`` (the Pydantic model). Function calling passes
    ``FunctionDeclaration``s and reads ``response.function_calls``.
    ``AssistantToolUse`` / ``ToolResultMessage`` turns are replayed as
    ``function_call`` / ``function_response`` parts (Google correlates results to
    calls by tool name). Implicit context caching is on by default for current
    Gemini, so ``cache_prefix`` is accepted as a no-op.
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
        """Call Google and normalize the response."""

        from google import genai
        from google.genai import types

        if schema is not None and tools is not None:
            raise ValueError("schema and tools are mutually exclusive")

        system, contents = _to_gemini_contents(messages, types)
        extra_parts: list[Any] = []
        for document in documents:
            extra_parts.append(
                types.Part.from_bytes(data=document.path.read_bytes(), mime_type=document.mime_type)
            )
        for figure in images:
            extra_parts.append(types.Part.from_text(text=f"Figure reference: {figure.path}"))
        if extra_parts:
            contents.append(types.Content(role="user", parts=extra_parts))

        params = dict(config.parameters)
        if system is not None:
            params["system_instruction"] = system
        if "thinking_config" in params:
            params["thinking_config"] = types.ThinkingConfig(**params["thinking_config"])

        if schema is not None:
            params["response_mime_type"] = "application/json"
            params["response_schema"] = _to_gemini_schema(schema)
        elif tools is not None:
            params["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool["name"],
                            description=tool.get("description", ""),
                            parameters=tool.get("input_schema", {"type": "object", "properties": {}}),
                        )
                        for tool in tools
                    ]
                )
            ]
            mode = "ANY" if tool_choice == "required" else "AUTO"
            params["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=mode)
            )

        generation_config = types.GenerateContentConfig(**params) if params else None

        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=config.model,
            contents=contents,
            config=generation_config,
        )

        parsed = None
        if schema is not None:
            parsed_obj = getattr(response, "parsed", None)
            if isinstance(parsed_obj, BaseModel):
                parsed = parsed_obj.model_dump(mode="json")
            elif parsed_obj is not None:
                parsed = parsed_obj
            else:
                text = getattr(response, "text", "") or ""
                parsed = json.loads(text) if text else None

        tool_calls = []
        if tools is not None:
            for fc in getattr(response, "function_calls", None) or []:
                tool_calls.append(
                    ToolCall(
                        id=getattr(fc, "id", "") or fc.name,
                        name=fc.name,
                        arguments=dict(fc.args or {}),
                    )
                )

        usage = getattr(response, "usage_metadata", None)
        return ModelResponse(
            content=getattr(response, "text", "") or "",
            parsed=parsed,
            tool_calls=tool_calls,
            usage=ModelUsage(
                input_tokens=getattr(usage, "prompt_token_count", 0) if usage else 0,
                output_tokens=getattr(usage, "candidates_token_count", 0) if usage else 0,
            ),
            raw=response,
        )
