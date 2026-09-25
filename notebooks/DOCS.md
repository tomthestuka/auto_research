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
- 2026-05-28 -> 2026-06-16 — Monitor inspection notebook, reworked into the live
  `usage_example.ipynb` (renamed from `step12_monitor_inspection.ipynb`): an existing-tools section
  (Cyg X-1 fit) and a code-authoring section (IXPE P1); human-only, never runtime context.
- 2026-07-06 -> 2026-07-15 — Runs use detached `run_research_nohup` (no in-notebook `RunMonitor`;
  `human_channel='cli'` rejected under detach); added the #21 Q–U lag cross-correlation section
  (telegram); authoring sections are #2/#7/#12/#21.
