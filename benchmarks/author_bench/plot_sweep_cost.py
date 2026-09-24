"""Cost against elapsed time for every trial of the coder model sweep, on one panel.

One line per trial, coloured by the vendor that authored it. A single panel rather than small
multiples because the question here is *between* vendors, not within a trial: the small-multiples
figure it replaces asked how one model's cost accrues, and that shape hides a comparison.

Colour never carries meaning alone, and it carries exactly one thing: the vendor. Every in-session
event is marked by **shape** and drawn in its own line's colour, so the two encodings never compete
and the figure survives greyscale printing and colour-blind readers. The event vocabulary is the
pressure figure's, so the two read as one family.

    python -m benchmarks.author_bench.plot_sweep_cost [out.png]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib.lines import Line2D

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.author_bench.legs import BRAND_COLOURS          # vendor identity, one source
from benchmarks.author_bench.timeline import load_corpus

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "logs" / "dev" / "author-bench"

INK, MUTED, SURFACE, GRID = "#0b0b0b", "#52514e", "#fcfcfb", "#eeedea"

# Trials the manuscript reports: K = 5 per leg, taken by trial number, never by outcome. The
# Claude arm is the exception at K = 10, its full reported batch (PUBLISHED below).
K = 5
# Colour = vendor. The reviewer is held at `gpt-5.6-sol` in every arm drawn here, so the author is
# the only variable; line style records whether that reviewer happens to share the author's
# provider, which it does for the GPT author alone. The same-provider Claude arm
# (`claude_sonnet_5_correlated`) is deliberately not drawn: its reviewer is `claude-opus-5`, so it
# varies the reviewer too, and the reviewer-coupling question is measured on the lead x reviewer
# grid instead, where 100 trials separate reviewer identity from provider sharing.
LEGS = [
    ("deepseek_v4_flash", "deepseek-v4-flash", "deepseek", "-"),
    ("glm_5_3", "glm-5.3", "zai", "-"),
    ("claude_published", "claude-sonnet-5", "anthropic", "-"),
    ("gpt_5_6_terra_correlated", "gpt-5.6-terra", "openai", "--"),
]

# The Claude arm is the reliability batch reported at K = 10 in the manuscript, so all ten of its
# trials are drawn: same brief, same base ref, same GPT reviewer. Named explicitly because its
# records predate the command counter, so the discovery filter below would drop them.
PUBLISHED = ([("20260818T214525Z", t) for t in (1, 2, 3, 4)]
             + [("20260819T011004Z-rerun", t) for t in (5, 6, 7, 8, 9, 10)])
# Shape carries the event, colour carries the vendor. Same vocabulary as the pressure figure.
EVENTS = [
    ("gate_reject",    "v", "gate rejection"),
    ("review_reject",  "o", "co-lead rejection"),
    ("review_approve", "*", "co-lead approval (commit)"),
    ("ask_lead",       "P", "coder asked the lead"),
]
FAIL_MARK = "X"


def throttle_ratio() -> dict[tuple[str, str, int], float]:
    """Fraction of author turns that died on a vendor rate limit, per (batch, leg, trial).

    A throttled trial measures the vendor's request limit rather than the model, so it is excluded.
    Read from the ledger because only the newest records carry the count as a field.
    """
    out: dict[tuple[str, str, int], list[int]] = {}
    for ledger in BENCH.glob("2026082[01]T*/*/trial-*/monitor.jsonl"):
        key = (ledger.parts[-4], ledger.parts[-3], int(ledger.parts[-2][-2:]))
        seen = out.setdefault(key, [0, 0])
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "coder.turn_end":
                continue
            seen[0] += 1
            if "ate limit" in str((row.get("data") or {}).get("error") or ""):
                seen[1] += 1
    return {k: (v[1] / v[0] if v[0] else 0.0) for k, v in out.items()}


def collect() -> dict[str, list]:
    """The reported trials per leg: newest valid record per trial number, first K by number."""
    ratio = throttle_ratio()
    chosen: dict[str, dict[int, tuple[Path, str]]] = {}
    for batch in sorted(BENCH.glob("2026082[01]T*/")):
        records = batch / "records.jsonl"
        if not records.exists():
            continue
        for line in records.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            leg, trial = r["leg"], r["trial"]
            if r["status"] not in ("committed", "failed"):
                continue
            if r.get("coder_commands_ok", 0) == 0:
                continue
            if ratio.get((batch.name, leg, trial), 0.0) >= 0.2:
                continue
            ledger = batch / leg / f"trial-{trial:02d}" / "monitor.jsonl"
            if ledger.exists():
                chosen.setdefault(leg, {})[trial] = (ledger, r["status"])
    out = {}
    for slug, _, _, _ in LEGS:
        if slug == "claude_published":
            srcs = []
            for batch, trial in PUBLISHED:
                led = BENCH / batch / "claude_sonnet_5_correlated" / f"trial-{trial:02d}" / "monitor.jsonl"
                if not led.exists():
                    led = BENCH / batch / f"trial-{trial:02d}" / "monitor.jsonl"
                status = ""
                rec = BENCH / batch / "records.jsonl"
                for line in rec.read_text(encoding="utf-8").splitlines():
                    r = json.loads(line)
                    if r["trial"] == trial:
                        status = r["status"]
                if led.exists():
                    srcs.append((led, trial, status))
            out[slug] = load_corpus(srcs)
            continue
        picked = sorted(chosen.get(slug, {}))[:K]
        out[slug] = load_corpus([(chosen[slug][t][0], t, chosen[slug][t][1]) for t in picked])
    return out


def _at(curve: list[tuple[float, float]], minute: float) -> float:
    """The cumulative cost at ``minute`` — markers ride the line rather than floating."""
    value = 0.0
    for at, amount in curve:
        if at <= minute:
            value = amount
    return value


def plot(corpus: dict[str, list], out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7.2, 4.6), facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#d8d7d2")
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)

    for slug, label, vendor, dash in LEGS:
        colour = BRAND_COLOURS[vendor]
        lines = corpus.get(slug) or []
        anchor = None   # rightmost endpoint: the one place per vendor with clear air beside it
        for t in lines:
            if not t.cost_curve:
                continue
            xs = [m for m, _ in t.cost_curve]
            ys = [c for _, c in t.cost_curve]
            ax.step(xs, ys, where="post", color=colour, linewidth=1.5, alpha=0.62,
                    linestyle=dash, solid_capstyle="round", zorder=2)
            # Events ride the line at the cost reached when they happened.
            for ev in t.events:
                shape = dict((k, m) for k, m, _ in EVENTS).get(ev.kind)
                if shape is None:
                    continue
                ax.plot(ev.minute, _at(t.cost_curve, ev.minute), marker=shape, color=colour,
                        markersize=8 if shape == "*" else 5.2, markeredgecolor=SURFACE,
                        markeredgewidth=0.8, linestyle="none", clip_on=False, zorder=4)
            if t.outcome != "committed":
                ax.plot(xs[-1], ys[-1], marker=FAIL_MARK, color=colour, markersize=6.5,
                        markeredgecolor=SURFACE, markeredgewidth=0.9, linestyle="none",
                        clip_on=False, zorder=5)
            if anchor is None or xs[-1] > anchor[0]:
                anchor = (xs[-1], ys[-1])
        if anchor and label:
            # Haloed, because twenty overlapping step lines leave no reliably empty background.
            ax.annotate(label, xy=anchor, xytext=(8, 0), textcoords="offset points",
                        fontsize=8.5, color=colour, va="center", clip_on=False,
                        fontweight="medium",
                        path_effects=[pe.withStroke(linewidth=2.6, foreground=SURFACE)])

    ax.set_xlabel("elapsed minutes", fontsize=9.5, color=MUTED)
    ax.set_ylabel("cumulative cost, list prices (log)", fontsize=9.5, color=MUTED)
    ax.tick_params(labelsize=8.5, colors=MUTED, length=3)
    # Derived, not pinned: the arms do not all run to the same wall clock, and a pinned right edge
    # silently drops the longest trial's tail and the arm label anchored to it.
    span = max((t.duration_min for group in corpus.values() for t in group), default=150.0)
    ax.set_xlim(0, span * 1.18)
    # Log, reluctantly: the Claude arm reaches US$114 while DeepSeek finishes under
    # US$11, and a linear axis spends four fifths of the panel on one leg. Values are read from the
    # labelled ticks rather than off the curve, so the axis is annotated at decade and half-decade.
    ax.set_yscale("log")
    ax.set_ylim(0.5, 200)
    ax.set_yticks([1, 3, 10, 30, 100])
    ax.set_yticklabels(["\\$1", "\\$3", "\\$10", "\\$30", "\\$100"])

    handles = [Line2D([], [], marker=m, color=MUTED, linestyle="none",
                      markersize=8 if m == "*" else 5.2, label=lab) for _, m, lab in EVENTS]
    handles.append(Line2D([], [], marker=FAIL_MARK, color=MUTED, linestyle="none",
                          markersize=6.5, label="honest failure"))
    handles.append(Line2D([], [], color=MUTED, linestyle="--", linewidth=1.5,
                          label="reviewer shares the coder's provider"))
    # Lower right: the one region no trial's curve reaches, so the key never sits on the data.
    ax.legend(handles=handles, loc="lower right", frameon=False, fontsize=8,
              labelcolor=MUTED, handletextpad=0.4, ncol=1, borderpad=0.2, labelspacing=0.35)
    fig.tight_layout()
    fig.savefig(out, dpi=220, facecolor=SURFACE, bbox_inches="tight")
    return out


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        REPO / "papers" / "FANGZ" / "figures" / "fig_coder_sweep_cost.png")
    corpus = collect()
    for slug, label, _, _ in LEGS:
        got = corpus.get(slug) or []
        total = sum(t.cost_usd for t in got)
        print(f"  {label:20} n={len(got)}  trials {[t.trial for t in got]}  "
              f"total ${total:.2f}  median ${sorted(t.cost_usd for t in got)[len(got)//2]:.2f}"
              if got else f"  {label:20} n=0")
    plot(corpus, out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
