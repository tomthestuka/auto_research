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

**Machine-local config vs. tracked templates.** `models.yaml` and `code_gate.yaml` are
machine-independent and tracked. The machine-specific files — `orchestration.yaml`,
`observations.yaml`, plus the sandbox `analyst_env.sh` and `repl_init.py` hooks — are
**gitignored**; the repo tracks a `*.example` template for each. A fresh clone runs
`bash install.sh` (writes the live copies) or copies each `*.example` by hand. The launch
config loaders (`graph.runtime.load_orchestration_config`, `observations.load_observations_config`)
raise a "copy the `.example` / run install.sh" message when a live file is absent.

## Interfaces / Contracts
- `models.yaml` — role → non-empty `members` list. Each member has `name`,
  `provider`, `model`, `vision`, `parameters`, and optional `documents` and
  `priceMTk`.
  Loaded and validated by `auto_research.models.registry`. Required roles:
  `reviewer`, `analyst_lead`, `analyst_worker`, `librarian_io`, `coder`, `analyst_colead`,
  and `status_monitor` — the full `REQUIRED_ROLES` set, validated at launch. Reviewer and
  analyst-tier models **must** be marked `vision: true` so figure references can
  be passed at the multimodal boundary (see
  [§7](../CLAUDE.md#7-schemas--protocols)). Phase-2 tool-authoring roles (optional, not
  in the required set): `coder` — the tool author, which names **both a model and the harness
  that drives it** (`harness: claude_sdk | codex`, plus `base_url`/`key_env` when the vendor is
  not that harness's native one; legal pairings and their documented endpoints are in
  `sandbox/coder/harness.py`, checked at launch by `coder_harness_supported` /
  `coder_harness_available`). Exactly one member: one harness drives one model.
  `analyst_colead` — the tool reviewer, which **must stay decorrelated from the author's
  provider** (the §6/§9.3 invariant, non-Anthropic while the author is Claude), backed by
  GPT-5.5 reasoning xhigh.
- `observations.yaml` — a single `obs_db_root`: the **one folder** the obs database lives
  in. Machine-local (gitignored; tracked as `observations.yaml.example`).
  Loaded by `auto_research.observations`, which discovers missions from disk — a subdir
  `<obs_db_root>/<MISSION>` is a mission iff it holds a `catalog.json` (the marker that
  separates real mission folders from unrelated working dirs sharing the root). The subdir
  basename lowercases to the mission name (`research_toolkit` derives it the same way); a
  single mission's path is resolved by `mission(name)` (case-insensitive subdir match, so it
  works before the catalog exists). Mission names are data, not special cases in code.
  `research_toolkit` tools (`xspec_utils.load_data`, `heasarc_utils.acquire`) already take a
  per-mission directory and are unchanged.
- `orchestration.yaml` — runtime data dir, graph iteration caps, debate round cap,
  ReAct/structured retry caps, toolkit root, analyst sandbox environment (env script +
  REPL-init hook + env probe), report settings, Phase-2 authoring caps, the raise-to-human
  channel default + ping timeout, and MCP server configuration. Machine-local (gitignored;
  tracked as `orchestration.yaml.example`).
- `code_gate.yaml` — the no-fabrication scan lists for the Phase-2 deterministic gate
  (`sandbox/gate.py`, §9.4/§9.5): `hard_fail_vocabulary`, `hard_fail_code_patterns` (RNG),
  `finding_vocabulary` + `finding_code_patterns` (soft flags → co-lead review, not fail),
  `allowlist_obsid_tokens`.
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
- `orchestration.yaml.react_loop_cap` — general inner ReAct cap; bounds the
  budget-*consuming* iterations (those calling a non-free tool).
- `orchestration.yaml.react_loop_absolute_cap` — hard ceiling on *total* ReAct iterations
  incl. budget-free ones (the lead's §5.5 `plan_and_debate` design rounds don't count toward
  `react_loop_cap`, only toward this runaway backstop). Defaults to 60.
- `orchestration.yaml.librarian_react_loop_cap` — tighter cap for the literature loop,
  whose repeated searches re-send a growing paper context.
- `orchestration.yaml.status_react_loop_cap` — cap for the `/status` reporter's ledger
  investigation. Defaults to 6, deliberately tight: a human is waiting on a phone, and the
  ledger it reads runs to thousands of events.
- `orchestration.yaml.structured_retry_cap` — bounded off-schema structured-output retry
  count.
- `orchestration.yaml.tool_search_enabled` — provider-native **tool search / lazy tool loading**
  (shipped **true** since 2026-07-28; the *code* default stays **false**, so a config lacking the
  key keeps every tool resident, byte-identical to before; read via `env.tool_search_config`).
  When true, every `research_toolkit` tool is sent
  with `defer_loading: true` and its schema stays out of the model's context until the model
  discovers it through the provider's server-side search tool; harness tools stay resident.
  Two protocol strings — `tool_search_anthropic_tool` (default
  `tool_search_tool_bm25_20251119`) and `tool_search_openai_tool` (default `tool_search`) —
  carry dated provider identifiers and are overridable without a code change; they default in
  code and stay out of the sample config to keep it clean. The Anthropic entry is the **dated
  type** only — the adapter derives the undated `name` the API requires beside it. **Live-verified
  2026-07-28 on both providers** (`tests/test_live_tool_search.py`).
- `orchestration.yaml.context_mgmt_*` — Anthropic provider-native **context editing** (the
  context-window pressure valve for long ReAct loops; read via `env.context_editing_config`).
  `context_mgmt_enabled` (shipped **true** since 2026-07-28; the *code* default stays **false**, so
  a config lacking the key keeps the non-beta path)
  routes the Anthropic adapter's `cache_prefix` calls through `client.beta.messages` with a
  `clear_tool_uses_20250919` `context_management` edit; `context_mgmt_trigger_tokens` (default
  120000) is the input-token watermark at which the oldest tool results are cleared server-side;
  `context_mgmt_clear_at_least_tokens` (default 20000) the minimum reclaimed per pass (amortizes
  the cache-prefix invalidation); `context_mgmt_keep_tool_uses` (default 3) how many most-recent
  tool results stay verbatim. Two protocol strings — `context_mgmt_beta_header`
  (`context-management-2025-06-27`) and `context_mgmt_strategy` (`clear_tool_uses_20250919`) —
  default in code and are overridable here when Anthropic bumps the dated beta identifiers (kept
  out of the sample config to stay clean). **Live-verified 2026-07-28**
  (`tests/test_live_context_editing.py`). Anthropic-only.
- `orchestration.yaml.sandbox_call_timeout_s` — global default sandbox per-tool-call timeout
  (seconds, default 300): a tool call is SIGTERM-killed (exit 124) at this many seconds unless its
  package `<domain>_utils/tool_config.yaml` raises it per tool (`call_timeout_s`), which takes
  precedence. No upper clamp on the per-tool override (trusted infra). Read by
  `ToolkitRegistry.call_timeout_ceiling` via `env.sandbox_call_timeout_default`.
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
- `code_gate.yaml.hard_fail_vocabulary` — explicit fabrication phrases that, in a
  comment/docstring/string literal of an added line, fail the gate (`example data`,
  `fake data`, `in operational mode this would`, `placeholder`, `TODO`, …). Whole-word/phrase
  matched, case-insensitive.
- `code_gate.yaml.hard_fail_code_patterns` — dotted call/name prefixes that fail the gate:
  any RNG, seeded or not (`np.random`, `default_rng`, `.rvs`, …). §9.5 made absolute.
- `code_gate.yaml.finding_vocabulary` — **non-failing** soft flags for the co-lead to judge:
  incompleteness markers (`stub`, `not implemented`) + bare ambiguous descriptors that recur
  in honest §9.5 disclosures (`synthetic`, `synthesized`, `illustrative`) (C1).
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
- `orchestration.yaml.repl_init_py` — path to a Python file exec'd once at each analyst REPL
  subprocess boot (`sandbox/persistent.py`), inside `analyze_env`, for domain **session** setup
  (imports, non-interactive settings, model/table loads). Best-effort — a failure is swallowed
  so a missing optional model never kills the REPL. Empty/absent ⇒ bare REPL (no-op). Template:
  `src/auto_research/sandbox/repl_init.py.example`. Replaces the formerly-hardcoded XSPEC/relxill
  boot, so the sandbox core carries no domain code (§9.2/S1).
- `orchestration.yaml.analyze_probe` — a one-line Python snippet run under `analyze_env` to
  smoke-test the analysis stack (e.g. `import xspec, xspec_utils`). Used by the installer and the
  preflight env-probe check, and it supplies the model-facing probe text in the `bash_exec`
  description. Empty/absent ⇒ probe skipped.
- `orchestration.yaml.analyze_env` — conda/venv prefix whose `bin/python` is used
  for analyst sandbox subprocesses (must have PyXSPEC + analysis stack installed,
  including `astroquery` for `heasarc_utils` HEASARC downloads).
- `orchestration.yaml.mcp_servers` — optional stdio MCP server configs exposed
  alongside manifest tools. Empty or omitted means use only the built-in MCP
  server (`bash_exec`, `list_dir`, `read_file` at present).

## Published token rates for the harness vendors

`priceMTk` on a member is the **fallback** used when litellm's maintained map does not know a
model (`observability/costs.token_rates_for` is litellm-first). It takes `input` and `output` in
USD per million tokens, plus optional `cache_read` / `cache_write` (defaulted to 0.1x and 1.25x
of input). Omitting it for an unknown model is not neutral: the run books real usage against a
zero rate and the cost column reads `$0.00` with nothing to show it lied. Observed 2026-08-19 —
a Z.AI leg booked 40 calls at $0.00.

litellm knows **Anthropic, OpenAI and DeepSeek**, so members for those need no `priceMTk`, and
adding one would shadow a maintained rate with a hand-copied one. The two that need it:

| model | input | output | cache read | source |
|---|---|---|---|---|
| `glm-5.3` | 1.40 | 4.40 | 0.26 | docs.z.ai/guides/overview/pricing, read 2026-08-19 |
| `glm-5.2` | 1.40 | 4.40 | 0.26 | same |
| `glm-5-turbo` | 1.20 | 4.00 | 0.24 | same |
| `qwen3.7-plus` | 0.32 | 1.28 | — | aggregators, **not vendor-confirmed** |
| `qwen3.6-flash` | 0.19 | 1.13 | — | aggregators, **not vendor-confirmed** |

Only the Qwen Plus and Flash tiers are here: litellm carries real rates for `qwen3.8-max`
(2.00/6.00) and `qwen3.7-max` (2.50/7.50), and a hand-copied rate that shadows a maintained one is
a liability rather than a safety net. The two rows above carry a caveat of their own — QwenCloud's
pricing page renders its table by script, so the vendor's own numbers could not be read directly.
Confirm them in the console before quoting a Qwen cost anywhere it matters, and note that a plan
discount would appear there too.

**A known id with no rates is the trap to watch.** litellm *knows* `qwen3.7-plus` and carries no
prices for it, which used to be worse than an unknown model: the resolver accepted the entry, priced
every token at zero, and reported `source="litellm"` while the member's own `priceMTk` could never
engage. Since 2026-08-19 a rate-less entry is treated as absent so the fallback works. A model that
is genuinely free (Z.AI's Flash tiers are) says so explicitly with `priceMTk: {input: 0, output: 0}`,
which at least labels the zero as ours.

Two rate structures no static table can express, worth knowing before a cost column is trusted:
DeepSeek moved to **peak / off-peak** multipliers (effective 2026-08-16), so a recorded DeepSeek
cost is a lower bound during peak hours; and Model Studio applies a 50 per cent batch discount and
a context-caching discount that cannot be combined.

The same numbers are kept in `tests/test_live_coder_matrix.py: _PRICES` for the throwaway members
that file constructs, since a live leg needs them without being configured here.

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
- 2026-06-22 — Added `sandbox_call_timeout_s` (default 300): the global sandbox per-tool-call
  timeout. A live run showed every tool call is SIGTERM-killed at ~300s (exit 124) regardless of
  the tool's internal timeout, so a slow HEASARC download never completed. A package's
  `<domain>_utils/tool_config.yaml` (`call_timeout_s` per tool) overrides this, no upper clamp;
  read by `ToolkitRegistry.call_timeout_ceiling` (see `sandbox/DOCS.md`).
- 2026-06-24 — Added `orchestration.yaml.react_loop_absolute_cap` (default 60, fix-list #5.5):
  the hard ceiling on total ReAct iterations. `react_loop_cap` (30) now bounds only the
  budget-consuming iterations; the analyst lead's budget-free `plan_and_debate` design rounds are
  uncounted against it and bounded only by this backstop.
- 2026-07-06 — **C1: bare ambiguous descriptors moved from hard-fail to soft finding.**
  `code_gate.yaml` reclassifies `synthetic`/`synthesized`/`synthesised`/`illustrative` from
  `hard_fail_vocabulary` to `finding_vocabulary` — they recur in honest §9.5 disclosures
  ("synthetic fixture", docstring "illustrative example") and taxed every authoring run a
  rework round. Explicit fabrication phrases (`fake data`, `mock data`, `in operational mode
  this would`, …) + all RNG patterns stay hard-fail; the co-lead judges the soft findings
  (§9.4 two-pass). Config-only; `tests/test_gate.py` updated.
- 2026-07-08 — **Reviewer group's Anthropic member swapped Opus 4.8 → Fable 5.** The reviewer
  runs `_select_model`'s `handles[0]`, so its first member is now `claude-fable-5` (vision-capable,
  §6) — a reasoning reviewer for adjudication and report authoring, where Opus 4.8 ran with
  thinking off (the adapter sends no `thinking` field, and Opus treats that as disabled).
  `priceMTk` → 10/50; `max_tokens` 16384 → 21000 because Fable's thinking is always-on and shares
  the output budget — 21333 is the SDK non-streaming ceiling (`3600·max_tokens/128000 > 600`
  raises; a streaming report call would lift it). Requires the org to allow ≥30-day retention
  (Fable 400s under zero-data-retention). GPT-5.5 and Gemini members unchanged; `analyst_colead`
  stays non-Anthropic (§9.3). `tests/test_models.py` + README updated.
- 2026-07-08 — **Adaptive thinking enabled on all Anthropic chat members.** Added
  `thinking: {type: adaptive}` to the `parameters` of every Anthropic member the chat adapter
  drives: reviewer (Fable, already always-on), analyst_lead (Opus), analyst_worker (Sonnet),
  librarian_io (Sonnet). They previously ran thinking-OFF — the adapter sends no `thinking` field
  and Opus/Sonnet treat omission as disabled, so only the GPT (`reasoning.effort`) and Gemini
  (`thinking_config`) members reasoned. `max_tokens` raised for headroom (thinking is billed and
  shares the output budget even at `display:omitted`): lead/worker 16384 → 21000, librarian
  8192 → 12000 (reviewer already 21000); all ≤ the 21333 non-streaming ceiling. Live-verified that
  adaptive thinking is compatible with the adapter's tool-loop reconstruction (no thinking-block
  preservation needed), forced `tool_choice`, and `messages.parse` structured output. **coder
  excluded**: it runs the Claude Agent SDK (`merge_flow` reads only its model id; `parameters`
  ignored) and already reasons at the SDK default. `effort` deliberately not set —
  `output_config.effort` collides with the `.parse()` structured-output path; `thinking` alone is
  safe on both paths.
- 2026-07-14 — **Install-script increment 0: machine-local config as gitignored + tracked
  `*.example`.** `orchestration.yaml`, `observations.yaml`, `sandbox/analyst_env.sh`, and the new
  `sandbox/repl_init.py` are now gitignored; each ships a tracked `*.example` template. Added two
  `orchestration.yaml` keys: `repl_init_py` (path to a Python file exec'd at REPL-subprocess boot
  for domain session setup — replaces the hardcoded XSPEC/relxill boot in `persistent.py`, closing
  the deepest domain leak S1) and `analyze_probe` (a one-line stack smoke-test used by the
  installer, preflight, and the `bash_exec` description). Launch config loaders now raise a
  "copy the `.example` / run install.sh" message on a missing live file. Migration for an existing
  checkout: `git rm --cached config/orchestration.yaml config/observations.yaml
  src/auto_research/sandbox/analyst_env.sh` (the live files stay on disk).
- 2026-07-22 — **Context-management keys (`context_mgmt_*`) for Anthropic provider-native context
  editing.** Added `context_mgmt_enabled` (default false), `context_mgmt_trigger_tokens` (120000),
  `context_mgmt_clear_at_least_tokens` (20000), `context_mgmt_keep_tool_uses` (3) to
  `orchestration.yaml` + `.example`; read via `env.context_editing_config`. Enables the long-loop
  context-pressure valve (clear oldest tool results server-side once a request crosses the token
  watermark — see `models/DOCS.md`). Ships **disabled** because the beta path is **NOT LIVE TESTED
  as of 2026-07-22** (blocked by an Anthropic credit-balance 400); run `tests/test_live_context_editing.py`
  and flip `context_mgmt_enabled: true` once credits return. Protocol strings `context_mgmt_beta_header`/
  `context_mgmt_strategy` default in code, overridable, kept out of the sample config.

- 2026-07-27 — Added `tool_search_enabled` (default **false**) for provider-native tool search /
  lazy tool loading, plus the overridable protocol strings `tool_search_anthropic_tool` /
  `tool_search_openai_tool` (defaulted in code, kept out of the sample config). Every
  `research_toolkit` tool defers; harness tools stay resident. Measured on `gpt-5.5` with the
  real 25-tool manifest: 19,004 → 2,382 resident tokens. **NOT LIVE TESTED** — run
  `tests/test_live_tool_search.py` before flipping it true.
- 2026-07-28 — **`context_mgmt_enabled` and `tool_search_enabled` flipped `true`** in both `orchestration.yaml` and the tracked `orchestration.yaml.example`, after each feature's env-gated live test passed on 2026-07-28 (the Anthropic key that blocked them since 2026-07-27 was reinstated). Context editing passed unchanged; tool search needed a one-value fix to the Anthropic search-tool `name` first (see `models/DOCS.md`). The two protocol-string keys (`tool_search_anthropic_tool` / `tool_search_openai_tool`) still default in code and stay out of the sample config — note that `tool_search_anthropic_tool` carries the **dated** type and the adapter derives the undated `name` from it, so overriding it needs only the dated string.

- 2026-08-19 — **Published token rates written down for the harness vendors.** litellm prices
  Anthropic, OpenAI and DeepSeek; it does not know `glm-*` or `qwen*`, so a member for either books
  real usage at a zero rate and reports `$0.00` (observed: a Z.AI leg, 40 calls, $0.00). The rates
  now live in a table above with their source and date — Z.AI's from their own pricing page, Qwen's
  from aggregators because the vendor's page renders by script, labelled as unconfirmed. Also
  recorded: DeepSeek's new peak/off-peak multipliers and Model Studio's batch/caching discounts,
  neither of which a static per-model rate can express.
