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
  parameters: dict[str, Any] = {})` — one model member from YAML.
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
Compacted 2026-08-05: entries through 2026-08-03 folded into themed ranges (detail in git history).

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
- 2026-08-05 — **`ModelResponse` is the neutral assistant turn; the provider's parts are carried whole.**
  `schema` and `tools` were **self-imposed** as mutually exclusive (asserted in four places); a live probe
  showed all three providers accept both in one request. That exclusivity is what forced the terminal
  compose to declare no tools — the request shape behind the 2026-07-29 run-killer and its
  `include_discovery` amputation — and it is gone. **The response type now IS the assistant turn:**
  `AssistantToolUse` deleted, `ConversationMessage = ModelMessage | ModelResponse | ToolResultMessage`,
  and `_react` appends the response it received instead of hand-copying three fields into a twin — the
  copy that had silently dropped tool-search blocks until 2026-07-27 and `thinking` until today.
  `provider_blocks` → **`provider_parts`, allowlist deleted** (we read 3 of Anthropic's 16 block types;
  the set is never complete — `anthropic 0.104.1`'s `ContentBlock` union already lacks the documented
  `fallback` block). **Replay follows each provider's own documentation** (Anthropic: *"echo the assistant
  message exactly as received"*; Google stateless mode: *"MUST always resend all `thought` blocks exactly
  as they were received"*; OpenAI: recommended pass-back, items between the last user message and the
  function-call output untouched), so Anthropic and Google echo the whole array while OpenAI keeps its
  capture-time projection — whose field allowlist is now **derived from the SDK's own
  `ResponseInputItemParam` union** (27 types), load-bearing once the carrier is open, since an unmapped
  type previously got no filtering. Google also stops reading the `.text`/`.function_calls` accessors
  (they flatten a mixed turn and hide `thought_signature`) and captures the signature onto
  `ToolCall.provider_fields` — the [I8] mechanism, **not yet live-verified**. **Two rules were wrong before
  the docs were read**, both inferred from live 400s: a "thinking first" reassembly order and a "drop only
  the tool-referencing blocks" filter — the docs forbid rebuilding or filtering at all. A request declaring
  no tools carries no provider parts and rebuilds from the interpreted views. Live-verified 2026-08-05 on
  both providers (Anthropic `thinking → server_tool_use → tool_search_tool_result → thinking → tool_use`
  echoed intact; tool-less structured compose over the same trajectory). Tests: `tests/test_providers.py`
  (open carrier, verbatim echo, tool-less rebuild, synthesized-turn rebuild, schema+tools together,
  SDK-derived allowlist, Gemini signature round trip), `tests/test_react_tools.py`, `test_tool_search.py`
  + `test_context_editing.py` updated; suite 657 → 665. **Deferred, recorded here:** `stop_reason` is not
  modelled (`_react` infers "done" from the absence of tool calls, so `max_tokens`/`refusal`/`pause_turn`
  all read as a clean finish); tool `input_schema`s get no dialect normalization; the tool_use `caller`
  field is dropped on a synthesized-turn rebuild; letting the terminal compose declare its tools would put
  it back on the documented echo path.
- 2026-08-06 — **The unification finished: one request builder, and no tool-less replay.**
  The 2026-08-05 pass removed the `schema`/`tools` *assertions* but not the branching: both
  adapters still ran `if schema is not None: … return` **before** `if tools is not None`, so
  passing both silently dropped the tools (only Google merged them), and
  `test_schema_and_tools_are_not_mutually_exclusive` passed anyway because it asserted `parsed`
  came back rather than that the tools reached the request. The terminal compose therefore still
  could not declare tools, `include_provider_parts=tools is not None` still dropped every carried
  part on that path, and the `function_call` items a few lines below still replayed
  unconditionally — orphaning the reasoning items they require. That killed live run `8c75990e`
  at **$29.50**, one step before its first commit, with the same 400 as `b-run-q21-0803c`
  (`Item 'fc_…' of type 'function_call' was provided without its required 'reasoning' item`) on
  the GPT co-lead's `review_tool` compose. **Now:** each adapter builds ONE request (schema and
  tools independent; Anthropic keeps `parse` vs `create` only because `output_format` lives on
  `parse`) and runs ONE extraction filling `content`/`parsed`/`tool_calls`/`provider_parts`;
  `tool_choice` gains `"none"` — `"none"` (OpenAI), `{"type": "none"}` (Anthropic),
  `mode="NONE"` (Google), each read from that provider's own documentation; `_call_over_messages`
  / `_structured_loop` take the provider `_react` just used and declare it with
  `tool_choice="none"`, so a compose keeps its declarations while being forbidden to call (with
  `"auto"` the model can call again and return `parsed=None`, burning a `structured_retry_cap`
  attempt); and **`include_provider_parts` is deleted** — with every replaying request declaring
  its tools, the drop branch was dead code that could only reintroduce the bug. The same shape
  was fixed in `StatusMonitorAgent`'s cap-exhaustion fallback, a plausible cause of the open
  *"`/status` returns silence"* defect, since that call replayed a trajectory with no tools
  declared on the GPT reporter. **Live-verified 2026-08-06 on both providers before any code was
  written** (`tmp_scripts/probe_tool_choice_none.py`, cents): OpenAI accepts
  `tools` + `tool_choice="none"` + `text.format`, and Anthropic's `messages.parse` accepts
  `output_format` + `tools` + `tool_choice` + `thinking` in one call — the one risk that could
  have forced a redesign. New live test `test_live_compose_declares_its_tools` covers the shape
  that shipped this bug: an **ordinary function-call** trajectory (the co-lead's tools are
  resident harness tools), where every prior live test had exercised tool-search discovery
  instead. Also fixed there: `test_live_tool_search_defers_and_discovers` compared
  `usage.input_tokens`, which is net of cache, so a warm prefix made an all-resident baseline
  report 443 tokens against a 35k prompt — it now compares uncached + cache-read. Suite 665 → 670;
  live 7/7 on both providers.
