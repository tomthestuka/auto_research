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

Structured agent messages use **provider-native structured output** (OpenAI Responses
`text.format`, Google `response_schema`, Anthropic `messages.parse(output_format=)`).
Every agent runs an **inner ReAct loop**: it calls tools via provider-native function
calling, sees each result in-context, and chains dependent calls to completion (e.g. the
analyst's load → fit → run → plot in one pass), then emits its structured result grounded
in those results. Each role's tool surface is a uniform `ToolProvider` — the analyst's
science sandbox, the reviewer/lead's read-only inspection tools, the librarian's literature
sources. The loop is bounded by `react_loop_cap`; prompt caching is a uniform `cache_prefix`
knob each provider realizes natively. There is no prompt-injected JSON schema and no salvage
parsing — results are Pydantic-validated and fail loudly; an off-schema structured call is
re-issued a few times (`structured_retry_cap`) with the error fed back before raising.
Runtime prompts carry role and scope only; tool names, arguments, and descriptions reach
the model through tool loading, not the prompt.

Model groups are **config-driven** (`config/models.yaml`), spanning OpenAI, Anthropic,
and Google. No model identifier is hardcoded. Defaults: reviewer group = Opus 4.8,
GPT-5.5 high effort, and Gemini 3.5 Flash high thinking; analyst lead = Opus 4.8;
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
| 8 | Done | Agent roles: reviewer, librarian I/O, analyst worker, and analyst lead wrappers; local retrieval is pure Python. |
| 9 | Done | Consensus: debate-to-converge strategy with cap 3 and lead adjudication. |
| 10 | Done | Orchestration: LangGraph graph with capped paper-search and analysis-result feedback edges. |
| 11 | Done | Sandbox & tool execution: `ToolkitRegistry` (manifest-based discovery + `tool_definitions()` for native function calling), persistent per-worker `PersistentSandbox` with non-interactive XSPEC boot policy and timeout/death restart, built-in MCP tools. Every agent runs an **inner ReAct loop** (`AgentRole._react` over a uniform `ToolProvider`): the analyst chains its science tools to completion in one pass, the reviewer/lead inspect artifacts via read-only tools, the librarian iterates over its literature sources. `merge_flow.py` stub (Phase 2 → Claude Agent SDK). LangGraph Studio: `langgraph dev` exposes live graph state per node. |
| 12 | Done | Entry points & tests: importable monitored library entry, CLI, inspection notebook, and deterministic pytest coverage. |

See `CLAUDE.md` §14 for checkpoints and deferred Phase 2 stubs.

Step 8 validates per-role plumbing: prompt loading, config-driven model
resolution, structured Pydantic input/output, and trace/cost logging. It does
not mean each role can complete its full real-world job yet.

| Role | Tools available at step 8? | What "one task" means |
|---|---|---|
| Librarian I/O | Yes — step 5 built arXiv / ADS / Tavily, and document attachments are wired through the model boundary. | Real source/document input can produce structured search results or memory abstractions. |
| Local retrieval | Yes — step 6 built `text_search()` over SQLite. | Pure-Python retrieval returns local paper paths from the DB; no runtime LLM role. |
| Analyst worker | Yes — step 11 wired `ToolkitRegistry` + subprocess sandbox. `analyze()` runs an inner ReAct loop in the sandbox (chaining load→fit→run→plot, self-correcting on errors in-loop), then reports grounded in real results. | Tool-catalogue-aware analysis; `xspec_utils` and any registered research_toolkit tools. |
| Analyst lead | Adjudication + a read-only inspection loop (artifacts/figures/memory) before synthesis. | Given worker reports, inspect evidence and produce a structured adjudication/review. |
| Reviewer | N/A — pure dispatch + review. | Given a request or stub results, produce a structured dispatch/review decision. |

---

## Setup

There are **two environments**, and you only need the second one to run the analyst
XSPEC tools:

1. **Orchestration env** (`auto_research` conda env) — required for everything: the
   LangGraph graph, LLM calls, librarians, reviewer, report composition. Pure Python +
   API keys.
2. **Analysis sandbox env** — required only so analyst workers can execute the
   `research_toolkit` XSPEC tools (`load_data`, `fit_*`, `plot_*`). This is HEASoft /
   PyXSPEC / `ixpeobssim` / `relxill` plus the `research_toolkit` repo and the observation
   data. Heavy and host-specific. **Without it the graph still runs end-to-end** — only
   the analyst tool calls fail (caught by the error-feedback loop).

### 1. Orchestration env (everyone)

```bash
conda env create -f environment.yml      # creates the `auto_research` env (Python 3.13)
conda activate auto_research             # `pip install -e .` + langgraph-cli run automatically
cp .env.example .env                     # then fill in your keys (see Configuration)
```

Verify it imports and the deterministic tests pass (these need **no** API keys, no XSPEC):

```bash
pytest -q          # ~150 deterministic tests; live/XSPEC tests are opt-in and skipped
```

### 2a. On our cluster (fastest)

The analysis env and data already exist and work — point the config at them and you are
done. The committed `config/orchestration.yaml` and `config/observations.yaml` already
reference these paths:

| What | Path (already present on the cluster) |
|---|---|
| Analysis Python env (PyXSPEC, ixpeobssim) | `/grad/fzhou/.conda/envs/astropy` |
| HEASoft/CALDB init script | `src/auto_research/sandbox/analyst_env.sh` (HEASoft 6.34) |
| `research_toolkit` repo (`toolkit_root`) | `/data/fzhou/research_toolkit` |
| `relxill` model tables | `/data/fzhou/XSPEC/relxill/` |
| Cyg X-1 observations | `/data/fzhou/XSPEC/Cyg_X-1/{IXPE,NICER,NuSTAR}` |

So on the cluster you only need: your own `auto_research` conda env (step 1), your own
`.env` with API keys, and an `AUTO_RESEARCH_RUNTIME_DIR` you can write to. Leave the
analysis paths as-is.

### 2b. On your own machine

To run the analyst XSPEC tools off-cluster you must reproduce the analysis stack and
repoint the config. Roughly:

1. **HEASoft (incl. PyXSPEC)** — install HEASoft and confirm `python -c "import xspec"`
   works under its Python. Edit `src/auto_research/sandbox/analyst_env.sh` so `HEADAS`,
   `CALDB`, etc. point at your install.
2. **Analysis Python env** — a conda/venv whose Python has PyXSPEC on the path plus
   `ixpeobssim` (for IXPE reduction/response). Set `analyze_env` in
   `config/orchestration.yaml` to its prefix.
3. **`relxill`** — install the model tables; the boot policy loads them from
   `/data/fzhou/XSPEC/relxill/` in `src/auto_research/sandbox/persistent.py` — change that
   path to yours.
4. **`research_toolkit`** — clone the sibling repo and set `toolkit_root` in
   `config/orchestration.yaml`.
5. **Observation data** — put your data somewhere and set the per-mission `obs_db_path`s
   in `config/observations.yaml`.

If you only care about the literature/reviewer loop, skip step 2 entirely — the analyst
tools simply won't be available.

---

## Configuration

| File | Purpose |
|---|---|
| `.env` | API keys + runtime dir. Loaded automatically. Never committed (`cp .env.example .env`). |
| `config/models.yaml` | Role → model-group mapping (OpenAI/Anthropic/Google). No model IDs in code. |
| `config/orchestration.yaml` | Iteration caps, `toolkit_root`, `analyze_env`, `analyze_ini_sh`, `disabled_tools`, `report_formats`, `report_retry_cap`. |
| `config/observations.yaml` | `missions.<name>.obs_db_path` — local observation locations, treated as data. |

**Keys** (the provider SDKs read these directly; see `.env.example` for the full template):
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ADS_DEV_KEY` (NASA ADS),
`TAVILY_API_KEY`; optional LangSmith tracing (`LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`).

**Runtime data dir** (`AUTO_RESEARCH_RUNTIME_DIR`, defaults to `~/.auto_research`) lives
**outside both repos** and is not committed. It holds the SQLite DB, figures/artifacts,
run logs, and reviewer reports. On an accepted verdict,
`runs/{run_id}/reviewer/report/` holds `report.jsonl` (machine handle for upstream
planning teams), `report.tex` + symlinked `figures/` (human LaTeX project), and
`report.pdf` (compiled by `pdflatex` when available).

---

## Usage & entry points

**CLI** — one request end-to-end:

```bash
python -m auto_research "your research question" --chatter 10
# options: --context TEXT  --constraint TEXT (repeatable)
#          --report-format {jsonl,tex} (repeatable; default both)
#          --format {text,json}  --monitor-jsonl PATH  --monitor-md PATH
```

**Library** — monitored run from Python / a notebook:

```python
from auto_research import run_research

run = await run_research("your research question", chatter=10)
run.verdict          # ReviewVerdict
run.markdown()       # grouped monitor report
run.report_pdf       # path to the compiled report (if accepted + tex requested)
```

**LangGraph Studio** — live per-node graph state (`langgraph.json` → `graph/serve.py`):

```bash
langgraph dev        # reads .env, serves the `research` graph in Studio
```

**Notebook** — `notebooks/step12_monitor_inspection.ipynb` reconstructs and inspects a
run from its monitor JSONL.

**Run visualiser** — Gantt timeline + cost pie from any monitor JSONL:

```bash
python -m auto_research.observability.plot_run <monitor.jsonl> [output.png]
# reads pricing and model assignment from config/models.yaml automatically
# default output: <stem>.viz.png in the current directory
```

**Tests** — `pytest -q` for deterministic checks. Live API / XSPEC tests are opt-in via
env flags (e.g. `AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS=1`) and write logs under
`logs/dev/step-N/`.

`--chatter` follows an XSPEC-style 1-10 verbosity scale; `10` records the full agent,
consensus, tool, and reviewer narrative as JSONL plus a grouped Markdown report.

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
- When unexpected failures, environment/tooling oddities, ambiguous runtime behavior, or
  suspected dependency issues appear, coding agents collect minimal facts and consult the
  human before attempting fixes or workarounds.
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
`bash_exec` figure capture (diff worker cwd for new images → `FigureRef`s) ·
the lead-scientist group that mines the knowledge base to generate new research ideas.
