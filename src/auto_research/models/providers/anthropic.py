"""Anthropic provider adapter."""

import base64
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from auto_research.env import context_editing_config, tool_search_config
from auto_research.models.keys import ANTHROPIC, active_key
from auto_research.models.providers._tool_search import anthropic_search_tool_name
from auto_research.models.providers._media import (
    MediaLimits,
    MediaPlan,
    caption_with_note,
    nonmedia_weight,
    plan_for_call,
)
from auto_research.models.types import (
    ConversationMessage,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    RoleModelConfig,
    ToolCall,
    ToolResultMessage,
    stop_from_anthropic,
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


# Anthropic's documented media limits (platform.claude.com/docs/en/build-with-claude/vision,
# "Request limits"; .../pdf-support). Enforced per request by `_media.plan_for_call`, the only
# place that sees every image in the conversation at once.
_LIMITS = MediaLimits(
    # "Claude supports JPEG, PNG, GIF, and WebP images".
    image_mimes=frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"}),
    doc_mimes=frozenset({"application/pdf", "text/plain"}),
    # "100 per request on the API, for models with a 200k-token context window."
    max_images=100,
    # "If a single API request contains more than 20 images, a stricter per-image dimension limit
    # applies… resize each image so that neither dimension exceeds 2000 px" — the rule that killed
    # run 8c75990e, where a 2677x1554 figure rode a request carrying >20 image blocks.
    strict_block_threshold=20,
    strict_max_edge=2000,
    # "The maximum dimensions per image are 8000x8000 px."
    max_edge=8000,
    # "10 MB (base64-encoded) when using the Claude API directly."
    max_image_bytes=10 * 1024 * 1024,
    # "Maximum request size 32 MB" — the ceiling for the whole payload, PDFs included.
    max_request_bytes=32 * 1024 * 1024,
    # "Maximum pages per request 600 (100 when the request's context window is under 1M tokens)":
    # our configured models are the sub-1M tier, so 100 is the live limit.
    max_pdf_pages=100,
)


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
        # One media plan for the WHOLE request: the count/dimension/payload rules are defined over
        # everything the request carries, and this is the only point that sees all of it (§stateless).
        # The media budget is the request ceiling MINUS everything else this request
        # carries: Anthropic's limits are "on the entire request payload, including any
        # other content sent alongside PDFs" (.../build-with-claude/pdf-support).
        plan = plan_for_call(
            messages, images, documents, _LIMITS,
            reserved_bytes=nonmedia_weight(messages, tools=tools),
        )
        system, provider_messages = _to_anthropic_messages(messages, plan)

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
            document_blocks: list[dict[str, Any]] = []
            for document in documents:
                resolved = plan.resolve(document)
                if resolved.dropped:
                    # Withheld, never silent: the model is told which document it is missing and
                    # why, so it cites from metadata rather than reasoning as if it had read it.
                    document_blocks.append({"type": "text", "text": resolved.note or ""})
                    continue
                document_blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": resolved.mime,
                            "data": resolved.b64(),
                        },
                    }
                )
            if cache_prefix and document_blocks:
                document_blocks[-1]["cache_control"] = {"type": "ephemeral"}
            if document_blocks:
                provider_messages.insert(0, {"role": "user", "content": document_blocks})

        # Cross-agent figures (peer plots, a worker's figures the reviewer must see) have no
        # tool-turn in *this* conversation to attach to, so they ride this call-time channel —
        # a caption block then the image block, at the end. (Figures a tool produced *this*
        # loop instead live inline at their tool-result turn; see `_to_anthropic_messages`.)
        if images:
            image_content: list[dict[str, Any]] = []
            for figure in images:
                image_content.extend(_figure_blocks(figure, plan))
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

        # The key is read from the pool rather than left to the SDK's own environment
        # lookup, because a key-scoped failure (rate limit, spent balance) rotates the live
        # key mid-run and the SDK resolves `ANTHROPIC_API_KEY` once per client. `None` means
        # no key is configured here, which hands resolution back to the SDK unchanged.
        client = AsyncAnthropic(api_key=active_key(ANTHROPIC))
        # beta.messages accepts betas + context_management; its response carries the same
        # content/usage shape, and ParsedBetaMessage.parsed_output is a property mirroring the
        # non-beta parse — so the extraction below is identical on either namespace.
        messages_api = client.beta.messages if use_ctx_edit else client.messages

        # ONE request, built from whatever the caller gave us. `schema` and `tools` are
        # independent: declaring both lets a terminal compose keep its tool declarations while
        # `tool_choice={"type": "none"}` forbids calling them, so the trajectory's `tool_use`
        # blocks always resolve against declared tools. `output_format` lives on `parse`, so the
        # SDK method differs — the request and the extraction do not. Live-verified 2026-08-06:
        # `parse` accepts `tools` + `tool_choice` + `thinking` in one call.
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
                # Strict mode is OPT-IN per tool and off by default: without it Anthropic
                # documents that Claude "might return incompatible types or omit required
                # fields", which is exactly what a live lead did to a required chain
                # (2026-09-02). A tool that asks for enforcement gets it; the schema must then
                # sit inside the compilable subset (`additionalProperties: false` on every
                # object, every property in `required`), which `react._strictify` guarantees.
                if tool.get("strict"):
                    entry["strict"] = True
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
            request["tool_choice"] = _tool_choice_block(tool_choice)

        if schema is not None:
            response = await messages_api.parse(
                output_format=schema, **request, **ctx_extra
            )
            parsed_obj = response.parsed_output
            parsed = parsed_obj.model_dump(mode="json") if parsed_obj is not None else None
        else:
            response = await messages_api.create(**request, **ctx_extra)
            parsed = None

        # ONE extraction pass over the turn the model authored, whichever method sent it.
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
            parsed=parsed,
            tool_calls=tool_calls,
            usage=_anthropic_usage(usage),
            stop=stop_from_anthropic(response),
            context_edits=_context_edits(response),
            provider_parts=provider_parts,
            raw=response,
        )


def _tool_choice_block(tool_choice: str) -> dict[str, str]:
    """Neutral ``tool_choice`` → the Messages-API block.

    ``"none"`` keeps the declarations in the request while forbidding a call; Anthropic
    prices it alongside ``auto`` in the tool-use system-prompt table
    (platform.claude.com/docs/en/agents-and-tools/tool-use/overview), i.e. the tools are
    still sent. A terminal compose wants exactly that: the trajectory's ``tool_use`` blocks
    resolve, and the model must answer rather than call again.
    """
    if tool_choice == "required":
        return {"type": "any"}
    return {"type": "none"} if tool_choice == "none" else {"type": "auto"}


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


def _figure_blocks(figure: FigureRef, plan: MediaPlan | None = None) -> list[dict[str, Any]]:
    """Render one figure as Anthropic content blocks: a caption then the image bytes.

    Loading bytes here is the multimodal boundary — the only place a figure's pixels
    enter a request (elsewhere figures travel as lightweight ``FigureRef``). The caption is
    rendered verbatim: the presentation layer may prefix it with the report-local number
    ("Figure 1 — …") or the catalog handle ("[analyst_worker_0:2] …"), so prose references
    line up with the images (V-4).
    """
    caption = f"{figure.caption} (produced by {figure.produced_by})"
    resolved = (plan or MediaPlan()).resolve(figure)
    text = {"type": "text", "text": caption_with_note(caption, resolved)}
    if resolved.dropped:
        # The caption and the reason survive even when the pixels cannot: a model told what it is
        # missing reasons honestly about it, where a silently absent figure invites invention.
        return [text]
    return [
        text,
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": resolved.mime,
                "data": resolved.b64(),
            },
        },
    ]


def _assistant_blocks(turn: ModelResponse) -> list[dict]:
    """Rebuild one assistant turn's content blocks.

    The documented rule for a tool-use continuation is to **echo the assistant message exactly as
    received**: *"when you return a tool result, the thinking blocks from the assistant message
    must come back with it"*, and *"rebuilding the message or filtering out `redacted_thinking`
    blocks triggers a 400 error"*
    (platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows). Its sample code is
    literally ``{"role": "assistant", "content": response.content}``. So when we hold the array
    Anthropic authored — the normal path — it goes back whole, unfiltered and unreordered.

    Only a turn **we synthesized** (a test fake, anything built as
    ``ModelResponse(tool_calls=[…])``) has no array to echo; it is rebuilt from the interpreted
    views. A request declaring no tools used to take that path too, because a block naming a tool
    is rejected when that tool is absent (*"Tool reference 'x' not found in available tools"*,
    run-killer 2026-07-29) and filtering the offending blocks out is what the rule above forbids.
    That dilemma is now resolved upstream rather than here: a terminal compose declares its tools
    with ``tool_choice={"type": "none"}``, so every reference resolves and the echo path always
    applies.
    """
    if turn.provider_parts:
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
    plan: MediaPlan | None = None,
) -> tuple[str | None, list[dict]]:
    """Split system text out and translate the rest into Anthropic message dicts.

    ``ModelResponse`` → an assistant message from that turn's blocks (:func:`_assistant_blocks`);
    ``ToolResultMessage`` → a user message with ``tool_result`` blocks; plain
    ``ModelMessage`` → a text message (system text is joined and returned).
    """
    system_texts: list[str] = []
    provider_messages: list[dict] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            blocks = _assistant_blocks(message)
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
            provider_messages.append({"role": "user", "content": content})
            # A tool's figures live inline at this turn, but in their OWN following user message —
            # never appended after the tool_result blocks in the same one (F5f).
            #
            # Mixed content there is legal ("any text must come AFTER all tool results"), and that
            # is what this used to do. But the stop-reasons page names exactly that arrangement as
            # the cause of an accidental empty turn: *"Sometimes Claude returns an empty response
            # (exactly 2-3 tokens with no content) with `stop_reason: "end_turn"` ... particularly
            # after tool results"*, with the prevention *"Never add text blocks immediately after
            # tool results"* (platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).
            # `_figure_blocks` renders a caption — a text block — then the image, so every
            # figure-bearing tool result was producing that shape. Slot 3's lead had figures on
            # its turns and returned `output_tokens: 2`, killing the run.
            #
            # A separate user message keeps every other property: the figures still replay in
            # place every round, still cache with the growing prefix, and still sit adjacent to
            # the result that produced them.
            figure_content: list[dict] = []
            for part in message.results:
                for figure in part.figures:
                    figure_content.extend(_figure_blocks(figure, plan))
            if figure_content:
                provider_messages.append({"role": "user", "content": figure_content})
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
