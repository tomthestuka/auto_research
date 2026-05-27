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
human request  →  reviewer dispatches literature + analysis tasks
               →  librarians search · analysts analyze (debate to converge)
               →  reviewer digests results, cross-checks prior publications
               →  human review
```

with two feedback edges (each capped at 3 iterations): the reviewer can send literature
back to the librarians ("insufficient / obsolete — search again") and analyses back to
the analysts ("rework this").

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

Model tiers are **config-driven** (`config/models.yaml`), spanning OpenAI, Anthropic, and
Google. No model identifier is hardcoded. Defaults: reviewer / analyst-lead = top tier
(Opus 4.7 · GPT-5.5 · Gemini 3.1 Pro); librarian-I/O & analyst-worker = medium
(Sonnet 4.6 · GPT-5.5 medium · Gemini 3 Flash); librarian-searcher = light
(Haiku 4.5 · GPT-5.4-nano · Gemini 3.1 Flash-Lite).

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

Statuses: **Todo** = in Phase 1 scope, not yet built · **Done** = built & documented ·
**Stub** = interface left in place, body deferred · **Deferred** = Phase 2.
Coding agents flip `Todo → Done` as work lands and keep this table synced (same discipline
as `DOCS.md`).

| Component | Status |
|---|---|
| Repo scaffolding, conda env, `.env.example`, symlinks | Todo |
| Pydantic schemas (incl. `FigureRef`) | Todo |
| Config-driven model registry (OpenAI / Anthropic / Google) | Todo |
| LangSmith tracing + per-call cost logging | Todo |
| SQLite storage (papers / memory / query_log) | Todo |
| Reserved embedding column/table | Todo |
| `QuerySource` interface (extensible) | Todo |
| Librarian source — arXiv | Todo |
| Librarian source — NASA ADS | Todo |
| Librarian source — web (Tavily) | Todo |
| Retrieval — `text_search()` | Todo |
| Retrieval — `vector_search()` | Stub |
| Vector DB (sqlite-vec) + embeddings | Deferred |
| Runtime `prompts/` per role | Todo |
| Reviewer agent | Todo |
| Librarian agents (I/O + searcher) | Todo |
| Analyst agents (worker + lead) | Todo |
| Consensus — debate-to-converge (cap 3) | Todo |
| LangGraph orchestration + 2 feedback edges (cap 3) | Todo |
| Tool-authoring flow (propose → review → human-approve merge) | Todo |
| Execution sandbox (subprocess · timeout · workdir confinement) | Todo |
| Autonomous tool-merge toggle | Deferred |
| Sandbox hardening (containers / seccomp / egress) | Deferred |
| CLI entry + notebook | Todo |
| Minimal pytest (deterministic parts) | Todo |
| Lead-scientist idea-generation group | Deferred |

See `CLAUDE.md` §14 for the dependency-ordered build sequence.

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
- Coding agents **never run git**; they edit files and summarize changes for the human to
  commit.
- `CLAUDE.md` and `DOCS.md` are **build-time only** — never fed to runtime agents, which
  are configured solely from `prompts/`.

---

## Phase 2 roadmap (deferred)

Vector retrieval (sqlite-vec) · autonomous tool-merge toggle · deeper sandbox hardening ·
the lead-scientist group that mines the knowledge base to generate new research ideas.
