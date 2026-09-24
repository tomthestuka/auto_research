"""Detached (nohup-style) research runs — survive a killed kernel, reconnect by re-running.

`run_research_nohup` spawns the real pipeline (`python -m auto_research`) as a **detached**
child in its own session, then tails its on-disk ledger and reconstructs a `ResearchRun`.
A killed kernel (e.g. logout) does not kill the detached child; re-running the same call
reconnects to the surviving/finished run via a **deterministic `run_id`** (`sha256` of the
request). `run_research` itself is unchanged — everything here is additive.

On-disk contract under `run_dir(run_id)` (`<runtime_dir>/runs/<run_id>/`):
- `backend.pid`  — `{pid, create_time}` the launcher writes right after spawn (liveness).
- `monitor.jsonl`— the run's monitor ledger (the pipeline streams it).
- `result.json`  — the CLI writes it when the pipeline returns (terminal marker + payload).
- `backend.log`  — the child's stdout/stderr.

Liveness is **process existence** (pid + start-time, PID-reuse-safe), never log freshness —
a long silent reduction must not be misjudged dead. Output is always reconstructed from the
JSONL (+ `result.json` when done); a dead run raises `RunDied` with the partial, never a
silent relaunch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from auto_research.monitor import MonitorEvent, RunMonitor, write_stream_event
from auto_research.run_control import UserInterruptionError
from auto_research.sandbox.workspace import run_dir
from auto_research.schemas import ResearchRequest
from auto_research.schemas.report import ResearchReport
from auto_research.schemas.review import ReviewerReport
from auto_research.graph.runtime import ResearchRun

_DEFAULT_POLL_S = 5.0
_PID_FILE = "backend.pid"
_RESULT_FILE = "result.json"
_MONITOR_JSONL = "monitor.jsonl"
_BACKEND_LOG = "backend.log"


def _record(run_directory: Path, name: str) -> Path:
    """Where one of this run's RECORD files lives (`workspace.ledger_dir`, or the legacy path).

    `run_dir(run_id)` ends in the run id, so the directory names its own run and no extra
    argument has to be threaded through the module. Opening for append through the same
    resolver is deliberate: a run that started under the old layout keeps appending where its
    earlier lines already are.
    """
    from auto_research.sandbox.workspace import record_beside

    return record_beside(run_directory, name)
_RUN_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class RunDied(RuntimeError):
    """A detached run ended without a usable result — its process died mid-run, or the
    pipeline raised. Carries the ``run_id``, a ``reason``, the ``log_path``, and the
    reconstructed ``partial`` monitor markdown so the caller can see how far it got. Honest
    surfacing, never a silent relaunch (§9.6)."""

    def __init__(self, run_id: str, reason: str, *, log_path: Path | None = None, partial: str = ""):
        self.run_id = run_id
        self.reason = reason
        self.log_path = log_path
        self.partial = partial
        suffix = f" (log: {log_path})" if log_path else ""
        super().__init__(f"detached run {run_id} died: {reason}{suffix}")


def _normalize_request(request: ResearchRequest | str) -> ResearchRequest:
    return ResearchRequest(question=request) if isinstance(request, str) else request


def resolve_run_id(request: ResearchRequest | str, run_id: str = "auto") -> str:
    """``'auto'`` → ``sha256`` of the normalized request (deterministic, so re-running the
    same call reconnects); any other string → a path-safe named run (validated — it becomes
    a directory name)."""
    if run_id == "auto":
        key = _normalize_request(request).model_dump_json()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"run_id must be 'auto' or path-safe [A-Za-z0-9_-]+, got {run_id!r}")
    return run_id


def _proc_create_time(pid: int) -> int | None:
    """Linux ``/proc/<pid>/stat`` start-time (clock ticks since boot), or ``None`` if the
    process is gone **or a zombie**. A zombie is dead for every purpose here — a backend
    that died at preflight lingers as one until the spawning kernel reaps it, and it must
    not block ``overwrite=True`` or read as alive. Paired with the recorded value the
    start-time makes liveness PID-reuse-safe."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        # comm (field 2) is parenthesized and may contain spaces/parens — split on the LAST
        # ')'. In the remainder rhs[0] is field 3 (state), so field 22 (starttime) is rhs[19].
        rhs = data.rsplit(")", 1)[1].split()
        if rhs[0] == "Z":
            return None
        return int(rhs[19])
    except (IndexError, ValueError):
        return None


def _read_backend(run_directory: Path) -> dict | None:
    try:
        return json.loads((run_directory / _PID_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_run_alive(run_directory: Path) -> bool:
    """True iff the recorded backend pid exists **and** its start-time matches (PID-reuse-safe).
    Process existence, not log freshness — a long, silent reduction stays correctly alive."""
    record = _read_backend(run_directory)
    if not record:
        return False
    create_time = _proc_create_time(int(record["pid"]))
    return create_time is not None and create_time == record.get("create_time")


def run_status(run_directory: Path) -> str:
    """One of ``'done'`` (result.json present), ``'alive'``, ``'dead'``, ``'absent'``.

    ``result.json`` is checked first: the CLI writes it just before exit, so a finished run
    reads ``'done'`` even though its process is now gone."""
    if (run_directory / _RESULT_FILE).exists():
        return "done"
    if (run_directory / _PID_FILE).exists():
        return "alive" if is_run_alive(run_directory) else "dead"
    return "absent"


def jsonable(value: Any) -> Any:
    """`json.dumps(default=...)` helper: Pydantic models → JSON dict, Paths → str."""
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _partial(run_directory: Path) -> tuple[str, Path | None]:
    """Reconstruct the monitor markdown so far (for a RunDied), plus the backend log path.

    The end-of-run digest leads it: a run that was hard-killed never recorded a ``run.summary``
    event, so it is derived here from the JSONL — a died run still reports what it spent and how
    far it got, on the same block a finished run ends with."""
    jsonl = _record(run_directory, _MONITOR_JSONL)
    log = _record(run_directory, _BACKEND_LOG)
    log_path = log if log.exists() else None
    if jsonl.exists():
        try:
            from auto_research.monitor.summary import render_run_summary, summarize_jsonl

            digest = f"```\n{render_run_summary(summarize_jsonl(jsonl))}```\n\n"
            return digest + RunMonitor.from_jsonl(jsonl).render_markdown(grouped=True), log_path
        except Exception:  # noqa: BLE001 — a partial/corrupt ledger must not mask the death
            pass
    return "", log_path


def reconstruct_run(request: ResearchRequest | str, run_directory: Path, run_id: str) -> ResearchRun:
    """Rebuild a `ResearchRun` from ``result.json`` + the monitor JSONL. Raises `RunDied`
    if the result records a pipeline failure (status != 'done' or no verdict), or
    `UserInterruptionError` if a human stopped it."""
    result = json.loads((run_directory / _RESULT_FILE).read_text(encoding="utf-8"))
    if result.get("status") == "interrupted":
        # A human stop is not a death: the backend cleaned up and exited on purpose, so the
        # detached caller sees the same exception an in-process run raises rather than `RunDied`.
        raise UserInterruptionError(
            result.get("error") or "the run was interrupted",
            artifacts={"run_dir": str(run_directory),
                       "ledger": str(_record(run_directory, _MONITOR_JSONL))},
        )
    if result.get("status") != "done" or not result.get("verdict"):
        partial, log = _partial(run_directory)
        raise RunDied(run_id, result.get("error") or "pipeline reported failure",
                      log_path=log, partial=partial)
    jsonl = _record(run_directory, _MONITOR_JSONL)
    monitor = RunMonitor.from_jsonl(jsonl) if jsonl.exists() else RunMonitor(chatter=10)
    report_jsonl = result.get("report_jsonl")
    research_report = None
    if report_jsonl and Path(report_jsonl).exists():
        research_report = ResearchReport.model_validate_json(
            Path(report_jsonl).read_text(encoding="utf-8")
        )
    return ResearchRun(
        request=_normalize_request(request),
        verdict=ReviewerReport.model_validate(result["verdict"]),
        final_state=result.get("final_state") or {},
        monitor=monitor,
        research_report=research_report,
        report_jsonl=Path(report_jsonl) if report_jsonl else None,
        report_latex=Path(result["report_latex"]) if result.get("report_latex") else None,
        report_pdf=Path(result["report_pdf"]) if result.get("report_pdf") else None,
    )


# Runs in a short-lived intermediary interpreter: spawn the backend in its own session,
# report its pid on stdout, exit. The intermediary's death orphans the backend at once.
_ORPHAN_SPAWNER = """\
import json, os, subprocess, sys
argv = json.loads(sys.argv[1])
env = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
with open(sys.argv[2], "a", encoding="utf-8") as log:
    proc = subprocess.Popen(
        argv, stdout=log, stderr=log, start_new_session=True,
        env=(dict(os.environ, **env) if env else None),
    )
print(proc.pid, flush=True)
"""
# Opened for APPEND, not write: a resumed run re-spawns the backend over the same `backend.log`,
# and truncating it erased the dead attempt's output — the traceback a `RunDied` points the human
# at, and the only record of why the run needed resuming. A genuine fresh start goes through
# `overwrite=True`, which removes the run dir first.


def _spawn_orphaned(
    argv: list[str], log_path: Path, env: dict[str, str] | None = None
) -> int:
    """Spawn ``argv`` detached **and orphaned** (double-fork) and return its pid.

    ``start_new_session`` alone shields the backend from signals and from the kernel
    dying, but leaves the parent→child ``/proc`` link intact — and a graceful
    VSCode/Jupyter teardown kills process TREES by walking exactly that link (observed
    2026-07-16: a logout killed a live detached run seconds before reconnect, while
    unrelated user processes survived). A short-lived intermediary starts the backend
    and exits immediately, reparenting it to init: no ancestry, nothing to walk.

    ``env`` **adds to** the caller's environment rather than replacing it, so a caller can
    hand the backend one variable — which is how a process is pointed at a configuration
    other than the repository's — without having to reconstruct a working environment for
    a science stack it does not know the shape of. The default is unchanged: inherit."""
    argv_json = json.dumps(argv)
    command = [sys.executable, "-c", _ORPHAN_SPAWNER, argv_json, str(log_path)]
    if env:
        command.append(json.dumps(env))
    helper = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=True,
    )
    return int(helper.stdout.strip())


def _default_launcher(
    request: ResearchRequest | str,
    run_id: str,
    run_directory: Path,
    *,
    chatter: int,
    report_formats: list[str] | None,
    human_channel: str | None,
    max_iterations: int | None,
    resume: bool = False,
) -> int:
    """Spawn ``python -m auto_research`` detached and orphaned (``_spawn_orphaned``, so
    neither a killed kernel nor a logout's tree-walking cleanup reaches it), record
    ``backend.pid``, and return the child pid. ``resume=True`` adds ``--resume`` so the
    backend continues its ``run_id`` from the latest checkpoint."""
    req = _normalize_request(request)
    run_directory.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, "-m", "auto_research", req.question,
        "--run-id", run_id, "--chatter", str(chatter),
        "--human", human_channel or "none",
    ]
    if req.context:
        argv += ["--context", req.context]
    for constraint in (req.constraints or []):
        argv += ["--constraint", constraint]
    for fmt in (report_formats or []):
        argv += ["--report-format", fmt]
    if max_iterations is not None:
        argv += ["--max-iterations", str(max_iterations)]
    if resume:
        argv += ["--resume"]
    backend_log = _record(run_directory, _BACKEND_LOG)
    pid = _spawn_orphaned(argv, backend_log)
    (run_directory / _PID_FILE).write_text(
        json.dumps({"pid": pid, "create_time": _proc_create_time(pid)}),
        encoding="utf-8",
    )
    return pid


# Sentinel so `stream` resolves to sys.stdout at CALL time — ipykernel rebinds sys.stdout per
# cell, so capturing it as a def-time default would write to the wrong (or a dead) cell.
_STREAM_DEFAULT = object()


def _drain_jsonl(path: Path, offset: int, chatter: int, stream: Any) -> int:
    """Render new complete JSONL lines since byte ``offset`` to ``stream``, chatter-filtered
    (``level <= chatter``, matching the live monitor), and return the advanced offset.

    The detached backend streams *every* event to ``monitor.jsonl`` regardless of chatter, so
    tailing that file reproduces the same live output the in-process monitor gave. A partial
    trailing line (no newline yet) is left unconsumed for the next drain."""
    try:
        raw = path.read_bytes()
    except OSError:
        return offset
    if len(raw) <= offset:
        return offset
    chunk = raw[offset:]
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return offset  # only a partial line so far
    consumed = chunk[: last_nl + 1]
    for bline in consumed.split(b"\n"):
        if not bline.strip():
            continue
        try:
            event = MonitorEvent.model_validate_json(bline.decode("utf-8"))
        except Exception:  # noqa: BLE001 — skip a corrupt/partial line, never break the follow
            continue
        if event.level <= chatter:
            write_stream_event(stream, event)
    return offset + len(consumed)


async def run_research_nohup(
    request: ResearchRequest | str,
    *,
    chatter: int = 10,
    report_formats: str | list[str] | None = None,
    human_channel: str | None = None,
    max_iterations: int | None = None,
    run_id: str = "auto",
    overwrite: bool = False,
    resume: bool = False,
    poll_s: float = _DEFAULT_POLL_S,
    stream: Any = _STREAM_DEFAULT,
    _launcher: Callable[..., int] = _default_launcher,
) -> ResearchRun:
    """Run the pipeline in a detached backend that survives a killed kernel; reconnect by
    re-running the same call.

    ``run_id='auto'`` keys on the request (re-run to reconnect); a named `run_id` forces a
    distinct run. ``overwrite=True`` erases an existing run and starts fresh — but **refuses
    if that run is still alive** (stop it first). ``human_channel='cli'`` is rejected (a
    detached child has no stdin). Returns a reconstructed `ResearchRun`; raises `RunDied`
    if the run's process died without writing a result.

    ``resume=True`` recovers a *backend that died* (host reboot / OOM / kill): instead of
    raising `RunDied`, it re-launches the backend under ``--resume`` so it continues its
    ``run_id`` from the latest checkpoint (§12). It also re-launches a run whose backend
    crashed with a ``failed`` marker (a stale ``result.json`` is cleared first). ``resume``
    and ``overwrite`` are mutually exclusive (overwrite wipes the checkpoint).

    While waiting, live progress is streamed to ``stream`` (default: the current cell's
    ``sys.stdout``; pass ``None`` to silence) by following the run's ``monitor.jsonl`` —
    chatter-filtered, identical to a non-detached live run. So detaching does not cost the
    live view. On reconnect the JSONL is replayed from the start, then followed.
    """
    if stream is _STREAM_DEFAULT:
        stream = sys.stdout
    if human_channel == "cli":
        raise ValueError(
            "run_research_nohup runs detached (no stdin), so human_channel='cli' cannot "
            "work — use 'telegram'/'slack'/'none'."
        )
    if overwrite and resume:
        raise ValueError("overwrite=True and resume=True are mutually exclusive: overwrite "
                         "wipes the run dir (and its checkpoint), leaving nothing to resume.")
    formats = [report_formats] if isinstance(report_formats, str) else report_formats
    rid = resolve_run_id(request, run_id)
    rdir = run_dir(rid)

    if overwrite:
        if is_run_alive(rdir):
            record = _read_backend(rdir) or {}
            raise RuntimeError(
                f"refusing to overwrite run {rid}: it is still alive (pid "
                f"{record.get('pid')}). Stop it first (e.g. kill {record.get('pid')}) or "
                f"use a different run_id."
            )
        shutil.rmtree(rdir, ignore_errors=True)

    jsonl = rdir / _MONITOR_JSONL
    offset = 0

    def _pump() -> None:
        nonlocal offset
        if stream is not None:
            offset = _drain_jsonl(jsonl, offset, chatter, stream)

    def _relaunch_resume() -> None:
        """Re-launch a dead/failed backend under --resume to continue from its checkpoint;
        refuse if the run is somehow still alive."""
        if is_run_alive(rdir):
            record = _read_backend(rdir) or {}
            raise RuntimeError(
                f"refusing to resume run {rid}: it is still alive (pid {record.get('pid')})."
            )
        (rdir / _RESULT_FILE).unlink(missing_ok=True)  # clear a stale terminal marker
        _launcher(request, rid, rdir, chatter=chatter, report_formats=formats,
                  human_channel=human_channel, max_iterations=max_iterations, resume=True)

    status = run_status(rdir)
    if status == "done":
        _pump()
        try:
            return reconstruct_run(request, rdir, rid)
        except RunDied:
            if not resume:
                raise
            _relaunch_resume()  # a crashed run (failed marker) + resume → continue from checkpoint
    elif status == "dead":
        _pump()
        if not resume:
            partial, log = _partial(rdir)
            raise RunDied(rid, "process is gone and no result was written",
                          log_path=log, partial=partial)
        _relaunch_resume()
    elif status == "absent":
        _launcher(request, rid, rdir, chatter=chatter, report_formats=formats,
                  human_channel=human_channel, max_iterations=max_iterations, resume=resume)

    # Alive (a prior launch still running) or just launched → tail to a terminal state,
    # streaming new monitor events as they land. Follow faster than the status poll when
    # streaming, so progress feels live rather than arriving in `poll_s`-sized batches.
    tick = min(poll_s, 1.0) if stream is not None else poll_s
    while True:
        _pump()
        await asyncio.sleep(tick)
        status = run_status(rdir)
        if status == "done":
            _pump()
            return reconstruct_run(request, rdir, rid)
        if status == "dead":
            _pump()
            partial, log = _partial(rdir)
            raise RunDied(rid, "process died mid-run before writing a result",
                          log_path=log, partial=partial)


__all__ = ["run_research_nohup", "RunDied", "resolve_run_id", "run_status", "is_run_alive"]
