"""Google Gemini provider adapter."""

import copy
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.models.providers._json import loads_first_json_object
from auto_research.models.types import (
    ConversationMessage,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
)
from auto_research.schemas import DocumentRef, FigureRef

_log = logging.getLogger(__name__)

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


def _gemini_usage(usage: Any) -> ModelUsage:
    """Map a Gemini ``usage_metadata`` object into the neutral non-overlapping buckets ([I5]).

    ``prompt_token_count`` is the TOTAL prompt tokens (cached + uncached); ``cached_content_token_count``
    is the cached subset (billed at a reduced rate), so uncached full-price input = total - cached.
    ``candidates_token_count`` excludes thinking, which Gemini reports separately as
    ``thoughts_token_count`` and bills as output — so output = candidates + thoughts, with thoughts
    surfaced as ``reasoning_tokens``. Implicit caching has no write premium, so ``cache_write_tokens``
    stays 0.
    """
    if usage is None:
        return ModelUsage()
    total_input = getattr(usage, "prompt_token_count", 0) or 0
    cached = getattr(usage, "cached_content_token_count", 0) or 0
    candidates = getattr(usage, "candidates_token_count", 0) or 0
    thoughts = getattr(usage, "thoughts_token_count", 0) or 0
    return ModelUsage(
        input_tokens=max(total_input - cached, 0),
        output_tokens=candidates + thoughts,
        cache_read_tokens=cached,
        reasoning_tokens=thoughts,
    )


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


def _response_parts(response: Any) -> list:
    """The first candidate's content parts, or ``[]`` (a blocked/empty candidate has none)."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    content = getattr(candidates[0], "content", None)
    return list(getattr(content, "parts", None) or [])


def _part_to_dict(part: Any) -> dict[str, Any]:
    """Plain-dict view of a Gemini ``Part``, dropping unset fields (for verbatim replay)."""
    try:
        return part.model_dump(mode="json", exclude_none=True)
    except Exception:  # noqa: BLE001 — a part we cannot serialize must never break the call
        return {}


def _part_from_dict(data: Mapping[str, Any], types: Any) -> Any | None:
    """Rebuild a carried ``Part`` from its dict form; ``None`` when it cannot be rebuilt.

    Unlike the other two providers, Gemini's message content is typed SDK objects rather than
    dicts, so a carried part has to be revalidated rather than spliced in as-is. The dict came
    from that same SDK's ``model_dump``, so a failure here means the round trip is lossy — it is
    logged rather than swallowed, because a silently dropped part is the failure mode this whole
    carrier exists to remove.
    """
    if not data:
        return None
    try:
        return types.Part.model_validate(dict(data))
    except Exception as exc:  # noqa: BLE001 — losing one part must not fail the call
        _log.warning("dropping a carried Gemini part that would not rebuild (%s): %s", exc, data)
        return None


def _figure_parts(figure: FigureRef, types: Any) -> list:
    """Render one figure as Gemini ``Part``s: a caption then the image bytes.

    Loading bytes here is the multimodal boundary — the only place a figure's pixels
    enter a request (elsewhere figures travel as lightweight ``FigureRef``). The caption is
    rendered verbatim: the presentation layer may prefix it with the report-local number
    ("Figure 1 — …") or the catalog handle ("[analyst_worker_0:2] …") so prose lines up
    with the images (V-4).
    """
    return [
        types.Part.from_text(text=f"{figure.caption} (produced by {figure.produced_by})"),
        types.Part.from_bytes(data=figure.path.read_bytes(), mime_type=figure.mime_type),
    ]


def _to_gemini_contents(messages: Sequence[ConversationMessage], types: Any) -> tuple[str | None, list]:
    """Split system text out and translate the rest into Gemini ``Content`` turns.

    ``ModelResponse`` → a ``model`` turn rebuilt from that turn's parts;
    ``ToolResultMessage`` → a ``user`` turn with ``function_response`` parts
    (keyed by tool name); plain ``ModelMessage`` → a text turn.

    A turn Gemini authored is echoed VERBATIM, for the reason its docs give: we manage
    conversation state ourselves (stateless mode), where *"you MUST always resend all `thought`
    blocks exactly as they were received from the model"* and *"should NOT remove or modify thought
    blocks from the history, as they contain the signatures required for the model to continue its
    reasoning"* (ai.google.dev/gemini-api/docs/thinking#signatures). Signatures sit on ``thought``
    steps and on built-in tool steps, and a ``Part`` is a struct rather than a tagged union, so a
    call's ``thought_signature`` rides on the same part as its ``function_call`` — echoing the
    array keeps them together. Dropping it is why Gemini's multi-turn tool use breaks today
    (``Function call is missing a thought_signature``, [I8]).

    A turn we synthesized has no array to echo, so it is rebuilt from the interpreted views, with
    the signature taken off the call's ``provider_fields``.
    """
    system_texts: list[str] = []
    contents: list = []
    for message in messages:
        if isinstance(message, ModelResponse):
            if message.provider_parts:
                parts = [_part_from_dict(part, types) for part in message.provider_parts]
                parts = [part for part in parts if part is not None]
            else:
                parts = []
                if message.content:
                    parts.append(types.Part.from_text(text=message.content))
                for call in message.tool_calls:
                    signature = call.provider_fields.get("thought_signature")
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(name=call.name, args=call.arguments),
                            **({"thought_signature": signature} if signature else {}),
                        )
                    )
            contents.append(types.Content(role="model", parts=parts))
        elif isinstance(message, ToolResultMessage):
            parts = [
                types.Part.from_function_response(name=part.name, response={"result": part.content})
                for part in message.results
            ]
            contents.append(types.Content(role="user", parts=parts))
            # A tool's figures live inline at this turn: a user Content carrying the image(s)
            # placed right after the function_response Content, so they replay in place every
            # round and land in the (implicit) cached prefix as the loop grows.
            figure_parts: list = []
            for part in message.results:
                for figure in part.figures:
                    figure_parts.extend(_figure_parts(figure, types))
            if figure_parts:
                contents.append(types.Content(role="user", parts=figure_parts))
        elif message.role == "system":
            system_texts.append(message.content)
        else:
            role = "model" if message.role == "assistant" else "user"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=message.content)]))
    return ("\n".join(system_texts) or None), contents


class GoogleProvider:
    """Thin adapter around Google Gen AI models.

    Native structured output uses ``response_mime_type="application/json"`` plus a
    ``response_schema`` (the Pydantic model); function calling passes ``FunctionDeclaration``s.
    Both may be declared in one request (live-verified 2026-08-04 on ``gemini-3.5-flash``,
    which lifts the old "function calling with a json response mime type is unsupported"
    restriction). Responses are read part-by-part rather than through the ``.text`` /
    ``.function_calls`` accessors, which flatten a mixed turn; parts we do not interpret ride
    on ``ModelResponse.provider_parts``. Prior ``ModelResponse`` / ``ToolResultMessage`` turns
    are replayed as ``function_call`` / ``function_response`` parts (Google correlates results
    to calls by tool name). Implicit context caching is on by default for current Gemini, so
    ``cache_prefix`` is accepted as a no-op.
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

        system, contents = _to_gemini_contents(messages, types)
        extra_parts: list[Any] = []
        for document in documents:
            extra_parts.append(
                types.Part.from_bytes(data=document.path.read_bytes(), mime_type=document.mime_type)
            )
        # Cross-agent figures (from another agent's report) ride this call-time channel;
        # figures a tool produced *this* loop live inline at their tool-result turn instead
        # (see `_to_gemini_contents`).
        for figure in images:
            extra_parts.extend(_figure_parts(figure, types))
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
        if tools is not None:
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
            # ``NONE`` keeps the declarations while forbidding a call — *"Model is prohibited
            # from making function calls"* (ai.google.dev/gemini-api/docs/function-calling) —
            # which is what a terminal compose declares so its replayed parts still resolve.
            mode = {"required": "ANY", "none": "NONE"}.get(tool_choice, "AUTO")
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

        # Walk the parts rather than the `.text` / `.function_calls` accessors: a Part is a
        # struct, not a tagged union, so those accessors flatten a mixed turn (`.text` warns
        # and returns only the text parts when a function_call is present) and neither exposes
        # `thought_signature`. EVERY part is carried (the array is the turn, echoed verbatim by
        # `_to_gemini_contents`); plain text → `content` and `function_call` → `tool_calls` are
        # views onto it for the harness.
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        provider_parts: list[dict[str, Any]] = []
        for part in _response_parts(response):
            provider_parts.append(_part_to_dict(part))
            call = getattr(part, "function_call", None)
            if call is not None:
                signature = getattr(part, "thought_signature", None)
                tool_calls.append(
                    ToolCall(
                        id=getattr(call, "id", "") or call.name,
                        name=call.name,
                        arguments=dict(call.args or {}),
                        # The signature rides on this part, so it belongs to this call ([I8]).
                        provider_fields=({"thought_signature": signature} if signature else {}),
                    )
                )
            elif getattr(part, "text", None) is not None and not getattr(part, "thought", False):
                text_parts.append(part.text)
            else:
                provider_parts.append(_part_to_dict(part))

        content = "".join(text_parts)
        parsed = None
        if schema is not None:
            parsed_obj = getattr(response, "parsed", None)
            if isinstance(parsed_obj, BaseModel):
                parsed = parsed_obj.model_dump(mode="json")
            elif parsed_obj is not None:
                parsed = parsed_obj
            else:
                parsed = loads_first_json_object(content)

        usage = getattr(response, "usage_metadata", None)
        return ModelResponse(
            content=content,
            parsed=parsed,
            tool_calls=tool_calls,
            provider_parts=provider_parts,
            usage=_gemini_usage(usage),
            raw=response,
        )
