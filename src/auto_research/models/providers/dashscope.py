"""Alibaba Model Studio Qwen adapter over the OpenAI Responses dialect.

Live probes on 2026-08-24 established structured answers through a function-tool call. Alibaba's
Responses documentation includes custom function tools and ``allowed_tools`` tool choice. It
recommends workspace-specific regional endpoints while retaining the legacy DashScope endpoints.

Alibaba's current model page lists ``qwen3.8-max`` with image, text, and video input and native
visual understanding, so figures remain enabled. Media-volume limits stay unset until the vendor
publishes limits for this exact model/API combination.
"""

from auto_research.models.providers._media import MediaLimits
from auto_research.models.providers._responses import ResponsesProvider, ResponsesVendor


# The current qwen3.8-max model page lists image input. This descriptor enforces only published
# Responses limits; none specific to this model/API combination are currently listed.
_LIMITS = MediaLimits(
    image_mimes=frozenset({"image/jpeg", "image/png", "image/webp"}),
)

DASHSCOPE_VENDOR = ResponsesVendor(
    name="dashscope",
    limits=_LIMITS,
    # Legacy international endpoint. Alibaba recommends workspace-specific regional URLs for
    # new deployments and documents the legacy endpoints as functional.
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    key_env="DASHSCOPE_API_KEY",
    supports_prompt_cache_key=False,
    # Enable Responses session caching on every request; repeated prefixes then receive the
    # vendor's latency and cost benefit without relying on every caller to opt in separately.
    cache_headers=(("x-dashscope-session-cache", "enable"),),
    # Documented as json_object mode only, with no json_schema member.
    supports_strict_schema=False,
    # A structured answer is reachable through a function-tool call rather than `text.format`.
    schema_via_tool=True,
    # Thinking mode accepts automatic tool choice. A declined emit call raises loudly.
    forces_tool_choice=False,
    # The full set: the narrow one is DeepSeek's, imposed by *its* strict-mode validator,
    # and this vendor never goes through that path. Measured 2026-08-24: `format: date`
    # is accepted in tool parameters here, so narrowing it would strip a hint for no
    # reason -- enforce what this vendor refuses, not what another one does.
    # Opposite needs per position, which is why this is not a boolean: `anyOf: [{$ref}, null]`
    # works 5/5 as sent and breaks 0/5 when resolved (it answers with a JSON-encoded string in the
    # object field), while `items: {$ref}` is the reverse, 0/5 as sent and 5/5 resolved.
    resolve_refs_in=frozenset({"items"}),
    supports_tool_search=False,
    # The one vendor here with REAL provider-executed types. .../model-studio/
    # qwen-api-via-openai-responses (read 2026-09-11) enumerates the emitted `output[].type` set as
    # message / reasoning / function_call plus these seven, each triggered by declaring its tool
    # caller-side. Every one folds its result INTO the call item (`action.sources`, `output`,
    # `outputs[].logs`, `queries`), so an unresolved one can only show as a non-terminal `status`
    # (`in_progress` / `searching` are documented values). Unreachable today — the adapter declares
    # only function tools — and declared anyway so the census test has a documented set to hold us
    # to. Three of these are absent from openai's own input union, so they would otherwise ride
    # through `_as_input_item` unclassified and read `False` in silence.
    server_tool_item_types=frozenset({
        "web_search_call",
        "web_extractor_call",
        "code_interpreter_call",
        "web_search_image_call",
        "image_search_call",
        "file_search_call",
        "mcp_call",
    }),
    # Read off .../model-studio/qwen-api-via-openai-responses (2026-09-03), which rules the native
    # path out twice over. The `input_file` part takes `file_url` -- "The public URL of the file.
    # Required when `type` is `input_file`" -- and accepts neither inline base64 `file_data` (the
    # shape this adapter builds) nor a `file_id`; and PDF input is "Currently only supported by
    # `qwen3.5-ocr`", which is not a model any role here holds. A public URL is not available to
    # us either: papers live as local files under the runtime directory, and publishing one to
    # obtain a URL is not something a run may do. Alibaba does serve document *understanding*
    # elsewhere -- upload with `purpose=file-extract`, then Qwen-Long or Qwen-Doc-Turbo -- but
    # that is a different endpoint and a different model, not this seat.
    accepts_inline_documents=False,
)


class DashscopeProvider(ResponsesProvider):
    """Qwen over the Responses dialect (behaviour in :class:`ResponsesProvider`)."""

    vendor = DASHSCOPE_VENDOR
