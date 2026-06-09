"""Command-line interface for monitored research runs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from auto_research.env import check_required_env
from auto_research.graph import run_research
from auto_research.monitor import default_monitor_jsonl_path, validate_chatter
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one research request from the command line."""

    load_dotenv()
    check_required_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        chatter = validate_chatter(args.chatter)
    except ValueError as exc:
        parser.error(str(exc))

    jsonl_path = args.monitor_jsonl or default_monitor_jsonl_path("cli_run")
    md_path = args.monitor_md or jsonl_path.with_suffix(".md")
    request = ResearchRequest(
        question=args.request,
        context=args.context,
        constraints=args.constraint,
    )

    run = asyncio.run(
        run_research(
            request,
            chatter=chatter,
            monitor_jsonl=jsonl_path,
            report_formats=args.report_formats,
        )
    )
    run.monitor.write_markdown(md_path)

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
