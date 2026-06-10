# `auto_research/` — orchestration package

## Purpose
Top-level Python package for the orchestration half of the system: the LangGraph
graph, agent role wrappers, schemas, storage, providers, and the literature
sources. The system's runtime **cannot** modify this code; new domain tools land
in the sibling `research_toolkit` package via the gated flow in
[CLAUDE.md §8](../../CLAUDE.md#8-runtime-tool-authoring--sandbox).

## Architecture
A src-layout package. Submodules are added in the order set by
[CLAUDE.md §14](../../CLAUDE.md#14-build-order-phase-1):

| Submodule | Step | Role |
|---|---|---|
| `env.py` | — | Launch-time required-env check (`check_required_env()`). |
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
| `sandbox` | 11 | Subprocess sandbox + `research_toolkit` propose/branch/review/merge flow. |
| `monitor` | 12 | Async-safe run monitor with 1-10 chatter, JSONL logs, and team-block Markdown reports. |
| `cli` | 12 | `python -m auto_research "..."` entry point. |

Async-first throughout ([CLAUDE.md §9](../../CLAUDE.md#9-tech-stack--conventions)).

## Interfaces / Contracts
- Package exports its version as `auto_research.__version__`.
- Package exports monitored library entry points: `run_research()` returns a
  `ResearchRun` with final state and monitor; `run_request()` remains the
  verdict-only compatibility entry.
- Package exports `RunMonitor` for notebook and test instrumentation.
- Each submodule documents its own interface in its `DOCS.md`. Cross-module
  contracts go through `auto_research.schemas`.
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
- `run_research(request, ..., chatter=10, monitor_jsonl=None) -> ResearchRun` —
  monitored library entry for CLI/notebook use.
- `run_request(request, ...) -> ReviewVerdict` — compatibility wrapper returning
  only the final verdict.
- `ResearchRun` — result object with request, final verdict, final graph state,
  and monitor.
- `RunMonitor` — async-safe event collector and renderer from
  `auto_research.monitor`.

## Changelog
- 2026-05-27 — Step 8 completed: `auto_research.agents` now exposes async
  role wrappers for reviewer, librarian I/O, librarian searcher, analyst
  worker, and analyst lead.
- 2026-05-27 — Step 9 completed: `auto_research.consensus` now exposes a
  bounded debate-to-converge strategy for analyst workers and lead synthesis.
- 2026-05-27 — Clarified that step 8 validates role plumbing rather than full
  production capability for roles whose tools are still deferred.
- 2026-05-27 — Added generic observation-location config loading and document
  attachment support for librarian I/O ingestion.
- 2026-05-27 — Step 7 completed: runtime prompts now exist for all Phase 1 roles,
  with a guarded loader in `auto_research.agents`.
- 2026-05-27 — Step 6 completed: `auto_research.retrieval` now exposes SQLite
  text search over papers/memory and a `vector_search()` Phase 2 stub.
- 2026-05-27 — Step 5 completed: `auto_research.sources` now exposes the shared
  `QuerySource` protocol plus arXiv, NASA ADS, and Tavily source adapters.
- 2026-05-27 — Step 4 completed: `auto_research.storage` now provides SQLite
  schema initialization, paper/memory/query-log writes, local PDF ingestion, and
  reserved embedding space.
- 2026-05-27 — Step 3 completed: provider calls now flow through
  `auto_research.observability` for optional LangSmith tracing and JSONL cost
  ledger logging.
- 2026-05-27 — Corrected step 2 so model roles resolve to groups instead of
  single handles; `auto_research.models` now exposes `ModelGroupHandle`.
- 2026-05-27 — Step 2 completed: `auto_research.models` now loads
  `config/models.yaml`, resolves roles to provider handles, and exposes
  provider-neutral call/usage types.
- 2026-05-27 — Step 1 completed: `auto_research.schemas` now exports the
  Pydantic v2 inter-agent protocol and schema validation tests pass.
- 2026-05-26 — Package seeded with submodule folders (each with its own `DOCS.md`)
  during step 0. No code beyond the version constant.
- 2026-06-09 — Added `auto_research.env` (`check_required_env()`, `runtime_data_dir()`): crashes at launch when any required env var is missing; called in `cli/main.py` after `load_dotenv()` and at the top of `graph/runtime.run_research()`. Runtime data directory moved from `AUTO_RESEARCH_RUNTIME_DIR` env var to `config/orchestration.yaml::runtime_dir`; all four callers (`workspace`, `monitor`, `observability`, `storage`) now use `env.runtime_data_dir()`. `sandbox/runner._source_ini_env` now raises `RuntimeError` on failure instead of falling back to `os.environ`.
- 2026-05-28 — Step 12: added `auto_research.monitor`, exported
  `run_research()` / `ResearchRun` / `RunMonitor`, and kept `run_request()` as
  the verdict-only compatibility entry.
