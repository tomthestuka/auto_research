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

- **2026-09-02 (second) — the media budget reserves the non-media request weight.** Run
  `fbb93f26` died at its last node, `review_analysis`, on Anthropic `413 request_too_large`, and
  **no media-guard event fired** — the guard had judged the payload legal. Measured from the
  ledger: the reviewer held two PDFs (24.6 MB + 0.47 MB raw, one opened twice and deduped by
  sha256) coming to **33,512,296 encoded bytes against the 33,554,432 ceiling**, so the guard
  stopped with **42 KB** of headroom for a request whose system prompt, tool definitions and five
  iterations of tool results were orders of magnitude larger. The ceiling was being applied to a
  *subset* of what counts toward it. Anthropic states the rule for both of its document limits:
  *"Both limits are on the entire request payload, including any other content sent alongside
  PDFs"* (.../build-with-claude/pdf-support). New `_media.nonmedia_weight` estimates what the
  request carries besides media — message text, tool-call arguments, tool-result text, every
  verbatim provider part, the declared tool schemas, plus a flat per-part JSON allowance — and
  every adapter passes it as `plan_for_call(..., reserved_bytes=…)`, so eviction now happens while
  there is still room for the text that has to travel with it. It is deliberately an **estimate
  over the neutral conversation**, because the payload cannot be built until the plan exists and an
  exact figure would be circular; it rounds up, since under-reserving costs the whole request while
  over-reserving withholds one figure with a note.
  **No per-document byte cap was added**, and that is a reading of the docs rather than a
  preference: the PDF page publishes only the whole-request ceiling and the page count, so a
  per-file limit would be a number we invented — which `MediaLimits` explicitly forbids
  (`0` means "not documented for this provider"). The deleted `_OPEN_PAPER_MAX_MB` stays deleted.
  **Residual, stated:** eviction is oldest-first by design (the current turn's evidence is the most
  relevant), so a single near-ceiling document now survives while several small ones are evicted
  one at a time to make room for it. Before this change nothing was evicted and the request died,
  so the fix is strictly better — but *which* media survives is now load-bearing where it was not.
  **The Files API is not the escape route** and is worth recording as such: Anthropic recommends
  `file_id` references "to keep request payloads small", but anthropic-sdk-python#1098 reports a
  413 at ~6.49 MB of *referenced* PDFs with an ~18 KB HTTP body, i.e. an undocumented ~6.3 MB
  combined ceiling enforced during server-side extraction, unanswered. It would trade a documented
  32 MB ceiling for a tighter undocumented one. Four new tests in `tests/test_media_guards.py`.

- **2026-09-03 — a vendor with no usable document channel now receives text plus figure crops.**
  Found while gating the science sweep. Two defects, one visible and one latent. The visible one:
  `ModelHandle.call` **raises** when documents reach a member declaring `documents: false`, and the
  sweep's Qwen reviewer declared exactly that while the reviewer opens papers inside its own loop
  and forwards them into the verdict call — four slots would have died at that node. Ruled to keep
  the raise (right for a genuinely mis-declared member) and fix the declaration. The latent one:
  `doc_mimes` is empty on all three Responses-dialect vendors and the check reads
  `if limits.doc_mimes and ...`, so an empty set skipped the guard **entirely** and a raw base64
  PDF was being sent to a vendor that accepts none.
  Alibaba's Create-a-response reference (read 2026-09-03) rules the native path out twice —
  `input_file` takes `file_url`, "The public URL of the file", and neither `file_data` nor
  `file_id`; PDF input is "Currently only supported by `qwen3.5-ocr`" — so local parsing is the
  only route rather than a preference. New `ResponsesVendor.accepts_inline_documents` (default
  `True`, so OpenAI/DeepSeek/Z.AI wire bytes are unchanged, which matters because OpenAI shares
  this adapter and a science-sweep slot had already run on it) and a new `_doc_parse` module:
  Docling render-and-crop, disk cache keyed on `sha256`, manifest stamping the parser version.
  **Docling rather than a lighter renderer** because extracting embedded images is measured to
  recover almost nothing from a scientific paper, and its layout model crops the figure region
  with its caption already linked. Six new tests in `tests/test_document_substitution.py`, each
  verified to fail against the unflipped vendor flag; two `science_bench` tests rewritten from the
  old decision to the new invariant. Suite 1752 → 1760.

- 2026-09-09 — **F7: a documented-transient condition wearing a non-transient status; N6: an
  adapter raise is still billed.** *F7:* slot 6 died in **one HTTP attempt**, 4.97 s after a
  successful call ($11.45, zero tools), on Alibaba returning **HTTP 400** with
  `code: "ServiceUnavailable"` and message `"<503> InternalError.Algo: ... [Too many requests ...
  throttled ... Please try again later.]"`. `is_transient_provider_error` reads `status_code`
  first; it is an `int`, so the string-`code` fallback on the next line is **unreachable**, 400 is
  in neither transient set, and no message inspection existed for this provider at all. Zero
  retries fired (proven by positive control: a sibling run's `retry.py` cooldown warning appears
  in these logs, this run's has none), and the throttle was **not** self-inflicted — a sibling
  made a successful call on the same key 31 s later at 0.007% of the documented 15,000 RPM.
  New `_TRANSIENT_BODY_OVERRIDES` + `_transient_by_body`: status-first stays the rule, and the
  body is consulted **only** on the statuses a vendor is documented to mislabel, per vendor, each
  entry beside its own citation. `dashscope` (400) and `deepseek` (400,
  `insufficient_system_resource`, which that vendor reports as a *finish reason*) are wired;
  Anthropic and OpenAI need nothing, both publishing transient conditions under truthful statuses.
  The `code` alone cannot classify it — the same page documents `400-ServiceUnavailableError` as a
  permanent malformed request — so markers are matched as **vocabulary, never as a bare
  three-digit substring** (`0.36692140104299825` contains "429"), pinned by a test.
  `is_transient_provider_error` gains an optional `provider`; without it every 400 stays fatal, so
  no other vendor becomes retryable by accident. **Adjacent gap closed:** Anthropic's documented
  spend-cap 429 `enforced_spend_limit_reached` was absent; it now sits in `exhausted_codes`, not in
  the message markers, because it arrives typed `rate_limit_error` and that type early-returns
  "refilling" before any marker is read — the first attempt put it in the markers and was inert.
  *N6:* usage was recorded only on `traced_model_call`'s return path, so a turn our own extraction
  rejected after a billable response was silently free — slot 5's two failing structured attempts
  (~195k input tokens) appear neither in its ledger nor in its reported $29.23. The four
  `_responses.py` extraction raises now go through `_raise_billed(exc, response)`, which attaches
  `billed_usage` and re-raises **the same exception type**, because the type is the role layer's
  retry contract (`ValueError` retried, `RuntimeError` terminal). `traced_model_call` records
  `billed_usage` on the exception path; a provider refusal before generation carries none and is
  correctly billed as nothing. Tests: `test_provider_retry.py::test_f7_*` (6),
  `test_observability.py::test_n6_*` (2).

- 2026-09-09 (second) — **F7's live status, and what a failed provocation measured instead.**
  The human pointed out that concurrency can provoke a vendor throttle on demand, which retired my
  claim that this class was not live-testable. A gated probe now exists
  (`test_live_schema_matrix.py::test_live_a_provoked_qwen_throttle_is_waited_out_not_fatal`,
  `AUTO_RESEARCH_LIVE_THROTTLE_PROBE=1`, with concurrency / model / heavy as env knobs). Its proof
  channel is exact rather than a proxy: `call_with_transient_retry` emits its cooldown warning
  **only** on the branch taken after `is_transient_provider_error(exc, provider)` returns True, so
  a captured warning naming a 400 `ServiceUnavailable` is the predicate classifying a real vendor
  error, seen through the production path.
  **It did not fire, twice, and the non-event is the measurement.** 20 light concurrent calls on
  `qwen3.7-plus`: 20/20 in ~18 s, zero throttles (two runs). 48 **heavy** concurrent calls on
  `qwen3.8-max` at `reasoning.effort: high`: **48/48 in 251 s, zero throttles.** That *confirms*
  the slot-6 forensics rather than failing to reproduce them — that run died at 0.007% of
  Alibaba's documented 15,000 RPM with a sibling succeeding on the same key 31 s later, i.e.
  vendor-side **serving capacity**, not a quota. A rate limit would have refused 48 concurrent long
  generations; capacity did not care, so load from our side cannot reach it.
  **So F7 is verified deterministically against the exact recorded body and is NOT live-verified**,
  and that is stated rather than papered over. The probe stays committed and gated, asserting the
  property that matters (no call may die on a condition the vendor documents as retryable), so a
  real occurrence is caught when it happens. Incidental gain worth keeping: this is the first
  *sustained*-load figure for Alibaba — both earlier concurrency measurements carried the stated
  limit "short calls, not sustained multi-hour load" — and it retires the worry that the sweep's
  concurrency of 5 sat near a vendor ceiling.

- 2026-09-10 — **A1: a tool's figures nest inside their `tool_result` on Anthropic.** A run died
  after 3h34m and $42.11, with 4 tools committed and a finished worker report in hand, on
  `400 … tool_search_tool_bm25 tool use with id srvtoolu_01PCDhG72zkmP2EY5L4D1nmX was found
  without a corresponding tool_search_tool_bm25_tool_result block`. A 400 is outside the transient
  class, so one attempt, no retry, and the exception killed the whole run rather than the worker.
  **Verified against the artifact, not inferred from the message:** `prompts.jsonl` line 279 holds
  assistant turn 12 with that exact `server_tool_use` id unpaired beside `tool_use review_report`,
  and message 13 is that tool's result carrying 4 figures. Rate in the leg: **1 of 6**
  server-tool-bearing turns, the only one whose result carried figures.
  **Three documented rules bind, all at the message top level, and only nesting satisfies all
  three** — which is what F5f got wrong by answering one of them in isolation. (1) A text block
  *beside* the results invites the 2-3-token empty turn that killed slot 3. (2) With an unresolved
  server-tool call, "that message must contain nothing except the `tool_result` blocks", and
  "Adding anything after the `tool_result` blocks in that user message, such as text, ends the
  assistant turn" — a following message is still "anything after", which is exactly what F5f
  emitted. (3) For several images, label each one. Nesting moves the caption from sibling position
  to *inside* the result, so (1) and (2) hold and (3) applies where it belongs.
  New `anthropic._tool_result_block`; the no-figure path stays a **bare string**, byte-identical,
  so the common cached prefix is untouched. It also fixes a shape no model asked for: a figure the
  media guard **withholds** used to degrade to a bare caption block right after the results, i.e.
  rule (1)'s forbidden arrangement produced by our own guard.
  **Scope is one adapter, decided per vendor from each vendor's own reference** (table above), and
  the deciding facts were two: only Anthropic and OpenAI set `supports_tool_search=True`, so the
  pending-server-tool hazard can only arise on those two; and Alibaba types its tool output as a
  required string, so its separate-item shape is a contract rather than a workaround. OpenAI is
  deliberately left alone — its array form is real ("For functions that return images or files,
  you can pass an array of image or file objects instead of a string") but it publishes no
  counterpart to the "only `tool_result` blocks" rule, no worked example, and one measured leg
  pushed 548 requests through the current shape including one carrying **18 image blocks** with no
  failure. **No `_responses.py` or `openai.py` change.**
  **Volume was measured and ruled out of scope.** `review_report` returns the entire citable
  catalogue on every call, budget-free, and each result replays inline forever: one result carried
  **8 images**, and a single request reached **18 image blocks** (two short of the >20 threshold
  that killed run `8c75990e` at 21). Nesting is limit-neutral — "All `image` blocks in the request
  count toward this threshold, including … images nested inside `tool_result` content" — so this
  change neither creates nor worsens it; recorded as the first §13 loose end instead.
  Also corrected: `ToolResultPart.figures`' docstring asserted "an image turn adjacent to this
  tool-result turn", stating one adapter's rendering choice as though the neutral layer required
  it — which is how an adapter-local decision came to look like a design invariant.
  `_mark_last_block_cacheable` now puts the breakpoint on a `tool_result` block; that is
  type-legal in anthropic 0.104.1 (`cache_control` is a field of `ToolResultBlockParam`) but
  **SDK evidence only**, documented in no prose, and noted as such rather than cited.
  **The full suite is what caught the second pinned test** (`test_turn_end.py`'s F5f test), which
  the three obvious suites did not touch; and the media-guard test failed in the predictable
  direction — its walker read only top-level blocks, so nested images read as "the guard dropped
  everything", now fixed with a depth-aware helper. Suite 1419 → 1421 passed, 188 skipped.
  Regression direction verified before trusting it: the old shape leaves a trailing `user` message
  (would 400) and the new one leaves nothing, both carrying the pixels.
  **LIVE-VERIFIED 2026-09-10**
  (`tests/test_live_figure_vision.py::test_live_pending_server_tool_with_figures_that_used_to_400`),
  through the real manifest and `ModelHandle`: turn 1 came back
  `['thinking', 'server_tool_use', 'tool_use']` — the documented pending state beside a client
  call, i.e. the precondition itself rather than a simulation of it — and the figure-bearing
  continuation was accepted. The cell skips rather than passes if the model declines to co-emit,
  so it cannot pass without producing the shape.
  **The assertion reads the model's answer rather than counting tokens** (the human's call, and
  the better instrument). Each panel is a flat fill in an unmistakable colour and its caption
  names no colour, so a model handed only captions cannot produce them; the reply named both, and
  incidentally proved the pending search resolved on that request — *"The tool search found the
  relevant tool: research_toolkit_xspec_utils_ixpe_pcube"*. One request, three properties: the
  shape is accepted, the pixels were delivered, and the server tool resolved.
  The token-count version it replaced was indirect **and** easy to get wrong, which is worth
  recording on its own: "total input" needs all THREE classes summed, because `input_tokens` is
  net of cache and on a *cold* `cache_prefix` request the entire payload bills as
  `cache_write_tokens` — measured on this request as 2 uncached + 0 cache-read + ~11,491
  cache-write. Summing only `input + cache_read` reads that as **2** and cannot tell a delivered
  request from an empty one. `tests/test_live_tool_search._sent` carries exactly that omission;
  left alone as out of scope, recorded so it is not trusted blindly. Delivery was also confirmed
  directly while diagnosing it: the same conversation reports **1,982** input tokens with the
  figures nested against 1,506 for the two figures alone.

- 2026-09-11 — **Anthropic's spend cap has a third face, and it cost two live trials.** A batch lost
  slots 2 and 3 at **$80.24** and **$73.61**, each with four tools already committed, to
  `400 invalid_request_error` — *"You have reached your specified workspace API usage limits. You
  will regain access on 2026-10-01 at 00:00 UTC."* **Key rotation never fired**, and the reason is
  that all three structured arms of `is_key_scoped_provider_error` miss it: the status is **400**
  (not the 429 in `key_scoped_statuses`), `.type` is `invalid_request_error` (not `billing_error`),
  and `.code` is **None** (so `enforced_spend_limit_reached` cannot match). Only a message marker
  could catch it, and the tuple held exactly one entry, `"credit balance"` — written for the
  *other* 400. Added `"workspace api usage limits"` to `exhausted_message_markers`.
  **It belongs in the *exhausted* tuple, not merely the key-scoped one**, because the message names
  a return date weeks out: the key is retired for the process, so rotation cannot come back to it at
  the fallback's next routine 429 and turn a one-minute wait into a dead run (the Q21 shape). Human
  confirmation that the two keys sit in **different workspaces** is what makes rotation the right
  answer here rather than a cosmetic one.
  Three notes worth keeping. **Markers are matched against `str(exc).lower()`**, so a marker
  carrying the vendor's own capitalisation can never fire — the same inert-rule trap that moved the
  spend-cap 429 out of the markers and into `exhausted_codes`; a new test asserts every Anthropic
  marker is lowercase. **A source edit does not reach a running process**: the three trials in
  flight had imported `retry.py` at launch, so the fix could not save them and only a newly started
  run is protected — the reason the recovery was a fresh relaunch rather than a resume.
  **And the first reconstruction of the exception was unfaithful**: it passed only `"Error code:
  400"` as the message where the SDK embeds the whole body, which made even the known-good
  credit-balance case return `False` and would have sent the diagnosis after a second, non-existent
  bug. The run's own recorded string settled it; the test now uses that verbatim format.
  `tests/test_provider_key_failover.py` 54 (+2, +1 factory), verified to fail against the unfixed
  rule before being kept; suite 1421 → 1423.

- 2026-09-12 — **A1: `ModelResponse.has_unresolved_server_tool`, normalised per adapter.** The
  provider-neutral answer to "is this turn mid-server-tool", so `_react` can hold an injected user
  turn rather than splice it into a conversation the API says is closed to it. Investigated per
  adapter family against each vendor's own documentation before any code was written, and
  cross-checked against the verbatim prompt dumps on disk.
  **Server tools come in four shapes, not one.** *Anthropic* — a separate result block, id-paired,
  and the only place the state is both documented and measured (3 of 121 distinct assistant turns
  in one batch). *OpenAI* — a separate result item, tool search only, and only in **client**
  execution mode, which we never use; hosted mode always returns the pair together. *Google* —
  separate id-paired parts (`tool_call`/`tool_response`, `executable_code`/`code_execution_result`),
  unreachable while the adapter declares no hosted tool and never sets
  `include_server_side_tool_invocations`. *Alibaba* — the result is folded **into** the call item
  across seven documented hosted tools, so "unresolved" can only mean a non-terminal `status`.
  DeepSeek documents every built-in type as *Ignored*; Z.AI publishes no contract for `/responses`
  at all, so its empty set rests on **undocumented**, not documented-absent, grounds — recorded in
  its module because it changes what a later reader may conclude.
  **Five rules, each with a failure behind it.** Pair by id, never by position — one measured
  Anthropic turn holds **seven** server calls in interleaved groups. Never match by tool name —
  our own *client* tools are named `web_search`/`web_fetch`, colliding with Anthropic's server
  tools of the same names (134 declarations each in one dump). Never key on OpenAI's `call_id` —
  hosted mode sets it `null` on both items and `_as_input_item` drops `None`, so id-pairing reads
  `True` on every real discovery turn. Read `status` where the result is folded in. **Fail
  closed**: a missing id, an absent status, or an undeclared vendor reads unresolved — a wrong
  hold costs one iteration, the other error cost a run ($42.11, 4 committed tools, 2026-09-10).
  **`stop` is not a proxy**, measured: Anthropic returns the same finish reason on resolved and
  unresolved turns alike, 47/47 and 155/155. The two normalizers stay independent fields.
  **Guard tests assert the SET, not the constant.** Four of six vendors are a constant `False`,
  and a test asserting a constant cannot fail for the reason we care about. Instead: census
  Google's `Part` fields and `ToolType` enum, and each Responses vendor's declared
  `server_tool_item_types`. Three of Alibaba's seven types are absent from openai's own input
  union, so an unclassified hosted item would otherwise ride through capture and read `False` in
  silence.
  **Recorded, not fixed** (human rulings): Google's response walk appends each non-text,
  non-function-call part **twice** (`google.py` line 367 unconditional + the `else` branch),
  measured 7 real parts captured as 12 and replayed verbatim — introduced 2026-08-05, i.e. after
  the [I8] measurement, and left alone because Google holds no active seat. And OpenAI documents
  an assistant message's `phase` as preserve-and-resend while our replay rebuilds the text rather
  than echoing the item — observed by reading the code, never probed, and deliberately untouched.
  Corrected in place: `deepseek.py` claimed the vendor supports a hosted `web_search`; its own
  compatibility table (read 2026-09-11) lists every built-in type as *Ignored*. Moot either way —
  web search reaches every role as our own Tavily **function** tool — but the comment read as
  evidence of a capability the vendor disclaims.

- 2026-09-13 — **Alibaba's output-arm content refusal is retried, on our own measurement and
  against the vendor's documentation.** Slot 6 of the demonstration batch died at $22.83 after
  104 minutes, with 12 of 14 quantities already measured, on `400 DataInspectionFailed / "Output
  data may contain inappropriate content"` from a `qwen3.7-plus` worker — one HTTP attempt, no
  retry, and the exception killed the whole graph.
  **Measured before anything was decided** (100 live calls, $0.67, our own key and a payload
  reconstructed from the run's own ledger to 119,826 input tokens, which reproduced the
  production 400 on the first attempt): the block is **stochastic at 42%, 17 of 40 draws on
  byte-identical input**, output arm on all 40 blocks, runs test consistent with independent
  draws. The input arm was deterministic, 4 of 4. So a resend is a fresh draw — the block
  depends on the *generation*, which differs per sample.
  **Every external source says the opposite**, and the entry says so in those words: Alibaba
  files the code at HTTP 400 with the remedy *"Modify the input content and retry"*, an action
  rather than a wait; its own SDK's retry set is 503/504 only; and `QwenLM/qwen-code`,
  `QwenLM/Qwen-Agent`, `dataelement/bisheng` and `modelscope/ms-agent` all classify it terminal —
  two of those being Alibaba's own. One personal repo retries it, on an unmeasured premise.
  The rule is therefore labelled in code as **our live measurement, uncited by the vendor**.
  **Two traps, both mutation-pinned.** Alibaba publishes THREE messages under one code and the
  arm lives only in the text, so (a) a code-set entry short-circuits before the markers and would
  retry the **input** arm, and (b) the arm-ambiguous *"Input or output data…"* string CONTAINS
  the output-arm sentence as a substring, so a plain marker matches it — as does the separate
  `CustomRoleBlocked` code, whose documented message shares that wording. `_TRANSIENT_BODY_OVERRIDES`
  gained a fourth field, **excluded markers**, checked before the code short-circuit so an
  excluded verdict cannot be rescued by any other rule in the entry; an ambiguous verdict is
  treated as the arm we do not retry. Dashscope only: the rule is one vendor's measured
  behaviour, not a shared truth about moderation, and it never rotates or retires a key because
  Green Net is account-independent.
  **Stated limits.** The retry rides the ordinary transient path (human ruling: reuse the
  machinery we have), so it pays that path's exponential cooldown — a resample that would clear
  instantly waits ~60 s, which is cheap against losing a run but is dead wall-clock. A blocked
  call returns **no usage object**, so what a retry bills is unmeasurable from the response and
  undocumented by the vendor. And the retry lowers frequency without reaching zero, so the
  containment of a worker's provider exception ([F15]) remains the primary fix and is unaffected.
  **Separately measured and NOT acted on:** the trigger is the lead's own `what_to_probe` text,
  handed to every worker verbatim — 0 of 24 draws blocked without it against 17 of 40 with it
  (Fisher p = 0.000079), with request size excluded because stripping 46% of the payload *raises*
  the rate. That text is model-generated per round, so there is no fixed string to edit, and
  removing the field changes what the model is asked to do, so the experiment does not isolate a
  phrase. Recorded in the handoff rather than fixed.
  `tests/test_provider_retry.py` 16 → 23, every string taken from a vendor page or a real
  failure; both traps verified against their own mutation.

