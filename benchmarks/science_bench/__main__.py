"""CLI: run the science sweep's trials, or check that they could run.

    python -m benchmarks.science_bench --check              # every no-model check, no spend
    python -m benchmarks.science_bench --block0             # the leak probe: one run, recorded
    python -m benchmarks.science_bench --slots 2-6          # the rest of the demonstration
    python -m benchmarks.science_bench --status DIR         # progress and liveness, costs nothing
    python -m benchmarks.science_bench --score  DIR         # tables and figures, no spend

The default slot set is the **demonstration**: two seat assignments, three trials each. The
11-slot screening array is kept under ``--block1`` as the record of what was designed.

Every trial is a whole research run with authoring on: three chat roles, an author subprocess, a
gate and a tool review, over hours. The report states what it cost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from benchmarks.science_bench import arms, environment as env_mod, report as report_mod
from benchmarks.science_bench import request as request_mod, score as score_mod, trial
from benchmarks.science_bench.arms import BLOCK0, BLOCK1, DEMO, RunSlot, demo_tree_slug

OUT_ROOT = trial.REPO_ROOT / "logs" / "dev" / "science-bench"

#: Where private trial worlds are built. Two properties matter and both are checked rather than
#: trusted: it sits outside the filesystem allow-list a trial is given, so the one file naming its
#: whole arm is unreadable by any actor; and its *name* says nothing about the experiment.
#:
#: The second cost a run. Block 0's first launch used a workspace called ``science_bench``, and
#: five minutes in the librarian's verbatim prompt carried
#: ``/data/.../science_bench/<hash>/papers/...`` in every downloaded-paper path -- a model that
#: knows it is being benchmarked behaves differently whatever its seat. The name here is
#: deliberately empty of meaning, and :func:`arms.forbidden_in_path` is what actually enforces it.
DEFAULT_WORKSPACE = Path("/data/fzhou/ar_runtime/ws")

#: A trial's own copy of the data is about 4 GB and its runtime directory grows with reduction
#: products. Checked before a batch starts, because running out of disk twelve hours in loses
#: everything in flight.
GIGABYTES_PER_TRIAL = 12


# ── Preflight ───────────────────────────────────────────────────────────────


def _provider_key(provider: str) -> tuple[str, ...]:
    from auto_research.env import provider_key_aliases

    return provider_key_aliases(provider)


def preflight(slots: list[RunSlot], *, workspace: Path) -> list[str]:
    """Every check decidable without a model call. Returns the problems found, empty if good.

    The gate benchmark lost 26 runs to a missing key discovered one failed call at a time, and a
    trial here is hours and tens of dollars rather than one call. Everything that can be known
    before spending is checked before spending.
    """
    from auto_research.config_paths import code_gate_yaml, config_dir, observations_yaml
    from auto_research.config_paths import orchestration_yaml

    problems: list[str] = []

    # Secrets live in the project's own dotenv, not in the shell, which is how every production
    # entry point finds them. Loading it here rather than reading the raw environment is the
    # difference between checking what a trial will see and checking what this terminal happens
    # to export.
    try:
        from dotenv import load_dotenv

        from auto_research.env import _ENV_FILE

        load_dotenv(_ENV_FILE)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"could not load the project dotenv: {type(exc).__name__}: {exc}")

    if os.environ.get("AUTO_RESEARCH_CONFIG_DIR"):
        problems.append(
            "AUTO_RESEARCH_CONFIG_DIR is set in this shell. The batch reads the live "
            "configuration as its source and hands each trial its own; running it already "
            "pointed elsewhere would clone the wrong toolkit and read the wrong archive."
        )
    for path in (orchestration_yaml(), observations_yaml(), code_gate_yaml()):
        if not path.exists():
            problems.append(f"missing configuration: {path}")
    if problems:
        return problems  # nothing below can be decided without it

    source = yaml.safe_load(orchestration_yaml().read_text(encoding="utf-8")) or {}
    toolkit_source = Path(str(source.get("toolkit_root", ""))).expanduser()
    obs_source = Path(
        str((yaml.safe_load(observations_yaml().read_text(encoding="utf-8")) or {}).get("obs_db_root", ""))
    ).expanduser()

    # ── the question and its key ────────────────────────────────────────────
    try:
        posed = request_mod.load_request()
        problems += [] if posed.question.strip() else ["the frozen question is empty"]
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the frozen question does not load: {type(exc).__name__}: {exc}")

    # The scoring key must sit outside every root an actor can read. Not secrecy -- this question's
    # answer is published and the tree is *supposed* to read the paper -- but tamper hygiene: a
    # scored artifact an actor could reach is not evidence about that actor. `runtime_dir`,
    # `papers_dir` and `toolkit_root` are the lead's and reviewer's read roots, and the whole
    # filesystem allow-list is what the OS jail mounts, so the key is checked against all of them
    # rather than against the three by name.
    try:
        from benchmarks.science_bench import extract as extract_mod

        key_path = Path(extract_mod.ANSWER_KEY).resolve()
        if not key_path.is_file():
            problems.append(f"the frozen answer key is missing: {key_path}")
        readable = [
            str(source.get(name))
            for name in ("runtime_dir", "papers_dir", "toolkit_root")
            if source.get(name)
        ] + [str(root) for root in (source.get("agent_fs_allow") or [])]
        for root in readable:
            try:
                resolved_root = Path(root).expanduser().resolve()
            except OSError:
                continue
            if key_path.is_relative_to(resolved_root):
                problems.append(
                    f"the frozen answer key {key_path} sits inside {resolved_root}, which an "
                    f"actor can read; move it outside every allow-listed and read root"
                )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"the answer key does not load: {type(exc).__name__}: {exc}")

    # ── the toolkit the trees author from ───────────────────────────────────
    if not (toolkit_source / ".git").is_dir():
        problems.append(f"toolkit_root {toolkit_source} is not a git checkout")
    else:
        import subprocess

        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"{trial.BASE_REF}^{{commit}}"],
            cwd=str(toolkit_source), capture_output=True, text=True,
        )
        if resolved.returncode != 0:
            problems.append(
                f"the pinned base {trial.BASE_REF} does not resolve in {toolkit_source}: every "
                f"tree authors from the state before this question's own tools existed"
            )

    # ── the data the question is posed on ───────────────────────────────────
    for obsid in env_mod.QUESTION_OBSIDS:
        events = obs_source / env_mod.QUESTION_MISSION / obsid / "event_l2"
        if not events.is_dir():
            problems.append(f"observation {obsid} has no event_l2 under {obs_source}")
            continue
        raw = [p for p in events.iterdir() if env_mod.RAW_EVENT_LIST.match(p.name)]
        if not raw:
            problems.append(
                f"observation {obsid} has no archive event lists matching the expected naming; "
                f"the copy filter would take nothing and the trial would fail as science"
            )

    # ── the keys every seat in this batch needs ─────────────────────────────
    wanted: dict[str, set[str]] = {}
    for slot in slots:
        for seat in slot.arm.seats():
            wanted.setdefault(seat.provider, set()).add(seat.model)
    for provider, models in sorted(wanted.items()):
        aliases = _provider_key(provider)
        if aliases and not any(os.environ.get(name) for name in aliases):
            problems.append(
                f"no API key for {provider} (set one of {', '.join(aliases)}); "
                f"needed by {', '.join(sorted(models))}"
            )

    # ── where trial worlds are built ────────────────────────────────────────
    workspace = Path(workspace)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".writable").write_text("", encoding="utf-8")
        (workspace / ".writable").unlink()
    except OSError as exc:
        problems.append(f"workspace {workspace} is not writable: {exc}")
    else:
        free_gb = shutil.disk_usage(workspace).free / 1e9
        needed = GIGABYTES_PER_TRIAL * max(1, len(slots))
        if free_gb < needed:
            problems.append(
                f"{free_gb:.0f} GB free at {workspace}, and {len(slots)} trials want about "
                f"{needed} GB"
            )

    # ── the leak guard, checked rather than assumed ─────────────────────────
    leaks = arms.forbidden_in_path(workspace.resolve())
    if leaks:
        problems.append(
            f"the workspace path {workspace} carries {', '.join(leaks)}; an actor reads its roots "
            f"in every tool description and shell command, and a model that knows it is being "
            f"benchmarked behaves differently whatever its seat"
        )

    allow = [
        str(Path(str(root)).expanduser().resolve()) for root in (source.get("agent_fs_allow") or [])
    ]
    resolved_workspace = str(workspace.resolve())
    for root in allow:
        if resolved_workspace == root or resolved_workspace.startswith(root.rstrip("/") + "/"):
            problems.append(
                f"the workspace {workspace} sits inside the allow-listed root {root}, so a trial "
                f"could read its own configuration -- the one file naming its whole arm"
            )

    # ── the author's harness pairing ────────────────────────────────────────
    if not any(_provider_key(arms.CODER.provider)):
        problems.append(f"no key variable is known for the author's vendor {arms.CODER.provider}")
    elif not os.environ.get(arms.CODER_KEY_ENV):
        problems.append(f"{arms.CODER_KEY_ENV} is unset; the author cannot authenticate")

    # The pipeline's own key check, over the live configuration. It covers the roles this sweep
    # does not vary -- the librarian and the status reporter, which a trial inherits verbatim --
    # and those are as capable of failing a launch as any seat on an axis.
    try:
        from auto_research.env import check_required_env

        check_required_env()
    except Exception as exc:  # noqa: BLE001
        problems.append(str(exc).replace("\n", " "))

    if config_dir() != config_dir():  # pragma: no cover - defensive, never true
        problems.append("the configuration directory is not stable within one process")
    return problems


# ── Scheduling ──────────────────────────────────────────────────────────────


async def run_batch(slots: list[RunSlot], cfg: trial.TrialConfig, *, concurrency: int) -> list[dict]:
    """Run the slots, never exceeding a vendor's ceiling or the global limit.

    The ceilings are the roster's, and a single global number cannot express them: every run calls
    the pinned author and the pinned tool reviewer, and six of block 1's eleven slots carry the one
    vendor with a measured ceiling, so "launch ten" saturates it three times over.
    """
    pending = list(slots)
    running: dict[asyncio.Task, RunSlot] = {}
    records: list[dict] = []

    while pending or running:
        started_any = False
        for slot in list(pending):
            if len(running) >= concurrency:
                break
            if not arms.admissible([s.arm for s in running.values()], slot.arm):
                continue
            pending.remove(slot)
            running[asyncio.ensure_future(trial.run_trial(slot, cfg))] = slot
            started_any = True
            print(f"  started slot {slot.index:2d}  {slot.arm.slug}  run {slot.run_id[:12]}",
                  flush=True)
        if not running:
            if not started_any and pending:  # pragma: no cover - the roster forbids it
                raise RuntimeError(f"no pending slot is admissible: {[s.index for s in pending]}")
            break
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            slot = running.pop(task)
            record = task.result()
            records.append(record)
            trial.append_record(cfg.out_dir, record)
            print(
                f"  finished slot {slot.index:2d}  {record.get('infrastructure')}  "
                f"accepted={record.get('measured', {}).get('accepted')}  "
                f"${record.get('cost_usd', 0):.2f}  {record.get('duration_s', 0) / 60:.0f} min",
                flush=True,
            )
    return records


# ── The leak scan ───────────────────────────────────────────────────────────


def scan_prompts(path: Path) -> tuple[list[str], dict[str, int]]:
    """Read a verbatim prompt dump and report every leak in it, with what it covers.

    Machine-checked rather than eyeballed, because the leak is never in the field you were
    thinking about: it was the campaign's directory name inside a downloaded-paper path, four
    levels down in a tool result. A regex over the whole corpus finds that; reading the payload
    you designed does not.

    The coverage tally matters as much as the findings. A clean scan over a dump holding only the
    librarian says nothing about the lead, the reviewer or the workers, so the caller has to see
    which actors actually spoke before reading silence as safety. The author is not here at all --
    it is a harness subprocess whose prompts never pass this chokepoint -- so its own paths are
    read from the run's ledger instead.

    **It scans paths, not prose, and that distinction is what keeps it usable.** The first version
    ran the word list over whole records and reported forty-nine leaks, of which one was real: the
    others were an astronomy abstract saying *probe*, a fit described as an *anchor*, and the
    librarian's own model name. A guard that cries wolf is one somebody switches off, and it would
    have been switched off over the run that mattered. So the word list is applied to the
    path-shaped substrings a record contains -- which is where the real leak lived, four levels
    inside a downloaded-paper path -- and separately, as an exact match, to the handful of strings
    that cannot occur in scientific prose at all.
    """
    findings: list[str] = []
    coverage: Counter[str] = Counter()
    if not Path(path).exists():
        return [f"no prompt dump at {path}"], {}

    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        coverage[f"{record.get('role')}/{record.get('model')}"] += 1
        # A seat naming its own model is not a leak: it knows what it is, and the arm is the
        # combination of seats, not any one of them.
        own = {str(record.get("model", "")).lower(), str(record.get("provider", ""))}
        own |= set(str(record.get("model", "")).lower().replace("-", " ").split())
        for leak in sorted(_leaks_in(_scannable_text(record))):
            if leak not in own:
                findings.append(f"line {number} ({record.get('role')}): {leak}")
    return findings, dict(coverage)


#: Strings that cannot occur in scientific prose, so they are worth hunting anywhere in a record
#: rather than only in a path.
_UNMISTAKABLE = ("science_bench", "sciencebench", "block0", "block1", "block2")

#: Path-shaped substrings. The channel the real leak used: a read-only tool provider renders its
#: roots into the tool descriptions a model reads, and every shell command prints its directory.
#:
#: **At least two segments.** A single `/word` is not a path, and treating one as a path reported
#: 186 leaks over one run -- every `coder/deepseek_v4_flash` role label on a cost line, which is
#: the ledger's own bookkeeping and reaches no actor. The second slash is what distinguishes a
#: filesystem path from a label that happens to contain one.
_PATHLIKE = re.compile(r"/[\w.+-]+/[\w./+-]{2,}")


#: Fields whose values are opaque provider bookkeeping, never prose an actor reads: an Anthropic
#: extended-thinking ``signature``, the captured raw response, and provider/tool ids. Skipped
#: rather than scanned. Message CONTENT is never under these keys, so an id that leaks through a
#: brief (the 2026-08-27 gate_bench `ToolSpec.id` leak travelled inside content text) is still seen.
_OPAQUE_KEYS = frozenset({"signature", "raw", "id", "tool_call_id", "thought_signature"})

#: A long unbroken run of base64 alphabet. Anthropic thinking signatures are exactly this, they
#: contain ``/``, and so the path regex read them as filesystem paths -- a scan of a real dump
#: (2026-09-03) reported eleven leaks of which ten were the token ``w3`` occurring by chance inside
#: encrypted reasoning blobs, while the one real leak came from the ledger half. A guard that cries
#: wolf is one somebody switches off.
_BASE64ISH = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")


def _scannable_text(record: object) -> str:
    """The prose of one dumped record: everything an actor could read, opaque blobs removed."""
    parts: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                if name not in _OPAQUE_KEYS:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and not _BASE64ISH.match(node):
            parts.append(node)

    walk(record)
    return "\n".join(parts)


def _leaks_in(text: str) -> set[str]:
    """Everything in one record's readable text an actor must not be able to read. The prompt
    dump passes ``_scannable_text(record)``; the ledger half passes the raw line."""
    found = {word for word in _UNMISTAKABLE if word in text.lower()}
    for candidate in _PATHLIKE.findall(text):
        found.update(arms.forbidden_in_path(candidate))
    return found


def scan_ledger_paths(path: Path) -> list[str]:
    """The author's half of the audit: what the coder typed, read from the run's own ledger.

    The prompt chokepoint cannot see it -- the author is a subprocess speaking its harness's own
    protocol -- so its exposure is the paths in its commands and their results.
    """
    findings: list[str] = []
    if not Path(path).exists():
        return [f"no ledger at {path}"]
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if '"coder' not in line:
            continue
        try:
            event = json.loads(line).get("event", "")
        except json.JSONDecodeError:
            continue
        # Cost bookkeeping names every role and its member by design and reaches no actor; it is
        # the harness's own accounting, not anything the coder was shown.
        if event in _BOOKKEEPING_EVENTS:
            continue
        for leak in sorted(_leaks_in(line)):
            findings.append(f"ledger line {number}: {leak}")
    return findings


#: Ledger events that are the harness's own accounting rather than anything an actor saw.
_BOOKKEEPING_EVENTS = frozenset({"cost.call", "provider.key_rotation", "run.summary"})


#: Files inside the run directory that name every seat at once.
#:
#: ``backend.log`` prefixes its lines with ``role(model)`` for every role, and the ledger records a
#: ``role/member`` on each cost line -- so either one, read whole, hands an actor the entire arm.
#: They sit inside the run directory, which production makes a read root for the lead and the
#: reviewer, and that is deliberate there: a role investigating its own run is a feature.
#:
#: For a blinded trial it is a channel, and the honest handling is to **measure** it rather than
#: close it: closing it would change the pipeline being measured, on the strength of a behaviour no
#: run has yet been observed to exhibit. So a trial in which nothing touched them is clean, and a
#: trial in which something did is a contaminated trial we know about rather than one we do not.
SEAT_NAMING_ARTIFACTS: tuple[str, ...] = ("backend.log", "monitor.jsonl", "result.json")


def scan_actor_reads(path: Path) -> list[str]:
    """Whether any actor read a run artifact that names every seat.

    Looks for those filenames inside the *arguments* an actor passed to a read tool, not merely
    anywhere in the ledger -- the ledger is full of its own path, and a scan that cannot tell a
    record from a read would report every line.
    """
    findings: list[str] = []
    if not Path(path).exists():
        return [f"no ledger at {path}"]
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if ".tool_call" not in line and "tool_use" not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        arguments = json.dumps((row.get("data") or {}).get("args")
                               or (row.get("data") or {}).get("arguments")
                               or (row.get("data") or {}).get("command") or "")
        for artifact in SEAT_NAMING_ARTIFACTS:
            if artifact in arguments:
                findings.append(
                    f"ledger line {number}: {row.get('actor')} read {artifact}, which names "
                    f"every seat"
                )
    return findings


# ── Status ──────────────────────────────────────────────────────────────────


def batch_status(directory: Path) -> str:
    """A batch's progress, read from its records. Costs nothing and never touches the run."""
    records = trial.load_records(Path(directory))
    if not records:
        return f"{directory}: no records yet"
    lines = [f"{directory}: {len(records)} trial(s) recorded"]
    for record in sorted(records, key=lambda r: r.get("slot", 0)):
        measured = record.get("measured") or {}
        lines.append(
            f"  slot {record.get('slot'):>2}  {record.get('infrastructure'):<12} "
            f"accepted={str(measured.get('accepted')):<5} "
            f"tools={record.get('tools_committed_n', 0)} "
            f"${record.get('cost_usd', 0):.2f}  {record.get('duration_s', 0) / 60:.0f} min"
        )
    lines.append(f"  total ${sum(r.get('cost_usd', 0) for r in records):.2f}")
    return "\n".join(lines)


# ── Entry ───────────────────────────────────────────────────────────────────


def parse_slots(spec: str | None, pool: Sequence[RunSlot] | None = None) -> list[RunSlot]:
    """``None`` = every slot of ``pool``; ``'1'``/``'2-11'``/``'1,4,7'`` = exactly those.

    ``pool`` defaults to the demonstration, which is what is actually run: block 1's array is kept
    as the record of what was designed and is selectable with ``--block1``.
    """
    slots = list(pool if pool is not None else DEMO)
    if not spec:
        return slots
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            low, high = part.split("-", 1)
            wanted.update(range(int(low), int(high) + 1))
        elif part:
            wanted.add(int(part))
    chosen = [slot for slot in slots if slot.index in wanted]
    unknown = wanted - {slot.index for slot in chosen}
    if unknown:
        raise SystemExit(f"no such slot(s): {sorted(unknown)}; this set is 1-{len(slots)}")
    return chosen


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="benchmarks.science_bench", description=__doc__)
    parser.add_argument("--block0", action="store_true",
                        help="run the leak probe alone: block 1's first slot, with verbatim "
                             "recording of every actor's prompt. Read its transcript for the arm "
                             "before batching anything else; if it is clean it IS slot 1")
    parser.add_argument("--slots", default=None,
                        help="which slots of the selected set to run (e.g. 2-6); default all")
    parser.add_argument("--block1", action="store_true",
                        help="select the superseded 11-slot screening array instead of the "
                             "demonstration. Kept as the record of what was designed; running it "
                             "is a deliberate act, not the default")
    parser.add_argument("--score", metavar="DIR", type=Path, default=None,
                        help="score a finished batch from its records and reports, and write the "
                             "report, tables and figures. No model, no spend")
    parser.add_argument("--concurrency", type=int, default=3,
                        help="trials in flight at once, under the roster's vendor ceilings")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="where private trial worlds are built (outside the allow-list)")
    parser.add_argument("--out", type=Path, default=None,
                        help="records directory (default logs/dev/science-bench/<UTC>)")
    parser.add_argument("--chatter", type=int, default=7, help="per-trial ledger chatter")
    parser.add_argument("--check", action="store_true", help="preflight only, no spend")
    parser.add_argument("--status", metavar="DIR", type=Path, default=None,
                        help="print a batch's progress; costs nothing")
    parser.add_argument("--scan", metavar="DIR", type=Path, default=None,
                        help="read block 0's verbatim prompt dump and its ledger and report every "
                             "leak, with which actors the dump actually covers. Costs nothing")
    parser.add_argument("--sweep-artifacts", action="store_true",
                        help="also delete each trial's runtime directory when it ends. Off by "
                             "default: it holds the ledger, figures and report the whole scoring "
                             "pass reads")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.status is not None:
        print(batch_status(args.status))
        return 0

    if args.scan is not None:
        dump = Path(args.scan) / "slot-01" / "prompts.jsonl"
        findings, coverage = scan_prompts(dump)
        print(f"prompt dump: {dump}")
        for actor, count in sorted(coverage.items()):
            print(f"  covered  {actor:<44} {count:>5} call(s)")
        record = next((r for r in trial.load_records(Path(args.scan)) if r.get("slot") == 1), {})
        if record.get("run_dir"):
            ledger = Path(record["run_dir"]) / "monitor.jsonl"
            findings += scan_ledger_paths(ledger)
            findings += scan_actor_reads(ledger)
        for finding in findings:
            print(f"  LEAK  {finding}")
        print(f"  {len(findings)} leak(s)" if findings else "  clean")
        return 1 if findings else 0

    if args.score is not None:
        records = trial.load_records(Path(args.score))
        if not records:
            print(f"no records under {args.score}")
            return 1
        for record in records:
            slot = next((s for s in DEMO if s.run_id == record.get("run_id")), None)
            if slot is not None:
                record["tree"] = demo_tree_slug(slot)
        scores = score_mod.score_batch(records)
        score_mod.write_scores(Path(args.score), scores)
        path = report_mod.write_report(Path(args.score), scores, {})
        print(report_mod.build_report(scores, {}))
        print(f"\n  wrote {path} and its two figures")
        return 0

    pool = BLOCK1 if args.block1 else DEMO
    slots = [BLOCK0] if args.block0 else parse_slots(args.slots, pool)

    print(f"science_bench: {len(slots)} slot(s)")
    for slot in slots:
        print(f"  slot {slot.index:2d}  {slot.arm.slug}"
              f"{'  [verbatim recording]' if slot.verbatim_recording else ''}")

    problems = preflight(slots, workspace=args.workspace)
    for problem in problems:
        print(f"  FAIL  {problem}")
    if problems:
        return 1
    print("  preflight: all checks pass")
    if args.check:
        return 0

    out_dir = args.out or (OUT_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    out_dir.mkdir(parents=True, exist_ok=True)

    source = yaml.safe_load(
        (Path(__import__("auto_research.config_paths", fromlist=["x"]).orchestration_yaml())).read_text()
    )
    stamps = trial.collect_stamps(Path(source["toolkit_root"]).expanduser())
    (out_dir / "batch.json").write_text(
        json.dumps({"slots": [s.index for s in slots], "stamps": stamps,
                    "workspace": str(args.workspace), "concurrency": args.concurrency}, indent=2),
        encoding="utf-8",
    )
    print(f"  out: {out_dir}\n  stamps: {json.dumps(stamps, indent=2)}", flush=True)

    cfg = trial.TrialConfig(
        workspace_root=args.workspace, out_dir=out_dir, stamps=stamps,
        chatter=args.chatter, keep_artifacts=not args.sweep_artifacts,
    )
    records = asyncio.run(run_batch(slots, cfg, concurrency=args.concurrency))
    print("\n" + batch_status(out_dir))
    return 0 if all(r.get("infrastructure") in ("done", "interrupted") for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
