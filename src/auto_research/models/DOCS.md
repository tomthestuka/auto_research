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
  `analyst_lead`, `analyst_worker`, `librarian_io`, and
  `librarian_searcher`. Each role maps to `members`, a non-empty list of model
  configs.
- Model identifiers and per-role parameters live in YAML, not Python source.
- `ModelHandle.call()` accepts text messages and optional `FigureRef` images.
  If images are passed to a role member whose config has `vision: false`, it
  raises `ValueError`.
- `ModelRegistry.resolve(role)` returns a `ModelGroupHandle`; callers choose a
  member with `member(name)` or iterate through `handles`.
- `schema` is accepted in the call signature as the structured-output extension
  point; provider-specific structured output is wired when agents need it.
- Provider calls return a normalized `ModelResponse` with `content`, `usage`,
  and the raw SDK response for later observability.
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
- `ModelResponse(content: str, usage: ModelUsage = ModelUsage(), raw: Any =
  None)` — normalized provider output.
- `RoleModelConfig(name: str, provider: ProviderName, model: str,
  vision: bool = False, priceMTk: dict[str, float] = {},
  parameters: dict[str, Any] = {})` — one model member from YAML.
- `RoleModelGroup(members: list[RoleModelConfig])` — non-empty model group for
  one role.
- `ModelsConfig(roles: dict[str, RoleModelGroup])` — validated
  `config/models.yaml` shape.
- `ModelProvider` — async protocol implemented by provider adapters:
  `call(messages, config, *, schema=None, images=()) -> ModelResponse`.
- `ModelHandle(role: str, config: RoleModelConfig, provider: ModelProvider)` —
  resolved callable handle for one role member. Method:
  `async call(messages, *, schema=None, images=()) -> ModelResponse`.
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
  `ModelResponse`.

## Changelog
- 2026-05-27 — Replaced `pricing_usd_per_million_tokens` with the shorter
  config field `priceMTk`.
- 2026-05-27 — Wired `ModelHandle.call()` through step-3 observability so every
  provider call can be traced and cost-logged.
- 2026-05-27 — Changed the registry contract from one model per role to
  role-level model groups and added tests for the requested reviewer, analyst
  worker, and librarian I/O memberships.
- 2026-05-27 — Implemented step 2: `config/models.yaml`, validated role registry,
  provider-neutral call types, OpenAI/Anthropic/Google adapters, and
  deterministic registry tests.
- 2026-05-26 — Folder seeded during step 0.
