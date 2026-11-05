# `auto_research/` — orchestration package

## Purpose
Top-level Python package for the orchestration half of the system: the LangGraph
graph, agent role wrappers, schemas, storage, providers, and the literature
sources. The system's runtime **cannot** modify this code; new domain tools land
in the sibling `research_toolkit` package via the gated flow in
[CLAUDE.md §9](../../CLAUDE.md#9-phase-2--tool-authoring-the-active-phase).

## Architecture
A src-layout package. Submodules are organized around the Phase 1 system tour
in [CLAUDE.md §8](../../CLAUDE.md#8-what-phase-1-built-the-system-as-it-stands)
and the active Phase 2 build order in
[CLAUDE.md §9.10](../../CLAUDE.md#910-build-order-phase-2):

| Submodule | Step | Role |
|---|---|---|
| `config_paths.py` | — | Where the five config files are, resolved when asked for. `config_dir()` answers with the repository's own `config/` unless `AUTO_RESEARCH_CONFIG_DIR` names another, so a process can be run against a different configuration and every reader agrees. Before it, eleven modules each computed the location relative to their own source file. |
| `env.py` | — | Launch-time `.env` loading and required-env check (`check_required_env()`, whose model-key requirements are **derived from `config/models.yaml`**: one group per provider a role is configured with, or the exact variable a member names with `key_env`); small `orchestration.yaml` accessors (`runtime_data_dir`, `papers_dir`, `sandbox_call_timeout_default`, `provider_retry_policy`). |
| `setup.py` | — | The install-script Python stage (`python -m auto_research.setup`, run by `install.sh`): discovers defaults, prompts (interactive `questionary`) or reads `--answers` (headless), then writes the gitignored `config/orchestration.yaml` + `observations.yaml` from their `*.example` templates (deriving `agent_fs_allow` from the answered paths) and runs the preflight smoke test. |
| `preflight.py` | — | Launch-time **checklist** (`Preflight` / async `run_preflight()`): runs every launch precondition (all no-LLM but the last, `member_media`, one tiny image and PDF per chat member, Z1e), and carries a warning tier (`PreflightWarning`) for a precondition that is satisfiable but never proven live — the launch continues and the caution is recorded (`.env` shape, API-key fallback usability, runtime DB schema health, author↔reviewer decorrelation, toolkit git identity/branch, data-catalog health, `analyze_probe` stack smoke-test, human-channel ping) before any model client is built. |
| `hitl` | 5 | Raise-to-human clarification channel: the injectable `HumanHandler`, the CLI/Slack/WhatsApp connectors, the CLI-vs-library resolver, and the launch-time channel ping. |
| `schemas` | 1 | Pydantic v2 message protocol between agents. |
| `models` | 2 | Provider abstraction + role→model registry from `config/models.yaml`. |
| `observability` | 3 | LangSmith tracing + per-call cost logging hooked into the provider layer. |
| `storage` | 4 | SQLite layer (papers, memory, query_log; embedding column reserved for Phase 2). |
| `sources` | 5 | `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. |
| `retrieval` | 6 | `RetrievalBackend` (text search now, vector search stubbed). |
| `observations.py` | 8 | Single `obs_db_root` loader (`config/observations.yaml`) that discovers per-mission `<root>/<MISSION>` subdirs from disk, plus the `catalog_health` preflight check. (The catalog **read** moved to `research_toolkit`'s `catalog_utils` — the `data_catalog` tool.) |
| `agents` | 7-8 | Runtime prompt loader plus thin per-role wrappers that load `prompts/<role>.md`, call configured model groups, and validate schema JSON. |
| `consensus` | 9 | `ConsensusStrategy` — debate-to-converge with lead convergence checks and adjudication. |
| `graph` | 10 | LangGraph assembly + the two capped feedback edges. |
| `sandbox` | 11 + Phase 2 | Subprocess sandbox, `research_toolkit` invocation, and the propose/branch/gate authoring flow. |
| `monitor` | 12 | Async-safe run monitor with 1-10 chatter, JSONL logs, and team-block Markdown reports. |
| `cli` | 12 | `python -m auto_research "..."` entry point. |

Async-first throughout ([CLAUDE.md §10](../../CLAUDE.md#10-tech-stack--conventions)).

## Interfaces / Contracts
- Package exports its version as `auto_research.__version__`.
- Package exports monitored library entry points: `run_research()` returns a
  `ResearchRun` with final state and monitor; `run_request()` remains the
  verdict-only compatibility entry.
- Package exports `RunMonitor` for notebook and test instrumentation.
- Each submodule documents its own interface in its `DOCS.md`. Cross-module
  contracts go through `auto_research.schemas`.
- `auto_research.config_paths.config_dir()` / `orchestration_yaml()` / `models_yaml()` /
  `observations_yaml()` / `code_gate_yaml()` / `model_capabilities_yaml()` — the configuration
  directory and the five files in it, resolved **per call** (the capability table, Z1d, describes
  models and is copied unchanged into any other config dir). Unset, they are the repository's own `config/`, which is every
  ordinary run. `AUTO_RESEARCH_CONFIG_DIR` overrides them, and being an environment variable is
  the point: the sandbox REPL, the author harness and the gate's test runner are subprocesses that
  inherit it, and two of the three are not Python, so nothing process-local could reach them.
  Setting it before the process starts is also what makes the four `lru_cache`d readers harmless —
  and those now key on the resolved path rather than on nothing, so a test that moves the directory
  is not served a stale answer. The installer is deliberately not routed through here: it writes
  the live configuration *from* the tracked `.example` templates, which live in the repository
  whatever a run is configured with. Secrets likewise stay at the repository root — an API key is a
  property of the machine, not of the experiment.
- `auto_research.env.check_required_env()` loads the repo-root `.env` with
  `override=False` and validates required keys before SDK clients are constructed. The source keys
  (ADS, Tavily) are fixed; the **model keys are derived from `config/models.yaml`** — a provider a
  role names implies its SDK's variable (`provider_key_aliases`, which derives the standard name
  from `models.keys.env_var_names` so the launch check, the key pool and the author harnesses
  cannot disagree), and a member naming `key_env`
  requires exactly that variable, which is how the coder harnesses' vendor keys become a launch
  requirement. A run therefore requires the vendors it actually calls and no others, and the
  failure names the member that wanted each missing key. An unreadable config falls back to the
  historical three rather than relaxing to nothing.
- `auto_research.preflight.run_preflight(*, human_handler=None, monitor=None)` (async) is the
  launch-time entry that runs the whole deterministic checklist (registered via
  `build_default_preflight(human_handler)`): `check_required_env`,
  `models.keys.check_api_key_fallbacks` (an optional `<PROVIDER>_API_KEY_FALLBACK` is usable —
  not blank, not identical to the primary, not set without one; runs right after
  `check_required_env`, which is what loads `.env`),
  `models.capabilities.check_member_capabilities` (every configured member's model has an entry in
  `config/model_capabilities.yaml`, Z1d),
  `models.media_probe.check_member_media` (registered last: every distinct configured chat
  member answers one tiny image and one tiny PDF through the real call; paid, its cost on the
  ledger; Z1e),
  `storage.check_database_health` (the runtime SQLite DB exists with its full schema,
  created if absent — so a fresh run's first `text_search` can't hit `no such table: papers`),
  `sandbox.review.check_author_reviewer_decorrelation`,
  `sandbox.merge_flow.check_toolkit_root_configured` (`toolkit_root` is set in
  `config/orchestration.yaml` and is a directory — every runtime path hint derives from it),
  `sandbox.merge_flow.check_toolkit_git_identity` (the `research_toolkit` checkout has a git
  commit identity, so a runtime tool commit can't fail mid-flow),
  `sandbox.registry.check_toolkit_manifests` (every `*_utils` package imports and exposes a
  `TOOL_MANIFEST`; a failing one would reach the analysts as missing tools and nothing else),
  `observations.check_catalog_health` (every mission's local data catalog is healthy via
  `research_toolkit`'s detect-only `obsdb_catalog.healthcheck`),
  `sandbox.runner.check_analyze_probe` (runs the configured `analyze_probe` stack smoke-test under
  `analyze_env`; skips when unset — the domain/lit-only escape hatch), and — when a channel is
  resolved — `hitl.make_human_channel_check` (pings the channel, green only on a reply). `run_research()`
  resolves the handler then awaits this before any LLM; aggregates every failure into one
  `PreflightError`.
- `auto_research.observations.load_observations_config()` loads the single `obs_db_root`
  from config and discovers missions from disk (`<root>/<MISSION>` subdirs marked by a
  `catalog.json`); mission names are not hardcoded in agent wrappers. `config.missions` is
  the discovered map; `config.mission(name)` resolves one mission's path by case-insensitive
  subdir match. The catalog **read** (parsing/listing `<obs_db_path>/catalog.json`) moved to
  `research_toolkit`'s `catalog_utils` package — surfaced as the `data_catalog` tool — alongside
  the `obsdb_catalog` writer (§2: domain reads belong to the toolkit). This module keeps only the
  obs-db-location config and the launch-time health check.
  `check_catalog_health()` is the launch-time preflight check: it runs `research_toolkit`'s
  detect-only `obsdb_catalog.healthcheck` (pure stdlib, never writes) over every configured
  mission and raises on any drift (malformed/missing/regressed/level-inconsistent/uncatalogued),
  aggregating all missions; the human repairs via `obsdb_catalog.repair` out of band. The root
  itself must be a directory this user can write; an empty one is healthy (H2: `acquire` creates
  a mission's folder on its first observation).
- `observations.obsid_folder(mission_dir, obsid, entry=None, *, mission=None, index=None) -> Path` —
  where an ObsID's folder is under its mission folder, as the toolkit's `obsdb_catalog.locate`
  finds it: the catalog entry's record first (a product path up to the ObsID part, the newest
  level-2 event first; relative, no `..`, an existing folder), then `mission`'s `OBSID_TREES` rule
  (default the folder's name; a unique match, two raise `LookupError`), else `<mission_dir>/<obsid>`.
  `observations.obsid_index(mission_dir, mission) -> {obsid: [folders]}` walks the tree once;
  `index` takes a callable returning it, so a call naming many strings walks it at most once. `OBSID_TREES` mirrors
  `research_toolkit`'s `obsdb_catalog._domain.TREES` (HEASARC's tree per mission, or flat); the two
  change together. `observations.obsid_mission(folder) -> str` names the mission folder of an
  ObsID folder under either layout, or the nearest folder above it holding a `catalog.json` (the
  HTML report's `observation <mission> <obsid>`).

## Dependencies
- External: `pydantic`, `langgraph`, `langsmith`, provider SDKs (`anthropic`,
  `openai`, `google-genai`), source clients (`arxiv`, `ads`, `tavily-python`),
  `python-dotenv`, `pyyaml`. Pinned in `environment.yml`.
- Internal: imports from the sibling `research_toolkit` package at runtime
  (analyst tools); that package is managed/installed separately by the human.

## Dictionary-level reference
- `__version__: str` — package version. `"0.0.0"` until the first non-skeleton
  release.
- `run_research(request, ..., chatter=10, monitor_jsonl=None, report_formats=None,
  install_signal_handlers=False) -> ResearchRun` — monitored library entry for CLI/notebook use.
  Raises `run_control.UserInterruptionError` when a human stops the run (`/stop`, or SIGINT/SIGTERM
  when `install_signal_handlers`); only a process that owns its signals may pass that flag.
- `run_control.RunControl` — run-wide control shared by the agents and the HITL command channel.
  `authoring_session` is the live coding session; `unclosed_sessions` holds sessions whose close
  raised, closed again by the run's teardown (C2c).
  Steer is cooperative (queued, delivered at the target's next ReAct turn); **stop is an
  interrupt**: `interrupt(reason, by)` flags the run and cancels the registered run task, so it
  needs no cooperation from the pipeline.
  **Per-seat inboxes (E12):** every text a seat receives — a `/steer`, the human's reply, another
  seat's `message`, the coder's question, a runtime notice — is an `InboxEntry(sender, text,
  handles, kind)` queued by `add_steer(key, text, *, sender="human", kind="steer", handles=())`
  and drained once by `drain_inbox(key)` (`drain_steer` returns the texts); `render()` is the user
  turn the seat reads; `peek_inbox(key)` lists the waiting entries without draining them, and
  `WAKING_KINDS` (`message`, `reply`, `steer`) are the kinds a seat is resumed to read (D5;
  `agents/DOCS.md` `TurnOutcome`, `hitl/DOCS.md` `wake`). `fail(exc)` (ruling D6) ends the run with
  a failure raised in a channel task nobody awaits: it sets `fatal` and cancels the registered run
  task, and `run_research` raises `fatal` instead of treating the cancel as a stop. A second
  `interrupt` (a second `/stop`) does nothing, so it cannot cancel the cleanup the first started.
  `cancel_resumed(timeout=None)` cancels the channel tasks and waits at most `timeout`, raising
  `TimeoutError` with how many still run. `running` says whether a run task is registered and
  live; the stop's or failure's cancel goes out once (`_cancel_sent`). Only the first
  failure counts and none after a human stop (the stop wins); `interrupt` after a failure does
  nothing. Both cancel through `_cancel_run`, which reads the run task when it fires (none once the
  graph has returned and unregistered it) and never cancels a task twice. `set_live`/`is_live` mark a running loop; `register_resumed(sender, seat,
  task)` / `resumed_by(sender)` track seats a message resumed; `register_background(task)` holds
  awaited human replies; `cancel_resumed()` cancels both (the interrupt cleanup's first step).
- `run_control.UserInterruptionError` — raised in place of the `CancelledError` that carried a
  human stop. Carries `reason`/`by`/`where` and the paths that exist (`artifacts`); no report is
  composed on that path, and `resume=True` continues from the last completed node.
- `run_request(request, ...) -> ReviewVerdict` — compatibility wrapper returning
  only the final verdict.
- `ResearchRun` — result object with request, final verdict, final graph state,
  and monitor.
- `RunMonitor` — async-safe event collector and renderer from
  `auto_research.monitor`.
- `check_required_env()` — loads `.env` without overriding existing process
  environment variables, then raises a clear error if any required key group is absent.
- `runtime_data_dir() -> Path` — resolves `runtime_dir` from `orchestration.yaml`
  (default `~/.auto_research`); `AUTO_RESEARCH_RUNTIME_DIR` (`RUNTIME_DIR_ENV`), when set, names
  it instead, per call, so children inherit it. The test suite sets it for every deterministic
  test (`tests/conftest.py`, D4) so none writes the configured runtime dir; a live test keeps the
  configured one. It moves `runtime_dir` only: an explicit `papers_dir` key stays where it is, and
  `agent_fs_allow` (which names the configured `runs` root) does not follow it, so a process that
  wants a private runtime outside the suite uses its own `AUTO_RESEARCH_CONFIG_DIR`.
- `papers_dir() -> Path` — resolves `papers_dir` from `orchestration.yaml`
  (default `<runtime_dir>/papers`); re-exported through `auto_research.sources`.
- `sandbox_call_timeout_default() -> int` — resolves `sandbox_call_timeout_s` from
  `orchestration.yaml` (default 300): the global sandbox per-tool-call timeout a tool's
  `tool_config.yaml` overrides per tool (`auto_research.sandbox.registry`).
- `ProviderRetryPolicy(max_retries, base_cooldown_s, max_cooldown_s)` /
  `provider_retry_policy() -> ProviderRetryPolicy` — the transient-provider retry knobs
  from `orchestration.yaml` (`provider_retry_max` 8, `provider_retry_base_cooldown_s` 60,
  `provider_retry_max_cooldown_s` 600 — the "Internet stability" section; per-field
  fallback on missing/malformed values). Consumed by `models.retry` at the
  `ModelHandle.call` choke point.
- `preflight.Preflight` — an ordered registry of launch-time checks. `.register(name, fn)`
  (also usable as a decorator), `.names()`, async `.run_checklist(*, monitor=None)` (runs all —
  sync or async checks — and aggregates failures into `PreflightError`).
  `build_default_preflight(human_handler=None)` assembles the project checklist; async
  `run_preflight(*, human_handler=None, monitor=None)` builds and runs it. Each check is a
  zero-arg fn that raises on failure and lives in its own module.
  **Checks are recorded onto the run ledger when a `monitor` is passed** — `preflight.<name>`
  at level 5 for a pass, level 1 for a failure, plus a level-3 `preflight.summary` tally. The
  stdout progress lines are unchanged; the monitor is additive, because a detached or notebook
  run (the primary mode) never sees the terminal, and a preflight failure used to leave no run
  artifact at all. `run_research` builds its monitor **before** the preflight to enable this;
  standalone callers (`setup.py`'s smoke test) pass no monitor and are unaffected. A monitor
  fault is swallowed — observability never decides whether a runnable launch proceeds.
- `setup.derive_fs_allow(values, extra_paths=None) -> list[str]` — the `agent_fs_allow` allow-list
  derived from the answered paths (toolkit_root/obs_db_root/analyze_env/runtime_dir + domain-dep
  `extra_paths` + `/tmp`); placeholders dropped, deduped, `auto_research` never included.
- `setup.render_orchestration(template_text, values, fs_allow) -> str` /
  `setup.render_observations(template_text, obs_db_root) -> str` — line-based renderers that fill the
  `*.example` template values while preserving comments (the `agent_fs_allow` block is regenerated).
- `setup.seed_defaults() -> dict` — default answers: prior live-config values (upgrade) over template
  placeholders. `setup.Answers` resolves each value from the answers file → interactive prompt →
  default, else raises `SetupError` (headless fail-loud). `setup.main(argv)` is the CLI entry.
- `setup` domain-dependency (page 3) helpers: `render_analyst_env(entries)` / `render_repl_init(entries)`
  materialize the two sandbox hooks from typed entries (`shell_init`/`env_var`/`license_tool` →
  `analyst_env.sh`; `python_init` → `repl_init.py`); `domain_fs_paths(entries)` collects the extra
  `agent_fs_allow` roots (bucket A/D/E); `domain_probe(entries, existing)` assembles `analyze_probe`;
  `package_specs(entries)` splits conda/pip installs; `discover_domain_deps()` suggests from `$HEADAS`/
  `$CALDB`; `seed_extra_fs_paths()` preserves prior domain roots when the page is skipped.
- `setup` research_toolkit (page 4) helpers: `scaffold_toolkit_package(toolkit_root, domain)` writes a
  fresh `<domain>_utils` skeleton mirroring the §9.2 layout (human-half `lib/`, pure-data ai-half
  `skills/`, `tests/`); `init_toolkit_git(toolkit_root, branch="ai")` git-inits + sets identity +
  checks out the runtime's `ai` branch (§9.9); `stage_raw_code(src, runtime_dir, name)` copies raw
  domain code into `<runtime_dir>/staging/` (outside the worktree so `git clean -fd` can't delete it);
  `discover_toolkit()` suggests prior/sibling checkouts; `page_toolkit(...)` resolves `toolkit_root`
  via point / clone / start-fresh.

## Changelog
Compacted 2026-09-01, again 2026-09-24 (every entry), 2026-10-07 and 2026-10-19 (every entry);
detail in git history.

- 2026-05-26 -> 2026-06-25 — Phase 1 complete; Phase 2 begun: `check_required_env`, `preflight.py`
  (`run_preflight`), `hitl` + `human_handler`, `papers_dir()`, toolkit checks, `observations.py`.
- 2026-07-14 -> 2026-07-16 — Installer increments 0–4 (`*.example` config, `repl_init_py`/
  `analyze_probe` + `check_analyze_probe`, `install.sh`, `python -m auto_research.setup`); 5 deferred.
- 2026-07-16 -> 2026-07-22 — `env.ProviderRetryPolicy`/`provider_retry_policy()`, `env.toolkit_root()`.
- 2026-07-31 — `/stop` and Ctrl-C are one interrupt: `UserInterruptionError`,
  `RunControl.register_run_task`/`authoring_session`, `run_research(install_signal_handlers=)`.
- 2026-08-10 -> 2026-09-12 — Preflight `api_key_fallbacks`; keys derived from `config/models.yaml`
  (`key_env` wins), `env.provider_key_aliases(provider)`. 09-01: `LedgerCaps`/`ledger_caps()`.
- 2026-09-25 — [E12] `RunControl` inboxes carry `InboxEntry`; seat liveness, resumed- and
  background-task registries, `cancel_resumed()`.
- 2026-10-06 — Env plan B1/B4: `analysis_environment`, `host_environment`, `env_lock_dir`,
  `declared_externals`, `env_catalogue`, `agent_conda_dirs` → `agent_conda_dir`, `replay_environment`.
- 2026-10-17 — Ledger caps retired (`LedgerCaps`, `ledger_caps()`, `ledger_*_cap`; user ruling: every
  field whole); CLI `--report-format` accepts `html`, default the configured `report_formats`.
- 2026-10-18 — [D1] `InboxEntry` kind `requeued`, retired by [D5 review] (`RunControl.peek_inbox`,
  `WAKING_KINDS`); [D6 ruling] `RunControl.fatal`/`fail(exc)` end the run on a resumed seat's
  `ReactBudgetExhausted`, [D6 review] first failure only, via `_cancel_run`; [D8] `interrupt` a no-op
  after a stop request, `cancel_resumed(timeout=)` bounded, `RunControl.running`.
- 2026-10-27 — Preflight gains `toolkit_manifests` (`sandbox.registry.check_toolkit_manifests`).
  `run_control.InboxEntry.render` names the sender, the receipts a text names, and the address that
  answers it (a human steer and reply included).
- 2026-11-04 — A1: `document_parser_ready` reads each member's adapter-declared capability
  (`models.providers.media_capability`, documents `"parsed"`), not a config flag.
- 2026-11-04 — C2c: `RunControl.unclosed_sessions` (coding sessions whose close raised, closed
  again by the run's teardown).
- 2026-11-04 — D4: `env.runtime_data_dir` honours `AUTO_RESEARCH_RUNTIME_DIR` (`RUNTIME_DIR_ENV`),
  which tests already set but nothing read; the suite's conftest sets it for every deterministic
  test (live tests keep the configured dir). `sandbox.jail.check_os_jail_probe` takes its HOME from
  `jail_home("probe")` (it read `runtime_dir` from config directly); `benchmarks/science_bench`'s
  preflight refuses an inherited variable.
- 2026-11-04 — Z1d: `config_paths.model_capabilities_yaml()`; preflight `member_capabilities`
  (`models.capabilities.check_member_capabilities`).
- 2026-11-04 — Z1e: preflight `member_media` (`models.media_probe.check_member_media`).
- 2026-11-04 — H2: `check_catalog_health` names a root that is missing or unwritable and passes an empty one; `observations.OBSID_TREES` (mirror of the toolkit's `obsdb_catalog._domain.TREES`), `obsid_folder`, `obsid_mission`.
- 2026-11-04 — Z1f: `setup.page_preflight` meters its preflight like a run (own run id, ledger under the runtime dir, cost meter, spend printed with the ledger path).
- 2026-11-04 — H2 review: `obsid_folder` orders level-2 records newest first (toolkit 148b9bd), takes `mission=` and a lazy `index=` (`obsid_index`, one tree walk per call); `obsid_mission` walks up to the catalog for a folder recorded outside the tree; a parity test pins `OBSID_TREES == obsdb_catalog._domain.TREES` and `obsid_folder == locate`.
- 2026-11-04 — Z1g: `setup.page_preflight` reports a setup ledger it cannot write (`[setup_ledger]`) with the checklist, counting the spend in memory.
