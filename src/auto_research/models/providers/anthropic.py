"""Anthropic provider adapter."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.env import context_editing_config, tool_search_config
from auto_research.models.providers._tool_search import anthropic_search_tool_name
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



def _block_to_dict(block: Any) -> dict[str, Any]:
    """Best-effort plain-dict view of an SDK content block (for verbatim replay)."""
    for attr in ("model_dump", "to_dict", "dict"):
        dumper = getattr(block, attr, None)
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
    if isinstance(block, dict):
        return dict(block)
    return {"type": getattr(block, "type", None)}


class AnthropicProvider:
    """Thin adapter around Anthropic messages.

    A ``schema`` request uses Anthropic's **native structured output** via
    ``messages.parse(output_format=schema)``: the SDK transforms the Pydantic
    model into a strict ``json_schema`` output format (dropping unsupported
    keywords, inlining ``$defs``, forcing ``additionalProperties:false``) and
    constrains the model to emit conforming JSON, then validates it into an
    instance. We return that instance as a plain dict in ``ModelResponse.parsed``
    for parity with the OpenAI/Google adapters.

    A ``tools`` request is native function calling via ``messages.create``; a request may
    declare ``schema`` and ``tools`` together, and the model then either calls a tool or
    emits the structured answer.

    ``text`` and ``tool_use`` blocks are read into typed fields as views for the harness, but
    **every** block the model authored — those two plus ``thinking``, the tool-search discovery
    pair, whatever Anthropic adds next — is carried verbatim on
    ``ModelResponse.provider_parts``, which is what :func:`_assistant_blocks` replays.
    ``messages`` may interleave prior
    ``ModelResponse`` / ``ToolResultMessage`` turns so a ReAct loop can replay its
    trajectory. ``cache_prefix`` sets Anthropic-native
    ``cache_control: ephemeral`` breakpoints on the stable prefix (the last system
    block, which also caches the tool defs that render before it; or the last tool
    when there is no system) and on the rolling conversation tail.
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
        """Call Anthropic and normalize the response."""

        from anthropic import AsyncAnthropic

        params = dict(config.parameters)
        max_tokens = params.pop("max_tokens", 1024)
        system, provider_messages = _to_anthropic_messages(
            messages, include_provider_parts=tools is not None
        )

        # Documents (e.g. PDFs the reviewer opens) go at the FRONT — a stable position
        # right after the system prompt — and grow append-only across a ReAct loop, so
        # each document is cache-written once and cache-read (~0.1x) on every later turn.
        # Re-sending them at the END (after a conversation that grows in the middle) breaks
        # the cache prefix right after `system` and re-bills every document each turn.
        # The cache breakpoint on the last document block, plus the stable [system + docs]
        # prefix, is what lets Anthropic serve the prior documents from cache as new ones
        # accumulate. (Anthropic prompt-caching: prefix match, ≤4 breakpoints, 20-block
        # lookback — the front position also keeps docs inside that lookback window.)
        if documents:
            document_blocks: list[dict[str, Any]] = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": document.mime_type,
                        "data": base64.b64encode(document.path.read_bytes()).decode("ascii"),
                    },
                }
                for document in documents
            ]
            if cache_prefix:
                document_blocks[-1]["cache_control"] = {"type": "ephemeral"}
            provider_messages.insert(0, {"role": "user", "content": document_blocks})

        # Cross-agent figures (peer plots, a worker's figures the reviewer must see) have no
        # tool-turn in *this* conversation to attach to, so they ride this call-time channel —
        # a caption block then the image block, at the end. (Figures a tool produced *this*
        # loop instead live inline at their tool-result turn; see `_to_anthropic_messages`.)
        if images:
            image_content: list[dict[str, Any]] = []
            for figure in images:
                image_content.extend(_figure_blocks(figure))
            provider_messages.append({"role": "user", "content": image_content})

        system_param = _system_param(system, cache_prefix)
        if cache_prefix:
            _mark_last_block_cacheable(provider_messages)

        # Provider-native context editing (Anthropic beta): on the cache_prefix paths — the
        # ReAct tool loop and the terminal report call — clear the oldest tool RESULTS
        # server-side once the request crosses the token trigger, keeping the last N. This is
        # a context-window pressure valve for long loops (with prompt caching, raw history is
        # cheap and any clear breaks the cached prefix, so clear_at_least amortizes it). Routed
        # through the beta.messages namespace only when active, so every other call stays
        # byte-identical to before. Live-verified 2026-07-28.
        ctx_cfg = context_editing_config()
        use_ctx_edit = cache_prefix and ctx_cfg.enabled
        ctx_extra: dict[str, Any] = {}
        if use_ctx_edit:
            ctx_extra["betas"] = [ctx_cfg.beta_header]
            ctx_extra["context_management"] = {
                "edits": [
                    {
                        "type": ctx_cfg.strategy,
                        "trigger": {"type": "input_tokens", "value": ctx_cfg.trigger_tokens},
                        "keep": {"type": "tool_uses", "value": ctx_cfg.keep_tool_uses},
                        "clear_at_least": {
                            "type": "input_tokens",
                            "value": ctx_cfg.clear_at_least_tokens,
                        },
                    }
                ]
            }

        client = AsyncAnthropic()
        # beta.messages accepts betas + context_management; its response carries the same
        # content/usage shape, and ParsedBetaMessage.parsed_output is a property mirroring the
        # non-beta parse — so the extraction below is identical on either namespace.
        messages_api = client.beta.messages if use_ctx_edit else client.messages

        if schema is not None:
            response = await messages_api.parse(
                model=config.model,
                max_tokens=max_tokens,
                messages=provider_messages,
                output_format=schema,
                **({"system": system_param} if system is not None else {}),
                **params,
                **ctx_extra,
            )
            parsed_obj = response.parsed_output
            text_parts = [
                block.text for block in response.content if getattr(block, "type", None) == "text"
            ]
            usage = getattr(response, "usage", None)
            return ModelResponse(
                content="".join(text_parts),
                parsed=parsed_obj.model_dump(mode="json") if parsed_obj is not None else None,
                usage=_anthropic_usage(usage),
                context_edits=_context_edits(response),
                raw=response,
            )

        request: dict[str, Any] = {
            "model": config.model,
            "max_tokens": max_tokens,
            "messages": provider_messages,
            **params,
        }
        if system is not None:
            request["system"] = system_param
        if tools is not None:
            ts_cfg = tool_search_config()
            tool_defs = []
            for tool in tools:
                entry: dict[str, Any] = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object", "properties": {}}),
                }
                # Deferred tools stay out of the system-prompt prefix; Claude pulls one in
                # via the search tool, which appends a ``tool_reference`` inline in the
                # conversation, so the cached prefix is untouched. Live-verified 2026-07-28: the
                # expanded definition falls inside the conversation cache breakpoint too.
                if ts_cfg.enabled and tool.get("defer_loading"):
                    entry["defer_loading"] = True
                tool_defs.append(entry)
            if ts_cfg.enabled and any(t.get("defer_loading") for t in tool_defs):
                # Anthropic requires ≥1 non-deferred tool; the harness tools are it.
                tool_defs.append({
                    "type": ts_cfg.anthropic_tool,
                    "name": anthropic_search_tool_name(ts_cfg.anthropic_tool),
                })
            # When there is no system block to carry the breakpoint, cache the tool
            # defs directly (they render first, so this caches the whole prefix). The
            # breakpoint must land on a NON-deferred tool: a deferred tool carrying
            # ``cache_control`` is a 400, since it is not part of the prefix being cached.
            if cache_prefix and system is None and tool_defs:
                resident = [t for t in tool_defs if not t.get("defer_loading")]
                if resident:
                    resident[-1]["cache_control"] = {"type": "ephemeral"}
            request["tools"] = tool_defs
            request["tool_choice"] = {"type": "any"} if tool_choice == "required" else {"type": "auto"}

        response = await messages_api.create(**request, **ctx_extra)

        text_parts = []
        tool_calls: list[ToolCall] = []
        provider_parts: list[dict[str, Any]] = []
        for block in response.content:
            btype = getattr(block, "type", None)
            # EVERY block is carried, in emission order — this list is the turn, and
            # `_assistant_blocks` replays it verbatim. The typed extractions below are views
            # onto it for the harness, not a second source of truth.
            provider_parts.append(_block_to_dict(block))
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {}))
                )

        usage = getattr(response, "usage", None)
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=_anthropic_usage(usage),
            context_edits=_context_edits(response),
            provider_parts=provider_parts,
            raw=response,
        )


def _anthropic_usage(usage: Any) -> ModelUsage:
    """Map an Anthropic ``usage`` object into the neutral non-overlapping buckets ([I5]).

    Anthropic's ``input_tokens`` is already the *uncached* remainder; cached volume is reported
    separately as ``cache_read_input_tokens`` (~0.1x) and ``cache_creation_input_tokens`` (~1.25x),
    so no subtraction is needed. Thinking is billed as output and already counted in
    ``output_tokens``, so ``reasoning_tokens`` stays 0.
    """
    if usage is None:
        return ModelUsage()
    return ModelUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _context_edits(response: Any) -> list[dict[str, Any]]:
    """Extract the provider-native context-management edits applied to ``response``.

    Anthropic reports cleared context under ``response.context_management.applied_edits``
    (present only on the beta context-management path; each edit e.g.
    ``{"type": "clear_tool_uses_20250919", "cleared_tool_uses": N, "cleared_input_tokens": M}``).
    Returns ``[]`` when the field is absent (every non-beta call). Live-verified 2026-07-28.
    """
    cm = getattr(response, "context_management", None)
    applied = getattr(cm, "applied_edits", None) if cm is not None else None
    if not applied:
        return []
    edits: list[dict[str, Any]] = []
    for edit in applied:
        try:
            edits.append(edit.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 — a report we cannot serialize must never break the call
            edits.append({"type": getattr(edit, "type", "unknown")})
    return edits


def _figure_blocks(figure: FigureRef) -> list[dict[str, Any]]:
    """Render one figure as Anthropic content blocks: a caption then the image bytes.

    Loading bytes here is the multimodal boundary — the only place a figure's pixels
    enter a request (elsewhere figures travel as lightweight ``FigureRef``). The caption is
    rendered verbatim: the presentation layer may prefix it with the report-local number
    ("Figure 1 — …") or the catalog handle ("[analyst_worker_0:2] …"), so prose references
    line up with the images (V-4).
    """
    return [
        {"type": "text", "text": f"{figure.caption} (produced by {figure.produced_by})"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": figure.mime_type,
                "data": base64.b64encode(figure.path.read_bytes()).decode("ascii"),
            },
        },
    ]


def _assistant_blocks(turn: ModelResponse, include_provider_parts: bool) -> list[dict]:
    """Rebuild one assistant turn's content blocks.

    The documented rule for a tool-use continuation is to **echo the assistant message exactly as
    received**: *"when you return a tool result, the thinking blocks from the assistant message
    must come back with it"*, and *"rebuilding the message or filtering out `redacted_thinking`
    blocks triggers a 400 error"*
    (platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows). Its sample code is
    literally ``{"role": "assistant", "content": response.content}``. So when we hold the array
    Anthropic authored — the normal path — it goes back whole, unfiltered and unreordered.

    Two cases have no array to echo. A turn **we synthesized** (a test fake, anything built as
    ``ModelResponse(tool_calls=[…])``) never had one. And a request declaring **no tools** — our
    terminal structured compose — cannot echo one: a block naming a tool is rejected when that
    tool is absent from the request (*"Tool reference 'x' not found in available tools"*,
    run-killer 2026-07-29), and per the rule above we may not filter those blocks out either
    (confirmed live 2026-08-05: dropping only the tool-referencing blocks still 400s). Both cases
    fall back to rebuilding from the interpreted views, which is what this adapter did before any
    part was carried. Note what that costs: the compose model does not see its own prior
    reasoning. The documented way out is for the compose to declare its tools, which puts it back
    on the echo path — a change deferred out of this one.
    """
    if turn.provider_parts and include_provider_parts:
        return list(turn.provider_parts)
    blocks: list[dict] = []
    if turn.content:
        blocks.append({"type": "text", "text": turn.content})
    for call in turn.tool_calls:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return blocks


def _to_anthropic_messages(
    messages: Sequence[ConversationMessage],
    *,
    include_provider_parts: bool = True,
) -> tuple[str | None, list[dict]]:
    """Split system text out and translate the rest into Anthropic message dicts.

    ``ModelResponse`` → an assistant message from that turn's blocks (:func:`_assistant_blocks`);
    ``ToolResultMessage`` → a user message with ``tool_result`` blocks; plain
    ``ModelMessage`` → a text message (system text is joined and returned).

    ``include_provider_parts=False`` (a request declaring no tools) replays the assistant turn
    from the interpreted views instead of its carried blocks — see :func:`_assistant_blocks` for
    why the two API rules leave no third option there.
    """
    system_texts: list[str] = []
    provider_messages: list[dict] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            blocks = _assistant_blocks(message, include_provider_parts)
            provider_messages.append({"role": "assistant", "content": blocks})
        elif isinstance(message, ToolResultMessage):
            content: list[dict] = [
                {
                    "type": "tool_result",
                    "tool_use_id": part.tool_call_id,
                    "content": part.content,
                    "is_error": part.is_error,
                }
                for part in message.results
            ]
            # A tool's figures live inline at this turn: caption+image blocks appended after
            # the tool_result blocks in the SAME user message (Anthropic allows mixed content
            # there), so they replay in place every round and cache with the growing prefix.
            for part in message.results:
                for figure in part.figures:
                    content.extend(_figure_blocks(figure))
            provider_messages.append({"role": "user", "content": content})
        elif message.role == "system":
            system_texts.append(message.content)
        else:
            provider_messages.append({"role": message.role, "content": message.content})
    return ("\n".join(system_texts) or None), provider_messages


def _system_param(system: str | None, cache_prefix: bool) -> Any:
    """Return the ``system`` request value, marking it cacheable when requested."""
    if system is None:
        return None
    if cache_prefix:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def _mark_last_block_cacheable(provider_messages: list[dict]) -> None:
    """Mark the current Anthropic conversation tail as a prompt-cache breakpoint."""
    if not provider_messages:
        return
    content = provider_messages[-1].get("content")
    cache_control = {"type": "ephemeral"}
    if isinstance(content, str):
        provider_messages[-1]["content"] = [
            {"type": "text", "text": content, "cache_control": cache_control}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = cache_control
