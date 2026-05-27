# `auto_research.models` — provider abstraction & registry

## Purpose
Resolves an agent role (e.g. `reviewer`) to a concrete LLM call, using
`config/models.yaml`. Hides the per-provider SDK differences behind a thin
common interface so any role can be backed by OpenAI, Anthropic, or Google by
editing YAML alone ([CLAUDE.md §6](../../../CLAUDE.md#6-config-driven-model-registry)).

## Architecture
- `registry.py` — load + validate `config/models.yaml`; expose
  `resolve(role) -> ModelHandle`.
- `providers/openai.py`, `providers/anthropic.py`, `providers/google.py` — thin
  adapters implementing the common interface (text + multimodal call, with
  cost/usage returned alongside the response).
- Cost and trace hooks call into `auto_research.observability` (step 3); the
  registry layer itself stays provider-agnostic.

## Interfaces / Contracts
*To be populated in step 2.* Public surface (planned):
- `class ModelHandle` — async `call(messages, *, schema=None, images=None) -> Response`.
- `resolve(role: str) -> ModelHandle` — config-driven role lookup.
- Reviewer- and analyst-tier handles must accept image content blocks.

## Dependencies
- External: `anthropic`, `openai`, `google-genai`, `pyyaml`, `pydantic`.
- Imported by: `auto_research.agents`, `auto_research.observability`.

## Dictionary-level reference
*(none yet.)*

## Changelog
- 2026-05-26 — Folder seeded during step 0.