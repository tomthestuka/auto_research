"""Visualise an auto_research monitor JSONL — Gantt timeline + cost pie.

Usage:
    python -m auto_research.observability.plot_run <monitor.jsonl> [output.png]

Pricing and model assignment are read from config/models.yaml so the chart
stays accurate when models are swapped in config without touching this file.
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

_MODELS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "models.yaml"

_TEAM_FOR_ROLE = {
    "reviewer": "reviewer",
    "analyst_lead": "analyst",
    "analyst_worker": "analyst",
    "librarian_io": "librarian",
}
_TEAM_COLORS = {
    "reviewer":  "#E05C5C",
    "analyst":   "#5B8DB8",
    "librarian": "#5BAE85",
    "run":       "#AAAAAA",
}
# Per-team color palettes: analyst slot 0 = lead, slots 1+ = workers in order.
_TEAM_PALETTES: dict[str, list[str]] = {
    "reviewer":  ["#E05C5C"],
    "analyst":   ["#3A6A98", "#89BCDE", "#2D4B7A", "#7AAECF"],
    "librarian": ["#5BAE85"],
}
_ACTIVITY_LABEL = {
    "reviewer.review":                        "reviewer · review",
    "reviewer.compose_machine_report":         "reviewer · machine report",
    "reviewer.compose_latex_report":           "reviewer · LaTeX report",
    "analyst_lead.judge_convergence":          "analyst lead · judge conv.",
    "analyst_lead.adjudicate":                 "analyst lead · adjudicate",
    "analyst_worker.analyze":                  "analyst worker · analyze",
    "analyst_worker.request_tools":            "analyst worker · tool call",
    "librarian_io.compose_search_strategy":    "librarian IO · search strategy",
    "librarian_io.ingest_document":            "librarian IO · ingest doc",
    "librarian_io.judge_abstract_convergence": "librarian IO · judge conv.",
    "librarian_io.adjudicate_abstract":        "librarian IO · adjudicate",
}
_ACTIVITY_ORDER = [
    "reviewer.review",
    "reviewer.compose_machine_report",
    "reviewer.compose_latex_report",
    "librarian_io.compose_search_strategy",
    "librarian_io.ingest_document",
    "librarian_io.judge_abstract_convergence",
    "librarian_io.adjudicate_abstract",
    "analyst_lead.judge_convergence",
    "analyst_lead.adjudicate",
    "analyst_worker.analyze",
    "analyst_worker.request_tools",
]


def _load_models_yaml(path: Path = _MODELS_CONFIG) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _build_pricing(roles: dict) -> dict[str, dict[str, float]]:
    """Return {model_id: {"in": float, "out": float}} from models.yaml roles."""
    pricing: dict[str, dict[str, float]] = {}
    for group in roles.values():
        for member in group.get("members", []):
            model = member.get("model", "")
            price = member.get("priceMTk", {})
            if price and model and model not in pricing:
                pricing[model] = {
                    "in": float(price.get("input", 0.0)),
                    "out": float(price.get("output", 0.0)),
                }
    return pricing


def _build_role_maps(
    roles: dict,
) -> tuple[dict[str, tuple[str, str]], dict[int, str]]:
    """Return (role_model_fixed, analyst_worker_models) from roles config.

    role_model_fixed: {role -> (team, model_id)} for single-model roles.
    analyst_worker_models: {worker_index -> model_id}.
    """
    role_model_fixed: dict[str, tuple[str, str]] = {}
    for role in ("reviewer", "analyst_lead", "librarian_io"):
        members = roles.get(role, {}).get("members", [])
        if members:
            role_model_fixed[role] = (_TEAM_FOR_ROLE[role], members[0]["model"])

    analyst_worker_models: dict[int, str] = {}
    for i, member in enumerate(roles.get("analyst_worker", {}).get("members", [])):
        analyst_worker_models[i] = member["model"]

    return role_model_fixed, analyst_worker_models


def _build_model_colors(
    role_model_fixed: dict[str, tuple[str, str]],
    analyst_worker_models: dict[int, str],
) -> dict[tuple[str, str], str]:
    """Map (team, model_id) → hex color from per-team palettes."""
    colors: dict[tuple[str, str], str] = {}

    for role, (team, model) in role_model_fixed.items():
        palette = _TEAM_PALETTES.get(team, ["#AAAAAA"])
        # analyst_lead occupies palette slot 0; other fixed roles use slot 0 of their team
        colors[(team, model)] = palette[0]

    for idx, model in analyst_worker_models.items():
        palette = _TEAM_PALETTES.get("analyst", ["#5B8DB8"])
        # slot 0 is analyst lead; workers start at slot 1
        colors[("analyst", model)] = palette[min(idx + 1, len(palette) - 1)]

    return colors


def main() -> None:  # noqa: C901
    if len(sys.argv) < 2:
        sys.exit(
            "Usage: python -m auto_research.observability.plot_run <monitor.jsonl> [output.png]"
        )

    log = Path(sys.argv[1])
    if not log.exists():
        sys.exit(f"File not found: {log}")
    out = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(log.stem + ".viz.png")

    # Config-derived model/pricing tables
    roles = _load_models_yaml().get("roles", {})
    pricing = _build_pricing(roles)
    role_model_fixed, analyst_worker_models = _build_role_maps(roles)
    model_colors = _build_model_colors(role_model_fixed, analyst_worker_models)
    analyst_lead_model = role_model_fixed.get("analyst_lead", (None, None))[1]

    # Load events
    events = []
    with open(log) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    if not events:
        sys.exit("Log file is empty.")

    events.sort(key=lambda e: e["sequence"])
    t0 = datetime.fromisoformat(events[0]["created_at"].replace("Z", "+00:00"))

    def to_min(ts: str) -> float:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (t - t0).total_seconds() / 60

    # Build analyst worker attribution: worker-1/worker-2 actor events mark
    # when worker_0/worker_1 finished a phase. Each analyst_worker output event
    # belongs to the worker whose marker has the lowest sequence > output_seq.
    _MARKER_ACTORS = {"worker-1": 0, "worker-2": 1}
    worker_markers: list[tuple[int, int]] = sorted(
        (e["sequence"], _MARKER_ACTORS[e.get("actor", "")])
        for e in events
        if e.get("actor", "") in _MARKER_ACTORS
    )

    def analyst_worker_index(output_seq: int) -> int:
        for marker_seq, worker_idx in worker_markers:
            if marker_seq > output_seq:
                return worker_idx
        return 0

    # Match .input → .output pairs (level-10 events; require usage field)
    calls = []
    used_seqs: set[int] = set()
    for e in events:
        evt = e.get("event", "")
        if not evt.endswith(".input"):
            continue
        base = evt[:-6]
        out_type = base + ".output"
        role_prefix = base.split(".")[0]

        for o in events:
            if (
                o.get("event") == out_type
                and o["sequence"] > e["sequence"]
                and o["sequence"] not in used_seqs
                and "usage" in o.get("data", {})
            ):
                used_seqs.add(o["sequence"])
                usage = o["data"]["usage"]
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)

                if role_prefix == "analyst_worker":
                    widx = analyst_worker_index(o["sequence"])
                    team = "analyst"
                    model = analyst_worker_models.get(widx)
                elif role_prefix in role_model_fixed:
                    team, model = role_model_fixed[role_prefix]
                else:
                    team, model = "run", None

                cost = 0.0
                if model and model in pricing:
                    p = pricing[model]
                    cost = (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000

                calls.append(
                    {
                        "base": base,
                        "team": team,
                        "model": model,
                        "start": to_min(e["created_at"]),
                        "end": to_min(o["created_at"]),
                        "cost": cost,
                        "in_tok": in_tok,
                        "out_tok": out_tok,
                    }
                )
                break

    print(f"Matched {len(calls)} LLM call pairs.")

    # Activity ordering
    present = {c["base"] for c in calls}
    order = [a for a in _ACTIVITY_ORDER if a in present]
    for c in calls:
        if c["base"] not in order:
            order.append(c["base"])
    yticks = {act: i for i, act in enumerate(order)}

    # ── Figure ────────────────────────────────────────────────────────────────
    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(19, 7), gridspec_kw={"width_ratios": [2.2, 1]}
    )
    fig.patch.set_facecolor("#F8F8F8")

    # Gantt timeline
    ax1.set_facecolor("#FAFAFA")
    BAR_H = 0.65
    for c in calls:
        y = yticks.get(c["base"], -1)
        if y < 0:
            continue
        dur = max(c["end"] - c["start"], 0.015)
        ax1.broken_barh(
            [(c["start"], dur)],
            (y - BAR_H / 2, BAR_H),
            facecolors=_TEAM_COLORS[c["team"]],
            edgecolors="white",
            linewidth=0.6,
            alpha=0.90,
        )
    for i in range(len(order)):
        ax1.axhline(i + 0.5, color="#E0E0E0", linewidth=0.5, zorder=0)
    ax1.set_yticks(list(yticks.values()))
    ax1.set_yticklabels([_ACTIVITY_LABEL.get(a, a) for a in order], fontsize=8.5)
    ax1.set_xlabel("Time from run start (minutes)", fontsize=10)
    ax1.set_title(f"LLM call timeline — {log.stem}", fontsize=11, fontweight="bold", pad=10)
    ax1.set_ylim(-0.8, len(order) - 0.2)
    ax1.spines[["top", "right"]].set_visible(False)
    for c in calls:
        y = yticks.get(c["base"], -1)
        if y < 0:
            continue
        dur = c["end"] - c["start"]
        if dur > 0.12:
            mid = c["start"] + dur / 2
            ax1.text(
                mid, y, f"{dur:.1f}m", ha="center", va="center",
                fontsize=5.5, color="white", fontweight="bold",
            )
    ax1.legend(
        handles=[
            mpatches.Patch(color=_TEAM_COLORS["reviewer"],  label="Reviewer"),
            mpatches.Patch(color=_TEAM_COLORS["librarian"], label="Librarian IO"),
            mpatches.Patch(color=_TEAM_COLORS["analyst"],   label="Analyst"),
        ],
        loc="upper right", fontsize=9, framealpha=0.85, edgecolor="#CCCCCC",
    )

    # Cost pie
    ax2.set_facecolor("#FAFAFA")
    cost_by: dict[tuple, float] = defaultdict(float)
    for c in calls:
        if c["model"]:
            cost_by[(c["team"], c["model"])] += c["cost"]
    total_cost = sum(cost_by.values())

    # Slice order: reviewer → analyst lead → analyst workers → librarian
    _slice_candidates = (
        [(k, cost_by[k]) for k in cost_by if k[0] == "reviewer"]
        + [
            (("analyst", analyst_lead_model), cost_by.get(("analyst", analyst_lead_model), 0.0))
        ]
        + [
            (("analyst", m), cost_by.get(("analyst", m), 0.0))
            for m in analyst_worker_models.values()
        ]
        + [(k, cost_by[k]) for k in cost_by if k[0] == "librarian"]
    )
    seen: set = set()
    slices = []
    for k, v in _slice_candidates:
        if k not in seen and cost_by.get(k, 0.0) > 0:
            seen.add(k)
            slices.append((k, cost_by[k]))

    def _pie_label(k: tuple[str, str]) -> str:
        team, model = k
        if team == "reviewer":
            return f"{model}\n(reviewer)"
        if team == "analyst" and model == analyst_lead_model:
            return f"{model}\n(analyst lead)"
        for idx, m in analyst_worker_models.items():
            if m == model and team == "analyst":
                return f"{model}\n(analyst worker {idx})"
        return f"{model}\n({team})"

    if slices:
        keys = [k for k, _ in slices]
        sizes = [v for _, v in slices]
        colors = [model_colors.get(k, "#BBBBBB") for k in keys]

        def _autopct(pct: float) -> str:
            return f"${pct / 100 * total_cost:.4f}\n({pct:.1f}%)"

        wedges, texts, autotexts = ax2.pie(
            sizes,
            labels=[_pie_label(k) for k in keys],
            colors=colors,
            autopct=_autopct,
            startangle=90,
            counterclock=False,
            pctdistance=0.68,
            labeldistance=1.28,
            wedgeprops={"linewidth": 1.5, "edgecolor": "white"},
        )
        for at in autotexts:
            at.set_fontsize(8)
            at.set_color("white")
            at.set_fontweight("bold")
        for t in texts:
            t.set_fontsize(8.5)

    ax2.set_title(
        f"Dollar cost by team & model\nTotal: ${total_cost:.4f}",
        fontsize=11, fontweight="bold", pad=16,
    )
    ax2.legend(
        handles=[
            mpatches.Patch(color=_TEAM_COLORS["reviewer"],  label="Reviewer"),
            mpatches.Patch(color=_TEAM_COLORS["analyst"],   label="Analyst"),
            mpatches.Patch(color=_TEAM_COLORS["librarian"], label="Librarian IO"),
        ],
        loc="lower center", bbox_to_anchor=(0.5, -0.12), ncol=3,
        fontsize=9, framealpha=0.85, edgecolor="#CCCCCC",
    )

    plt.tight_layout(pad=2.5)
    plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved → {out}")

    print("\nCost breakdown:")
    for (team, model), cost in sorted(cost_by.items()):
        pct = 100 * cost / total_cost if total_cost else 0
        print(f"  {team:12s} / {model:22s}: ${cost:.4f}  ({pct:.1f}%)")
    print(f"  {'TOTAL':12s}                           : ${total_cost:.4f}")


if __name__ == "__main__":
    main()
