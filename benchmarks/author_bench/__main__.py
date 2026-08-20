"""CLI: author one fixed brief K times through the production loop and report the distribution.

    python -m benchmarks.author_bench --k 15 --concurrency 4
    python -m benchmarks.author_bench --check          # preflight only, no spend
    python -m benchmarks.author_bench --rescore DIR    # re-render a finished batch

Every trial is real model spend on three roles (Opus lead, Sonnet coder, GPT co-lead). The
report states what it cost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import yaml

from benchmarks.author_bench.brief import BASE_SHA, VALIDATION_OBSIDS, load_brief
from benchmarks.author_bench.score import build_report, load_records
from benchmarks.author_bench.trial import REPO_ROOT, TrialConfig, collect_stamps, run_trial

OUT_ROOT = REPO_ROOT / "logs" / "dev" / "author-bench"


def trial_indices(args: argparse.Namespace) -> list[int]:
    """The trial numbers to run: ``1..k``, or exactly the ones ``--only`` names.

    Numbering is the identity of a trial across batches, so a re-run keeps the numbers it
    replaces and the two record sets merge without renaming anything.
    """

    if args.only:
        return [int(part) for part in args.only.split(",") if part.strip()]
    return list(range(1, args.k + 1))


def _describe(indices: list[int]) -> str:
    return ", ".join(str(i) for i in indices)


def _orchestration() -> dict:
    with (REPO_ROOT / "config" / "orchestration.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _toolkit_root() -> Path:
    return Path(_orchestration()["toolkit_root"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="benchmarks.author_bench", description=__doc__)
    parser.add_argument("--k", type=int, default=15, help="number of trials (default 15)")
    parser.add_argument("--only", help="comma-separated trial indices to run instead of 1..k, for "
                                       "re-running specific trials (their numbering is preserved, "
                                       "so a re-run merges with the batch it replaces)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="trials in flight at once (default 4)")
    parser.add_argument("--base-ref", default=BASE_SHA,
                        help=f"toolkit state each trial authors from (default {BASE_SHA}, the "
                             "parent of the commit that originally landed this tool)")
    parser.add_argument("--out", type=Path, help="output directory (default logs/dev/author-bench/<UTC>)")
    parser.add_argument("--chatter", type=int, default=4, help="per-trial monitor chatter (default 4)")
    parser.add_argument("--keep-clones", action="store_true", help="leave trial clones on disk")
    parser.add_argument("--check", action="store_true",
                        help="run the preflight (env, base ref, validation data, brief) and exit")
    parser.add_argument("--rescore", type=Path, nargs="+", metavar="DIR",
                        help="re-render finished batches from their records; costs nothing. Several "
                             "directories merge into one report, a later directory's trial "
                             "replacing an earlier one of the same number — which is how a re-run "
                             "of contaminated trials is combined with the batch it repairs")
    return parser.parse_args(argv)


def preflight(toolkit_root: Path, base_ref: str) -> list[str]:
    """Every check that can be decided without a model call, before any trial spends anything.

    The gate benchmark lost 26 runs to a missing ``.env`` that was only discovered one failed
    call at a time; a batch of authoring trials is far more expensive to discover that way.
    """
    from auto_research.env import check_required_env
    from auto_research.models.keys import check_api_key_fallbacks

    from benchmarks.author_bench.brief import BRIEF_PATH
    from benchmarks.author_bench.trial import resolve_base_sha

    problems: list[str] = []
    try:
        check_required_env()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"environment: {exc}")
    # A batch leans on key failover: it runs for hours across three roles, and the first batch
    # was lost to a provider quota. A fallback that is silently unusable would be discovered the
    # same way, so it is validated here rather than believed.
    try:
        check_api_key_fallbacks()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"api key fallbacks: {exc}")
    try:
        spec = load_brief()
        if not spec.expected_behavior.strip():
            problems.append(f"brief {BRIEF_PATH} carries no expected_behavior")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"brief: {exc}")
    try:
        resolve_base_sha(toolkit_root, base_ref)
    except SystemExit as exc:
        problems.append(str(exc))

    # The brief's validation observations must be on disk: K coders discovering a missing input
    # one at a time would measure data availability, not authoring reliability.
    obs_root = Path(yaml.safe_load(
        (REPO_ROOT / "config" / "observations.yaml").read_text(encoding="utf-8")
    )["obs_db_root"]) / "IXPE"
    missing = [o for o in VALIDATION_OBSIDS
               if not (obs_root / o / f"pcube_timeseg_{o}.csv").exists()]
    if missing:
        problems.append(f"validation products absent under {obs_root}: {', '.join(missing)}")

    # A coder session is a Claude Agent SDK subprocess, so the CLI it drives has to resolve before
    # K trials discover its absence one dead session at a time. The SDK is asked where its own CLI
    # is rather than probing PATH: recent versions ship a bundled binary that ``which`` cannot see,
    # so a PATH probe would reject a working setup.
    try:
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

        SubprocessCLITransport._find_cli(SubprocessCLITransport.__new__(SubprocessCLITransport))
    except Exception as exc:  # noqa: BLE001 — the SDK's own message names the fix
        problems.append(f"Claude Agent SDK CLI: {exc}")
    return problems


async def _run(args: argparse.Namespace) -> int:
    toolkit_root = _toolkit_root()
    problems = preflight(toolkit_root, args.base_ref)
    if problems:
        for problem in problems:
            print(f"PREFLIGHT: {problem}")
        return 2
    indices = trial_indices(args)
    print(f"preflight clean: {len(indices)} trials ({_describe(indices)}), "
          f"{args.concurrency} concurrent, base {args.base_ref}")
    if args.check:
        return 0

    stamps = collect_stamps(toolkit_root, args.base_ref)
    out_dir = args.out or OUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "records.jsonl"
    (out_dir / "brief.json").write_text(
        json.dumps(load_brief().model_dump(mode="json"), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    cfg = TrialConfig(toolkit_root=toolkit_root, out_dir=out_dir, base_sha=args.base_ref,
                      stamps=stamps, keep_clones=args.keep_clones, chatter=args.chatter)
    gate = asyncio.Semaphore(args.concurrency)
    write_lock = asyncio.Lock()
    records: list[dict] = []

    async def one(index: int) -> None:
        async with gate:
            print(f"[trial {index:02d}] start", flush=True)
            record = await run_trial(index, cfg)
            async with write_lock:
                records.append(record)
                with jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
            print(
                f"[trial {index:02d}] {record.get('status')}"
                f"  gate {record.get('gate_passes', 0)}/{record.get('gate_rejections', 0)}"
                f"  review {record.get('review_approvals', 0)}/{record.get('review_rejections', 0)}"
                f"  ${record.get('cost_usd', 0.0):.2f}"
                f"  {record.get('duration_s', 0.0) / 60:.0f} min", flush=True,
            )

    await asyncio.gather(*(asyncio.create_task(one(i)) for i in trial_indices(args)))

    report = build_report(records, stamps)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"wrote {jsonl_path} and {out_dir / 'report.md'}")
    return 1 if any(r.get("status") == "error" for r in records) else 0


def rescore(run_dirs: list[Path]) -> int:
    """Re-render finished batches. Measurements on disk are never recomputed, only presented.

    With several directories, a later one's trial replaces an earlier one of the same number:
    a re-run repairs the batch it replaces rather than being reported beside it.
    """
    by_trial: dict[int, dict] = {}
    for run_dir in run_dirs:
        for record in load_records(run_dir):
            by_trial[record["trial"]] = record
    records = [by_trial[k] for k in sorted(by_trial)]
    run_dir = run_dirs[-1]
    stamp_keys = ("auto_research_sha", "auto_research_dirty", "toolkit_base_ref",
                  "toolkit_base_sha", "code_gate_sha256", "brief_run_id", "brief_lead_model")
    stamps = {k: records[0][k] for k in stamp_keys if records and k in records[0]}
    report = build_report(records, stamps)
    (Path(run_dir) / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.rescore:
        return rescore(args.rescore)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
