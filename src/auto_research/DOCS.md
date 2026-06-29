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
| `env.py` | — | Launch-time `.env` loading and required-env check (`check_required_env()`). |
| `preflight.py` | — | Launch-time deterministic **checklist** (`Preflight` / async `run_preflight()`): runs every no-LLM precondition (`.env` shape, runtime DB schema health, author↔reviewer decorrelation, toolkit git identity/branch, data-catalog health, human-channel ping) before any model client is built. |
| `hitl` | 5 | Raise-to-human clarification channel: the injectable `HumanHandler`, the CLI/Slack/WhatsApp connectors, the CLI-vs-library resolver, and the launch-time channel ping. |
| `schemas` | 1 | Pydantic v2 message protocol between agents. |
| `models` | 2 | Provider abstraction + role→model registry from `config/models.yaml`. |
| `observability` | 3 | LangSmith tracing + per-call cost logging hooked into the provider layer. |
| `storage` | 4 | SQLite layer (papers, memory, query_log; embedding column reserved for Phase 2). |
| `sources` | 5 | `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. |
| `retrieval` | 6 | `RetrievalBackend` (text search now, vector search stubbed). |
| `observations.py` | 8 | Single `obs_db_root` loader (`config/observations.yaml`) that discovers per-mission `<root>/<MISSION>` subdirs from disk, plus the read-side data catalog and the `catalog_health` preflight check. |
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
- `auto_research.preflight.run_preflight(*, human_handler=None)` (async) is the launch-time
  entry that runs the whole deterministic checklist (registered via
  `build_default_preflight(human_handler)`): `check_required_env`,
  `storage.check_database_health` (the runtime SQLite DB exists with its full schema,
  created if absent — so a fresh run's first `text_search` can't hit `no such table: papers`),
  `sandbox.review.check_author_reviewer_decorrelation`,
  `sandbox.merge_flow.check_toolkit_root_configured` (`toolkit_root` is set in
  `config/orchestration.yaml` and is a directory — every runtime path hint derives from it),
  `sandbox.merge_flow.check_toolkit_git_identity` (the `research_toolkit` checkout has a git
  commit identity, so a runtime tool commit can't fail mid-flow),
  `observations.check_catalog_health` (every mission's local data catalog is healthy via
  `research_toolkit`'s detect-only `obsdb_catalog.healthcheck`), and — when a channel is
  resolved — `hitl.make_human_channel_check` (pings the channel, green only on a reply). `run_research()`
  resolves the handler then awaits this before any LLM; aggregates every failure into one
  `PreflightError`.
- `auto_research.observations.load_observations_config()` loads the single `obs_db_root`
  from config and discovers missions from disk (`<root>/<MISSION>` subdirs marked by a
  `catalog.json`); mission names are not hardcoded in agent wrappers. `config.missions` is
  the discovered map; `config.mission(name)` resolves one mission's path by case-insensitive
  subdir match. `read_catalog(mission)` / `local_obsids(mission, min_level=…)` read the
  per-mission data catalog (`<obs_db_path>/catalog.json`, a `DataCatalog`/`CatalogEntry`/
  `DataProduct` model) — the read side of the data inventory the authored toolkit tools write.
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
- `preflight.Preflight` — an ordered registry of launch-time checks. `.register(name, fn)`
  (also usable as a decorator), `.names()`, async `.run_checklist()` (runs all — sync or async
  checks — and aggregates failures into `PreflightError`). `build_default_preflight(human_handler=None)`
  assembles the project checklist; async `run_preflight(*, human_handler=None)` builds and runs
  it. Each check is a zero-arg fn that raises on failure and lives in its own module.

## Changelog
- 2026-05-26 -> 2026-06-12 — Phase 1 completed and verified (schemas, providers, observability,
  storage/retrieval, sources, prompts, agents, consensus, graph/runtime, sandbox, report, CLI,
  notebook, env checks, config-driven `runtime_dir`). Phase 2 active: off-repo scratch,
  `ToolSpec`/merge-flow/gate scaffolding, `research_toolkit` the only runtime-authored repo.
- 2026-06-12 -> 2026-06-18 — **Launch preflight + `hitl` + observations (steps 5–7).** Centralized
  `.env` loading in `check_required_env`; `preflight.py` (`Preflight` checklist owning every no-LLM
  launch check, run by `run_checklist`; `run_research`/CLI call `run_preflight`); made it **async**
  with the `human_channel` ping; added the `hitl` raise-to-human channel + `human_handler`/
  `human_channel`. `papers_dir()` helper. Registered toolkit git-identity/branch-clean checks.
  `observations.py` read-side data catalog (`DataCatalog`/`read_catalog`/`local_obsids`, written by
  authored tools, JSON contract in `prompts/coder.md`) + `check_catalog_health` (delegates to
  `research_toolkit`'s detect-only healthcheck; hard-blocks launch on drift). `observations.yaml`
  → single `obs_db_root`, missions discovered from disk. Analyst env spec `environment-analyst.yml`.
- 2026-06-22 -> 2026-06-25 — **Resilience + preflight UX.** `__init__` mutes two non-actionable
  Pydantic v2 warning families (narrowly). `env.sandbox_call_timeout_default()`. `database_health`
  preflight (creates the SQLite schema if absent, so first `text_search` doesn't die).
  `toolkit_root_configured` preflight (fix-list #2); `run_checklist` prints one progress line per
  check (legible blocking-ping). `check_catalog_health` renders a friendly per-mission drift summary
  (`_summarize_drift`) instead of a raw dict.
