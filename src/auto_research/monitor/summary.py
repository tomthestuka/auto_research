"""End-of-run summary, derived from the monitor event stream.

The monitor JSONL is a run's single source of truth ([J1]), so the summary is **computed from the
events** — never assembled by hand at the call site — and then stored back as one ``run.summary``
event. Every consumer renders that stored dict: the live stream, the detached follower tailing
``monitor.jsonl``, ``ResearchRun.summary()`` in a notebook, and a replay of a run that died. A run
that crashed therefore ends on the same digest as one that finished.

What it answers, all from events already recorded: what the verdict was, how long it took, what it
cost (total, by role, and the coder's Agent-SDK share), which tools were authored and committed,
how much rework the gate/co-lead demanded, how often a human was raised to, and where the report
artifacts landed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


# Events that mean "a human was asked something": a seat's `message(to="human")` (the router's
# `channel.message` with to=human, E12) and the coder's own raise.
_HITL_TOOLS = ("channel.message", "coder.ask_human")


def _as_dict(event: Any) -> dict[str, Any]:
    """Normalize a ``MonitorEvent`` or a plain JSONL dict into a dict."""

    if isinstance(event, Mapping):
        return dict(event)
    dump = getattr(event, "model_dump", None)
    return dump(mode="json") if callable(dump) else dict(vars(event))


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def build_run_summary(events: Iterable[Any]) -> dict[str, Any]:
    """Derive the summary dict from a run's monitor events (dicts or ``MonitorEvent``s)."""

    rows = [_as_dict(event) for event in events]
    rows.sort(key=lambda row: row.get("sequence") or 0)

    summary: dict[str, Any] = {
        "run_id": rows[0].get("run_id", "") if rows else "",
        "status": "unknown",
        "events": len(rows),
        "cost_usd": 0.0,
        "llm_calls": 0,
        "cost_by_role": {},
        "cost_by_model": {},
        "coder_usd": 0.0,
        "tools_authored": [],
        "authoring": {"sessions": 0, "gate_passed": 0, "gate_rejected": 0,
                      "review_approved": 0, "review_rejected": 0},
        "hitl_raises": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "reports": {},
    }
    if not rows:
        return summary

    first_time = _parse_time(rows[0].get("created_at"))
    last_time = _parse_time(rows[-1].get("created_at"))
    if first_time and last_time:
        summary["started_at"] = first_time.isoformat()
        summary["finished_at"] = last_time.isoformat()
        summary["wall_clock_s"] = max(0.0, (last_time - first_time).total_seconds())

    accepted: bool | None = None
    # Spend is the SUM of the priced calls, not the highest stamped running total: a resumed run
    # builds a fresh meter, so its stamps restart mid-ledger and the maximum under-reports the run
    # (live: a $8.85 headline against a $8.98 by-role sum). The stamp maximum stays the fallback
    # for a ledger that carries stamps but no cost.call rows.
    stamped_max = 0.0
    summed = 0.0
    for row in rows:
        name = str(row.get("event", ""))
        data = row.get("data") or {}
        total = row.get("cost_usd_total")
        if isinstance(total, int | float):
            stamped_max = max(stamped_max, float(total))

        if name == "cost.call":
            summary["llm_calls"] += 1
            spend = float(data.get("total_cost_usd", 0.0) or 0.0)
            summed += spend
            for field, bucket in (("role", "cost_by_role"), ("model", "cost_by_model")):
                key = str(data.get(field, "?"))
                summary[bucket][key] = summary[bucket].get(key, 0.0) + spend
            if data.get("price_source") == "claude_sdk":
                summary["coder_usd"] = float(summary["coder_usd"]) + spend
        elif name == "commit":
            summary["tools_authored"].append(
                {"tool": data.get("tool") or "?", "commit_sha": str(data.get("commit_sha", ""))[:10]}
            )
        elif name == "coder.session_start":
            summary["authoring"]["sessions"] += 1
        elif name == "gate.result":
            key = "gate_passed" if data.get("passed") else "gate_rejected"
            summary["authoring"][key] += 1
        elif name == "review.result":
            key = "review_approved" if data.get("approved") else "review_rejected"
            summary["authoring"][key] += 1
        elif name == "coder.ask_human":
            summary["hitl_raises"] += 1
        elif name == "run.start":
            for field in ("run_dir", "monitor_jsonl"):
                if isinstance(data.get(field), str) and data[field]:
                    summary[field] = data[field]
        elif name == "run.report":
            summary["reports"] = {k: v for k, v in data.items() if isinstance(v, str)}
        elif name == "run.error":
            summary["status"] = "error"
            summary["error"] = str(data.get("error", ""))
        elif name == "run.honest_failure":
            summary["status"] = "honest_failure"
        elif name == "reviewer.review.verdict":
            verdict = data.get("verdict")
            if isinstance(verdict, Mapping) and verdict.get("assign_to") == "human":
                accepted = bool(verdict.get("accepted"))
                summary["verdict_rationale"] = str(verdict.get("rationale", ""))
        elif name == "run.finish":
            summary["lit_iterations"] = data.get("lit_iterations")
            summary["analysis_iterations"] = data.get("analysis_iterations")
            summary["human_rounds"] = data.get("human_rounds")

        if name in _HITL_TOOLS and (name != "channel.message" or data.get("to") == "human"):
            summary["hitl_raises"] += 1
        elif name.endswith(".tool_result"):
            summary["tool_calls"] += 1
            if data.get("is_error"):
                summary["tool_errors"] += 1

    if summary["status"] == "unknown" and accepted is not None:
        summary["status"] = "accepted" if accepted else "not accepted"
    summary["cost_usd"] = summed if summary["llm_calls"] else stamped_max
    return summary


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _fmt_clock(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.strftime("%H:%M:%S") if parsed else "?"


def _top_costs(totals: Mapping[str, Any], limit: int = 4) -> str:
    rows = sorted(
        ((name, float(cost)) for name, cost in totals.items() if float(cost) > 0),
        key=lambda item: item[1],
        reverse=True,
    )[:limit]
    return " · ".join(f"{name} ${cost:.2f}" for name, cost in rows) or "-"


def render_run_summary(summary: Mapping[str, Any]) -> str:
    """Render a stored summary dict as the plain-text block shown at the end of a run."""

    rule = "─" * 78
    run_id = str(summary.get("run_id", ""))[:12]
    status = str(summary.get("status", "unknown"))
    lines = [rule, f"RUN SUMMARY  {run_id}  —  {status}"]

    wall = _fmt_duration(summary.get("wall_clock_s"))
    span = f"{_fmt_clock(summary.get('started_at'))} → {_fmt_clock(summary.get('finished_at'))}"
    lines.append(f"  wall clock   {wall}  ({span})")

    cost = float(summary.get("cost_usd", 0.0) or 0.0)
    calls = int(summary.get("llm_calls", 0) or 0)
    coder = float(summary.get("coder_usd", 0.0) or 0.0)
    coder_note = f"  (coder SDK ${coder:.2f})" if coder else ""
    lines.append(f"  cost         ${cost:.4f} over {calls} LLM calls{coder_note}")
    by_role = summary.get("cost_by_role") or {}
    if by_role:
        lines.append(f"               {_top_costs(by_role)}")

    authored = summary.get("tools_authored") or []
    authoring = summary.get("authoring") or {}
    if authored or authoring.get("sessions"):
        landed = ", ".join(
            f"{item.get('tool', '?')} → {item.get('commit_sha', '')}" for item in authored
        ) or "none committed"
        lines.append(f"  tools        {len(authored)} authored: {landed}")
        lines.append(
            f"               {authoring.get('sessions', 0)} coder session(s) · "
            f"gate {authoring.get('gate_passed', 0)}✓/{authoring.get('gate_rejected', 0)}✗ · "
            f"co-lead {authoring.get('review_approved', 0)}✓/{authoring.get('review_rejected', 0)}✗"
        )

    lines.append(
        f"  iterations   literature {summary.get('lit_iterations', '?')} · "
        f"analysis {summary.get('analysis_iterations', '?')} · "
        f"reports to the human {summary.get('human_rounds', '?')} · "
        f"HITL raises {summary.get('hitl_raises', 0)}"
    )
    lines.append(
        f"  tool calls   {summary.get('tool_calls', 0)} "
        f"({summary.get('tool_errors', 0)} error(s))  ·  {summary.get('events', 0)} events"
    )

    # One labelled line per artifact, full path — the point of the digest is that you can open
    # what the run produced without going hunting. The run dir and the ledger are always printed,
    # so a run that produced no report still says where to look.
    reports = summary.get("reports") or {}
    for key, label in (("report_jsonl", "jsonl"), ("report_latex", "tex"), ("report_pdf", "pdf")):
        if reports.get(key):
            lines.append(f"  report {label:<6}{reports[key]}")
    for key, label in (("run_dir", "run dir"), ("monitor_jsonl", "ledger")):
        if summary.get(key):
            lines.append(f"  {label:<13}{summary[key]}")

    rationale = str(summary.get("verdict_rationale", "") or "")
    if rationale:
        lines.append(f"  verdict      {_clip(rationale, 200)}")
    error = str(summary.get("error", "") or "")
    if error:
        lines.append(f"  error        {_clip(error, 300)}")
    lines.append(rule)
    return "\n".join(lines) + "\n"


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "~"


def summarize_jsonl(path: str | Path) -> dict[str, Any]:
    """Build a run summary straight from a monitor JSONL file (full fidelity, any chatter).

    Reading the file rather than a live monitor's in-memory events matters: ``cost.call`` events are
    level 10, so at any chatter below 10 they exist only in the log.
    """

    import json

    rows: list[dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return build_run_summary([])
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return build_run_summary(rows)


def summary_from_events(events: Sequence[Any]) -> dict[str, Any] | None:
    """Return the ``run.summary`` payload already recorded in ``events``, else ``None``.

    Consumers prefer the stored dict over recomputing, so what a reader sees is exactly what the
    run recorded (and a chatter-filtered in-memory event list still carries it — it is level 1).
    """

    for row in reversed([_as_dict(event) for event in events]):
        if str(row.get("event", "")) == "run.summary" and row.get("data"):
            return dict(row["data"])
    return None
