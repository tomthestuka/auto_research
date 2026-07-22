# auto_research

`auto_research` is an automated research assistant: a team of LLM agents, orchestrated
with **LangGraph**, that takes a research request, searches the literature, runs real
analyses on real data, and reviews the results against prior work — producing a
machine-readable report handle (JSONL) and a human one (compilable LaTeX/PDF).

The system also **extends its own analysis reach**: when a task needs a capability its
domain tool library (`research_toolkit`, a separate repo) lacks, the analyst team
authors the tool itself — through a deterministic mergeability gate (tests run twice, a
no-fabrication scan, provenance hashes) plus a provider-decorrelated code review, landing
one atomic commit per tool on a working branch. The design goal is **verifiability, not
trust**: every authored tool ships a human-runnable interface and tests a human can
rerun, honest failure is a first-class outcome, and nothing reaches the toolkit's trusted
branch except by a manual, out-of-band human merge. The runtime cannot write to this repo
at all — self-modification is impossible by construction.

Coding agents: read **`CLAUDE.md`** before doing anything. It is the single source of
truth for how this project is built — current status, the phase spec, and the build
order live there, not here.

---

## Setup

Requires **Linux or macOS** (the sandbox uses POSIX process groups / signals). The fastest
path is the installer; a manual path follows for full control.

### Quick start — `bash install.sh`

```bash
git clone <this repo> && cd auto_research
cp .env.example .env      # fill in your API keys (see Configuration)
bash install.sh           # from the repo root, in a real terminal
```

`install.sh` bootstraps conda (offers to install Miniforge if none is found), creates/updates
the `auto_research` orchestration env from `environment.yml`, then runs the interactive Python
stage (`python -m auto_research.setup`). That stage walks you, page by page, through: **conda**,
the **analysis env** (from `environment-analyst.yml`), your **domain dependencies** (init
scripts / env vars / conda-pip packages / a REPL session hook / **licensed vendor tools**), the
machine **paths** (runtime dir, data root), and the **`research_toolkit`** checkout — then writes
the machine-local `config/orchestration.yaml` + `config/observations.yaml` (deriving
`agent_fs_allow` for you), copies the sandbox hooks, and runs the launch preflight as a smoke
test. A re-run is an **upgrade**: it seeds every prompt from your existing config.

Machine-local files (`config/orchestration.yaml`, `config/observations.yaml`,
`src/auto_research/sandbox/{analyst_env.sh,repl_init.py}`) are **gitignored**; the repo tracks a
`*.example` template for each. The installer writes the live copies; a fresh clone that skips the
installer must copy each `*.example` by hand.

Headless (CI / an agent): `bash install.sh --answers answers.yaml` (a YAML/JSON file of the
prompt values) — a missing value fails loudly rather than hanging.

Verify the deterministic tests pass (no API keys, no XSPEC needed):

```bash
conda activate auto_research
pytest -q          # ~477 deterministic tests; no API keys needed
```

The suite is **portable**: tests that need a fully-provisioned machine (a real `analyze_env`
interpreter, `obsdb_catalog` in the toolkit, a populated data root) **skip** gracefully when those
are absent, rather than failing — so a fresh clone reports passes + skips, not errors. Live/XSPEC
tests are opt-in and env-gated.

### The two environments

1. **Orchestration env** (`auto_research`) — required for everything: the LangGraph graph, LLM
   calls, librarians, reviewer, report composition. Pure Python + API keys. `claude-agent-sdk`
   (tool authoring) and `langgraph-cli` (Studio) come from the `pip:` block in `environment.yml`.
2. **Analysis sandbox env** (`analyze_env`) — required only so analyst workers can execute the
   `research_toolkit` tools (XSPEC `xspec_utils.*`, archive `heasarc_utils.acquire`). This is
   HEASoft / PyXSPEC / `ixpeobssim` / `relxill` / `astroquery` plus the `research_toolkit` repo and
   the observation data. Heavy and host-specific. **Without it the graph still runs end-to-end** —
   only the analyst tool calls fail (caught by the error-feedback loop).

### Manual setup (or what the installer automates)

Everything the installer does, by hand:

1. **Orchestration env** — `conda env create -f environment.yml && conda activate auto_research`.
2. **Config** — `cp config/orchestration.yaml.example config/orchestration.yaml` and
   `cp config/observations.yaml.example config/observations.yaml`; edit the paths. Keep
   `agent_fs_allow` in sync with them (the installer derives it).
3. **HEASoft (incl. PyXSPEC)** — install HEASoft; `cp src/auto_research/sandbox/analyst_env.sh.example
   src/auto_research/sandbox/analyst_env.sh` and edit `HEADAS`/`CALDB`; point `analyze_ini_sh` at it.
4. **Analysis Python env** — `conda env create -f environment-analyst.yml -p <prefix>`; set
   `analyze_env: <prefix>`. (This env excludes PyXSPEC/CALDB/`relxill` — those come from the HEASoft
   build + `analyst_env.sh`.)
5. **`relxill` / REPL session setup** — `cp src/auto_research/sandbox/repl_init.py.example
   src/auto_research/sandbox/repl_init.py`, set the `relxill` path inside it, and point `repl_init_py`
   at it. (This file is exec'd at each analyst REPL boot — the domain session hook.)
6. **`research_toolkit`** — clone the sibling repo, `git checkout -b ai` (the branch the runtime
   commits authored tools onto), ensure a git `user.name`/`user.email`, and set `toolkit_root`.
7. **Observation data** — put data under one folder with a `<MISSION>` subdirectory per mission;
   set `obs_db_root`. Missions are discovered from disk (a subdir with a `catalog.json` is a mission).

If you only care about the literature/reviewer loop, you still need `research_toolkit` present on
its `ai` branch (the launch preflight checks it); set `analyze_probe: ""` to skip the analysis-stack
smoke test.

---

## Configuration

| File | Purpose |
|---|---|
| `.env` | API keys and optional tracing flags. Loaded automatically. Never committed (`cp .env.example .env`). |
| `config/models.yaml` | Role → model-group mapping (OpenAI/Anthropic/Google). No model IDs in code. |
| `config/orchestration.yaml` | `runtime_dir`, iteration/ReAct caps, `toolkit_root`, `analyze_env`, `analyze_ini_sh`, `disabled_tools`, `agent_fs_allow` (the per-agent filesystem allow-list), the internet-stability retry policy (`provider_retry_*` — transient provider failures waited out with growing cooldowns instead of killing the run), report formats, report retry cap, and Phase-2 authoring caps. |
| `config/observations.yaml` | `obs_db_root` — the single obs-db folder; missions are its `<MISSION>` subdirs (those holding a `catalog.json`), discovered from disk and treated as data. |
| `config/code_gate.yaml` | The Phase-2 gate's no-fabrication scan lists (banned vocabulary, hard-fail vs finding-tier code patterns — which list a pattern sits in decides its severity; RNG is finding-tier since Monte-Carlo tools need randomness — literal-array findings, ObsID allowlist). Human-tunable; no scan constant in code. |

**Keys** (the provider SDKs read these directly; see `.env.example` for the full template):
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `ADS_DEV_KEY` (NASA ADS),
`TAVILY_API_KEY`; optional LangSmith tracing (`LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`);
optional raise-to-human connectors (only when selected) — **Telegram** (recommended:
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`), Slack
(`SLACK_BOT_TOKEN`/`SLACK_APP_TOKEN`/`SLACK_HUMAN_CHANNEL`), or the deprecated Twilio WhatsApp
(`TWILIO_ACCOUNT_SID`/`TWILIO_API_KEY_SID`/`TWILIO_API_KEY_SECRET`/`TWILIO_WHATSAPP_FROM`/`WHATSAPP_TO`);
the terminal (`cli`) channel needs none.

**Runtime data dir** (`config/orchestration.yaml::runtime_dir`, defaults to
`~/.auto_research` if omitted) lives **outside both repos** and is not committed. It holds
the SQLite DB, figures/artifacts, per-worker sandbox scratch, run logs, cost ledger, and
reviewer reports. On an accepted verdict,
`runs/{run_id}/reviewer/report/` holds `report.jsonl` (machine handle for upstream
planning teams), `report.tex` + symlinked `figures/` and `papers/` (human LaTeX project;
the References section is `\href` links into `papers/`, gate-checked to resolve to real
PDFs), and `report.pdf` (compiled by `pdflatex` when available).

---

## Usage & entry points

**CLI** — one request end-to-end:

```bash
python -m auto_research "your research question" --chatter 10
# options: --context TEXT  --constraint TEXT (repeatable)
#          --report-format {jsonl,tex} (repeatable; default both)
#          --format {text,json}  --monitor-jsonl PATH  --monitor-md PATH
```
The CLI prints compact live monitor summaries to stderr during the run, with
model-backed agent lines prefixed as `team(model)` and compact per-tool result
lines, then prints the final verdict/artifact paths to stdout. `--format json`
keeps stdout machine-readable.

**Library** — monitored run from Python / a notebook:

```python
from auto_research import run_research

run = await run_research("your research question", chatter=10)
run.verdict          # ReviewVerdict
run.markdown()       # grouped monitor report
run.report_pdf       # path to the compiled report (if accepted + tex requested)
```

**Detached** — survive a killed kernel/logout (notebooks); reconnect by re-running:

```python
from auto_research import run_research_nohup

# Runs the pipeline in a detached, orphaned process (own session, double-fork spawn —
# reparented to init so neither a dead kernel nor a logout's process-tree cleanup can
# reach it). The run finishes to disk; re-running the *same* call reconnects by a
# deterministic run_id (sha256 of the request) and returns the result.
run = await run_research_nohup("your research question", human_channel="telegram")
# overwrite=True starts fresh (refuses if the run is still alive);
# a dead run raises RunDied (carrying how far it got). human_channel='cli' is rejected
# (a detached child has no stdin) — use a connector or 'none'.
```

**LangGraph Studio** — live per-node graph state (`langgraph.json` → `graph/serve.py`):

```bash
langgraph dev        # reads .env, serves the `research` graph in Studio
```

**Notebook** — `notebooks/usage_example.ipynb` can run with live
`team(model)` monitor summaries and compact tool-result lines in the cell output,
or reconstruct and inspect a run from its monitor JSONL.

**Run visualiser** — Gantt timeline + cost pie from any monitor JSONL:

```bash
python -m auto_research.observability.plot_run <monitor.jsonl> [output.png]
# reads pricing and model assignment from config/models.yaml automatically
# default output: <stem>.viz.png in the current directory
```

**Spend rollup** — cumulative cost across all runs (no provider admin panel needed):

```bash
python -m auto_research.observability.usage [--since YYYY-MM-DD] [--top N]
# total + by provider/model/role/day from <runtime_dir>/cost_ledger.jsonl
```

Cost is priced **litellm-first, config-fallback**: litellm's community price map (incl. cache/reasoning
tokens) prices what it knows, `config/models.yaml: priceMTk` fills the gaps. When `LANGCHAIN_TRACING_V2`
is on, each call's tokens + cost are also attached to its LangSmith run. The number is an estimate
against list prices — it cannot reconcile to an org's negotiated bill (that needs an admin key).

**Tests** — `pytest -q` for deterministic checks. Live API / XSPEC tests are opt-in via
env flags (e.g. `AUTO_RESEARCH_LIVE_FULL_SUITE_TESTS=1`) and write logs under
`logs/dev/step-N/`.

`--chatter` follows an XSPEC-style 1-10 verbosity scale; it filters live summaries and
grouped Markdown, while the monitor JSONL still records the full agent, consensus, tool,
and reviewer narrative.

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

For now the request enters from the human. The lead-scientist planning team is deferred
beyond Phase 2 and will route requests through the same reviewer gate when built.

---

## LLM architecture

```
      ╔════════════════════════════════════════════════════════════════════╗
      ║ auto_research repo                                                 ║
      ║ graph · agents · prompts · schemas · providers · sandbox/gate      ║
      ║ runtime agents read this repo only; build-time commits are human   ║
      ╚═══════════════════════════════╤════════════════════════════════════╝
                                      │ config + prompts + tool surfaces
                                      ▼
      ┌────────────────────────────────────────────────────────────────────┐
      │                               HUMAN                                │
      │                    research request · report review                │
      └───────────────────────────────┬────────────────────────────────────┘
                                      │ request
                                      ▼
      ┌────────────────────────────────────────────────────────────────────┐
      │                        REVIEWER group (top)                        │
      │              triage · dispatch · digest · cross-check              │
      └───────────────┬────────────────────────────────────┬───────────────┘
                      │ literature tasks                    │ analysis tasks
                      ▼                                     ▼
      ┌──────────────────────────────┐       ┌──────────────────────────────┐
      │        LIBRARIAN I/O         │       │           ANALYSTS           │
      │ medium group · ReAct sources │       │ workers debate · lead judges │
      │ arXiv · ADS · web · local DB │       │ data work · figures · tools  │
      └───────────────┬──────────────┘       └───────┬──────────────┬───────┘
                      │ papers + memory              │ read-only     │ ToolSpec
                      ▼                              │ tool calls    │ missing/fix
      ┌──────────────────────────────┐               ▼               ▼
      │      SQLite papers/memory    │       ╔══════════════════════╗ ┌──────────────────────┐
      │      text search now         │       ║ research_toolkit repo║ │ CODER + GATE flow    │
      │      vector search Todo      │       ║ xspec_utils today    ║ │ Claude author · gate │
      └───────────────┬──────────────┘       ║ future *_utils       ║ │ GPT co-lead review   │
                      │ literature results   ║ TOOL_MANIFEST        ║◄┤ isolated branch Todo │
                      │                      ╚══════════╤═══════════╝ └──────────────────────┘
                      │                                 │ tool results + figs
                      │                                 ▼
                      │                      ┌──────────────────────────────┐
                      │                      │      ANALYST reports         │
                      │                      │ findings · uncertainty · figs│
                      │                      └──────────────┬───────────────┘
                      │                                     │ analysis results
                      └────────────────────┐                │
                                           ▼                ▼
      ┌────────────────────────────────────────────────────────────────────┐
      │                  REVIEWER receives evidence/results                │
      │              capped feedback to librarians / analysts              │
      └───────────────────────────────┬────────────────────────────────────┘
                                      │ final verdict + report
                                      ▼
      ┌────────────────────────────────────────────────────────────────────┐
      │                               HUMAN                                │
      └────────────────────────────────────────────────────────────────────┘
```

Structured agent messages use **provider-native structured output** (OpenAI Responses
`text.format`, Google `response_schema`, Anthropic `messages.parse(output_format=)`).
Every agent runs an **inner ReAct loop**: it calls tools via provider-native function
calling, sees each result in-context, and chains dependent calls to completion (e.g. the
analyst's load → fit → run → plot in one pass), then emits its structured result grounded
in those results. Each role's tool surface is a uniform `ToolProvider`: the analyst's
science sandbox, the reviewer/lead's read-only inspection tools, and the librarian's
literature sources. The loop is bounded by `react_loop_cap`, with
`librarian_react_loop_cap` kept tighter because repeated searches re-send a growing paper
context. Prompt caching is a uniform `cache_prefix` knob each provider realizes natively
(Anthropic explicit stable-prefix + rolling-tail `cache_control`; OpenAI/Google native
automatic caching). For long loops, the same knob optionally carries Anthropic's
provider-native **context editing** — clearing the oldest tool results server-side once a
request crosses a token watermark (`config/orchestration.yaml: context_mgmt_*`), a
context-window pressure valve, not a cost lever. It is Anthropic-only and ships disabled
(beta; **not live tested** as of 2026-07-22).
There is no prompt-injected JSON schema and no salvage parsing: results are
Pydantic-validated and fail loudly, with bounded `structured_retry_cap` retries. Runtime
prompts carry role and scope only; tool names, arguments, and descriptions reach the model
through tool loading, not prompt text.

Model groups are **config-driven** (`config/models.yaml`), spanning OpenAI, Anthropic,
and Google. No model identifier is hardcoded. Defaults: reviewer group = Fable 5,
GPT-5.5 high effort, and Gemini 3.5 Flash high thinking; analyst lead = Opus 4.8;
analyst worker group = Sonnet 4.6 and GPT-5.5 medium effort; librarian I/O group =
Sonnet 4.6, GPT-5.5 medium effort, and Gemini 3.5 Flash thinking. Phase 2 adds `coder`
(Claude Sonnet via Claude Agent SDK) and `analyst_colead` (GPT-5.5 xhigh) so tool authoring
and review are provider-decorrelated. There is no LLM librarian-searcher role; local
retrieval is pure Python.

Current role/tool surfaces:

| Role | Tool surface | What "one task" means |
|---|---|---|
| Librarian I/O | `SourceToolProvider`: arXiv, ADS, Tavily web, and local text search. | Real source/document input can produce structured search results or memory abstractions. |
| Local retrieval | Pure-Python `text_search()`/`vector_search()` (stub) over SQLite, reached through the **reviewer's** `text_search` + `open_paper` tools — not a graph node. | Returns local paper paths and attaches chosen PDFs; no standalone LLM role. |
| Analyst worker | `SandboxToolProvider`: `research_toolkit` manifest tools (including `catalog_utils`'s read-only `data_catalog`) + MCP tools in a persistent sandbox, plus `web_search`, `web_fetch`, and the report tools `edit_report`/`review_report`. | `analyze()` chains load→fit→run→plot or other tools, self-corrects in-loop, then **writes its report via `edit_report`**, citing figures from its catalog. |
| Analyst lead | `ReadOnlyToolProvider`: run artifacts, figures, memory, papers dir, full `research_toolkit` source, the `data_catalog` tool (from `research_toolkit`'s `catalog_utils`, run in-process for the sandbox-less lead), `web_search` + `web_fetch` (`include_text_search=False` — knowledge-base search is the reviewer's), plus `edit_report`/`review_report` during adjudication. | Adjudicates debate (writing the report via `edit_report`, citing figures across workers/rounds), triages `ToolSpec`, decides authoring (`decide_authoring`), and answers author clarifications. |
| Analyst co-lead | `ReadOnlyToolProvider`: the authored toolkit worktree + `web_search` + `web_fetch` (`include_text_search=False`). | Provider-decorrelated **code review** of one authored tool — brief-match, Layer-B check adequacy, no-fabrication second pass, subtle bugs → `ToolReviewVerdict`. |
| Reviewer | `ReadOnlyToolProvider`: run artifacts, figures, memory, `web_search` + `web_fetch`, plus the knowledge-base tools `text_search` (find papers) + `open_paper` (read a chosen PDF). | Pulls and reads literature itself, dispatches/reviews work, requests capped rework, and composes final report handles (References = local-PDF hyperlinks it used). |

---

## Repositories & trust boundary

Two separate repos. The split is a **safety mechanism**: the system's own code lives in a
repo it cannot write to, so it cannot modify itself — only extend the tool library.

```
  ┌──────────────────────────────────────┐      ┌──────────────────────────────┐
  │            auto_research             │      │       research_toolkit       │
  │  orchestration · agents · prompts ·  │      │  xspec_utils · LLM-authored  │
  │  schemas · sqlite · source modules   │      │  *_utils domain packages     │
  │                                      │      │                              │
  │  commits: HUMAN ONLY                 │      │  master: HUMAN ONLY          │
  │  runtime access: READ-ONLY           │ ───► │  runtime: read-only invoke   │
  │  (cannot modify its own code)        │      │  via TOOL_MANIFEST+sandbox   │
  │                                      │      │                              │
  │  Phase 2 authoring code              │ ───► │  isolated worktree/branch    │
  │  can create gated toolkit branches   │      │  gate + co-lead review +     │
  │  but never merges master             │      │  atomic commit built         │
  └──────────────────────────────────────┘      └──────────────────────────────┘
```

**Build-time coding agents never run git** — staging, commits, and pushes are the human's
job. Separately, the runtime toolkit-branch flow is application behavior inside
`merge_flow`: the author harness, deterministic gate, provider-decorrelated GPT co-lead
review, and the `approve_edit()` atomic branch commit all exist now; merges to
`research_toolkit` master remain manual and out of band.

---

## For contributors & coding agents

- **`CLAUDE.md` is mandatory reading** and the single source of truth — including the
  current build status and phase build order. Other agent files (`AGENTS.md`, etc.) are
  symlinks to it.
- Every module folder keeps a **`DOCS.md`** (purpose · architecture · interfaces ·
  dependencies · dictionary-level reference · changelog), always synced to the code.
- **`README.md` is kept synced too.** Any change that touches what this file describes
  — the architecture diagram, model defaults, setup/configuration/usage, or these
  contributor rules — must update the README in the same session.
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

## Beyond Phase 2 (deferred stubs)

Vector retrieval (ChromaDB; embedding column reserved) · durable HITL suspend/resume
(LangGraph checkpointer) · OS-level sandbox jailing (bind-mounts / seccomp — every agent's file
access is already confined to the `agent_fs_allow` allow-list via `sandbox.fs_guard.PathGuard`; the
OS jail would also gate a shell subprocess's implicit reads) ·
`bash_exec` figure capture (diff worker cwd → `FigureRef`s) · librarian IO ingest
consensus (activate `IODebateToConverge`, replace `accepted=True` stub) ·
the lead-scientist group that mines the knowledge base to propose new research requests.
