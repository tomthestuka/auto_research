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
import sys
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
    parser.add_argument("--detach", action="store_true",
                        help="spawn the batch orphaned (double-fork) and return immediately, so it "
                             "survives this shell, an SSH drop and a VSCode logout. Prints the "
                             "output directory and the --status/--resume commands for it")
    parser.add_argument("--resume", metavar="DIR", type=Path, default=None,
                        help="continue an interrupted batch in DIR: re-runs exactly the (leg, "
                             "trial) pairs with no record yet, or whose record is an "
                             "infrastructure error, and leaves every completed measurement alone")
    parser.add_argument("--status", metavar="DIR", type=Path, default=None,
                        help="print a batch's progress and whether it is still alive; costs "
                             "nothing and never touches the run")
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


def probe_chat_member(provider_name: str, model: str) -> str | None:
    """One trivial call to a vendor a leg pins as its lead or co-lead.

    The chat analogue of :func:`probe_leg_vendor`, and it goes through ``ModelHandle`` rather than
    a raw SDK client, so it exercises the layer the run exercises -- key resolution, rotation and
    the retry policy included. A vendor that answers here is one whose account, entitlement and
    balance are good for the roles the grid pins it to.

    Returns a problem string, or ``None`` when the member is good to run.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from auto_research.models.providers import provider_for
    from auto_research.models.types import ModelHandle, ModelMessage, RoleModelConfig

    member = RoleModelConfig(
        name=f"preflight_{provider_name}",
        provider=provider_name,
        model=model,
        vision=False,
        parameters=(
            {"max_tokens": 16}
            if provider_name == "anthropic"
            else {"max_output_tokens": 16}
        ),
    )
    handle = ModelHandle(
        role="preflight", config=member, provider=provider_for(provider_name)
    )
    # On its own thread with its own loop, because `preflight` is a blocking function called from
    # inside the batch's event loop: a bare `asyncio.run` here raises "cannot be called from a
    # running event loop", which the except below would then report as five vendor refusals,
    # hiding whatever the vendors actually said. `probe_leg_vendor` dodges this by using the
    # synchronous SDK client; this probe cannot, because going through `ModelHandle` is the whole
    # point (it is what makes key rotation apply), and `ModelHandle.call` is async.
    def _call() -> None:
        # Not `asyncio.run`: it closes the loop the instant the coroutine returns, while the
        # adapter's per-call SDK client is still holding an httpx transport whose finalizer then
        # fires against a dead loop and prints an "Event loop is closed" traceback. Harmless, but
        # it lands in a log a human reads days later looking for the reason a batch died. Draining
        # async generators and yielding once before closing lets those transports shut down first.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handle.call([ModelMessage(role="user", content="ok")]))
            loop.run_until_complete(asyncio.sleep(0))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(_call).result()
    except Exception as exc:  # noqa: BLE001 — the vendor's own message names the fix
        return f"refused: {exc}"
    return None


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

    # The same argument for the *chat* vendors a leg pins. `probe_leg_vendor` covers the author
    # alone, which was sufficient while every lead and reviewer came from the configured roles and
    # so from an account the run was already using. The lead x co-lead grid breaks that assumption:
    # a cell can pin a lead and a reviewer from two vendors the author never touches, and a dead
    # key there surfaces mid-trial, after the author has already been paid for. Probed once per
    # distinct (provider, model) rather than once per leg, since twenty cells share four vendors.
    for provider_name, model in sorted({
        pair
        for leg in legs
        for pair in (
            (leg.lead_provider, leg.lead_model),
            (leg.colead_provider, leg.colead_model),
        )
        if pair[0] and pair[1]
    }):
        problem = probe_chat_member(provider_name, model)
        if problem:
            problems.append(f"pinned {provider_name}/{model}: {problem}")

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


#: What a batch writes about itself at launch, so `--resume` and `--status` need nothing beyond
#: the directory. A resume that has to be handed the original `--legs` and `--k` again is a resume
#: that gets them wrong at 3am, and a twenty-leg grid is exactly where that costs a day.
BATCH_PLAN = "batch.json"
BATCH_PID = "batch.pid"
BATCH_LOG = "batch.log"


def _alive(pid: int) -> bool:
    """Whether a recorded batch pid is still running. A pid we may not signal is still alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def missing_indices(out_dir: Path, plan: dict) -> dict[str, list[int]]:
    """The (leg, trial) pairs a resume must run: never recorded, or recorded as an error.

    An ``error`` record is an infrastructure failure rather than a terminal state -- ``score.py``
    partitions trials by terminal state and ``error`` is not one of them -- so a resume re-runs it.
    A committed or honestly-failed trial IS a measurement and is never re-run, which is what keeps
    a resume from quietly changing the batch it repairs.

    Per leg, because a twenty-leg batch dies with different gaps in each and a single global
    ``--only`` list cannot express that: it would re-run trial 3 of every leg named, including the
    legs whose trial 3 already succeeded.
    """
    from benchmarks.author_bench.score import load_records

    try:
        records = load_records(Path(out_dir))
    except FileNotFoundError:
        records = []
    usable: dict[str, set[int]] = {}
    for record in records:
        if record.get("status") != "error":
            usable.setdefault(record.get("leg") or "", set()).add(record["trial"])
    out: dict[str, list[int]] = {}
    for slug, k in (plan.get("trials_per_leg") or {}).items():
        gaps = [i for i in range(1, int(k) + 1) if i not in usable.get(slug, set())]
        if gaps:
            out[slug] = gaps
    return out


def batch_status(out_dir: Path) -> str:
    """A batch's progress, read from what it has already written. Never touches the run.

    Reads the per-trial records rather than the report: the report is written once at the very
    end, so for the whole two days of a long batch the records are the only truth on disk.
    """
    from benchmarks.author_bench.score import load_records

    out_dir = Path(out_dir)
    plan_path = out_dir / BATCH_PLAN
    if not plan_path.exists():
        return f"{out_dir}: no {BATCH_PLAN}; not an author_bench batch directory"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    try:
        records = load_records(out_dir)
    except FileNotFoundError:
        records = []

    done: dict[str, list[dict]] = {}
    for record in records:
        done.setdefault(record.get("leg") or "", []).append(record)

    lines = [
        f"batch {out_dir.name}   base {plan.get('base_ref')}   started {plan.get('started')}"
    ]
    pid_path = out_dir / BATCH_PID
    pid = int(pid_path.read_text(encoding="utf-8").strip() or 0) if pid_path.exists() else 0
    if pid:
        state = "ALIVE" if _alive(pid) else "NOT RUNNING"
        lines.append(f"pid {pid}: {state}   log {out_dir / BATCH_LOG}")
    else:
        lines.append("no pid recorded (foreground run, or launched before --detach existed)")

    expected = plan.get("trials_per_leg") or {}
    total_expected = sum(int(v) for v in expected.values())
    total_done = sum(len(v) for v in done.values())
    cost = sum(r.get("cost_usd", 0.0) for r in records)
    lines += [
        f"trials {total_done}/{total_expected}   spend ${cost:.2f}",
        "",
        f"{'leg':42} {'done':>7}  {'terminal states':<32} {'US$':>8}",
    ]
    for slug in plan.get("legs", []):
        rows = done.get(slug, [])
        states: dict[str, int] = {}
        for record in rows:
            key = record.get("status") or "?"
            states[key] = states.get(key, 0) + 1
        summary = ", ".join(f"{k} {v}" for k, v in sorted(states.items())) or "-"
        leg_cost = sum(r.get("cost_usd", 0.0) for r in rows)
        lines.append(
            f"{slug:42} {len(rows):>3}/{int(expected.get(slug, 0)):<3}  {summary:<32} {leg_cost:>8.2f}"
        )

    gaps = missing_indices(out_dir, plan)
    if gaps:
        outstanding = sum(len(v) for v in gaps.values())
        lines += [
            "",
            f"{outstanding} trial(s) outstanding; continue with:",
            f"  python -m benchmarks.author_bench --resume {out_dir}",
        ]
    else:
        lines += ["", "every planned trial has a record"]
    return "\n".join(lines)


def _detach(args: argparse.Namespace, out_dir: Path) -> int:
    """Relaunch this same command orphaned, so a two-day batch outlives every session above it.

    Reuses ``graph.detached._spawn_orphaned`` rather than a local ``nohup``: that double fork
    exists because ``start_new_session`` alone leaves the parent->child ``/proc`` link a graceful
    VSCode or Jupyter teardown walks when it kills process trees, which killed a live detached run
    once already (2026-07-16). Reinventing it here would reintroduce exactly that bug.
    """
    from auto_research.graph.detached import _spawn_orphaned

    argv = [sys.executable, "-m", "benchmarks.author_bench"]
    if args.resume:
        argv += ["--resume", str(out_dir)]
    else:
        argv += ["--out", str(out_dir), "--base-ref", args.base_ref]
        if args.legs:
            argv += ["--legs", args.legs]
        if args.k is not None:
            argv += ["--k", str(args.k)]
    argv += ["--concurrency", str(args.concurrency), "--chatter", str(args.chatter)]
    if args.keep_clones:
        argv.append("--keep-clones")

    out_dir.mkdir(parents=True, exist_ok=True)
    pid = _spawn_orphaned(argv, out_dir / BATCH_LOG)
    (out_dir / BATCH_PID).write_text(f"{pid}\n", encoding="utf-8")
    print(f"detached: pid {pid}")
    print(f"  out    {out_dir}")
    print(f"  log    {out_dir / BATCH_LOG}")
    print(f"  status python -m benchmarks.author_bench --status {out_dir}")
    print(f"  resume python -m benchmarks.author_bench --resume {out_dir}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    toolkit_root = _toolkit_root()

    # A resume reads the batch's own recorded plan, so it continues exactly what was launched
    # rather than whatever flags happen to be typed the second time.
    resumed_plan: dict | None = None
    if args.resume:
        plan_path = Path(args.resume) / BATCH_PLAN
        if not plan_path.exists():
            print(f"PREFLIGHT: {args.resume} has no {BATCH_PLAN}; nothing to resume")
            return 2
        resumed_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        args.base_ref = resumed_plan.get("base_ref", args.base_ref)
        from benchmarks.author_bench.legs import BY_SLUG

        legs = [BY_SLUG[slug] for slug in resumed_plan["legs"] if slug in BY_SLUG]
    else:
        legs = resolve_legs(args.legs)

    problems = preflight(toolkit_root, args.base_ref, legs)
    if problems:
        for problem in problems:
            print(f"PREFLIGHT: {problem}")
        return 2

    if resumed_plan is not None:
        gaps = missing_indices(Path(args.resume), resumed_plan)
        plan = [(leg, gaps[leg.slug]) for leg in legs if gaps.get(leg.slug)]
        if not plan:
            print("every planned trial already has a record; nothing to resume")
            return 0
    else:
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

    out_dir = (
        Path(args.resume) if args.resume
        else (args.out or OUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    # Detaching AFTER preflight on purpose: a batch that would have failed its checks should say so
    # on the terminal that launched it, not bury the reason in a log nobody reads for two days.
    if args.detach:
        return _detach(args, out_dir)

    stamps = collect_stamps(toolkit_root, args.base_ref)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The batch records its own plan first, so --status and --resume work from the moment it
    # starts rather than only once something has finished.
    if resumed_plan is None:
        (out_dir / BATCH_PLAN).write_text(
            json.dumps(
                {
                    "legs": [leg.slug for leg, _ in plan],
                    "trials_per_leg": {leg.slug: len(idx) for leg, idx in plan},
                    "base_ref": args.base_ref,
                    "concurrency": args.concurrency,
                    "started": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    (out_dir / BATCH_PID).write_text(f"{os.getpid()}\n", encoding="utf-8")
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

    # Rendered from every record on disk, not from this process's own list: a resumed batch would
    # otherwise report only the trials it happened to re-run and silently lose the first sitting.
    from benchmarks.author_bench.score import load_records

    report = build_report(load_records(out_dir), stamps)
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
    if args.status:
        print(batch_status(args.status))
        return 0
    if args.rescore:
        return rescore(args.rescore)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
