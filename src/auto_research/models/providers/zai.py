"""Z.AI (GLM) provider adapter (OpenAI Responses dialect).

Z.AI publishes a Responses endpoint but documents nothing about which parameters it honours: its
Codex page states only that "Codex requires a dedicated OpenAI Responses protocol endpoint"
(docs.z.ai/devpack/tool/codex). What it does document, on Chat Completions, is JSON *mode* alone,
``response_format`` converging on ``text`` and ``json_object`` with no ``json_schema`` member, and
its structured-output guide recommends putting the schema in the system message and validating
client-side (docs.z.ai/guides/capabilities/struct-output).

**So the capability flags here are measured, not read.** Live-probed 2026-08-24: a plain call, a
function tool with ``tool_choice="auto"``, and the tools-plus-schema-plus-``tool_choice="none"``
shape all returned HTTP 200 and behaved, but ``text.format`` with ``strict: true`` was **accepted
and ignored** twice over: once answering with prose containing no JSON at all, and once with a
fenced object whose keys the schema never declared. That is worse than a rejection, because the
call succeeds. Hence ``supports_strict_schema=False``, which makes a schema-emitting role fail
with a specific reason instead of returning invented keys into a Pydantic validator.

The vendor's own workaround does work: with the schema in the prompt and no ``text.format``, a
realistic nested verdict schema came back with exactly the declared keys. That is a different
extraction mechanism from every other member, so wiring it is a deliberate decision rather than
an implementation detail, and it is not wired here.

One operational note carried from the coder sweep: this endpoint throttles hard. Probing it three
times in a minute drew ``rate_limit_exceeded`` on every call, matching the 87 to 97 per cent
author-turn throttling recorded at concurrency 3 and 4.
"""

from auto_research.models.providers._media import MediaLimits
from auto_research.models.providers._responses import ResponsesProvider, ResponsesVendor


# Z.AI documents no media limits for this endpoint, so almost nothing is enforced: an undocumented
# limit stays unenforced until a doc or a live 400 says otherwise, which is the same rule the other
# adapters follow. The mime set is the one its vision guides name; every volume field is left at
# "undocumented" rather than borrowed from another vendor.
_LIMITS = MediaLimits(
    image_mimes=frozenset({"image/jpeg", "image/png", "image/webp"}),
)

ZAI_VENDOR = ResponsesVendor(
    name="zai",
    limits=_LIMITS,
    # The Responses host the vendor documents for this dialect (its Chat Completions API lives
    # at a different path, `/api/paas/v4`, which this dialect does not use).
    base_url="https://api.z.ai/api/v1",
    key_env="ZAI_API_KEY",
    # Undocumented, so not sent.
    supports_prompt_cache_key=False,
    # Measured: accepted and ignored. See the module docstring.
    supports_strict_schema=False,
    # A structured answer is reachable, just not through `text.format`: the schema rides as a
    # forced tool call instead. Its Responses endpoint honours a forced tool call: live-verified 2026-08-24 with the
    # real `ToolReviewVerdict`, which validated on both dialects it publishes. That is the path its
    # Claude Code and Codex integrations already depend on.
    schema_via_tool=True,
    # **Z.AI ignores a NAMED `tool_choice`.** Its own function-calling guide documents
    # `tool_choice` as accepting `auto`, `required`, or a named tool
    # (docs.z.ai/guides/capabilities/function-calling), so this is the same accept-and-ignore
    # pattern already recorded above for `text.format` — now confirmed at a second parameter.
    # Measured 2026-08-27, 5 draws of 5 against api.z.ai: pinned to `emit_structured_response` it
    # called `read_file` every time and never the pinned tool.
    #
    # The community reports the same class with a mechanism: GLM is natively an XML tool-caller,
    # and compelling a named JSON tool breaks the parse (vllm-project/vllm#48095, a GLM-5.2 forced
    # tool call left unparsed in `content`; huggingface zai-org/GLM-5.1-FP8 discussion #1). Those
    # are self-hosted runtimes rather than this endpoint, but the model behaviour is the vendor's
    # own and the live probe above confirms it reaches the hosted API too.
    #
    # **`required` is not a safer middle ground:** sgl-project/sglang#13514 reports
    # `tool_choice: "required"` causing unbounded generation on GLM-4.x. So the emit call cannot be
    # compelled here at all, and this vendor takes the same `auto` + terminal-instruction route as
    # Qwen.
    #
    # Cost of having asserted otherwise: in the eight-reviewer gate_bench swap, 14 of 116 reviews
    # (12 per cent) died because the adapter treated "no emit call" as a guarantee violation and
    # raised a deliberately non-retryable error, when the model was simply asking to read a file
    # first. Those 14 changesets are missing from that leg's denominators.
    forces_tool_choice=False,
    # The full set: the narrow one is DeepSeek's, imposed by *its* strict-mode validator,
    # and this vendor never goes through that path. Measured 2026-08-24: `format: date`
    # is accepted in tool parameters here, so narrowing it would strip a hint for no
    # reason -- enforce what this vendor refuses, not what another one does.
    # No entry: measured 5/5 in all four conditions, so this vendor resolves every `$ref`
    # position itself and needs no help. `resolve_refs_in` stays empty by default.
    # No server-side tool-search tool is documented, so every tool is declared resident.
    supports_tool_search=False,
)


class ZaiProvider(ResponsesProvider):
    """Z.AI GLM over the Responses dialect (behaviour in :class:`ResponsesProvider`)."""

    vendor = ZAI_VENDOR
