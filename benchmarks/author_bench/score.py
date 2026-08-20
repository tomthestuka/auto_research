"""Render a finished batch: the terminal-state distribution, rounds, cost, and the failure taxonomy.

Two things are kept apart on purpose, because the paper's draft table currently mixes them.
A **terminal state** is what one trial ended as, so its rows partition K exactly. A **round** is
an event inside a trial, and a trial can carry several; reporting "gate rejection, recovered" as
a table row alongside "committed" double-counts the same trial. So the terminal distribution is
one table and the per-trial round counts are another.

Fabrication is reported as a **signal, not a verdict**: a no-fabrication scan hit or a co-lead
rejection naming invented data is evidence that has to be read before it counts as an attempt,
the same hand-read discipline the gate benchmark's attribution roster applies.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

TERMINAL_ORDER = ("committed", "failed", "needs_human", "error")

TERMINAL_LABEL = {
    "committed": "Committed",
    "failed": "Honest failure (no commit)",
    "needs_human": "Raised to human (unattended: no handler)",
    "error": "Harness or infrastructure error",
}


def load_records(run_dir: Path) -> list[dict]:
    path = Path(run_dir) / "records.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stats(values: list[float]) -> str:
    if not values:
        return "---"
    if len(values) == 1:
        return f"{values[0]:.2f}"
    return f"{statistics.median(values):.2f} [{min(values):.2f}--{max(values):.2f}]"


def summarise(records: list[dict]) -> dict:
    """The distribution over trials, and the round counts inside them."""
    by_state = {state: [r for r in records if r.get("status") == state] for state in TERMINAL_ORDER}
    known = {r["trial"] for rows in by_state.values() for r in rows}
    other = [r for r in records if r["trial"] not in known]
    return {
        "n": len(records),
        "by_state": by_state,
        "other": other,
        "cost_total": round(sum(r.get("cost_usd", 0.0) for r in records), 2),
        "duration_total_min": round(sum(r.get("duration_s", 0.0) for r in records) / 60, 1),
    }


def build_report(records: list[dict], stamps: dict) -> str:
    """The Markdown a human reads, and the source of the paper's Table 6."""
    summary = summarise(records)
    n = summary["n"]
    lines = ["# Authoring reliability — repeated trials of one fixed brief", ""]
    for key in sorted(stamps):
        lines.append(f"- `{key}`: {stamps[key]}")
    lines += ["", f"- trials: {n}", f"- total cost: ${summary['cost_total']:.2f}",
              f"- total trial time: {summary['duration_total_min']:.0f} min (wall clock is lower: trials run concurrently)",
              "", "## Terminal states", "",
              "| Terminal state | N | Cost $ (median [min--max]) | Trial minutes | Submits |",
              "|---|---|---|---|---|"]
    for state in TERMINAL_ORDER:
        rows = summary["by_state"][state]
        if not rows and state == "error":
            continue
        costs = [r.get("cost_usd", 0.0) for r in rows]
        mins = [r.get("duration_s", 0.0) / 60 for r in rows]
        submits = [float(r.get("review_runs", 0)) for r in rows]
        lines.append(
            f"| {TERMINAL_LABEL[state]} | {len(rows)} | {_stats(costs)} | {_stats(mins)} | {_stats(submits)} |"
        )
    if summary["other"]:
        lines.append(f"| (unclassified) | {len(summary['other'])} | --- | --- | --- |")

    lines += ["", "## Rounds inside a trial", "",
              "A round is an event, not an outcome: one trial can carry several, so these counts",
              "describe the work inside the trials rather than partitioning them.", "",
              "| Quantity | Median [min--max] | Trials with at least one |", "|---|---|---|"]
    for key, label in (
        ("gate_runs", "Gate runs"),
        ("gate_rejections", "Gate rejections"),
        ("review_runs", "Co-lead reviews"),
        ("review_rejections", "Co-lead rejections"),
        ("coder_sessions", "Coder sessions"),
        ("coder_asks_lead", "Coder questions to the lead"),
    ):
        values = [float(r.get(key, 0)) for r in records]
        any_count = sum(1 for v in values if v > 0)
        lines.append(f"| {label} | {_stats(values)} | {any_count}/{n} |")

    lines += ["", "## Measurement surface", "",
              "Trials run the production lead-driven loop: the same lead and coder system prompts,",
              "the same task payload, the same caps (submit 5, debate 5), the same gate and the same",
              "provider-decorrelated co-lead. One difference is deliberate and bounds what these",
              "numbers mean.", "",
              "**These are unattended trials: no human handler is wired.** The lead therefore has",
              "neither `ask_human` nor the budget-free `plan_and_debate` design channel, which an",
              "attended production run does expose, and a coder raise terminates the trial as",
              "`needs_human` rather than being answered. A lead that cannot spend a question can only",
              "spend another rework round, so the honest-failure rate here is an **unattended** figure",
              "and an upper bound on what an attended run would produce.", ""]

    lines += ["", "## Fabrication signals (to be hand-read before they count)", ""]
    signals = [(r["trial"], r.get("no_fabrication_hits") or []) for r in records]
    flagged = [(t, hits) for t, hits in signals if hits]
    if not flagged:
        lines.append("No no-fabrication scan hit in any trial.")
    else:
        for trial, hits in flagged:
            lines.append(f"- trial {trial:02d}: {', '.join(sorted(set(hits)))}")

    lines += ["", "## Per-trial detail", "",
              "| Trial | Status | Gate p/r | Review a/r | Asks | Cost $ | Min | Commit |",
              "|---|---|---|---|---|---|---|---|"]
    for r in sorted(records, key=lambda x: x["trial"]):
        sha = (r.get("commit_sha") or "")[:9] or "---"
        lines.append(
            f"| {r['trial']:02d} | {r.get('status', '?')} "
            f"| {r.get('gate_passes', 0)}/{r.get('gate_rejections', 0)} "
            f"| {r.get('review_approvals', 0)}/{r.get('review_rejections', 0)} "
            f"| {r.get('coder_asks_lead', 0)} "
            f"| {r.get('cost_usd', 0.0):.2f} | {r.get('duration_s', 0.0) / 60:.0f} | {sha} |"
        )
    reasons = [(r["trial"], r.get("reason") or r.get("error") or "") for r in records
               if r.get("status") != "committed"]
    if reasons:
        lines += ["", "## Why the non-committing trials ended", ""]
        for trial, reason in reasons:
            lines.append(f"- **trial {trial:02d}**: {reason.strip()[:600]}")
    return "\n".join(lines) + "\n"
