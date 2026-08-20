"""The model-swap report: how the review layer behaves when the reviewer model changes.

Two things are rendered from the same records. A summary table, whose axes are chosen for the
case where catch saturates: if every model rejects every defect, the informative differences are
false rejections on the matched controls, whether the stated reason is the planted defect, and
price, which spans an order of magnitude across the roster. And a verbatim audit trail, because a
claim that a model "caught" a defect is only checkable by reading what it actually said.

Pair discrimination is the headline the matched controls buy: a model demonstrates it identified
the defect only when it rejects the mutant **and** accepts the same edit made honestly. Rejecting
both is suspicion, not discrimination, and a bare catch rate cannot tell the two apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.gate_bench.mutants import FAM_CONTROL, FAMILY_ORDER


def load_run(run_dir: Path) -> tuple[str, list[dict]]:
    """`(reviewer name, records)` for one finished run directory."""
    records = [json.loads(line) for line in
               (Path(run_dir) / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    # ``pairs_with`` is corpus metadata rather than a measurement, so it is safe to fill in for
    # runs recorded before the harness carried it.
    from benchmarks.gate_bench.mutants import by_id

    catalogue = by_id()
    for record in records:
        if record.get("pairs_with") is None:
            entry = catalogue.get(record["id"])
            if entry is not None:
                record["pairs_with"] = entry.pairs_with

    # Re-score against the roster as it stands now. A record carries whatever attribution was
    # known when it was written, which for a fresh run is always "undecided" — the reading is a
    # human pass that happens afterwards, so the report must apply the current roster rather than
    # the snapshot in the file.
    from benchmarks.gate_bench.score import ROSTER_NAME, load_roster, score_record

    roster = load_roster(Path(run_dir).parent / ROSTER_NAME)
    records = [score_record(r, roster) for r in records]
    reviewer = next(
        ((r.get("review") or {}).get("reviewer") for r in records
         if (r.get("review") or {}).get("reviewer")),
        "unknown",
    )
    return reviewer, records


def _rejected(record: dict) -> bool:
    return (record.get("review") or {}).get("approved") is False


def _reviewed(record: dict) -> bool:
    return bool((record.get("review") or {}).get("ran"))


def summarise(reviewer: str, records: list[dict]) -> dict:
    """One row of the comparison: what this model did to defects, controls and pairs."""
    by_id = {r["id"]: r for r in records}
    defects = [r for r in records if r["family"] != FAM_CONTROL]
    controls = [r for r in records if r["family"] == FAM_CONTROL]

    reviewed_defects = [r for r in defects if _reviewed(r)]
    reviewed_controls = [r for r in controls if _reviewed(r)]

    # A pair is discriminated when the defect is rejected and its honest twin is accepted.
    pairs = [(by_id.get(c["pairs_with"]), c) for c in controls if c.get("pairs_with")]
    judged = [(m, c) for m, c in pairs if m is not None and _reviewed(m) and _reviewed(c)]
    discriminated = [(m, c) for m, c in judged if _rejected(m) and not _rejected(c)]

    attributions = [r.get("review_attribution") for r in defects if _rejected(r)]
    landed = [r for r in defects
              if not [layer for layer in r.get("caught_by", []) if layer != "commit_sweep"]]

    return {
        "reviewer": reviewer,
        "defects_reviewed": len(reviewed_defects),
        "defects_rejected": sum(1 for r in reviewed_defects if _rejected(r)),
        "controls_reviewed": len(reviewed_controls),
        "controls_rejected": sum(1 for r in reviewed_controls if _rejected(r)),
        "pairs_judged": len(judged),
        "pairs_discriminated": len(discriminated),
        "on_target": sum(1 for a in attributions if a == "on_target"),
        "off_target": sum(1 for a in attributions if a == "off_target"),
        "unread": sum(1 for a in attributions if a in ("undecided", "stale")),
        "reaching_commit": len(landed),
        "cost_usd": sum(r.get("cost_usd", 0.0) for r in records),
        "seconds": sum(r.get("duration_s", 0.0) for r in records),
    }


def _verdict_block(record: dict) -> list[str]:
    """One model's verbatim verdict, or why there is none."""
    review = record.get("review") or {}
    if not review.get("ran"):
        caught = ", ".join(record.get("caught_by", [])) or "none"
        return [f"  *No review: the gate stopped it first ({caught}).*", ""]
    head = "APPROVED" if review.get("approved") else "REJECTED"
    lines = [f"  **{head}** · checks_adequate={review.get('checks_adequate')}"
             f" · attribution={record.get('review_attribution') or '---'}"
             f" · ${record.get('cost_usd', 0):.3f}", ""]
    for issue in review.get("issues", []):
        lines += [f"  - {issue}"]
    if review.get("issues"):
        lines.append("")
    if review.get("rationale"):
        lines += [f"  > {review['rationale']}", ""]
    return lines


def build_swap_report(runs: list[tuple[str, list[dict]]]) -> str:
    """Summary table plus the full per-mutant, per-model audit trail."""
    rows = [summarise(reviewer, records) for reviewer, records in runs]
    order = [reviewer for reviewer, _ in runs]

    lines = ["# Reviewer model swap", ""]
    lines += [
        "The corpus plants its defects by hand, so there is **no author**: nothing is correlated",
        "with anything and this leg measures reviewer-model *sensitivity*, not provider",
        "decorrelation, which needs an author to be decorrelated from.",
        "",
        "Every model saw the identical corpus at the identical pinned references, through the",
        "production review hook, with parameters harmonised across the roster so the comparison is",
        "between models rather than between role configurations.",
        "",
        "## Summary",
        "",
        "| Reviewer | Defects rejected | Controls rejected | Pairs discriminated | On-target | Unread | Reaching a commit | Spend | Minutes |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['reviewer']}` | {row['defects_rejected']}/{row['defects_reviewed']} "
            f"| {row['controls_rejected']}/{row['controls_reviewed']} "
            f"| {row['pairs_discriminated']}/{row['pairs_judged']} "
            f"| {row['on_target']} | {row['unread']} | {row['reaching_commit']} "
            f"| ${row['cost_usd']:.2f} | {row['seconds'] / 60:.0f} |"
        )
    lines += [
        "",
        "`Pairs discriminated` counts defects rejected whose matched control was accepted: the",
        "model separated the defect from the same edit made honestly. Rejecting both is suspicion,",
        "not discrimination. `On-target` counts rejections a human read as naming the planted",
        "defect; `unread` are those not yet judged, and they are not credited anywhere.",
        "",
    ]

    # ── per-family, defects only ────────────────────────────────────────────
    lines += ["## Defects rejected by family", "",
              "| Defect family | n | " + " | ".join(f"`{r}`" for r in order) + " |",
              "|---" * (len(order) + 2) + "|"]
    for family in FAMILY_ORDER:
        if family == FAM_CONTROL:
            continue
        cells, total = [], 0
        for _reviewer, records in runs:
            fam = [r for r in records if r["family"] == family and _reviewed(r)]
            total = max(total, len([r for r in records if r["family"] == family]))
            cells.append(f"{sum(1 for r in fam if _rejected(r))}/{len(fam)}" if fam else "---")
        lines.append(f"| {family} | {total} | " + " | ".join(cells) + " |")
    lines += ["", "Cells are rejections over the changesets that reached the review, which is only",
              "those the deterministic gate passed. `---` means the gate stopped every one of them.",
              ""]

    # ── the audit trail ─────────────────────────────────────────────────────
    lines += [
        "## Audit trail",
        "",
        "Every model's verbatim verdict on every changeset, defect and matched control together,",
        "so a reader can check each judgement rather than take the table on trust.",
        "",
    ]
    ids: list[str] = []
    for _reviewer, records in runs:
        for record in records:
            if record["id"] not in ids:
                ids.append(record["id"])
    by_run = [(reviewer, {r["id"]: r for r in records}) for reviewer, records in runs]

    for mutant_id in ids:
        any_record = next((d[mutant_id] for _r, d in by_run if mutant_id in d), None)
        if any_record is None:
            continue
        pair = any_record.get("pairs_with")
        heading = f"### `{mutant_id}`"
        if pair:
            heading += f"  (control for `{pair}`)"
        lines += [heading, "", f"*{any_record.get('description', '')}*", ""]
        for reviewer, records_by_id in by_run:
            record = records_by_id.get(mutant_id)
            if record is None:
                continue
            lines.append(f"- **`{reviewer}`**")
            lines += _verdict_block(record)
    return "\n".join(lines) + "\n"


def write_swap_report(run_dirs: list[Path], out_path: Path) -> dict[str, Any]:
    """Merge finished runs into one audit document; returns the summary rows."""
    runs = [load_run(Path(d)) for d in run_dirs]
    out_path.write_text(build_swap_report(runs), encoding="utf-8")
    return {"runs": [summarise(rev, rec) for rev, rec in runs], "path": str(out_path)}
