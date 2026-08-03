"""OpenAI provider adapter."""

import base64
import importlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.env import tool_search_config
from auto_research.models.providers._json import loads_first_json_object
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


_DISCOVERY_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})
"""Tool-search items replayed on later turns, so tools loaded once stay callable without
re-searching. The provider authors these — every other item we send, we build ourselves."""

#: The item types above, mapped to the SDK type that declares their **input** shape.
_INPUT_SHAPE_TYPES = {
    "tool_search_call": ("openai.types.responses.response_input_item_param", "ToolSearchCall"),
    "tool_search_output": (
        "openai.types.responses.response_tool_search_output_item_param_param",
        "ResponseToolSearchOutputItemParamParam",
    ),
}

#: Fallback keep-lists, mirroring the input types above as of openai 2.38.0. Used only if the
#: SDK's module layout moves; sending a known-good subset beats sending an output item raw.
_INPUT_SHAPE_FALLBACK = {
    "tool_search_call": frozenset({"arguments", "type", "id", "call_id", "execution", "status"}),
    "tool_search_output": frozenset({"tools", "type", "id", "call_id", "execution", "status"}),
}

_input_fields_cache: dict[str, frozenset[str]] = {}


def _input_fields(item_type: str) -> frozenset[str]:
    """Fields the Responses API accepts for ``item_type`` when it is sent as *input*.

    Read from the SDK's own input TypedDict rather than hand-listed, because the set grows:
    OpenAI adds output-only bookkeeping to response items over time, and a hand-listed
    blacklist rots into a mid-run 400 the next time they do.
    """
    cached = _input_fields_cache.get(item_type)
    if cached is not None:
        return cached
    fields = _INPUT_SHAPE_FALLBACK.get(item_type, frozenset())
    target = _INPUT_SHAPE_TYPES.get(item_type)
    if target:
        module_name, type_name = target
        try:
            module = importlib.import_module(module_name)
            declared = getattr(getattr(module, type_name), "__annotations__", None)
            if declared:
                fields = frozenset(declared)
        except Exception:  # noqa: BLE001 — the fallback above is the point
            pass
    _input_fields_cache[item_type] = fields
    return fields


def _as_input_item(item: Any) -> dict[str, Any]:
    """Convert a Responses *output* item into the *input* item that echoes it.

    The API speaks a larger language than it accepts: a response item carries bookkeeping the
    server stamps on its own output (``created_by`` — "the identifier of the actor that created
    the item"), and the input schema rejects it as an unknown parameter. Round-tripping the raw
    item therefore 400s on the next turn — which killed a live run on 2026-08-03, and is a known
    open defect in the SDK itself across several item types (openai-python #3008, #2323, #2670,
    where the missing piece is described as an ``.as_input()`` helper). This is that helper, kept
    deliberately narrow: only the items we actually replay.

    Projection is **unconditional** — the same fields in the same order on every turn — because a
    value-dependent rewrite (the popular ``exclude_none`` workaround) makes an item serialize one
    way when a field is null and another way when it is not. The moment that flips mid-conversation
    the cached prefix diverges and every later turn silently re-bills at full price.
    """
    raw = _item_to_dict(item)
    allowed = _input_fields(str(raw.get("type") or ""))
    if not allowed:
        return raw
    return {key: value for key, value in raw.items() if key in allowed}


def _item_to_dict(item: Any) -> dict[str, Any]:
    """Best-effort plain-dict view of a Responses output item (for verbatim replay)."""
    for attr in ("model_dump", "to_dict", "dict"):
        dumper = getattr(item, attr, None)
        if callable(dumper):
            try:
                return dict(dumper(mode="json") if attr == "model_dump" else dumper())
            except TypeError:
                try:
                    return dict(dumper())
                except Exception:  # noqa: BLE001 — fall through to the generic path
                    pass
            except Exception:  # noqa: BLE001
                pass
    if isinstance(item, dict):
        return dict(item)
    return {"type": getattr(item, "type", None)}


class OpenAIProvider:
    """Thin adapter around the OpenAI Responses API.

    Structured output uses ``responses.create`` with a hand-sanitized strict JSON
    schema (``_strict_json_schema_format``), not the SDK's ``responses.parse``:
    the SDK's own Pydantic-to-strict conversion keeps ``format: path`` and
    ``default`` values that OpenAI strict mode rejects, so we scrub those on the
    send side ourselves. The response is deserialized with
    ``loads_first_json_object`` — which takes the first complete JSON value and
    tolerates any leading/trailing content a reasoning model may add (a bare
    ``json.loads`` on that surplus once crashed a run). Function calling passes
    per-tool function definitions and reads ``function_call`` items from the
    response output. ``AssistantToolUse`` /
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

        input_items = _to_openai_input(messages, include_discovery=tools is not None)
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
            parsed = loads_first_json_object(text)
            return _response(content=text, parsed=parsed, raw=response)

        if tools is not None:
            ts_cfg = tool_search_config()
            function_tools = []
            for tool in tools:
                entry: dict[str, Any] = {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    "strict": False,
                }
                # Deferred: OpenAI keeps the name+description resident and withholds only the
                # parameter schema until the model discovers the tool, injecting it at the END
                # of the context window so the cache is preserved. Live-verified 2026-07-28.
                if ts_cfg.enabled and tool.get("defer_loading"):
                    entry["defer_loading"] = True
                function_tools.append(entry)
            if ts_cfg.enabled and any(t.get("defer_loading") for t in function_tools):
                function_tools.append({"type": ts_cfg.openai_tool})
            response = await client.responses.create(
                model=config.model,
                input=input_items,
                tools=function_tools,
                tool_choice="required" if tool_choice == "required" else "auto",
                **cache_kwargs,
                **config.parameters,
            )
            tool_calls = []
            provider_blocks: list[dict[str, Any]] = []
            for item in getattr(response, "output", []) or []:
                itype = getattr(item, "type", None)
                if itype == "function_call":
                    try:
                        args = json.loads(item.arguments) if item.arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_calls.append(
                        ToolCall(id=getattr(item, "call_id", "") or "", name=item.name, arguments=args)
                    )
                elif itype in _DISCOVERY_ITEM_TYPES:
                    # Tool-search discovery items: replayed so the loaded tools stay callable on
                    # later turns without re-searching. Translated to their input shape *here*,
                    # at capture, so the stored item is legal to send by construction and is the
                    # same bytes on every turn that replays it (see `_as_input_item`).
                    provider_blocks.append(_as_input_item(item))
            return _response(
                content=getattr(response, "output_text", ""),
                tool_calls=tool_calls,
                provider_blocks=provider_blocks,
                raw=response,
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


def _to_openai_input(
    messages: Sequence[ConversationMessage], *, include_discovery: bool = True
) -> list[dict]:
    """Translate neutral conversation turns into Responses-API input items.

    ``AssistantToolUse`` → ``function_call`` items; ``ToolResultMessage`` →
    ``function_call_output`` items; plain ``ModelMessage`` → a text message
    (assistant turns use ``output_text``, others ``input_text``).
    """
    items: list[dict] = []
    for message in messages:
        if isinstance(message, AssistantToolUse):
            # Tool-search discovery items replay verbatim, before the calls they enabled —
            # unless this request declares no tools, in which case they are dropped: a
            # discovery item references a tool by name, and a request that offers no tools
            # cannot honour that reference (the Anthropic half of this killed a live run on
            # 2026-07-29). A terminal structured compose is exactly that request.
            if include_discovery:
                items.extend(
                    b for b in message.provider_blocks if b.get("type") in _DISCOVERY_ITEM_TYPES
                )
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


def _response(
    *, content="", parsed=None, tool_calls=None, provider_blocks=None, raw=None
) -> ModelResponse:
    return ModelResponse(
        content=content or "",
        parsed=parsed,
        tool_calls=tool_calls or [],
        usage=_openai_usage(getattr(raw, "usage", None)),
        provider_blocks=provider_blocks or [],
        raw=raw,
    )


def _openai_usage(usage: Any) -> ModelUsage:
    """Map an OpenAI Responses ``usage`` object into the neutral non-overlapping buckets ([I5]).

    ``usage.input_tokens`` is the TOTAL prompt tokens (cached + uncached); the cached subset is
    ``input_tokens_details.cached_tokens`` (billed at a reduced rate), so uncached full-price
    input = total - cached. ``output_tokens`` already includes reasoning tokens (billed as output);
    ``output_tokens_details.reasoning_tokens`` is surfaced for visibility only. OpenAI caches
    implicitly with no write premium, so ``cache_write_tokens`` stays 0.
    """
    if usage is None:
        return ModelUsage()
    total_input = getattr(usage, "input_tokens", 0) or 0
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    return ModelUsage(
        input_tokens=max(total_input - cached, 0),
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )
