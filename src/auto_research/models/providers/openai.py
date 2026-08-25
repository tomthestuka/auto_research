"""OpenAI provider adapter: the Responses-API wire logic, at OpenAI's own settings.

The dialect this speaks is shared with every other vendor that publishes it, so the mechanics
live in :mod:`auto_research.models.providers._responses` and only OpenAI's *policy* is here.
OpenAI is the reference vendor: it supports every capability the shared layer knows about, so
this descriptor turns nothing off.

The private helpers below are re-exported because they are addressed by name from the tests
that hold the Responses layer's behaviour, and by `_openai_usage` from the cost tests.
"""

from auto_research.models.keys import OPENAI
from auto_research.models.providers._media import MediaLimits
from auto_research.models.providers._responses import (  # noqa: F401 — re-exported for tests
    _INPUT_SHAPE_FALLBACK,
    _STRICT_SCHEMA_FORMATS as _OPENAI_SUPPORTED_FORMATS,
    _UNCARRIED_ITEM_TYPES,
    _as_input_item,
    _figure_items,
    _input_fields,
    _input_shapes,
    _responses_usage as _openai_usage,
    _sanitize_strict,
    _strict_json_schema_format,
    _to_responses_input as _to_openai_input,
    ResponsesProvider,
    ResponsesVendor,
)


# OpenAI's documented media limits (developers.openai.com/api/docs/guides/images-vision and
# .../file-inputs). Enforced per request by `_media.plan_for_call`. OpenAI publishes no
# ">N images ⇒ stricter dimension" rule, so none is invented here — we enforce what is documented.
_LIMITS = MediaLimits(
    # "PNG (.png), JPEG (.jpeg and .jpg), WEBP (.webp), Non-animated GIF (.gif)".
    image_mimes=frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
    doc_mimes=frozenset({"application/pdf", "text/plain"}),
    # "Up to 1500 individual image inputs per request".
    max_images=1500,
    # "each file must be under 50 MB… combined limit across all files in the request is 50 MB".
    max_doc_bytes=50 * 1024 * 1024,
    # "Up to 512 MB total payload size per request".
    max_request_bytes=512 * 1024 * 1024,
)

OPENAI_VENDOR = ResponsesVendor(
    name=OPENAI,
    limits=_LIMITS,
    # No base_url: this is the SDK's own default endpoint.
    key_env="OPENAI_API_KEY",
    # OpenAI defines this dialect, so every capability the shared layer knows is available here.
    supports_prompt_cache_key=True,
    supports_strict_schema=True,
    supports_tool_search=True,
)


class OpenAIProvider(ResponsesProvider):
    """Thin adapter around the OpenAI Responses API (behaviour in :class:`ResponsesProvider`)."""

    vendor = OPENAI_VENDOR
