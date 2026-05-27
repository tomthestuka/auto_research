# `config/` — runtime configuration

## Purpose
Centralizes all configuration that the running system reads at startup: model
selection (which provider/model is bound to which agent role), default parameters
per call tier, runtime data-dir path, and any other knob that should change without
a code edit.

Per [§3](../CLAUDE.md#3-coding-agent-contract-read-this-every-session) and
[§6](../CLAUDE.md#6-config-driven-model-registry) of the project `CLAUDE.md`, **no
model identifier or provider name appears in source code**. All such bindings live
here.

## Architecture
Each concern is a single YAML file in this directory. Code in
`auto_research.models` and downstream modules loads the YAML, validates it against
a Pydantic schema, and hands the resolved object to whoever needs it. Adding a new
role or swapping a provider is a YAML edit; no Python change.

## Interfaces / Contracts
- `models.yaml` — role → provider + model id + parameters mapping. Loaded and
  validated by `auto_research.models.registry` (built in step 2 of the build
  order). Required roles: `reviewer`, `analyst_lead`, `analyst_worker`,
  `librarian_io`, `librarian_searcher`. Reviewer and analyst-tier models **must**
  be vision-capable (see [§7](../CLAUDE.md#7-schemas--protocols)).
- *(future)* `runtime.yaml` — runtime data dir (default `~/.auto_research/`),
  iteration caps, sandbox limits. Schema landed when first consumer needs it.

## Dependencies
- Read by: `auto_research.models`, plus any downstream module that needs to
  resolve a role to a provider/model.
- Read at: process start. Hot-reload is **not** a Phase 1 feature.

## Dictionary-level reference
*(none — pure data files; consult the loader in `auto_research.models` for the
schema)*

## Changelog
- 2026-05-26 — Folder seeded as part of step 0 scaffolding. `models.yaml` to be
  added in step 2.