"""Attribution and reporting: which layer caught each planted defect, and the matrix.

A catch is attributed to the **first defence that would have stopped the commit**, read off
the machine-readable gate report rather than inferred: a failed ``no_fabrication.*`` or
``provenance.*`` check is the mechanical scan, ``structural.*`` / ``determinism`` /
``layer_b.*`` are the deterministic gate, a review verdict of not-approved is the
cross-provider review, and the post-commit sweep is its own column for the one probe family
that needs it.

Tier-3 findings are reported in their own column and are **never** a catch: a finding does
not flip ``GateReport.passed``, so a defect that produces one still reaches the review.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from pathlib import Path

from benchmarks.gate_bench.mutants import FAM_CONTROL, FAMILY_ORDER, LAYERS

Record = dict

_SCAN_PREFIXES = ("no_fabrication.", "provenance.")
_GATE_LAYERS = ("structural", "determinism", "layer_b")

# ── Attribution of co-lead catches ───────────────────────────────────────────
# A co-lead rejection only demonstrates a catch when its stated reason is the planted defect.
# Counting every rejection would credit the layer for unrelated objections, which is how the
# 2026-08-17 mislabelled-fixture confound inflated it. The judgement is a human read, kept in a
# roster file so it survives re-runs, and keyed by the verdict's own fingerprint so a *different*
# verdict for the same mutant is re-read rather than inheriting the old decision.
ROSTER_NAME = "review_roster.json"
ATTRIBUTIONS = ("on_target", "off_target")


def review_fingerprint(record: Record) -> str:
    """Stable hash of the co-lead verdict a roster decision was made against."""
    review = record.get("review") or {}
    payload = json.dumps(
        [review.get("approved"), review.get("checks_adequate"),
         review.get("issues", []), review.get("rationale", "")],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_roster(path: Path | str) -> dict:
    """Read the attribution roster, or an empty one when it does not exist yet."""
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")) or {}


def roster_key(record: Record) -> str:
    """``<reviewer>::<mutant>`` — a reading belongs to one model's verdict, not to the mutant."""
    reviewer = (record.get("review") or {}).get("reviewer") or record.get("reviewer") or "unknown"
    return f"{reviewer}::{record['id']}"


def attribute(record: Record, roster: dict) -> str | None:
    """``on_target`` / ``off_target`` / ``undecided`` / ``stale``, or None when not applicable.

    ``stale`` means a decision exists but the model said something different this time, so the
    earlier read does not cover it. Reported separately rather than silently reused.
    """
    if "review" not in record.get("caught_by", []):
        return None
    entry = roster.get(roster_key(record)) or roster.get(record["id"])
    if not entry:
        return "undecided"
    if entry.get("verdict_fingerprint") != review_fingerprint(record):
        return "stale"
    verdict = entry.get("attribution")
    return verdict if verdict in ATTRIBUTIONS else "undecided"


def failed_checks(record: Record) -> tuple[list[str], list[str]]:
    """``(failed Layer-A check names, failed Layer-B check names)``."""
    gate = record.get("gate") or {}
    return (
        [c["name"] for c in gate.get("layer_a", []) if not c["passed"]],
        [c["name"] for c in gate.get("layer_b", []) if not c["passed"]],
    )


def caught_layers(record: Record) -> list[str]:
    """Every defence layer that rejected this changeset, in stack order."""
    failed_a, failed_b = failed_checks(record)
    out: list[str] = []
    if any(name.startswith(_SCAN_PREFIXES) for name in failed_a):
        out.append("scan")
    structural = [n for n in failed_a if n.startswith("structural.")]
    if structural:
        out.append("structural")
    # The determinism check reports a failure when it was *skipped* because structure failed;
    # attributing that to determinism would credit a layer that never ran.
    if "determinism" in failed_a and not structural:
        out.append("determinism")
    if failed_b:
        out.append("layer_b")
    review = record.get("review") or {}
    if review.get("ran") and review.get("approved") is False:
        out.append("review")
    sweep = record.get("commit_sweep") or {}
    if sweep.get("caught"):
        out.append("commit_sweep")
    return out


def score_record(record: Record, roster: dict | None = None) -> Record:
    """Annotate one record with ``caught_by``, ``outcome``, ``expectation``, attribution.

    ``review_attribution`` is additive: it never changes ``caught_by`` or ``outcome``, so the
    layer attribution stays exactly what the defences reported and the human judgement sits
    beside it rather than inside it.
    """
    if record.get("error"):
        record["caught_by"] = []
        record["outcome"] = "error"
        record["expectation"] = "error"
        record["review_attribution"] = None
        return record

    caught = caught_layers(record)
    is_control = record["family"] == FAM_CONTROL
    expected = record["expected"]
    reviewed = bool((record.get("review") or {}).get("ran"))

    record["caught_by"] = caught
    record["review_attribution"] = attribute(record, roster or {})
    if is_control:
        approved = (record.get("review") or {}).get("approved")
        clean = not caught and (approved is not False)
        record["outcome"] = "clean_pass" if clean else "false_reject"
        record["expectation"] = "met" if clean else "unmet"
        return record

    record["outcome"] = "caught" if caught else "reached_commit"
    if expected == "miss":
        record["expectation"] = "met" if not caught else "unmet"
    elif expected == "review" and not reviewed and not caught:
        record["expectation"] = "not_evaluated"
    else:
        record["expectation"] = "met" if expected in caught else "unmet"
    return record


def score_all(records: Iterable[Record], roster: dict | None = None) -> list[Record]:
    return [score_record(dict(r), roster) for r in records]


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------


def _count(records: Sequence[Record], predicate) -> int:
    return sum(1 for r in records if predicate(r))


def _n_findings(record: Record) -> int:
    return len((record.get("gate") or {}).get("findings", []))


def build_matrix(records: Sequence[Record], stamps: dict[str, str] | None = None) -> str:
    """Render the catch-rate matrix and its supporting tables as Markdown."""
    records = [r for r in records]
    stamps = stamps or {}
    lines: list[str] = ["# Gate catch-rate matrix", ""]

    total_cost = sum(r.get("cost_usd", 0.0) for r in records)
    total_time = sum(r.get("duration_s", 0.0) for r in records)
    reviewed = _count(records, lambda r: (r.get("review") or {}).get("ran"))
    lines += [
        f"- mutants: **{len(records)}** "
        f"({_count(records, lambda r: r['base'] == 'synthetic')} synthetic, "
        f"{_count(records, lambda r: r['base'] == 'real')} real)",
        f"- cross-provider review: **{'run on ' + str(reviewed) + ' gate-passing changesets' if reviewed else 'not run'}**",
        f"- wall clock: {total_time / 60:.1f} min · model spend: ${total_cost:.2f}",
        "",
    ]
    if stamps:
        lines += ["Measured against:", ""]
        lines += [f"- `{k}`: `{v}`" for k, v in stamps.items()]
        lines.append("")

    # ── the paper's table shape ────────────────────────────────────────────
    lines += [
        "## Catch rate by defect family and defence layer",
        "",
        "| Defect family | Mutants | Mechanical scan | Deterministic gate | "
        "Cross-provider review | Reaching a commit | Findings |",
        "|---|---|---|---|---|---|---|",
    ]
    for family in FAMILY_ORDER:
        rows = [r for r in records if r["family"] == family]
        if not rows:
            continue
        scan = _count(rows, lambda r: "scan" in r["caught_by"])
        det = _count(rows, lambda r: any(l in r["caught_by"] for l in _GATE_LAYERS))
        fam_reviewed = [r for r in rows if (r.get("review") or {}).get("ran")]
        rev = _count(fam_reviewed, lambda r: "review" in r["caught_by"])
        reaching = _count(
            rows, lambda r: not [l for l in r["caught_by"] if l != "commit_sweep"]
        )
        findings = sum(_n_findings(r) for r in rows)
        rev_cell = f"{rev}/{len(fam_reviewed)}" if fam_reviewed else "---"
        lines.append(
            f"| {family} | {len(rows)} | {scan} | {det} | {rev_cell} | {reaching} | {findings} |"
        )
    lines += [
        "",
        "`Reaching a commit` counts changesets no layer rejected; the post-commit sweep "
        "(below) records what a commit would then have swept in unscanned. A finding never "
        "counts as a catch: it does not fail the gate.",
        "",
    ]

    # ── per-layer detail ───────────────────────────────────────────────────
    lines += ["## Per-layer attribution", "", "| Layer | Defects caught |", "|---|---|"]
    defects = [r for r in records if r["family"] != FAM_CONTROL]
    for layer in LAYERS:
        lines.append(f"| {layer} | {_count(defects, lambda r: layer in r['caught_by'])} |")
    lines += [
        f"| (none) | {_count(defects, lambda r: not r['caught_by'])} |",
        "",
    ]

    # ── controls and expectations ──────────────────────────────────────────
    controls = [r for r in records if r["family"] == FAM_CONTROL]
    false_rejects = [r for r in controls if r["outcome"] == "false_reject"]
    lines += [
        "## Controls (false-rejection rate)",
        "",
        f"{len(controls) - len(false_rejects)}/{len(controls)} clean changesets passed every "
        "layer they reached.",
        "",
    ]
    for record in false_rejects:
        lines.append(f"- **{record['id']}** rejected by {record['caught_by']}")
    if false_rejects:
        lines.append("")

    # ── attribution of co-lead catches ─────────────────────────────────────
    review_catches = [r for r in records if "review" in r.get("caught_by", [])]
    if review_catches:
        buckets = {k: [r for r in review_catches if r.get("review_attribution") == k]
                   for k in ("on_target", "off_target", "undecided", "stale")}
        lines += [
            "## Attribution of co-lead catches",
            "",
            f"{len(buckets['on_target'])}/{len(review_catches)} co-lead rejections state the "
            "planted defect as their reason (hand-read; a rejection on unrelated grounds is not "
            "a catch of the plant).",
            "",
            "| Judgement | n | Mutants |",
            "|---|---|---|",
        ]
        for key, rows in buckets.items():
            if rows:
                lines.append(f"| {key} | {len(rows)} | {', '.join(r['id'] for r in rows)} |")
        lines.append("")

    # ── the Table-5 number: what a commit would carry under this configuration ──
    defects = [r for r in records if r["family"] != FAM_CONTROL]
    committed = [r for r in defects
                 if not [l for l in r.get("caught_by", []) if l != "commit_sweep"]]
    ablation = next((r.get("ablation") for r in records if r.get("ablation")), "none")
    reviewed_any = any((r.get("review") or {}).get("ran") for r in records)
    lines += [
        "## Defects reaching a commit under this configuration",
        "",
        f"**{len(committed)}/{len(defects)}** planted defects reach a commit with "
        f"`ablation={ablation}`, cross-provider review "
        f"{'enabled' if reviewed_any else 'disabled'}.",
        "",
    ]
    if committed:
        lines += ["| Mutant | Family | Hole |", "|---|---|---|"]
        lines += [f"| {r['id']} | {r['family']} | {r.get('known_hole') or '---'} |"
                  for r in committed]
        lines.append("")

    unmet = [r for r in records if r["expectation"] in ("unmet", "error")]
    lines += ["## Expectation mismatches", ""]
    if ablation != "none":
        lines += [
            "The corpus predicts the **full** stack, so a displaced catch here is the "
            "measurement, not a fault: with a defence disabled, a defect it used to stop is "
            "expected to fall through to a later layer or to a commit.",
            "",
        ]
    if not unmet:
        lines += ["Every mutant behaved as the corpus predicted.", ""]
    else:
        lines += ["| Mutant | Expected | Caught by | Note |", "|---|---|---|---|"]
        for record in unmet:
            note = record.get("error") or (record.get("gate") or {}).get("reason", "")
            lines.append(
                f"| {record['id']} | {record['expected']} | "
                f"{record['caught_by'] or '(none)'} | {note[:120].replace('|', '/')} |"
            )
        lines.append("")

    sweeps = [r for r in records if (r.get("commit_sweep") or {}).get("ran")]
    if sweeps:
        lines += ["## Post-commit sweep (handoff [G3])", ""]
        for record in sweeps:
            sweep = record["commit_sweep"]
            if sweep.get("refused"):
                lines.append(f"- **{record['id']}**: commit refused — {sweep['refused']}")
                continue
            lines.append(
                f"- **{record['id']}**: commit {sweep['commit_sha'][:10]} staged "
                f"{sweep['n_committed']} path(s), of which unscanned: "
                f"{sweep['unscanned_paths'] or 'none'}"
            )
        lines.append("")

    # ── per-mutant ledger ──────────────────────────────────────────────────
    lines += [
        "## Per-mutant record",
        "",
        "| Mutant | Family | Base | Expected | Caught by | Outcome | Attribution | Hole | s | $ |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            f"| {record['id']} | {record['family']} | {record['base']} | {record['expected']} | "
            f"{', '.join(record['caught_by']) or '---'} | {record['outcome']} | "
            f"{record.get('review_attribution') or '---'} | "
            f"{record.get('known_hole') or '---'} | {record.get('duration_s', 0):.0f} | "
            f"{record.get('cost_usd', 0):.2f} |"
        )
    lines.append("")
    return "\n".join(lines)
