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
- `ModelRegistry.resolve(role)` returns a `ModelGroupHandle`; callers choose a
  member with `member(name)` or iterate through `handles`.
- **Native structured output and function calling.** `call(..., schema=Model)`
  requests provider-native **structured output** (OpenAI Responses `text.format`
  json_schema, Google `response_schema`, Anthropic `messages.parse(output_format=)`);
  the parsed object is returned as a dict in `ModelResponse.parsed`.
  `call(..., tools=[...], tool_choice=...)`
  requests provider-native **function calling**; the model's tool calls are
  returned in `ModelResponse.tool_calls`. `schema` and `tools` are mutually
  exclusive in one call (the two are separate provider mechanisms). Each tool is
  a provider-neutral dict `{"name", "description", "input_schema"}`. There is no
  prompt-injected JSON schema and no salvage JSON parsing — agents validate the
  parsed result with Pydantic and fail loudly on mismatch.
- Provider calls return a normalized `ModelResponse` with `content`, `parsed`,
  `tool_calls`, `usage`, and the raw SDK response for later observability.
- `priceMTk` is optional per model member and stores USD rates per million
  tokens with `input` and `output` keys. When absent, observability still
  records token counts with zero calculated dollar cost.

## Dependencies
- External: `anthropic`, `openai`, `google-genai`, `pyyaml`, `pydantic`.
- Imported by: `auto_research.agents`, `auto_research.observability`.

## Dictionary-level reference
- `ProviderName` — literal provider label accepted in `config/models.yaml`.
- `MessageRole` — one of `"system"`, `"user"`, or `"assistant"`.
- `ModelMessage(role: MessageRole, content: str)` — provider-neutral text
  message. Unknown fields are forbidden.
- `ModelUsage(input_tokens: int = 0, output_tokens: int = 0)` — normalized token
  usage.
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
  raw: Any = None)` — normalized provider output. `parsed` carries native
  structured output; `tool_calls` carries native function calls.
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
  `schema` and `tools` are passed.
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
