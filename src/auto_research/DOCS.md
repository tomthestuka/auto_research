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
| `config_paths.py` | — | Where the four config files are, resolved when asked for. `config_dir()` answers with the repository's own `config/` unless `AUTO_RESEARCH_CONFIG_DIR` names another, so a process can be run against a different configuration and every reader agrees. Before it, eleven modules each computed the location relative to their own source file. |
| `env.py` | — | Launch-time `.env` loading and required-env check (`check_required_env()`, whose model-key requirements are **derived from `config/models.yaml`**: one group per provider a role is configured with, or the exact variable a member names with `key_env`); small `orchestration.yaml` accessors (`runtime_data_dir`, `papers_dir`, `sandbox_call_timeout_default`, `provider_retry_policy`, `private_variables` — `{variable: separator}` from `env_externals.<name>.private`, refused unless a variable name and a non-empty separator, a name outside `_RESERVED_PRIVATE`, and declared once, also up to case). |
| `setup.py` | — | The install-script Python stage (`python -m auto_research.setup`, run by `install.sh`): discovers defaults, prompts (interactive `questionary`) or reads `--answers` (headless), then writes the gitignored `config/orchestration.yaml` from its `*.example` template (deriving `agent_fs_allow` from the answered paths) and runs the preflight smoke test. |
| `preflight.py` | — | Launch-time **checklist** (`Preflight` / async `run_preflight()`): runs every launch precondition (all no-LLM but the last, `member_media`, one tiny image and PDF per chat member, Z1e), and carries a warning tier (`PreflightWarning`) for a precondition that is satisfiable but never proven live — the launch continues and the caution is recorded (`.env` shape, API-key fallback usability, runtime DB schema health, author↔reviewer decorrelation, toolkit git identity/branch, the data stores, human environments, the default environment's private variables and `probe` stack smoke-test, human-channel ping) before any model client is built. |
| `hitl` | 5 | Raise-to-human clarification channel: the injectable `HumanHandler`, the CLI/Slack/WhatsApp connectors, the CLI-vs-library resolver, and the launch-time channel ping. |
| `schemas` | 1 | Pydantic v2 message protocol between agents. |
| `models` | 2 | Provider abstraction + role→model registry from `config/models.yaml`. |
| `observability` | 3 | LangSmith tracing + per-call cost logging hooked into the provider layer. |
| `storage` | 4 | SQLite layer (papers, memory, query_log; embedding column reserved for Phase 2). |
| `store` | P3 0b | The data stores (S3 objects + a Postgres catalog): the `stores:` block, a local store's servers (versitygw + IAM, Postgres) started by the runtime, the `store_*` preflight items, per-agent keys (`keys`), the catalog (`catalog`, `query`), receipts from the store's records (`records`), the reconciler (`reconcile`), the CI pipeline's throwaway store (`ci`) and the human's CLI (`python -m auto_research.store`). Its own `store/DOCS.md`. |
| `sources` | 5 | `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. |
| `retrieval` | 6 | `RetrievalBackend` (text search now, vector search stubbed). |
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
  `code_gate_yaml()` / `model_capabilities_yaml()` — the configuration
  directory and the four files in it, resolved **per call** (the capability table, Z1d, describes
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
  `build_default_preflight(human_handler)`): `check_config_keys` (no retired `orchestration.yaml`
  key, each named with what to do instead; an `agent_fs_allow` that is neither empty nor names `/`),
  `check_required_env`,
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
  `sandbox.jail.check_os_jail_available` (this host runs the bwrap jail; every runtime process runs
  inside it, so a host that cannot is refused with the reason, D25) and
  `sandbox.jail.check_os_jail_harness` (the author harness's CLI is reachable inside it),
  `toolkit_contract.check_toolkit_contract` (the `toolkits:` block validates, its root is a
  directory holding the template package and a `README.md` at its root and in every package, the
  domain briefing the roles read — every runtime path hint derives from the contract),
  `sandbox.merge_flow.check_toolkit_git_identity` (the toolkit checkout has a git
  commit identity, so a runtime tool commit can't fail mid-flow),
  `store.health` (`store_config`, `store_servers`, `store_admin`, `store_buckets`, `store_catalog`,
  registered after `toolkit_manifests`: the `stores:` block, a local store's servers started and
  answering, the admin key, every bucket versioned, the catalog's schema; `store/DOCS.md`), then
  `store.reconcile.check_store_integrity` (`store_integrity`: no recorded object of a committed or
  verified product missing or changed on disk; checks only), `store.ci.check_ci_leftovers`
  (`store_ci_leftovers`: a notice, never a failure, naming the CI pipeline's leftover throwaway folders,
  their size and `python -m auto_research.store ci-sweep`),
  `sandbox.registry.check_toolkit_manifests` (every package of the contract's `package_glob` imports,
  exposes a `TOOL_MANIFEST` and keys each tool `<name>.<package>.<tool>`; a failing one would reach
  the analysts as missing tools and nothing else),
  `env.check_human_envs` (each `human_envs` prefix has a Python; every toolkit definition's `base`
  is declared), `check_private_variables` (each declared private variable is set, with its
  separator, by the default environment's activation, read back by `runner.analysis_activation_env`),
  `sandbox.runner.check_analysis_probe` (runs the default environment's `env.yaml` `probe` as a
  unit's REPL starts, inside the jail; no probe, nothing runs), and — when a channel is
  resolved — `hitl.make_human_channel_check` (pings the channel, green only on a reply). `run_research()`
  resolves the handler then awaits this before any LLM; aggregates every failure into one
  `PreflightError`.
- `auto_research.toolkit_contract` — the toolkit contract: every fact the orchestration relies on
  about a toolkit, read from the `toolkits:` block of `orchestration.yaml` (a list of exactly one
  until Phase 3 step 2). Code reads the toolkit root, package glob, template package, authoring
  branch, fast-test selection and the declared name through it only. The declared name decides
  evidence (`is_toolkit_producer`, used by `provenance.store.is_evidence`): a producer
  `<name>.<package>.<tool>` of a declared toolkit is a toolkit result. A name that is no identifier,
  or equals or prefixes a harness producer (`provenance.store.HARNESS_PRODUCERS`), is refused when
  the contract is built, so no configuration widens evidence to what a role only read (§2b).

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
  `credentials` holds the run's store keys (`store.keys.RunKeys`, S4), revoked by the run's teardown
  and a stop's cleanup once nothing holding them runs (leaf to root); a dead unit's alone through
  `credentials.revoke(unit)`.
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
  derived from the answered paths (toolkit_root/analyze_env/runtime_dir + domain-dep
  `extra_paths` + `/tmp`); placeholders dropped, deduped, `auto_research` never included.
- `setup.render_orchestration(template_text, values, fs_allow) -> str` — a line-based renderer that
  fills the `*.example` template values while preserving comments (the `agent_fs_allow` block is
  regenerated); `setup.write_configs(values, extra_fs_paths=None) -> Path` writes the live
  `orchestration.yaml`.
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

- `toolkit_contract.ToolkitContract` (frozen, `extra="forbid"`) — `name` (identifier, no harness
  producer's first segment), `root` (expanded), `package_glob` (default `*_utils`, top-level only),
  `template_package` (matches the glob), `authoring_branch` (required), `default_env` (required: the environment, a folder under `envs/`, a unit's own REPL and shell run in and where a package naming none runs and is tested; the launch refuses one without `envs/<default_env>/env.yaml`), `test_classes:
  ToolkitTestClasses` (`fast`, default `"not slow and not live"`: the pytest `-m` expression the
  gate's regression run selects; `live`, default `"live"`: the expression the gate deselects from a
  change's own tests, its run having no network; `data`, default `"data"`: the marker of a test
  reading a named data set, `data("<name>")`). Methods: `is_package(name)`, `packages(root=None)`
  (top-level folders matching the glob), `owns(produced_by)` (the producer starts `<name>.`; the registry serves only keys `<name>.<package>.<tool>`), `key_problem(key, package)` (why a manifest key is outside the
  contract, or `None`).
- `toolkit_contract.test_data(config=None) -> dict[str, Path]` — the host's data sets for the
  toolkit's `data` tests, `orchestration.yaml` `test_data: {name: folder}`; raises on a name outside
  `[a-z][a-z0-9_]*` or a folder that is not absolute (an absent folder is allowed: its tests skip, and
  the gate fails a change that leaves one skipped). Checked at launch by `check_toolkit_contract`.
  `data_variable(name)` → `TOOLKIT_DATA_<NAME>` (`DATA_VARIABLE_PREFIX`), the variable the gate sets to
  the folder and the toolkit's root `conftest.py` reads.
- `toolkit_contract.data_needs(contract=None) -> (dict[str, list[str]], list[str])` — `({data set:
  [files]}, problems)`: every data mark in the toolkit's package sources, read with `ast` (no test
  collected or imported): `pytest.mark.<test_classes.data>(...)`, `mark.<data>(...)` with `mark`
  imported from pytest under any name, or a name bound to either; a name that is not one string
  literal is a problem, `file:line`. `check_toolkit_test_data()` — launch preflight `toolkit_test_data`:
  every named data set is declared in `test_data` and every declared folder exists.
- `toolkit_contract.toolkits(config=None) -> list[ToolkitContract]` — without `config`, the
  process's **bound** contract: bound by the first successful read of the configured
  `orchestration.yaml` (the launch preflight, in a run) and kept for the process's life, so the
  evidence predicate, the run (`graph.runtime`) and every reader see one contract; with `config`,
  that configuration parsed and never bound (a benchmark's trial config, a probe given a path).
  Raises `ValueError` when the block is absent or holds other than one entry, a `ValidationError` on
  a bad field. `toolkit(config=None)` — the one entry. `bind(config)` — bind a configuration held as
  data; `reset()` — unbind (tests: `tests/conftest.py` resets around every test).
  `is_toolkit_producer(produced_by) -> bool`. `RESERVED_FOLDER = "envs"`: `package_glob` may not
  match it, nor be `*`, contain `**` or `/`; package and toolkit names are ASCII identifiers.
  `harness_names() -> frozenset[str]` — the names a toolkit may not take.
  `check_toolkit_contract()` — the launch check (registered as `toolkit_contract`).

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
- 2026-11-09 — Phase 3 step 0a W5 (D19): `env.private_variables(config=None)` reads the variables the declared externals give every process and call a private folder (`env_externals.<name>.private: {VAR: separator}`); applied by `sandbox.runner.private_folders` (sandbox DOCS).
- 2026-11-09 — W5 follow-up: preflight check `private_variables` (`preflight.check_private_variables` over `env.private_variables`, registered after `required_env`) refuses a malformed `env_externals.<name>.private` declaration at launch.
- 2026-11-09 — W5 review: `env.private_variables` refuses a non-mapping, reserved names (`_RESERVED_PRIVATE`: `AR_PRIVATE_FOLDERS`, `PWD`, `MARK`, `HOME`, `PATH`, `TMPDIR`) and a variable declared twice or up to case; `preflight.check_private_variables` also sources the analysis activation (`sandbox.runner.analysis_activation_env`, the analyze probe's) and refuses a declared variable it leaves unset or sets without the separator, naming the external.
- 2026-11-09 — Phase 3 step 0a W2: new `toolkit_contract` (the toolkit contract, a `toolkits:` block replacing `toolkit_root` and `tool_author_branch`; no default branch). Every reader goes through it: the evidence predicate (`provenance.store.is_evidence`, `HARNESS_PRODUCERS` and the name guard B2), `env.toolkit_root`, `env.env_catalogue` (definitions from the contract; none marked superseded for a config without the block), `observations._toolkit_root`, the registry's package discovery and manifest-key check, the gate's package tests and fast-test selection, the coder's template package, the run's root and branch, the jail probe, `graph.builder`. Preflight: `toolkit_contract` replaces `toolkit_root_configured`.
- 2026-11-09 — W2 review (M1, M2, L1–L5): the contract is bound once per process (`bind`, `reset`; no per-call re-read), and the run reads the bound one (`toolkit()`); `package_glob` refuses `*`, `**`, `/` and a glob matching `envs`; package and toolkit names are ASCII identifiers. Tests: the producer-literal scan against `HARNESS_PRODUCERS`, wrong-package and four-segment keys, a non-default glob through the registry and the gate.
- 2026-11-09 — W2 addition (main): `ToolkitContract.default_env` (required; checked at launch against `<root>/envs/<default_env>/env.yaml`); live config and `.example` name `analysis` (W4 creates the definition; until then the live launch check fails, expected mid-step).
- 2026-11-09 — W2 re-review: the lead's catalog kind is the bound contract's (`react._catalog_kind()`, `<name>.catalog_utils.data_catalog`, read when used; the tool's agent-facing name `DATA_CATALOG_TOOL` stays until step 3); `key_problem` requires an ASCII tool segment; `benchmarks/conftest.py` resets the contract around every benchmark test; the producer scan is closed (every unresolved producer argument is a listed run-time site).
- 2026-11-09 — Phase 3 step 0a W4: `env.analysis_environment(config=None)` is the toolkit's default environment (`default_env` of the bound contract, via `env.default_definition()`), resolved through `env_build.build`; new `env.human_environments(config=None)` (`human_envs`: name → bare human `Environment`) supplies every `base_envs`; new preflight `env.check_human_envs`; `env_catalogue` lists built and derived environments only (no bare human one). Preflight: `analysis_probe` (`runner.check_analysis_probe`) replaces `analyze_probe` and `os_jail_probe`; `private_variables` now runs after the toolkit checks, on the default environment's activation. Readers take the contract from the bound `toolkit()` only; `graph.runtime.bind_run_contract` binds it per run before the preflight. The installer helpers above still name the retired keys (frozen, D16).
- 2026-11-09 — Phase 3 step 0a W3: `ToolkitTestClasses.live` and `.data`; `toolkit_contract.test_data`, `data_variable`, `DATA_VARIABLE_PREFIX`; `check_toolkit_contract` validates `test_data`.
- 2026-11-09 — W4 review L4: `env.human_environments` refuses a name that is not an environment name and a prefix inside `agent_conda_dir()`; `check_human_envs` and `default_definition` name the unreadable `env.yaml` and join its problems into one sentence (`_problems_text`).
- 2026-11-09 — W3 review: `toolkit_contract.data_needs`, `check_toolkit_test_data` (preflight `toolkit_test_data`).
- 2026-11-09 — W3 re-review: `data_needs` reads every spelling of the data mark and reports non-literal names; `check_toolkit_contract`'s docstring names what `toolkit_test_data` checks.
- 2026-11-09 — Phase 3 step 0a W10 (ruling D25): jailing is unconditional. `preflight`'s `os_jail_available` fails the launch (was a warning) on a host that cannot run bwrap; `env.analysis_environment` and `env.replay_environment` build and replay jailed always (the `jailed` argument of `env_build` is gone); `provenance.capture._allow_listed` always excludes the host `/tmp` (a list naming only it admits nothing).
- 2026-11-09 — W10 review fixes: `preflight.check_config_keys` (registered first, `config_keys`) refuses each retired `orchestration.yaml` key with its replacement (`RETIRED_KEYS`: `agent_os_jail`, `mcp_servers`, `toolkit_root`, `tool_author_branch`, `analyze_env`, `analyze_ini_sh`, `repl_init_py`, `analyze_probe`, `gate_readonly_roots`) and an empty `agent_fs_allow`.
- 2026-11-09 — W10 sweep (D25): `observations.check_catalog_health` runs `obsdb_catalog.healthcheck` through `runner.call_in_jail` (`_import_healthcheck` and its `sys.path` insertion are gone). Sweep of `src` for toolkit code run in the orchestrator: `ToolkitRegistry.load`/`reload` (now read manifests as data), `react.execute_data_catalog`/`data_catalog_tool_def` (jailed call, manifest as data), `runner.run_tool` (deleted), this check (jailed). What remains is jailed by construction: the REPL script (`persistent.py`), the gate's recorder plugin (`returns.py`, inside the gate's jailed pytest), and the reproduce code `report/html.py` writes for a human (text, never run). Guard: `tests/test_toolkit_contract.py::test_the_launch_path_imports_no_toolkit_module_into_the_orchestrator`.
- 2026-11-09 — W10 sweep re-review: `check_config_keys` words each retired key as what to do (`agent_os_jail` "is retired (every runtime process runs in the jail); delete the key") and refuses an `agent_fs_allow` naming `/`.
- 2026-11-09 — W6 review M2: `check_toolkit_contract` refuses a toolkit missing a `README.md` at its root or in any package (the roles read them as the domain briefing; the prompts point to them).
- 2026-11-10 — Phase 3 step 0b S1: new package `store` (`store/DOCS.md`); `preflight.build_default_preflight` registers `store_config`, `store_servers`, `store_admin`, `store_buckets`, `store_catalog` after `catalog_health`.
- 2026-11-10 — Phase 3 step 0b S4: `RunControl.credentials` (the run's store keys, revoked at teardown); `python -m auto_research.store status|stop` (`store/DOCS.md`).
- 2026-11-10 — Phase 3 step 0b S6: `preflight.build_default_preflight` registers `store_integrity` (`store.reconcile.check_store_integrity`) after `store_catalog` (`store/DOCS.md` ## Reconciler).
- 2026-11-10 — Phase 3 step 0b S9: the obs-db leaves the orchestration (not backwards compatible). `observations.py` is deleted (the obs-db root loader, `check_catalog_health`, `OBSID_TREES`, `obsid_folder`/`obsid_index`/`obsid_mission`), with `config/observations.yaml(.example)` and `config_paths.observations_yaml`; preflight's `catalog_health` is gone (`store_integrity` checks the store's bytes against its catalog). `setup.py` (frozen installer, D16): only what broke is removed — the obs-db root question, `render_observations`, the observations file it wrote (`write_configs` returns the one path) and the root in `derive_fs_allow`. The lead reads a store's catalog through `catalog_query` (`store/query.py`, `agents/DOCS.md`).
- 2026-11-11 — S12 (docs): the module table's `store` row lists every submodule built in step 0b (keys, catalog, query, records, reconcile, ci, the CLI).
- 2026-11-11 — Phase 3 step 0b S11: `store/ingest.py` (the human's ingest of files on disk as products) and the CLI actions `start`, `ingest`, `produce` (`store/DOCS.md` `## Migration and ingest`).
- 2026-11-11 — S7 follow-up (main's ruling 1): `build_default_preflight` registers `store_ci_leftovers` after `store_integrity`, a notice when the CI pipeline's throwaway stores left folders behind.
