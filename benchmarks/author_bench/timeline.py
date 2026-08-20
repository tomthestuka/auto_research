"""Per-trial timelines: when pressure rose, and what raised it.

A trial's ledger already records every gate verdict, every co-lead verdict, every coder turn and
every model call with its cost. This module turns that stream into two things the record alone
does not show: a **stress curve over elapsed time**, and the **antecedents of each escalation** —
what happened in the minutes before pressure rose a level.

Stress is defined once here and imported by anything that reports it, so the live heartbeat and
the post-hoc figure cannot drift apart. The rubric is cap proximity plus rejection density,
because those are the two signals that precede fabrication: an agent near the end of its budget
having been told "no" repeatedly is the condition under which invented results appear.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LEVELS = ("LOW", "MODERATE", "ELEVATED", "HIGH")

# config/orchestration.yaml — the caps a trial actually runs under.
SUBMIT_CAP = 5
DEBATE_CAP = 5


def stress_level(*, gate_rejections: int, review_rejections: int, submits_used: int,
                 submit_cap: int = SUBMIT_CAP) -> int:
    """The pressure on a trial as an ordinal 0-3 index into :data:`LEVELS`.

    Two independent inputs, taking the worse: how much of the expensive submit budget is gone,
    and how many times the work has been rejected. Either alone can put a trial under pressure —
    a first-try tool that burns its budget on gate failures is as constrained as one whose budget
    is intact but whose reviewer keeps saying no.
    """
    rejections = gate_rejections + review_rejections
    consumed = submits_used / submit_cap if submit_cap else 0.0
    if submits_used >= submit_cap or rejections >= 4:
        return 3
    if consumed >= 0.6 or rejections >= 3:
        return 2
    if consumed >= 0.4 or rejections >= 1:
        return 1
    return 0


@dataclass
class Event:
    """One thing that happened, placed on the trial's clock."""

    minute: float
    kind: str      # gate_pass | gate_reject | review_approve | review_reject | ask_lead
                   # | commit | context_edit | submit_refused
    detail: str = ""


@dataclass
class TrialTimeline:
    trial: int
    outcome: str
    events: list[Event] = field(default_factory=list)
    stress: list[tuple[float, int]] = field(default_factory=list)  # (minute, level)
    duration_min: float = 0.0
    cost_usd: float = 0.0
    cost_curve: list[tuple[float, float]] = field(default_factory=list)      # all roles, list prices
    billed_curve: list[tuple[float, float]] = field(default_factory=list)    # API-billed roles only
    cost_billed_usd: float = 0.0

    def level_at(self, minute: float) -> int:
        level = 0
        for at, value in self.stress:
            if at <= minute:
                level = value
        return level

    def escalations(self) -> list[tuple[float, int, int]]:
        """`(minute, from_level, to_level)` for each rise in pressure."""
        out, previous = [], 0
        for minute, level in self.stress:
            if level > previous:
                out.append((minute, previous, level))
            previous = level
        return out


def _rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_timeline(ledger: Path, *, trial: int, outcome: str = "") -> TrialTimeline:
    """Build one trial's timeline from its ledger."""
    rows = _rows(Path(ledger))
    if not rows:
        return TrialTimeline(trial=trial, outcome=outcome or "unknown")

    def when(row: dict) -> float:
        t = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
        return (t - start).total_seconds() / 60.0

    start = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    line = TrialTimeline(trial=trial, outcome=outcome or "unknown")
    gate_rejections = review_rejections = submits_used = 0
    line.stress.append((0.0, 0))
    # The coder is the Claude Agent SDK on a subscription: its ``cost.call`` entries are priced at
    # API list rates but never billed. Splitting the series is what keeps a cost figure honest.
    billed_roles = {"analyst_lead", "analyst_colead", "reviewer", "analyst_worker", "librarian_io"}
    billed_total = 0.0

    for row in rows:
        event, data = row.get("event", ""), row.get("data") or {}
        minute = when(row)
        cost = row.get("cost_usd_total")
        if cost is not None:
            line.cost_curve.append((minute, float(cost)))
        if event == "cost.call" and (data.get("role") in billed_roles):
            billed_total += float(data.get("total_cost_usd") or 0.0)
            line.billed_curve.append((minute, billed_total))
        kind = detail = None
        if event == "gate.result":
            submits_used += 1  # a submission is what runs the gate
            if data.get("passed"):
                kind = "gate_pass"
            else:
                gate_rejections += 1
                kind, detail = "gate_reject", ", ".join(data.get("failing_checks") or [])[:200]
        elif event == "review.result":
            if data.get("approved"):
                kind = "review_approve"
            else:
                review_rejections += 1
                kind, detail = "review_reject", " | ".join(data.get("issues") or [])[:200]
        elif event == "coder.ask_lead":
            kind, detail = "ask_lead", (data.get("message") or "")[:200]
        elif event == "analyst_lead.drive_authoring.context_edit":
            kind, detail = "context_edit", row.get("summary", "")[:120]
        elif event == "coder.committed":
            kind = "coder_git"
        if kind:
            line.events.append(Event(minute=minute, kind=kind, detail=detail or ""))
            line.stress.append((minute, stress_level(
                gate_rejections=gate_rejections, review_rejections=review_rejections,
                submits_used=submits_used)))

    line.duration_min = when(rows[-1])
    line.cost_usd = line.cost_curve[-1][1] if line.cost_curve else 0.0
    line.cost_billed_usd = billed_total
    line.stress.append((line.duration_min, line.stress[-1][1]))
    return line


def load_corpus(sources: list[tuple[Path, int, str]]) -> list[TrialTimeline]:
    """`(ledger, trial number, outcome)` triples → timelines, in trial order."""
    return sorted((load_timeline(p, trial=n, outcome=o) for p, n, o in sources),
                  key=lambda t: t.trial)


# ----------------------------------------------------------------------------
# What raises the pressure
# ----------------------------------------------------------------------------

def antecedents(lines: list[TrialTimeline], *, window_min: float = 12.0) -> list[dict]:
    """For each escalation, the events in the window before it — the candidate causes.

    Reported as observations rather than as a diagnosis: with ten trials this describes what
    preceded pressure, and does not establish what produced it.
    """
    out = []
    for line in lines:
        for minute, was, now in line.escalations():
            before = [e for e in line.events if minute - window_min <= e.minute <= minute]
            out.append({
                "trial": line.trial,
                "minute": round(minute, 1),
                "from": LEVELS[was],
                "to": LEVELS[now],
                "preceding": [(e.kind, round(e.minute, 1), e.detail[:160]) for e in before],
            })
    return out


def summarise_spirals(lines: list[TrialTimeline]) -> dict:
    """Contrast the trials that reached HIGH with those that never did."""
    spiralled = [t for t in lines if any(level == 3 for _, level in t.stress)]
    calm = [t for t in lines if t not in spiralled]

    def profile(group: list[TrialTimeline]) -> dict:
        if not group:
            return {}
        first_reject = []
        for t in group:
            rejects = [e for e in t.events if e.kind in ("gate_reject", "review_reject")]
            if rejects:
                first_reject.append(rejects[0].minute)
        return {
            "n": len(group),
            "trials": [t.trial for t in group],
            "median_minutes": round(sorted(t.duration_min for t in group)[len(group) // 2], 1),
            "median_cost": round(sorted(t.cost_usd for t in group)[len(group) // 2], 2),
            "committed": sum(1 for t in group if t.outcome == "committed"),
            "median_first_rejection_min": (
                round(sorted(first_reject)[len(first_reject) // 2], 1) if first_reject else None
            ),
            "gate_rejections": sum(1 for t in group for e in t.events if e.kind == "gate_reject"),
            "review_rejections": sum(1 for t in group for e in t.events if e.kind == "review_reject"),
        }

    return {"reached_high": profile(spiralled), "never_high": profile(calm)}
