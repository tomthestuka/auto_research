"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auto_research.config_paths import orchestration_yaml
from auto_research.preflight import run_preflight
from auto_research.hitl import HumanHandler, resolve_handler
from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.builder import IOAgentLike, ReviewerLike, _record, build_graph
from auto_research.graph.checkpoint import checkpoint_path, open_checkpointer
from auto_research.run_control import RunControl, UserInterruptionError
from auto_research.monitor import RunMonitor
from auto_research.models import resolve
from auto_research.observability import install_cost_meter
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend
from auto_research.sandbox.mcp_client import MCPToolClient
from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.provenance import ReceiptStore
from auto_research.agents.roles import _model_label
from auto_research.sandbox.workspace import (
    agent_dir,
    create_run_workspace,
    ledger_dir,
    memory_path,
    run_dir,
)
from auto_research.schemas import ResearchRequest, ReviewerReport
from auto_research.storage import Storage


DEFAULT_WORKER_COUNT = 2


@dataclass(frozen=True)
class ResearchRun:
    """Result object returned by the monitored library entry."""

    request: ResearchRequest
    verdict: ReviewerReport
    final_state: dict[str, Any]
    monitor: RunMonitor
    research_report: Any = None  # ResearchReport | None — avoids circular import at class def
    report_jsonl: Path | None = None
    report_latex: Path | None = None
    report_pdf: Path | None = None
    report_html: Path | None = None

    def markdown(self) -> str:
        """Return the grouped monitor report."""

        return self.monitor.render_markdown(grouped=True)

    def summary(self) -> str:
        """Return the end-of-run summary block (cost, wall clock, tools authored, reports).

        Renders the ``run.summary`` event the run recorded, so a reconnected/replayed run reports
        exactly what the run itself computed; falls back to deriving it from the events at hand.
        """

        from auto_research.monitor.summary import (
            build_run_summary,
            render_run_summary,
            summary_from_events,
        )

        events = self.monitor.events
        stored = summary_from_events(events)
        return render_run_summary(stored if stored is not None else build_run_summary(events))


def load_orchestration_config(path: str | Path | None = None) -> dict:
    """Load iteration caps from the configured ``orchestration.yaml``.

    ``path=None`` asks :mod:`auto_research.config_paths`, which answers with the repository's own
    configuration unless this process was launched against another one. Resolved per call rather
    than captured as a default argument, since a default argument is evaluated at import and would
    pin the answer before a caller could set it.
    """
    path = Path(path) if path is not None else orchestration_yaml()
    if not path.exists():
        raise RuntimeError(
            f"Config not found: {path}. Run `bash install.sh` from the repo root, or copy "
            f"{path.name}.example to {path.name} and edit it."
        )
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _env_spelling(environment: Any, analyze_env: Path | None, analyze_ini_sh: str | None):
    """``(prefix, init script)`` for the shell server, from the analysis environment object when
    there is one (env plan B1: every reader builds from it), else the configured keys."""
    if environment is None:
        return analyze_env, analyze_ini_sh
    return environment.prefix, (str(environment.activate) if environment.activate else analyze_ini_sh)


def _builtin_server_config(
    analyze_env: Path | None,
    analyze_ini_sh: str | None,
    toolkit_root: Path,
    cwd: Path | None = None,
    analyze_probe: str | None = None,
    writable_dirs: Sequence[Path] | None = None,
) -> dict:
    """Build the default builtin-MCP-server config dict.

    ``writable_dirs`` are the folders the shell may write (default: ``cwd``). The run path passes
    each worker's own folder, so the shell cannot rename or replace a folder another process binds
    (a worker folder swapped for a symlink would be bound writable by the next REPL start).

    When ``analyze_env`` is set, the server subprocess is spawned from that
    env's Python interpreter so ``bash_exec`` commands can import astropy and
    xspec_utils.  The subprocess environment is populated from the analyst init
    script (HEADAS, CALDB, LD_LIBRARY_PATH) plus PYTHONPATH=toolkit_root so the
    research_toolkit packages are importable without the REPL.

    **The server runs inside the OS jail** when ``agent_os_jail`` is on, with the same allow-list
    the REPL gets (F20: it used to be launched unwrapped, so ``bash_exec`` reached the whole disk
    while the command-string guard stood down as "superseded by the jail"). Its source is passed
    with ``-c``, as the REPL's is, so the jail never needs to mount this repository to run it.
    """
    from auto_research.sandbox.builtin_server import __file__ as _bs_file
    from auto_research.sandbox.jail import analysis_policy, jail_home, private_tmp, wrap
    from auto_research.sandbox.runner import _source_ini_env, with_private_param_dir

    builtin_source = Path(_bs_file).read_text(encoding="utf-8")
    if analyze_env:
        python = str(analyze_env / "bin" / "python")
    else:
        import sys
        python = sys.executable

    # Build env from the analyst ini script — same pattern as PersistentSandbox.
    # os.environ is NOT inherited: `_source_ini_env` sources the script in a clean
    # environment, so no credential, conda activation or launching-shell state rides
    # along.  analyze_ini_sh is the authoritative source; it captures the HEASoft env
    # (headas-init.sh sets PATH with HEASoft's Python first, LD_LIBRARY_PATH,
    # etc.).  PYTHONPATH covers toolkit_root (import xspec_utils) and its parent
    # (from research_toolkit import xspec_utils).
    if not analyze_ini_sh:
        raise ValueError("analyze_ini_sh must be configured — no env fallback")
    base_env = _source_ini_env(analyze_ini_sh)
    existing = base_env.get("PYTHONPATH", "")
    pythonpath = str(toolkit_root) + os.pathsep + str(toolkit_root.parent)
    if existing:
        pythonpath += os.pathsep + existing
    env = {**base_env, "PYTHONPATH": pythonpath}

    # bash_exec runs a bare `python` through the shell, which resolves against PATH — not
    # the server's own interpreter. Pin {analyze_env}/bin to the FRONT of PATH so that a
    # bare `python`/`python3`/`pip` inside bash_exec is the SAME interpreter the toolkit
    # tools invoke directly ({analyze_env}/bin/python). Without this, bash_exec's
    # `import astropy` / `import xspec_utils` resolves to whatever conda env happened to
    # leak onto the launching shell's PATH (the analyst's astropy env is never on it),
    # so inline inspection breaks the moment the orchestrator env lacks the science stack.
    if analyze_env:
        env["PATH"] = str(analyze_env / "bin") + os.pathsep + env.get("PATH", "")

    # The configured stack smoke-test (config: analyze_probe) supplies the concrete probe
    # example in the bash_exec description the worker reads — no domain string in source.
    if analyze_probe:
        env["AR_ANALYZE_PROBE"] = analyze_probe

    # A user site-packages under the (persistent) jail home would run code at the next start.
    env["PYTHONNOUSERSITE"] = "1"

    # The analysis policy, as for the REPL: allow-list with the toolkit read-only, a private /tmp,
    # the run folder writable (one server serves every worker of the run) but its `authoring/`
    # read-only — a resume trusts the base ref recorded there — and network on.
    scratch = Path(cwd) if cwd is not None else jail_home("shell")
    authoring = scratch / "authoring"
    authoring.mkdir(parents=True, exist_ok=True)  # an overlay binds only an existing directory
    writable = [Path(d) for d in writable_dirs] if writable_dirs is not None else [scratch]
    for folder in writable:
        folder.mkdir(parents=True, exist_ok=True)
    tmp = private_tmp("shell", scratch)
    # Environments the pipeline built, read-only, so `use_env` can point the shell at one; bound
    # even when empty, so one built later in the run is visible too.
    from auto_research.env import agent_conda_dir

    built = [agent_conda_dir() / "envs", agent_conda_dir() / "activation"]
    for folder in built:
        folder.mkdir(parents=True, exist_ok=True)
    argv = wrap(
        # -P: no cwd on sys.path, so a module planted in the run folder cannot hijack the server.
        # (3.11+ only; the analysis interpreter is 3.13. The REPL uses SAFE_PATH_PRELUDE instead.)
        [python, "-P", "-c", builtin_source],
        analysis_policy(
            label="shell", toolkit_root=toolkit_root,
            writable=tuple(str(folder) for folder in writable),
            readonly=(str(authoring), *(str(folder) for folder in built)),
            tmp=tmp, home=private_tmp("shell-home", scratch),  # this run's own, outside its folder
        ),
    )

    # Its own HEASoft parameter folder (F23): /tmp is private when jailed.
    env = with_private_param_dir(
        env, "/tmp/pfiles" if argv[0] != python else tmp / "pfiles", create=tmp / "pfiles",
    )
    # In-tree `__pycache__` is never read (git ignores it, so nothing judges it): bytecode goes to
    # the server's private folder, never the shared host /tmp.
    env["PYTHONPYCACHEPREFIX"] = "/tmp/ar-pycache" if argv[0] != python else str(tmp / "ar-pycache")

    return {
        "name": "builtin",
        "command": argv[0],
        "args": argv[1:],
        # What the launch actually produced, not what config asked for: `wrap` leaves the argv
        # unchanged when the jail is off or bwrap is absent, and the shell guard reads this flag.
        "jailed": argv[0] != python,
        # Removed by `MCPToolClient.shutdown`, so the server's private /tmp dies with it.
        "private_tmp": str(tmp),
        "env": env,
        # Pin the server process cwd under the runtime data dir so nothing it does
        # relative to its own cwd can touch either repo (per-call bash_exec still
        # gets its own injected worker cwd; this is the by-construction backstop).
        **({"cwd": str(cwd)} if cwd is not None else {}),
    }


# ── Role wiring: one definition, used by the run path and by anything that probes it ──────
#
# These exist because a caller that assembles a role by hand assembles a *different* role. The
# worker is the sharp case: `AnalystWorkerAgent.analyze` runs its inner ReAct tool loop only when a
# sandbox and a registry are attached and otherwise falls back to a plain structured call, so a
# hand-built worker looks like a worker, answers like a worker, and never calls a tool. A probe
# built that way cannot tell a model that will not drive a loop from a model that was never given
# one. Same shape for the reviewer and the lead, whose read-only providers differ from each other
# by three flags and from a hand-rolled version by five arguments.
#
# `run_research` calls these, so a test that calls them is exercising the production wiring rather
# than a copy of it that drifts.


def readonly_tools_for(
    role: str,
    *,
    roots: Sequence[Path],
    retrieval: RetrievalBackend | None = None,
    web_source: Any = None,
) -> ReadOnlyToolProvider:
    """The read-only inspection loop production gives ``role``.

    ``role`` is ``"reviewer"`` or ``"analyst_lead"``, and the two differ deliberately. The lead
    gets the data catalog and the budget-free design channel and is denied the paper knowledge
    base, because it investigates toolkit source and the archive rather than literature; the
    reviewer is the paper surface. Getting those flags wrong does not fail — it silently measures a
    role the pipeline does not have.
    """
    if role not in ("reviewer", "analyst_lead"):
        raise ValueError(f"no read-only tool loop is defined for role {role!r}")
    lead = role == "analyst_lead"
    return ReadOnlyToolProvider(
        roots=list(roots),
        retrieval=retrieval,
        web_source=web_source,
        include_data_catalog=lead,
        include_text_search=not lead,
        # The reviewer reads papers through its own receipted `read_paper` (the Docling text its
        # quotes are checked against), so the native-PDF `open_paper` is not declared for it.
        include_open_paper=False,
    )


def attach_channel(
    seats: Sequence[Any],
    *,
    run_id: str,
    run_control: RunControl,
    human_handler: HumanHandler | None = None,
    monitor: RunMonitor | None = None,
) -> Any:
    """Put ``seats`` on one quick channel (E12) and return its ``SeatRouter``.

    Every seat passed can text every other and, when ``human_handler`` is wired, the human. Only
    the graph seats are passed: the co-lead and the status monitor are never registered, so no
    seat can reach them and they receive nothing. A seat needs its ``agent_key`` first (its
    address); one without it is skipped.
    """
    from auto_research.hitl.router import SeatRouter

    orch = load_orchestration_config()
    router = SeatRouter(
        run_control, human_handler, monitor=monitor,
        mirror=bool(orch.get("channel_mirror", True)),
        store=ReceiptStore.for_run(run_dir(run_id)),
    )
    for seat in seats:
        key = getattr(seat, "agent_key", None)
        if not key:
            continue
        try:
            seat.channel = router
            if getattr(seat, "run_control", None) is None:
                seat.run_control = run_control
            router.register(key, seat)
        except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
            pass
    return router


def readonly_roots_for(run_id: str, toolkit_root: Path) -> list[Path]:
    """The three roots the reviewer and the lead may read: the run, the papers, the toolkit."""
    from auto_research.sources.downloads import papers_dir

    return [run_dir(run_id), papers_dir(), toolkit_root]


@dataclass(frozen=True)
class SandboxSettings:
    """Where a worker's REPL comes from: the analysis environment and its boot scripts.

    One definition, because these are read from configuration rather than chosen: a caller that
    omits them does not get a slightly different sandbox, it gets a sandbox that refuses to start
    (``analyze_ini_sh must be configured — no env fallback``). That is the right failure, and this
    is how a caller outside a full run gets the same answer the run path gets.
    """

    analyze_env: Path | None
    analyze_ini_sh: str | None
    repl_init_py: str | None
    analyze_probe: str | None
    environment: Any = None
    """The same analysis environment as one object (``auto_research.env.analysis_environment``)."""
    catalogue: Any = None
    """``() -> list[Environment]``: every environment a unit may list and use (``env_catalogue``);
    called when asked, so an environment the pipeline builds mid-run appears."""

    @classmethod
    def from_config(cls, orch: dict[str, Any] | None = None) -> "SandboxSettings":
        from auto_research.env import analysis_environment

        from functools import partial

        from auto_research.env import env_catalogue

        settings = orch if orch is not None else load_orchestration_config()
        env = settings.get("analyze_env")
        return cls(
            analyze_env=Path(env) if env else None,
            analyze_ini_sh=settings.get("analyze_ini_sh") or None,
            repl_init_py=settings.get("repl_init_py") or None,
            analyze_probe=settings.get("analyze_probe") or None,
            environment=analysis_environment(settings),
            catalogue=partial(env_catalogue, settings),
        )


def attach_reviewer_runtime(
    reviewer: Any,
    *,
    run_id: str,
    toolkit_root: Path,
    run_monitor: Any | None = None,
    retrieval: Any | None = None,
    web_source: Any | None = None,
    human_handler: Any | None = None,
) -> None:
    """Give the reviewer the tools, report folder, receipt store and monitor production gives it.

    The same seam as :func:`attach_lead_runtime`, for the same reason: a live cell driving a real
    reviewer outside the graph gets exactly production's wiring rather than a copy of it. Leaves a
    pre-wired ``tools`` alone and is best-effort for duck-typed fakes.
    """
    if getattr(reviewer, "tools", None) is None:
        try:
            reviewer.tools = readonly_tools_for(
                "reviewer",
                roots=readonly_roots_for(run_id, toolkit_root),
                retrieval=retrieval,
                web_source=web_source,
            )
        except Exception:  # noqa: BLE001 — tools are optional; never block the run
            pass
    # The reviewer's work folder (one report JSONL per review) and the run's ONE receipt store.
    try:
        if getattr(reviewer, "report_dir", None) is None:
            reviewer.report_dir = agent_dir(run_id, "reviewer")
        if getattr(reviewer, "receipts", None) is None:
            reviewer.receipts = ReceiptStore.for_run(run_dir(run_id))
    except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
        pass
    if run_monitor is not None:
        _attach_monitor(reviewer, run_monitor)


def attach_lead_runtime(
    lead: Any,
    *,
    run_id: str,
    toolkit_root: Path,
    run_monitor: Any | None = None,
    run_control: RunControl | None = None,
    retrieval: Any | None = None,
    web_source: Any | None = None,
    human_handler: Any | None = None,
) -> None:
    """Give the lead the identity, report folder, monitor, stop channel and tools production gives it.

    The analyst lead's wiring was spread across ~40 lines of ``run_request`` while the workers'
    had a seam of their own (:func:`attach_worker_runtime`, 2026-09-08), so anything driving a
    real lead outside the graph — a live drill, a probe — restated it by hand and measured its own
    scaffolding rather than production's. That is the defect that made a seat probe drive a worker
    with no sandbox: *a test below the layer the defects live in measures the test.*

    Deliberately mirrors ``run_request``'s order and its best-effort posture, including leaving a
    pre-wired ``tools`` alone, so routing the run path through this is behaviour-preserving.
    """
    try:
        lead.agent_key = "analyst_lead"
        lead.report_dir = agent_dir(run_id, "analyst_lead")
        lead.receipts = ReceiptStore.for_run(run_dir(run_id))
    except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
        pass
    if run_monitor is not None:
        _attach_monitor(lead, run_monitor)
    if run_control is not None:
        try:
            lead.run_control = run_control
        except Exception:  # noqa: BLE001
            pass
    if getattr(lead, "tools", None) is None:
        try:
            lead.tools = readonly_tools_for(
                "analyst_lead",
                roots=readonly_roots_for(run_id, toolkit_root),
                retrieval=retrieval,
                web_source=web_source,
            )
        except Exception:  # noqa: BLE001 — tools are optional; never block the run
            pass
    # Provenance: what the lead reads is receipted under its key, so it can ground its report.
    tools = getattr(lead, "tools", None)
    if tools is not None and getattr(tools, "receipts", None) is None:
        try:
            tools.receipts = ReceiptStore.for_run(run_dir(run_id))
            tools.receipt_source = "analyst_lead"
        except Exception:  # noqa: BLE001 — a pre-wired fake may not accept it
            pass


def attach_worker_runtime(
    workers: Sequence[WorkerLike],
    *,
    run_id: str,
    registry: ToolkitRegistry,
    toolkit_root: Path,
    sandbox_settings: SandboxSettings | None = None,
    mcp_client: MCPToolClient | None = None,
    run_control: RunControl | None = None,
) -> list[PersistentSandbox]:
    """Give each worker the identity, memory, sandbox and registry production gives it.

    Returns the sandboxes so the caller can shut them down (``await sandbox.shutdown()``).
    **Without this a worker has no tool loop at all**, which is the difference between measuring a
    seat and measuring nothing. ``sandbox_settings`` defaults to the configured ones, so a caller
    cannot accidentally build a worker whose REPL is configured differently from production's.
    """
    settings = sandbox_settings or SandboxSettings.from_config()
    sandboxes: list[PersistentSandbox] = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, AnalystWorkerAgent):
            continue
        key = f"analyst_worker_{index}"
        worker.memory_path = memory_path(run_id, key)
        worker.agent_key = key
        worker.report_dir = agent_dir(run_id, key)
        worker.report_dir.mkdir(parents=True, exist_ok=True)
        worker.receipts = ReceiptStore.for_run(run_dir(run_id))
        sandbox = PersistentSandbox(
            toolkit_root=toolkit_root,
            workdir=worker.report_dir,
            analyze_env=settings.analyze_env,
            analyze_ini_sh=settings.analyze_ini_sh,
            repl_init_py=settings.repl_init_py,
            mcp_client=mcp_client,
            environment=settings.environment,
            environments=settings.catalogue,
        )
        worker.sandbox = sandbox
        worker.registry = registry
        sandboxes.append(sandbox)
        if run_control is not None:
            run_control.register_aborter(key, sandbox.abort)
    return sandboxes


def _default_graph():
    """Build the compiled graph with all defaults from config.

    Synchronous; called at module import time by serve.py for LangGraph Studio.
    Reads config/orchestration.yaml and config/models.yaml. Requires API keys
    to be present in the environment (langgraph-cli loads .env before importing).
    Workers are constructed without sandboxes (Studio use — no persistent processes).
    """
    orch = load_orchestration_config()
    max_iterations = int(orch.get("max_iterations", 3))
    debate_max_rounds = int(orch.get("debate_max_rounds", 3))
    tool_retry_cap = int(orch.get("tool_retry_cap", 3))
    toolkit_root = Path(orch["toolkit_root"])  # required; validated by the launch preflight
    disabled_tools: list[str] = orch.get("disabled_tools") or []

    storage = Storage()
    worker_group = resolve("analyst_worker")
    workers = [AnalystWorkerAgent(model=h) for h in worker_group.handles[:DEFAULT_WORKER_COUNT]]
    registry = ToolkitRegistry(toolkit_root, disabled_tools=disabled_tools)

    return build_graph(
        reviewer=ReviewerAgent(),
        io_agent=LibrarianIOAgent(storage=storage),
        retrieval=SqliteRetrievalBackend(storage),
        workers=workers,
        lead=AnalystLeadAgent(),
        consensus=DebateToConverge(
            max_rounds=debate_max_rounds,
            registry=registry,
            tool_retry_cap=tool_retry_cap,
        ),
        max_iterations=max_iterations,
        registry=registry,
    )


async def run_request(
    request: ResearchRequest | str,
    *,
    reviewer: ReviewerLike | None = None,
    io_agent: IOAgentLike | None = None,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike] | None = None,
    lead: LeadLike | None = None,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int | None = None,
    chatter: int = 10,
    monitor: RunMonitor | None = None,
    monitor_jsonl: str | Path | None = None,
    report_formats: str | list[str] | None = None,
) -> ReviewerReport:
    """Run the research-loop graph for one request and return the verdict."""

    run = await run_research(
        request,
        reviewer=reviewer,
        io_agent=io_agent,
        retrieval=retrieval,
        workers=workers,
        lead=lead,
        consensus=consensus,
        max_iterations=max_iterations,
        chatter=chatter,
        monitor=monitor,
        monitor_jsonl=monitor_jsonl,
        report_formats=report_formats,
    )
    return run.verdict


def _normalize_report_formats(
    value: str | list[str] | None,
    default: list[str],
) -> list[str]:
    """Normalize report_formats to a validated list (the configured default is validated too)."""
    _valid = {"jsonl", "tex", "html"}
    if value is None:
        value = list(default)
    if isinstance(value, str):
        value = [value]
    unknown = set(value) - _valid
    if unknown:
        raise ValueError(f"Unknown report_formats: {sorted(unknown)}. Valid: {sorted(_valid)}")
    return list(value)


async def _reconcile_environments(toolkit_root: Path, monitor: Any) -> None:
    """The pipeline's environments against their committed definitions, before any unit runs (env
    plan B3c-5): missing builds made, drift rebuilt visibly, changed externals reported — each a
    ledger event. A problem is recorded, never the end of the launch; a stop kills the builds."""
    from auto_research.sandbox.env_build import StoppableRunner
    from auto_research.sandbox.merge_flow import reconcile_environments

    runner = StoppableRunner()
    try:
        results = await asyncio.to_thread(reconcile_environments, toolkit_root, runner=runner)
    except asyncio.CancelledError:
        runner.stop()
        raise
    except Exception as exc:  # noqa: BLE001 — environments are reported, never the launch's end
        await monitor.record("run", "env.reconcile.error", level=2,
                             summary=f"environments not reconciled: {type(exc).__name__}: {exc}",
                             data={"error": repr(exc)})
        return
    summaries = {
        "built": lambda r: f"built {r.get('name')} (its build record was {r.get('cause')})",
        "drift": lambda r: (f"ar-{r['env']} drifted from its definition: kept as {Path(r['kept']).name}, "
                            "rebuilt from the definition" if r.get("rebuilt") else
                            f"ar-{r['env']} drifted from its definition; the rebuild failed "
                            f"({r.get('error')}), so the drifted prefix stays in use"),
        "tainted": lambda r: (f"an unjailed build ran in this conda dir: every jailed build is fresh "
                              f"(nothing reused) until a human checks and removes {r['marker']}"),
        "external_changed": lambda r: f"ar-{r['env']}: declared externals changed: {', '.join(r['externals'])}",
        "failed": lambda r: f"ar-{r['env']}: {r.get('error')}",
    }
    for result in results:
        if result["status"] in summaries:
            event = {"built": "env.build", "drift": "env.drift", "external_changed": "env.external_changed",
                     "failed": "env.build.error", "tainted": "env.tainted"}[result["status"]]
            await monitor.record("run", event, level=2 if result["status"] != "built" else 3,
                                 summary=summaries[result["status"]](result), data=result)


async def run_research(
    request: ResearchRequest | str,
    *,
    reviewer: ReviewerLike | None = None,
    io_agent: IOAgentLike | None = None,
    retrieval: RetrievalBackend | None = None,
    workers: Sequence[WorkerLike] | None = None,
    lead: LeadLike | None = None,
    consensus: ConsensusStrategy | None = None,
    max_iterations: int | None = None,
    chatter: int = 10,
    monitor: RunMonitor | None = None,
    monitor_jsonl: str | Path | None = None,
    report_formats: str | list[str] | None = None,
    human_handler: HumanHandler | None = None,
    human_channel: str | None = None,
    run_id: str | None = None,
    resume: bool = False,
    install_signal_handlers: bool = False,
) -> ResearchRun:
    """Run the graph and return the verdict, final state, and monitor.

    ``install_signal_handlers=True`` makes SIGINT/SIGTERM interrupt the run. Only a process we
    **own** may ask for this — the CLI and the detached backend do; a library caller must not,
    because seizing the host's signal handling is not a runtime's business (and off the main
    thread ``signal`` raises). A notebook gets the same capability through ``/stop``, which is
    what Ctrl-C would be if the run had a terminal.

    ``resume=True`` re-enters the run keyed by ``run_id``: if a checkpoint exists it continues
    from the latest completed node (the in-flight node's transient ReAct history is not
    persisted, so that node re-runs), else it starts fresh. A stable ``run_id`` (e.g. the
    detached backend's deterministic id) is required for resume to find its checkpoint.

    ``max_iterations`` and ``debate_max_rounds`` are read from
    ``config/orchestration.yaml`` when not supplied explicitly.

    Missing agent dependencies are constructed from ``config/models.yaml``.
    Local retrieval uses ``SqliteRetrievalBackend`` by default (no LLM call).
    ``chatter`` follows the monitor's 1-10 verbosity scale; 10 records the full
    agent/tool/consensus narrative.

    A per-run workspace is created under
    ``<runtime_dir>/runs/{run_id}/`` (``runtime_dir`` from ``config/orchestration.yaml``) for each agent.  One
    ``PersistentSandbox`` is spawned per analyst worker; all sandboxes are torn
    down after the graph completes or raises.

    A human stop (``/stop``, or a signal when ``install_signal_handlers``) **cancels** this
    coroutine, runs the deterministic cleanup below, and raises
    :class:`~auto_research.run_control.UserInterruptionError`. No report is composed and no model
    is called on that path — see :func:`_interrupt_cleanup`.
    """

    # Resolve the human-in-the-loop channel (explicit handler → channel → auto-detect).
    human_handler = resolve_handler(human_handler, human_channel)

    if isinstance(request, str):
        request = ResearchRequest(question=request)

    # A caller (e.g. the detached `run_research_nohup` backend) may pin the run_id so its
    # workspace/report/JSONL land in a known, reconnectable dir; else auto-generate.
    run_id = run_id or uuid.uuid4().hex

    # Resume-time branch self-heal (§12 Stage 2): a hard-kill *during authoring* can leave the
    # `ai` branch dirty (its in-process failure reset never ran), which the preflight clean-tree
    # check would then refuse — an unrecoverable restart loop. On resume, tidy it to the killed
    # attempt's recorded base-ref FIRST (no-op on a clean branch), so the preflight then passes.
    heal_result = None
    if resume:
        from auto_research.sandbox.merge_flow import heal_toolkit_branch_if_dirty

        heal_result = heal_toolkit_branch_if_dirty(run_dir(run_id))

    # The monitor is built BEFORE the preflight so the launch checklist lands in the run
    # ledger ([J1]a): previously preflight ran first and printed only to stdout, so a detached
    # or notebook run — the primary mode — kept no record of why it refused to start, and a
    # preflight failure produced no run artifact at all.
    # The monitor carries the RUN's id, not a fresh uuid of its own: the ledger is the run's
    # record, so its `run_id` must name the run/workspace dir a reader can go look in (before
    # this, `RunMonitor` minted an unrelated uuid4 and nothing tied a log line to its run).
    run_monitor = monitor or RunMonitor(chatter=chatter, jsonl_path=monitor_jsonl, run_id=run_id)
    run_monitor.run_id = run_id  # a caller-supplied monitor (the CLI's) gets the same guarantee
    # Cost accounting: one meter per run, installed BEFORE any model client exists so every call
    # (ours and the coder's Agent-SDK sessions) accumulates in one place, and every monitor event
    # — the preflight checklist included — carries the running total. The monitor ledger is the
    # only cost record there is (§J1); nothing writes a separate cost log.
    cost_meter = install_cost_meter(run_id=run_id, monitor=run_monitor)
    # Receipted files read back changed (F26) are alerted on the run's own ledger.
    from auto_research.provenance.store import install_drift_alert

    install_drift_alert(run_monitor)
    if getattr(run_monitor, "cost_meter", None) is None:
        run_monitor.cost_meter = cost_meter
    if resume and heal_result is not None and heal_result.reason not in ("clean", "not_git"):
        # Recorded whatever the outcome: a refused heal (e.g. a base ref the branch never passed
        # through) is a tampering signal, not only a dirty tree.
        await run_monitor.record(
            "run", "run.resume.heal", level=1,
            summary=("Resume: healed a dirty authoring branch left by a prior hard-kill"
                     if heal_result.healed else f"Resume: authoring branch NOT healed ({heal_result.reason})"),
            data={
                "reason": heal_result.reason,
                "base_ref": heal_result.base_ref,
                "patch": str(heal_result.patch_path) if heal_result.patch_path else None,
            },
        )

    # Run the full launch checklist — which pings the channel — before any LLM (§9.8).
    await run_preflight(human_handler=human_handler, monitor=run_monitor)

    orch = load_orchestration_config()

    if max_iterations is None:
        max_iterations = int(orch.get("max_iterations", 3))
    debate_max_rounds = int(orch.get("debate_max_rounds", 3))
    tool_retry_cap = int(orch.get("tool_retry_cap", 3))
    toolkit_root = Path(orch["toolkit_root"])  # required; validated by the launch preflight
    await _reconcile_environments(toolkit_root, run_monitor)
    sandbox_settings = SandboxSettings.from_config(orch)
    # Every receipt names the environment it was produced in (env plan B1): in-process tools run
    # in the orchestrator's; the analysis environment's lock is written now, the worker stamps it.
    try:
        from auto_research.env import env_lock_dir, host_environment
        from auto_research.provenance.store import ReceiptStore as _Store
        from auto_research.sandbox.envs import fingerprint_and_lock

        _Store.for_run(run_dir(run_id)).default_env = fingerprint_and_lock(
            host_environment(), env_lock_dir()
        )
        analysis = sandbox_settings.environment  # the same object the REPL and the shell run in
        if analysis is not None:
            fingerprint_and_lock(analysis, env_lock_dir())
        env_error: Exception | None = None
    except Exception as exc:  # noqa: BLE001 — an unreadable env leaves receipts unstamped, never the run dead
        env_error = exc  # recorded after run.start, which opens the ledger
    analyze_env = sandbox_settings.analyze_env
    analyze_ini_sh = sandbox_settings.analyze_ini_sh
    repl_init_py = sandbox_settings.repl_init_py
    analyze_probe = sandbox_settings.analyze_probe
    disabled_tools: list[str] = orch.get("disabled_tools") or []
    default_formats: list[str] = orch.get("report_formats") or ["jsonl", "tex"]
    resolved_formats = _normalize_report_formats(report_formats, default_formats)

    # Run-wide control (HITL /steer, /stop). Threaded onto every role + the consensus strategy;
    # the command channel writes to it. A steer is read cooperatively by the ReAct loops; a stop
    # cancels the task registered here, so it needs no cooperation from anything.
    run_control = RunControl()
    run_control.register_run_task(asyncio.current_task())
    _restore_signals = _install_interrupt_signals(run_control) if install_signal_handlers else None

    storage = Storage()
    reviewer = reviewer or ReviewerAgent(monitor=run_monitor)
    io_agent = io_agent or LibrarianIOAgent(storage=storage, monitor=run_monitor)
    retrieval = retrieval or SqliteRetrievalBackend(storage)
    lead = lead or AnalystLeadAgent(monitor=run_monitor)

    # Build or validate worker list
    if workers is None:
        worker_group = resolve("analyst_worker")
        worker_handles = worker_group.handles[:DEFAULT_WORKER_COUNT]
        workers = [
            AnalystWorkerAgent(
                model=handle, monitor=run_monitor, worker_index=i,
                human_handler=human_handler,
            )
            for i, handle in enumerate(worker_handles)
        ]
    else:
        # Caller-supplied workers still get the resolved raise-to-human channel.
        for worker in workers:
            if getattr(worker, "human_handler", None) is None:
                try:
                    worker.human_handler = human_handler
                except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
                    pass

    # Create per-agent workspace folders
    analyst_keys = [f"analyst_worker_{i}" for i in range(len(workers))]
    agent_keys = ["reviewer", "analyst_lead", "librarian_io"] + analyst_keys
    create_run_workspace(run_id, agent_keys)
    # The librarian's working folder (its per-pass report JSONL) and the run's ONE receipt store,
    # so its paper/web handles share counters with every other role's.
    try:
        if getattr(io_agent, "report_dir", None) is None:
            io_agent.report_dir = agent_dir(run_id, "librarian_io")
        if getattr(io_agent, "receipts", None) is None:
            from auto_research.provenance.store import ReceiptStore

            io_agent.receipts = ReceiptStore.for_run(run_dir(run_id))
    except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
        pass

    # Give the reviewer and lead a read-only inspection loop (run artifacts +
    # knowledge base), unless a caller pre-wired their own tools.
    from auto_research.sources import WebSource

    # web_search is on for the reviewer and lead (and workers, wired at the provider) so any
    # role can confirm an authoritative-doc/science point against a primary source rather than
    # guessing (their prompts promise it). WebSource's Tavily client is lazy; TAVILY_API_KEY is a
    # launch preflight requirement, so construction never touches the key here.
    web_source = WebSource()

    # The reviewer and analyst lead both browse the whole research_toolkit read-only.
    # The lead needs it to decide a tool proposal's disposition (extend an existing package
    # vs author a new one) by reading the full lib/ source — not just the minimal skills/
    # stubs the workers see (CLAUDE.md §9.3). The reviewer needs it to inspect the same
    # tool source/manifests and confirm results came from correctly-parameterised tool
    # calls. Read-only/path-confined, no shell: the runtime never writes the toolkit (§2)
    # and this matches the §12 "read-only file-tree exploration" posture.
    attach_reviewer_runtime(
        reviewer,
        run_id=run_id,
        toolkit_root=toolkit_root,
        retrieval=retrieval,
        web_source=web_source,
        human_handler=human_handler,
    )

    # V-4: a stable key + report folder for the lead's per-round adjudicated report JSONLs, its
    # monitor, its stop channel and its read-only tools — all through the one seam, so a live
    # drill driving a real lead outside the graph gets exactly this and cannot drift from it.
    attach_lead_runtime(
        lead,
        run_id=run_id,
        toolkit_root=toolkit_root,
        run_monitor=run_monitor,
        run_control=run_control,
        retrieval=retrieval,
        web_source=web_source,
        human_handler=human_handler,
    )

    _attach_monitor(reviewer, run_monitor)
    _attach_monitor(io_agent, run_monitor)
    for worker in workers:
        _attach_monitor(worker, run_monitor)

    # HITL /steer, /stop: give every role a stable steer key (workers/lead already keyed above)
    # and the shared RunControl its ReAct loop consults. Best-effort so duck-typed fakes are fine.
    for target, key in ((reviewer, "reviewer"), (io_agent, "librarian_io")):
        try:
            if getattr(target, "agent_key", None) is None:
                target.agent_key = key
            target.run_control = run_control
        except Exception:  # noqa: BLE001
            pass
    for worker in workers:
        try:
            worker.run_control = run_control
        except Exception:  # noqa: BLE001
            pass

    # HITL slash-commands (Telegram/Slack): give a command-capable connector a run-scoped context
    # — the live roster (for /help + /steer targeting), the monitor JSONL (for /status), and a
    # light status model (LLM /status). No-op for connectors without attach_commands (cli/whatsapp,
    # none). The connector reads run_control from the ctx so /stop unblocks a pending ask.
    if hasattr(human_handler, "attach_commands"):
        def _roster() -> list[tuple[str, str, str]]:
            entries = [
                ("reviewer", getattr(reviewer, "agent_key", None) or "reviewer",
                 _model_label(reviewer.model) or "?"),
                ("analyst", getattr(lead, "agent_key", None) or "analyst_lead",
                 _model_label(lead.model) or "?"),
            ]
            for i, w in enumerate(workers):
                entries.append(("analyst", getattr(w, "agent_key", None) or f"analyst_worker_{i}",
                                _model_label(w.model) or "?"))
            entries.append(("librarian", getattr(io_agent, "agent_key", None) or "librarian_io",
                            _model_label(io_agent.model) or "?"))
            return entries

        # `status_monitor` is a REQUIRED role (models/registry.py), so this resolves or the run
        # never launched — there is no optional path and no deterministic fallback behind it.
        from auto_research.hitl.commands import CommandContext, make_status_answer

        jsonl_path = getattr(run_monitor, "jsonl_path", None)
        status_answer = make_status_answer(
            resolve("status_monitor").handles[0], jsonl_path, monitor=run_monitor
        )
        try:
            human_handler.attach_commands(
                CommandContext(
                    run_control=run_control,
                    roster=_roster,
                    monitor_jsonl_path=jsonl_path,
                    status_answer=status_answer,
                    monitor=run_monitor,  # commands land in the ledger, like every other actor
                    run_id=run_id,  # button payloads carry it, so another run's tap is refused
                )
            )
        except Exception:  # noqa: BLE001 — commands are optional; never block the run
            pass

    registry = ToolkitRegistry(toolkit_root, disabled_tools=disabled_tools)

    # Start MCP client and index its tools into the registry.
    # When mcp_servers is empty/unset, build the default builtin-server config
    # using the analysis env python so bash_exec can import astropy/xspec_utils.
    mcp_server_configs: list[dict] | None = orch.get("mcp_servers") or None
    if not mcp_server_configs:
        mcp_server_configs = [
            _builtin_server_config(
                *_env_spelling(sandbox_settings.environment, analyze_env, analyze_ini_sh),
                toolkit_root,
                cwd=run_dir(run_id), analyze_probe=analyze_probe,
                # The workers' own folders only (the shell runs each command in the caller's).
                writable_dirs=[
                    agent_dir(run_id, f"analyst_worker_{index}")
                    for index, worker in enumerate(workers)
                    if isinstance(worker, AnalystWorkerAgent)
                ],
            )
        ]
    mcp_client = MCPToolClient(servers=mcp_server_configs)
    await mcp_client.start()
    registry.load_mcp(mcp_client)

    if consensus is None:
        consensus = DebateToConverge(
            max_rounds=debate_max_rounds,
            monitor=run_monitor,
            registry=registry,
            tool_retry_cap=tool_retry_cap,
            # Phase-2 step-7 authoring/resume: the lead authors missing capability into
            # research_toolkit on the ambient `ai` branch, then workers rerun (§9.10).
            tool_author_cap=int(orch.get("tool_author_cap", 3)),
            toolkit_root=toolkit_root,
            worktree_base=run_dir(run_id) / "authoring",
            direct_branch=str(orch.get("tool_author_branch", "ai")),
            human_handler=human_handler,
            run_control=run_control,
        )

    # Identity, memory, one persistent sandbox each and the registry — the wiring without which a
    # worker has no tool loop. `attach_worker_runtime` is the one definition of it, so anything
    # that drives a worker outside a full run (a probe, a benchmark) gets the same worker rather
    # than a look-alike. It also registers each sandbox's aborter, so a human /stop cancels an
    # in-flight tool call instead of waiting for a safe point hours away.
    sandboxes = attach_worker_runtime(
        workers,
        run_id=run_id,
        registry=registry,
        toolkit_root=toolkit_root,
        sandbox_settings=sandbox_settings,
        mcp_client=mcp_client,
        run_control=run_control,
    )

    # The quick channel (E12): every graph seat on one router (the co-lead and status monitor never).
    attach_channel(
        (reviewer, io_agent, lead, *workers), run_id=run_id, run_control=run_control,
        human_handler=human_handler, monitor=run_monitor,
    )

    await run_monitor.record(
        "run",
        "run.start",
        level=1,
        summary="Research run started",
        data={
            "run_id": run_id,
            "request": request,
            "max_iterations": max_iterations,
            "tool_retry_cap": tool_retry_cap,
            # Recorded so the end-of-run summary can point at them without rebuilding paths from
            # config — the digest stays derivable from the ledger alone, incl. on a replay.
            "run_dir": str(run_dir(run_id)),
            "monitor_jsonl": str(getattr(run_monitor, "jsonl_path", "") or ""),
        },
    )
    if env_error is not None:
        await run_monitor.record(
            "run", "env.fingerprint.error", level=3,
            summary=f"Environment fingerprint unavailable: {env_error!r}", data={},
        )

    # Start all sandboxes before the graph runs
    for sandbox in sandboxes:
        await sandbox.start()

    # Durable resume (§12): compile the graph with a per-run sqlite checkpointer keyed by
    # run_id (thread_id). LangGraph snapshots ResearchState after every node; durability="sync"
    # flushes before the next node so even a hard kill leaves a valid last-node checkpoint.
    # Real runs always attach it (no silent non-durable fallback — the preflight asserts the
    # package is present); resume=True re-enters from the latest snapshot instead of the top.
    initial_state: dict[str, Any] = {
        "request": request,
        "report": None,
        "verdict": None,
        "verdicts": [],
        "arrived": "request",
        "human_rounds": 0,
        "lit_iterations": 0,
        "analysis_iterations": 0,
        "max_iterations": max_iterations,
    }
    config = {"configurable": {"thread_id": run_id}}
    # The checkpoint is the run's serialized state, not its workspace — it moves with the
    # ledger, out of the agents' reach. It is the heaviest single self-read available: the whole
    # graph state, including every message, in one file.
    async with open_checkpointer(checkpoint_path(ledger_dir(run_id))) as saver:
        graph = build_graph(
            reviewer=reviewer,
            io_agent=io_agent,
            retrieval=retrieval,
            workers=workers,
            lead=lead,
            consensus=consensus,
            max_iterations=max_iterations,
            registry=registry,
            monitor=run_monitor,
            run_id=run_id,
            receipts=ReceiptStore.for_run(run_dir(run_id)),
            report_formats=resolved_formats,
            checkpointer=saver,
            human_handler=human_handler,
        )

        try:
            graph_input: Any = initial_state
            if resume:
                existing = await saver.aget_tuple(config)
                if existing is not None:
                    _refuse_pre_sole_io_checkpoint(existing)
                    graph_input = None  # None → LangGraph continues from the latest snapshot
                    await run_monitor.record(
                        "run",
                        "run.resume",
                        level=1,
                        summary="Resuming from the latest checkpoint",
                        data={
                            "run_id": run_id,
                            "checkpoint_step": (existing.metadata or {}).get("step"),
                        },
                    )
                else:
                    await run_monitor.record(
                        "run",
                        "run.resume",
                        level=1,
                        summary="Resume requested but no checkpoint found — starting fresh",
                        data={"run_id": run_id},
                    )
            final_state: dict[str, Any] = await graph.ainvoke(
                graph_input, config=config, durability="sync"
            )
        except asyncio.CancelledError:
            if not run_control.stop_requested:
                raise  # not ours: an outer cancel (kernel shutdown, a caller's timeout)
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()  # the cancellation was the delivery mechanism; it ends here
            # A human stop. Cleanup is shielded because we are already inside a cancelled scope,
            # where a bare `await` in teardown is cancelled the moment it suspends — the hole that
            # left sandbox children running and the authoring branch dirty.
            await asyncio.shield(
                _interrupt_cleanup(
                    run_control,
                    sandboxes=sandboxes,
                    mcp_client=mcp_client,
                    human_handler=human_handler,
                    monitor=run_monitor,
                )
            )
            raise UserInterruptionError(
                run_control.stop_reason,
                by=run_control.stopped_by or "human",
                artifacts={
                    "run_dir": str(run_dir(run_id)),
                    "ledger": str(run_monitor.jsonl_path or ""),
                },
            ) from None
        except Exception as exc:
            await run_monitor.record(
                "run",
                "run.error",
                level=1,
                summary="Research run failed",
                data={"error": repr(exc)},
            )
            # A crashed run gets the same digest as a finished one — what it spent, how far it got,
            # what it authored. Guarded: a summary fault must never mask the real exception.
            await _record_run_summary(run_monitor)
            raise
        finally:
            if _restore_signals is not None:
                _restore_signals()
            # An interrupt already killed these; shutdown is idempotent and returns at once.
            for sandbox in sandboxes:
                await sandbox.shutdown()
            await mcp_client.shutdown()

    verdict = final_state["verdict"]
    await run_monitor.record(
        "run",
        "run.finish",
        level=1,
        summary="Research run finished",
        data={
            "run_id": run_id,
            "verdict": verdict,
            "lit_iterations": final_state.get("lit_iterations", 0),
            "analysis_iterations": final_state.get("analysis_iterations", 0),
            "human_rounds": final_state.get("human_rounds", 0),
            "cost_usd": cost_meter.total_usd,
        },
    )
    await _record_run_summary(run_monitor)
    return ResearchRun(
        request=request,
        verdict=verdict,
        final_state=final_state,
        monitor=run_monitor,
        research_report=final_state.get("research_report"),
        report_jsonl=final_state.get("report_jsonl"),
        report_latex=final_state.get("report_latex"),
        report_pdf=final_state.get("report_pdf"),
        report_html=final_state.get("report_html"),
    )


def _refuse_pre_sole_io_checkpoint(existing: Any) -> None:
    """Refuse, loudly, to resume a run checkpointed before the reviewer became the sole I/O.

    That graph (before 2026-09-25) had other nodes (``review_literature``, ``review_analysis``) and
    held one literature report where this one accumulates a list, so LangGraph would fail deep
    inside a node, or resume into a node that no longer exists. Every state this graph writes
    carries ``arrived``; one without it came from the old graph.
    """
    values = (getattr(existing, "checkpoint", None) or {}).get("channel_values") or {}
    if values and "arrived" not in values:
        raise RuntimeError(
            "this run was checkpointed on the graph before the reviewer became the sole I/O "
            "(2026-09-25) and cannot resume on this one; start it again as a new run"
        )


# Per-step bounds on interrupt cleanup. Generous enough for a real kill or connector close, short
# enough that a wedged step (a git lock, a D-state child on NFS) never turns an interrupt into a
# hang — the failure the interrupt design exists to remove. The git reset gets more room because it
# is the one step whose omission costs something beyond this run (a dirty authoring branch).
_INTERRUPT_STEP_TIMEOUT_S = 10.0
_INTERRUPT_RESET_TIMEOUT_S = 30.0


def _install_interrupt_signals(run_control: RunControl):
    """Route SIGINT/SIGTERM to :meth:`RunControl.interrupt`; return a restore callable.

    Installed **only** when the caller owns the process (the CLI and the detached backend), never
    by a library caller. Uses the loop's signal API rather than ``signal.signal`` so the handler
    runs as a loop callback — a signal handler cannot await, and the interrupt needs the loop to
    schedule the cancellation. Degrades to a no-op wherever handlers cannot be installed (a
    non-main thread, or a platform without them) rather than refusing to run.
    """
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda s=sig: run_control.interrupt(f"signal {s.name}", by="signal")
            )
            installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):  # non-main thread / unsupported
            continue

    def restore() -> None:
        for sig in installed:
            try:
                loop.remove_signal_handler(sig)
            except Exception:  # noqa: BLE001 — teardown must never mask a result
                pass

    return restore


async def _interrupt_cleanup(
    run_control: RunControl,
    *,
    sandboxes: Sequence[PersistentSandbox],
    mcp_client: MCPToolClient,
    human_handler: HumanHandler | None,
    monitor: RunMonitor,
) -> None:
    """Deterministic teardown for a human interrupt. **No model is called from here.**

    Three phases, in this order for one reason: *nothing may be able to write to the toolkit when
    the toolkit is repaired.* Resetting the authoring branch while the coder subprocess or a gate
    pytest run is still alive just lets them dirty it again a moment later.

    1. **Kill** every process that can write — each sandbox REPL and its descendants, and (via the
       authoring session's disconnect) the coder SDK subprocess.
    2. **Repair** — reset the authoring branch to the attempt's base ref, saving the discarded work
       as a patch first. An interrupted attempt *is* a non-committed attempt, so this is
       ``AuthoringSession.aclose()``'s existing failure path, reached here because its own
       ``finally`` sits inside the cancelled scope.
    3. **Close and record** — MCP client, HITL connector, then ``run.interrupted`` and the derived
       ``run.summary`` (both read the event stream; neither calls a model).

    Every step is bounded and guarded: a wedged step (a git lock, a D-state child on NFS, a
    connector waiting on the network) logs and yields to the next, because an interrupt that hangs
    in teardown is the failure this design exists to remove. A branch left dirty by a timeout is
    caught by the next launch's clean-tree preflight and by the resume-time heal.
    """

    async def _step(name: str, coro, timeout: float | None = None) -> None:
        try:
            await asyncio.wait_for(coro, timeout=timeout or _INTERRUPT_STEP_TIMEOUT_S)
        except (TimeoutError, asyncio.TimeoutError):
            await _record(monitor, "run.interrupt.step_timeout", level=2,
                          summary=f"Interrupt cleanup step '{name}' timed out — continuing",
                          data={"step": name, "timeout_s": timeout})
        except Exception as exc:  # noqa: BLE001 — one failed step never blocks the rest
            await _record(monitor, "run.interrupt.step_error", level=2,
                          summary=f"Interrupt cleanup step '{name}' failed — continuing",
                          data={"step": name, "error": repr(exc)})

    # 1 — kill everything that can still write to disk. Resumed seats first (E12): a seat resumed
    # by a message runs its own loop beside the run task, so cancelling the run task alone would
    # leave it calling tools into the sandboxes this step kills.
    await _step("cancel_resumed_seats", run_control.cancel_resumed())
    for index, sandbox in enumerate(sandboxes):
        await _step(f"kill_sandbox_{index}", sandbox.kill_now())

    # 2 — repair the toolkit (disconnects the coder, then resets to the attempt's base ref).
    session = run_control.authoring_session
    if session is not None:
        run_control.authoring_session = None  # idempotent: never reset twice
        await _step("authoring_reset", session.aclose(), timeout=_INTERRUPT_RESET_TIMEOUT_S)

    # 3 — close the channels, then record. Recording last so the ledger carries the outcome of
    # every step above, including any that timed out.
    await _step("mcp_shutdown", mcp_client.shutdown())
    closer = getattr(human_handler, "shutdown", None) or getattr(human_handler, "close", None)
    if closer is not None:
        await _step("hitl_shutdown", closer())
    await _record(
        monitor, "run.interrupted", level=1,
        summary=f"Run interrupted by {run_control.stopped_by or 'human'} — no report composed",
        data={"reason": run_control.stop_reason, "by": run_control.stopped_by},
    )
    await _record_run_summary(monitor)


async def _record_run_summary(monitor: RunMonitor) -> None:
    """Derive the end-of-run digest from the run's own event stream and record it as one
    ``run.summary`` event, so the stream/follower/notebook all end on the same block.

    Derived from the **JSONL** when there is one: ``cost.call`` events are level 10, so below
    chatter 10 the per-role cost breakdown exists only in the log. Fully guarded — a summary
    failure never changes a run's outcome (the [J2] rationale).
    """

    from auto_research.monitor.summary import build_run_summary, summarize_jsonl

    try:
        path = getattr(monitor, "jsonl_path", None)
        summary = summarize_jsonl(path) if path else build_run_summary(monitor.events)
        await monitor.record(
            "run",
            "run.summary",
            level=1,
            summary=(
                f"Run summary: {summary.get('status', 'unknown')}, "
                f"${float(summary.get('cost_usd', 0.0)):.4f}"
            ),
            data=summary,
        )
    except Exception:  # noqa: BLE001 — the digest is observability, never a run outcome
        pass


def _attach_monitor(target: object, monitor: RunMonitor) -> None:
    if hasattr(target, "monitor") and getattr(target, "monitor", None) is None:
        try:
            setattr(target, "monitor", monitor)
        except Exception:
            pass
