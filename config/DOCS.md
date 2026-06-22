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
  [§7](../CLAUDE.md#7-schemas--protocols)). Phase-2 tool-authoring roles (optional, not
  in the required set): `coder` — the Sonnet Claude author (`merge_flow` resolves its
  model id here); `analyst_colead` — the tool reviewer, which **must stay non-Anthropic**
  because the coder is Claude (the §6/§9.3 provider-decorrelation invariant), backed by
  GPT-5.5 reasoning xhigh.
- `observations.yaml` — a single `obs_db_root`: the **one folder** the obs database lives
  in. Loaded by `auto_research.observations`, which discovers missions from disk — a subdir
  `<obs_db_root>/<MISSION>` is a mission iff it holds a `catalog.json` (the marker that
  separates real mission folders from unrelated working dirs sharing the root). The subdir
  basename lowercases to the mission name (`research_toolkit` derives it the same way); a
  single mission's path is resolved by `mission(name)` (case-insensitive subdir match, so it
  works before the catalog exists). Mission names are data, not special cases in code.
  `research_toolkit` tools (`xspec_utils.load_data`, `heasarc_utils.acquire`) already take a
  per-mission directory and are unchanged.
- `orchestration.yaml` — runtime data dir, graph iteration caps, debate round cap,
  ReAct/structured retry caps, toolkit root, analyst sandbox environment, report settings,
  Phase-2 authoring caps, the raise-to-human channel default + ping timeout, and MCP server
  configuration.
- `code_gate.yaml` — the no-fabrication scan lists for the Phase-2 deterministic gate
  (`sandbox/gate.py`, §9.4/§9.5): `hard_fail_vocabulary`, `hard_fail_code_patterns` (RNG),
  `finding_code_patterns` (literal arrays → review, not fail), `allowlist_obsid_tokens`.
  The single place a human tunes what the gate rejects vs. flags — no scan constant lives
  in source. Loaded by `gate._code_gate_config()`.

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
- `observations.yaml.obs_db_root` — the single root folder; each mission is a subdirectory
  `<obs_db_root>/<MISSION>` discovered from disk (marked by its `catalog.json`).
- `orchestration.yaml.runtime_dir` — writable runtime data directory outside both repos.
  Holds SQLite, run workspaces, figures/artifacts, report outputs, logs, and cost ledgers.
- `orchestration.yaml.max_iterations` — hard cap for each outer reviewer
  feedback edge.
- `orchestration.yaml.debate_max_rounds` — hard cap for analyst debate rounds
  before lead adjudication.
- `orchestration.yaml.io_search_max_iterations` — deferred hook for future
  librarian I/O search-refinement loops; current config keeps it at `1` because
  `evaluate_search_results()` is a stub.
- `orchestration.yaml.tool_retry_cap` — legacy accepted cap for old tool-execution
  call sites; the current worker ReAct loop self-corrects inside `react_loop_cap`.
- `orchestration.yaml.react_loop_cap` — general inner ReAct tool-call cap.
- `orchestration.yaml.librarian_react_loop_cap` — tighter cap for the literature loop,
  whose repeated searches re-send a growing paper context.
- `orchestration.yaml.structured_retry_cap` — bounded off-schema structured-output retry
  count.
- `orchestration.yaml.tool_author_max_turns` — Phase-2 cap on the Sonnet author's own
  self-correction loop (L0, Claude-SDK turns). Consumed by `merge_flow.propose_and_pr`.
- `orchestration.yaml.tool_author_gate_rework_cap` — how many times the author is
  re-driven after a deterministic-gate rejection (L2) before honest termination.
- `orchestration.yaml.tool_author_review_rework_cap` — how many times the author is
  re-driven after a co-lead review rejection (L3). All three are *pathology backstops*,
  not workflow budgets (like the ReAct caps).
- `orchestration.yaml.tool_author_branch` — the branch in `toolkit_root` the runtime commits
  authored tools onto directly (default `ai`; it never merges to master — §9.9). The launch
  preflight (`check_toolkit_branch_clean`) asserts `research_toolkit` is on this branch with a
  clean tree. Consumed by `graph.runtime` → `DebateToConverge` → `merge_flow.propose_and_pr`.
- `orchestration.yaml.tool_author_cap` — max tool-authoring attempts per analysis (default 3),
  bounding the acquire→reduce→analyse dependency chain; a pathology backstop, not a budget.
  Consumed by `DebateToConverge`.
- `orchestration.yaml.human_channel` — default raise-to-human connector for an unattended
  (library/non-TTY) run with no explicit channel: `cli|slack|whatsapp|none`. Default `none`
  (headless) so unattended runs and tests never block; an interactive CLI auto-detects the
  terminal regardless. Consumed by `hitl.resolve_handler` (§9.8).
- `orchestration.yaml.human_channel_ping_timeout_s` — how long the launch-time preflight
  channel ping waits for a reply before failing the run (green only when a reply arrives).
- `code_gate.yaml.hard_fail_vocabulary` — phrases that, in a comment/docstring/string
  literal of an added line, fail the gate (`synthetic`, `example data`, `placeholder`,
  `TODO`, …). Whole-word/phrase matched, case-insensitive.
- `code_gate.yaml.hard_fail_code_patterns` — dotted call/name prefixes that fail the gate:
  any RNG, seeded or not (`np.random`, `default_rng`, `.rvs`, …). §9.5 made absolute.
- `code_gate.yaml.finding_code_patterns` — AST shapes recorded as **non-failing** findings
  for the co-lead to judge (`literal_array_return`/`_plot`/`_dataframe`).
- `code_gate.yaml.allowlist_obsid_tokens` — regexes (default `synth\d{6}`) that exempt a
  line from the vocabulary scan, so §9.5 synthetic-ObsID fixtures stay legal.
- `orchestration.yaml.toolkit_root` — path to the sibling `research_toolkit`
  repository used by `ToolkitRegistry` and `PersistentSandbox`.
- `orchestration.yaml.analyze_ini_sh` — path to the dedicated headless analyst
  env script (`src/auto_research/sandbox/analyst_env.sh`). Sourced by
  `_source_ini_env()` in `sandbox/runner.py` before each persistent REPL subprocess
  is spawned. Sets HEADAS, CALDB, CALDBCONFIG, CALDBALIAS, GEOMAG_PATH. No SSH
  agent, no conda activation (the sandbox invokes `analyze_env/bin/python` directly).
- `orchestration.yaml.analyze_env` — conda/venv prefix whose `bin/python` is used
  for analyst sandbox subprocesses (must have PyXSPEC + analysis stack installed,
  including `astroquery` for `heasarc_utils` HEASARC downloads).
- `orchestration.yaml.mcp_servers` — optional stdio MCP server configs exposed
  alongside manifest tools. Empty or omitted means use only the built-in MCP
  server (`bash_exec`, `list_dir`, `read_file` at present).

## Changelog
- 2026-05-26 -> 2026-06-12 — Configuration now covers model groups/pricing,
  vision/document flags, mission observation roots, runtime data dir, ReAct/structured caps,
  disabled tools, report formats, and the Phase 2 `coder` + non-Anthropic
  `analyst_colead` roles with authoring rework caps.
- 2026-06-12 — Phase 2 step 3 added `code_gate.yaml`, the human-tunable no-fabrication
  gate config for hard-fail vocabulary/RNG patterns, review-only literal-array findings, and
  marked synthetic-ObsID allowlists; compacted changelog and documented `runtime_dir` plus
  current ReAct cap semantics.
- 2026-06-15 -> 2026-06-18 — Phase 2 steps 5 and 7 config: `orchestration.yaml` gained
  `human_channel` + `human_channel_ping_timeout_s` (raise-to-human default channel and
  launch-ping timeout) and `tool_author_branch` (default `ai`) + `tool_author_cap` (default 3)
  for runtime tool authoring/resume. `observations.yaml` was restructured to a single
  `obs_db_root` (the per-mission `obs_db_path` map is gone); `observations.py` now discovers
  missions from disk — a `<root>/<MISSION>` subdir is a mission iff it holds a `catalog.json`.
  Documented that `analyze_env` must include **`astroquery`** (for `heasarc_utils` acquire) and
  added `environment-analyst.yml` (the analyst sandbox conda env spec) created via
  `conda env create` per `README.md` §2b.
