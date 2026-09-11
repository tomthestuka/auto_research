"""Turn scored trials into the campaign's tables and figures. One generator, so they cannot drift.

Three artifacts, each with a job the others cannot do:

**The tree table** is the body result: one row per seat assignment, with the Tier-0 counts and the
Tier-1 recovery beside each other. It is deliberately not a ranking. The demonstration claim is
that a tree of weaker models still produces a verifiable result or declines cleanly, so the table
reports what each tree did and the reader compares; with three trials per tree nothing here
supports an ordering, and the ``n`` column says so on every row.

**The appendix table** lays out every quantity of every run against the frozen key, which is what
makes the body row auditable: a reader who doubts a recovery count can see the fourteen numbers it
came from, and the four approximate key values are marked so their contribution is visible.

**The forest figure** is the same instrument the campaign already published for run
``bf1e39e0`` — signed z per quantity, one panel per run, identical axes — because the trap is a
*pattern* across quantities and a scalar cannot show it. Every polarization degree low while every
angle sits at zero is a shape you see and do not read.

Cost is at API list prices, and unlike the coder sweep every seat here bills to a key, so the
figures are money somebody paid.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from benchmarks.science_bench.score import TrialScore, load_answer_key


def _fmt(value: Any, digits: int = 2, dash: str = "---") -> str:
    if value is None:
        return dash
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _median(values: list[float]) -> float | None:
    return round(median(values), 2) if values else None


def by_tree(scores: list[TrialScore]) -> dict[str, list[TrialScore]]:
    """Scores grouped by seat assignment, insertion-ordered so the anchor stays first."""
    grouped: dict[str, list[TrialScore]] = defaultdict(list)
    for score in scores:
        grouped[score.tree or "?"].append(score)
    return dict(grouped)


def tree_summary(scores: list[TrialScore]) -> dict[str, Any]:
    """One tree's row: what its trials did, with medians over the trials that measured anything."""
    measured = [s for s in scores if s.quantities_reported]
    return {
        "n": len(scores),
        "accepted": sum(1 for s in scores if s.terminal == "accepted"),
        "accepted_with_limits": sum(1 for s in scores if s.terminal == "accepted_with_limits"),
        "declined": sum(1 for s in scores if s.terminal == "declined"),
        "not_measured": sum(1 for s in scores if not s.correct_outcome),
        "committed": _median([float(s.committed) for s in scores]),
        "compliant": sum(1 for s in scores if s.compliant),
        "reported": _median([float(s.quantities_reported) for s in measured]),
        "within": _median([float(s.within_tolerance) for s in measured]),
        "within_exact": _median([float(s.within_tolerance_exact) for s in measured]),
        "useful": sum(1 for s in scores if s.useful),
        "mean_abs_z": _median([s.mean_abs_z for s in measured if s.mean_abs_z is not None]),
        "pd_signed_z": _median(
            [s.pd_mean_signed_z for s in measured if s.pd_mean_signed_z is not None]
        ),
        "trap": sum(1 for s in measured if s.trap_reproduced),
        "unit_mismatches": sum(s.unit_mismatches for s in scores),
        "wanted_a_human": sum(s.wanted_a_human for s in scores),
        "terminals_relabelled": sum(s.terminals_relabelled for s in scores),
    }


def tree_table_markdown(scores: list[TrialScore]) -> str:
    """The body table, as Markdown for the batch report."""
    rows = [
        "| Tree | n | Accepted | With limits | Declined | Not measured | Tools | k/14 | k/10 exact "
        "| Useful | mean \\|z\\| | PD signed z | Trap | Wanted human |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for tree, group in by_tree(scores).items():
        s = tree_summary(group)
        rows.append(
            f"| {tree} | {s['n']} | {s['accepted']} | {s['accepted_with_limits']} | "
            f"{s['declined']} | {s['not_measured']} | {_fmt(s['committed'], 1)} | "
            f"{_fmt(s['within'], 1)} | {_fmt(s['within_exact'], 1)} | {s['useful']}/{s['n']} | "
            f"{_fmt(s['mean_abs_z'])} | {_fmt(s['pd_signed_z'])} | {s['trap']}/{s['n']} | "
            f"{s['wanted_a_human']} |"
        )
    return "\n".join(rows)


def appendix_table_markdown(scores: list[TrialScore], key: dict[str, Any] | None = None) -> str:
    """Every quantity of every run against the frozen key — the auditable full result."""
    answer_key = key or load_answer_key()
    labels = list(answer_key["quantities"])
    measured = [s for s in scores if s.quantities]
    header = "| Quantity | Unit | Key | " + " | ".join(
        f"{s.tree or '?'} #{i + 1}" for i, s in enumerate(measured)
    ) + " |"
    rule = "|---" * (3 + len(measured)) + "|"
    rows = [header, rule]
    for label in labels:
        expected = answer_key["quantities"][label]
        mark = "$^{a}$" if expected.get("approximate") else ""
        cells = []
        for score in measured:
            q = score.quantities.get(label)
            if q is None or not q.reported:
                cells.append("not reported")
            else:
                flag = "!" if q.unit_mismatch else ""
                cells.append(f"{_fmt(q.value, 3)} ± {_fmt(q.error, 3)} (z={_fmt(q.z)}){flag}")
        rows.append(
            f"| {label}{mark} | {expected['unit']} | "
            f"{_fmt(expected['paper_value'], 3)} ± {_fmt(expected['paper_error'], 3)} | "
            + " | ".join(cells)
            + " |"
        )
    rows.append("")
    notes = []
    if any(q.get("approximate") for q in answer_key["quantities"].values()):
        # Emitted only when the key marks something, so the note can never describe a mark the
        # table does not carry. Every value of the 2026-09-10 key is a verbatim table read.
        notes.append(
            "$^{a}$ Key value is approximate: read off a figure rather than quoted from the "
            "reference's tables."
        )
    notes.append("`!` marks a unit mismatch, which is scored apart from a wrong value.")
    rows.append(" ".join(notes))
    return "\n".join(rows)


def forest_figure(scores: list[TrialScore], path: Path) -> Path | None:
    """Signed z per quantity, one panel per run on identical axes. The trap is a shape.

    Returns ``None`` when no trial measured anything, rather than writing an empty figure that
    would later be mistaken for a result.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    measured = [s for s in scores if s.quantities_reported]
    if not measured:
        return None

    key = load_answer_key()
    labels = list(key["quantities"])
    columns = len(measured)
    fig, axes = plt.subplots(
        1, columns, figsize=(3.1 * columns + 2.2, 5.0), sharey=True, squeeze=False
    )
    positions = range(len(labels))

    for axis, score in zip(axes[0], measured, strict=True):
        values, marks = [], []
        for label in labels:
            q = score.quantities.get(label)
            values.append(q.z if (q and q.z is not None) else None)
            marks.append(bool(q and q.kind == "PA"))
        axis.axvline(0.0, color="0.3", lw=0.9)
        for threshold in (-3.0, 3.0):
            axis.axvline(threshold, color="0.75", lw=0.8, ls="--")
        for y, (z, is_pa) in enumerate(zip(values, marks, strict=True)):
            if z is None:
                axis.plot(0.0, y, marker="x", color="0.6", ms=6)
                continue
            axis.plot(
                z,
                y,
                marker="s" if is_pa else "o",
                color="0.15" if abs(z) <= 3.0 else "0.55",
                ms=5,
            )
        axis.set_yticks(list(positions))
        # Top-down in the key's own order, so the panel and the appendix table read the same way.
        axis.set_ylim(len(labels) - 0.5, -0.5)
        axis.set_xlim(-5.2, 5.2)
        axis.set_title(f"{score.tree or '?'}\n{score.run_id[:8]}", fontsize=8)
        axis.set_xlabel("signed $z$")
        axis.grid(axis="y", color="0.92", lw=0.6)
        axis.set_axisbelow(True)

    axes[0][0].set_yticklabels(labels, fontsize=7)
    fig.suptitle(
        "Recovery of the fourteen prescribed quantities against Krawczynski et al. 2022\n"
        "circle = polarization degree, square = angle, x = not reported; dashed = the "
        "3$\\sigma$ flag",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def summary_scatter(scores: list[TrialScore], path: Path) -> Path | None:
    """One point per trial: mean PD signed z against recovery count, coloured by tree.

    Reads the two axes the demonstration turns on at once — did the tree recover the numbers, and
    did it reproduce the coherent offset rather than reporting agreement.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    measured = [s for s in scores if s.quantities_reported and s.pd_mean_signed_z is not None]
    if not measured:
        return None

    fig, axis = plt.subplots(figsize=(6.0, 4.2))
    trees = list(dict.fromkeys(s.tree or "?" for s in measured))
    markers = ["o", "s", "^", "D", "v"]
    for index, tree in enumerate(trees):
        group = [s for s in measured if (s.tree or "?") == tree]
        axis.scatter(
            [s.within_tolerance for s in group],
            [s.pd_mean_signed_z for s in group],
            marker=markers[index % len(markers)],
            s=54,
            facecolors="none",
            edgecolors=f"C{index}",
            label=f"{tree} (n={len(group)})",
        )
    axis.axhline(0.0, color="0.6", lw=0.9, ls="--")
    axis.axvline(
        float(load_answer_key().get("useful_threshold_k", 10)),
        color="0.6",
        lw=0.9,
        ls=":",
    )
    axis.set_xlabel("quantities recovered within $3\\sigma$ (of 14)")
    axis.set_ylabel("mean signed $z$ over PD quantities")
    axis.set_title("Recovery against the coherent PD offset, per trial", fontsize=10)
    axis.legend(fontsize=8, loc="best", frameon=False)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def build_report(scores: list[TrialScore], stamps: dict[str, Any] | None = None) -> str:
    """The batch's Markdown report: provenance, the tree table, then the appendix table."""
    key = load_answer_key()
    lines = [
        "# science_bench — seat demonstration",
        "",
        "Three trials per seat assignment, one fixed question with a published answer. This is a "
        "demonstration, not an estimation: with n=3 per tree nothing here supports a ranking of "
        "models, and no seat effect is estimated. The variance floor for a stable per-cell "
        "estimate on agentic tasks is 8-16 trials (arXiv 2512.06710).",
        "",
        f"- answer key: `{key['source_artifact']}` sha256 `{key.get('answer_key_sha256', '')[:16]}`",
        f"- reference: {key['reference']}",
        f"- useful threshold: k >= {key.get('useful_threshold_k')} of 14 within "
        f"{key.get('sigma_flag')} sigma",
    ]
    for name, value in sorted((stamps or {}).items()):
        lines.append(f"- {name}: `{value}`")
    if scores:
        lines.append(f"- scorer sha256: `{scores[0].scorer_sha256[:16]}`")
    lines += [
        "",
        "## Trees",
        "",
        tree_table_markdown(scores),
        "",
        "`Not measured` counts trials lost to a harness or provider fault, which are not the "
        "tree's outcome. `With limits` is the [A20] reclassification: an authoring cycle that "
        "committed and then filed a terminal report. `Trap` counts trials reproducing the "
        "coherent pattern (every PD low, every PA at zero); whether the report *states* it as "
        "systematic is a hand-read column, deferred with Tier 2.",
        "",
        "## Every quantity, every run",
        "",
        appendix_table_markdown(scores, key),
        "",
    ]
    return "\n".join(lines)


def write_report(out_dir: Path, scores: list[TrialScore], stamps: dict[str, Any] | None = None) -> Path:
    """Write the report and both figures under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"
    path.write_text(build_report(scores, stamps), encoding="utf-8")
    forest_figure(scores, out_dir / "fig_seat_forest.png")
    summary_scatter(scores, out_dir / "fig_seat_recovery.png")
    (out_dir / "scores.json").write_text(
        json.dumps([s.to_dict() for s in scores], indent=1), encoding="utf-8"
    )
    return path


__all__ = [
    "appendix_table_markdown",
    "build_report",
    "by_tree",
    "forest_figure",
    "summary_scatter",
    "tree_summary",
    "tree_table_markdown",
    "write_report",
]
