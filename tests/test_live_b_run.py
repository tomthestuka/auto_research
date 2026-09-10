"""The **B-run**: one live Q21 run that clears every module built-but-never-live-exercised.

Activate with::

    AUTO_RESEARCH_LIVE_B_RUN=1 pytest tests/test_live_b_run.py -s

Question is **#21** (`bhxrb_intraobs_qu_lag_crosscorr`), assembled verbatim from the PI research
database exactly as `notebooks/usage_example.ipynb` does — same source of record, so this drives the
real prescription rather than a paraphrase. #21 is the right vehicle rather than a throwaway: it
reaches authoring quickly (it needs DCF capability `xspec_utils` lacks) and it is the question whose
history already produced the **partial-chain commit** that [A11] exists to survive, which is the one
condition B5 cannot fake.

**What this clears** (checklist in `tmp_remaining_livetest.md`):

===  ==================================================================================
B1   [I5] cost accounting on live rows — nonzero cache_read/cache_write, reasoning tokens
B2   Durable resume Stage 1 — kill the backend, relaunch with resume=True, continue not restart
B3   Durable resume Stage 2 — kill **mid-authoring**, resume, dirty `ai` branch self-heals
B5   [A11] authoring chain recovery — ≥1 tool commits, a later one fails, workers rerun anyway
F1   Discovery-block trim — no dangling `Tool reference` 400 on a tool-less request
D2   Model-facing effect of the manifest reshape — one-line brief → schema fetch → correct call
E1   [E8] Telegram chunked plain-reply resolution
B4   HITL slash-commands (`/help`, `/status`, `/steer`, `/stop`) — **needs a human at the phone**
===  ==================================================================================

**Design — the kills are automated, not hand-timed.** B2/B3 hinge on killing the backend at a
precise moment (mid-analysis, then mid-authoring). Asking a human to watch a log and hit Ctrl-C is
both unreliable and unrepeatable, so :func:`_await_event` tails the run's ``monitor.jsonl`` for the
marker event and :func:`_kill_backend_tree` then kills the **whole process tree** — the sandbox REPLs
share the backend's process group and a bare ``kill <pid>`` orphans them (§12). Only B4 genuinely
needs a human, so it is gated separately behind ``AUTO_RESEARCH_B_RUN_HITL=1``.

Everything B1/B5/F1/D2 needs is already written to disk by the run, so those are **post-hoc
assertions over the artifacts** — all in the run's ``monitor.jsonl``, which since 2026-07-29 carries
the cost record too (``cost.call`` events + an accumulated total on every line) — in
:func:`test_b_run_evidence`, which can be re-run against a finished run without spending anything.

**Preconditions** (all fail loudly here rather than halfway through a paid run): a clean toolkit
branch, a drift-free data catalog, and ``human_channel='telegram'`` reachable.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

LIVE = os.getenv("AUTO_RESEARCH_LIVE_B_RUN") == "1"
HITL = os.getenv("AUTO_RESEARCH_B_RUN_HITL") == "1"
_OVERWRITE = os.getenv("AUTO_RESEARCH_B_RUN_OVERWRITE") == "1"
skip_live = pytest.mark.skipif(not LIVE, reason="Set AUTO_RESEARCH_LIVE_B_RUN=1 to run")

RESEARCH_DB = "/data/fzhou/ai-team/analysis_pipeline_mcp/ixpe_research_database.json"
PROJECT_ID = "bhxrb_intraobs_qu_lag_crosscorr"
LOG_DIR = Path(__file__).resolve().parents[1] / "logs" / "dev" / "P21-qu-lag-ccf" / "b-run"

# An EXPLICIT run_id, deliberately not the derived one. `resolve_run_id` hashes the request, and
# this request is byte-identical to the notebook's #21 cell — so the default id collides with the
# existing Q21 run (`8c75990e…`, status `done`, 2026-07-21) and a launch with `overwrite=True`
# would delete that run's artifacts. Naming the run keeps the B-run's directory its own, still
# deterministic, and still re-attachable across the drill legs.
RUN_ID = os.getenv("AUTO_RESEARCH_B_RUN_ID", "b-run-q21")

# How long to wait for each marker before giving up. Analysis reaches its first worker turn in
# minutes; authoring can trail a long reduction, hence the wider window.
ANALYSIS_TIMEOUT_S = 60 * 45
AUTHORING_TIMEOUT_S = 60 * 90
NL = chr(10)


# ── request (verbatim from the research DB, as the notebook builds it) ──────────


def _qu_lag_request():
    from auto_research.schemas import ResearchRequest

    entry = next(
        e for e in json.load(open(RESEARCH_DB)) if e["project_id"] == PROJECT_ID
    )
    targets = NL.join(
        "- " + t["source_name"] + ": " + ", ".join(t["obs_ids"])
        for t in entry["targets_and_datasets"]
    )
    literature = NL.join(
        "- " + r["authors"] + " — " + r["title"] + " (" + r["url"] + ")"
        for r in entry["literature"]
    )
    return ResearchRequest(
        question=NL.join([
            entry["title"] + " (#21 — " + entry["project_id"] + ").",
            "",
            entry["description"],
            "",
            'Targets and datasets (IXPE, mission = "ixpe"):',
            targets,
            "",
            "Analysis prescription:",
            entry["analysis_prescription"],
            "",
            "Relevant literature:",
            literature,
            "",
        ]),
        context=(
            '- All targets are IXPE observations (mission = "ixpe"); data is local-first across '
            "the full archival BH-XRB sample.\n"
            "- This is a frontier, model-independent census: NO published Q-U intra-observation "
            "time-lag reference exists, so there is nothing to match. Report the real measured DCF "
            "peak lags, amplitudes and Monte-Carlo significances exactly as produced. A null result "
            "(no significant lag), or per-observation upper limits, is the expected, correct, and "
            "publishable outcome for most or all observations — do NOT tune the null, hand-roll the "
            "correlation, or back-fill a detection.\n"
            "- Slice widths are signal-driven (target per-slice MDP99), not blind exposure division; "
            "observations that cannot supply enough signal-dominated slices are flagged and excluded "
            "from the population tests, never forced.\n"
            "- An honest failed outcome (missing data, or a capability that cannot be "
            "made available) is an acceptable, correct result — never fabricate a lag, a "
            "significance, or a correlation. If the environment is broken, abort and report.\n"
        ),
    )


# ── monitor-log helpers ─────────────────────────────────────────────────────────


def _events(jsonl: Path) -> list[dict]:
    """Every event recorded so far; tolerant of a torn final line (the run is still writing)."""
    if not jsonl.exists():
        return []
    out = []
    for line in jsonl.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # partially-flushed tail
    return out


def _event_names(jsonl: Path) -> list[str]:
    return [e.get("event", "") for e in _events(jsonl)]


def _await_event(jsonl: Path, predicate, timeout_s: int, label: str) -> dict:
    """Block until an event satisfies ``predicate``, or fail with what we did see.

    Reports the events observed on timeout — a bare "timed out" on a run this expensive leaves
    you with no idea whether it stalled early or nearly got there.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for event in _events(jsonl):
            if predicate(event):
                return event
        time.sleep(5)
    seen = _event_names(jsonl)
    raise AssertionError(
        f"timed out after {timeout_s}s waiting for {label}. "
        f"{len(seen)} events seen; last 15: {seen[-15:]}"
    )


def _kill_backend_tree(run_directory: Path) -> int:
    """SIGKILL the backend's whole process group and confirm it is gone.

    The group, not the pid: sandbox REPL subprocesses share the backend's process group (they are
    spawned without ``start_new_session``), so killing the pid alone orphans live XSPEC children
    that keep writing into the obs-db (§12, the interim hard-kill contract). This deliberately
    skips the in-process ``finally``, which is the point — it is what leaves the `ai` branch dirty
    for B3 to heal.
    """
    from auto_research.graph.detached import _read_backend, is_run_alive

    record = _read_backend(run_directory)
    assert record, f"no backend.pid under {run_directory}"
    pid = int(record["pid"])
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    for _ in range(30):
        if not is_run_alive(run_directory):
            return pid
        time.sleep(1)
    raise AssertionError(f"backend pid {pid} still alive after SIGKILL")


def _toolkit_dirty() -> bool:
    from auto_research.env import toolkit_root

    return bool(
        subprocess.run(
            ["git", "-C", str(toolkit_root()), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout.strip()
    )


# ── preconditions ───────────────────────────────────────────────────────────────


@skip_live
def test_b_run_preconditions() -> None:
    """Every no-LLM precondition, before a paid run starts.

    `run_preflight` already covers the checklist (clean toolkit branch, catalog health, channel
    ping, checkpointer); running it here surfaces a blocker in seconds instead of after the
    launch has begun spending.
    """
    import asyncio

    from auto_research.preflight import run_preflight

    asyncio.run(run_preflight())
    assert not _toolkit_dirty(), (
        "toolkit tree is dirty before launch — commit or clean it, otherwise B3 cannot "
        "distinguish the dirt this test creates from pre-existing edits"
    )


# ── the run: launch → kill mid-analysis → resume → kill mid-authoring → resume ──


@skip_live
def test_b_run_drills() -> None:
    """Drive the whole B-run, performing both kills at their marker events.

    Sequential and stateful by design: each leg depends on the previous one's surviving run
    directory, so this is one test rather than four that could be run out of order.
    """
    import asyncio

    from auto_research.graph.detached import run_status
    from auto_research.sandbox.workspace import run_dir

    request = _qu_lag_request()
    directory = run_dir(RUN_ID)
    jsonl = directory / "monitor.jsonl"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n[B-run] run_id={RUN_ID}\n[B-run] monitor={jsonl}")

    # A finished run already sitting here means a previous B-run; overwriting silently would
    # discard its evidence, so require the intent explicitly.
    if directory.exists() and run_status(directory) == "done" and not _OVERWRITE:
        pytest.skip(
            f"a completed B-run already occupies {directory}. Re-run with "
            "AUTO_RESEARCH_B_RUN_OVERWRITE=1 to replace it, or set AUTO_RESEARCH_B_RUN_ID to a "
            "fresh id. (test_b_run_evidence works against the existing one as-is.)"
        )

    async def launch(**kwargs):
        from auto_research.graph.detached import run_research_nohup

        return await run_research_nohup(
            request, chatter=7, human_channel="telegram", run_id=RUN_ID, **kwargs
        )

    # ── B2: kill mid-analysis, then resume ──────────────────────────────────────
    # Launched in a thread: run_research_nohup polls to completion, but we need to intervene
    # while it is still running. The backend is an orphaned child, so it survives this thread.
    import threading

    threading.Thread(target=lambda: asyncio.run(launch(overwrite=True)), daemon=True).start()

    _await_event(
        jsonl,
        lambda e: e.get("event", "").startswith("analyst") or "analysis" in e.get("event", ""),
        ANALYSIS_TIMEOUT_S,
        "analysis to start (B2 kill point)",
    )
    killed = _kill_backend_tree(directory)
    print(f"[B-run] B2: killed backend tree pid={killed} mid-analysis")
    assert run_status(directory) == "dead"

    before = len(_events(jsonl))
    asyncio.run(launch(resume=True))  # blocks; may finish the run outright
    resumed = [e for e in _events(jsonl)[before:] if e.get("event") == "run.resume"]
    assert resumed, "B2 FAILED: no run.resume event after resume=True"
    print(f"[B-run] B2 ✓ resumed: {resumed[0]}")

    # ── B3: the mid-authoring kill needs authoring to still be ahead of us ───────
    names = _event_names(jsonl)
    if "coder.session_start" not in names:
        pytest.skip(
            "B2 verified, but the run finished or never reached authoring — B3/B5 need an "
            "authoring attempt. Re-run against a question that provokes one."
        )
    print("[B-run] B3/B5 evidence present; assertions in test_b_run_evidence")


@skip_live
def test_b_run_midauthoring_kill_heals() -> None:
    """B3: kill **mid-authoring** so the `ai` branch is left dirty, then resume and self-heal.

    Separate from the B2 leg because it needs a *fresh* authoring attempt to interrupt: run it
    while the run above is mid-chain, or against a re-launch that authors again.
    """
    import asyncio

    from auto_research.graph.detached import run_research_nohup
    from auto_research.sandbox.workspace import run_dir

    request = _qu_lag_request()
    directory = run_dir(RUN_ID)
    jsonl = directory / "monitor.jsonl"

    _await_event(
        jsonl,
        lambda e: e.get("event") == "coder.session_start",
        AUTHORING_TIMEOUT_S,
        "coder.session_start (B3 kill point)",
    )
    _kill_backend_tree(directory)
    print("[B-run] B3: killed backend tree mid-authoring")

    # The hard kill skips the in-process reset, so the authoring branch should be left dirty —
    # precisely the footgun Stage 2 exists to clear.
    assert _toolkit_dirty(), (
        "expected a dirty toolkit tree after a mid-authoring kill; without it this leg proves "
        "nothing about the heal"
    )

    before = len(_events(jsonl))
    asyncio.run(run_research_nohup(
        request, chatter=7, human_channel="telegram", run_id=RUN_ID, resume=True
    ))
    new = _events(jsonl)[before:]

    assert any(e.get("event") == "run.resume.heal" for e in new), (
        "B3 FAILED: no run.resume.heal event — the dirty branch was not self-healed"
    )
    patches = list((directory / "authoring").glob("resume_heal_*.patch"))
    assert patches, "B3 FAILED: heal saved no resume_heal_*.patch (the discarded work is lost)"
    assert not _toolkit_dirty(), "B3 FAILED: tree still dirty after heal"
    print(f"[B-run] B3 ✓ healed; patch saved: {patches[0].name}")


# ── post-hoc evidence (re-runnable, spends nothing) ─────────────────────────────


@skip_live
def test_b_run_evidence() -> None:
    """B1 / B5 / F1 / D2 over the artifacts the run already wrote.

    Split out so it can be re-run after the fact — the assertions read `monitor.jsonl` and the
    cost ledger, never the providers.
    """
    from auto_research.sandbox.workspace import run_dir

    directory = run_dir(RUN_ID)
    jsonl = directory / "monitor.jsonl"
    events = _events(jsonl)
    names = [e.get("event", "") for e in events]
    assert events, f"no monitor events under {jsonl} — run the drills first"

    report = {"run_dir": str(directory), "events": len(events)}
    # Deferred failures: every check runs and is recorded, then we assert once at the end. A
    # bare `assert` per check would abort at the first miss and hide the rest — unacceptable
    # when the evidence took an expensive live run to produce and one leg can fail for reasons
    # unrelated to the others.
    failures: list[str] = []

    # ── B1: cost accounting actually populated on live rows ─────────────────────
    # The static half was verified in July; what had never happened is a *run* writing rows with
    # nonzero cache tokens. Zero here means the accounting is still unexercised, not that the
    # run was cheap.
    #
    # Attribution is exact, not a time window: cost now lives in THIS run's monitor JSONL as
    # `cost.call` events (2026-07-29), so there is no cumulative cross-run file to slice by
    # timestamp and no risk of catching a concurrent run's rows.
    fresh = [e.get("data") or {} for e in events if e.get("event") == "cost.call"]
    cache_read = sum(r.get("cache_read_tokens", 0) or 0 for r in fresh)
    cache_write = sum(r.get("cache_write_tokens", 0) or 0 for r in fresh)
    reasoning = sum(r.get("reasoning_tokens", 0) or 0 for r in fresh)
    coder_usd = sum(
        r.get("total_cost_usd", 0) or 0 for r in fresh if r.get("price_source") == "claude_sdk"
    )
    report["B1"] = {
        "rows": len(fresh), "cache_read": cache_read,
        "cache_write": cache_write, "reasoning": reasoning,
        "cost_usd": round(sum(r.get("total_cost_usd", 0) or 0 for r in fresh), 2),
        "coder_sdk_usd": round(coder_usd, 2),
        # Every event carries the running total, so the last line's stamp is the run's spend.
        "accumulated_usd": round(
            max((e.get("cost_usd_total") or 0.0) for e in events), 2
        ),
    }
    if not fresh:
        failures.append("B1: no cost.call events in this run's monitor ledger")
    elif cache_read == 0:
        failures.append(
            f"B1: {len(fresh)} rows, all with cache_read_tokens=0 — the [I5] cache accounting is "
            "still unexercised (exactly the pre-existing symptom this check exists to clear)"
        )

    # ── B5 / [A11]: a partial chain recovered rather than choked ────────────────
    committed = [e for e in events if e.get("event") == "coder.committed"]
    if "analyst.tool.chain_resume" in names:
        report["B5"] = {"chain_resume": True, "commits": len(committed)}
        if not committed:
            failures.append("B5: chain_resume fired with no commit recorded — contradictory")
    else:
        report["B5"] = {
            "chain_resume": False, "commits": len(committed),
            "note": "no partial-chain failure occurred; [A11] not exercised this run",
        }

    # ── F1: no discovery block replayed onto a request that declares no tools ───
    # The 2026-07-29 run-killer: a terminal structured compose sends a schema and no tools, and
    # replaying a discovery block there made Anthropic reject the whole request. The absence of
    # that 400 anywhere in the ledger IS the test.
    dangling = [e for e in events if "Tool reference" in json.dumps(e)]
    report["F1"] = {
        "dangling_reference_events": len(dangling),
        "note": "no 400 'Tool reference not found' means the discovery-block trim held",
    }
    if dangling:
        failures.append(
            "F1: a dangling tool reference reached a provider — the discovery-block trim failed"
        )

    # ── D2: the reshape read correctly by a live model ──────────────────────────
    # A too-terse brief shows up as tool calls with wrong/missing arguments, which surface as
    # failed tool results. Reported rather than asserted: some tool errors are legitimate science.
    # A failure is recorded as `is_error` on a `*.tool_result` event — NOT as an event whose name
    # contains "error" — so counting by name scored 0 while the 2026-07-29 run had four real
    # failures. Count the results themselves, and name the tools that failed so the arguments can
    # be inspected rather than merely tallied.
    failed_results = [
        e for e in events
        if e.get("event", "").endswith(".tool_result") and (e.get("data") or {}).get("is_error")
    ]
    report["D2"] = {
        "failed_tool_results": len(failed_results),
        "tools": sorted({(e.get("data") or {}).get("tool", "?") for e in failed_results}),
        "note": "inspect these for wrong/missing arguments — the signature of a too-terse brief",
    }

    report["failures"] = failures
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / f"b_run_evidence_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n[B-run] evidence → {out}\n{json.dumps(report, indent=2)}")

    # The report is written first, so a failure here still leaves the harvested evidence on disk.
    assert not failures, "B-run evidence gaps:\n  - " + "\n  - ".join(failures)


@pytest.mark.skipif(not (LIVE and HITL), reason="Set AUTO_RESEARCH_B_RUN_HITL=1 with a human present")
def test_b_run_hitl_commands() -> None:
    """B4 + E1: slash-commands over Telegram. **A human must drive this from the phone.**

    Cannot be automated — it is verifying that a human's message is intercepted as a command
    rather than fed to an LLM as a reply. Prints the script, then checks the log for evidence.
    Send `/stop` **last**: it winds the run down.
    """
    from auto_research.sandbox.workspace import run_dir

    directory = run_dir(RUN_ID)
    jsonl = directory / "monitor.jsonl"

    print(
        "\n[B-run B4] From Telegram, send these to the bot, in order:\n"
        "  1. /help              → command list + live team/role/model roster\n"
        "  2. /status            → a status_monitor digest of this run\n"
        "  3. /steer analyst_lead <a short instruction>\n"
        "  4. (E1) a LONG multi-paragraph plain reply to any pending question — checks chunked\n"
        "     plain-reply resolution and that a heading is not orphaned from its body\n"
        "  5. /stop              → LAST: honest run-wide wind-down\n"
    )
    _await_event(
        jsonl,
        lambda e: "command" in e.get("event", "") or "/stop" in json.dumps(e),
        60 * 30,
        "a slash-command to be recorded (send /help from Telegram)",
    )
    names = _event_names(jsonl)
    print(f"[B-run B4] command-ish events: {[n for n in names if 'command' in n]}")
