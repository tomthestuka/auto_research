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
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml

from benchmarks.author_bench.brief import BASE_SHA, VALIDATION_OBSIDS, load_brief
from benchmarks.author_bench.legs import Leg, resolve as resolve_legs
from benchmarks.author_bench.score import build_report, load_records
from benchmarks.author_bench.trial import REPO_ROOT, TrialConfig, collect_stamps, run_trial

OUT_ROOT = REPO_ROOT / "logs" / "dev" / "author-bench"


def trial_indices(args: argparse.Namespace, leg=None) -> list[int]:
    """The trial numbers to run: ``1..k``, or exactly the ones ``--only`` names.

    Numbering is the identity of a trial **within its leg**, so a re-run keeps the numbers it
    replaces and the two record sets merge without renaming anything. ``--k`` overrides the leg's
    own K; without it each leg runs the K its roster row declares.
    """

    if args.only:
        return [int(part) for part in args.only.split(",") if part.strip()]
    k = args.k if args.k is not None else (leg.trials if leg is not None else 10)
    return list(range(1, k + 1))


def _describe(indices: list[int]) -> str:
    return ", ".join(str(i) for i in indices)


def _orchestration() -> dict:
    with (REPO_ROOT / "config" / "orchestration.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _toolkit_root() -> Path:
    return Path(_orchestration()["toolkit_root"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="benchmarks.author_bench", description=__doc__)
    parser.add_argument("--k", type=int, default=None,
                        help="trials per leg (default: the leg's own K, 10)")
    parser.add_argument("--legs", default=None,
                        help="comma-separated leg slugs to run (default: every runnable leg in "
                             "benchmarks.author_bench.legs.ROSTER; rows marked reuse are skipped "
                             "unless named). One leg = one author/reviewer arrangement.")
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


def probe_leg_vendor(leg: Leg) -> str | None:
    """One trivial call to a leg's vendor: does this account answer for this model?

    Returns a problem string, or ``None`` when the leg is good to run. Costs a fraction of a cent.
    The Claude harness has nothing to probe here: it authenticates from stored subscription
    credentials rather than a key, and the SDK-CLI check below covers its one failure mode.

    **The probe participates in key failover, and that is the point.** The key pool only advances
    reactively, when a call fails, so at launch it still holds the primary even when the primary is
    spent. This probe is the run's first real call to that vendor, so it fails over exactly as
    ``call_with_transient_retry`` does: a spent balance retires that key for the process and a rate
    limit rotates past it. The pool is process-global, so the batch then launches its authors on the
    key that answered rather than discovering the dead one inside an author subprocess, one trial at
    a time, after each trial's lead spend is already gone.
    """
    if leg.harness != "codex":
        return None

    from openai import OpenAI

    from auto_research.models.keys import ROTATING_PROVIDERS, key_pool
    from auto_research.models.retry import (
        is_exhausted_key_error,
        is_key_scoped_provider_error,
    )
    from auto_research.sandbox.coder.codex_sdk import resolve_vendor_key

    where = leg.base_url or "the default endpoint"
    tried: list[str] = []
    while True:
        # Resolved exactly as the author process will resolve it, pool and all: a probe reading the
        # raw variable would report on a credential nothing ends up using.
        key, slot = resolve_vendor_key(leg.provider, leg.key_env)
        if leg.key_env and not key:
            return f"${leg.key_env} is unset"
        try:
            # The synchronous client on purpose: preflight is a blocking function called from
            # inside the batch's event loop, so an `asyncio.run` here would report the loop
            # conflict as a vendor refusal and hide whatever the vendor actually said.
            OpenAI(api_key=key or None, base_url=leg.base_url).responses.create(
                model=leg.model, input="ok", max_output_tokens=16
            )
            return None
        except Exception as exc:  # noqa: BLE001 — the vendor's own message names the fix
            tried.append(slot)
            rotatable = (
                leg.provider in ROTATING_PROVIDERS
                and is_key_scoped_provider_error(exc, leg.provider)
            )
            if not rotatable:
                return f"{leg.model} at {where} (key slot {slot}) refused: {exc}"
            pool = key_pool(leg.provider)
            if is_exhausted_key_error(exc, leg.provider):
                retired = pool.retire_active()
                print(f"  {leg.slug}: key slot {retired} is spent, retired for this process")
            if pool.rotate(exclude=frozenset(tried)) is None:
                return (
                    f"{leg.model} at {where} refused on every configured key "
                    f"({', '.join(tried)}): {exc}"
                )


def preflight(toolkit_root: Path, base_ref: str, legs: list[Leg]) -> list[str]:
    """Every check that can be decided without a model call, before any trial spends anything.

    The gate benchmark lost 26 runs to a missing ``.env`` that was only discovered one failed
    call at a time; a batch of authoring trials is far more expensive to discover that way.
    """
    from auto_research.env import check_required_env
    from auto_research.models.keys import check_api_key_fallbacks
    from auto_research.sandbox.jail import (
        binary_reachable_in_jail,
        check_os_jail_available,
        check_os_jail_probe,
        harness_binary,
    )

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
    # With the OS jail on, every author subprocess and every Layer-B pytest runs inside it. A jail
    # that cannot start, or that breaks the science stack, would otherwise fail identically in all
    # K trials — so it is decided once, here, before anything spends.
    for check in (check_os_jail_available, check_os_jail_probe):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 — the check's own message names the fix
            problems.append(f"os jail ({check.__name__}): {exc}")
    # Per leg, not just the configured harness: a sweep runs harnesses `models.yaml` does not name,
    # and an unbound harness binary is what lost all 40 trials of the first jailed batch.
    for harness in sorted({leg.harness for leg in legs}):
        binary = harness_binary(harness)
        if binary is None:
            problems.append(f"os jail: the {harness} author's CLI could not be resolved")
            continue
        ok, why = binary_reachable_in_jail(binary, label=harness)
        if not ok:
            problems.append(f"os jail: the {harness} author's CLI ({binary}) is unusable: {why}")
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

    # Each leg's vendor must answer before the batch spends anything on the Opus lead. A leg whose
    # key is missing, whose account is not entitled, or whose prepaid balance is empty fails
    # *inside* the author subprocess, one trial at a time, after that trial's lead spend is already
    # gone — and a harness-only vendor's key never passes through the chat-provider key rotation, so
    # nothing recovers it. The gate benchmark learned this the expensive way, losing 26 runs to a
    # missing key discovered one failed call at a time.
    for leg in legs:
        problem = probe_leg_vendor(leg)
        if problem:
            problems.append(f"leg {leg.slug}: {problem}")

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
    legs = resolve_legs(args.legs)
    problems = preflight(toolkit_root, args.base_ref, legs)
    if problems:
        for problem in problems:
            print(f"PREFLIGHT: {problem}")
        return 2
    plan = [(leg, trial_indices(args, leg)) for leg in legs]
    total = sum(len(idx) for _, idx in plan)
    print(f"preflight clean: {len(plan)} leg(s), {total} trials, base {args.base_ref}")
    share_preview = max(1, -(-args.concurrency // max(1, len(plan))))
    for leg, idx in plan:
        limit = min(share_preview, leg.max_concurrency or share_preview)
        print(f"  {leg.slug:28} {leg.model:20} {len(idx)} trials, <={limit} concurrent"
              f"{'  [correlated review]' if leg.correlated else ''}")
    print(f"  legs run concurrently: a fair share each, under a global ceiling of "
          f"{args.concurrency} trials in flight")
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

    write_lock = asyncio.Lock()
    records: list[dict] = []

    async def one(leg: Leg, cfg: TrialConfig, own: asyncio.Semaphore,
                  everything: asyncio.Semaphore, index: int) -> None:
        # Per-leg first, global second, and the order is the whole point: taking the global slot
        # first lets a trial hold it while queued behind its own leg's cap, so the legs created
        # first hold every global slot and the later legs are starved to zero (observed 2026-08-20 —
        # two legs never started until the first two had drained). It does not deadlock, since a
        # finishing trial releases both, but it serialises what is meant to interleave.
        async with own, everything:
            print(f"[{leg.slug} {index:02d}] start", flush=True)
            record = await run_trial(index, cfg)
            async with write_lock:
                records.append(record)
                with jsonl_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
            print(
                f"[{leg.slug} {index:02d}] {record.get('status')}"
                f"  gate {record.get('gate_passes', 0)}/{record.get('gate_rejections', 0)}"
                f"  review {record.get('review_approvals', 0)}/{record.get('review_rejections', 0)}"
                f"  ${record.get('cost_usd', 0.0):.2f}"
                f"  {record.get('duration_s', 0.0) / 60:.0f} min", flush=True,
            )

    # Legs run concurrently, under TWO ceilings, because the constraints are not the same shape.
    #
    # Each leg's author is a different vendor, so their coder rate limits do not interact — that is
    # what makes cross-leg concurrency safe. What every trial *does* share is the rest of the team:
    # the Opus lead is Anthropic in all 40 trials and the co-lead is OpenAI in every tier trial, so
    # lead and co-lead rate pressure is a function of **total** trials in flight, not of any one
    # leg. Hence a global ceiling (`--concurrency`) over the whole batch and a per-leg ceiling from
    # the roster row (the Claude author's shared subscription session cap).
    everything = asyncio.Semaphore(args.concurrency)
    # A global ceiling alone is not enough: the first leg's trials all queue on it and take every
    # slot, so the batch runs one leg at a time in disguise (observed 2026-08-20 — ten DeepSeek
    # trials held all ten slots and no other leg started). Each leg therefore also gets a fair
    # share, so every leg progresses and the comparison is not one leg measured hours after another
    # under different provider conditions.
    share = max(1, -(-args.concurrency // max(1, len(plan))))

    async def run_leg(leg: Leg, indices: list[int]) -> None:
        cfg = TrialConfig(
            toolkit_root=toolkit_root, out_dir=out_dir / leg.slug, base_sha=args.base_ref,
            stamps={**stamps, "leg": leg.slug, "coder_model": leg.model,
                    "coder_provider": leg.provider, "coder_harness": leg.harness,
                    "colead_model": leg.colead_model or "(configured)",
                    "correlated_review": str(leg.correlated)},
            keep_clones=args.keep_clones, chatter=args.chatter, leg=leg,
        )
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        own = asyncio.Semaphore(min(share, leg.max_concurrency or share))
        await asyncio.gather(
            *(asyncio.create_task(one(leg, cfg, own, everything, i)) for i in indices)
        )

    await asyncio.gather(*(asyncio.create_task(run_leg(leg, idx)) for leg, idx in plan))

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
    by_trial: dict[tuple[str, int], dict] = {}
    for run_dir in run_dirs:
        for record in load_records(run_dir):
            by_trial[(record.get("leg") or "", record["trial"])] = record
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
