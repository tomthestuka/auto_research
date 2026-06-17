# `notebooks/` - human inspection notebooks

## Purpose
Provides human-facing notebooks for inspecting development and live research
runs. Notebooks are not loaded into runtime LLM context.

## Architecture
Notebook files are plain `.ipynb` documents at the repository root level. They
import the public library entry points and read monitor artifacts from the
runtime data directory or `logs/dev/step-N/`.

## Interfaces / Contracts
- `usage_example.ipynb` runs monitored requests with
  `RunMonitor(..., stream=sys.stdout)` so compact chatter-filtered progress
  lines appear while the run is active, including `team(model)` agent prefixes
  and compact tool-result summaries, then displays the grouped Markdown monitor
  report. It has two example sections:
  - **Using existing tools** — a Cyg X-1 joint broadband spectral-fit question the
    current `xspec_utils` toolkit already answers.
  - **Test code authoring** — IXPE project P1 (intra-observation PA/PD stability),
    which needs PCUBE/reduction/acquire capability the manifest does not expose, so
    the analyst lead authors the missing tool(s) on branch `ai` and the workers
    rerun on them (Phase 2 step 7). Logs under `logs/dev/step-7/`.
- The notebook can also inspect an existing monitor JSONL/Markdown pair written
  by the CLI or live tests.
- Notebooks must not be used as runtime prompts or retrieved context for runtime
  agents.

## Dependencies
- Internal: `auto_research.run_research`, `auto_research.monitor`.
- External: a Jupyter/IPython environment for display, supplied by the human's
  notebook tooling.

## Dictionary-level reference
- `usage_example.ipynb` - end-to-end usage notebook: "using existing tools" and
  "test code authoring" example runs, plus a monitor-log inspection cell.

## Changelog
- 2026-05-28 -> 2026-06-12 — Added and retained the Step 12 monitor inspection notebook
  for live-run and monitor-artifact review; notebooks remain human-only and never runtime
  prompt context.
- 2026-06-12 — Compacted changelog; no notebook behavior changes.
- 2026-06-15 — Updated the Step 12 notebook to use live monitor progress lines,
  `chatter=7` tool/debate summaries, quiet noisy HTTP/source loggers, and cleared stale outputs.
- 2026-06-15 — Documented that live notebook progress lines include model-prefixed
  agent events and compact per-tool result summaries.
- 2026-06-16 — Renamed `step12_monitor_inspection.ipynb` → `usage_example.ipynb`;
  restructured into a "using existing tools" section (the Cyg X-1 spectral fit) and a new
  "test code authoring" section (IXPE P1 PA/PD stability, Phase 2 step-7 authoring chain),
  keeping the monitor-inspection cell.
