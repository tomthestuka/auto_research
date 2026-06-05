"""
Usage:
    python plot_run.py <path-to-monitor.jsonl> [output.png]

Two-panel visualization for an auto_research run:

Graph 1: Color-coded Gantt timeline — each LLM call as a horizontal bar.
Graph 2: Pie chart of dollar cost — one color per team, sliced by model.

Model assignment (from runtime.py + roles.py):
  reviewer       → handles[0] only = claude-opus-4-7  ($5.00 in / $25.00 out per MTk)
  analyst_lead   → handles[0] only = claude-opus-4-7  ($5.00 in / $25.00 out per MTk)
  analyst_worker → ALL handles (2 workers, each with a different handle):
                     worker_0 = handles[0] = claude-sonnet-4-6  ($3.00 / $15.00 per MTk)
                     worker_1 = handles[1] = gpt-5.5            ($5.00 / $30.00 per MTk)
  librarian_io   → handles[0] only = claude-sonnet-4-6 ($3.00 in / $15.00 out per MTk)
                   (IODebateToConverge is called with [self, self] — same agent twice)

Worker attribution: in DebateToConverge workers run sequentially (for loop). The
monitor emits actor='worker-1' / actor='worker-2' summary events after each worker
finishes its phase. Every analyst_worker LLM call that precedes a 'worker-1' marker
belongs to worker_0 (handles[0]); calls between a 'worker-1' and the following
'worker-2' marker belong to worker_1 (handles[1]).
"""

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

# ── CLI ───────────────────────────────────────────────────────────────────────
if len(sys.argv) < 2:
    sys.exit("Usage: python plot_run.py <monitor.jsonl> [output.png]")

LOG = Path(sys.argv[1])
if not LOG.exists():
    sys.exit(f"File not found: {LOG}")

OUT = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(LOG.stem + ".viz.png")

# ── pricing ($/MTk from config/models.yaml) ───────────────────────────────────
PRICING = {
    "claude-opus-4-7":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "gpt-5.5":           {"in": 5.00, "out": 30.00},
}

# Non-analyst roles: always use handles[0]
ROLE_MODEL_FIXED = {
    "reviewer":    ("reviewer", "claude-opus-4-7"),
    "analyst_lead":("analyst",  "claude-opus-4-7"),
    "librarian_io":("librarian","claude-sonnet-4-6"),
}
# Analyst workers: per-index model assignment
ANALYST_WORKER_MODELS = {
    0: "claude-sonnet-4-6",   # handles[0]
    1: "gpt-5.5",             # handles[1]
}

# ── color palette ─────────────────────────────────────────────────────────────
TEAM_COLORS = {
    "reviewer":  "#E05C5C",
    "analyst":   "#5B8DB8",
    "librarian": "#5BAE85",
    "run":       "#AAAAAA",
}
MODEL_COLORS = {
    ("reviewer",  "claude-opus-4-7"):   "#E05C5C",
    ("analyst",   "claude-opus-4-7"):   "#3A6A98",   # analyst lead  — darker blue
    ("analyst",   "claude-sonnet-4-6"): "#89BCDE",   # analyst worker_0 — lighter blue
    ("analyst",   "gpt-5.5"):           "#2D4B7A",   # analyst worker_1 — darkest blue
    ("librarian", "claude-sonnet-4-6"): "#5BAE85",
}

# ── load events ───────────────────────────────────────────────────────────────
events = []
with open(LOG) as f:
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

# ── build analyst worker attribution map ──────────────────────────────────────
# worker-1/worker-2 actor events mark when worker_0/worker_1 finished a phase.
# Every analyst_worker output event whose sequence < next marker's sequence
# belongs to the worker indicated by that marker.
#
# We build a sorted list of (marker_seq, worker_index) for marker events, then
# for each analyst_worker output event, find the first marker with seq > output_seq.
WORKER_MARKER_ACTORS = {"worker-1": 0, "worker-2": 1}
worker_markers: list[tuple[int, int]] = []
for e in events:
    actor = e.get("actor") or ""
    if actor in WORKER_MARKER_ACTORS:
        worker_markers.append((e["sequence"], WORKER_MARKER_ACTORS[actor]))
worker_markers.sort()

def analyst_worker_index(output_seq: int) -> int:
    """Return the worker index (0 or 1) for an analyst_worker output event."""
    for marker_seq, worker_idx in worker_markers:
        if marker_seq > output_seq:
            return worker_idx
    return 0  # fallback: last phase before end of run

# ── match input → output pairs ────────────────────────────────────────────────
calls = []
used_seqs: set[int] = set()

for e in events:
    evt = e.get("event", "")
    if not evt.endswith(".input"):
        continue
    base = evt[:-6]
    out_type = base + ".output"

    # Determine role prefix
    role_prefix = base.split(".")[0]  # e.g. 'analyst_worker', 'reviewer', etc.

    for o in events:
        if (
            o.get("event") == out_type
            and o["sequence"] > e["sequence"]
            and o["sequence"] not in used_seqs
            and "usage" in o.get("data", {})
        ):
            used_seqs.add(o["sequence"])
            usage = o["data"]["usage"]
            in_tok  = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)

            if role_prefix == "analyst_worker":
                widx  = analyst_worker_index(o["sequence"])
                team  = "analyst"
                model = ANALYST_WORKER_MODELS[widx]
            elif role_prefix in ROLE_MODEL_FIXED:
                team, model = ROLE_MODEL_FIXED[role_prefix]
            else:
                team, model = "run", None

            cost = 0.0
            if model and model in PRICING:
                p = PRICING[model]
                cost = (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000

            calls.append({
                "base":    base,
                "team":    team,
                "model":   model,
                "start":   to_min(e["created_at"]),
                "end":     to_min(o["created_at"]),
                "cost":    cost,
                "in_tok":  in_tok,
                "out_tok": out_tok,
            })
            break

print(f"Matched {len(calls)} LLM call pairs.")

# ── human-readable labels ─────────────────────────────────────────────────────
ACTIVITY_LABEL = {
    "reviewer.review":                       "reviewer · review",
    "reviewer.compose_machine_report":        "reviewer · machine report",
    "reviewer.compose_latex_report":          "reviewer · LaTeX report",
    "analyst_lead.judge_convergence":         "analyst lead · judge conv.",
    "analyst_lead.adjudicate":                "analyst lead · adjudicate",
    "analyst_worker.analyze":                 "analyst worker · analyze",
    "analyst_worker.request_tools":           "analyst worker · tool call",
    "librarian_io.compose_search_strategy":   "librarian IO · search strategy",
    "librarian_io.ingest_document":           "librarian IO · ingest doc",
    "librarian_io.judge_abstract_convergence":"librarian IO · judge conv.",
    "librarian_io.adjudicate_abstract":       "librarian IO · adjudicate",
}

ORDER = [
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
present = {c["base"] for c in calls}
ORDER = [a for a in ORDER if a in present]
for c in calls:
    if c["base"] not in ORDER:
        ORDER.append(c["base"])

yticks = {act: i for i, act in enumerate(ORDER)}

# ─────────────────────────────────────────────────────────────────────────────
# FIGURE
# ─────────────────────────────────────────────────────────────────────────────
fig, (ax1, ax2) = plt.subplots(
    1, 2,
    figsize=(19, 7),
    gridspec_kw={"width_ratios": [2.2, 1]},
)
fig.patch.set_facecolor("#F8F8F8")

# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 1 — GANTT TIMELINE
# ══════════════════════════════════════════════════════════════════════════════
ax1.set_facecolor("#FAFAFA")
BAR_H = 0.65

for c in calls:
    y = yticks.get(c["base"], -1)
    if y < 0:
        continue
    dur = max(c["end"] - c["start"], 0.015)
    ax1.broken_barh(
        [(c["start"], dur)], (y - BAR_H / 2, BAR_H),
        facecolors=TEAM_COLORS[c["team"]],
        edgecolors="white",
        linewidth=0.6,
        alpha=0.90,
    )

for i in range(len(ORDER)):
    ax1.axhline(i + 0.5, color="#E0E0E0", linewidth=0.5, zorder=0)

ax1.set_yticks(list(yticks.values()))
ax1.set_yticklabels(
    [ACTIVITY_LABEL.get(a, a) for a in ORDER],
    fontsize=8.5,
)
ax1.set_xlabel("Time from run start (minutes)", fontsize=10)
ax1.set_title(
    f"LLM call timeline — {LOG.stem}",
    fontsize=11, fontweight="bold", pad=10,
)
ax1.set_ylim(-0.8, len(ORDER) - 0.2)
ax1.spines[["top", "right"]].set_visible(False)

for c in calls:
    y = yticks.get(c["base"], -1)
    if y < 0:
        continue
    dur = c["end"] - c["start"]
    if dur > 0.12:
        mid = c["start"] + dur / 2
        ax1.text(mid, y, f"{dur:.1f}m", ha="center", va="center",
                 fontsize=5.5, color="white", fontweight="bold")

legend_patches = [
    mpatches.Patch(color=TEAM_COLORS["reviewer"],  label="Reviewer"),
    mpatches.Patch(color=TEAM_COLORS["librarian"], label="Librarian IO"),
    mpatches.Patch(color=TEAM_COLORS["analyst"],   label="Analyst"),
]
ax1.legend(handles=legend_patches, loc="upper right", fontsize=9,
           framealpha=0.85, edgecolor="#CCCCCC")

# ══════════════════════════════════════════════════════════════════════════════
# GRAPH 2 — COST PIE CHART
# ══════════════════════════════════════════════════════════════════════════════
ax2.set_facecolor("#FAFAFA")

cost_by: dict[tuple, float] = defaultdict(float)
for c in calls:
    if c["model"]:
        cost_by[(c["team"], c["model"])] += c["cost"]

total_cost = sum(cost_by.values())

SLICE_ORDER = [
    ("reviewer",  "claude-opus-4-7"),
    ("analyst",   "claude-opus-4-7"),
    ("analyst",   "claude-sonnet-4-6"),
    ("analyst",   "gpt-5.5"),
    ("librarian", "claude-sonnet-4-6"),
]
slices = [(k, cost_by[k]) for k in SLICE_ORDER if k in cost_by and cost_by[k] > 0]
keys   = [k for k, _ in slices]
sizes  = [v for _, v in slices]
colors = [MODEL_COLORS.get(k, "#BBBBBB") for k in keys]

LABEL_TEXT = {
    ("reviewer",  "claude-opus-4-7"):   "claude-opus-4-7\n(reviewer)",
    ("analyst",   "claude-opus-4-7"):   "claude-opus-4-7\n(analyst lead)",
    ("analyst",   "claude-sonnet-4-6"): "claude-sonnet-4-6\n(analyst worker 0)",
    ("analyst",   "gpt-5.5"):           "gpt-5.5\n(analyst worker 1)",
    ("librarian", "claude-sonnet-4-6"): "claude-sonnet-4-6\n(librarian IO)",
}
pie_labels = [LABEL_TEXT.get(k, str(k)) for k in keys]

def autopct(pct):
    cost = pct / 100 * total_cost
    return f"${cost:.4f}\n({pct:.1f}%)"

wedges, texts, autotexts = ax2.pie(
    sizes,
    labels=pie_labels,
    colors=colors,
    autopct=autopct,
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

team_legend = [
    mpatches.Patch(color=TEAM_COLORS["reviewer"],  label="Reviewer"),
    mpatches.Patch(color=TEAM_COLORS["analyst"],   label="Analyst"),
    mpatches.Patch(color=TEAM_COLORS["librarian"], label="Librarian IO"),
]
ax2.legend(handles=team_legend, loc="lower center", bbox_to_anchor=(0.5, -0.12),
           ncol=3, fontsize=9, framealpha=0.85, edgecolor="#CCCCCC")

# ─────────────────────────────────────────────────────────────────────────────
plt.tight_layout(pad=2.5)
plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"Saved → {OUT}")

print("\nCost breakdown:")
for (team, model), cost in sorted(cost_by.items()):
    pct = 100 * cost / total_cost if total_cost else 0
    print(f"  {team:12s} / {model:22s}: ${cost:.4f}  ({pct:.1f}%)")
print(f"  {'TOTAL':12s}                           : ${total_cost:.4f}")
