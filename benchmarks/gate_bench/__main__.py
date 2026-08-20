"""CLI: run the corpus through the production gate and write the catch-rate matrix.

    python -m benchmarks.gate_bench [--only id,...] [--families f,...] [--review]
                                    [--toolkit-ref REF] [--out DIR] [--list]

Deterministic legs cost nothing and need no keys. ``--review`` adds the live
provider-decorrelated co-lead call on every gate-passing changeset, which is real model
spend; the matrix reports what it cost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from benchmarks.gate_bench.harness import (
    ABLATIONS,
    DEFAULT_TOOLKIT_REF,
    REPO_ROOT,
    RunConfig,
    collect_stamps,
    run_mutant,
)
from benchmarks.gate_bench.mutants import FAMILY_ORDER, by_id, corpus
from benchmarks.gate_bench.score import ROSTER_NAME, build_matrix, load_roster, score_record

OUT_ROOT = REPO_ROOT / "logs" / "dev" / "gate-bench"


def _toolkit_root() -> Path:
    with (REPO_ROOT / "config" / "orchestration.yaml").open(encoding="utf-8") as handle:
        return Path((yaml.safe_load(handle) or {})["toolkit_root"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="benchmarks.gate_bench", description=__doc__)
    parser.add_argument("--only", help="comma-separated mutant ids")
    parser.add_argument("--families", help="comma-separated family names (substring match)")
    parser.add_argument("--review", action="store_true", help="run the live co-lead review leg")
    parser.add_argument("--toolkit-ref", default=DEFAULT_TOOLKIT_REF,
                        help=f"toolkit branch or sha the real mutants are built on (default {DEFAULT_TOOLKIT_REF})")
    parser.add_argument("--out", type=Path, help="output directory (default logs/dev/gate-bench/<UTC>)")
    parser.add_argument("--ablate", default="none", choices=sorted(ABLATIONS),
                        help="disable one defence over the identical corpus (paper item 2). "
                             "'review' is not a value here: omit --review for that configuration")
    parser.add_argument("--keep-worktrees", action="store_true", help="leave temp worktrees on disk")
    parser.add_argument("--list", action="store_true", help="list the corpus and exit")
    parser.add_argument("--reviewer", default="gpt_5_6_sol",
                        help="roster member driving the review leg (benchmarks/gate_bench/"
                             "reviewers.yaml); only meaningful with --review")
    parser.add_argument("--compare", type=Path, nargs="+", metavar="DIR",
                        help="merge finished runs into one cross-model audit report and exit")
    parser.add_argument("--rescore", type=Path, metavar="DIR",
                        help="re-score an existing run's records against the roster and rewrite "
                             "its matrix, without re-running anything (costs nothing)")
    return parser.parse_args(argv)


def run_dir_name(stamp: str, *, reviewer: str | None, pid: int | None = None) -> str:
    """Name a run directory so that concurrent runs cannot share one.

    The timestamp alone is not enough: runs launched together reach this line within the same
    second, and three processes appending to one ``records.jsonl`` interleave their writes.
    Records here exceed the 4 KB size past which an append stops being atomic, so a collision
    risks corrupting the audit trail rather than merely mixing it (observed 2026-08-18).
    """
    return f"{stamp}-{reviewer or 'nolive'}-{pid or os.getpid()}"


def compare(run_dirs: list[Path]) -> int:
    """Merge finished per-model runs into one audit document under the output root."""
    from benchmarks.gate_bench.swap import write_swap_report

    out_dir = OUT_ROOT / ("model-swap-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    out_dir.mkdir(parents=True, exist_ok=True)
    result = write_swap_report(list(run_dirs), out_dir / "model_swap.md")
    for row in result["runs"]:
        print(f"{row['reviewer']:18s} defects {row['defects_rejected']:>2}/{row['defects_reviewed']:<2}"
              f"  controls rejected {row['controls_rejected']:>2}/{row['controls_reviewed']:<2}"
              f"  pairs {row['pairs_discriminated']:>2}/{row['pairs_judged']:<2}"
              f"  ${row['cost_usd']:.2f}")
    print(f"\nwrote {result['path']}")
    return 0


def rescore(run_dir: Path) -> int:
    """Re-apply the attribution roster to a finished run and rewrite its matrix.

    Reading verdicts is a human pass that happens *after* a run, and a re-run would charge for
    the same co-lead calls again, so the roster is applied to the records already on disk.
    """
    jsonl_path = run_dir / "records.jsonl"
    records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    roster = load_roster(OUT_ROOT / ROSTER_NAME)

    # Corpus *predictions* are refreshed from the current corpus; the *measurements* (the gate
    # report, the co-lead verdict, cost, duration) are never touched. An expectation is a claim
    # the corpus makes and may revise once measured — scoring a finished run against a retired
    # expectation would report a mismatch that no longer exists.
    current = by_id()
    for record in records:
        mutant = current.get(record["id"])
        if mutant is not None:
            record.update(family=mutant.family, expected=mutant.expected,
                          known_hole=mutant.known_hole, notes=mutant.notes,
                          description=mutant.description)
    scored = [score_record(record, roster) for record in records]
    stamps = {k: scored[0][k] for k in (
        "auto_research_sha", "auto_research_dirty", "toolkit_ref", "toolkit_sha",
        "code_gate_sha256", "ablation", "review_enabled") if scored and k in scored[0]}
    jsonl_path.write_text(
        "".join(json.dumps(r) + "\n" for r in scored), encoding="utf-8"
    )
    matrix = build_matrix(scored, stamps)
    (run_dir / "matrix.md").write_text(matrix, encoding="utf-8")
    print(matrix)
    print(f"\nre-scored {len(scored)} records in {run_dir} against {len(roster)} roster decisions")
    return 1 if any(r["expectation"] in ("unmet", "error") for r in scored) else 0


def select(args: argparse.Namespace):
    mutants = corpus()
    if args.only:
        wanted = {m.strip() for m in args.only.split(",")}
        unknown = wanted - {m.id for m in mutants}
        if unknown:
            raise SystemExit(f"unknown mutant id(s): {sorted(unknown)}")
        mutants = [m for m in mutants if m.id in wanted]
    if args.families:
        keys = [k.strip().lower() for k in args.families.split(",")]
        mutants = [m for m in mutants if any(k in m.family.lower() for k in keys)]
    return mutants


async def _run(args: argparse.Namespace) -> int:
    mutants = select(args)
    toolkit_root = _toolkit_root()
    needs_toolkit = any(m.base == "real" for m in mutants)

    stamps = collect_stamps(toolkit_root, args.toolkit_ref, resolve_toolkit=needs_toolkit)
    # The configuration is part of the measurement, so it is stamped beside the commit hashes:
    # a matrix is only meaningful against the set of defences that were actually enabled.
    stamps = {**stamps, "ablation": args.ablate,
              "review_enabled": str(bool(args.review)),
              "reviewer": args.reviewer if args.review else "(review not run)"}

    cfg = RunConfig(
        toolkit_root=toolkit_root, toolkit_ref=args.toolkit_ref, review=args.review,
        keep_worktrees=args.keep_worktrees, stamps=stamps,
        ablation=args.ablate, reviewer=args.reviewer, **ABLATIONS[args.ablate],
    )

    meter = None
    if args.review:
        # The review leg makes real provider calls, so it needs the project .env the pipeline
        # loads at launch. Validate it here, before the first mutant, rather than letting every
        # co-lead call fail on missing credentials one by one.
        from auto_research.env import check_required_env
        from auto_research.observability.costs import install_cost_meter

        check_required_env()
        meter = install_cost_meter(run_id="gate-bench")

    out_dir = args.out or OUT_ROOT / run_dir_name(
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        reviewer=args.reviewer if args.review else None,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "records.jsonl"

    # The attribution roster lives beside the per-run directories, so a hand-read decision
    # survives every later run of the corpus.
    roster = load_roster(OUT_ROOT / ROSTER_NAME)

    records = []
    for index, mutant in enumerate(mutants, 1):
        print(f"[{index:>2}/{len(mutants)}] {mutant.id} ({mutant.family})", flush=True)
        record = score_record(await run_mutant(mutant, cfg, meter=meter), roster)
        records.append(record)
        with jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        print(
            f"          -> {record['outcome']}"
            f" [{', '.join(record['caught_by']) or 'no layer'}]"
            f" ({record['duration_s']:.0f}s)"
            + (f"  ERROR {record['error']}" if record.get("error") else ""),
            flush=True,
        )

    matrix = build_matrix(records, stamps)
    (out_dir / "matrix.md").write_text(matrix, encoding="utf-8")
    print("\n" + matrix)
    print(f"\nwrote {jsonl_path} and {out_dir / 'matrix.md'}")
    if any(r.get("error") for r in records):
        return 1
    # The corpus's expectations describe the FULL stack, so under an ablation a displaced catch is
    # the measurement rather than a fault: exit status asserts predictions only when every defence
    # was enabled. Errors still fail regardless of configuration.
    if args.ablate != "none":
        return 0
    return 1 if any(r["expectation"] == "unmet" for r in records) else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.compare:
        return compare(args.compare)
    if args.rescore:
        return rescore(args.rescore)
    if args.list:
        for family in FAMILY_ORDER:
            rows = [m for m in select(args) if m.family == family]
            if not rows:
                continue
            print(f"\n{family}")
            for mutant in rows:
                hole = f" [{mutant.known_hole}]" if mutant.known_hole else ""
                print(f"  {mutant.id:<34} {mutant.base:<10} expect={mutant.expected}{hole}")
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
