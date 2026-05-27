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
| `schemas` | 1 | Pydantic v2 message protocol between agents. |
| `models` | 2 | Provider abstraction + role→model registry from `config/models.yaml`. |
| `observability` | 3 | LangSmith tracing + per-call cost logging hooked into the provider layer. |
| `storage` | 4 | SQLite layer (papers, memory, query_log; embedding column reserved for Phase 2). |
| `sources` | 5 | `QuerySource` interface + `arxiv`, `nasa_ads`, `web` (Tavily) modules. |
| `retrieval` | 6 | `RetrievalBackend` (text search now, vector search stubbed). |
| `agents` | 8 | Thin per-role wrappers that load their `prompts/<role>.md` and call the provider layer. |
| `consensus` | 9 | `ConsensusStrategy` — debate-to-converge with lead adjudication. |
| `graph` | 10 | LangGraph assembly + the two capped feedback edges. |
| `sandbox` | 11 | Subprocess sandbox + `research_toolkit` propose/branch/review/merge flow. |
| `cli` | 12 | `python -m auto_research "..."` entry point. |

Async-first throughout ([CLAUDE.md §9](../../CLAUDE.md#9-tech-stack--conventions)).

## Interfaces / Contracts
- Package exports its version as `auto_research.__version__`.
- Each submodule documents its own interface in its `DOCS.md`. Cross-module
  contracts go through `auto_research.schemas`.

## Dependencies
- External: `pydantic`, `langgraph`, `langsmith`, provider SDKs (`anthropic`,
  `openai`, `google-genai`), source clients (`arxiv`, `ads`, `tavily-python`),
  `python-dotenv`, `pyyaml`. Pinned in `environment.yml`.
- Internal: imports from the sibling `research_toolkit` package at runtime
  (analyst tools); that package is managed/installed separately by the human.

## Dictionary-level reference
- `__version__: str` — package version. `"0.0.0"` until the first non-skeleton
  release.

## Changelog
- 2026-05-27 — Step 1 completed: `auto_research.schemas` now exports the
  Pydantic v2 inter-agent protocol and schema validation tests pass.
- 2026-05-26 — Package seeded with submodule folders (each with its own `DOCS.md`)
  during step 0. No code beyond the version constant.
