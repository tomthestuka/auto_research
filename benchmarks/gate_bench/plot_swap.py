"""Figure: reviewer-model sensitivity (papers/FANGZ Sect. 5.2, Fig. 4).

Lives beside the benchmark it reads rather than in `tmp_scripts/`: a retrieval or
plotting script for a benchmark is part of that instrument, and a paper figure must not
depend on scratch code (user, 2026-08-27).

The finding is a rank inversion: detection is nearly flat across the five reviewers while false
rejection, self-consistency and cost are not, so a model chosen on detection alone is chosen on the
one axis that does not separate them. Four panels, one measure each, models as rows sorted by false
rejection.

Pair discrimination was measured and dropped (2026-08-18): it counts a defect rejected whose
matched control was accepted, but pairs are only judged when the gate let the defect through, so a
reviewer's false alarms on the other pairs are excluded. gpt-5.6-sol scored 21/21 while rejecting
two clean changesets, which flatters rather than informs.

Form per the dataviz method: magnitude across a small set of named entities is a dot plot; each
panel is a single series, so identity needs no categorical palette and no legend box. Provider is
carried by marker shape, not colour, so it survives print and colour-vision deficiency. Exact
values live in the table; the figure carries shape only, and so labels no individual point.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# Read from the run directories rather than transcribed, so the figure and the table it sits with
# cannot drift apart, and so the three denominators (defects reaching review, controls, clean-base
# repeats) follow the corpus instead of being restated. Usage:
#
#     python -m benchmarks.gate_bench.plot_swap OUT.png RUN_DIR [RUN_DIR ...]
#
# Each RUN_DIR is one finished reviewer leg (its own `records.jsonl`).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.gate_bench.mutants import FAM_CONTROL          # noqa: E402
from benchmarks.gate_bench.harness import REVIEWERS_CONFIG     # noqa: E402
from benchmarks.gate_bench.swap import load_run, summarise     # noqa: E402

def _roster() -> dict:
    """`{roster name: (model id, provider)}` read from the roster the legs actually ran under.

    The model id is read rather than derived from the member name: `gpt_5_6_sol` names the member,
    `gpt-5.6-sol` names the model, and transforming one into the other loses the version dot.
    """
    import yaml

    # The harness's own constant, not a path rebuilt here: this module used to live in
    # `tmp_scripts/` and carried its own `parents[1] / "benchmarks" / "gate_bench" / ...`, which
    # became `benchmarks/benchmarks/gate_bench/` the moment the file moved into the package. One
    # definition, in the module that owns the file.
    data = yaml.safe_load(REVIEWERS_CONFIG.read_text(encoding="utf-8"))
    members = data["roles"]["bench_reviewer"]["members"]
    return {m["name"]: (m["model"], m["provider"]) for m in members}


def collect(run_dirs):
    """`(model, provider, rejected, false_rejections, unstable, USD)` per leg, from the records."""
    roster = _roster()
    rows = []
    for run_dir in run_dirs:
        reviewer, records = load_run(Path(run_dir))
        row = summarise(reviewer, records)
        # "Unstable" counts rejections among the byte-identical clean-base repeats: any rejection
        # there contradicts the reviewer's own acceptance of the same input.
        repeats = [r for r in records
                   if r["family"] == FAM_CONTROL and r.get("notes") == "clean-base repeat"
                   and (r.get("review") or {}).get("ran")]
        unstable = sum(1 for r in repeats
                       if (r.get("review") or {}).get("approved") is False)
        member = next((r["review"]["reviewer"] for r in records
                        if (r.get("review") or {}).get("reviewer")), reviewer)
        model, provider = roster.get(member, (member, "other"))
        rows.append((
            model, provider,
            row["defects_rejected"], row["defects_reviewed"],
            row["controls_rejected"], row["controls_reviewed"],
            unstable, len(repeats),
            row["cost_usd"],
        ))
    rows.sort(key=lambda r: (-r[4], -r[2]))    # fewest false rejections reads at the top
    return rows

INK, MUTED, SERIES = "#0b0b0b", "#52514e", "#2a78d6"
# Marker shape distinguishes the provider, so no reading depends on colour alone. The three
# Responses-dialect vendors joined the roster 2026-08-27.
MARKER = {"anthropic": "s", "openai": "o", "zai": "^", "dashscope": "D", "deepseek": "v"}

plt.rcParams.update({
    "font.family": "serif", "font.size": 8.5,
    "axes.edgecolor": "#c9c8c3", "axes.linewidth": 0.6,
    "xtick.color": MUTED, "ytick.color": INK,
    "xtick.labelsize": 8, "ytick.labelsize": 8.5,
})

# The three count panels share a 0-100 per-cent axis so they can be read against one another:
# their denominators differ (21 defects reach the review, 41 controls do), and raw counts on
# different bases invite a comparison that is not there. Cost keeps its own units.
panels = [
    ("Defects detected",  2, 3, "per cent of the {d} reviewed", False),
    ("False rejections",  4, 5, "per cent of {d} controls",     False),
    ("Unstable verdicts", 6, 7, "per cent of {d} repeats",      False),
    ("Review cost",       8, None, "USD",                      True),
]

def main(argv):
    """Render the figure from finished reviewer legs."""
    global ROWS
    ROWS = collect(argv[2:])
    if not ROWS:
        raise SystemExit(
            "usage: python -m benchmarks.gate_bench.plot_swap OUT.png RUN_DIR [RUN_DIR ...]"
        )
    fig, axes = plt.subplots(1, 4, figsize=(7.1, 2.05), sharey=True)
    names = [r[0] for r in ROWS]
    y = range(len(ROWS))

    for ax, (title, idx, den_idx, sub, logx) in zip(axes, panels):
        denom = None if den_idx is None else ROWS[0][den_idx]
        sub = sub if logx else sub.format(d=denom)
        for i, row in enumerate(ROWS):
            v = row[idx] if logx else 100.0 * row[idx] / row[den_idx]
            ax.plot([0 if not logx else 0.3, v], [i, i], color="#dedcd6", lw=1.6,
                    zorder=1, solid_capstyle="butt")
            ax.plot(v, i, MARKER[row[1]], color=SERIES, ms=6.5, zorder=3,
                    markeredgecolor="white", markeredgewidth=0.8)
        ax.set_title(title, fontsize=8.5, color=INK, pad=7)
        ax.set_xlabel(sub, fontsize=7.5, color=MUTED, labelpad=3)
        # Every point is labelled: the table that carried these values was removed, so this figure is
        # the only place they appear. The rate panels label the COUNT rather than the per cent the
        # axis already shows, so position and label carry different information.
        if logx:
            ax.set_xscale("log"); ax.set_xlim(0.3, 150)
            ax.set_xticks([1, 10]); ax.set_xticklabels(["1", "10"])
            for i, row in enumerate(ROWS):
                ax.annotate(f"{row[8]:.2f}", (row[8], i), textcoords="offset points",
                            xytext=(7, 0), va="center", fontsize=7, color=MUTED)
        else:
            ax.set_xlim(0, 128)
            ax.set_xticks([0, 50, 100]); ax.set_xticklabels(["0", "50", "100%"])
            for i, row in enumerate(ROWS):
                ax.annotate(f"{row[idx]}", (100.0 * row[idx] / row[den_idx], i),
                            textcoords="offset points",
                            xytext=(7, 0), va="center", fontsize=7, color=MUTED)
        ax.grid(axis="x", color="#eeece6", lw=0.6, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)

    axes[0].set_yticks(list(y)); axes[0].set_yticklabels(names)
    axes[0].set_ylim(-0.6, len(ROWS) - 0.4)
    # One entry per provider ACTUALLY plotted, derived from the rows rather than listed: a legend
    # naming two providers while five marker shapes are drawn is worse than none.
    labels = {"openai": "OpenAI", "anthropic": "Anthropic", "zai": "Z.AI",
              "dashscope": "Alibaba", "deepseek": "DeepSeek"}
    plotted = list(dict.fromkeys(row[1] for row in ROWS))
    fig.legend(handles=[Line2D([], [], marker=MARKER[p], color=SERIES, ls="", ms=6.5,
                               label=labels.get(p, p)) for p in plotted],
               loc="lower right", bbox_to_anchor=(0.995, -0.02), frameon=False,
               fontsize=7.5, ncol=len(plotted), handletextpad=0.3, columnspacing=1.0)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    out = argv[1] if len(argv) > 1 else "papers/FANGZ/figures/fig_model_swap.png"
    fig.savefig(out, dpi=300)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv)
