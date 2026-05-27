# auto_research

An automated research assistant: a team of LLM agents — orchestrated with **LangGraph** —
that takes a research request, searches the literature, runs analyses, and reviews the
results against prior work.

> **Status: Phase 1 — minimal modular skeleton (in progress).**
> Phase 1 delivers a full agent team running real LLM calls end-to-end, with vector
> retrieval, autonomous tool-merge, and the lead-scientist idea-generation group left as
> documented stubs for Phase 2.

Coding agents: read **`CLAUDE.md`** before doing anything. It is the single source of
truth for how this project is built.

---

## The loop

```
research request  →  reviewer dispatches literature + analysis tasks
               →  librarians search · analysts analyze (debate to converge)
               →  reviewer digests results, cross-checks prior publications
               →  human review
```

with two feedback edges (each capped at 3 iterations): the reviewer can send literature
back to the librarians ("insufficient / obsolete — search again") and analyses back to
the analysts ("rework this").

In Phase 1 the request enters from the human. In Phase 2, authorized lead-scientist
planning teams may also route requests through the same reviewer gate.

---

## LLM architecture

```
            ┌────────────────────────────────────────────┐
            │                   HUMAN                      │
            │        request in   ·   review out           │
            └───────────────────────┬──────────────────────┘
                                    │
                                    ▼
                     ┌───────────────────────────┐
                     │         REVIEWER          │   top tier
                     │  triage · dispatch ·      │
                     │  digest · cross-check     │
                     └────┬──────────────────┬───┘
       dispatch           │                  │           dispatch
   literature tasks       │                  │        analysis tasks
                          ▼                  ▼
        ┌──────────────────────┐   ┌────────────────────────────┐
        │      LIBRARIANS      │   │          ANALYSTS          │
        ├──────────────────────┤   ├────────────────────────────┤
        │ I/O team (medium)    │   │ workers (medium)           │
        │   arxiv · ADS · web  │   │   ┌── debate-to-converge ──┐│
        │          │           │   │   │        (≤ 3 rounds)    ││
        │          ▼           │   │   └───────────┬────────────┘│
        │ searchers (light)    │   │               ▼            │
        │   text · vector*     │   │ lead (top): adjudicate,    │
        │          │           │   │ maintain hygiene, own      │
        │          ▼           │   │ research_toolkit commits   │
        │   ┌──────────────┐   │   │               │            │
        │   │  SQLite +    │   │   │               ▼            │
        │   │  memory db   │   │   │ tools: xspec_utils, web,   │
        │   └──────────────┘   │   │ gated new tools            │
        └───────────┬──────────┘   └─────────────┬──────────────┘
                    │                             │
                    └────── results ──► REVIEWER ◄── results ──────┘
                                           │
          feedback ① paper-search review   (reviewer → librarians, ≤ 3)
          feedback ② analysis review       (reviewer → analysts,   ≤ 3)

        * vector search = Phase 2 stub (raises NotImplementedError)
```

Model groups are **config-driven** (`config/models.yaml`), spanning OpenAI, Anthropic,
and Google. No model identifier is hardcoded. Defaults: reviewer group = Opus 4.7,
GPT-5.5 high effort, and Gemini 3.5 Flash high thinking; analyst lead = Opus 4.7;
analyst worker group = Sonnet 4.6 and GPT-5.5 medium effort; librarian I/O group =
Sonnet 4.6, GPT-5.5 medium effort, and Gemini 3.5 Flash thinking; librarian searcher group =
GPT-5.4-nano.

---

## Repositories & trust boundary

Two separate repos. The split is a **safety mechanism**: the system's own code lives in a
repo it cannot write to, so it cannot modify itself — only extend the tool library.

```
  ┌──────────────────────────────────────┐      ┌──────────────────────────────┐
  │            auto_research             │      │       research_toolkit       │
  │  orchestration · agents · prompts ·  │      │  xspec_utils · LLM-authored  │
  │  schemas · sqlite · source modules   │      │  tools                       │
  │                                      │      │                              │
  │  commits: HUMAN ONLY                 │ ───► │  commits: human + runtime    │
  │  runtime access: READ-ONLY           │      │  (gated: propose → lead      │
  │  (cannot modify its own code)        │      │  review → human-approve      │
  │                                      │      │  merge → sandboxed run)      │
  └──────────────────────────────────────┘      └──────────────────────────────┘
```

**All git is done by the human** — staging, commits, pushes, for both repos. Coding
agents never run git. (The runtime toolkit-commit flow above is application behavior the
system performs on `research_toolkit`; it is not a coding agent committing.)

---

## Build status (Phase 1)

Statuses: **Todo** = in Phase 1 scope, not yet built · **Done** = built & documented.
Coding agents flip `Todo → Done` as work lands and keep this table synced (same
discipline as `DOCS.md`). Phase 2 items remain documented as deferred stubs in
`CLAUDE.md`.

| # | Status | What is being built |
|---|---|---|
| 0 | Done | Scaffolding & environment: repo skeleton, `environment.yml`, `.env.example`, `.gitignore`, `CLAUDE.md`, `AGENTS.md` symlink, `config/`, and seed `DOCS.md` files. |
| 1 | Done | Schemas: Pydantic v2 message models including `FigureRef`. |
| 2 | Done | Config + model registry: `config/models.yaml`, provider abstraction, and role-to-model resolution. |
| 3 | Done | Observability: LangSmith tracing and per-call cost logging in the provider layer. |
| 4 | Done | Storage: SQLite papers, memory, query log, and reserved embedding space. |
| 5 | Done | Librarian sources: shared `QuerySource` interface plus arXiv, NASA ADS, and Tavily web modules. |
| 6 | Done | Retrieval: `text_search()` over SQLite and `vector_search()` stub. |
| 7 | Done | Runtime prompts: one `prompts/<role>.md` file per role. |
| 8 | Done | Agent roles: reviewer, librarian I/O, librarian searcher, analyst worker, and analyst lead wrappers. |
| 9 | Todo | Consensus: debate-to-converge strategy with cap 3 and lead adjudication. |
| 10 | Todo | Orchestration: LangGraph graph with capped paper-search and analysis-result feedback edges. |
| 11 | Todo | Tool-authoring & sandbox: propose-review-human-approve flow plus subprocess sandbox. |
| 12 | Todo | Entry points & tests: CLI, inspection notebook, and deterministic pytest coverage. |

See `CLAUDE.md` §14 for checkpoints and deferred Phase 2 stubs.

Step 8 validates per-role plumbing: prompt loading, config-driven model
resolution, structured Pydantic input/output, and trace/cost logging. It does
not mean each role can complete its full real-world job yet.

| Role | Tools available at step 8? | What "one task" means |
|---|---|---|
| Librarian I/O | Yes — step 5 built arXiv / ADS / Tavily, and document attachments are wired through the model boundary. | Real source/document input can produce structured search results or memory abstractions. |
| Librarian searcher | Yes — step 6 built `text_search()` over SQLite. | Real query returns structured records from the DB. |
| Analyst worker | No — `research_toolkit` sandbox is not wired until step 11. | LLM-only reasoning or structured tool-request capture; no shell/tool execution yet. |
| Analyst lead | N/A — pure adjudication. | Given worker reports or tool requests, produce structured adjudication/review. |
| Reviewer | N/A — pure dispatch + review. | Given a request or stub results, produce a structured dispatch/review decision. |

---

## Getting started

```bash
conda env create -f environment.yml
conda activate auto_research
cp .env.example .env          # then fill in your keys
```

Keys expected in `.env` (never committed): `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GOOGLE_API_KEY`, `ADS_DEV_KEY` (NASA ADS), `TAVILY_API_KEY`, and LangSmith tracing
(`LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY`).

Runtime data (the SQLite file, the figures/artifacts store, run logs) lives in a
configurable directory **outside both repos** and is not committed.
For the current development runtime, `.env` may set
`AUTO_RESEARCH_RUNTIME_DIR=/data/fzhou/ar_runtime/test_runtime`, yielding
`/data/fzhou/ar_runtime/test_runtime/auto_research.sqlite3`. Locally supplied
papers live in `/data/fzhou/ar_runtime/papers`.

Mission observation locations are configured generically in
`config/observations.yaml` as `missions.<name>.obs_db_path`; agent code treats
mission names as data rather than special cases.

---

## Running

As a CLI:

```bash
python -m auto_research "your research question"
```

Or import the library / drive it from a notebook for inspection:

```python
from auto_research import run_request   # interface; see module DOCS.md
```

---

## For contributors & coding agents

- **`CLAUDE.md` is mandatory reading** and the single source of truth. Other agent files
  (`AGENTS.md`, etc.) are symlinks to it.
- Every module folder keeps a **`DOCS.md`** (purpose · architecture · interfaces ·
  dependencies · dictionary-level reference · changelog), always synced to the code.
- **`README.md` is kept synced too.** Any change that touches what this file describes
  — the build-status table, architecture diagram, model defaults, getting-started, or
  these contributor rules — must update the README in the same session.
- Coding agents **never run git**; they edit files and summarize changes for the human to
  commit.
- `CLAUDE.md` and `DOCS.md` are **build-time only** — never fed to runtime agents, which
  are configured solely from `prompts/`.
- External API integrations keep opt-in live test stubs, and coding agents default to
  running the relevant live API checks unless told otherwise or blocked by credentials,
  network, or spend concerns.
- Development test logs, live model outputs, usage, and cost ledgers go under
  `logs/dev/step-N/`, sorted by build-order step.

---

## Phase 2 roadmap (deferred)

Vector retrieval (sqlite-vec) · autonomous tool-merge toggle · deeper sandbox hardening ·
the lead-scientist group that mines the knowledge base to generate new research ideas.
