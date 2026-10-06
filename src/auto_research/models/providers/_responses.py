"""Wire logic for the OpenAI Responses API, shared by every vendor that publishes it.

**Why this is shared rather than copied.** The Responses dialect is not OpenAI's alone: DeepSeek,
Z.AI and Alibaba Model Studio each publish an endpoint in it (the table in
``sandbox/coder/harness.py`` records where, and which of them a coder harness may drive). The
logic below is what it costs to speak that dialect correctly — the input projection, the
capture-time field filtering, the replay ordering, the one-request/one-extraction pass — and it
was written against three live run-killers in one week (2026-08-03, -05, -06). Copying it per
vendor would fork those fixes four ways and rot; the vendors differ in *policy*, not in wire
format.

**So the split mirrors ``_media.py``:** the mechanics live here, and each vendor module holds a
:class:`ResponsesVendor` describing only what that vendor's own documentation states, beside its
citations. A capability a vendor does not document stays off, which is the same rule the media
guards follow: enforce what is published, invent nothing from an error we never saw.

Two asymmetries decide who may hold a role. A vendor can accept ``text.format`` and **ignore**
it, answering with prose or keys of its own invention, so ``supports_strict_schema`` is a
measured fact rather than an assumption from the dialect; where it is false, a schema instead
rides as forced tool-call arguments (``schema_via_tool``), the route a coding harness already
uses. And a compatibility layer generally does **not** resolve ``$ref`` into ``$defs``, so a
a schema is dereferenced for it — but only in the ``$ref`` positions that vendor is measured to
need (``resolve_refs_in``), because resolving elsewhere is not free: on one vendor it turns a
working shape into a failing one.
"""

import base64
import json
import re
import typing
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel

from auto_research.env import ToolSearchConfig, tool_search_config
from auto_research.models.keys import resolve_key
from auto_research.models.providers._doc_parse import ParsedDocument, parse_document
from auto_research.models.providers._json import loads_first_json_object
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
    stop_from_openai_responses,
)
from auto_research.schemas import DocumentRef, FigureRef


_DISCOVERY_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})
"""Tool-search items: the reason any of this exists. A tool discovered once stays callable only
while these survive replay."""

#: Provider-executed item types whose RESULT arrives as a SEPARATE output item, mapped to that
#: result's type. Tool search is the only one in this dialect, and only in *client* execution
#: mode: hosted mode returns both items in the same response
#: (developers.openai.com/api/docs/guides/tools-tool-search — *"the response includes two
#: additional output items … `tool_search_call` … `tool_search_output`"*, and in hosted mode
#: *"`execution` is set to `server` and `call_id` is set to `null`"*).
_PAIRED_SERVER_TOOL_ITEMS: dict[str, str] = {"tool_search_call": "tool_search_output"}

#: Provider-executed item types that carry their own result INLINE on the call item
#: (`action`/`results`/`outputs`/`result`/`output`), so the only way one is unresolved is a
#: non-terminal `status`. `mcp_list_tools` is deliberately absent: it declares no `status` field
#: at all and carries its `tools` inline, so it is a listing rather than a call that can pend, and
#: including it would make the fail-closed absent-status rule fire on every one.
_INLINE_SERVER_TOOL_ITEMS = frozenset({
    "web_search_call",
    "file_search_call",
    "code_interpreter_call",
    "image_generation_call",
    "mcp_call",
})

#: The OpenAI hosted set, and the default for any vendor that declares nothing (see
#: ``ResponsesVendor.server_tool_item_types``).
_OPENAI_SERVER_TOOL_ITEMS = frozenset(_PAIRED_SERVER_TOOL_ITEMS) | _INLINE_SERVER_TOOL_ITEMS

#: Item statuses that mean the provider is finished with that item. Anything else — including an
#: ABSENT status on a type that declares one — reads pending, which is the fail-closed direction.
_TERMINAL_ITEM_STATUSES = frozenset({"completed", "failed", "incomplete"})

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


def _raise_billed(exc: Exception, response: Any) -> None:
    """Raise ``exc``, carrying the usage of the response that was already billed (N6).

    Every raise below fires **after** a successful HTTP response, so the vendor has generated —
    and charged for — those tokens. `traced_model_call` records usage only on the return path, so
    an adapter-raised turn was silently free: slot 5's two failing attempts (~195k input tokens)
    appear neither in its ledger nor in its reported $29.23.

    The exception **type is preserved**, because the role layer branches on it (`ValueError` is
    retried, `RuntimeError` ends the call). The usage rides as an attribute instead.
    """
    exc.billed_usage = _responses_usage(getattr(response, "usage", None))  # type: ignore[attr-defined]
    raise exc


def _tool_choice_value(tool_choice: str) -> str:
    """Neutral ``tool_choice`` → the Responses API spelling.

    ``"none"`` keeps the declarations in the request while forbidding a call —
    *"set `tool_choice` to `none` to imitate the behavior of passing no functions"*
    (developers.openai.com/api/docs/guides/function-calling). That is what a terminal
    compose wants: the trajectory's items still resolve against declared tools, and the
    model must answer rather than call again.
    """
    return tool_choice if tool_choice in ("required", "none") else "auto"


# JSON Schema string `format` values OpenAI structured outputs accepts. Pydantic
# emits others (e.g. `path` for `Path`, `uri` for `HttpUrl`) that the API rejects.
_STRICT_SCHEMA_FORMATS = frozenset({
    "date-time", "date", "time", "duration", "email", "hostname",
    "ipv4", "ipv6", "uuid",
})

# The subset every Responses-dialect vendor has been observed to accept. A compatibility layer may
# implement a narrower strict mode than the API it copies, and the temporal formats are where they
# diverge: DeepSeek refuses `date` and names its whole accepted set in the refusal
# ("unknown variant `date`, expected one of `email`, `hostname`, `ipv4`, `ipv6`, `uuid`"), which
# rejected three of the nine real schemas at request time. A `format` we drop costs nothing —
# it is an annotation on a string, not a constraint we rely on — so a vendor unsure of its own
# support is better given this set than a 400.
_CONSERVATIVE_SCHEMA_FORMATS = frozenset({"email", "hostname", "ipv4", "ipv6", "uuid"})


#: How much of an unusable emit-arguments string reaches the error message. Enough to see
#: whether it is truncated JSON, prose, or empty, without pasting a 16 kB payload into a
#: ledger event that is replayed on every later turn.
_RAW_ARGUMENTS_CAP = 2000

_EMIT_TOOL = "emit_structured_response"
"""Name of the synthetic tool that carries a structured answer on a ``schema_via_tool`` vendor.

Deliberately distinct from anything a toolkit could register, because the adapter consumes this
call into ``parsed`` and must never hand it to the ReAct loop as real work to execute."""


_NO_TOOL_SEARCH = ToolSearchConfig(enabled=False, anthropic_tool="", openai_tool="")
"""Stand-in for a vendor that runs no server-side tool-search tool. Every tool is then declared
resident, which is what ``defer_loading`` costs when nobody can resolve a deferred reference."""

_REQUEST_TIMEOUT_S = 300.0
"""Wall-clock ceiling for one Responses HTTP attempt.

The official SDK default is ten minutes and it retries timeouts twice. This project owns retries
above the provider layer, with visible cooldown logging, so the client gets one bounded attempt
and the shared retry policy decides what happens next.
"""


@dataclass(frozen=True)
class ResponsesVendor:
    """One vendor's Responses-API policy: where it lives, and what its docs say it supports.

    Every flag defaults to the *OpenAI* behaviour, because this dialect is OpenAI's and the
    others are compatibility layers over it. A vendor module turns a flag off only with a
    citation, so the reason a parameter is withheld is always readable next to the withholding.
    """

    name: str
    """Provider name as configured. Also the key-pool identity, so it must match
    ``ProviderName``."""

    limits: MediaLimits
    """Media limits from *this* vendor's own documentation (see ``_media.MediaLimits``)."""

    base_url: str | None = None
    """Endpoint for this vendor, or ``None`` to let the SDK use its own default (OpenAI). A
    member's own ``base_url`` still wins, so a plan-specific host stays configurable."""

    key_env: str | None = None
    """Environment variable holding this vendor's key, when it is not the standard
    ``<PROVIDER>_API_KEY`` the key pool already derives."""

    supports_prompt_cache_key: bool = True
    """Whether ``prompt_cache_key`` is accepted. Prefix caching is automatic on this API either
    way, so a vendor that rejects the parameter loses only the cache-routing hint."""

    cache_headers: tuple[tuple[str, str], ...] = ()
    """Vendor headers sent on every request, such as an opt-in session-cache header."""

    supports_strict_schema: bool = True
    """Whether ``text.format`` with ``strict: true`` actually constrains the output. A vendor may
    accept the parameter and ignore it, which is worse than rejecting it: the call succeeds and
    returns keys the schema never declared."""

    supports_tool_search: bool = True
    """Whether the vendor runs the server-side tool-search tool that ``defer_loading`` needs."""

    server_tool_item_types: frozenset[str] = _OPENAI_SERVER_TOOL_ITEMS
    """Output-item types this vendor emits for a **provider-executed** tool (A1).

    Defaults to OpenAI's set like every other flag here, which is also the fail-safe direction: a
    vendor we have not read defaults to "can produce the hazard", where a wrong hold costs one
    iteration and the opposite error cost a run. A vendor empties it only with a citation.

    Declared, not detected, because detection is impossible from our side: three of Alibaba's
    documented hosted types are absent from openai's own input union, so they ride through
    ``_as_input_item`` unfiltered and would read as ordinary unknown items. The census test in
    ``test_responses_vendors.py`` is what turns "a vendor shipped a type we do not classify" from
    silence into a failure."""

    accepts_inline_documents: bool = True
    """Whether this vendor takes a document as an ``input_file`` part carrying inline base64
    ``file_data`` — the shape :meth:`ResponsesProvider.call` builds.

    A **capability**, not a volume limit, so it is declared per vendor from that vendor's own
    reference rather than inferred from a failure. ``True`` is the OpenAI shape and therefore the
    default, which keeps every vendor whose document reference has not been read byte-identical on
    the wire.

    ``False`` routes the document through :mod:`._doc_parse` instead: text plus cropped figures,
    assembled locally and announced to the model. Alibaba is the measured case — its
    Create-a-response reference accepts ``input_file`` by **``file_url`` (a public URL) only**,
    neither ``file_data`` nor ``file_id``, and states PDF input is "Currently only supported by
    ``qwen3.5-ocr``". Both halves rule the native path out here: our papers are local files under
    the runtime directory, and publishing one to obtain a URL is not something a run may do."""

    strict_schema_formats: frozenset[str] = _STRICT_SCHEMA_FORMATS
    """String ``format`` values this vendor's strict mode accepts. Narrower on a compatibility
    layer than on OpenAI itself, and a `format` outside the set is a 400 rather than a value the
    vendor ignores, so it is stripped before sending."""

    schema_via_tool: bool = False
    """Whether a structured answer is obtained as terminal tool-call arguments rather than
    through ``text.format``.

    This is the mechanism a coding harness already relies on: Claude Code's and Codex's own
    file-editing tools are JSON-Schema-parameterised functions, so a vendor that works inside
    those harnesses handles schema-shaped tool arguments even when its structured-output path is
    a no-op. Declaring one tool whose parameters *are* the schema therefore uses the route the
    vendor actually supports; :attr:`forces_tool_choice` records whether that call can be pinned.
    Live-verified on Z.AI 2026-08-24, on both dialects it publishes, with the real
    ``ToolReviewVerdict``.

    Set it only where ``supports_strict_schema`` is false; a vendor with native strict decoding
    has no reason to spend a tool slot."""

    forces_tool_choice: bool = True
    """Whether ``tool_choice`` may name a specific tool, or must be left at ``auto``.

    Thinking models commonly refuse to be compelled: Anthropic documents that with thinking only
    ``{"type": "auto"}`` or ``{"type": "none"}`` are supported and that ``{"type": "any"}`` or
    ``{"type": "tool", ...}`` are not, and Alibaba enforces the same rule with a clearer message
    ("The tool_choice parameter does not support being set to required or object in thinking
    mode"). The reasoning is the same on both: forcing a call pre-empts the deliberation thinking
    exists to produce.

    A vendor with this false still reaches a structured answer through :attr:`schema_via_tool` —
    the emit tool is declared and its description asks for exactly one call. What is lost is the
    structural guarantee, so the model *may* answer in prose instead; that case raises rather than
    returning a quiet ``parsed=None``. Measured 2026-08-24: under thinking with ``auto``, Qwen
    called the emit tool and the real ``ToolReviewVerdict`` validated."""

    resolve_refs_in: frozenset[str] = frozenset()
    """Which ``$ref`` positions this vendor needs resolved for it: ``"anyOf"``, ``"items"``.

    A boolean cannot express this, which is why it stopped being one. Measured at 5 draws per
    cell on 2026-08-24, every cell 0/5 or 5/5 with no variance:

    ==========  ======================  ====================
    vendor      ``anyOf: [{$ref}, …]``  ``items: {$ref}``
    ==========  ======================  ====================
    DeepSeek    refused (0/5)           fine (5/5)
    Z.AI        fine (5/5)              fine (5/5)
    Qwen        fine (5/5)              **refused (0/5)**
    ==========  ======================  ====================

    Qwen is the case that forced the shape: resolving its ``anyOf`` refs makes a *working* call
    fail 0/5 (it answers with a JSON-encoded string in the object field), while its ``items``
    refs must be resolved. One vendor therefore needs opposite treatment in two positions, so a
    per-vendor switch is wrong whichever way it is set.

    Resolving is not free and is never a safe default: it is applied only where a vendor is
    measured to need it. DeepSeek's ``/beta`` endpoint and its strict function-calling path were
    both probed and reject the same shape, so this is the vendor's rule rather than an endpoint
    artifact."""

    vision_models: frozenset[str] = field(default_factory=frozenset)
    """Models that accept image input, when the vendor serves vision on only some of them. Empty
    means every model does, which is the OpenAI case."""

    def endpoint_for(self, config: RoleModelConfig) -> str | None:
        """This call's base URL: the member's own if it names one, else the vendor default."""
        return config.base_url or self.base_url

    def limits_for(self, model: str) -> MediaLimits:
        """This vendor's media limits, narrowed to what ``model`` actually accepts.

        Volume limits are the vendor's; image capability can be per model, so a text-only model
        gets limits that withhold every image with a note rather than sending one into a 400.
        """
        if not self.vision_models or model in self.vision_models:
            return self.limits
        return replace(self.limits, images_accepted=False)


class ResponsesProvider:
    """Adapter over the OpenAI Responses API, parameterized by :class:`ResponsesVendor`.

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

    **One request, one extraction.** ``schema`` and ``tools`` are independent inputs, not
    branches: both ride the same ``responses.create`` (live-verified 2026-08-06) and the
    single pass over ``response.output`` fills whichever of ``content`` / ``parsed`` /
    ``tool_calls`` / ``provider_parts`` the response actually carried.
    """

    vendor: ResponsesVendor
    """Set by each vendor subclass. Everything vendor-specific is read from here."""

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
        """Call the vendor's Responses endpoint and normalize the response."""

        from openai import AsyncOpenAI

        vendor = self.vendor
        # One media plan for the WHOLE request (§stateless: this is the only point that sees
        # every image in the conversation plus this call's attachments at once).
        # A vendor with no usable file channel gets the document assembled locally instead: text
        # plus cropped figures. This happens BEFORE planning, deliberately — those crops are
        # images, and an image that skips the media plan is uncounted and unbudgeted, which is the
        # class of defect that killed run `8c75990e`. Substituted figures therefore ride the
        # ordinary `images` walk and are evicted oldest-first like any other block.
        substitutions: list[tuple[DocumentRef, ParsedDocument | None]] = []
        planned_images: list[FigureRef] = list(images)
        planned_documents: Sequence[DocumentRef] = documents
        if documents and not vendor.accepts_inline_documents:
            for document in documents:
                parsed = parse_document(document)
                substitutions.append((document, parsed))
                if parsed is not None:
                    planned_images.extend(parsed.figures)
            planned_documents = ()

        # Substituted text is ordinary request text the plan cannot see, so it is added to the
        # reserve explicitly. Leaving it out would let media fill a ceiling the text has already
        # eaten into — the same under-reserving that produced a live 413 with 42 KB of headroom.
        reserved = nonmedia_weight(messages, tools=tools) + sum(
            len(parsed.text.encode("utf-8")) for _, parsed in substitutions if parsed is not None
        )
        plan = plan_for_call(
            messages, planned_images, planned_documents, vendor.limits_for(config.model),
            reserved_bytes=reserved,
        )
        input_items = _to_responses_input(messages, plan)
        cache_kwargs = (
            {"prompt_cache_key": config.name}
            if cache_prefix and vendor.supports_prompt_cache_key
            else {}
        )
        attachment_content = []
        for document, parsed in substitutions:
            # Announced, never silent: the note names the file, says the text was extracted here
            # rather than read by the model, and says which figures came with it. A model that
            # quietly receives less than it expected invents the difference.
            attachment_content.append(
                {"type": "input_text", "text": _substituted_document_text(document, parsed)}
            )
            for figure in parsed.figures if parsed is not None else ():
                attachment_content.extend(_figure_items(figure, plan))
        for document in planned_documents:
            resolved = plan.resolve(document)
            if resolved.dropped:
                # Withheld, never silent — the model is told which document it lost and why.
                attachment_content.append({"type": "input_text", "text": resolved.note or ""})
                continue
            if resolved.note:  # sent, but changed since its receipt (F26): say so
                attachment_content.append({"type": "input_text", "text": resolved.note})
            attachment_content.append(
                {
                    "type": "input_file",
                    "filename": document.path.name,
                    "file_data": f"data:{resolved.mime};base64,{resolved.b64()}",
                }
            )
        # Cross-agent figures (from another agent's report) ride this call-time channel;
        # figures a tool produced *this* loop live inline at their tool-result turn instead
        # (see `_to_responses_input`).
        for figure in images:
            attachment_content.extend(_figure_items(figure, plan))
        if attachment_content:
            input_items.append({"role": "user", "content": attachment_content})

        # Explicit key, not the SDK's own env lookup: the SDK reads its key once per client
        # construction, so a rotation would never reach a client built this way. `resolve_key`
        # reads the live pool for a provider whose failover is wired and the plain variable
        # otherwise, which is the same resolution the coder harness uses — a run must not
        # authenticate two of its own actors differently.
        key, _slot = resolve_key(vendor.name, config.key_env or vendor.key_env)
        client = AsyncOpenAI(
            api_key=key or None,
            base_url=vendor.endpoint_for(config),
            timeout=_REQUEST_TIMEOUT_S,
            max_retries=0,
            default_headers=dict(vendor.cache_headers) or None,
        )

        # ONE request, built from whatever the caller gave us. `schema` and `tools` are
        # independent: declaring both is what lets the terminal compose keep its tool
        # declarations while `tool_choice="none"` forbids calling them, so no request ever
        # replays a trajectory whose items reference tools it did not declare.
        request: dict[str, Any] = {
            "model": config.model,
            "input": input_items,
            **cache_kwargs,
            **config.parameters,
        }
        # How this vendor can be made to answer in a schema. Native decoding where it exists;
        # otherwise the forced-tool route, which is the one its harness integrations use.
        emit_tool: dict[str, Any] | None = None
        if schema is not None:
            schema_json = _strict_json_schema_format(
                schema, vendor.strict_schema_formats, vendor.resolve_refs_in
            )
            if vendor.supports_strict_schema:
                request["text"] = {"format": schema_json}
            elif vendor.schema_via_tool:
                emit_tool = {
                    "type": "function",
                    "name": _EMIT_TOOL,
                    "description": (
                        f"Record the final {schema.__name__} answer. Call this exactly once, "
                        "with every required field filled from the work above."
                    ),
                    "parameters": schema_json["schema"],
                    "strict": False,
                }
            else:
                raise RuntimeError(
                    f"{vendor.name!r} accepts `text.format` but does not honour it: the call "
                    "succeeds and returns whatever keys the model chose, so a structured "
                    "response cannot be relied on. Give a schema-emitting role a member whose "
                    "provider constrains its output, or set `schema_via_tool` if this vendor "
                    "supports forced tool calls."
                )
        if tools is not None:
            ts_cfg = tool_search_config() if vendor.supports_tool_search else _NO_TOOL_SEARCH
            function_tools: list[dict[str, Any]] = []
            for tool in tools:
                entry: dict[str, Any] = {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    # Per-tool rather than blanket-False: a form whose whole purpose is that a
                    # required field cannot be left blank has to be enforced, and `strict` is the
                    # only thing that enforces it. Everything else stays permissive, because a
                    # strict schema must sit in the compilable subset and most tools here are not
                    # written for it.
                    "strict": bool(tool.get("strict", False)),
                }
                # Deferred: OpenAI keeps the name+description resident and withholds only the
                # parameter schema until the model discovers the tool, injecting it at the END
                # of the context window so the cache is preserved. Live-verified 2026-07-28.
                if ts_cfg.enabled and tool.get("defer_loading"):
                    entry["defer_loading"] = True
                function_tools.append(entry)
            if ts_cfg.enabled and any(t.get("defer_loading") for t in function_tools):
                function_tools.append({"type": ts_cfg.openai_tool})
            request["tools"] = function_tools
            request["tool_choice"] = _tool_choice_value(tool_choice)

        if emit_tool is not None:
            # The loop's own tools stay DECLARED beside the emit tool, so a replayed trajectory
            # still resolves every item that names one — the same reason the terminal compose
            # declares tools under `tool_choice="none"`. Forcing the emit tool replaces that
            # "none", which is what makes the two requirements compatible on one request.
            request["tools"] = [*request.get("tools", []), emit_tool]
            if vendor.forces_tool_choice:
                request["tool_choice"] = {"type": "function", "name": _EMIT_TOOL}
            else:
                # This vendor refuses to be compelled while thinking, and we want the thinking.
                # `auto` keeps the emit tool available and lets the model choose it; declining is
                # then a loud failure below rather than a silent one.
                request["tool_choice"] = "auto"
                # Without compulsion the instruction is the only thing that carries, and a task
                # prompt written for native structured output may actively forbid tool calls —
                # which here forbids the answer itself. Stating it last makes the emit call the
                # most recent instruction the model reads. Measured 2026-08-24: a prompt ending
                # "Do not call any tool" otherwise produces no emit call at all.
                request["input"] = [
                    *request["input"],
                    {
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": (
                                f"Deliver your final answer by calling `{_EMIT_TOOL}` exactly "
                                "once, with every required field filled. That call IS the "
                                "answer, so make it even if you were told not to use tools; "
                                "any other tool here stays off-limits."
                            ),
                        }],
                    },
                ]

        response = await client.responses.create(**request)

        # ONE extraction pass: the response carries what it carries, and the caller reads the
        # view it needs. Structured output and tool calls are not alternatives here — a request
        # declaring both can come back either way.
        text = getattr(response, "output_text", "") or ""
        emitted: dict[str, Any] | None = None
        emitted_raw: str | None = None
        emitted_fault: str | None = None
        tool_calls: list[ToolCall] = []
        provider_parts: list[dict[str, Any]] = []
        for item in getattr(response, "output", []) or []:
            itype = getattr(item, "type", None)
            if itype == "function_call":
                raw_arguments = getattr(item, "arguments", None)
                try:
                    args = json.loads(raw_arguments) if raw_arguments else {}
                    arg_fault: str | None = None
                except (json.JSONDecodeError, TypeError) as exc:
                    # Kept, not swallowed. A parse failure and a genuinely empty call used to
                    # collapse to the same `{}` here, with the raw string dropped — so a live
                    # run that died four times on an empty structured answer could not be
                    # diagnosed at all, and a probe of 12 draws could not reproduce it
                    # (2026-09-14, reviewer/qwen3.8-max). The fault travels to the emit branch
                    # below, which is the only place that knows whether it mattered.
                    args = {}
                    arg_fault = f"{type(exc).__name__}: {exc}"
                if emit_tool is not None and getattr(item, "name", "") == _EMIT_TOOL:
                    # The structured answer, arriving as arguments. It is consumed here and
                    # deliberately NOT appended to `tool_calls`: the ReAct loop would otherwise
                    # try to execute a tool that does not exist, and replay would emit a
                    # `function_call` with no matching output.
                    emitted = _normalise_null_strings(args, emit_tool["parameters"])
                    emitted_raw, emitted_fault = raw_arguments, arg_fault
                    continue
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
                        arguments_error=(
                            None if arg_fault is None
                            else f"{arg_fault}; raw arguments: {str(raw_arguments)[:2000]}"
                        ),
                    )
                )
            elif itype not in _UNCARRIED_ITEM_TYPES:
                # Everything else the provider authored is carried whole, in output order, so
                # a later turn can hand it back: the discovery items (which keep a searched
                # tool callable) and the reasoning items they depend on. Translated to their
                # input shape *here*, at capture, so each is legal to send by construction and
                # is the same bytes on every turn that replays it (see `_as_input_item`).
                provider_parts.append(_as_input_item(item))
        if schema is None:
            parsed = None
        elif emit_tool is not None:
            # Three outcomes, and they are NOT one condition. The distinction is which exception
            # type: the role layer retries `ValueError` and lets `RuntimeError` end the call
            # (`roles.py`, "shape problems are retryable; everything else propagates"), so
            # choosing the wrong one either kills a recoverable turn or loops on a dead one.
            if emitted is None and tool_calls:
                # The model is still working: it asked for a loop tool instead of answering. That
                # is a turn to continue, not a broken vendor — the same "premature" condition as
                # emitting alongside a tool call, and retryable for the same reason.
                #
                # This branch exists because Z.AI ignores a named `tool_choice` (measured 5/5,
                # 2026-08-27; see `zai.py`). It previously fell into the RuntimeError below, whose
                # non-retryability then ended 14 of 116 reviews in the gate_bench swap.
                names = ", ".join(call.name for call in tool_calls)
                _raise_billed(ValueError(
                    f"{vendor.name!r} requested tool calls ({names}) instead of delivering "
                    f"{schema.__name__} through `{_EMIT_TOOL}`; the turn is unfinished and will "
                    "be retried."
                ), response)
            if emitted is None:
                # No answer and nothing pending: the vendor declined outright. Not a shape problem
                # a reworded retry can fix, so it stays non-retryable and loud.
                _raise_billed(RuntimeError(
                    f"{vendor.name!r} was asked for {schema.__name__} through a "
                    f"`{_EMIT_TOOL}` call and returned neither that call nor any tool call, so "
                    "there is no structured answer to parse and nothing in progress."
                ), response)
            if tool_calls:
                names = ", ".join(call.name for call in tool_calls)
                _raise_billed(ValueError(
                    f"{vendor.name!r} returned a structured answer while also requesting "
                    f"tool calls ({names}); the answer is premature and will be retried."
                ), response)
            if not emitted:
                # The emit call arrived carrying nothing usable. This used to pass `{}` down to
                # the role layer, where Pydantic rejected it with `input_value={}` and no trace
                # of what the vendor actually sent — the shape that killed a run on 2026-09-14
                # and that 12 live draws then could not reproduce. Retryable (ValueError), the
                # same class as the native branch's unparseable answer, and reported the same
                # way: status and `incomplete_details` distinguish truncation from a refusal,
                # and the raw argument string distinguishes malformed JSON from an empty call.
                status = getattr(response, "status", None)
                incomplete = getattr(response, "incomplete_details", None)
                raw = emitted_raw if emitted_raw is not None else ""
                shown = raw[:_RAW_ARGUMENTS_CAP]
                if len(raw) > _RAW_ARGUMENTS_CAP:
                    shown += f"… [+{len(raw) - _RAW_ARGUMENTS_CAP} chars]"
                _raise_billed(ValueError(
                    f"{vendor.name!r} delivered an empty {schema.__name__} through "
                    f"`{_EMIT_TOOL}` (status={status!r}, incomplete_details={incomplete!r}, "
                    f"raw_argument_chars={len(raw)}, json_fault={emitted_fault!r}); "
                    f"retrying. Raw arguments: {shown!r}"
                ), response)
            parsed = emitted
        else:
            parsed = loads_first_json_object(text)
            if parsed is None:
                # Native `text.format` came back with nothing parseable. Retryable (ValueError, so
                # the role layer's bounded structured retry gets it), because for at least one
                # vendor this is a documented TRANSIENT defect rather than a wiring fault:
                # DeepSeek's JSON-output guide states "the API may occasionally return empty
                # content. We are actively working on optimizing this issue"
                # (api-docs.deepseek.com/guides/json_mode), and deepseek-ai/DeepSeek-V3#1453
                # reports `deepseek-v4-pro` returning an empty response specifically after tool
                # results are fed back — the long-trajectory shape, which is why this hit 25 per
                # cent of authoring reviews and 2 per cent of the much shorter gate reviews.
                #
                # The status is included because without it the failure is indistinguishable
                # between empty content, output-budget truncation, and a refusal, which are three
                # different fixes. Community guidance for this vendor is explicitly to log
                # `finish_reason` alongside a bounded retry rather than to retry blind.
                status = getattr(response, "status", None)
                incomplete = getattr(response, "incomplete_details", None)
                _raise_billed(ValueError(
                    f"{vendor.name!r} returned no parseable {schema.__name__} from native "
                    f"`text.format` (status={status!r}, incomplete_details={incomplete!r}, "
                    f"output_chars={len(text)}); retrying."
                ), response)
        return _response(
            content=text,
            parsed=parsed,
            tool_calls=tool_calls,
            provider_parts=provider_parts,
            raw=response,
            vendor=vendor,
        )


def _substituted_document_text(document: DocumentRef, parsed: ParsedDocument | None) -> str:
    """The text block introducing a locally-assembled document.

    Says three things the model cannot otherwise know: that the text was extracted here rather
    than read by the model, how many figures came with it, and — when parsing failed — that the
    document is unread rather than empty. "I received nothing" and "I received an empty paper"
    invite different and equally wrong inferences, so they are never allowed to look alike.
    """
    name = document.path.name
    if parsed is None:
        return (
            f"[unavailable] {name} ({document.title}): this model accepts no document "
            f"attachment, and the file could not be parsed here. Treat it as UNREAD — not as "
            f"empty — and cite it from its text_search metadata rather than from its contents."
        )
    figures = (
        f"Its {len(parsed.figures)} figure(s) are attached as images below, in document order."
        if parsed.figures
        else "No figures could be recovered from it, so any statement about its figures is "
        "unsupported by what you were given."
    )
    truncation = " The text was truncated; a marker names where." if parsed.truncated else ""
    return (
        f"[extracted document] {name} ({document.title}). This model accepts no document "
        f"attachment, so the file's text was extracted locally and is reproduced below. "
        f"{figures}{truncation}\n\n{parsed.text}"
    )


def _figure_items(figure: FigureRef, plan: MediaPlan | None = None) -> list[dict]:
    """Render one figure as Responses-API content items: a caption then the image bytes.

    Loading bytes here is the multimodal boundary — the only place a figure's pixels
    enter a request (elsewhere figures travel as lightweight ``FigureRef``). The caption is
    rendered verbatim: the presentation layer may prefix it with the report-local number
    ("Figure 1 — …") or the catalog handle ("[analyst_worker_0:2] …") so prose lines up
    with the images (V-4).
    """
    caption = f"{figure.caption} (produced by {figure.produced_by})"
    resolved = (plan or MediaPlan()).resolve(figure)
    text = {"type": "input_text", "text": caption_with_note(caption, resolved)}
    if resolved.dropped:
        return [text]
    return [
        text,
        {"type": "input_image", "image_url": f"data:{resolved.mime};base64,{resolved.b64()}"},
    ]


def _to_responses_input(
    messages: Sequence[ConversationMessage], plan: MediaPlan | None = None
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
            # (developers.openai.com/api/docs/guides/reasoning). An item is rejected without the
            # reasoning item that produced it — observed live twice, on a discovery item
            # (2026-08-03, `tool_search_call … without its required 'reasoning' item`) and on a
            # plain call (2026-08-05, `function_call … without its required 'reasoning' item`,
            # which killed run 8c75990e) — so order within the carried list is load-bearing and
            # the parts are ALWAYS carried.
            #
            # They used to be dropped wholesale when a request declared no tools, inherited from
            # the 2026-07-29 discovery-trim where only a *tool-referencing* item was the problem.
            # Applied to the whole list it orphaned the `function_call` items emitted just below,
            # which are unconditional. The trim's real fix is upstream: every caller that replays
            # a tool trajectory declares its tools (`tool_choice="none"` on a terminal compose),
            # so nothing dangles and nothing needs dropping.
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
                    figure_content.extend(_figure_items(figure, plan))
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


def _strict_json_schema_format(
    schema: type[BaseModel],
    formats: frozenset[str] = _STRICT_SCHEMA_FORMATS,
    resolve_refs_in: frozenset[str] = frozenset(),
) -> dict:
    """Build a Responses-API `text.format` json_schema block for a Pydantic model.

    Strict-mode JSON Schema requires that every object set ``additionalProperties: false`` and
    list all properties in ``required``, that ``default`` be absent, and that a string ``format``
    come from a small accepted set. Pydantic's generated schema violates all three (optionals,
    defaults, ``format: path``/``uri``), so we sanitize a copy before sending it.

    ``formats`` is the accepted set for *this vendor*, because it is not shared: see
    :data:`_CONSERVATIVE_SCHEMA_FORMATS`. ``resolve_refs_in`` names the ``$ref`` positions this
    vendor cannot resolve itself, which is measured per vendor AND per position
    (``ResponsesVendor.resolve_refs_in``).
    """
    json_schema = schema.model_json_schema()
    _sanitize_strict(json_schema, formats)
    json_schema = _dereference(json_schema, resolve_refs_in)
    return {
        "type": "json_schema",
        "name": schema.__name__,
        "strict": True,
        "schema": json_schema,
    }


_NULL_STRINGS = frozenset({"null", "none"})
"""Literal strings a model may write where it means JSON ``null``."""


def _accepts_null_spelling(branch: Any, value: str) -> bool:
    """Whether a string branch accepts this exact literal as meaningful content.

    Exact-value constraints matter here: an enum containing ``"null"`` expressly preserves that
    string, while a date-format branch rejects it. Unknown formats and malformed regular
    expressions preserve content because their validation semantics are application-defined.
    """
    if not isinstance(branch, Mapping) or branch.get("type") != "string":
        return False
    if "const" in branch and branch["const"] != value:
        return False
    enum = branch.get("enum")
    if isinstance(enum, list) and value not in enum:
        return False
    if isinstance(branch.get("minLength"), int) and len(value) < branch["minLength"]:
        return False
    if isinstance(branch.get("maxLength"), int) and len(value) > branch["maxLength"]:
        return False
    pattern = branch.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.search(pattern, value) is None:
                return False
        except re.error:
            return True
    if branch.get("format") in _STRICT_SCHEMA_FORMATS:
        return False
    return True


def _resolve(schema: Any, defs: Mapping[str, Any]) -> Any:
    """Follow a ``$ref`` into ``$defs`` so the walk can descend through referenced models."""
    if isinstance(schema, Mapping) and set(schema) == {"$ref"}:
        return defs.get(str(schema["$ref"]).rsplit("/", 1)[-1], schema)
    return schema


def _normalise_null_strings(
    value: Any, schema: Mapping[str, Any], defs: Mapping[str, Any] | None = None
) -> Any:
    """Turn a literal ``"null"`` string into ``None`` where the schema plainly means null.

    **Why this exists.** A vendor answering through a forced tool call writes the arguments
    itself, with no server-side decoder to hold it to the declared types. GLM-5.3 intermittently
    fills an optional field with the *string* ``"null"`` instead of JSON ``null`` — schema-valid
    against a ``string`` branch, and a type error for anything else. Observed live 2026-08-24 on
    ``SearchRequest.since``; the same call produced correct JSON ``null`` for an optional model
    field, so it is intermittent rather than systematic, which is what makes it a batch hazard
    rather than an obvious break.

    **Why it is safe.** The coercion applies only where the field's schema accepts null *and* no
    branch accepts that exact spelling. For ``date | None`` or ``float | None`` the string
    ``"null"`` cannot be a legitimate value, so reading it as null is the only interpretation
    available. For ``str | None`` it is left exactly as written, because there the string is
    meaningful and guessing would destroy real content.

    This normalises spelling, never content: no field is invented, defaulted or dropped.
    """
    if defs is None:
        defs = schema.get("$defs", {}) if isinstance(schema, Mapping) else {}
    schema = _resolve(schema, defs)
    if not isinstance(schema, Mapping):
        return value
    branches = schema.get("anyOf")
    if isinstance(value, str) and isinstance(branches, list):
        resolved = [_resolve(b, defs) for b in branches]
        accepts_null = any(
            isinstance(b, Mapping) and b.get("type") == "null" for b in resolved
        )
        if (
            accepts_null
            and not any(_accepts_null_spelling(b, value) for b in resolved)
            and value.strip().lower() in _NULL_STRINGS
        ):
            return None
    if isinstance(value, dict):
        props = schema.get("properties")
        if isinstance(props, Mapping):
            return {
                key: _normalise_null_strings(item, props[key], defs) if key in props else item
                for key, item in value.items()
            }
        # A union of object branches: the declared shape is whichever branch has properties.
        for branch in branches or []:
            resolved_branch = _resolve(branch, defs)
            if isinstance(resolved_branch, Mapping) and isinstance(
                resolved_branch.get("properties"), Mapping
            ):
                return _normalise_null_strings(value, resolved_branch, defs)
        return value
    if isinstance(value, list):
        items = _resolve(schema.get("items"), defs)
        if isinstance(items, Mapping):
            return [_normalise_null_strings(item, items, defs) for item in value]
    return value


def _surviving_refs(node: Any) -> bool:
    """Whether any ``$ref`` remains after inlining (a recursive back-reference)."""
    if isinstance(node, dict):
        if set(node) == {"$ref"}:
            return True
        return any(_surviving_refs(value) for value in node.values())
    if isinstance(node, list):
        return any(_surviving_refs(item) for item in node)
    return False


def _dereference(schema: dict[str, Any], positions: frozenset[str]) -> dict[str, Any]:
    """Resolve ``$ref`` only in the positions this vendor cannot handle itself.

    ``positions`` holds ``"anyOf"`` and/or ``"items"``. Resolving elsewhere is not a harmless
    extra: on Qwen it turns a 5/5 shape into 0/5.

    ``$defs`` is kept whenever any reference survives — a recursive back-reference always does,
    and so does every position left untouched — and dropped once nothing points into it.
    """
    if not positions:
        return schema
    defs = schema.get("$defs", {})
    root = schema
    if set(schema) - {"$defs"} == {"$ref"}:
        # Pydantic puts `$defs` and `$ref` together at the root of a self-referencing model, so a
        # `$ref` plus `$defs` is the bare-root form. Other siblings are constraints in JSON
        # Schema 2020-12 and stay attached to the reference.
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        if name in defs:
            root = deepcopy(defs[name])
    resolved = _resolve_positions(root, defs, positions)
    if _surviving_refs(resolved) and defs:
        resolved["$defs"] = defs
    elif "$defs" in resolved:
        del resolved["$defs"]
    return resolved


def _resolve_positions(
    node: Any, defs: Mapping[str, Any], positions: frozenset[str], seen: tuple[str, ...] = ()
) -> Any:
    """Walk the schema, expanding a bare ``$ref`` only under a key named in ``positions``.

    A definition already open on the current path is left as a reference, so a recursive schema
    expands one level rather than forever.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$defs":
                out[key] = value
            elif key in positions:
                # `_expand` resolves AND recurses with the path recorded, so the result must not
                # be walked again here: re-walking forgets `seen`, and a recursive definition
                # then expands without bound. (Same bug, twice — see the guard test.)
                out[key] = _expand(value, defs, positions, seen)
            else:
                out[key] = _resolve_positions(value, defs, positions, seen)
        return out
    if isinstance(node, list):
        return [_resolve_positions(item, defs, positions, seen) for item in node]
    return node


def _expand(
    value: Any, defs: Mapping[str, Any], positions: frozenset[str], seen: tuple[str, ...]
) -> Any:
    """Replace a bare ``$ref`` (or each bare ``$ref`` among a list of branches) with its target.

    Only a node whose *only* key is ``$ref`` is replaced: a reference carrying siblings already
    says something a definition would overwrite.
    """

    def one(item: Any) -> Any:
        if isinstance(item, dict) and set(item) == {"$ref"}:
            name = str(item["$ref"]).rsplit("/", 1)[-1]
            if name in defs and name not in seen:
                return _resolve_positions(deepcopy(defs[name]), defs, positions, seen + (name,))
            return item
        # Not a bare reference: keep walking it, since nested positions may still need resolving.
        return _resolve_positions(item, defs, positions, seen)

    if isinstance(value, list):
        return [one(item) for item in value]
    return one(value)


def _sanitize_strict(
    node: object, formats: frozenset[str] = _STRICT_SCHEMA_FORMATS
) -> None:
    """Recursively make a Pydantic JSON schema strict-mode compatible (in place).

    ``properties`` keys are field *names* (which may be ``default``/``format``),
    not schema keywords, so we recurse into the field subschemas without touching
    the field names.
    """
    if isinstance(node, dict):
        fmt = node.get("format")
        if isinstance(fmt, str) and fmt not in formats:
            node.pop("format")
        node.pop("default", None)
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for subschema in value.values():
                    _sanitize_strict(subschema, formats)
            else:
                _sanitize_strict(value, formats)
    elif isinstance(node, list):
        for item in node:
            _sanitize_strict(item, formats)


def _has_unresolved_server_tool(raw: Any, vendor: "ResponsesVendor") -> bool:
    """Does this turn hold a provider-executed call with no result? (A1)

    Two arms, because this dialect has two hosted-tool shapes:

    * **Separate result item** — only ``tool_search_call``/``tool_search_output``, and only in
      *client* execution mode. **Paired by count and order, never by id**: hosted mode sets
      ``call_id`` to ``null`` on *both* items and ``_as_input_item`` drops ``None`` values, so an
      id-matching predicate finds no pairs and would read ``True`` on every real discovery turn —
      measured across 27 distinct pairs in the production dumps, all of them hosted.
    * **Result folded into the call item** — everything else in ``_INLINE_SERVER_TOOL_ITEMS``, plus
      Alibaba's seven documented types. There is no sibling to look for, so "unresolved" can only
      mean a non-terminal ``status``.

    Reads ``raw.output`` rather than ``provider_parts``: the raw items still carry ``call_id`` and
    every field the vendor sent, and the property is about the turn we *received* rather than
    about the projection we chose to keep.

    Returns ``False`` for a vendor that declares no hosted types — which is DeepSeek (built-ins
    documented *Ignored*) and Z.AI (nothing published for this endpoint at all).
    """
    if not vendor.server_tool_item_types:
        return False
    output = getattr(raw, "output", None) or []
    pending = 0
    for item in output:
        itype = getattr(item, "type", None)
        if itype not in vendor.server_tool_item_types:
            continue
        status = getattr(item, "status", None)
        if status not in _TERMINAL_ITEM_STATUSES:
            return True
        if itype in _PAIRED_SERVER_TOOL_ITEMS:
            pending += 1
    for item in output:
        if getattr(item, "type", None) in _PAIRED_SERVER_TOOL_ITEMS.values():
            pending -= 1
    return pending > 0


def _response(
    *, content="", parsed=None, tool_calls=None, provider_parts=None, raw=None, vendor=None
) -> ModelResponse:
    return ModelResponse(
        content=content or "",
        parsed=parsed,
        tool_calls=tool_calls or [],
        usage=_responses_usage(getattr(raw, "usage", None)),
        stop=stop_from_openai_responses(raw),
        has_unresolved_server_tool=(
            _has_unresolved_server_tool(raw, vendor) if vendor is not None else False
        ),
        provider_parts=provider_parts or [],
        raw=raw,
    )


def _responses_usage(usage: Any) -> ModelUsage:
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
