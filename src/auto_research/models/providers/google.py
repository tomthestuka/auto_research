"""Google Gemini provider adapter."""

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.models.types import ModelMessage, ModelResponse, ModelUsage, RoleModelConfig, ToolCall
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


class GoogleProvider:
    """Thin adapter around Google Gen AI models.

    Native structured output uses ``response_mime_type="application/json"`` plus
    a ``response_schema`` (the Pydantic model). Function calling passes
    ``FunctionDeclaration``s and reads ``response.function_calls``.
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
        """Call Google and normalize the response."""

        from google import genai
        from google.genai import types

        if schema is not None and tools is not None:
            raise ValueError("schema and tools are mutually exclusive")

        contents: list[Any] = [
            "\n\n".join(f"{message.role}: {message.content}" for message in messages)
        ]
        for document in documents:
            contents.append(types.Part.from_bytes(data=document.path.read_bytes(), mime_type=document.mime_type))
        if images:
            contents.append("\n".join(f"Figure reference: {figure.path}" for figure in images))

        params = dict(config.parameters)
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
                tool_calls.append(ToolCall(name=fc.name, arguments=dict(fc.args or {})))

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
