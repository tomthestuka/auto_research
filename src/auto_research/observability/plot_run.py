"""Visualise an auto_research monitor JSONL — per-role call timeline + cost pie.

Usage:
    python -m auto_research.observability.plot_run <monitor.jsonl> [output.png]

**Every priced call, every role.** The chart is driven by the run's own `cost.call` events — one
per priced call, carrying the role, member, model, tokens and dollars the ledger recorded at call
time. That matters because the two things this file used to do both under-reported:

* The timeline paired `<role>.<action>.input` → `.output` events, which only structured composes
  emit. On a real run that is a fraction of the calls (54 of 282 on `8c75990e`), and the **coder
  was absent entirely** — it runs in the Agent SDK and emits `coder.turn_end`, never that pair.
* The pie keyed slices on `(team, model)`, so two roles sharing a model merged into one slice: the
  GPT co-lead's spend and a GPT analyst worker's were added together under "analyst /
  gpt-5.6-terra", hiding the provider-decorrelated reviewer's real cost.

Both now key on **role**, so a role is never folded into another's slice and never missing.

For logs written before `cost.call` existed, the old pair-matching estimate is still used as a
fallback, priced from `config/models.yaml` through the same litellm-first path as the ledger.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import yaml

from auto_research.config_paths import models_yaml


_TEAM_FOR_ROLE = {
    "reviewer": "reviewer",
    "analyst_lead": "analyst",
    "analyst_worker": "analyst",
    "analyst_colead": "analyst",
    "librarian_io": "librarian",
    "librarian_searcher": "librarian",
    "coder": "tooling",
    "status_monitor": "run",
}

#: One colour per role — the unit the chart groups by. Two roles on the same model must stay
#: visually distinct (the co-lead and a GPT worker were previously indistinguishable *and*
#: summed).
_ROLE_COLORS = {
    "reviewer": "#E05C5C",
    "analyst_lead": "#3A6A98",
    "analyst_worker": "#89BCDE",
    "analyst_colead": "#7E5AA8",
    "librarian_io": "#5BAE85",
    "librarian_searcher": "#8FCFB0",
    "coder": "#C08A3E",
    "status_monitor": "#9A9A9A",
}
_ROLE_LABEL = {
    "reviewer": "reviewer",
    "analyst_lead": "analyst lead",
    "analyst_worker": "analyst worker",
    "analyst_colead": "analyst co-lead (review)",
    "librarian_io": "librarian IO",
    "librarian_searcher": "librarian searcher",
    "coder": "coder (authoring)",
    "status_monitor": "/status reporter",
}
#: Top-to-bottom lane order; anything unknown is appended, so a new role still appears.
_ROLE_ORDER = [
    "reviewer",
    "librarian_io",
    "librarian_searcher",
    "analyst_lead",
    "analyst_worker",
    "analyst_colead",
    "coder",
    "status_monitor",
]


def _load_models_yaml(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else models_yaml()
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_rates(roles: dict) -> dict:
    """{model_id: TokenRates} — the litellm-first / config-fallback rates the cost ledger uses.

    Only needed for the pre-`cost.call` fallback path; a modern log carries its own prices.
    """
    from auto_research.models.types import RoleModelConfig
    from auto_research.observability.costs import token_rates_for

    rates: dict = {}
    for group in roles.values():
        for member in group.get("members", []):
            model = member.get("model", "")
            if not model or model in rates:
                continue
            try:
                cfg = RoleModelConfig(
                    name=member.get("name", model),
                    provider=member["provider"],
                    model=model,
                    priceMTk=member.get("priceMTk", {}),
                )
                rates[model] = token_rates_for(cfg)
            except Exception:  # noqa: BLE001 — a malformed member just drops out of the chart
                continue
    return rates


def _calls_from_cost_events(events: list[dict], to_min) -> list[dict]:
    """One record per priced call, from the run's own ledger. Complete by construction."""
    calls = []
    for event in events:
        if event.get("event") != "cost.call":
            continue
        data = event.get("data") or {}
        role = str(data.get("role") or "unknown")
        calls.append(
            {
                "role": role,
                "member": data.get("member") or "",
                "model": data.get("model") or "?",
                "at": to_min(event["created_at"]),
                "cost": float(data.get("total_cost_usd", 0.0) or 0.0),
                "in_tok": int(data.get("input_tokens", 0) or 0),
                "out_tok": int(data.get("output_tokens", 0) or 0),
                "cache_read": int(data.get("cache_read_tokens", 0) or 0),
                "source": data.get("price_source") or "?",
            }
        )
    return calls


def _calls_from_pairs(events: list[dict], rates: dict, roles_cfg: dict, to_min) -> list[dict]:
    """Fallback for logs predating `cost.call`: match `.input` → `.output` and price it."""
    model_for_role: dict[str, str] = {}
    for role, group in roles_cfg.items():
        members = group.get("members", [])
        if members:
            model_for_role[role] = members[0]["model"]

    calls: list[dict] = []
    used: set[int] = set()
    for start in events:
        name = start.get("event", "")
        if not name.endswith(".input"):
            continue
        base, role = name[:-6], name.split(".")[0]
        for end in events:
            if (
                end.get("event") == base + ".output"
                and end["sequence"] > start["sequence"]
                and end["sequence"] not in used
                and "usage" in (end.get("data") or {})
            ):
                used.add(end["sequence"])
                usage = end["data"]["usage"]
                model = model_for_role.get(role, "?")
                rate = rates.get(model)
                cost = (
                    usage.get("input_tokens", 0) * rate.input
                    + usage.get("output_tokens", 0) * rate.output
                    + usage.get("cache_read_tokens", 0) * rate.cache_read
                    + usage.get("cache_write_tokens", 0) * rate.cache_write
                ) if rate else 0.0
                calls.append(
                    {
                        "role": role,
                        "member": "",
                        "model": model,
                        "at": to_min(end["created_at"]),
                        "cost": cost,
                        "in_tok": usage.get("input_tokens", 0),
                        "out_tok": usage.get("output_tokens", 0),
                        "cache_read": usage.get("cache_read_tokens", 0),
                        "source": "estimated",
                    }
                )
                break
    return calls


def _slice_key(call: dict) -> tuple[str, str]:
    """Pie/table key: **role first**. Two roles on one model stay separate slices."""
    return (call["role"], call["model"])


def main() -> None:  # noqa: C901
    if len(sys.argv) < 2:
        sys.exit(
            "Usage: python -m auto_research.observability.plot_run <monitor.jsonl> [output.png]"
        )

    log = Path(sys.argv[1])
    if not log.exists():
        sys.exit(f"File not found: {log}")
    out = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(log.stem + ".viz.png")

    events = []
    with open(log) as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    if not events:
        sys.exit("Log file is empty.")
    events.sort(key=lambda e: e["sequence"])

    t0 = datetime.fromisoformat(events[0]["created_at"].replace("Z", "+00:00"))

    def to_min(stamp: str) -> float:
        return (
            datetime.fromisoformat(stamp.replace("Z", "+00:00")) - t0
        ).total_seconds() / 60

    calls = _calls_from_cost_events(events, to_min)
    if calls:
        print(f"Read {len(calls)} priced calls from the run's cost ledger.")
    else:
        roles_cfg = _load_models_yaml().get("roles", {})
        calls = _calls_from_pairs(events, _build_rates(roles_cfg), roles_cfg, to_min)
        print(f"No cost.call events; estimated {len(calls)} calls from .input/.output pairs.")
    if not calls:
        sys.exit("No priced calls found in this log.")

    lanes = [r for r in _ROLE_ORDER if any(c["role"] == r for c in calls)]
    lanes += sorted({c["role"] for c in calls} - set(lanes))
    lane_y = {role: i for i, role in enumerate(lanes)}

    cost_by: dict[tuple[str, str], float] = defaultdict(float)
    count_by: dict[tuple[str, str], int] = defaultdict(int)
    for call in calls:
        cost_by[_slice_key(call)] += call["cost"]
        count_by[_slice_key(call)] += 1
    total_cost = sum(cost_by.values())

    # ── Figure ────────────────────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(19, 7), gridspec_kw={"width_ratios": [2.2, 1]}
    )
    fig.patch.set_facecolor("#F8F8F8")

    # Left: one lane per role, one mark per call, width ∝ cost. `cost.call` records when a call
    # was priced (its completion), not a span — so these are marks in time, not durations.
    ax1.set_facecolor("#FAFAFA")
    span = max((c["at"] for c in calls), default=1.0) or 1.0
    widest = max((c["cost"] for c in calls), default=0.0) or 1.0
    for call in calls:
        y = lane_y[call["role"]]
        # Scale each mark between a visible floor and ~2% of the run, so a $12 coder turn reads
        # heavier than a $0.02 status call without swamping the axis.
        width = span * (0.004 + 0.016 * (call["cost"] / widest))
        ax1.broken_barh(
            [(call["at"] - width / 2, width)],
            (y - 0.32, 0.64),
            facecolors=_ROLE_COLORS.get(call["role"], "#BBBBBB"),
            edgecolors="none",
            alpha=0.85,
        )
    for i in range(len(lanes)):
        ax1.axhline(i + 0.5, color="#E4E4E4", linewidth=0.5, zorder=0)

    # Cumulative spend, so "where did the money go, and when" is answerable from one glance.
    ax_cum = ax1.twinx()
    running, series = 0.0, []
    for call in sorted(calls, key=lambda c: c["at"]):
        running += call["cost"]
        series.append((call["at"], running))
    ax_cum.plot(
        [p[0] for p in series], [p[1] for p in series],
        color="#333333", linewidth=1.4, alpha=0.75, zorder=5,
    )
    ax_cum.set_ylabel("cumulative $", fontsize=9, color="#333333")
    ax_cum.tick_params(labelsize=8, colors="#333333")
    ax_cum.set_ylim(0, max(total_cost * 1.05, 0.01))

    ax1.set_yticks(list(lane_y.values()))
    ax1.set_yticklabels([_ROLE_LABEL.get(r, r) for r in lanes], fontsize=9)
    ax1.set_xlabel("Time from run start (minutes)  ·  mark width ∝ call cost", fontsize=10)
    ax1.set_title(
        f"Every priced call — {log.stem}  ({len(calls)} calls)",
        fontsize=11, fontweight="bold", pad=10,
    )
    ax1.set_ylim(-0.8, len(lanes) - 0.2)
    ax1.spines[["top"]].set_visible(False)

    # Right: cost by ROLE and model.
    ax2.set_facecolor("#FAFAFA")
    slices = sorted(
        ((k, v) for k, v in cost_by.items() if v > 0),
        key=lambda kv: (_ROLE_ORDER.index(kv[0][0]) if kv[0][0] in _ROLE_ORDER else 99, -kv[1]),
    )
    if slices:
        sizes = [v for _, v in slices]
        colors = [_ROLE_COLORS.get(role, "#BBBBBB") for (role, _), _ in slices]
        labels = [f"{_ROLE_LABEL.get(role, role)}\n{model}" for (role, model), _ in slices]

        # Below ~3% the outer label and the inner percentage collide with their neighbours and
        # the chart stops being readable; the legend names every role, so small slices rely on it.
        _MIN_LABEL_SHARE = 3.0

        def _autopct(pct: float) -> str:
            if pct < _MIN_LABEL_SHARE:
                return ""
            return f"${pct / 100 * total_cost:,.2f}\n({pct:.1f}%)"

        shown = [
            label if 100 * size / total_cost >= _MIN_LABEL_SHARE else ""
            for label, size in zip(labels, sizes)
        ]
        _, texts, autotexts = ax2.pie(
            sizes,
            labels=shown,
            colors=colors,
            autopct=_autopct,
            startangle=90,
            counterclock=False,
            pctdistance=0.70,
            labeldistance=1.22,
            wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
        )
        for at in autotexts:
            at.set_fontsize(8)
            at.set_color("white")
            at.set_fontweight("bold")
        for t in texts:
            t.set_fontsize(8.5)

    ax2.set_title(
        f"Dollar cost by role & model\nTotal: ${total_cost:,.2f}",
        fontsize=11, fontweight="bold", pad=16,
    )
    ax2.legend(
        handles=[
            mpatches.Patch(color=_ROLE_COLORS.get(r, "#BBBBBB"), label=_ROLE_LABEL.get(r, r))
            for r in lanes
        ],
        loc="lower center", bbox_to_anchor=(0.5, -0.16), ncol=3,
        fontsize=8, framealpha=0.85, edgecolor="#CCCCCC",
    )

    plt.tight_layout(pad=2.5)
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved → {out}")

    print(f"\nCost by role & model  ({len(calls)} calls, {span:.0f} min):")
    print(f"  {'role':26s} {'model':18s} {'calls':>6s} {'$':>10s} {'share':>7s}")
    for (role, model), cost in slices:
        share = 100 * cost / total_cost if total_cost else 0.0
        print(f"  {role:26s} {model:18s} {count_by[(role, model)]:6d} "
              f"{cost:10.2f} {share:6.1f}%")
    print(f"  {'TOTAL':26s} {'':18s} {len(calls):6d} {total_cost:10.2f} {100.0:6.1f}%")

    tokens: dict[str, int] = defaultdict(int)
    for call in calls:
        tokens["input"] += call["in_tok"]
        tokens["output"] += call["out_tok"]
        tokens["cache_read"] += call["cache_read"]
    by_source: dict[str, float] = defaultdict(float)
    for call in calls:
        by_source[call["source"]] += call["cost"]
    print("\n  tokens: " + "  ".join(f"{k}={v:,}" for k, v in tokens.items()))
    print("  priced by: " + "  ".join(f"{k}=${v:,.2f}" for k, v in sorted(by_source.items())))


if __name__ == "__main__":
    main()
