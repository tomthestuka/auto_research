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
  `analyst_lead`, `analyst_worker`, and `librarian_io`. Each role maps to
  `members`, a non-empty list of model configs.
- Model identifiers and per-role parameters live in YAML, not Python source.
- `ModelHandle.call()` accepts text messages and optional `FigureRef` images.
  If images are passed to a role member whose config has `vision: false`, it
  raises `ValueError`.
- `ModelHandle.call()` also accepts optional `DocumentRef` attachments. If
  documents are passed to a role member whose config has `documents: false`, it
  raises `ValueError`.
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
- `ToolCall(name: str, arguments: dict[str, Any] = {})` — one provider-native
  tool/function call extracted from a model response.
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
  images=(), documents=()) -> ModelResponse`.
- `ModelHandle(role: str, config: RoleModelConfig, provider: ModelProvider)` —
  resolved callable handle for one role member. Method:
  `async call(messages, *, schema=None, tools=None, tool_choice="auto",
  images=(), documents=()) -> ModelResponse`. Raises `ValueError` if both
  `schema` and `tools` are passed.
- `ModelGroupHandle(role: str, handles: tuple[ModelHandle, ...])` — resolved
  model group. Method: `member(name: str) -> ModelHandle`.
- `REQUIRED_ROLES` — required role names that must appear in config.
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
- 2026-05-26/27 — Steps 2–3 built: `config/models.yaml`, validated role registry (four roles: `reviewer`, `analyst_lead`, `analyst_worker`, `librarian_io`), role-level model groups, OpenAI/Anthropic/Google adapters, `ModelHandle.call()` wired through observability, `priceMTk` per-million-token pricing, `vision`/`documents` capability flags for PDF-attaching librarian I/O members.
- 2026-06-09 — Native structured output + function calling (P0). The three provider adapters now implement the `schema=`/`tools=` contract instead of raising `NotImplementedError`: OpenAI uses Responses `text.format` json_schema for structured output and Responses `function` tools for function calling; Google uses `response_mime_type`+`response_schema` and `FunctionDeclaration`s; Anthropic uses a forced single synthetic tool for structured output and native `tools`+`tool_choice` for function calling. `ModelResponse` gained `parsed` and `tool_calls`; `ToolCall` added. `call()` accepts `tools`/`tool_choice`; `schema` and `tools` are mutually exclusive. This replaces the prompt-injected-JSON-schema + salvage-parser path that crashed on hand-serialized nested arguments (the `plot_fit`/`plot_iqu` `indices` truncation bug).
- 2026-06-09 — Provider schema sanitizers (follow-up). OpenAI/Google structured output accept only restricted JSON-Schema dialects, so the rich Pydantic schemas (`AnalystReport`/`ResearchReport`/… embedding `Path`/`HttpUrl`/`$ref`/`extra="forbid"`) are sanitized per provider before sending: OpenAI (`_strict_json_schema_format`) strips unsupported `format` (`path`/`uri`) and `default`, then strict-ifies (`additionalProperties:false`, all-`required`) and sends via `responses.create(text={"format":{"type":"json_schema",...}})` — replacing `responses.parse`, which 400'd on `format:path`. Google (`_to_gemini_schema`) inlines `$ref`/`$defs` and strips `additionalProperties`/`default`/`title`/unsupported `format`. Both sanitizers are **properties-aware** — they strip JSON-Schema keyword *annotations* but never a property whose field name happens to be `title`/`default`/`format` (regression: `ResearchReport.title`). Verified live: all six message schemas pass structured output on OpenAI and Google.
- 2026-06-11 — Anthropic structured output now uses the SDK-native `messages.parse(output_format=schema)` instead of the hand-rolled forced-single-tool workaround. The SDK's `transform_schema` strict-ifies the Pydantic model (drops unsupported string `format`s like `path`, folds numeric/length constraints into descriptions, inlines `$defs`, forces `additionalProperties:false`) and the API constrains decoding, then validates into an instance — returned as a dict (`.model_dump(mode="json")`) in `ModelResponse.parsed` for parity with the OpenAI/Google adapters. This fixes the `judge_convergence` crash where Opus, under the unenforced forced tool, nested the result as `{"parameter": {...}}` and failed Pydantic validation. The `tools=` (function-calling) path is unchanged (`messages.create`). No `AnthropicProvider`-specific schema sanitizer is needed — the SDK owns it.
