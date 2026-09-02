# `auto_research.models` — provider abstraction & registry

## Purpose
Resolves an agent role (e.g. `reviewer`) to a configured group of LLM calls,
using `config/models.yaml`. Hides the per-provider SDK differences behind a
thin common interface so a capability-compatible role group can use any of the six configured
providers by editing YAML alone
([CLAUDE.md §6](../../../CLAUDE.md#6-config-driven-model-registry)).

## Architecture
- `types.py` — provider-neutral Pydantic models, `ModelHandle`, and
  `ModelGroupHandle`.
- `registry.py` — loads and validates `config/models.yaml`; exposes
  `load_registry()` and `resolve(role)`.
- `providers/` — one adapter per vendor. SDK clients are imported only inside `call()` so
  importing the package does not require credentials or make network calls. Anthropic and
  Google each have their own wire logic; the four vendors that speak the **OpenAI Responses
  dialect** (OpenAI, DeepSeek, Z.AI, Alibaba Model Studio) share `_responses.py` and differ
  only by a `ResponsesVendor` descriptor in their own module, beside that vendor's citations.
  The split mirrors `_media.py`: mechanics shared, policy per vendor. The shared client gives
  each HTTP attempt a 300-second ceiling and disables the SDK's two hidden retries; the outer
  `retry.py` policy owns subsequent attempts and records their cooldowns.
  Alibaba's Responses session cache is enabled on every Qwen request through its documented
  `x-dashscope-session-cache: enable` header; live schema tests also mark their stable prefixes.
- `keys.py` — the per-provider API-key pool, its rotation rule (Anthropic and OpenAI), and
  `resolve_key`, the single resolution every actor uses to pick the key it authenticates with.
- `retry.py` — one chokepoint for provider failures, with **two** answers because there are
  two kinds: *wait* for provider capacity (529/5xx/connection), *switch keys* for a quota of
  the key itself (429 rate limit, spent credit balance).
- `ModelHandle.call()` calls into `auto_research.observability` to add optional
  LangSmith tracing and append a per-call cost ledger row.

## Interfaces / Contracts
- `config/models.yaml` must define all required roles: `reviewer`,
  `analyst_lead`, `analyst_worker`, `librarian_io`, and the Phase-2 authoring roles
  `coder` and `analyst_colead`. Each role maps to `members`, a non-empty list of model
  configs. (All roles are currently required; non-required roles return only as the system
  grows.)
- Model identifiers and per-role parameters live in YAML, not Python source.
- `ModelHandle.call()` accepts text messages and optional `FigureRef` images.
  If images are passed to a role member whose config has `vision: false`, it
  raises `ValueError`.
- `ModelHandle.call()` also accepts optional `DocumentRef` attachments. If
  documents are passed to a role member whose config has `documents: false`, it
  raises `ValueError`. The Anthropic adapter places document blocks **at the front**
  (a stable position right after `system`, before the conversation) with a
  `cache_control` breakpoint on the last document, so documents that accumulate
  append-only across a ReAct loop are cache-written once and cache-read on later
  turns rather than re-billed each turn.
- **Figures carry pixels at the multimodal boundary, two ways.** A `FigureRef`'s
  bytes are loaded and base64'd into a native image block only inside the adapter —
  never elsewhere (figures otherwise travel as lightweight references). (1) **Cross-agent
  figures** (a peer's plot, a worker's figures the reviewer must see) have no tool-turn
  in this conversation, so they ride the call-time `images=` channel, appended at the end.
  (2) **A tool's own figures** (`ToolResultPart.figures`, set by the ReAct loop) render
  **inline at their tool-result turn** — Anthropic in the same user turn as the
  `tool_result`, OpenAI/Google as a user turn immediately after — so they replay in place
  every round and fall inside the cached prefix as the loop grows (the model keeps *seeing*
  its plots, and the terminal report call that replays the trajectory does too).
- **Provider-native context editing (Anthropic, beta — the context-pressure valve for long
  loops).** On the `cache_prefix` paths (the ReAct tool loop and the terminal report call),
  when `context_mgmt_enabled` (`orchestration.yaml`, read via `env.context_editing_config`),
  the Anthropic adapter routes through `client.beta.messages` and attaches a
  `context_management` block (`clear_tool_uses_20250919`): once a request crosses
  `context_mgmt_trigger_tokens` input tokens, Anthropic clears the *oldest tool results*
  server-side (keeping the last `context_mgmt_keep_tool_uses`, clearing at least
  `context_mgmt_clear_at_least_tokens` per pass so the unavoidable cache-prefix invalidation is
  amortized). The applied-edits report is normalized onto `ModelResponse.context_edits` and
  surfaced by `AgentRole` as a `<role>.<action>.context_edit` monitor event. Every other call
  (no `cache_prefix`, or disabled) stays on the non-beta namespace, byte-identical to before —
  minimal blast radius. Anthropic-only (OpenAI's `responses.compact` needs stateful Responses;
  Google has no equivalent). This is a *context-window pressure valve, not a cost lever*: with
  prompt caching raw history is cheap and any clear breaks the cached prefix. **Live-verified
  2026-07-28** (`tests/test_live_context_editing.py`): the beta path cleared tool results and
  reported them on `context_edits`, and structured output still parses on the beta namespace.
  Ships enabled (`context_mgmt_enabled: true`).
- **Provider-native tool search / deferred tool loading** (`tool_search_enabled` in
  `orchestration.yaml`, read via `env.tool_search_config`). A tool arriving with
  `defer_loading: true` (every `research_toolkit` tool — see `sandbox/DOCS.md`) is withheld from
  the model's context; the adapter declares the provider's server-side search tool (Anthropic
  `tool_search_tool_bm25_20251119`, OpenAI `tool_search` on gpt-5.4+; Google has no equivalent
  and keeps every tool resident). On discovery the definition is appended inline in the
  conversation **tail**, so the cached prefix is untouched and the tool stays callable for the
  rest of the conversation without re-searching. Three consequences the adapters implement:
  the Anthropic `cache_control` breakpoint moves to the last **non-deferred** tool (a deferred
  tool carrying it is a 400); discovery blocks ride on `ModelResponse.provider_parts` and are
  replayed verbatim, since the API re-expands referenced definitions from history each turn; and
  a reference resolves only against a **declared** tool, so every request that replays a
  trajectory declares its tools — the terminal compose with `tool_choice="none"`. Providers
  differ in what stays resident: Anthropic hides a deferred tool entirely, OpenAI
  keeps its name and description and withholds only the parameter schema. The Anthropic search
  tool is declared as the dated `type` beside its **undated** `name`
  (`anthropic_search_tool_name` derives one from the other; a mismatch is a 400). **Live-verified
  2026-07-28 on both providers** (`tests/test_live_tool_search.py`): resident input 19,074 →
  3,789 tokens (OpenAI) and 35,208 → 9,003 (Anthropic) on the real manifest, the model searched
  and called the discovered tool, and a measured three-turn probe confirmed the expanded
  definition falls **inside** the conversation cache breakpoint (cache-read at 0.1× thereafter) —
  the one behaviour no provider documents. Ships enabled (`tool_search_enabled: true`).
- **Multimodal request guards ([V-5], `providers/_media.py`).** Every request that carries figures
  or documents passes through one media plan, computed once per call, over the **whole** request:
  the figures replayed inline through the conversation, this call's `images=`, and its `documents=`.
  Being stateless is what makes that possible — the adapter holds everything before it sends, and
  provider limits are defined over exactly that set. Each adapter owns its own `_LIMITS`
  (`MediaLimits`), every field quoting the provider doc it comes from, because the limits differ
  enough that a shared cap would be wrong for all three:

  | | Anthropic | OpenAI | Google |
  |---|---|---|---|
  | image mimes | jpeg/png/gif/webp | png/jpeg/webp/gif | png/jpeg/webp/heic/heif |
  | max images | 100 | 1500 | 3600 |
  | >20 blocks ⇒ stricter edge | **2000 px** | — | — |
  | max edge | 8000 px | — | — |
  | max bytes/image | 10 MB | — | — |
  | max request | 32 MB | 512 MB | **20 MB inline** |
  | documents | PDF ≤100 pages | ≤50 MB | within 20 MB |

  Checks apply in order — mime, count, pages, per-item bytes, dimensions, then the whole-request
  payload budget, which evicts **oldest-first** (the current turn's evidence is what the model is
  reasoning about). An image that breaches a dimension or byte limit is **resized**; a document,
  which cannot be, is **withheld**. Either way the block is replaced by a model-visible note naming
  the file and the reason — never a silent absence, which is what invites invention. A resize
  changes only the transmitted copy: the artifact and its `FigureRef.sha256` are untouched, so the
  provenance chain still points at the file a human reproduces. **Nothing is downscaled unless a
  limit would otherwise be breached** — providers already resize server-side to their processing
  tier and bill the smaller version, so client-side downscaling saves no tokens and would cost real
  fidelity on a hi-res-tier model.
- `ModelRegistry.resolve(role)` returns a `ModelGroupHandle`; callers choose a
  member with `member(name)` or iterate through `handles`.
- **Native structured output and function calling.** `call(..., schema=Model)`
  requests provider-native **structured output** (OpenAI Responses `text.format`
  json_schema, Google `response_schema`, Anthropic `messages.parse(output_format=)`);
  the parsed object is returned as a dict in `ModelResponse.parsed`.
  **Send side — schema:** Anthropic uses the SDK-native `messages.parse`, but OpenAI
  and Google hand-build their strict schema (`_strict_json_schema_format`,
  `_to_gemini_schema`) rather than using the SDK's own `parse` conversion, because
  that conversion keeps `format: path` and `default` values which OpenAI/Gemini strict
  mode reject (verified: all five structured schemas would 400). **Receive side —
  parse:** OpenAI and Google deserialize with `providers/_json.loads_first_json_object`,
  which takes the *first complete JSON value* and tolerates any leading/trailing content
  a reasoning model may append (a bare `json.loads` on that surplus crashed a live run,
  2026-07-20); it returns `None` when no JSON is present so the structured-retry loop
  re-prompts instead of raising.
  `call(..., tools=[...], tool_choice=...)`
  requests provider-native **function calling**; the model's tool calls are
  returned in `ModelResponse.tool_calls`. `schema` and `tools` **may be declared
  together** — live-verified on all three providers 2026-08-04 and sent together through these
  adapters since 2026-08-06; the model then either calls a tool or emits the structured answer.
  `tool_choice` is `"auto"` / `"required"` / `"none"`, where **`"none"` keeps the declarations
  and forbids a call** — the terminal-compose shape, so a replayed trajectory's items still
  resolve against declared tools. Each tool is
  a provider-neutral dict `{"name", "description", "input_schema"}`. There is no
  prompt-injected JSON schema; agents validate the parsed dict with Pydantic
  (`_structured_loop`) and retry on an off-schema shape, failing loudly on cap exhaustion.
- **`ModelResponse` is the neutral assistant turn, in both directions** — the analogue of
  Anthropic `Message.content`, OpenAI `response.output` and Google `Content.parts`. An adapter
  returns it and `_react` appends it straight back, so the turn replayed is the turn received
  rather than a re-typed copy (both 2026-08-03 run-killers were fields lost in such a copy).
  `content`/`tool_calls`/`parsed` are the interpreted views the harness acts on; everything else
  the provider authored rides verbatim on `provider_parts`, an **open** set — the installed
  `anthropic` `ContentBlock` union already lags its own docs, so an allowlist would be stale on
  arrival. The neutral conversation therefore splits by **authorship**: `ModelMessage` and
  `ToolResultMessage` are ours and we own their shape; `ModelResponse` is the provider's and we
  carry it whole. **Carry on ingest; the adapter decides what is legal to send** — see the
  per-provider replay rules below.
- `priceMTk` is optional per model member and stores USD rates per million
  tokens with `input` and `output` keys. When absent, observability still
  records token counts with zero calculated dollar cost.

### API-key fallback (rate limits and spent balances)
A second key is optional; set `ANTHROPIC_API_KEY_FALLBACK` beside `ANTHROPIC_API_KEY` and
a key-scoped failure switches to it **immediately** instead of cooling down. The design
turns on one distinction:

| Failure | Whose quota | Answer |
|---|---|---|
| 429 `rate_limit_error` | the key's (per **organization**) | rotate — a different org's key is not rate-limited |
| 403 `billing_error`, or a 400 naming the credit balance | the key's | rotate — waiting never refills a spent balance; before this it **killed the run** |
| 529 `overloaded_error`, 5xx, connection drops | Anthropic's capacity | cool down (unchanged) — every key sees the same outage |
| 401 `authentication_error` | — | neither; a revoked key is a configuration fault a human must see |

- Rotation costs **no cooldown and no retry budget** — there is nothing to wait for — and
  offers each key at most once per call; once exhausted the same exception falls through to
  the existing cooldown/raise path, so a 429 on every key still waits and a balance spent on
  every key still raises honestly.
- The live key is **process state**, so rotation is bidirectional *across* calls: a
  per-minute bucket refills on one key while the other is in use and neither is stranded.
- **A spent balance retires that key for the process; a rate limit never does**
  (`is_exhausted_key_error` splits the two, `KeyPool.retire_active` applies it). The two
  quotas differ in whether they refill, and bidirectional rotation is only safe for the one
  that does: without retirement, a run whose primary is out of credit rotates *back* onto it
  the first time the fallback hits a routine 429, and since a balance error is non-transient
  that dead key's 400 becomes the exception that raises — a one-minute wait turned into a
  dead run, strictly worse than having no fallback at all. Retirement is process-scoped, so a
  topped-up key is live again on the next run. Pinned by
  `test_a_dead_primary_does_not_turn_a_fallback_rate_limit_into_a_fatal_error`.
- `AsyncAnthropic(api_key=active_key(ANTHROPIC))` and `AsyncOpenAI(api_key=active_key(OPENAI))`
  — read explicitly, because each SDK resolves its `*_API_KEY` once per client and the live key
  may no longer be that value.
- The coder is a **separate process** (Claude Agent SDK), so `merge_flow._with_live_anthropic_key`
  stamps the live key onto its environment — and only after a rotation, leaving the common
  path byte-identical. A session already in flight keeps the key it launched with.
- Each switch records one `provider.key_rotation` monitor event (level 3) naming the slot
  (`primary`/`fallback1`), never the key value.
- **The fallback must be a different Anthropic organization to relieve a 429** — rate limits
  are per-organization, so a same-org second key rotates onto the same exhausted bucket.
  `check_api_key_fallbacks` (preflight) cannot see the org; it rejects only what it can
  prove useless: a blank fallback, one identical to the primary, or one set without a primary.

## Dependencies
- External: `anthropic`, `openai`, `google-genai`, `pyyaml`, `pydantic`, `httpx`
  (error classification in `retry.py`).
- Internal: `auto_research.env` (`provider_retry_policy` — the transient-retry knobs).
  `keys.py` depends on nothing but the standard library, so the pool is safe to read from
  any layer.
- Imported by: `auto_research.agents`, `auto_research.observability`,
  `auto_research.preflight` (`check_api_key_fallbacks`), `auto_research.sandbox.merge_flow`
  (the coder's environment).

## Dictionary-level reference
- `ProviderName` — literal provider label accepted in `config/models.yaml`: `anthropic`,
  `google`, `openai`, `deepseek`, `zai`, `dashscope`. All six back a chat adapter as of
  2026-08-24.
- `CHAT_PROVIDERS` — the providers with a real adapter; anything outside it resolves to a
  `HarnessOnlyProvider`. Membership means a chat call *reaches* the vendor; role eligibility also
  requires the member's media and structured-output capabilities. Z.AI and Qwen reach schemas
  through function-tool arguments because their native strict-output path is unsuitable here.
  DeepSeek is treated as text-only **by policy, not by vendor limitation** — its only multimodal
  model is experimental (see `limits_for` below). Alibaba's current `qwen3.8-max` model page lists
  image, text, and video input.
- `ResponsesVendor` — one vendor's Responses-API policy: `name`, `limits` (its own documented
  `MediaLimits`), `base_url`, `key_env`, `supports_prompt_cache_key`, `cache_headers`, `supports_strict_schema`,
  `schema_via_tool`, `forces_tool_choice`, `strict_schema_formats`, `resolve_refs_in`,
  `supports_tool_search`, and `vision_models`.
  `cache_headers` are sent on every request; Qwen uses this to opt into Alibaba's session cache
  globally rather than depending on individual role call sites.
  **Reference resolution is per vendor AND per position** (`resolve_refs_in`), which is why it is
  a set of position names rather than a boolean. Measured 2026-08-24 at 5 draws per cell, every
  cell 0/5 or 5/5 with no variance:

  | vendor | `anyOf: [{$ref}, null]` | `items: {$ref}` |
  |---|---|---|
  | DeepSeek | **refused 0/5** | fine 5/5 |
  | Z.AI | fine 5/5 | fine 5/5 |
  | Qwen | fine 5/5 | **refused 0/5** |

  Qwen is what forced the shape: resolving its `anyOf` refs makes a *working* call fail 0/5, while
  its `items` refs must be resolved, so one vendor needs opposite treatment in two positions.
  Z.AI needs nothing at all. Resolving is therefore never a safe default and is applied only where
  a vendor is measured to need it.

  DeepSeek's rule is precise and was probed six ways: every `anyOf` branch must carry a literal
  `type`, checked *before* any reference is followed. The same refusal comes from the default
  endpoint, `/beta`, `/beta` strict function-calling, and all three `$defs`/`$def`/`definitions`
  spellings. Its docs list `anyOf` and `$ref` as supported and say nothing about the combination.

  "A compatibility layer cannot resolve a reference" is **not** a property any of them has; treat
  this as a measured table, never as a general rule.

  **No provider enforces JSON-Schema `pattern`.** Type, `required` and `additionalProperties` are
  enforced; a regex is advisory. Measured on Anthropic 2026-08-24 (it returned a plausible but
  wrong-length value for `FigureRef.sha256`), and DeepSeek's docs list `pattern` among the
  unsupported keywords. Harmless in production, where a `FigureRef` is built by a tool from a real
  file and a model only *cites* figures by index; it matters only where a model is asked to invent
  a whole record. A constrained field that a model must author is therefore validated by us, never
  by the provider.

  **Two routes to a structured answer.** Native decoding via `text.format` where
  `supports_strict_schema` holds (OpenAI, DeepSeek); otherwise `schema_via_tool` declares one
  synthetic tool whose parameters *are* the schema (Z.AI, Qwen). Z.AI pins the choice; Qwen keeps
  automatic choice while thinking and appends a terminal emit instruction. A response containing
  both the emitter and a working-tool request is rejected as premature, allowing the role's
  bounded structured retry to make a clean attempt. The tool route is the mechanism Claude Code's and
  Codex's own editing tools are JSON-Schema functions, so it reaches a vendor whose `text.format`
  is a no-op. The emit call is consumed into `parsed` and never appears in `tool_calls`; the
  loop's own tools stay declared beside it so a replayed trajectory still resolves. A vendor with
  neither route raises rather than returning invented keys. Every flag defaults to the OpenAI behaviour and
  is turned off only with a citation. `limits_for(model)` narrows the limits to what one model
  accepts, so a text-only model withholds images with a note. DeepSeek's Responses endpoint accepts
  `deepseek-v4-flash`, `deepseek-v4-pro` and `deepseek-v4-flash-vision-exp`, replacing image input
  with placeholder text on every model but the last
  (api-docs.deepseek.com/guides/responses_api). That vision model **is** documented, with its own
  limits — 600 images, 8192 px per side dropping to 4096 at 15 or more, 32 MiB per image, 48 MiB
  body (api-docs.deepseek.com/guides/vision) — so `images_accepted=False` for this vendor is a
  **standing policy decision taken 2026-08-25**, not an absent capability: the project declines to
  depend on a multimodal path the vendor labels experimental, and revisits when DeepSeek ships a
  non-experimental one. Alibaba's current `qwen3.8-max` page lists native visual understanding and
  image/text/video input.
  Qwen's `cache_headers` enables the vendor's session cache on every request; Alibaba documents
  that cache as disabled by default. Callers still mark stable-prefix paths with `cache_prefix`
  through the common interface so the intent remains explicit across providers.
- `responses_vendors() -> tuple[ResponsesVendor, ...]` — every Responses-dialect descriptor, so
  a check can hold a property across all of them instead of per adapter.
- `HarnessName` — which author harness drives a `coder` member: `claude_sdk` or `codex`. Legal
  `(provider, harness)` pairs live in `sandbox/coder/harness.py`, checked at launch.
- `MessageRole` — one of `"system"`, `"user"`, or `"assistant"`.
- `ModelMessage(role: MessageRole, content: str)` — provider-neutral text
  message. Unknown fields are forbidden.
- `ModelUsage(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
  reasoning_tokens=0)` — normalized token usage in **non-overlapping** classes so cost is a plain
  weighted sum ([I5]). `input_tokens` is full-price *uncached* prompt tokens (Anthropic reports this
  directly; OpenAI/Google adapters subtract the cached subset from their reported total);
  `cache_read_tokens` served from cache (~0.1x); `cache_write_tokens` cache-creation (~1.25x, 0 where
  the provider caches implicitly); `output_tokens` includes reasoning/thinking; `reasoning_tokens` is
  the reasoning subset of output, informational only (already priced within `output_tokens`).
- `ToolCall(id: str = "", name: str, arguments: dict[str, Any] = {})` — one
  provider-native tool/function call extracted from a model response. ``id`` is the
  provider tool-call id, used to correlate a result back to the call when replaying
  a ReAct trajectory.
- `ToolResultPart(tool_call_id: str = "", name: str = "", content: str = "",
  is_error: bool = False, figures: list[FigureRef] = [])` — one tool result fed back
  to the model. Carries both ``tool_call_id`` (Anthropic/OpenAI) and ``name`` (Google)
  so every adapter can build its native shape. ``figures`` are the figures the tool
  produced; each adapter renders them as an image turn inline at this tool-result turn
  (so they persist in history and cache), distinct from the call-time ``images=`` channel
  used for cross-agent figures.
- `ToolResultMessage(role="tool", results: list[ToolResultPart])` — the neutral tool-result
  turn (ours). `ConversationMessage = ModelMessage | ModelResponse | ToolResultMessage`;
  `call(messages=…)` accepts the union.
- `ModelResponse(role="assistant", content: str = "", parsed: Any | None = None,
  tool_calls: list[ToolCall] = [], provider_parts: list[dict] = [],
  usage: ModelUsage = ModelUsage(), context_edits: list[dict] = [], raw: Any = None)` — one
  provider-authored assistant turn, used as both the return value and the replayed input turn.
  `parsed` carries native structured output; `tool_calls` native function calls;
  `provider_parts` every part we do not interpret (Anthropic/Google carry the **whole** array —
  see the replay rules below); `context_edits` the provider-native context-management edits
  applied to this call (Anthropic `applied_edits`; empty elsewhere).
- **Per-provider replay rules, from each provider's documentation.** Anthropic: *"echo the
  assistant message exactly as received"*, and *"rebuilding the message or filtering out
  `redacted_thinking` blocks triggers a 400 error"*
  ([thinking-tool-workflows](https://platform.claude.com/docs/en/build-with-claude/thinking-tool-workflows))
  — so its adapter carries the whole content array and hands it back unfiltered and unreordered.
  Google, in stateless mode (ours): *"you MUST always resend all `thought` blocks exactly as they
  were received"* and *"should NOT remove or modify thought blocks"*
  ([thinking#signatures](https://ai.google.dev/gemini-api/docs/thinking#signatures)) — same
  verbatim echo, and a call's `thought_signature` rides on its own part, which is why it also
  lives on `ToolCall.provider_fields` for turns we synthesize. OpenAI documents pass-back as
  *recommended*, not required, plus *"ensure all items between the last user message and your
  function call output are passed into the next response untouched"*
  ([reasoning](https://developers.openai.com/api/docs/guides/reasoning)) — so its adapter carries
  every item except the two it rebuilds from our own state, projected into the accepted input
  shape at capture. **Parts are always carried; no request shape drops them.** The drop rule that
  used to fire when a request declared no tools is gone: it was inherited from the discovery-trim,
  where only a *tool-referencing* item was the problem, and applied to the whole list it orphaned
  the `function_call` items that replay unconditionally (`function_call … without its required
  'reasoning' item`, run-killer 2026-08-05). The dilemma it existed for — a block naming an absent
  tool is rejected, yet filtering those blocks out is what Anthropic's rule forbids — is resolved
  upstream instead: **a request that replays a trajectory declares its tools**, with
  `tool_choice="none"` when it must not call them. Only a turn we *synthesized* (a test fake) has
  no array to echo and is rebuilt from the interpreted views.
- `RoleModelConfig(name: str, provider: ProviderName, model: str,
  vision: bool = False, documents: bool = False, priceMTk: dict[str, float] = {},
  parameters: dict[str, Any] = {}, harness: HarnessName | None = None,
  base_url: str | None = None, key_env: str | None = None)` — one model member from YAML. The
  last three are the **coder-role** fields and are ignored by chat roles: which harness drives
  this model, the endpoint it is reached at when the vendor is not the harness's native one, and
  the environment variable holding that vendor's key.
- `RoleModelGroup(members: list[RoleModelConfig])` — non-empty model group for
  one role.
- `ModelsConfig(roles: dict[str, RoleModelGroup])` — validated
  `config/models.yaml` shape.
- `ModelProvider` — async protocol implemented by provider adapters:
  `call(messages, config, *, schema=None, tools=None, tool_choice="auto",
  images=(), documents=(), cache_prefix=False) -> ModelResponse`. `schema` and `tools` are
  independent — one request builder per adapter sends whichever were given, and one extraction
  pass fills `content`/`parsed`/`tool_calls`/`provider_parts` from whatever came back.
  `tool_choice` maps to `"none"` (OpenAI) / `{"type": "none"}` (Anthropic) /
  `FunctionCallingConfig(mode="NONE")` (Google). `cache_prefix`
  asks the adapter to use the provider's native prompt-cache mechanism: Anthropic
  sets `cache_control: ephemeral` breakpoints on the stable prefix (system + tool defs)
  and the rolling conversation tail; OpenAI sets a `prompt_cache_key` (prefix caching is
  automatic); Google relies on implicit caching (no-op). `messages` may include
  prior `ModelResponse`/`ToolResultMessage` turns, which each adapter translates to native
  tool_use/tool_result blocks.
- `ModelHandle(role: str, config: RoleModelConfig, provider: ModelProvider)` —
  resolved callable handle for one role member. Method:
  `async call(messages, *, schema=None, tools=None, tool_choice="auto",
  images=(), documents=()) -> ModelResponse`. `schema` and `tools` may be combined.
  Every call is wrapped in
  `retry.call_with_transient_retry`, so transient provider failures are waited out
  here — the single choke point all roles share — before the exception can reach
  the graph.
- `retry.is_transient_provider_error(exc) -> bool` — True for provider errors worth
  waiting out: HTTP 408/409/429/5xx (read from the SDK error's `status_code`,
  Anthropic/OpenAI, or `code`, Google genai) and connection/timeout drops
  (`httpx.TransportError`, the SDKs' `APIConnectionError`). Auth/bad-request/other
  exceptions are not transient.
- `retry.is_key_scoped_provider_error(exc, provider) -> bool` — True when the failure is a
  quota of the API **key**, not of the provider, judged in **that provider's own vocabulary**
  (`retry._PROVIDER_KEY_ERROR_RULES`, each entry citing the provider's error documentation).
  Anthropic: HTTP 429, a `type` of `rate_limit_error` or `billing_error`, or a message naming
  the credit balance. OpenAI: any 429, which is how it reports every key quota it has. False
  for 529/5xx and connection drops (provider capacity — identical on every key), deliberately
  for 401 `authentication_error`, and for any provider with no cited rules. `provider` is
  required with no default: defaulting it would apply one provider's mapping to another,
  which is the specific error the table exists to prevent.
- `retry.call_with_transient_retry(provider_call, *, describe, policy=None,
  sleep=asyncio.sleep, rotate_key=None, on_key_rotation=None) -> Any` — awaits
  `provider_call()`, retrying transient failures up to `policy.max_retries` with cooldowns
  growing exponentially from `base_cooldown_s` to `max_cooldown_s`, jittered to 50–100% so
  concurrent agents desynchronize. Policy defaults from `env.provider_retry_policy()`
  (`config/orchestration.yaml: provider_retry_*`, "Internet stability" section).
  Each wait logs a WARNING carrying `describe`; a non-transient error or an
  exhausted budget re-raises the original exception unchanged. `policy`/`sleep`
  are injectable for deterministic tests.
  `rotate_key(retire_current)` is consulted **first** and only for `is_key_scoped_provider_error`
  failures: it switches to a key this call has not used and returns its label (or `None`
  when exhausted), and the retry that follows spends no cooldown and no retry budget.
  `on_key_rotation(label, exc)` is awaited for the ledger event and never affects control
  flow (a raising hook is swallowed).
- `retry.is_exhausted_key_error(exc, provider) -> bool` — the non-refilling subset of the
  key-scoped class, again per provider. Anthropic: `billing_error`, or a message naming the
  credit balance; its 429 always refills. OpenAI: a `type` of `insufficient_quota` or a `code`
  of `credit_balance_exhausted` / `organization_spend_limit_exceeded` /
  `project_spend_limit_exceeded` / `organization_usage_limit_exceeded` — **all of which arrive
  as 429s**, so "a 429 refills" is true for Anthropic and false for OpenAI. A failure in this
  class also **raises rather than cooling down** once rotation is exhausted: a quota that does
  not refill cannot be waited out, and OpenAI's reads as transient by status alone.
- `keys.KeyPool(provider)` — the keys configured for one provider and which is live.
  Reads `<PROVIDER>_API_KEY` then `<PROVIDER>_API_KEY_FALLBACK`, dropping a fallback that is
  blank or identical to the primary. `.active()` (the key, or `None` so the SDK keeps its own
  resolution rather than being handed an empty string), `.active_label()` (`primary` /
  `fallback1` / `unset` — log-safe, never the value), `.has_fallback()`,
  `.rotate(exclude=…) -> str | None` (skips retired keys), `.retire_active() -> str`
  (takes the live key out of rotation for good — a spent balance only).
- `keys.key_pool(provider)` / `keys.active_key(provider)` — the process-wide pool, built
  lazily so it reads the environment *after* `.env` is loaded. `keys.reset_pools()` drops the
  cache (tests, and the preflight check).
- `keys.make_rotator(provider) -> Callable[[bool], str | None] | None` — a **per-call** rotation
  callback, or `None` when no fallback is configured *or* the provider is outside
  `ROTATING_PROVIDERS`. Offers each key once per call (the set of tried labels is per-call
  state; the live key is process state). Its `retire_current` argument retires the key it is
  leaving, which the caller sets from `retry.is_exhausted_key_error`.
- `keys.ROTATING_PROVIDERS: tuple[str, ...]` — the providers whose adapter actually sends
  `active_key` (Anthropic and OpenAI). Rotating any other would be **worse than doing nothing**:
  the client would keep the key the SDK resolved from the environment while the caller skipped
  its cooldown — a failover that looks like it works and silently does not. Wiring a provider
  takes **three** things, not one: a cited error taxonomy in `retry`, the adapter line, and its
  name here; `check_api_key_fallbacks` reads the same tuple, so a fallback is validated exactly
  where it can be honoured. **Google is absent deliberately**: its troubleshooting guide and its
  error reference disagree on the vocabulary (`RESOURCE_EXHAUSTED` against `quota_exceeded` /
  `rate_limit_exceeded`), neither publishes the response body, and its SDK's `APIError.code` is
  the HTTP status **integer** where OpenAI's `.code` is a **string** error code — so a shared
  field read would misclassify silently. No rule is guessed for it.
- `keys.env_var_names(provider) -> (primary, fallback)` — the env-var pair for a provider.
- `keys.check_api_key_fallbacks()` — preflight check. Passes when no fallback is set (it is
  optional); raises when one is set but blank, identical to the primary, or set without a
  primary — each a fallback that `KeyPool` silently drops, which would leave a human
  believing a run is protected against throttling when rotation has nowhere to go.
- `PROMPT_DUMP_ENV` (`"AUTO_RESEARCH_PROMPT_DUMP"`) / `_dump_prompt(...)` — verbatim prompt
  dump for leak audits: when the variable names a file, every `ModelHandle.call` appends one
  JSON record (role, member, provider/model, the whole normalized conversation, tool
  definitions, `tool_choice`, schema name, attachment counts) after the request is assembled
  and before dispatch — one record per logical call, not per retry. Unset, the cost is one
  `os.environ.get` and the request is byte-identical; dump failures are swallowed. Known
  narrowness (recorded 2026-08-28): the schema is dumped by name only and attachments by
  count, so field descriptions and figure captions are not in the record; the coder author is
  a harness subprocess and is not covered by this chokepoint.
- `ModelGroupHandle(role: str, handles: tuple[ModelHandle, ...])` — resolved
  model group. Method: `member(name: str) -> ModelHandle`.
- `REQUIRED_ROLES` — required role names that must appear in config: `reviewer`,
  `analyst_lead`, `analyst_worker`, `librarian_io`, `coder`, `analyst_colead`.
- The default `models.yaml` comes from `auto_research.config_paths.models_yaml()`, resolved per
  call, so a process pointed at another configuration resolves its roles from that one. The old
  `DEFAULT_CONFIG_PATH` constant is gone: a module-level default argument is evaluated at import
  and cannot see an override set afterwards.
- `ModelRegistry(config: ModelsConfig)` — loaded registry. Method:
  `resolve(role: str) -> ModelGroupHandle`.
- `load_registry(path: str | Path | None = None) -> ModelRegistry` —
  load and validate a registry from YAML.
- `resolve(role: str, path: str | Path | None = None) ->
  ModelGroupHandle` — convenience role resolver.
- `provider_for(name: ProviderName) -> ModelProvider` — construct a provider
  adapter for a config provider. A provider outside `CHAT_PROVIDERS` gets a
  `HarnessOnlyProvider`: the role still resolves (for stamps, decorrelation and the launch
  checks) and a chat call against it raises stating that its model is driven by an author
  harness.
- `ResponsesProvider.call(...)` — the shared Responses-dialect implementation, subclassed by
  `OpenAIProvider`, `DeepSeekProvider`, `ZaiProvider` and `DashscopeProvider`, each supplying
  only a `vendor` descriptor. It resolves the key through `keys.resolve_key` (the same
  resolution the coder harness uses, so a run never authenticates two of its own actors from
  different keys) and builds the client at the member's `base_url` or the vendor's.
- `AnthropicProvider.call(...)`,
  `GoogleProvider.call(...)` — provider-specific SDK calls returning
  `ModelResponse`. The Anthropic `schema=` path uses `messages.parse(output_format=)`
  (the SDK's `transform_schema` strict-ifies the Pydantic model and the API constrains
  decoding); `tools=` uses `messages.create`.

## Changelog
Compacted 2026-09-01: entries through 2026-08-28 folded into themed ranges (detail in git history).

- 2026-05-26 -> 2026-06-22 — Provider abstraction built and made fully native across OpenAI, Anthropic
  and Google: config-driven role groups, structured output, function calling, ReAct tool-turn replay,
  prefix caching, vision/document gates, usage normalization, `priceMTk` cost metadata. `REQUIRED_ROLES`
  gained the Phase-2 authoring roles `coder` + `analyst_colead` (all configured roles are required,
  validated at load + launch preflight). Anthropic prompt-cache tuning: `cache_prefix` marks the stable
  prefix *and* the rolling conversation tail `ephemeral`, with `documents` placed after `system` and a
  breakpoint on the last, so opened PDFs are cache-written once and read thereafter (inside the 20-block
  lookback).
- 2026-07-06 -> 2026-07-20 — **Figures, retries, and parse hardening.** [FIG-VIEW]+[V2]: all three
  adapters load figure bytes into native image blocks — cross-agent figures on the call-time `images=`
  channel, a tool's own via `ToolResultPart.figures` inline at its tool-result turn — replacing the old
  `"Figure reference: <path>"` text; V-4 then dropped the hardcoded `"Figure — "` prefix so the
  presentation layer numbers captions. `retry.py` closed an F6-class killer (two runs lost to Anthropic
  529s): every `ModelHandle.call` wraps the provider call in `call_with_transient_retry` — overloaded/
  429/5xx/connection retried with config-driven exponential-jittered cooldowns
  (`orchestration.yaml: provider_retry_*`), everything else re-raising unchanged. Receive hardened after
  a co-lead's valid-JSON-plus-trailing-text reply killed a ~4 h run: `providers/_json.loads_first_json_object`
  takes the first complete JSON value on the OpenAI + Google paths. **Send side stayed hand-sanitized** —
  an offline diff proved the SDK's own `responses.parse` conversion keeps `format: path` + defaults that
  OpenAI strict mode 400-rejects on all five structured schemas.
- 2026-07-22 -> 2026-07-28 — **The two context features: built, then live-verified and enabled.**
  [I5]: `ModelUsage` gained `cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens`, each adapter
  normalizing its provider's fields into them (closing the under-count where cached ReAct input read as
  zero). **Context editing** = Anthropic provider-native `clear_tool_uses_20250919` on the `cache_prefix`
  paths via `client.beta.messages` (`context_mgmt_*` config; new `ModelResponse.context_edits` +
  a `<role>.<action>.context_edit` monitor event) — chosen over a hand-rolled in-loop summarizer per §1
  "use the wheel" and the mid-2026 consensus (PwC *Don't Break the Cache*, arXiv 2601.06007). **Tool
  search / `defer_loading`**: both adapters honour the flag and declare the provider's search tool
  (Anthropic `tool_search_tool_bm25_20251119`, OpenAI `tool_search`; Google has none), with discovery
  blocks carried through replay because the API re-expands referenced definitions from history each turn.
  Anthropic's `cache_control` breakpoint moved off deferred tools (a 400). Both flags **live-verified
  2026-07-28 and flipped true**: context editing passed unchanged; tool search first needed the
  **undated** Anthropic server-tool `name` (`_tool_search.anthropic_search_tool_name()` strips the
  trailing `_YYYYMMDD`) — a one-value bug no deterministic test could see, so the declaration test now
  asserts the name. Resident input **19,074 → 3,789** on `gpt-5.5` and **35,208 → 9,003** on
  `claude-sonnet-5`; a three-turn probe proved an expanded definition lands **inside** the conversation
  cache breakpoint (written once at 1×, read at 0.1× thereafter); Anthropic BM25 returns up to **5**
  references per query and expands all of them. The flip also exposed a test-isolation defect it did not
  cause — two `cache_prefix` tests read the machine-local `orchestration.yaml` and had passed only while
  the flag was false; fixed with a shape-complete fake (both namespaces), per-test config pinning, and a
  **sweep over both settings** asserting the real invariant (`cache_control` placement is
  namespace-independent).
- 2026-07-29 -> 2026-08-03 — **The replay seam, in four live 400s.** (1) Discovery blocks replayed onto a
  terminal structured compose — which by the then-current `schema`/`tools` exclusivity declared no tools —
  got the whole request rejected (`400 Tool reference … not found in available tools`, ending run
  `b-run-q21`); both adapters took `include_discovery = tools is not None`. (2) `Unknown parameter:
  'input[2].created_by'`: the Responses API **accepts a smaller vocabulary than it emits**, and we replayed
  `model_dump()` output verbatim — a known open SDK defect (openai-python #3008/#2323/#2670 ask for the
  `.as_input()` helper the SDK does not ship), fixed by `_as_input_item` applied **at capture** so the
  stored item is legal by construction and byte-stable (prompt caching keys on an exact prefix match, so
  the popular value-dependent `exclude_none` workaround would silently re-bill later turns). (3) `Missing
  namespace for function_call` — a discovered tool is addressed by a namespace we had rebuilt away — and
  (4) `tool_search_call … without its required 'reasoning' item`: both answered by carrying the provider's
  own record (`ToolCall.provider_fields`; `_UNCARRIED_ITEM_TYPES` inverting the default so an
  unanticipated item type rides along). Also 2026-07-30: **the tombstone guard was deleted** — it stubbed
  a definition for a discovered-then-vanished tool, a state four independent topology grounds show this
  system cannot reach (the argument is kept in the module docstring for a refactor that would make it
  reachable). 2026-07-31: `status_monitor` joined `REQUIRED_ROLES` — with the deterministic `/status`
  digest deleted there is nothing behind it, so a missing role must fail at launch. **The test gap that
  let three of these ship:** the live tool-search test stopped after turn 1 and never handed the
  provider's records back; `test_live_second_turn_after_discovery` now does.
- 2026-08-05 -> 2026-08-06 — **One request, one extraction; and the multimodal guard ([V-5], [V-3]).**
  `schema`/`tools` mutual exclusivity was **self-imposed** (asserted in four places); a live probe
  showed all three providers accept both, and it was what forced the terminal compose to declare no
  tools — the 2026-07-29 run-killer and its `include_discovery` amputation. **The response type now IS
  the assistant turn:** `AssistantToolUse` deleted, `ConversationMessage = ModelMessage |
  ModelResponse | ToolResultMessage`, `_react` appends the response it received instead of hand-copying
  three fields (that copy had silently dropped tool-search blocks until 2026-07-27 and `thinking` until
  now); `provider_blocks` → **`provider_parts`, allowlist deleted** (we read 3 of Anthropic's 16 block
  types; `anthropic 0.104.1`'s `ContentBlock` union already lacks the documented `fallback` block).
  **Replay follows each provider's own documentation** — Anthropic *"echo the assistant message exactly
  as received"*, Google stateless *"MUST always resend all `thought` blocks exactly as they were
  received"*, OpenAI's pass-back leaving items between the last user message and the function-call
  output untouched — so Anthropic and Google echo the whole array while OpenAI keeps its capture-time
  projection, whose field allowlist is **derived from the SDK's own `ResponseInputItemParam` union**
  (27 types; an unmapped type previously got no filtering). Google stops reading the
  `.text`/`.function_calls` accessors (they flatten a mixed turn and hide `thought_signature`) and
  captures the signature onto `ToolCall.provider_fields` — the **[I8]** mechanism, **not yet
  live-verified**. **Two rules inferred from live 400s were wrong** (a "thinking first" reassembly
  order, a "drop only the tool-referencing blocks" filter): the docs forbid rebuilding or filtering at
  all, and a request declaring no tools carries no provider parts and rebuilds from the interpreted
  views. Live-verified 2026-08-05 on both providers (Anthropic `thinking → server_tool_use →
  tool_search_tool_result → thinking → tool_use` echoed intact; tool-less structured compose over the
  same trajectory). Suite 657 → 665.
  **2026-08-06 finished it.** Removing the *assertions* left the branching (`if schema is not None: …
  return` **before** `if tools is not None`, so passing both silently dropped the tools — only Google
  merged them); `include_provider_parts=tools is not None` still dropped every carried part, and
  `function_call` items replayed without their required reasoning items; live run `8c75990e` died at
  **$29.50**, one step before its first commit, with `b-run-q21-0803c`'s 400 verbatim (`Item 'fc_…' of
  type 'function_call' was provided without its required 'reasoning' item`) on the GPT co-lead's
  `review_tool` compose, while `test_schema_and_tools_are_not_mutually_exclusive` passed throughout by
  asserting `parsed` came back rather than that the tools reached the request. Each adapter now builds
  ONE request (schema and tools independent; Anthropic keeps `parse` vs `create` only because
  `output_format` lives on `parse`) and runs ONE extraction filling
  `content`/`parsed`/`tool_calls`/`provider_parts`; `tool_choice` gains **`"none"`** — `"none"`
  (OpenAI), `{"type": "none"}` (Anthropic), `mode="NONE"` (Google), each read from that provider's own
  documentation — and `_call_over_messages`/`_structured_loop` declare the tools `_react` just used
  under it (under `"auto"` the model can call again and return `parsed=None`, burning a
  `structured_retry_cap` attempt). **`include_provider_parts` is deleted** rather than narrowed: with
  every replaying request declaring its tools it was dead code that could only reintroduce the bug.
  Same shape fixed in `StatusMonitorAgent`'s cap-exhaustion fallback, a plausible cause of the open
  *"`/status` returns silence"* defect. **Probed live before any code was written**
  (`tmp_scripts/probe_tool_choice_none.py`, cents): OpenAI accepts `tools` + `tool_choice="none"` +
  `text.format`; Anthropic's `messages.parse` accepts `output_format` + `tools` + `tool_choice` +
  `thinking` in one call. New live test `test_live_compose_declares_its_tools` covers an **ordinary
  function-call** trajectory, where every prior live test had exercised tool-search discovery; also
  fixed, `test_live_tool_search_defers_and_discovers` compared `usage.input_tokens`, net of cache (a
  warm prefix made an all-resident baseline report 443 tokens against a 35k prompt), and now compares
  uncached + cache-read. Suite 665 → 670; live 7/7 both providers. **Deferred, recorded:**
  `stop_reason` is not modelled (`_react` infers "done" from the absence of tool calls, so
  `max_tokens`/`refusal`/`pause_turn` all read as a clean finish); tool `input_schema`s get no dialect
  normalization; the tool_use `caller` field is dropped on a synthesized-turn rebuild.
  **[V-5], same day: no adapter had any media guard at all** — neither count, dimensions, bytes, total
  payload, nor mime was checked. Run `8c75990e` then died at **$116.10**, after all five of its
  authored tools had committed, on `400 … image dimensions exceed max allowed size for many-image
  requests: 2000 pixels`: Anthropic tightens its per-image dimension cap once a request carries **more
  than 20 image blocks**, a worker's newly-authored plotter returned 3 PNGs per call that replay inline
  on every later request, and `dcf_panels.png` was 2677x1554. New `providers/_media.py` holds the
  mechanics (walk, plan, resize, page-count); each adapter holds its own `_LIMITS` with every value
  quoted from that provider's docs. Closes the mime half of **[V-3]** and deletes `open_paper`'s 8 MB
  cap (the gate belongs where the whole request is visible). **Files API deferred** (§12): by
  Anthropic's own docs it would not have prevented this, a `file_id` image still being an image block,
  still counted against 20, still measured against 2000 px. New deps `pillow` (previously undeclared,
  transitive via matplotlib) and `pypdf`. Tests `tests/test_media_guards.py` (20/21 boundary,
  oldest-first eviction, mime routing, page limit, under-limit request **byte-identical**); live
  `test_live_figure_vision.py::test_live_many_image_request_that_used_to_400` sends the exact 21-image
  shape that died and Anthropic answers *"I received 21 panels."* Suite 770 → 780.
- 2026-08-10 -> 2026-08-19 — **Key failover, its per-provider error taxonomy, and the harness-only
  vendors.** A rate limit and a spent balance are quotas of the **key**, not of the provider, and one
  wait-it-out policy answered both: a 429 stalled a call up to ~30 min across 8 cooldowns, and a spent
  credit balance — a non-transient 400 — killed the run outright (observed 2026-07-22). New `keys.py`
  holds a process-global per-provider key pool (`ANTHROPIC_API_KEY` + optional
  `ANTHROPIC_API_KEY_FALLBACK`); `retry.is_key_scoped_provider_error` splits that class off — 429 /
  `rate_limit_error`, `billing_error`, and a 400 whose message names the credit balance, each read from
  Anthropic's own error taxonomy rather than inferred from a live failure — and
  `call_with_transient_retry` consults an injected `rotate_key` **before** its cooldown, retrying
  immediately at no cooldown and no budget cost. 529/5xx/connection are explicitly **not** rotated
  (provider capacity is identical on every key) and **401 is deliberately excluded** (a revoked key is
  a configuration fault a human must see). Rotation offers each key once per call then falls through,
  so a 429 on every key still waits and a balance spent on every key still raises; the live key being
  process state makes rotation bidirectional across calls, stranding neither. The Anthropic adapter
  passes `api_key=` explicitly (the SDK resolves the env var once per client, so after a rotation that
  value is stale); the coder subprocess gets the live key stamped on **only when it has rotated**
  (`merge_flow._with_live_anthropic_key`, common path byte-identical); each switch records a
  `provider.key_rotation` monitor event naming the slot, never the key. New preflight
  `api_key_fallbacks` rejects the three shapes `KeyPool` silently deduplicates away — blank, identical
  to the primary, set without a primary. **Stated limit:** Anthropic rate limits are per-organization,
  so the fallback must be a different org for the 429 half to help; the check cannot see the org and
  does not pretend to. Anthropic-only by choice, and guarded: `ROTATING_PROVIDERS` stops a provider
  whose adapter ignores the pool from rotating, which would keep the SDK-resolved key *and* skip the
  cooldown. Tests `tests/test_provider_key_failover.py` (33); suite 781 → 814.
  **2026-08-18 generalized it to OpenAI: the vocabulary, not the pool, was Anthropic-specific.** An
  authoring-benchmark batch died on `429 insufficient_quota` / `credit_balance_exhausted` with the
  co-lead unable to review, after 48 minutes of backoff against a balance no wait could restore.
  Anthropic reports a spent balance as a 400/403 `billing_error`, so `is_exhausted_key_error`
  short-circuited "any 429 refills" — true there, false at OpenAI, where every key quota is a 429 and
  only `code` separates the one that refills from the four that do not; adding OpenAI to
  `ROTATING_PROVIDERS` alone would have rotated onto the fallback, never retired the dead primary, and
  bounced back onto it at the fallback's next routine rate limit. Both predicates now take a
  **required** `provider` and read a per-provider rules table whose every entry cites that provider's
  own error documentation, shapes version-exact from the SDKs (openai 2.38.0 parses `.type`/`.code` out
  of `body["error"]`) and confirmed against the live failure; an unrefillable quota **raises once
  rotation is exhausted** instead of taking the transient path, since OpenAI's arrives as a 429 and
  reads as retryable by status alone. `AsyncOpenAI` sends `api_key=` explicitly. **Google stays out**:
  its troubleshooting guide says `RESOURCE_EXHAUSTED` while its error reference says
  `quota_exceeded`/`rate_limit_exceeded`, neither publishes the response body, and google-genai's
  `APIError.code` is the HTTP status integer where OpenAI's is a string code — a shared field read
  would misclassify silently — and it is off the critical path (§6). Tests 40 → 49, including the same
  429 classified oppositely under each provider; suite 948 → 957.
  **2026-08-19: `ProviderName` widened to the harness-only vendors.** A `coder` model can be served by
  DeepSeek, Z.AI or Alibaba Model Studio and driven by an author harness, so `provider_for` returns a
  `HarnessOnlyProvider` for those: it answers which vendor serves the member and raises with the reason
  if anything ever tries to hold a conversation with it. `RoleModelConfig` gained `harness`, `base_url`
  and `key_env`, all optional, so every existing chat member is unchanged. Decorrelation needed no new
  rule: a DeepSeek-served coder reads as provider `deepseek`, which `select_decorrelated_member`
  already separates from the GPT co-lead. The legality table and its two launch checks live in
  `sandbox/coder/harness.py` (see `sandbox/DOCS.md`).
- 2026-08-24 -> 2026-08-28 — **The Responses factoring: six vendors, each capability measured; and the
  prompt dump.** DeepSeek, Z.AI and Alibaba Model Studio each already publish an OpenAI **Responses**
  endpoint (the dialect our OpenAI adapter speaks), so widening the lead/co-lead axis beyond Anthropic
  and OpenAI was a factoring, not three new adapters: `providers/_responses.py` holds the wire logic —
  input projection, capture-time field filtering, replay ordering, one request / one extraction — and
  each vendor module holds a `ResponsesVendor` of documented facts beside its citations. `openai.py` is
  that descriptor plus its `MediaLimits`; `deepseek.py`, `zai.py` and `dashscope.py` are new. Copying
  540 lines written against three run-killers in one week would have forked those fixes four ways.
  **Every flag is read from the vendor's own docs and then probed: a compatibility table states what
  is *accepted*, not what is *honoured*.** DeepSeek honours `text.format` with
  `strict: true` exactly (bare JSON, declared keys, on both its models), calls functions, and accepts
  the `tools`+`schema`+`tool_choice="none"` compose shape, so it is a full member; it **rejects
  `prompt_cache_key`** outright, which our lead role sends on every cached call, so the flag is
  load-bearing rather than cosmetic. **Z.AI accepts the same parameter and ignores it** — once prose
  containing no JSON, once a fenced object whose keys the schema never declared — and a 200 with
  invented keys cannot be caught downstream, so `supports_strict_schema=False` makes the adapter refuse
  a schema with a specific reason instead. `MediaLimits` gained `images_accepted`, so a text-only model
  withholds a figure with a model-visible note rather than sending it into a refusal;
  `resolve_vendor_key` moved from the Codex backend into `keys.resolve_key` (old name re-exported),
  since a run must not authenticate two of its own actors from different keys. **Qwen shipped
  unverified**: its key authenticates and lists 162 models, but every inference call was refused
  `AccessDenied.Unpurchased` on both dialects and with a current model id — a billing gate rather than
  our wiring — and its documented Responses hosts are workspace-scoped and do not include the one the
  coder harness table names, recorded in `dashscope.py` rather than silently changed. Tests: new
  `tests/test_responses_vendors.py` (44) sweeps every vendor for endpoint, explicit key, per-flag
  gating and shared wire behaviour, **asserting on the request rather than the result**;
  `tests/test_live_schema_matrix.py` gained DeepSeek across all nine real schemas and a schema-less leg
  for Z.AI and Qwen. Suite 1008 -> 1053.
  **2026-08-25 — Qwen endpoint behaviour, and two local schema bugs.** Alibaba's newer `qwen3.8-max`
  page lists image/text/video input and native visual understanding, so Qwen figures remain enabled
  (the catalog's text-generation heading alone is not a modality limit). Its Responses docs document
  custom function tools, include `allowed_tools`, and recommend workspace-specific
  regional domains for higher stability while retaining the legacy endpoint, which is still functional;
  three fresh Qwen terminal-tool variants (automatic, `allowed_tools`, named terminal choice) all
  stalled past 130 seconds (`allowed_tools` past six minutes), so the production choice policy
  **remains the last live-verified shape instead of encoding an unverified workaround**, and a
  deployment selects the recommended domain through member `base_url` once its workspace ID is known.
  Two data-integrity bugs: null normalization had used the *presence* of `enum` to classify every
  constrained string as non-string, so a legitimate enum, const, or pattern value `"null"` became JSON
  null — it now tests whether the exact spelling is accepted; and root dereferencing had replaced any
  schema carrying `$ref`, discarding its 2020-12 sibling constraints — only the bare `$ref` + `$defs`
  root form is expanded now. A Qwen terminal response that both emits structured data and requests a
  working tool is rejected as premature for bounded retry. The OpenAI SDK otherwise gives each request
  ten minutes and retries it **twice** (which also explains why a nominal per-attempt probe timeout
  looked ineffective), so Responses clients disable those hidden retries and cap one HTTP attempt at
  **300 seconds**, leaving `retry.py` the sole visible retry owner. Focused provider/media suite: **100
  passed**.
  **Qwen session caching became adapter-wide and explicit in both live schema paths:** Alibaba disables
  Responses session caching by default, so the descriptor's documented `x-dashscope-session-cache:
  enable` header rides every Qwen request whenever the caller marks a stable prefix — the prior adapter
  silently forfeited it on the long ReAct trajectories where it matters — and both the direct
  nine-schema matrix and the two trajectory composes pass `cache_prefix=True`. **Qwen latency then
  became a controlled live-test axis:** `qwen3.7-plus` joins `qwen3.8-max` through the existing
  `(provider_name, provider, model)` fixture, so both models traverse all nine real schemas and both
  real-trajectory composes through the same DashScope adapter; Qwen matrix legs set
  `reasoning.effort=minimal` explicitly instead of inheriting Alibaba's slower `medium` default, and a
  separate identical `MemoryRecord` request sweeps `none`/`minimal`/`medium` on both models.
  Development logs are model-specific and retain elapsed time plus input, cache-read, output and
  reasoning token counts, including per-call trajectory measurements.
  **DeepSeek's text-only treatment is a policy decision, and the reason first recorded for it was
  wrong.** The claims that DeepSeek's Responses API accepts only `deepseek-v4-flash` and that the
  removed `deepseek-v4-flash-vision-exp` entry "had no vendor source" are both false against the
  vendor's own documentation: the Responses guide lists `deepseek-v4-flash / deepseek-v4-pro /
  deepseek-v4-flash-vision-exp` as supported and says image input is replaced with placeholder text on
  models other than the vision one, while the vision guide documents that model with a full limit set —
  **600 images, 8192 px per side dropping to 4096 at 15 or more, 32 MiB per image, 48 MiB body,
  JPEG/PNG/GIF/WebP** — and names the Responses API among the endpoints that accept it; the deleted
  `_LIMITS` values were that source, transcribed correctly. **Human ruling: the code is unchanged and
  stays unchanged** (`images_accepted=False`) — the project declines to depend on a multimodal path its
  vendor labels experimental, and revisits when DeepSeek ships a non-experimental one. Only the
  justification changes: "the vendor has no vision model" is a fact a later reader would act on and
  would be wrong, where "we decline the experimental one" is a decision they can reopen. The standing
  rule that produced the error is `feedback_read_provider_docs_before_encoding_rules` — read every
  relevant page (the Responses reference was read; the vision guide was not). **Second correction:**
  `deepseek-v4-pro` is accepted by the Responses endpoint, so it is reachable here with no new wiring —
  but every DeepSeek structured-output flag (`supports_strict_schema`, `strict_schema_formats`,
  `resolve_refs_in`) was measured on `deepseek-v4-flash` only and is **unprobed on Pro**, which matters
  because the lead x co-lead grid adopts Pro as a co-lead emitting `ToolReviewVerdict`. Probe before
  that grid runs.
  **2026-08-28 — `_dump_prompt` documented** (built 2026-08-27 for the gate-bench leak audit; the sync
  was missed). `types.py` gains `PROMPT_DUMP_ENV = "AUTO_RESEARCH_PROMPT_DUMP"`: when that variable
  names a file, `ModelHandle.call` appends one JSON record per call — role, member, provider/model, the
  whole normalized conversation, the tool definitions and `tool_choice`, schema and attachments — after
  the request is assembled and before dispatch, so a leak audit reads the actual prompt every actor
  received rather than a hand-built reconstruction (the 2026-08-27 hand-rebuilt scan missed the
  worktree-path leak for exactly that reason; rationale in `_dump_prompt`'s docstring). With the flag
  unset the cost is one `os.environ.get` and the request is byte-identical; serialization is
  best-effort (`default=str`) and dump failures are swallowed so the hook can never fail a run.
  Evidence use: `logs/dev/leak-scan/20260827T093104Z/prompts_readable.txt`.
- 2026-09-02 — **The per-call output cap is gone wherever it is optional** (user ruling). A cap buys
  no cost protection: a vendor bills the tokens it generated either way, so the cap's only effect
  is on the call that hits it — and the failure it produces is total. Measured the same day with
  `tests/test_live_form_shapes.py`: `deepseek-v4-pro` at `reasoning.effort: high`, sent the real
  `author_chain` tool under `max_output_tokens: 8000`, returned an **empty 200** —
  `output_tokens: 8000, reasoning_tokens: 8000`, `provider_parts: ['reasoning']`. It had spent the
  entire allowance reasoning about a large tool schema and emitted nothing, billed in full.
  **The margin is the part worth keeping:** at 21,000 the same call filed a correct two-link
  chain, and **uncapped it used 19,772 output tokens of which 17,458 were reasoning** — so the
  figure that looked generous was within ~6% of choking as well, and a slightly harder brief would
  have taken it out too. Uncapped it also filed *three* links rather than two, with a real `None`
  in the optional float on the third (no `"null"`-string quirk on this path). The cap was the whole
  cause, and no safe value for it was ever identifiable from the outside. Paying for
  nothing is strictly worse than the unbounded case, and inside a benchmark trial a choked lead
  call is a *corrupted* measurement rather than a cheap one. `max_output_tokens` is therefore
  dropped from every Responses-dialect member on a substantive path (`_responses.py` splats
  `**config.parameters`, so omitting the key omits it from the request and the vendor uses its own
  maximum) and from all four vendors in `benchmarks/science_bench/arms._PINNED_PARAMETERS`.
  **Anthropic keeps its number and must**: the Messages API requires `max_tokens` and
  `providers/anthropic.py` falls back to **1024** without it, so removing it there would be a
  silent 20x truncation — the exact failure the ruling targets. 21000 is the ceiling for
  non-streaming calls (21333), i.e. the maximum available rather than a choice, and the librarian's
  Anthropic member was raised from 12000 to it (its "light role, small outputs" figure was a guess,
  and adaptive thinking shares that budget). Two caps left in place deliberately: `status_monitor`
  at 2048, whose tightness is a *latency* decision with a stated reason and which sits on no
  measured path, and the `coder` member's, which is dead config (an author is a harness subprocess,
  never a `ModelHandle`). **The trade, stated:** a cap does bound the worst case of one pathological
  call and removing it removes that bound; the choke has now been measured and the runaway has not,
  which is what makes the trade the right way round rather than free.
