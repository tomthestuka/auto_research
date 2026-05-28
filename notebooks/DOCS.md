# `notebooks/` - human inspection notebooks

## Purpose
Provides human-facing notebooks for inspecting development and live research
runs. Notebooks are not loaded into runtime LLM context.

## Architecture
Notebook files are plain `.ipynb` documents at the repository root level. They
import the public library entry points and read monitor artifacts from the
runtime data directory or `logs/dev/step-N/`.

## Interfaces / Contracts
- `step12_monitor_inspection.ipynb` can run a full-chatty monitored request via
  `run_research(..., chatter=10)` and display the grouped Markdown monitor
  report.
- The notebook can also inspect an existing monitor JSONL/Markdown pair written
  by the CLI or live tests.
- Notebooks must not be used as runtime prompts or retrieved context for runtime
  agents.

## Dependencies
- Internal: `auto_research.run_research`, `auto_research.monitor`.
- External: a Jupyter/IPython environment for display, supplied by the human's
  notebook tooling.

## Dictionary-level reference
- `step12_monitor_inspection.ipynb` - Step 12 inspection notebook for monitor
  logs and live run reports.

## Changelog
- 2026-05-28 - Added the Step 12 monitor inspection notebook.
