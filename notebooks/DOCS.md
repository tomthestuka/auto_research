# `notebooks/` - human inspection notebooks

## Purpose
Provides human-facing notebooks for inspecting development and live research
runs. Notebooks are not loaded into runtime LLM context.

## Architecture
Notebook files are plain `.ipynb` documents at the repository root level. They
import the public library entry points and read monitor artifacts from the
runtime data directory or `logs/dev/step-N/`.

## Interfaces / Contracts
- `usage_example.ipynb` drives requests with the **detached** entry
  `run_research_nohup(request, chatter=7, ...)` so a killed kernel (logout) does not lose the
  run — re-running a cell reconnects by the deterministic `run_id`. `chatter=7` progress lines
  **stream live into the cell** exactly as a non-detached run (the wait loop follows the run's
  `monitor.jsonl`), and everything also persists to that JSONL + `backend.log`. The returned
  `ResearchRun`'s monitor is reconstructed from the JSONL — `run.markdown()` displays the grouped
  report. The code-authoring runs pass a connector `human_channel` (a detached child has no stdin, so
  `'cli'` is rejected); the recent runs (#12, #21) use `human_channel="telegram"`. Sections:
  - **Using existing tools** — a Cyg X-1 joint broadband spectral-fit question the
    current `xspec_utils` toolkit already answers.
  - **Test code authoring** — IXPE BH-XRB projects the spectral-only manifest cannot answer, so the
    analyst lead authors the missing tool(s) on branch `ai` and the workers rerun on them (Phase 2
    step 7; logs under `logs/dev/P<N>-*/step-7/`): **#2** intra-observation PA/PD stability, **#7**
    broadband PD–hardness survey, **#12** Krawczynski+2022 Cyg X-1 reproduction, and **#21** Q–U
    time-lag cross-correlation census (queued). The #21 request is assembled **verbatim from the
    research database** (`bhxrb_intraobs_qu_lag_crosscorr`) rather than pasted, so the launched
    request matches the source of record exactly.
- The notebook can also inspect an existing monitor JSONL/Markdown pair written
  by the CLI or live tests.
- Notebooks must not be used as runtime prompts or retrieved context for runtime
  agents.

## Dependencies
- Internal: `auto_research.run_research_nohup` (detached entry), `auto_research.monitor`.
- External: a Jupyter/IPython environment for display, supplied by the human's
  notebook tooling.

## Dictionary-level reference
- `usage_example.ipynb` - end-to-end usage notebook: one "using existing tools" run and four
  "test code authoring" runs (#2, #7, #12, #21), each a `## … (#N)` markdown section plus a
  `run_research_nohup` code cell.

## Changelog
- 2026-05-28 -> 2026-06-12 — Added and retained the Step 12 monitor inspection notebook
  for live-run and monitor-artifact review; notebooks remain human-only and never runtime
  prompt context.
- 2026-06-12 — Compacted changelog; no notebook behavior changes.
- 2026-06-15 -> 2026-06-16 — Reworked into the live `usage_example.ipynb`. The notebook now
  uses live monitor progress lines (`chatter=7` tool/debate summaries, model-prefixed agent
  events, compact per-tool result summaries, quiet HTTP/source loggers). Renamed from
  `step12_monitor_inspection.ipynb` and restructured into a "using existing tools" section
  (the Cyg X-1 spectral fit) and a new "test code authoring" section (IXPE P1 PA/PD stability,
  the Phase 2 step-7 authoring chain), keeping the monitor-inspection cell.
- 2026-07-06 — **`usage_example.ipynb` switched to the detached entry `run_research_nohup`.**
  Both example runs (existing-tools + authoring) now use `run_research_nohup(request, chatter=7[,
  human_channel="slack"])` instead of `run_research(monitor=RunMonitor(...))`, so a killed kernel
  survives and re-running a cell reconnects by `run_id`. Dropped the in-notebook `RunMonitor`
  construction (the monitor lives in the detached backend; `run.monitor` is reconstructed from its
  JSONL); markdown notes updated (live progress = tail the run dir's `monitor.jsonl`; `human_channel='cli'`
  rejected under detach).
- 2026-07-15 — Added the **#21** Q–U time-lag cross-correlation census section (plain-English markdown
  summary + a run cell) to `usage_example.ipynb`. The run cell assembles the `ResearchRequest` verbatim
  from the research database (`bhxrb_intraobs_qu_lag_crosscorr`) and drives `run_research_nohup(...,
  chatter=7, human_channel="telegram")`, logging under `logs/dev/P21-qu-lag-ccf/step-7/`. Also brought
  this doc current: the code-authoring sections are #2/#7/#12/#21 (not just P1) and the recent channel is
  telegram, not slack. Existing notebook cells were left byte-identical.
