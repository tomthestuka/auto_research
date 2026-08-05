"""OpenAI provider adapter."""

import base64
import json
import re
import typing
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.env import tool_search_config
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


_DISCOVERY_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})
"""Tool-search items: the reason any of this exists. A tool discovered once stays callable only
while these survive replay."""

_UNCARRIED_ITEM_TYPES = frozenset({"function_call", "message"})
"""The only response items we do NOT carry, because we already replay them from our own state:
``function_call`` is rebuilt from :class:`ToolCall` (with the provider's record merged back in)
and ``message`` text is replayed from ``ModelResponse.content``.

Everything else is carried whole. Three run-killers in one week came from re-typing the
provider's records into a smaller shape of our own — a forbidden field added, a required field
dropped, and then a required *companion item* dropped (``tool_search_call`` is rejected without
the ``reasoning`` item that produced it). Carrying by default, and excluding by exception, means
an item type we have never heard of rides along instead of going missing."""

#: Fallback keep-lists for the two item types we rebuild from our own state, mirroring the SDK's
#: input TypedDicts as of openai 2.38.0. Used only if the union walk below finds nothing; sending
#: a known-good subset beats sending an output item raw.
_INPUT_SHAPE_FALLBACK = {
    "function_call": frozenset({"arguments", "call_id", "name", "type", "id", "namespace", "status"}),
    "reasoning": frozenset({"content", "encrypted_content", "id", "status", "summary", "type"}),
}

_input_fields_cache: dict[str, frozenset[str]] = {}


def _input_shapes() -> dict[str, frozenset[str]]:
    """Map ``type`` → accepted field names, read from the SDK's own input-item union.

    The Responses API speaks a larger language than it accepts, so every item we hand back has
    to be filtered to the fields its *input* shape declares. That map used to be hand-written,
    covering the four item types we happened to carry — which is fine only while the carried set
    is closed. It is not: an unrecognised item now rides along verbatim
    (``_UNCARRIED_ITEM_TYPES``), so an item type absent from a hand-written map would get **no**
    filtering at all and take its server-stamped fields with it into the next request — the exact
    400 that killed a run on 2026-08-03, one item type later.

    So the allowlist is derived instead: walk ``ResponseInputItemParam``, the union the SDK
    generates from the input schema, and index each member's declared fields by its ``type``
    literal. New item types are covered the moment the SDK knows them.
    """
    shapes: dict[str, frozenset[str]] = {}

    def visit(annotation: Any) -> None:
        for member in typing.get_args(annotation) or ():
            declared = getattr(member, "__annotations__", None)
            if not declared:
                visit(member)
                continue
            type_ann = declared.get("type")
            fields = frozenset(declared)
            for literal in _type_literals(type_ann):
                # First writer wins: the union lists the canonical item param before the
                # loose `EasyInputMessageParam` alias for "message".
                shapes.setdefault(literal, fields)
            if type_ann is None:
                visit(member)

    try:
        from openai.types.responses.response_input_item_param import ResponseInputItemParam

        visit(ResponseInputItemParam)
    except Exception:  # noqa: BLE001 — the per-type fallback below is the point
        pass
    return shapes


def _type_literals(annotation: Any) -> list[str]:
    """The ``Literal[...]`` strings an item param's ``type`` field may take (unwrapping
    ``Required``/``Optional``/``ForwardRef`` layers the SDK's TypedDicts wrap it in)."""
    if annotation is None:
        return []
    if isinstance(annotation, str):
        annotation = typing.ForwardRef(annotation)
    if isinstance(annotation, typing.ForwardRef):
        # The SDK stores these unevaluated under `from __future__ import annotations`; the
        # literal text is all we need, so read it rather than resolving the reference.
        text = annotation.__forward_arg__
        return re.findall(r"'([^']+)'", text)
    literals: list[str] = []
    for arg in typing.get_args(annotation) or ():
        if isinstance(arg, str):
            literals.append(arg)
        else:
            literals.extend(_type_literals(arg))
    return literals


def _input_fields(item_type: str) -> frozenset[str]:
    """Fields the Responses API accepts for ``item_type`` when it is sent as *input*.

    Read from the SDK's own input union (see :func:`_input_shapes`) rather than hand-listed,
    because the set grows: OpenAI adds output-only bookkeeping to response items over time, and
    a hand-written list rots into a mid-run 400 the next time it does.
    """
    cached = _input_fields_cache.get(item_type)
    if cached is None:
        if not _input_fields_cache:
            _input_fields_cache.update(_input_shapes())
            cached = _input_fields_cache.get(item_type)
        if cached is None:
            cached = _INPUT_SHAPE_FALLBACK.get(item_type, frozenset())
            _input_fields_cache[item_type] = cached
    return cached


def _as_input_item(item: Any) -> dict[str, Any]:
    """Convert a Responses *output* item into the *input* item that echoes it.

    The API speaks a larger language than it accepts: a response item carries bookkeeping the
    server stamps on its own output (``created_by`` — "the identifier of the actor that created
    the item"), and the input schema rejects it as an unknown parameter. Round-tripping the raw
    item therefore 400s on the next turn — which killed a live run on 2026-08-03, and is a known
    open defect in the SDK itself across several item types (openai-python #3008, #2323, #2670,
    where the missing piece is described as an ``.as_input()`` helper). This is that helper, kept
    deliberately narrow: only the items we actually replay.

    Two filters, both needed. **By name:** keep only what the input type declares, which drops
    output-only bookkeeping. **By value:** drop keys whose value is ``None``, because
    ``model_dump()`` fabricates absent optional fields as null and the API rejects a null it did
    not send — even for a field its own input type declares (``Unknown parameter:
    'input[2].status'``, openai-python #3008, reproduced live 2026-08-03).

    Both run **once, here at capture**, so the stored item is fixed for the life of the
    conversation and replays as identical bytes on every turn. That is what protects the cached
    prefix: prompt caching keys on an exact prefix match, so the hazard is a rule applied
    *differently* on different turns — not a rule that reads the values, which resolves the same
    way every time it looks at the same item.
    """
    raw = _item_to_dict(item)
    allowed = _input_fields(str(raw.get("type") or ""))
    if allowed:
        raw = {key: value for key, value in raw.items() if key in allowed}
    return {key: value for key, value in raw.items() if value is not None}


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
    response output; every other item it emits (``reasoning``, the tool-search
    discovery pair, anything new) is carried verbatim on ``ModelResponse.provider_parts``,
    projected into its accepted input shape at capture. Prior ``ModelResponse`` /
    ``ToolResultMessage`` turns are replayed as those items plus ``function_call`` /
    ``function_call_output`` inputs so a ReAct loop can carry its trajectory.
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

        input_items = _to_openai_input(messages, include_provider_parts=tools is not None)
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
            provider_parts: list[dict[str, Any]] = []
            for item in getattr(response, "output", []) or []:
                itype = getattr(item, "type", None)
                if itype == "function_call":
                    try:
                        args = json.loads(item.arguments) if item.arguments else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    tool_calls.append(
                        ToolCall(
                            id=getattr(item, "call_id", "") or "",
                            name=item.name,
                            arguments=args,
                            # The provider's own record of the call, kept whole (in its input
                            # shape) so the replay hands back fields we do not interpret —
                            # `namespace`, which addresses a tool discovered through tool search,
                            # and whatever OpenAI adds next.
                            provider_fields=_as_input_item(item),
                        )
                    )
                elif itype not in _UNCARRIED_ITEM_TYPES:
                    # Everything else the provider authored is carried whole, in output order, so
                    # a later turn can hand it back: the discovery items (which keep a searched
                    # tool callable) and the reasoning items they depend on. Translated to their
                    # input shape *here*, at capture, so each is legal to send by construction and
                    # is the same bytes on every turn that replays it (see `_as_input_item`).
                    provider_parts.append(_as_input_item(item))
            return _response(
                content=getattr(response, "output_text", ""),
                tool_calls=tool_calls,
                provider_parts=provider_parts,
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
    messages: Sequence[ConversationMessage], *, include_provider_parts: bool = True
) -> list[dict]:
    """Translate neutral conversation turns into Responses-API input items.

    ``ModelResponse`` → its carried items plus ``function_call`` items; ``ToolResultMessage`` →
    ``function_call_output`` items; plain ``ModelMessage`` → a text message
    (assistant turns use ``output_text``, others ``input_text``).
    """
    items: list[dict] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            # The provider's own items replay first, in the order it emitted them, ahead of the
            # calls they enabled: OpenAI's rule for a tool round trip is to "ensure all items
            # between the last user message and your function call output are passed into the
            # next response untouched"
            # (developers.openai.com/api/docs/guides/reasoning). A discovery item is also
            # rejected without the reasoning item that produced it — observed live 2026-08-03,
            # `Item 'tsc_…' of type 'tool_search_call' was provided without its required
            # 'reasoning' item: 'rs_…'` — so order within the carried list is load-bearing.
            #
            # A request declaring no tools drops them all. Unlike Anthropic and Google, which
            # document that reasoning must be resent unmodified, OpenAI documents pass-back as
            # *recommended* rather than required and says its systems "smartly ignore any
            # reasoning items that aren't relevant to your functions" — so omitting them is
            # supported, while replaying a discovery item that names an undeclared tool is not
            # (a live run-killer, 2026-07-29).
            if include_provider_parts:
                items.extend(message.provider_parts)
            for call in message.tool_calls:
                # Start from the provider's own record so fields we never interpret ride along
                # (`namespace` addresses a tool discovered through tool search — replaying without
                # it names a tool the API cannot resolve, which killed a live run on 2026-08-03).
                # Our canonical values then overwrite, so the executed call and the replayed call
                # can never disagree. A call captured by an adapter that sets no provider_fields
                # replays exactly as before.
                items.append(
                    {
                        **call.provider_fields,
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
    *, content="", parsed=None, tool_calls=None, provider_parts=None, raw=None
) -> ModelResponse:
    return ModelResponse(
        content=content or "",
        parsed=parsed,
        tool_calls=tool_calls or [],
        usage=_openai_usage(getattr(raw, "usage", None)),
        provider_parts=provider_parts or [],
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
