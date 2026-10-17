"""Emit the catch-rate table (Table 3) from a gate_bench run's records. Nothing is transcribed.

    python -m benchmarks.gate_bench.table_catchrate <run_dir>

Writes ``papers/FANGZ/tables/catchrate.tex``, which ``main.tex`` inputs.

**Why this exists rather than a hand-typed table.** On 2026-08-27 the table was updated by computing
the numbers in a throwaway shell heredoc and typing them into ``main.tex``. The first attempt was
wrong -- it classified catches on ``outcome == "caught"`` alone, not knowing the vocabulary also
contains ``false_reject``, which put the control row at 0/69 instead of 47/69. It was caught only
because the scan and gate columns should have been invariant across the change, and they were not.
A saved generator makes that class of error visible once instead of silently.

**The outcome vocabulary, which is what the first attempt got wrong** (from ``harness.run_mutant``):
``caught`` a defect was stopped; ``reached_commit`` a defect survived to a commit; ``false_reject``
a CONTROL was rejected; ``clean_pass`` a control was correctly approved. Attribution between layers
comes from ``caught_by``: ``scan`` is the no-fabrication scan, a catch with no reviewer recorded is
the deterministic gate, and the rest are the review.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

#: The paper consumes the fragment; the generator lives with the benchmark it reads (user,
#: 2026-08-27: benchmark retrievals and plotters stay together, never in scratch code).
OUT = Path(__file__).resolve().parents[2] / "papers/FANGZ/tables/catchrate.tex"

#: Row order as the manuscript prints it: fabrication first, then science, then structure. Fixed
#: here rather than sorted; `main` cross-checks the records' families against this list, so a
#: family added to the corpus fails loudly to be placed deliberately instead of silently
#: vanishing from the table (the tally dict is a defaultdict, which alone would hide it).
FAMILY_ORDER = [
    "Fabrication, in-vocabulary",
    "Fabrication, vocabulary gap",
    "Science bug, adequate test",
    "Science bug, inadequate test",
    "Vacuous or nondeterministic tests",
    "Silent capability bound",
    "Structural or manifest",
    "Known-hole probes",
]
CONTROL_FAMILY = "Control (clean changeset)"

#: The printed label for each record family. The keys are the strings the corpus writes into
#: `records.jsonl` and must not change; the values are what the manuscript prints, so the table
#: uses the intent names Sect.~5.2 declares rather than the corpus's internal wording. The
#: `<class>, <variant>` form is the paper's existing convention for a grouped row label.
DISPLAY = {
    "Fabrication, in-vocabulary": "Fabrication, with a banned word",
    "Fabrication, vocabulary gap": "Fabrication, without a banned word",
    "Science bug, adequate test": "Wrong result, test exposes it",
    "Science bug, inadequate test": "Wrong result, test misses it",
    "Vacuous or nondeterministic tests": "Tests that check nothing",
    "Silent capability bound": "Hidden limit",
    "Structural or manifest": "Missing structure or manifest",
    "Known-hole probes": "Invisible to the gate",
}


def tally(records: list[dict]) -> dict[str, dict[str, int]]:
    """Per-family counts, attributed to the layer that stopped each changeset."""
    out: dict[str, dict[str, int]] = defaultdict(
        lambda: dict(n=0, scan=0, gate=0, review_caught=0, reviewed=0, commit=0, false_reject=0)
    )
    for record in records:
        row = out[record.get("family", "?")]
        row["n"] += 1
        caught_by = [str(c) for c in (record.get("caught_by") or [])]
        reviewed = bool((record.get("review") or {}).get("reviewer"))
        if reviewed:
            row["reviewed"] += 1
        outcome = record.get("outcome")
        if outcome == "caught":
            if "scan" in caught_by:
                row["scan"] += 1
            elif not reviewed:
                row["gate"] += 1
            else:
                row["review_caught"] += 1
        elif outcome == "reached_commit":
            row["commit"] += 1
        elif outcome == "false_reject":
            row["false_reject"] += 1
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: make_catchrate_table.py <run_dir>")
    run_dir = Path(argv[1])
    records = [
        json.loads(line)
        for line in (run_dir / "records.jsonl").read_text().splitlines()
        if line.strip()
    ]
    counts = tally(records)
    unplaced = set(counts) - set(FAMILY_ORDER) - {CONTROL_FAMILY}
    missing = set(FAMILY_ORDER) - set(DISPLAY)
    if missing:
        raise SystemExit(f"families with no DISPLAY label: {sorted(missing)}")
    if unplaced:
        raise SystemExit(f"families in the records but not in FAMILY_ORDER: {sorted(unplaced)}")
    reviewer = next(
        (r["reviewer"] for r in records if r.get("reviewer")), "(unknown)"
    )
    # Record leg keys are `gpt_5_6_sol`; the model id the paper prints is `gpt-5.6-sol`.
    reviewer_id = re.sub(r"_(\d)_(\d)", r"-\1.\2", reviewer).replace("_", "-")

    rows: list[str] = []
    totals = dict(n=0, scan=0, gate=0, review_caught=0, reviewed=0, commit=0)
    for family in FAMILY_ORDER:
        c = counts[family]
        review = "---" if c["reviewed"] == 0 else f"{c['review_caught']}/{c['reviewed']}"
        rows.append(
            f"{DISPLAY[family]} & {c['n']} & {c['scan']} & {c['gate']} & {review} & "
            f"{c['commit']} \\\\"
        )
        for key in totals:
            totals[key] += c[key]
    rows.append("\\hline")
    rows.append(
        f"All planted defects & {totals['n']} & {totals['scan']} & {totals['gate']} & "
        f"{totals['review_caught']}/{totals['reviewed']} & {totals['commit']} \\\\"
    )
    ctl = counts[CONTROL_FAMILY]
    rows.append(
        f"Control (defect-free change) & {ctl['n']} & 0 & 0 & "
        f"{ctl['false_reject']}/{ctl['reviewed']} & {ctl['n'] - ctl['false_reject']} \\\\"
    )

    OUT.write_text(
        "% GENERATED by benchmarks/gate_bench/table_catchrate.py -- do not edit by hand.\n"
        f"% source: {run_dir}/records.jsonl\n"
        "\\centering\n"
        "\\captionof{table}{Planted defects stopped by each check, by defect class, with\n"
        f"\\texttt{{{reviewer_id}}} as the co-lead. The ``scan'' column covers gate checks 3--5 of\n"
        "Sect.~\\ref{ss:selfext}, and the ``tests, structure'' column covers checks 1, 2, 6 and 7. The\n"
        "co-lead column counts rejections among the changes that passed the gate. Tests that check\n"
        "nothing have no assertions, cannot fail, or are skipped. A hidden limit is a plausible fixed\n"
        "constant or an upstream product accepted as complete. Missing structure is a missing test file\n"
        "or manifest entry. Defects invisible to the gate exploit known weaknesses of the gate\n"
        "checks. The controls\n"
        "are defect-free changes, so each co-lead rejection of a control is a false rejection.\n"
        "Fig.~\\ref{f:modelswap} compares eight models as the co-lead.}\n"
        "\\label{t:catchrate}\n"
        "\\small\n"
        "\\begin{tabular}{@{}lccccc@{}}\n"
        "\\hline\n"
        "Defect class & Defects & \\multicolumn{2}{c}{Gate} & Co-lead & Reaching \\\\\n"
        " & & scan & tests, structure & & a commit \\\\\n"
        "\\hline\n" + "\n".join(rows) + "\n"
        "\\hline\n"
        "\\end{tabular}\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")
    print(
        f"  reviewer={reviewer}  planted={totals['n']}  scan={totals['scan']}  "
        f"gate={totals['gate']}  review={totals['review_caught']}/{totals['reviewed']}  "
        f"commits={totals['commit']}  false_rejections={ctl['false_reject']}/{ctl['reviewed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
