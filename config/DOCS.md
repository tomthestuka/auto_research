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
  `provider`, `model`, `vision`, `parameters`, and optional `documents` and
  `priceMTk`.
  Loaded and validated by `auto_research.models.registry`. Required roles:
  `reviewer`, `analyst_lead`, `analyst_worker`, and `librarian_io`. Reviewer and
  analyst-tier models **must** be marked `vision: true` so figure references can
  be passed at the multimodal boundary (see
  [§7](../CLAUDE.md#7-schemas--protocols)).
- `observations.yaml` — mission name → `obs_db_path`, a generic path to that
  mission's observation database or archive root. Loaded by
  `auto_research.observations`; mission names are data, not special cases in
  code.
- `orchestration.yaml` — graph iteration caps, debate round cap, tool retry cap,
  toolkit root, analyst sandbox environment, and MCP server configuration.

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
- `models.yaml.roles.<role>.members[].documents` — whether the member may
  receive document attachments. Librarian I/O members are configured with
  `documents: true` so each team member can ingest PDFs directly.
- `models.yaml.roles.<role>.members[].parameters` — provider-specific keyword
  arguments. OpenAI reasoning uses `reasoning.effort`; Gemini 3 thinking uses
  `thinking_config.thinking_level`.
- `models.yaml.roles.<role>.members[].priceMTk` — optional USD rates per
  million tokens with `input` and `output` keys, consumed by
  `auto_research.observability`.
- `observations.yaml.missions.<mission>.obs_db_path` — path to one mission's
  observation database or archive root.
- `orchestration.yaml.max_iterations` — hard cap for each outer reviewer
  feedback edge.
- `orchestration.yaml.debate_max_rounds` — hard cap for analyst debate rounds
  before lead adjudication.
- `orchestration.yaml.io_search_max_iterations` — Phase 2 hook for future
  librarian I/O search-refinement loops; Phase 1 keeps it at `1` because
  `evaluate_search_results()` is a stub.
- `orchestration.yaml.tool_retry_cap` — inner retry cap for
  `AnalystWorkerAgent.execute_tools()` error feedback.
- `orchestration.yaml.toolkit_root` — path to the sibling `research_toolkit`
  repository used by `ToolkitRegistry` and `PersistentSandbox`.
- `orchestration.yaml.analyze_ini_sh` — path to the dedicated headless analyst
  env script (`src/auto_research/sandbox/analyst_env.sh`). Sourced by
  `_source_ini_env()` in `sandbox/runner.py` before each persistent REPL subprocess
  is spawned. Sets HEADAS, CALDB, CALDBCONFIG, CALDBALIAS, GEOMAG_PATH. No SSH
  agent, no conda activation (the sandbox invokes `analyze_env/bin/python` directly).
- `orchestration.yaml.analyze_env` — conda/venv prefix whose `bin/python` is used
  for analyst sandbox subprocesses (must have PyXSPEC + analysis stack installed).
- `orchestration.yaml.mcp_servers` — optional stdio MCP server configs exposed
  alongside manifest tools. Empty or omitted means use only the built-in MCP
  server (`bash_exec`, `list_dir`, `read_file` at present).

## Changelog
- 2026-05-27 — Raised the Anthropic librarian I/O output cap so direct PDF
  ingestion can return complete memory abstractions.
- 2026-05-27 — Added `observations.yaml` for generic mission observation
  locations and documented the `documents` model capability flag used for
  librarian I/O PDF ingestion.
- 2026-05-27 — Added `priceMTk` unit prices for configured models using
  standard per-million-token public rates.
- 2026-05-27 — Documented optional per-model pricing rates consumed by the
  step-3 cost ledger.
- 2026-05-27 — Updated `models.yaml` to use role-level model groups for
  reviewers, analyst workers, librarian I/O, and the remaining role bindings.
- 2026-05-27 — Added `models.yaml` and documented the step-2 registry schema.
- 2026-05-26 — Folder seeded as part of step 0 scaffolding.
- 2026-06-01 — Synced config docs with the current four required model roles
  and documented the active `orchestration.yaml` keys, including persistent
  sandbox and MCP settings.
- 2026-06-01 — Corrected the built-in MCP description to match current code:
  the default server still exposes `bash_exec` in addition to `list_dir` and
  `read_file`; the read-only MCP restriction remains a separate P2 task.
