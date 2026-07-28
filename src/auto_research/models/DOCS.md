# `auto_research.models` — provider abstraction & registry

## Purpose
Resolves an agent role (e.g. `reviewer`) to a configured group of LLM calls,
using `config/models.yaml`. Hides the per-provider SDK differences behind a
thin common interface so any role group can be backed by OpenAI, Anthropic, or
Google by editing YAML alone
([CLAUDE.md §6](../../../CLAUDE.md#6-config-driven-model-registry)).

## Architecture
- `types.py` — provider-neutral Pydantic models, `ModelHandle`, and
  `ModelGroupHandle`.
- `registry.py` — loads and validates `config/models.yaml`; exposes
  `load_registry()` and `resolve(role)`.
- `providers/` — thin adapters for OpenAI, Anthropic, and Google. SDK clients
  are imported only inside `call()` so importing the package does not require
  credentials or make network calls.
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
  tool carrying it is a 400); discovery blocks are captured onto `ModelResponse.provider_blocks`
  and replayed verbatim from `AssistantToolUse.provider_blocks`, since the API re-expands
  referenced definitions from history each turn; and a reference to a tool that has since left
  the catalog gets a **tombstone** stub (`providers/_tool_search.py`) rather than a 400.
  Providers differ in what stays resident: Anthropic hides a deferred tool entirely, OpenAI
  keeps its name and description and withholds only the parameter schema. The Anthropic search
  tool is declared as the dated `type` beside its **undated** `name`
  (`anthropic_search_tool_name` derives one from the other; a mismatch is a 400). **Live-verified
  2026-07-28 on both providers** (`tests/test_live_tool_search.py`): resident input 19,074 →
  3,789 tokens (OpenAI) and 35,208 → 9,003 (Anthropic) on the real manifest, the model searched
  and called the discovered tool, and a measured three-turn probe confirmed the expanded
  definition falls **inside** the conversation cache breakpoint (cache-read at 0.1× thereafter) —
  the one behaviour no provider documents. Ships enabled (`tool_search_enabled: true`).
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
  returned in `ModelResponse.tool_calls`. `schema` and `tools` are mutually
  exclusive in one call (the two are separate provider mechanisms). Each tool is
  a provider-neutral dict `{"name", "description", "input_schema"}`. There is no
  prompt-injected JSON schema; agents validate the parsed dict with Pydantic
  (`_structured_loop`) and retry on an off-schema shape, failing loudly on cap exhaustion.
- Provider calls return a normalized `ModelResponse` with `content`, `parsed`,
  `tool_calls`, `usage`, and the raw SDK response for later observability.
- `priceMTk` is optional per model member and stores USD rates per million
  tokens with `input` and `output` keys. When absent, observability still
  records token counts with zero calculated dollar cost.

## Dependencies
- External: `anthropic`, `openai`, `google-genai`, `pyyaml`, `pydantic`, `httpx`
  (error classification in `retry.py`).
- Internal: `auto_research.env` (`provider_retry_policy` — the transient-retry knobs).
- Imported by: `auto_research.agents`, `auto_research.observability`.

## Dictionary-level reference
- `ProviderName` — literal provider label accepted in `config/models.yaml`.
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
- `AssistantToolUse(role="assistant", content: str = "", tool_calls: list[ToolCall])`
  and `ToolResultMessage(role="tool", results: list[ToolResultPart])` — neutral
  conversation turns for ReAct-loop replay. `ConversationMessage = ModelMessage |
  AssistantToolUse | ToolResultMessage`; `call(messages=…)` accepts the union.
- `ModelResponse(content: str = "", parsed: Any | None = None,
  tool_calls: list[ToolCall] = [], usage: ModelUsage = ModelUsage(),
  context_edits: list[dict] = [], raw: Any = None)` — normalized provider output.
  `parsed` carries native structured output; `tool_calls` carries native function
  calls; `context_edits` carries the provider-native context-management edits applied
  to this call (Anthropic `applied_edits`; empty on every other provider/call).
- `RoleModelConfig(name: str, provider: ProviderName, model: str,
  vision: bool = False, documents: bool = False, priceMTk: dict[str, float] = {},
  parameters: dict[str, Any] = {})` — one model member from YAML.
- `RoleModelGroup(members: list[RoleModelConfig])` — non-empty model group for
  one role.
- `ModelsConfig(roles: dict[str, RoleModelGroup])` — validated
  `config/models.yaml` shape.
- `ModelProvider` — async protocol implemented by provider adapters:
  `call(messages, config, *, schema=None, tools=None, tool_choice="auto",
  images=(), documents=(), cache_prefix=False) -> ModelResponse`. `cache_prefix`
  asks the adapter to use the provider's native prompt-cache mechanism: Anthropic
  sets `cache_control: ephemeral` breakpoints on the stable prefix (system + tool defs)
  and the rolling conversation tail; OpenAI sets a `prompt_cache_key` (prefix caching is
  automatic); Google relies on implicit caching (no-op). `messages` may include
  `AssistantToolUse`/`ToolResultMessage` turns, which each adapter translates to native
  tool_use/tool_result blocks.
- `ModelHandle(role: str, config: RoleModelConfig, provider: ModelProvider)` —
  resolved callable handle for one role member. Method:
  `async call(messages, *, schema=None, tools=None, tool_choice="auto",
  images=(), documents=()) -> ModelResponse`. Raises `ValueError` if both
  `schema` and `tools` are passed. Every call is wrapped in
  `retry.call_with_transient_retry`, so transient provider failures are waited out
  here — the single choke point all roles share — before the exception can reach
  the graph.
- `retry.is_transient_provider_error(exc) -> bool` — True for provider errors worth
  waiting out: HTTP 408/409/429/5xx (read from the SDK error's `status_code`,
  Anthropic/OpenAI, or `code`, Google genai) and connection/timeout drops
  (`httpx.TransportError`, the SDKs' `APIConnectionError`). Auth/bad-request/other
  exceptions are not transient.
- `retry.call_with_transient_retry(provider_call, *, describe, policy=None,
  sleep=asyncio.sleep) -> Any` — awaits `provider_call()`, retrying transient
  failures up to `policy.max_retries` with cooldowns growing exponentially from
  `base_cooldown_s` to `max_cooldown_s`, jittered to 50–100% so concurrent agents
  desynchronize. Policy defaults from `env.provider_retry_policy()`
  (`config/orchestration.yaml: provider_retry_*`, "Internet stability" section).
  Each wait logs a WARNING carrying `describe`; a non-transient error or an
  exhausted budget re-raises the original exception unchanged. `policy`/`sleep`
  are injectable for deterministic tests.
- `ModelGroupHandle(role: str, handles: tuple[ModelHandle, ...])` — resolved
  model group. Method: `member(name: str) -> ModelHandle`.
- `REQUIRED_ROLES` — required role names that must appear in config: `reviewer`,
  `analyst_lead`, `analyst_worker`, `librarian_io`, `coder`, `analyst_colead`.
- `DEFAULT_CONFIG_PATH` — default path to `config/models.yaml`.
- `ModelRegistry(config: ModelsConfig)` — loaded registry. Method:
  `resolve(role: str) -> ModelGroupHandle`.
- `load_registry(path: str | Path = DEFAULT_CONFIG_PATH) -> ModelRegistry` —
  load and validate a registry from YAML.
- `resolve(role: str, path: str | Path = DEFAULT_CONFIG_PATH) ->
  ModelGroupHandle` — convenience role resolver.
- `provider_for(name: ProviderName) -> ModelProvider` — construct a provider
  adapter for a config provider.
- `OpenAIProvider.call(...)`, `AnthropicProvider.call(...)`,
  `GoogleProvider.call(...)` — provider-specific SDK calls returning
  `ModelResponse`. The Anthropic `schema=` path uses `messages.parse(output_format=)`
  (the SDK's `transform_schema` strict-ifies the Pydantic model and the API constrains
  decoding); `tools=` uses `messages.create`.

## Changelog
- 2026-05-26 -> 2026-06-12 — Provider abstraction built and made fully native across OpenAI, Anthropic, and Google: config-driven role groups, structured output, function calling, ReAct tool-turn replay, prefix caching, vision/document gates, usage normalization, `priceMTk` cost metadata.
- 2026-06-15 — `REQUIRED_ROLES` adds the Phase-2 authoring roles `coder` and `analyst_colead`; all configured roles required (validated at load + launch preflight).
- 2026-06-16 -> 2026-06-22 — Anthropic prompt-cache tuning: `cache_prefix` marks both the stable prefix and the rolling conversation tail with `cache_control: ephemeral`; `documents` placed at the front (after `system`) with a breakpoint on the last, so opened PDFs are cache-written once and cache-read thereafter (kept inside the 20-block lookback).
- 2026-07-06 — Figures now carry pixels to the model ([FIG-VIEW]+[V2]): all three adapters load figure bytes into native image blocks — cross-agent figures on the call-time `images=` channel, a tool's own figures via `ToolResultPart.figures` rendered inline at their tool-result turn — replacing the old `"Figure reference: <path>"` text. Gemini stays on `generateContent`.
- 2026-07-14 — V-4: figure caption rendered verbatim; `_figure_blocks`/`_figure_items`/`_figure_parts` dropped the hardcoded `"Figure — "` prefix (the presentation layer now numbers/handles captions).
- 2026-07-16 — Transient-provider retry layer (`retry.py`), closing the F6 class that killed two live runs on Anthropic 529s: `ModelHandle.call` wraps every provider call in `call_with_transient_retry` — overloaded/429/5xx/connection errors retried with config-driven exponential-jittered cooldowns (`orchestration.yaml: provider_retry_*`, defaults 8 retries / 60 s base / 600 s cap); non-transient errors and budget exhaustion re-raise unchanged. Deterministic tests in `tests/test_provider_retry.py`.
- 2026-07-20 — Structured-output receive hardened (F6-class run-killer): a GPT co-lead tool review returned a valid JSON object followed by extra content, and `openai.py`'s bare `json.loads(output_text)` raised an unhandled `JSONDecodeError: Extra data` that killed a ~4 h live run at Tool-2 review. New `providers/_json.loads_first_json_object` takes the first complete JSON value, tolerates leading/trailing content, logs the surplus, and returns `None` (→ structured-retry) when no JSON is present; wired into the OpenAI structured path and Google's `json.loads` fallback. **Send side unchanged** — an offline diff proved the SDK's own `responses.parse`/`to_strict_json_schema` conversion keeps `format: path` + `default`s on all five structured schemas (which OpenAI strict mode 400-rejects), so the hand-rolled `_strict_json_schema_format` sanitizer stays necessary (hence B-hybrid, not full native). Stale `OpenAIProvider` docstring corrected (it claimed `responses.parse`). Tests: `tests/test_providers.py::test_loads_first_json_object_handles_surrounding_content`, `::test_openai_structured_output_tolerates_trailing_data`.
- 2026-07-22 — [I5] `ModelUsage` gains `cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens` (default 0, non-overlapping classes). Each adapter now normalizes its provider usage into them via a `_..._usage()` helper: Anthropic reads `cache_read_input_tokens`/`cache_creation_input_tokens` (input already uncached); OpenAI reads `input_tokens_details.cached_tokens` + `output_tokens_details.reasoning_tokens` and subtracts cached from total input; Google reads `cached_content_token_count` + `thoughts_token_count` (output = candidates+thoughts). Closes the under-reporting where heavily-cached ReAct input (cache reads, ~0.1x) was uncounted. Pricing/rollup live in `observability` (see its DOCS).
- 2026-07-22 — **Anthropic provider-native context editing (the context-pressure valve; live-verified 2026-07-28, see that entry below).** Aligned with the mid-2026 community consensus (Anthropic moved context management server-side; PwC "Don't Break the Cache", arXiv 2601.06007) and CLAUDE.md §1 "use the wheel" — replaced the originally-planned hand-rolled in-loop summarization. On the `cache_prefix` paths (ReAct loop + terminal report call), when `context_mgmt_enabled` (`env.context_editing_config`), `anthropic.py` routes through `client.beta.messages` with `betas=["context-management-2025-06-27"]` + a `context_management` `clear_tool_uses_20250919` edit (trigger/keep/clear_at_least from `orchestration.yaml: context_mgmt_*`); Anthropic clears the oldest tool results server-side. `_react`/adapter untouched otherwise — disabled or non-`cache_prefix` calls stay on the non-beta namespace, byte-identical. New `ModelResponse.context_edits` (normalized `applied_edits`) + `AgentRole._record_context_edit` → `<role>.<action>.context_edit` monitor event. Confirmed via SDK source/introspection that `beta.messages.parse` accepts `output_format`+`context_management`+`thinking`+`tool_choice` and `ParsedBetaMessage.parsed_output` is a property mirroring the non-beta parse; the live beta round-trip was blocked at the time by an Anthropic credit-balance 400, so this shipped with `context_mgmt_enabled` **false** (it passed on 2026-07-28 and was flipped true). OpenAI (`responses.compact`, needs stateful Responses) / Google (none) deferred. Deterministic tests `tests/test_context_editing.py`.
- 2026-07-27 — **Provider-native tool search / deferred tool loading (shipped disabled; live-verified and enabled 2026-07-28, see that entry below).** New `env.tool_search_config` (`tool_search_enabled`, default false); both adapters honour a tool's `defer_loading` flag and declare the provider's search tool (Anthropic `tool_search_tool_bm25_20251119`; OpenAI `tool_search`, gpt-5.4+; Google unchanged). New `AssistantToolUse.provider_blocks` / `ModelResponse.provider_blocks` carry the opaque discovery blocks (`server_tool_use`/`tool_search_tool_result`, `tool_search_call`/`tool_search_output`) through `_react` replay — the API re-expands referenced definitions from history each turn, so dropping them de-activates a discovered tool; **this is the same seam [I8] needs for Gemini's `thought_signature`**. Anthropic's `cache_control` breakpoint now lands on the last non-deferred tool (a deferred tool carrying it is a 400). New `providers/_tool_search.py` adds the tombstone guard for a reference whose tool has left the catalog (the failed-authoring-reset case) — a stub definition instead of an [F6]-class 400. Measured on `gpt-5.5` with the real 25-tool manifest: **19,004 → 2,382 resident tokens (−87%)**. Deterministic tests `tests/test_tool_search.py` (+12); the env-gated live round-trip `tests/test_live_tool_search.py` passed 2026-07-28 (after fixing the Anthropic search-tool `name`) and the flag was flipped true.
- 2026-07-28 — **Both context-management features live-verified; tool search fixed and enabled.** With the `ANTHROPIC_API_KEY` reinstated, the two 2026-07-22/27 features ran their env-gated live round-trips. **Context editing PASSED unchanged** — the beta path cleared tool results, reported them on `ModelResponse.context_edits`, and structured output still parsed on the beta namespace; `context_mgmt_enabled` flipped **true**. **Tool search needed a one-value fix the deterministic tests could not catch:** the Anthropic server-tool declaration sent `name: "tool_search"` beside the dated `type`, and the API pins the name to the **undated** identifier (400 *"Input should be 'tool_search_tool_bm25'"*). New `_tool_search.anthropic_search_tool_name()` derives the name by stripping the trailing `_YYYYMMDD`, so the dated type stays the single configured value and the `regex` variant follows the same rule; the declaration test now asserts the `name`, not only the `type`. Live results on the real manifest: resident input **19,074 → 3,789 tokens (−80%)** on `gpt-5.5` and **35,208 → 9,003 (−74%)** on `claude-sonnet-5`, with the model searching and calling the discovered tool on both. A three-turn `cache_prefix` probe settled the question no provider documents: the expanded definition lands **inside** the conversation cache breakpoint (turn 2 wrote 9,993 tokens, turn 3 read them back at 0.1×), because the expansion lives in the message tail and `_mark_last_block_cacheable` marks the last block. Note for capacity planning: Anthropic's BM25 returns up to **5** references per search and expands all of them, so a broad natural-language query costs more than the exact-name lookup the injected catalogue enables. `tool_search_enabled` flipped **true**. Flipping the flags also exposed a **test-isolation defect the flip did not cause**: two `cache_prefix` tests in `test_providers.py` read the machine-local `orchestration.yaml`, so enabling `context_mgmt_enabled` routed them to `client.beta.messages`, which that file's fake deliberately does not implement — they had been passing only because the flag happened to be false. Fixed by making the fake **shape-complete** — it now implements both `client.messages` and `client.beta.messages` (mirroring the real SDK) and records which namespace ran — and by pinning the config per test instead of reading it: an autouse fixture defaults the file to the base namespace, while the two namespace-sensitive `cache_prefix` tests **sweep both settings** and assert the routing plus that `context_management` rides the beta namespace only. Pinning alone would have left the file testing whichever branch the host selected; sweeping asserts `cache_control` placement is namespace-independent, which is the actual invariant. Verified by running the file under both `context_mgmt_enabled` values (22 passed each). Suite 568.
