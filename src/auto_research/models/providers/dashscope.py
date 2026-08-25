"""Alibaba Model Studio (Qwen) provider adapter (OpenAI Responses dialect).

**NOT LIVE VERIFIED, and the reason is the account rather than the wiring.** Probed 2026-08-24:
the configured key authenticates against the international host and ``GET /models`` returns 200
with a 162-model list, but every inference call is refused with HTTP 400
``AccessDenied.Unpurchased`` ("Access to app denied") on the Responses path and HTTP 403 with the
same code on Chat Completions. That held for a current model id (``qwen3.8-max``) as well as an
old one, so it is not a stale-name error: Model Studio gates pay-as-you-go on a complete billing
profile. This adapter is therefore configured from documentation alone and warns at launch, the
same treatment the Qwen coder pairing already carries (``sandbox/coder/harness.py``).

Documented parameters (alibabacloud.com/help/en/model-studio/compatibility-with-openai-responses-api):
``reasoning`` with effort ``none`` through ``high`` is supported. Structured output is documented
only as ``response_format`` with ``{"type": "json_object"}`` plus the word "JSON" in a message
(.../qwen-structured-output), with no ``json_schema`` member, so strict schema is off here for the
same reason it is off for Z.AI. Function tools are not documented for this endpoint either; only
the built-in web search, code interpreter and web extractor are.

**The endpoint recorded in the coder harness table disagrees with this documentation and is
likely wrong.** That table names ``https://dashscope-intl.aliyuncs.com/compatible-mode/v1``, which
appears nowhere in the Responses page's host list. The documented hosts are workspace-scoped,
``https://{WorkspaceId}.<region>.maas.aliyuncs.com/compatible-mode/v1``, plus one bare
``https://dashscope-us.aliyuncs.com/compatible-mode/v1``. The configured key's ``sk-ws-`` prefix
is consistent with a workspace credential, and the bare US host answered ``InvalidApiKey`` for it
while the international host recognised it. Since no host can be confirmed while the account is
unentitled, the host that at least authenticates is the one named here, and the discrepancy is
recorded rather than silently resolved.
"""

from auto_research.models.providers._media import MediaLimits
from auto_research.models.providers._responses import ResponsesProvider, ResponsesVendor


# No media limits are documented for this endpoint, and none can be measured while the account is
# unentitled, so nothing is enforced beyond keeping non-image mimes out of the image channel.
_LIMITS = MediaLimits(
    image_mimes=frozenset({"image/jpeg", "image/png", "image/webp"}),
)

DASHSCOPE_VENDOR = ResponsesVendor(
    name="dashscope",
    limits=_LIMITS,
    # The host that recognises the configured key. See the module docstring for why this
    # disagrees with the vendor's own Responses host list.
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    key_env="DASHSCOPE_API_KEY",
    supports_prompt_cache_key=False,
    # Documented as json_object mode only, with no json_schema member.
    supports_strict_schema=False,
    # A structured answer is reachable, just not through `text.format`: the schema rides as a
    # forced tool call instead. Documented as supporting the `tools` parameter, and the same forced-call route Z.AI is
    # verified on. UNVERIFIED here: the account cannot make any inference call (see the module
    # docstring), so this is configuration, not measurement.
    schema_via_tool=True,
    # It enforces the same rule Anthropic documents for thinking models: a named or `required`
    # tool_choice is refused ("does not support being set to required or object in thinking
    # mode", measured 2026-08-24). We want the thinking, so the emit tool is offered rather than
    # forced, and a declined call raises.
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
)


class DashscopeProvider(ResponsesProvider):
    """Qwen over the Responses dialect (behaviour in :class:`ResponsesProvider`)."""

    vendor = DASHSCOPE_VENDOR
