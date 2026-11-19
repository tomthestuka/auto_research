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
machine-independent and tracked. The machine-specific file — `orchestration.yaml` — is **gitignored**; the repo tracks a `*.example`
template for it. A fresh clone runs
`bash install.sh` (writes the live copies) or copies each `*.example` by hand. The launch
config loader (`graph.runtime.load_orchestration_config`) raises a "copy the `.example` / run install.sh" message when a live file is absent.

**This directory is the default, not the only one.** Every reader resolves the four files through
`auto_research.config_paths`, which answers with this directory unless `AUTO_RESEARCH_CONFIG_DIR`
names another. That exists so a process can be run against a whole different configuration — a
different set of model seats, a different toolkit checkout, a different runtime and data
root — without editing anything here, which is what a repeated-trial experiment over the seats
needs. It is an environment variable rather than a parameter because the readers include
subprocesses (the sandbox REPL, the author harness, the gate's test runner) that inherit the
environment and are not all Python.

Two things stay pinned to the repository whatever a run is configured with: the `*.example`
templates, since the installer writes a live configuration *from* them, and `.env`, since an API
key is a property of the machine rather than of the experiment. When overriding, supply all four
files: a directory missing one is read as that file being absent, which the loaders report as an
uninstalled configuration rather than silently falling back here.

## Interfaces / Contracts
- `models.yaml` — role → non-empty `members` list. Each member has `name`,
  `provider`, `model`, `parameters`, and optional `priceMTk`. What a member may be sent (images,
  documents) is its model's entry in `model_capabilities.yaml` (`models.providers.media_capability`,
  Z1d), never a key here; a retired `vision`/`documents` key is refused at load.
  Loaded and validated by `auto_research.models.registry`. Required roles:
  `reviewer`, `analyst_lead`, `analyst_worker`, `librarian_io`, `coder`, `analyst_colead`,
  and `status_monitor` — the full `REQUIRED_ROLES` set, validated at launch. Reviewer and
  analyst-tier members **must** be models whose capability entry takes images so figure references
  can be passed at the multimodal boundary (see
  [§7](../CLAUDE.md#7-schemas--protocols)). Phase-2 tool-authoring roles (optional, not
  in the required set): `coder` — the tool author, which names **both a model and the harness
  that drives it** (`harness: claude_sdk | codex`, plus `base_url`/`key_env` when the vendor is
  not that harness's native one; legal pairings and their documented endpoints are in
  `sandbox/coder/harness.py`, checked at launch by `coder_harness_supported` /
  `coder_harness_available`). Exactly one member: one harness drives one model.
  `analyst_colead` — the tool reviewer, which **must stay decorrelated from the author's
  provider** (the §6/§9.3 invariant, non-Anthropic while the author is Claude), backed by
  GPT-5.5 reasoning xhigh.
- `orchestration.yaml` — runtime data dir, graph iteration caps, debate round cap,
  ReAct/structured retry caps, toolkit root, analyst sandbox environment (env script +
  REPL-init hook + env probe), report settings, Phase-2 authoring caps, the raise-to-human
  channel default + ping timeout, and MCP server configuration. Machine-local (gitignored;
  tracked as `orchestration.yaml.example`).
- `code_gate.yaml` — the no-fabrication scan lists for the Phase-2 deterministic gate
  (`sandbox/gate.py`, §9.4/§9.5): `hard_fail_vocabulary`, `hard_fail_code_patterns` (RNG),
  `finding_vocabulary` + `finding_code_patterns` (soft flags → co-lead review, not fail).
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
- `models.yaml.roles.<role>.members[].provider` — provider adapter label: `anthropic`,
  `google`, `openai`, `deepseek`, `zai`, or `dashscope`. The last three are served over the
  OpenAI Responses dialect and are refused any role that emits a structured schema, because
  two of them accept the parameter and ignore it (see `models/DOCS.md`).
- `models.yaml.roles.<role>.members[].base_url` — optional endpoint override, for a member on
  a plan-specific host. Defaults to the vendor's documented endpoint.
- `models.yaml.roles.<role>.members[].key_env` — optional API-key variable, when the member
  does not authenticate from that provider's standard `<PROVIDER>_API_KEY`.
- `models.yaml.roles.<role>.members[].model` — provider model identifier.
- `models.yaml.roles.<role>.members[].parameters` — provider-specific keyword
  arguments. OpenAI reasoning uses `reasoning.effort`; Gemini 3 thinking uses
  `thinking_config.thinking_level`.
- `models.yaml.roles.<role>.members[].context_tokens` — optional override of the member's input
  limit in tokens. Unset, the limit is litellm's `max_input_tokens` for the model
  (`observability.costs.context_window_for`, the map prices come from; 2026-10-18 it matches the
  provider pages: Claude Fable/Opus/Sonnet 5 1,000,000, GPT-5.6 Sol/Terra/Luna 922,000, GPT-5.5
  1,050,000, Gemini 3.5 Flash 1,048,576). Set it where the usable limit is lower (a vendor that
  truncates silently). The limit sets the window-dependent media ceilings and the compaction
  trigger.
- `models.yaml.roles.session_compactor` — the model that condenses a long turn's middle for members
  whose provider has no server-side compaction (B7; `gpt-5.6-luna`). Optional: without it such a
  turn is not condensed and a `*.compaction_failed` event says so.
- `orchestration.yaml.compaction_enabled` (a boolean, code default false, configured true) and
  `compaction_trigger_fraction` (default 0.9) — a turn is condensed once its prompt reaches that
  fraction of its member's input limit; `compaction_server_providers` (default `[anthropic]`)
  compact on the provider's side (`compact_20260112`), every other provider by
  `session_compactor` (`agents/compaction.py`).
- `models.yaml.roles.<role>.members[].priceMTk` — optional USD rates per
  million tokens with `input` and `output` keys, consumed by
  `auto_research.observability`.
- `orchestration.yaml.env_externals` — software outside conda the analysis environment uses,
  `{name: {root, stamps, private}}`: `stamps` are files under `root` hashed into every receipt's
  environment fingerprint; `private: {VARIABLE: separator}` (optional, D19) names a variable whose
  value is `<folders><separator><kept>` and whose first part every process and every call gets as a
  fresh folder of its own (`sandbox.runner.private_folders`), the rest kept. Configured:
  `heasoft: {..., private: {PFILES: ";"}}` — HEASoft tools write learned parameters into the user half
  of `PFILES`, so a shared folder let one call's values reach the next (F23, G4). **An undeclared
  variable gets no isolation**: every process and call then shares the folder the activation names.
  Preflight (`private_variables`) refuses a malformed entry, a reserved name (`AR_PRIVATE_FOLDERS`,
  `PWD`, `MARK`, `HOME`, `PATH`, `TMPDIR`), a variable declared twice (also up to case), and a
  declared variable the toolkit's default environment's activation leaves unset or sets without the
  separator. The
  `.example` ships the `heasoft` line commented.
- `orchestration.yaml.runtime_dir` — writable runtime data directory outside both repos.
  Holds SQLite, run workspaces, figures/artifacts, report outputs, logs, and cost ledgers.
- `orchestration.yaml.max_iterations` — hard cap for each outer reviewer
  feedback edge.
- `orchestration.yaml.debate_max_rounds` — hard cap for analyst debate rounds
  before lead adjudication.
- `orchestration.yaml.tool_retry_cap` — legacy accepted cap for old tool-execution
  call sites; the current worker ReAct loop self-corrects inside `react_loop_cap`.
- `orchestration.yaml.react_loop_cap` — general inner ReAct cap; bounds the
  budget-*consuming* iterations (those calling a non-free tool).
- `orchestration.yaml.react_loop_absolute_cap` — hard ceiling on *total* ReAct iterations
  incl. budget-free ones (the lead's §5.5 `plan_and_debate` design rounds don't count toward
  `react_loop_cap`, only toward this runaway backstop). Defaults to 60.
- `orchestration.yaml.librarian_react_loop_cap` — tool budget (model turns) for the librarian's
  research loop; config 40. Reading is paged (24k characters per page), so a paper read in full
  costs several turns; report authoring and `turn_end` are budget-free.
- `orchestration.yaml.status_react_loop_cap` — cap for the `/status` reporter's ledger
  investigation. Defaults to 6, deliberately tight: a human is waiting on a phone, and the
  ledger it reads runs to thousands of events.
- `orchestration.yaml.structured_retry_cap` — bounded off-schema structured-output retry
  count.
- `orchestration.yaml.coder_wait_floor_s` / `coder_wait_default_s` / `coder_wait_max_s` — the
  analyst lead's blocking `wait_coder` (defaults 60 / 300 / 1800 s, read per call via
  `env.coder_wait_limits_s`, kept ordered floor ≤ default ≤ max). A requested timeout below the
  floor is raised to it and one above the max lowered to it, never refused; the max also bounds how
  long `submit_for_review` waits for a working coder before the gate.
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
- `orchestration.yaml.context_mgmt_*` — Anthropic provider-native **context editing**, shipped
  **off** since ruling B12 (2026-10-18: the B5 probe measured every clear rewriting the whole prompt
  cache; server-side compaction is the in-turn bound instead); the settings stay for re-enabling (the
  context-window pressure valve for long ReAct loops; read via `env.context_editing_config`).
  `context_mgmt_enabled` (shipped true 2026-07-28 → 2026-10-18, **false** since; the *code*
  default is **false**, so a config lacking the key keeps the non-beta path)
  routes the Anthropic adapter's `cache_prefix` calls through `client.beta.messages` with a
  `clear_tool_uses_20250919` `context_management` edit; `context_mgmt_trigger_tokens` (default
  120000) is the input-token watermark at which the oldest tool results are cleared server-side;
  `context_mgmt_clear_at_least_tokens` (code default 20000, configured 60000 since B5) the minimum
  reclaimed per pass (amortizes the cache-prefix invalidation); `context_mgmt_keep_tool_uses`
  (default 3) how many most-recent tool results stay verbatim; `context_mgmt_clear_tool_inputs`
  (a boolean, code default false, configured true) clears the calls' arguments with their results
  — the agent then finds a cleared call's output again only through its receipts
  (`view_receipt`); `context_mgmt_exclude_tools` (a list of names, default
  `[message, wait_for_reply]`, `env.CONTEXT_EDITING_EXCLUDED_TOOLS`, ruling B11: channel texts
  mint no receipt and cannot be read again; report tools, `view_receipt` and every receipt-backed
  result may be cleared) are never cleared, and the adapter adds the tool-search tool whenever a
  request defers tools. Two protocol strings — `context_mgmt_beta_header`
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
- `orchestration.yaml.tool_author_cap` — max tool-authoring attempts per analysis (default 3),
  bounding the acquire→reduce→analyse dependency chain; a pathology backstop, not a budget.
  Consumed by `DebateToConverge`.
- `orchestration.yaml.human_channel` — default raise-to-human connector for an unattended
  (library/non-TTY) run with no explicit channel: `cli|slack|whatsapp|none`. Default `none`
  (headless) so unattended runs and tests never block; an interactive CLI auto-detects the
  terminal regardless. Consumed by `hitl.resolve_handler` (§9.8).
- `orchestration.yaml.telegram_rich_messages` (default `true`) — send Telegram questions as Bot API
  rich messages (native tables/math); `false` keeps the HTML-subset rendering.
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
- `orchestration.yaml.toolkits` — the toolkit contract (`auto_research.toolkit_contract`), a list
  of exactly one toolkit until Phase 3 step 2 gives units their own (§9.5). It replaces
  `toolkit_root` and `tool_author_branch` (Phase 3 step 0a; no alias, ruling D16). Every fact the
  orchestration relies on about a toolkit is a field here, so pairing with another toolkit is a
  configuration change, never a code change:
  - `name` — the toolkit's declared name (`research_toolkit`, D12): the first segment of every
    manifest key (`<name>.<package>.<tool>`, refused at launch otherwise) and of every producer its
    tools mint. It decides evidence (`provenance.store.is_evidence`), so a name that is no Python
    identifier, or equals or prefixes a producer the harness mints (`mcp`, `read_file`, `input`, …:
    `provenance.store.HARNESS_PRODUCERS`), is refused when the contract loads: a misconfigured name
    can never make what a role only read count as evidence (CLAUDE.md §2b).
  - `root` — the toolkit checkout (the coder's worktree), used by `ToolkitRegistry`,
    `PersistentSandbox`, the gate and the run.
  - `package_glob` — the top-level folders that are tool packages (default `*_utils`).
  - `template_package` — the package the coder is shown as the layout to mirror; it must match
    `package_glob`, and the launch refuses a root that lacks it (`toolkit_contract`).
  - `authoring_branch` — the branch the runtime commits authored tools onto directly; it never
    merges to master (§9.9). Required, no default. The launch preflight
    (`check_toolkit_branch_clean`) asserts the checkout is on it with a clean tree. Consumed by
    `graph.runtime` → `DebateToConverge` → `merge_flow.propose_and_pr`.
  - `default_env` — the environment (a folder under the toolkit's `envs/`) a unit's own REPL and
    shell run in, and where a package naming no environment runs and is tested. Required; the launch
    refuses the config when `<root>/envs/<default_env>/env.yaml` is missing. Live: `analysis`.
  - `test_classes.fast` — the pytest `-m` expression of the toolkit's fast tests, which the gate
    reruns on every change for regression (default `not slow and not live`, the markers the
    toolkit's root `conftest.py` declares). `test_classes.live` (default `live`): the expression the
    gate deselects from a change's own tests (no network there). `test_classes.data` (default
    `data`): the marker of a test reading a named data set, `data("<name>")`.
- `orchestration.yaml.stores` — the data stores (`auto_research.store.config`, Phase 3 step 0b): a
  list of at least one, names unique, no two sharing a folder or a port; `db` is the default store
  (ruling 2026-11-10). Each entry:
  - `name` (identifier), `kind` (`local` | `remote`), `buckets` (3–63 of `[a-z0-9-]`; each must exist
    with versioning Enabled, preflight `store_buckets`), `admin_env` (`s3_access_key`, `s3_secret_key`,
    `catalog_password`: the NAMES of the `.env` variables holding the admin secrets, never a secret).
  - `local`: `root` (the objects folder served as S3, each top folder a bucket), `state_dir`
    (`postgres/`, `versions/`, `iam/`, `logs/`; made when absent, its parent must exist), `socket_dir`
    (a short absolute folder on local disk outside the temp root, mode 0700, for the IAM and Postgres
    Unix sockets; their paths ≤ 107 bytes), `ports` (`s3`, `iam`, `catalog` on 127.0.0.1, distinct, ≥ 1024), `catalog` (`database` default
    `db`, `admin_user` default `ar_admin`). `root` and `state_dir` must take `user.` xattrs; none of
    the three may meet a folder a jail may bind (`agent_fs_allow`, `agent_fs_readonly`, `test_data`,
    toolkit roots, `human_envs`, `env_externals` roots, the runtime dir; preflight `store_config`). A run's launch step starts the servers before its preflight (`graph.runtime.start_run_stores`; `store_servers` only checks) and leaves them running; `python -m auto_research.store status|stop` shows and stops them.
  - `roles` (optional; `{producer: [patterns]}`, default `[analyst_worker_*]`): units whose name matches a pattern hold the store's producer role (write any key of its buckets, create-only by the toolkit helper), every other unit the reader role (pinned reads); each unit is its own S3 user and Postgres login for the run (`store.keys`). Until unit definitions name the role (Phase 3 step 3).
  - `remote`: `endpoint` (http(s) URL), `region` (default `us-east-1`), `catalog_dsn` (libpq conninfo
    naming host, dbname and user, no password).
  Live: `db`, local, root `/xray02/fzhou_db`, state `/xray02/fzhou_db.d` (both made at migration, S11,
  2026-11-11), sockets `/scratch/fzhou_db.run`,
  ports 18070/18076/18432, buckets `ixpe`, `nicer`, `nustar` (default D-names, to confirm).
- `orchestration.yaml.test_data` — `{name: folder}`: the host's data sets the toolkit's
  `data("<name>")` tests read (the toolkit names a need, the host satisfies it by name). The gate binds
  each existing folder read-only into its test run only (never on `agent_fs_allow`) and names it by
  `TOOLKIT_DATA_<NAME>`; a data test skipped in the gate fails the change. Names `[a-z][a-z0-9_]*`,
  folders absolute (launch check); every data set a toolkit test names must be declared and every
  declared folder must exist (preflight `toolkit_test_data`). Live: `cygx1_ixpe`, `cygx1_nustar`
  (Cyg X-1's IXPE and NuSTAR folders, read by `xspec_utils/tests/test_outputs.py`, the only tests on
  the toolkit's `main` that read real data). Replaces `gate_readonly_roots`.
- `orchestration.yaml.human_envs` — `{name: conda prefix}`: the human's own conda environments a
  toolkit environment definition may name as its `base` (a derived environment layers its activation
  on that prefix, read-only). The toolkit names its base by name and this host supplies the prefix,
  the rule `env_externals` follows for externals (CI convention, A4). Live: `astropy:
  /grad/fzhou/.conda/envs/astropy`, the base of `research_toolkit/envs/analysis/`. Preflight
  (`human_envs`, `env.check_human_envs`) refuses a name that is not an environment name, a prefix
  inside the runtime's conda folder, a declared prefix without `bin/python`, and a
  toolkit definition whose `base` is not declared. Nothing here names the analysis environment
  itself: the toolkit's default environment (`toolkits[].default_env`, `envs/<id>/`) is what a
  unit's REPL and shell run in, its activation, boot file and launch `probe` in its definition
  (W4, 2026-11-09; the keys `analyze_env`, `analyze_ini_sh`, `repl_init_py` and `analyze_probe`
  retired with the files `sandbox/analyst_env.sh` and `sandbox/repl_init.py`). A human prefix a
  derived environment uses must be in `agent_fs_readonly` for the jailed processes to reach it.

## The OS jail and `agent_fs_readonly`

`agent_fs_allow` is the allow-list `PathGuard` enforces on the paths the runtime process itself
opens for a model (`read_file`, `list_dir`, `file_edit`). The jail turns the same list into a
**mount plan**, unconditionally (ruling D25, 2026-11-09): every runtime process that can touch a
disk (both author harnesses, the analyst REPL and shell, the gate's Layer-B pytest, environment
builds) runs under `bwrap`, and anything outside the list is not in its filesystem at all rather
than being refused by a rule. `agent_fs_readonly` names the subset mounted read-only — the analysis conda environment,
HEASoft and CALDB, which every run reads and nothing writes.

Two things to know, both learned when the jail was first turned on:

- **The allow-list may be incomplete as a mount plan even though it works as a guard.** Software
  reached through one allowed path can live behind another: every file in PyXspec's package
  directory is a symlink four levels up into a sibling of `$HEADAS`, so the HEASoft **release root**
  has to be listed too, not just `$HEADAS`. `runner.check_analysis_probe` runs the default
  environment's `env.yaml` `probe` inside the jail at launch and fails with the missing-root hint, rather than
  letting every tool call fail separately.
- **The gate's tests lose the network.** That is deliberate (§9.4: a mergeability verdict must
  depend on the data on disk), and it was verified against all 30 existing Layer-B suites before
  being wired. Author harnesses and the REPL keep the network, since one talks to a model API and
  the other downloads observations.

A host without `bwrap`, or one where user namespaces are administratively denied, is refused at
launch (`jail.check_os_jail_available`, with the reason).

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
is genuinely free says so explicitly with `priceMTk: {input: 0, output: 0}`, which at least labels
the zero as ours. GLM-5.3-Flash is not free: "GLM-5.3-Flash | $0.15 | $0.03 | … | $0.50" per
million input, cached-input and output tokens (docs.z.ai/guides/overview/pricing, read 2026-11-04),
the rates litellm carries for `zai/glm-5.3-flash`.

Two rate structures no static table can express, worth knowing before a cost column is trusted:
DeepSeek moved to **peak / off-peak** multipliers (effective 2026-08-16), so a recorded DeepSeek
cost is a lower bound during peak hours; and Model Studio applies a 50 per cent batch discount and
a context-caching discount that cannot be combined.

The same numbers are kept in `tests/test_live_coder_matrix.py: _PRICES` for the throwaway members
that file constructs, since a live leg needs them without being configured here.

## Model capabilities (`model_capabilities.yaml`)

The one source of what a model takes (user ruling 2026-11-04, Z1d): `provider -> model ->
{images, documents}`, written by the builders from each vendor's own model page; the adapters trust
it and keep only vendor-wide wire behaviour and limits. Every model a member names needs an entry,
or the launch preflight (`member_capabilities`) refuses the member. `documents` is the model's
native PDF input; a Responses vendor whose dialect takes no inline document (`accepts_inline_documents
= False`: Alibaba, DeepSeek, Z.AI) is sent the local parse whatever the entry says. Read 2026-11-04:

| provider / model | images | documents | source (quoted) |
|---|---|---|---|
| anthropic / claude-opus-5, claude-sonnet-5, claude-fable-5, claude-opus-5-5 | yes | yes | platform.claude.com/docs/en/models/{opus-5,sonnet-5,fable-5,opus-5-5}/overview: "Input → output \| Text and images → text"; build-with-claude/pdf-support lists all four; PDF "As a base64-encoded PDF in `document` content blocks" |
| openai / gpt-5.5, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna | yes | yes | developers.openai.com/api/docs/models/<id>: "Image \| Input only"; guides/file-inputs: "The Responses API accepts the file types listed below as `input_file` items", "PDF files: On models with vision capabilities, such as `gpt-4o` and later models, the API extracts both text and page images" |
| google / gemini-3.5-flash | yes | yes | ai.google.dev/gemini-api/docs/models/gemini-3.5-flash: "Inputs Text, Image, Video, Audio, and PDF" (deprecated, "automatically routed to `gemini-3.6-flash`", same inputs) |
| deepseek / deepseek-flash | yes | no | api-docs.deepseek.com/quick_start/pricing: model `deepseek-flash`, version "DeepSeek-V4.1-Flash-1017", Vision "✓"; guides/vision: "The `deepseek-flash` model accepts images alongside text"; guides/responses_api: "`deepseek-flash` processes `input_image` parts as real images", and on `message` "File inputs are not supported" (read 2026-11-04; the 2026-08-25 text-only policy is retired, user ruling 2026-11-04) |
| deepseek / deepseek-v4-flash | yes | no | quick_start/pricing, footnote (1): "The legacy names `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` are still accepted, but the corresponding models have been retired, their requests are served by the DeepSeek-V4.1-Flash-1017 model and billed at the Flash price" — the same model as `deepseek-flash` |
| deepseek / deepseek-v4-pro | no | no | api-docs.deepseek.com/quick_start/pricing: "Vision \| … Not supported"; guides/responses_api: "File inputs are not supported" |
| zai / glm-5.3 | no | no | docs.z.ai/guides/llm/glm-5.3: "GLM-5.3 currently supports text-only inputs" |
| zai / glm-5.3-flash | yes | yes | docs.z.ai/guides/vlm/glm-5.3-flash: "Input Modality: Video / Image / Text / File"; api-reference/llm/chat-completion: `file_url` "Only GLM-5.3-Flash series. Supports formats such as pdf …". Both documented on Chat Completions only; this adapter's Responses dialect is not documented by Z.AI and not yet live-probed for media |
| zai / glm-5.2 | no | no | docs.z.ai/guides/llm/glm-5.2: "Input Modalities: Text" |
| dashscope / qwen3.8-max | yes | yes | alibabacloud.com/help/en/model-studio/qwen3-8-max: "Input Modality: Image, Text, Video"; qwen-api-via-openai-responses: "input_file { type, file_url } File input. … Currently supported by qwen3.8-flash, qwen3.8-max, and qwen3.5-ocr" (by public URL only, so this adapter sends the parse) |
| dashscope / qwen3.7-plus | yes | no | alibabacloud.com/help/en/model-studio/qwen3-7-plus: "Input Modality: Image, Text, Video"; not in the `input_file` list above |

## Changelog
Compacted 2026-09-01, 2026-09-24, 2026-10-07 and 2026-10-19 (every entry); detail in git history.

- 2026-05-26 -> 2026-06-24 — Config surface: model groups/pricing, vision/document flags, `runtime_dir`,
  caps, report formats, `coder` + non-Anthropic `analyst_colead`, `code_gate.yaml`, `human_channel`,
  `tool_author_branch`/`_cap`, `obs_db_root`, `sandbox_call_timeout_s` (300), `react_loop_absolute_cap` (60).
- 2026-07-06 -> 2026-07-08 — [C1] `finding_vocabulary`; `claude-fable-5` reviewer member; adaptive
  `thinking` on Anthropic chat members, `max_tokens` raised. Live-verified.
- 2026-07-14 -> 2026-08-20 — `*.example` templates; `repl_init_py`, `analyze_probe`; `context_mgmt_*`,
  `tool_search_enabled` (NOT LIVE TESTED, on 07-28 after live tests); `glm-*`/`qwen*` rates;
  `agent_os_jail`, `agent_fs_readonly` (default off).
- 2026-09-24 -> 2026-09-25 — `librarian_react_loop_cap` 40; `io_search_max_iterations` removed;
  `coder_wait_floor_s`/`_default_s`/`_max_s` replace `check_coder_min_interval_s`; `telegram_rich_messages`.
- 2026-10-06 -> 2026-10-07 — `env_externals` (env plan B1); `/data/fzhou/XSPEC/relxill` in
  `agent_fs_allow`/`agent_fs_readonly`.
- 2026-10-18 — [B4] `context_tokens`, Sonnet 5 `priceMTk` 2/10; [B5 ruling] `context_tokens` override
  only; [B5] `context_mgmt_clear_*`, `context_mgmt_exclude_tools` ([B11 ruling] `[message,
  wait_for_reply]`); [B6/B7] `compaction_*`, role `session_compactor`; [B12 ruling]
  `context_mgmt_enabled: false`.
- 2026-11-04 — A1: `vision`/`documents` member keys retired from `models.yaml`; capability is the
  adapter's declaration (`models/DOCS.md`).
- 2026-11-04 — `code_gate.yaml` `allowlist_obsid_tokens`: the `synth_[A-Za-z0-9_]+` entry added earlier the same day is withdrawn (user ruling S1).
- 2026-11-04 — Z1b: the price note no longer calls Z.AI's Flash tier free; it quotes the pricing page
  for GLM-5.3-Flash ($0.15 / $0.03 / $0.50), which litellm carries.
- 2026-11-04 — Z1d: `model_capabilities.yaml` (what each model takes; the one source, read by
  `models.capabilities`), with the Model capabilities section citing each entry's vendor page.
- 2026-11-04 — H2: `obs_db_root` is a fresh, empty root (`/xray02/fzhou_obsdb`, also in `agent_fs_allow`); the old Cyg X-1 folder stays the human's archive; `.example` files unchanged.
- 2026-11-04 — `orchestration.yaml` `gate_readonly_roots` (H1 item 3): folders bound read-only into the gate's Layer-B test run only (`sandbox.gate._jailed_pytest`), never on `agent_fs_allow`, so the toolkit's real-data tests find their inputs after the obs-db moved to `/xray02/fzhou_obsdb` while the workers never see the old tree.
- 2026-11-04 — `models.yaml` `librarian_io`: only `claude_sonnet_5` remains; the `gpt_5_5_medium` and `gemini_3_5_flash_thinking` members were legacy fixtures (a role runs its first member unless one is named, `agents.roles._select_model`).
- 2026-11-04 — Z1h: the coder member is `deepseek-flash` (V4.1-Flash); capability rows for `deepseek-flash` and `deepseek-v4-flash` (images yes, documents no) from DeepSeek's pages; the text-only policy is retired.
- 2026-11-04 — `coder` member sets `model_reasoning_effort: high` (user ruling; DeepSeek's Codex integration page sets it; passed to Codex as a config key by `codex_sdk`).
- 2026-11-05 — `model_capabilities.yaml`: `openai/gpt-6.1-sol` images true ("Input modalities: text, image", developers.openai.com/api/docs/models/gpt-6.1-sol, read 2026-11-05); documents true per the file-inputs guide, as for the GPT-5.6 rows; confirmed by the paid image+PDF probe. Used as the lead of the live figure-shape attack cells.
- 2026-11-09 — Phase 3 step 0a W5 (D19): `env_externals.<name>.private: {VAR: separator}` declares the per-process and per-call private folders; `heasoft` declares `PFILES` with `;` (live config and the `.example` comment); the code holds no `PFILES` literal.
- 2026-11-09 — W5 review: `.example` ships a commented `heasoft` line with `private: {PFILES: ";"}`; an undeclared variable gets no isolation; preflight checks the declaration against the analysis activation.
- 2026-11-09 — Phase 3 step 0a W2: `toolkits:` (the toolkit contract: `name`, `root`, `package_glob`, `template_package`, `authoring_branch`, `test_classes.fast`) replaces `toolkit_root` and `tool_author_branch` in the live config and `.example`; the authoring branch has no default (the code default `ai` is gone).
- 2026-11-09 — `toolkits[].default_env` (required): `analysis` in the live config and `.example`.
- 2026-11-09 — Phase 3 step 0a W4: `analyze_env`, `analyze_ini_sh`, `repl_init_py` and `analyze_probe` retire (no aliases, D16); the analysis environment is the toolkit's default environment (`toolkits[].default_env`, `research_toolkit/envs/analysis/`: base, activation, probe in its definition). New `human_envs` (name → conda prefix) supplies the human bases a definition names; preflight `human_envs` checks it. `sandbox/analyst_env.sh`, `sandbox/repl_init.py` (and their `.example` templates) deleted; their `.gitignore` lines stay while the frozen installer writes them. The installer still writes the retired keys (frozen until Phase 3 ends, D16).
- 2026-11-09 — Phase 3 step 0a W3: `test_data` (`{name: folder}`, live: `cygx1_ixpe`, `cygx1_nustar`; `.example`: `{}`) replaces `gate_readonly_roots`; the NICER folder, read by no test, is dropped. `test_classes` gains `live` and `data` (defaults; the live config keeps `fast` only).
- 2026-11-09 — W4 review: `human_envs` names must be environment names and prefixes lie outside `agent_conda_dir()`; the `.gitignore` lines for `sandbox/analyst_env.sh` and `sandbox/repl_init.py` are restored (the frozen installer still writes them, D16).
- 2026-11-09 — W3 review: preflight `toolkit_test_data`. An `obsdb` data set was added for the ai-xspec branch's obs-db tests and removed when the toolkit moved to `main`, whose tests read no obs-db.
- 2026-11-09 — Phase 3 step 0a W10 (ruling D25): `agent_os_jail` retires; the jail is unconditional and a host that cannot run `bwrap` is refused at launch. `mcp_servers` retires too: a configured server ran outside the jail; the workers' shell is the jailed builtin server.
- 2026-11-09 — W10 review: the launch refuses a retired key (`preflight.check_config_keys`: `agent_os_jail`, `mcp_servers`, `toolkit_root`, `tool_author_branch`, `analyze_env`, `analyze_ini_sh`, `repl_init_py`, `analyze_probe`, `gate_readonly_roots`, each named with its replacement) and an empty `agent_fs_allow`.
- 2026-11-10 — Phase 3 step 0b S1: `stores:` (the data stores: `name`, `kind`, `buckets`, `admin_env`; local `root`, `state_dir`, `socket_dir`, `ports`, `catalog`; remote `endpoint`, `region`, `catalog_dsn`) in the live config (`db`, local) and `.example` (a local entry, a remote one commented).
- 2026-11-10 — Phase 3 step 0b S4: `stores[].roles` (`producer` patterns) in both files; the obs-db root leaves `agent_fs_allow` (live and `.example`): no jail mounts a store's folders, tools reach it through S3 with their unit's own keys.
- 2026-11-10 — Phase 3 step 0b S9: `observations.yaml` and `observations.yaml.example` are deleted with `auto_research.observations` (not backwards compatible): data lives in the `stores:` block's stores, read through a unit's own keys (S4) and, for the lead, the catalog query. The live file's last content (`obs_db_root: /xray02/fzhou_obsdb`) was kept in a scratch folder erased 2026-11-18.
- 2026-11-10 — S9 follow-up (main ruling 5): `stores[].roles.producer` is `[analyst_worker_*]` in both files and the default; the lead holds the reader role (it only queries the catalog). The `stores:` block must declare the default store `db` (`store_config`, ruling 6).
- 2026-11-10 — S7 follow-up: `code_gate.yaml` `allowlist_obsid_tokens` removed (the gate scans every added line since step 0b; synthetic data lives in a store's `synthetic_buckets`).
- 2026-11-11 — (main) store `db` admin secret names renamed to the human's choice: `AR_DB_S3_KEY_ID`, `AR_DB_S3_KEY_SECRET`, `AR_DB_S3_CATALOG_SECRET` (`stores[].admin_env`; also `.env.example`, README).
- 2026-11-11 — `stores:` live entry: the store `db` exists (migration S11 done).
- 2026-11-18 — The 2026-11-10 S9 entry no longer names its scratch copy of `observations.yaml` (the folder was erased; CLAUDE.md is self-contained).
