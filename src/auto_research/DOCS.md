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
| `env.py` | — | Launch-time `.env` loading and required-env check (`check_required_env()`); small `orchestration.yaml` accessors (`runtime_data_dir`, `papers_dir`, `sandbox_call_timeout_default`, `provider_retry_policy`). |
| `setup.py` | — | The install-script Python stage (`python -m auto_research.setup`, run by `install.sh`): discovers defaults, prompts (interactive `questionary`) or reads `--answers` (headless), then writes the gitignored `config/orchestration.yaml` + `observations.yaml` from their `*.example` templates (deriving `agent_fs_allow` from the answered paths) and runs the preflight smoke test. |
| `preflight.py` | — | Launch-time deterministic **checklist** (`Preflight` / async `run_preflight()`): runs every no-LLM precondition (`.env` shape, runtime DB schema health, author↔reviewer decorrelation, toolkit git identity/branch, data-catalog health, `analyze_probe` stack smoke-test, human-channel ping) before any model client is built. |
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
- `auto_research.env.check_required_env()` loads the repo-root `.env` with
  `override=False` and validates required provider/source keys before SDK clients
  are constructed.
- `auto_research.preflight.run_preflight(*, human_handler=None, monitor=None)` (async) is the
  launch-time entry that runs the whole deterministic checklist (registered via
  `build_default_preflight(human_handler)`): `check_required_env`,
  `storage.check_database_health` (the runtime SQLite DB exists with its full schema,
  created if absent — so a fresh run's first `text_search` can't hit `no such table: papers`),
  `sandbox.review.check_author_reviewer_decorrelation`,
  `sandbox.merge_flow.check_toolkit_root_configured` (`toolkit_root` is set in
  `config/orchestration.yaml` and is a directory — every runtime path hint derives from it),
  `sandbox.merge_flow.check_toolkit_git_identity` (the `research_toolkit` checkout has a git
  commit identity, so a runtime tool commit can't fail mid-flow),
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
  aggregating all missions; the human repairs via `obsdb_catalog.repair` out of band.

## Dependencies
- External: `pydantic`, `langgraph`, `langsmith`, provider SDKs (`anthropic`,
  `openai`, `google-genai`), source clients (`arxiv`, `ads`, `tavily-python`),
  `python-dotenv`, `pyyaml`. Pinned in `environment.yml`.
- Internal: imports from the sibling `research_toolkit` package at runtime
  (analyst tools); that package is managed/installed separately by the human.

## Dictionary-level reference
- `__version__: str` — package version. `"0.0.0"` until the first non-skeleton
  release.
- `run_research(request, ..., chatter=10, monitor_jsonl=None, report_formats=None) -> ResearchRun` —
  monitored library entry for CLI/notebook use.
- `run_request(request, ...) -> ReviewVerdict` — compatibility wrapper returning
  only the final verdict.
- `ResearchRun` — result object with request, final verdict, final graph state,
  and monitor.
- `RunMonitor` — async-safe event collector and renderer from
  `auto_research.monitor`.
- `check_required_env()` — loads `.env` without overriding existing process
  environment variables, then raises a clear error if any required key group is absent.
- `runtime_data_dir() -> Path` — resolves `runtime_dir` from `orchestration.yaml`
  (default `~/.auto_research`).
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
- 2026-05-26 -> 2026-06-12 — Phase 1 completed (schemas, providers, observability,
  storage/retrieval, sources, prompts, agents, consensus, graph/runtime, sandbox, report, CLI,
  notebook, env checks, config-driven `runtime_dir`); Phase 2 begun (off-repo scratch,
  `ToolSpec`/merge-flow/gate scaffolding, `research_toolkit` the only runtime-authored repo).
- 2026-06-12 -> 2026-06-18 — Launch preflight + `hitl` + observations (steps 5–7): centralized
  `.env` loading in `check_required_env`; `preflight.py` (`Preflight`/`run_preflight`, made async
  with the `human_channel` ping); the `hitl` raise-to-human channel + `human_handler`/
  `human_channel`; `papers_dir()`; toolkit git-identity/branch-clean checks; `observations.py`
  obs-db-location config + `check_catalog_health` (delegates to `research_toolkit`'s detect-only
  healthcheck); the catalog **read** now lives in `research_toolkit`'s `catalog_utils` (the
  `data_catalog` tool), not here; `observations.yaml` → single
  `obs_db_root` with missions discovered from disk; analyst env `environment-analyst.yml`.
- 2026-06-22 -> 2026-06-25 — Resilience + preflight UX: `__init__` mutes two Pydantic v2 warning
  families; `env.sandbox_call_timeout_default()`; `database_health` and `toolkit_root_configured`
  preflight checks; `run_checklist` prints one progress line per check; `check_catalog_health`
  renders a per-mission drift summary (`_summarize_drift`).
- 2026-07-14 — **Install-script increment 0/1.** Machine-local config (`orchestration.yaml`,
  `observations.yaml`, `sandbox/analyst_env.sh`, new `sandbox/repl_init.py`) is now gitignored with
  tracked `*.example` templates; launch config loaders raise a "copy the `.example` / run install.sh"
  message when a live file is missing. New `orchestration.yaml` keys `repl_init_py` (domain REPL
  session-setup hook, replacing the hardcoded XSPEC boot) and `analyze_probe` (stack smoke-test). Added
  the `sandbox.runner.check_analyze_probe` preflight check (runs `analyze_probe` under `analyze_env`;
  skips when unset). See `sandbox/DOCS.md` + `config/DOCS.md` for detail.
- 2026-07-14 — **Install-script increment 2: installer core.** Added `install.sh` (repo root; thin
  bash — bootstraps conda + the `auto_research` env, then execs the Python stage) and `setup.py`
  (`python -m auto_research.setup`): pages 1 (conda), 2 (analyst env from `environment-analyst.yml`),
  5 (paths), 6 (write configs from `*.example`, deriving `agent_fs_allow` — closes P4), 7 (preflight
  smoke test). Interactive via `questionary` (added to `environment.yml`), headless via `--answers`
  (fail-loud on a missing value). Pure renderers/`derive_fs_allow` are unit-tested
  (`tests/test_setup.py`). Pages 3 (domain deps + licensed/vendor tools) and 4 (research_toolkit) land
  in later increments; until then their keys keep prior/template values.
- 2026-07-14 — **Install-script increment 3: domain deps + bucket E.** `setup.py` page 3 collects typed
  domain-dependency entries (`shell_init`/`env_var`/`conda_pkg`/`pip_pkg`/`python_init`/`license_tool`)
  — headless from an answers `domain_deps` list, interactive via a discovery-seeded add loop — and
  materializes them: `analyst_env.sh` (boot commands, `$VAR`-expansion preserved) + `repl_init.py`
  (session snippets), installs conda/pip specs into `analyze_env`, contributes the `agent_fs_allow`
  domain roots (so HEADAS/CALDB/license dirs are auto-listed, not hand-synced), and assembles
  `analyze_probe`. **Bucket E** (`license_tool`) captures a human-installed vendor binary + license
  var/path (prompt-then-point). When the page is skipped, prior hooks + domain roots are preserved
  (`seed_extra_fs_paths`). Renderers/collectors unit-tested (`tests/test_setup.py`).
- 2026-07-14 — **Install-script increment 4: research_toolkit page.** `setup.py` page 4 resolves
  `toolkit_root` by scan → **point** (existing checkout) / **clone** (git URL) / **start-fresh**
  (scaffold a `<domain>_utils` skeleton mirroring the §9.2 layout, `git init` + `ai` branch, and stage
  the user's raw code under `<runtime_dir>/staging/` — *outside* the worktree). Scaffold templates live
  in `auto_research` and are written into the toolkit only at install-runtime (the build agent never
  edits `research_toolkit`). start-fresh prints the kickoff for the deferred (increment 5)
  `onboard_toolkit` authoring pipeline. Scaffold/git/staging tested into a temp dir
  (`tests/test_setup.py`). Increment 5 (`onboard_toolkit`) is deferred — it needs cross-domain
  collaborators.
- 2026-07-16 — `env.ProviderRetryPolicy` + `env.provider_retry_policy()`: config accessors for the
  new `orchestration.yaml` "Internet stability" section (`provider_retry_max` /
  `provider_retry_base_cooldown_s` / `provider_retry_max_cooldown_s`), backing the
  `models.retry` transient-provider retry layer (two live runs were killed by unhandled
  Anthropic 529s riding on the SDKs' seconds-scale default retries). Tests:
  `tests/test_provider_retry.py`.
- 2026-07-20 — **Catalog READ relocated to `research_toolkit` (option c).** The domain read of a
  mission's `catalog.json` (the old `DataCatalog`/`read_catalog`/`local_obsids` in `observations.py`)
  moved into `research_toolkit`'s new `catalog_utils` package (its `lib/catalog_read.py` reuses the
  trusted `obsdb_catalog.neutral.read`), surfaced as the `data_catalog` tool via `TOOL_MANIFEST`.
  `observations.py` now keeps only the obs-db-location config (`ObservationsConfig`/
  `load_observations_config`) + `check_catalog_health` (unchanged, still delegates to
  `obsdb_catalog.healthcheck`); its read side and the `json`/`datetime` imports are gone. New
  `env.toolkit_root()` accessor. Rationale: auto_research stays pure orchestration + generic infra;
  domain reads live in the toolkit beside the writer (§2). Read tests moved to
  `catalog_utils/tests`; `test_observations.py` keeps config/health + the tool-wiring tests.
