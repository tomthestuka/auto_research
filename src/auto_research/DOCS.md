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
| `preflight.py` | — | Launch-time deterministic **checklist** (`Preflight` / async `run_preflight()`): runs every no-LLM precondition (`.env` shape, author↔reviewer decorrelation, human-channel ping) before any model client is built. |
| `hitl` | 5 | Raise-to-human clarification channel: the injectable `HumanHandler`, the CLI/Slack/WhatsApp connectors, the CLI-vs-library resolver, and the launch-time channel ping. |
| `schemas` | 1 | Pydantic v2 message protocol between agents. |
| `models` | 2 | Provider abstraction + role→model registry from `config/models.yaml`. |
| `observability` | 3 | LangSmith tracing + per-call cost logging hooked into the provider layer. |
| `storage` | 4 | SQLite layer (papers, memory, query_log; embedding column reserved for Phase 2). |
| `sources` | 5 | `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. |
| `retrieval` | 6 | `RetrievalBackend` (text search now, vector search stubbed). |
| `observations.py` | 8 | Generic loader for mission observation database/archive paths from `config/observations.yaml`. |
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
  `sandbox.review.check_author_reviewer_decorrelation`, and — when a channel is resolved —
  `hitl.make_human_channel_check` (pings the channel, green only on a reply). `run_research()`
  resolves the handler then awaits this before any LLM; aggregates every failure into one
  `PreflightError`.
- `auto_research.observations.load_observations_config()` loads generic
  mission observation locations from config; mission names are not hardcoded in
  agent wrappers.

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
- `preflight.Preflight` — an ordered registry of launch-time checks. `.register(name, fn)`
  (also usable as a decorator), `.names()`, async `.run_checklist()` (runs all — sync or async
  checks — and aggregates failures into `PreflightError`). `build_default_preflight(human_handler=None)`
  assembles the project checklist; async `run_preflight(*, human_handler=None)` builds and runs
  it. Each check is a zero-arg fn that raises on failure and lives in its own module.

## Changelog
- 2026-05-26 -> 2026-06-12 — Phase 1 completed and verified: schemas, providers,
  observability, storage/retrieval, sources, prompts, agents, consensus, graph/runtime,
  sandbox, report, CLI, notebook, environment checks, and config-driven `runtime_dir`.
  Phase 2 is active: sandbox scratch is off-repo, `ToolSpec`/merge-flow/gate scaffolding
  exists, and `research_toolkit` remains the only runtime-authored repo.
- 2026-06-12 — Compacted changelog and refreshed stale CLAUDE section links; no code
  changes.
- 2026-06-15 — Centralized project `.env` loading in `check_required_env()` so
  library, CLI, notebook, and test paths validate against the same launch environment.
- 2026-06-15 — Added `preflight.py`: a `Preflight` checklist class that owns every
  launch-time deterministic check (each living in its home module, registered centrally,
  run together by `run_checklist()`). `run_research()` and the CLI now call `run_preflight()`
  instead of `check_required_env()` directly; the first registered extra check is the Phase-2
  author↔reviewer provider decorrelation.
- 2026-06-15 — Phase 2 step 5: added the `hitl` package (raise-to-human channel) and made the
  preflight **async** with a parameterized `human_channel` ping (green only when the resolved
  channel replies). `run_research` gained `human_handler`/`human_channel`; the CLI owns the
  full async preflight.
