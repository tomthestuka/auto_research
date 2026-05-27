# `config/` — runtime configuration

## Purpose
Centralizes all configuration that the running system reads at startup: model
selection (which provider/model members are bound to each agent role group),
default parameters per call tier, runtime data-dir path, and any other knob that
should change without a code edit.

Per [§3](../CLAUDE.md#3-coding-agent-contract-read-this-every-session) and
[§6](../CLAUDE.md#6-config-driven-model-registry) of the project `CLAUDE.md`, **no
model identifier appears in source code**. Role-to-provider bindings, model
identifiers, role-group memberships, and per-call parameters live here.

## Architecture
Each concern is a single YAML file in this directory. Code in
`auto_research.models` and downstream modules loads the YAML, validates it against
a Pydantic schema, and hands the resolved object to whoever needs it. Adding a new
role or swapping a provider is a YAML edit; no Python change.

## Interfaces / Contracts
- `models.yaml` — role → non-empty `members` list. Each member has `name`,
  `provider`, `model`, `vision`, `parameters`, and optional
  `pricing_usd_per_million_tokens`. Loaded and validated by
  `auto_research.models.registry`. Required roles: `reviewer`, `analyst_lead`,
  `analyst_worker`, `librarian_io`, `librarian_searcher`. Reviewer and
  analyst-tier models **must** be marked `vision: true` so figure references can
  be passed at the multimodal boundary (see
  [§7](../CLAUDE.md#7-schemas--protocols)).
- *(future)* `runtime.yaml` — runtime data dir (default `~/.auto_research/`),
  iteration caps, sandbox limits. Schema landed when first consumer needs it.

## Dependencies
- Read by: `auto_research.models`, plus any downstream module that needs to
  resolve a role to a provider/model.
- Read at: process start. Hot-reload is **not** a Phase 1 feature.

## Dictionary-level reference
- `models.yaml.roles.<role>.members` — non-empty model group.
- `models.yaml.roles.<role>.members[].name` — stable member name within the
  group.
- `models.yaml.roles.<role>.members[].provider` — provider adapter label.
- `models.yaml.roles.<role>.members[].model` — provider model identifier.
- `models.yaml.roles.<role>.members[].vision` — whether the member may receive
  figures.
- `models.yaml.roles.<role>.members[].parameters` — provider-specific keyword
  arguments. OpenAI reasoning uses `reasoning.effort`; Gemini 3 thinking uses
  `thinking_config.thinking_level`.
- `models.yaml.roles.<role>.members[].pricing_usd_per_million_tokens` —
  optional rates with `input` and `output` keys, consumed by
  `auto_research.observability`.

## Changelog
- 2026-05-27 — Documented optional per-model pricing rates consumed by the
  step-3 cost ledger.
- 2026-05-27 — Updated `models.yaml` to use role-level model groups for
  reviewers, analyst workers, librarian I/O, and the remaining role bindings.
- 2026-05-27 — Added `models.yaml` and documented the step-2 registry schema.
- 2026-05-26 — Folder seeded as part of step 0 scaffolding.
