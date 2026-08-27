"""DeepSeek provider adapter (OpenAI Responses dialect).

DeepSeek publishes a Responses endpoint and documents its compatibility parameter by parameter
(api-docs.deepseek.com/guides/responses_api), so this descriptor is read from that table rather
than inferred. Everything the shared layer needs is supported except the three parameters listed
below, which the vendor marks "Not supported" outright.

**Live-probed 2026-08-24**, because a documentation table states what is *accepted* and not what
is *honoured* (the Z.AI adapter beside this one is exactly that failure). All four request shapes
returned HTTP 200 in the completed probe: a plain call, ``text.format`` with ``strict: true``, a
function tool with ``tool_choice="auto"``, and the terminal-compose shape of tools plus schema plus
``tool_choice="none"``. The strict-schema reply carried exactly the declared keys, bare, with no
fence, so the constraint is real rather than advisory.

The Responses endpoint is text-only. Its API reference lists ``deepseek-v4-flash`` as the only
accepted model and states that image/file inputs are replaced with placeholder text. The adapter
therefore withholds images with a model-visible note; this provider is eligible for text-only chat
roles and the coder harness, rather than figure-reviewing analyst/reviewer roles.
"""

from auto_research.models.providers._media import MediaLimits
from auto_research.models.providers._responses import (
    _CONSERVATIVE_SCHEMA_FORMATS,
    ResponsesProvider,
    ResponsesVendor,
)


# DeepSeek's Responses reference explicitly says image and file inputs are unsupported and image
# parts are replaced with placeholder text. Withhold them here so the model sees an honest note
# instead of silently reasoning over pixels it never received.
_LIMITS = MediaLimits(
    image_mimes=frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"}),
    images_accepted=False,
)

DEEPSEEK_VENDOR = ResponsesVendor(
    name="deepseek",
    limits=_LIMITS,
    # The endpoint the vendor documents for this dialect; the same host the Codex harness uses.
    base_url="https://api.deepseek.com/",
    key_env="DEEPSEEK_API_KEY",
    # "prompt_cache_key / prompt_cache_retention: Not supported". Prefix caching is automatic on
    # this API, so withholding the parameter costs the routing hint and nothing else. Also
    # unsupported and never sent by the shared layer: `context_management`, `service_tier`,
    # `store`, `include`, `truncation`, `previous_response_id`, `conversation`.
    supports_prompt_cache_key=False,
    # "text: Partially supported. `format` fully supported" — and live-probed to actually
    # constrain the output, not merely to be accepted.
    supports_strict_schema=True,
    # Its strict mode accepts fewer `format` values than OpenAI's, and enumerates them itself
    # (and its docs independently list `format` among the unsupported schema features):
    # "unknown variant `date`, expected one of `email`, `hostname`, `ipv4`, `ipv6`, `uuid`"
    # (live, 2026-08-24). The four temporal formats Pydantic emits for date/time fields are
    # therefore stripped; without this, three of the nine real schemas are refused at request
    # time because they carry a date field.
    strict_schema_formats=_CONSERVATIVE_SCHEMA_FORMATS,
    # Every `anyOf` branch must carry a literal `type`, and the check runs BEFORE any reference is
    # followed, so a bare `$ref` branch is refused: "field `anyOf`: missing field `type`". Adding
    # the type moves the error on ("An object with no properties is not allowed"), which is what
    # shows the pointer is never resolved. Pydantic emits that shape for every optional
    # model-valued field, so union branches must be expanded here. `items: {$ref}` is fine.
    #
    # Measured 0/5 vs 5/5 (2026-08-24), and the rule is the vendor's rather than an artifact of
    # one route: the same refusal comes back from the default endpoint, from `/beta`, from
    # `/beta` strict function-calling, and under all three `$defs`/`$def`/`definitions` spellings.
    # The vendor documents `anyOf` and `$ref` as supported and says nothing about the
    # combination, so this is measured, not read.
    resolve_refs_in=frozenset({"anyOf"}),
    # "tools: Partially supported" — "function / web_search supported; other types ignored". The
    # server-side tool-search tool is one of the ignored types, so deferred loading would leave
    # the model unable to resolve a deferred reference. Every tool is therefore declared resident.
    supports_tool_search=False,
)


class DeepSeekProvider(ResponsesProvider):
    """DeepSeek over the Responses dialect (behaviour in :class:`ResponsesProvider`)."""

    vendor = DEEPSEEK_VENDOR
