"""Command-line interface for monitored research runs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Sequence

from auto_research.graph import run_research
from auto_research.graph.detached import jsonable
from auto_research.monitor import RunMonitor, default_monitor_jsonl_path, validate_chatter
from auto_research.sandbox.workspace import run_dir
from auto_research.schemas import ResearchRequest


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""

    parser = argparse.ArgumentParser(prog="python -m auto_research")
    parser.add_argument("request", help="Research question to run through the Phase 1 graph.")
    parser.add_argument("--context", help="Optional context for the research request.")
    parser.add_argument(
        "--constraint",
        action="append",
        default=[],
        help="Optional request constraint. May be supplied more than once.",
    )
    parser.add_argument(
        "--chatter",
        type=int,
        default=10,
        help="Monitor verbosity from 1 to 10. Higher values record more detail.",
    )
    parser.add_argument(
        "--monitor-jsonl",
        type=Path,
        help="Path for the monitor JSONL log. Defaults to the runtime data dir.",
    )
    parser.add_argument(
        "--monitor-md",
        type=Path,
        help="Path for the grouped Markdown monitor report. Defaults beside the JSONL log.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format for the final verdict.",
    )
    parser.add_argument(
        "--human",
        choices=("auto", "cli", "none", "slack", "telegram", "whatsapp"),
        default="auto",
        help=(
            "Channel a blocked agent uses to reach you. 'auto' (default) picks the terminal "
            "(stdin) when interactive, else the configured connector. 'none' runs headless."
        ),
    )
    parser.add_argument(
        "--report-format",
        action="append",
        choices=("jsonl", "tex"),
        default=None,
        dest="report_formats",
        metavar="{jsonl,tex}",
        help=(
            "Which report artifact(s) to produce on an accepted verdict. "
            "May be supplied more than once. Defaults to both jsonl and tex."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Hard cap for each reviewer feedback edge. Defaults to config.",
    )
    parser.add_argument(
        "--run-id",
        help=(
            "Pin the run_id so the workspace/report/JSONL land in a known, reconnectable dir "
            "and a terminal result.json is written. Used by the detached run_research_nohup "
            "backend; defaults to an auto-generated id."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume a prior run of this --run-id from its latest checkpoint (continues from "
            "the last completed node; the in-flight node re-runs). Requires a pinned --run-id."
        ),
    )
    return parser


def _write_result(run_id: str, payload: dict) -> None:
    """Write the detached run's terminal ``result.json`` (the marker run_research_nohup
    tails for). ``run_id`` is auto/path-safe by the time it reaches here."""
    target = run_dir(run_id) / "result.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, default=jsonable), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one research request from the command line."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        chatter = validate_chatter(args.chatter)
    except ValueError as exc:
        parser.error(str(exc))

    # A pinned --run-id (the detached backend) puts the JSONL in the reconnectable run dir.
    if args.monitor_jsonl:
        jsonl_path = args.monitor_jsonl
    elif args.run_id:
        jsonl_path = run_dir(args.run_id) / "monitor.jsonl"
    else:
        jsonl_path = default_monitor_jsonl_path("cli_run")
    md_path = args.monitor_md or jsonl_path.with_suffix(".md")
    monitor = RunMonitor(chatter=chatter, jsonl_path=jsonl_path, stream=sys.stderr)
    request = ResearchRequest(
        question=args.request,
        context=args.context,
        constraints=args.constraint,
    )

    try:
        run = asyncio.run(
            run_research(
                request,
                monitor=monitor,
                report_formats=args.report_formats,
                human_channel=args.human,
                max_iterations=args.max_iterations,
                run_id=args.run_id,
                resume=args.resume,
            )
        )
    except Exception as exc:  # noqa: BLE001 — record an honest terminal marker, then re-raise
        # A caught crash → a `failed` result.json so the detached client raises RunDied with
        # the reason (a hard kill writes nothing → the client detects a dead process instead).
        if args.run_id:
            _write_result(args.run_id, {"status": "failed", "error": repr(exc)})
        raise
    run.monitor.write_markdown(md_path)

    if args.run_id:
        _write_result(args.run_id, {
            "status": "done",
            "verdict": run.verdict.model_dump(mode="json"),
            "final_state": json.loads(json.dumps(run.final_state, default=jsonable)),
            "report_jsonl": str(run.report_jsonl) if run.report_jsonl else None,
            "report_latex": str(run.report_latex) if run.report_latex else None,
            "report_pdf": str(run.report_pdf) if run.report_pdf else None,
        })

    if args.format == "json":
        out: dict = {
            "verdict": run.verdict.model_dump(mode="json"),
            "monitor_jsonl": str(jsonl_path),
            "monitor_markdown": str(md_path),
        }
        if run.report_jsonl:
            out["report_jsonl"] = str(run.report_jsonl)
        if run.report_latex:
            out["report_latex"] = str(run.report_latex)
        if run.report_pdf:
            out["report_pdf"] = str(run.report_pdf)
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"Accepted: {run.verdict.accepted}")
        print(f"Needs more literature: {run.verdict.needs_more_literature}")
        print(f"Needs analysis rework: {run.verdict.needs_analysis_rework}")
        if run.verdict.follow_up:
            print(f"Follow-up: {run.verdict.follow_up}")
        print("")
        print(run.verdict.rationale)
        print("")
        print(f"Monitor JSONL: {jsonl_path}")
        print(f"Monitor Markdown: {md_path}")
        if run.report_jsonl:
            print(f"Report JSONL:   {run.report_jsonl}")
        if run.report_latex:
            print(f"Report LaTeX:   {run.report_latex}")
        if run.report_pdf:
            print(f"Report PDF:     {run.report_pdf}")
    return 0


__all__ = ["build_parser", "main"]
