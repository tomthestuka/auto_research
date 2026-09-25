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
  **inline at their tool-result turn**, so they replay in place every round and fall inside the
  cached prefix as the loop grows (the model keeps *seeing* its plots, and the terminal report
  call that replays the trajectory does too). **The exact shape is per-vendor and is a declared
  decision, not a shared default** — see the table below; the neutral `ToolResultPart` carries
  references and imposes no arrangement.

  | Vendor | A tool's figures go | Why, from that vendor's own reference |
  |---|---|---|
  | Anthropic | **nested inside `tool_result.content`** | `content` takes "a list of nested content blocks … `text`, `image`, `document`, or `search_result`", with a worked *"tool result with images"* example. **Required**, not merely allowed: when the same assistant turn has an unresolved `server_tool_use`, "that message must contain nothing except the `tool_result` blocks", and "Adding anything after the `tool_result` blocks in that user message, such as text, ends the assistant turn … the request then fails with a 400". A following message is still "anything after" — that cost a run $42.11 on 2026-09-10. Nesting also keeps the separate rule that a text block *beside* the results invites the 2-3-token empty turn. Limit-neutral: "All `image` blocks in the request count toward this threshold, including … images nested inside `tool_result` content" |
  | OpenAI | a `user` item after the `function_call_output` | `output` **may** be an array ("For functions that return images or files, you can pass an array of image or file objects instead of a string"), but OpenAI publishes no counterpart to the "only `tool_result` blocks" rule, its input is a flat item list rather than paired messages, and no failure has been observed on the current shape (548 requests in one measured leg, one carrying 18 image blocks). Left as-is deliberately; 8-20 parts in one output is undocumented and has no published example |
  | Alibaba | a `user` item after the `function_call_output` | `function_call_output.output` is typed "(string, required)" with no array variant, and image parts are legal only inside a `user` item. Bundling would violate a published type. Its one ordering rule — a `function_call_output` "must immediately follow its corresponding function_call message" — is why the image item comes strictly after, never between |
  | Z.AI | a `user` item after the `function_call_output` | Publishes no Responses item-level reference at all; its only documented tool-result schema (Chat Completions `tool` role) is string-only. Silence is not permission |
  | DeepSeek | withheld (`images_accepted=False`) | Its reference *does* document the array form for `deepseek-flash`, but this adapter sends no images, so the branch is unreachable |
  | Google | a `user` `Content` after the `functionResponse` | Its nesting is a different mechanism — `FunctionResponse.parts[].inline_data`, "available for Gemini 3 series models", and `FunctionResponsePart` has no text variant so a caption cannot travel with the image. Out of every figure-receiving seat anyway ([I8]) |
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
  reasoning about) against the ceiling **minus the request's non-media weight**. That reserve is
  what makes the whole-request rule real: Anthropic states both of its document limits are *"on
  the entire request payload, including any other content sent alongside PDFs"*, so budgeting
  media against the full ceiling admits a request the provider refuses (a live 413 with 42 KB of
  headroom left for the conversation). Each adapter measures its own with `nonmedia_weight` and
  passes it as `reserved_bytes`. An image that breaches a dimension or byte limit is **resized**;
  a document,
  which cannot be, is **withheld**. Either way the block is replaced by a model-visible note naming
  the file and the reason — never a silent absence, which is what invites invention.

  **A vendor with no usable file channel gets the document assembled locally instead.**
  `ResponsesVendor.accepts_inline_documents` declares whether a vendor takes the inline base64
  `input_file` part this adapter builds. It is a *capability*, read from that vendor's own
  reference, defaulting `True` (the OpenAI shape) so every unread vendor stays byte-identical on
  the wire. Alibaba is the one `False`: its Create-a-response reference accepts `input_file` by
  **`file_url` (a public URL) only** — neither `file_data` nor `file_id` — and restricts PDF input
  to `qwen3.5-ocr`. Our papers are local files, and publishing one to obtain a URL is not something
  a run may do.

  What replaces it is not a downgrade invented for one vendor: Anthropic and OpenAI both ingest a
  PDF as *text plus a rendered image per page* by their own documentation, so `_doc_parse` builds
  the same two channels where the vendor does not. Docling **renders** the page and crops the
  region its layout model detected — the reason a *vector* plot survives, where pulling embedded
  rasters recovers almost nothing (measured on Krawczynski et al. 2022: 11 of 15 figure pages
  carry no embedded image at all, because matplotlib/TikZ/R emit path operators, not image
  XObjects).

  Three properties keep it honest. The crops are added to the request **before** planning, so they
  are counted, budgeted and evicted like any other image rather than bypassing the guard. The
  extracted text is added to `reserved_bytes`, because it is request weight the plan cannot see.
  And a parse failure reports the document **UNREAD** rather than letting it look empty — "I
  received nothing" and "I received an empty paper" invite different and equally wrong inferences.
  Results are cached on disk under `<runtime_dir>/parsed_docs/<sha256>/` (text, figure PNGs, and a
  manifest naming the Docling version and `images_scale`), because a ReAct loop re-sends its
  attachments every turn and the paper this was built for takes ~40 s to parse. A resize
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
  produced, carried as lightweight references at their own turn (so they persist in history
  and cache), distinct from the call-time ``images=`` channel used for cross-agent figures.
  **The wire shape is each adapter's declared decision, not a contract of this type** — see
  the per-vendor table above.
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
Compacted 2026-09-01 and again 2026-09-24 (detail in git history).

- 2026-05-26 -> 2026-06-22 — Provider abstraction built, native on OpenAI/Anthropic/Google: role groups,
  structured output, function calling, ReAct replay, prefix caching, vision/document gates, usage,
  `priceMTk`. `REQUIRED_ROLES` gained `coder` + `analyst_colead`; every configured role is required.
- 2026-05-26 -> 2026-06-22 — Anthropic `cache_prefix` marks the stable prefix and the rolling tail
  `ephemeral`, `documents` after `system` with a breakpoint on the last (an opened PDF caches once).
- 2026-07-06 -> 2026-07-20 — [FIG-VIEW]+[V2]: all adapters send figure bytes as native image blocks
  (call-time `images=`; a tool's own via `ToolResultPart.figures`); [V-4] dropped the `"Figure — "` prefix.
- 2026-07-06 -> 2026-07-20 — `retry.call_with_transient_retry` wraps every `ModelHandle.call`
  (overloaded/429/5xx/connection, `provider_retry_*` cooldowns); `_json.loads_first_json_object` on the
  OpenAI + Google receive paths; the send side stays hand-sanitized for OpenAI strict mode.
- 2026-07-22 -> 2026-07-28 — [I5]: `ModelUsage` gained `cache_read_tokens`/`cache_write_tokens`/
  `reasoning_tokens`. Context editing: Anthropic-native `clear_tool_uses_20250919` on `cache_prefix`
  paths (`context_mgmt_*`; `ModelResponse.context_edits`, `<role>.<action>.context_edit` event).
- 2026-07-22 -> 2026-07-28 — Tool search / `defer_loading`: Anthropic `tool_search_tool_bm25_20251119`
  (sent under its undated `name`), OpenAI `tool_search`, Google none; discovery blocks replayed,
  `cache_control` kept off deferred tools. Both features LIVE-VERIFIED and enabled 07-28.
- 2026-07-29 -> 2026-08-03 — Replay seam (four live 400s): `_as_input_item` filters Responses items at
  capture (the API accepts less than it emits); `ToolCall.provider_fields` + an inverted
  `_UNCARRIED_ITEM_TYPES` carry the provider's own record. `include_discovery` stopgap (replaced 08-05).
- 2026-07-30 -> 2026-07-31 — Tombstone guard deleted (the state is unreachable; argument kept in the
  module docstring). `status_monitor` joined `REQUIRED_ROLES`.
- 2026-08-05 — `schema`/`tools` exclusivity removed (all providers accept both). The response IS the
  assistant turn: `AssistantToolUse` deleted, `ConversationMessage = ModelMessage | ModelResponse |
  ToolResultMessage`; `provider_blocks` → `provider_parts`, block allowlist deleted.
- 2026-08-05 — Replay per each provider's docs: Anthropic and Google echo the whole array; OpenAI keeps a
  capture-time projection derived from the SDK's `ResponseInputItemParam`; Google captures
  `thought_signature` onto `provider_fields` ([I8], not live-verified). LIVE-VERIFIED on Anthropic + OpenAI.
- 2026-08-06 — Each adapter builds ONE request, runs ONE extraction; `tool_choice` gains `"none"`
  (OpenAI `"none"`, Anthropic `{"type": "none"}`, Google `mode="NONE"`), under which every replaying
  compose declares its tools; `include_provider_parts` deleted. `stop_reason` deferred (see `stop`).
- 2026-08-06 — [V-5]: new `providers/_media.py` (walk, plan, resize, page-count) + per-adapter `_LIMITS`
  quoted from each provider's docs (Anthropic: >20 image blocks ⇒ 2000 px); closes [V-3]'s mime half;
  `open_paper`'s 8 MB cap deleted; Files API deferred; deps `pillow`, `pypdf`. LIVE-VERIFIED (21 images).
- 2026-08-10 -> 2026-08-17 — Key failover: `keys.py` per-provider key pool (`…_API_KEY` + optional
  `…_FALLBACK`); `retry.is_key_scoped_provider_error` (429, `billing_error`, credit-balance 400) makes
  `call_with_transient_retry` call `rotate_key` before its cooldown; 5xx/529 and 401 never rotate.
- 2026-08-10 -> 2026-08-17 — Adapters pass `api_key=` explicitly; each switch emits
  `provider.key_rotation` (slot, never key); preflight `api_key_fallbacks`; `ROTATING_PROVIDERS` guards
  providers whose adapter ignores the pool. Anthropic only at first.
- 2026-08-18 — OpenAI rotates: `is_key_scoped_provider_error`/`is_exhausted_key_error` take a required
  `provider` and read a per-provider, cited rules table (OpenAI reports every quota as a 429; only `code`
  separates the refilling one); an unrefillable quota raises once rotation is spent. Google unwired.
- 2026-08-19 — `ProviderName` widened to harness-only vendors (DeepSeek, Z.AI, Alibaba) →
  `HarnessOnlyProvider`, which raises on any chat; `RoleModelConfig` gained optional `harness`,
  `base_url`, `key_env`.
- 2026-08-24 — Responses factoring: `providers/_responses.py` holds the wire logic, each vendor a
  cited `ResponsesVendor`; new `deepseek.py`, `zai.py`, `dashscope.py`. DeepSeek honours strict
  `text.format` and rejects `prompt_cache_key`; Z.AI ignores `text.format` (`supports_strict_schema=False`).
- 2026-08-24 — `MediaLimits.images_accepted` (a text-only model withholds figures with a note);
  `resolve_vendor_key` moved into `keys.resolve_key` (old name re-exported). Qwen unverified then
  (`AccessDenied.Unpurchased`; its documented hosts are workspace-scoped, noted in `dashscope.py`).
- 2026-08-25 — Qwen keeps figures (`qwen3.8-max` lists image input) and the last live-verified tool-choice
  shape; a terminal response emitting data plus a working tool is rejected as premature. Qwen sends
  `x-dashscope-session-cache: enable` whenever `cache_prefix` is set.
- 2026-08-25 — Null normalization tests the exact spelling (an enum/const/pattern `"null"` survives);
  only a bare root `$ref` + `$defs` is expanded. Responses clients disable the SDK's hidden retries and
  cap one HTTP attempt at 300 s (`retry.py` sole retry owner).
- 2026-08-25 — DeepSeek text-only is policy (ruling: `images_accepted=False` stays while its vision model
  is experimental), not a vendor limit. `deepseek-v4-pro` is reachable; its structured-output flags were
  measured on `deepseek-v4-flash` only.
- 2026-08-28 — `PROMPT_DUMP_ENV` (`AUTO_RESEARCH_PROMPT_DUMP`): `ModelHandle.call` appends one JSON record
  of the whole request per call before dispatch; unset ⇒ byte-identical; dump failures swallowed.
- 2026-09-02 — Ruling: `max_output_tokens` dropped wherever optional (an 8000 cap choked
  `deepseek-v4-pro` into an empty, fully billed 200). Anthropic keeps `max_tokens` (required; the adapter
  falls back to 1024), at the 21000 non-streaming maximum; `status_monitor` keeps 2048.
- 2026-09-02 — `_media.nonmedia_weight` estimates a request's non-media bytes, passed by every adapter as
  `plan_for_call(..., reserved_bytes=…)` (a 413 had passed the guard with 42 KB headroom). No
  per-document byte cap (none documented). Residual: oldest-first eviction keeps one large document.
- 2026-09-03 — `ResponsesVendor.accepts_inline_documents` (default `True`; Alibaba `False`, its
  `input_file` takes URLs only) + new `_doc_parse` (Docling render-and-crop, sha256 disk cache): such a
  vendor gets text plus figure crops. Ruling: `documents: false` members still raise on documents.

- 2026-09-09 — F7: `_TRANSIENT_BODY_OVERRIDES` + `_transient_by_body` read the body only on statuses a
  vendor mislabels (Alibaba 400 `ServiceUnavailable` throttle; DeepSeek 400 `insufficient_system_resource`),
  markers matched as vocabulary; `is_transient_provider_error` gains optional `provider`.
- 2026-09-09 — Anthropic's `enforced_spend_limit_reached` 429 joined `exhausted_codes`. F7 is NOT LIVE
  TESTED: a gated throttle-provocation probe (`AUTO_RESEARCH_LIVE_THROTTLE_PROBE=1`) never provoked one.
- 2026-09-09 — N6: `_responses.py` extraction raises go through `_raise_billed` (attaches `billed_usage`,
  same exception type) and `traced_model_call` records it, so a rejected billable turn is billed.

- 2026-09-10 — A1: on Anthropic a tool's figures nest inside its `tool_result`
  (`anthropic._tool_result_block`; the no-figure path stays a bare string, byte-identical), as the
  pending-server-tool rule requires. OpenAI/Alibaba unchanged. LIVE-VERIFIED 09-10.
- 2026-09-10 — `_mark_last_block_cacheable` may mark a `tool_result` block (SDK-typed, undocumented);
  `ToolResultPart.figures`' docstring no longer asserts an adjacent image turn.
- 2026-09-11 — `"workspace api usage limits"` (Anthropic 400 `invalid_request_error`, `code` None) added
  to `exhausted_message_markers`, so it rotates and retires the key; markers must be lowercase
  (matched against `str(exc).lower()`).

- 2026-09-12 — A1: `ModelResponse.has_unresolved_server_tool`, per adapter: Anthropic id-paired result
  block; OpenAI client-mode tool search only; Google id-paired parts; Alibaba a non-terminal `status`;
  DeepSeek/Z.AI none. Paired by id, never by name or OpenAI `call_id`; fails closed; `stop` no proxy.
- 2026-09-12 — Recorded, not fixed (rulings): Google captures non-text, non-call parts twice; OpenAI's
  message `phase` is not replayed. `deepseek.py`'s hosted-`web_search` comment corrected (*Ignored*).
- 2026-09-13 — Alibaba's output-arm `DataInspectionFailed` 400 is retried as transient (our measurement:
  42% stochastic; the vendor calls it terminal); `_TRANSIENT_BODY_OVERRIDES` gained excluded markers so
  the input arm, the ambiguous message and `CustomRoleBlocked` stay fatal. Never rotates a key.
- 2026-09-14 — An emit-tool call carrying nothing usable raises a `ValueError` naming `status`,
  `incomplete_details`, `raw_argument_chars`, `json_fault` and the raw string (capped at
  `_RAW_ARGUMENTS_CAP` = 2000), where it was silently `{}`. No control-flow change.
