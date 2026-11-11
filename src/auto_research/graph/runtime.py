"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import Mapping, Sequence
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
from auto_research.toolkit_contract import toolkit
from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.provenance import ReceiptStore
from auto_research.agents.roles import LEAD_KEY, _model_label
from auto_research.sandbox.workspace import (
    agent_dir,
    create_run_workspace,
    ledger_dir,
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


def _builtin_server_config(
    environment: Any,
    toolkit_root: Path,
    cwd: Path | None = None,
    probe: str | None = None,
    writable_dirs: Sequence[Path] | None = None,
) -> dict:
    """Build the default builtin-MCP-server config dict.

    ``writable_dirs`` are the folders the shell may write (default: ``cwd``). The run path passes
    each worker's own folder, so the shell cannot rename or replace a folder another process binds
    (a worker folder swapped for a symlink would be bound writable by the next REPL start).

    The server runs in ``environment`` (the toolkit's default one, ``env.analysis_environment``),
    started as the REPL is: a pipeline environment through the shared launcher
    (``envs.in_environment``: a clean start, its activation sourced inside the jail, its ``bin``
    first on ``PATH`` so a bare ``python`` in ``bash_exec`` is the environment's, the toolkit on
    ``PYTHONPATH``); a human environment from its activation script sourced clean
    (``_source_ini_env``: no credential, conda activation or launching-shell state rides along).
    ``probe`` (the environment's ``env.yaml`` probe) supplies the probe example in ``bash_exec``'s
    description, so no domain string lives in this source.

    **The server runs inside the OS jail** (unconditional, D25), with the same allow-list the REPL
    gets (F20). Its source is passed
    with ``-c``, as the REPL's is, so the jail never needs to mount this repository to run it.
    """
    import shlex

    from auto_research.sandbox.builtin_server import __file__ as _bs_file
    from auto_research.sandbox.envs import in_environment
    from auto_research.sandbox.jail import analysis_policy, jail_home, private_tmp, wrap
    from auto_research.env import private_variables
    from auto_research.sandbox.runner import _source_ini_env, private_folders

    builtin_source = Path(_bs_file).read_text(encoding="utf-8")
    python = str(environment.python)
    # -P: no cwd on sys.path, so a module planted in the run folder cannot hijack the server.
    # (3.11+ only; the analysis interpreter is 3.13. The REPL uses SAFE_PATH_PRELUDE instead.)
    command = [python, "-P", "-c", builtin_source]
    pipeline = environment.owner == "pipeline"
    if pipeline:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
        script = in_environment(environment, f'{shlex.quote(python)} "$@"', toolkit_root, pass_args=True,
                                keep=("AR_ENV_PROBE", "PYTHONPYCACHEPREFIX"), quiet=True)
        command = ["/bin/bash", "--noprofile", "--norc", "-c", script, "_", *command[1:]]
    else:
        if environment.activate is None:
            raise ValueError(f"{environment.name} has no activation script: a human environment runs "
                             "the shell only with one (no fallback to this process's)")
        base_env = _source_ini_env(str(environment.activate))
        existing = base_env.get("PYTHONPATH", "")
        pythonpath = str(toolkit_root) + os.pathsep + str(toolkit_root.parent)
        if existing:
            pythonpath += os.pathsep + existing
        env = {**base_env, "PYTHONPATH": pythonpath}
        env["PATH"] = str(environment.prefix / "bin") + os.pathsep + env.get("PATH", "")
    if probe:
        env["AR_ENV_PROBE"] = probe

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
    # The environment's prefix, activation and boot file, read-only (a built prefix and the
    # runtime's activation copies lie under the folders above already).
    own = tuple(str(f) for f in (environment.prefix, environment.activate, environment.boot) if f is not None)
    argv = wrap(
        command,
        analysis_policy(
            label="shell", toolkit_root=toolkit_root,
            writable=tuple(str(folder) for folder in writable),
            readonly=(str(authoring), *(str(folder) for folder in built), *own),
            tmp=tmp, home=private_tmp("shell-home", scratch),  # this run's own, outside its folder
        ),
    )
    # Its own folder for each declared private variable (F23), in the jail's private /tmp. In a
    # pipeline environment the launcher puts the kept part back after the activation.
    env, _ = private_folders(env, private_variables(), at="/tmp", create=tmp, alone=pipeline)
    # In-tree `__pycache__` is never read (git ignores it, so nothing judges it): bytecode goes to
    # the server's private /tmp.
    env["PYTHONPYCACHEPREFIX"] = "/tmp/ar-pycache"

    return {
        "name": "builtin",
        "command": argv[0],
        "args": argv[1:],
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
    catalog: Mapping[str, Any] | None = None,
) -> ReadOnlyToolProvider:
    """The read-only inspection loop production gives ``role``.

    ``role`` is ``"reviewer"`` or ``"analyst_lead"``, and the two differ deliberately. The lead
    gets the stores' catalogs (``catalog``: store name → the lead's own reader-class login,
    :func:`issue_unit_keys`) and the budget-free design channel and is denied the paper knowledge
    base, because it investigates toolkit source and the data rather than literature; the
    reviewer is the paper surface and is given no catalog. Getting those flags wrong does not fail
    — it silently measures a role the pipeline does not have.
    """
    if role not in ("reviewer", "analyst_lead"):
        raise ValueError(f"no read-only tool loop is defined for role {role!r}")
    lead = role == "analyst_lead"
    return ReadOnlyToolProvider(
        roots=list(roots),
        retrieval=retrieval,
        web_source=web_source,
        catalog=catalog if lead else None,
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
    """Where a worker's REPL and shell come from: the toolkit's default environment.

    One definition, because it is resolved from the toolkit contract rather than chosen: a caller
    outside a full run gets the same environment, catalogue and probe the run path gets.
    """

    environment: Any
    """The toolkit's default environment as one object (``auto_research.env.analysis_environment``)."""
    catalogue: Any = None
    """``() -> list[Environment]``: every environment a unit may list and use (``env_catalogue``);
    called when asked, so an environment the pipeline builds mid-run appears."""
    probe: str | None = None
    """The default environment's ``env.yaml`` probe: the example in ``bash_exec``'s description."""

    @classmethod
    def from_config(cls, orch: dict[str, Any] | None = None) -> "SandboxSettings":
        from functools import partial

        from auto_research.env import analysis_environment, default_definition, env_catalogue

        settings = orch if orch is not None else load_orchestration_config()
        return cls(
            environment=analysis_environment(settings),
            catalogue=partial(env_catalogue, settings),
            probe=default_definition().probe or None,
        )


#: The runs of this process that hold the bound toolkit contract (``bind_run_contract``), by run id.
_CONTRACT_RUNS: set[str] = set()


def bind_run_contract(orch: dict[str, Any], run_id: str) -> None:
    """Bind the process to the toolkit contract of the configuration a run was launched with
    (``toolkit_contract.bind``), before its preflight, and hold it for ``run_id`` until
    :func:`release_run_contract`: every reader then takes the contract from ``toolkit()``, never
    from a later read of the file. A later run binds its own configuration's contract once no run of
    this process holds one; while one does, a run whose configuration declares another contract (or
    an invalid block) is refused, since the running one would see its contract change under it. With
    no run holding it, an invalid ``toolkits:`` block binds nothing: the preflight's contract check
    refuses it, recorded."""
    from auto_research import toolkit_contract

    try:
        declared = toolkit_contract.toolkits(orch)
    except ValueError as exc:
        if _CONTRACT_RUNS:
            raise RuntimeError(f"run {run_id}: the configuration's toolkit contract is invalid ({exc}) while "
                               f"run(s) {sorted(_CONTRACT_RUNS)} in this process run on the bound one") from exc
        toolkit_contract.reset()
        return
    if _CONTRACT_RUNS and declared != toolkit_contract.toolkits():
        raise RuntimeError(f"run {run_id}: the configuration declares another toolkit contract than run(s) "
                           f"{sorted(_CONTRACT_RUNS)} in this process run on; start it when they finish, "
                           "or in a process of its own")
    toolkit_contract.bind(orch)
    _CONTRACT_RUNS.add(run_id)


def release_run_contract(run_id: str) -> None:
    """``run_id`` no longer holds the bound contract (its run ended, however it ended)."""
    _CONTRACT_RUNS.discard(run_id)


#: The runs of this process that hold the bound stores (``bind_run_stores``), by run id.
_STORE_RUNS: set[str] = set()


def bind_run_stores(orch: dict[str, Any], run_id: str) -> None:
    """Bind the process to the ``stores:`` block of the configuration a run was launched with
    (``store.config.bind``) and hold it for ``run_id`` until :func:`release_run_stores`, as
    :func:`bind_run_contract` holds the toolkit contract: a run whose configuration declares other
    stores while another run holds them is refused. With none holding them, an absent or invalid
    block binds nothing; the preflight's ``store_config`` refuses it."""
    from auto_research.store import config as store_config

    try:
        declared = store_config.stores(orch)
    except ValueError as exc:
        if _STORE_RUNS:
            raise RuntimeError(f"run {run_id}: the configuration's stores are invalid ({exc}) while run(s) "
                               f"{sorted(_STORE_RUNS)} in this process run on the bound ones") from exc
        store_config.reset()
        return
    if _STORE_RUNS and declared != store_config.stores():
        raise RuntimeError(f"run {run_id}: the configuration declares other stores than run(s) "
                           f"{sorted(_STORE_RUNS)} in this process run on; start it when they finish, "
                           "or in a process of its own")
    store_config.bind(orch)
    _STORE_RUNS.add(run_id)


def release_run_stores(run_id: str) -> None:
    """``run_id`` no longer holds the bound stores (its run ended, however it ended)."""
    _STORE_RUNS.discard(run_id)


def _bound_stores() -> list[Any]:
    """The stores a run of this process bound (:func:`bind_run_stores`); none when the run's
    configuration declares none or an invalid block (``store_config`` refuses that at the preflight),
    never a block read afresh from the file."""
    from auto_research.store.config import stores

    return list(stores()) if _STORE_RUNS else []


#: The unit name the runtime's own reconciler login is issued under.
RECONCILER_UNIT = "reconciler"

#: The run's process record in its ledger folder: host, pid and the pid's start time.
RUN_PROCESS_FILE = "runtime.json"


def record_run_process(run_id: str) -> None:
    """Record that this process runs ``run_id`` (``ledgers/<run>/runtime.json``: host, pid and the
    pid's start time, so a recycled pid never reads as the run). A resumed run overwrites it."""
    import socket

    from auto_research.graph.detached import _proc_create_time

    path = ledger_dir(run_id) / RUN_PROCESS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{RUN_PROCESS_FILE}.{os.getpid()}")
    temporary.write_text(json.dumps({"host": socket.gethostname(), "pid": os.getpid(),
                                     "create_time": _proc_create_time(os.getpid())}))
    os.replace(temporary, path)


def run_process_record(run_id: str) -> dict | None:
    """``run_id``'s process record (``{host, pid, create_time}``) in this runtime dir, or None when
    there is none or it does not read as one."""
    try:
        record = json.loads((ledger_dir(run_id) / RUN_PROCESS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def run_process_alive(run_id: str) -> bool | None:
    """Whether ``run_id``'s runtime process still runs: True or False only for a well-formed record of
    this host (a string host, an integer pid and an integer start time); None whenever it cannot be
    known here (no record, a malformed one, a run of another host), which nothing reads as dead."""
    import socket

    from auto_research.graph.detached import _proc_create_time

    record = run_process_record(run_id) or {}
    pid, created = record.get("pid"), record.get("create_time")
    if (record.get("host") != socket.gethostname() or type(pid) is not int or type(created) is not int
            or pid <= 0):
        return None
    return _proc_create_time(pid) == created


async def sweep_dead_runs(store: Any, run_id: str, monitor: Any) -> list[str]:
    """Revoke the keys every other run on this host holds in ``store`` whose runtime process is gone
    (coordinator ruling: a run is dead when its process is); runs of another host, or with no record
    here, are left alone. Recorded per run (``store.credentials.sweep``; a failed revoke at level 2,
    the run goes on: its own keys are not affected). Returns the runs revoked."""
    from auto_research.store import keys

    swept = []
    for run, principals in (await keys.live_runs(store)).items():
        if run == run_id or run_process_alive(run) is not False:
            continue
        try:
            for name in principals:
                # Again before each: a resume of the run (same principals) writes its record before it
                # issues anything, so it is seen here and its fresh keys stay.
                if run_process_alive(run) is not False:
                    break
                await keys.revoke_principal(store, name)
        except Exception as exc:  # noqa: BLE001 - recorded; the next launch tries again
            await _record(monitor, "store.credentials.sweep_error", level=2,
                          summary=f"Store {store.name}: the keys of dead run {run} were not all revoked: {exc}",
                          data={"store": store.name, "run": run, "error": repr(exc)})
            continue
        swept.append(run)
        await _record(monitor, "store.credentials.sweep", level=2,
                      summary=f"Store {store.name}: revoked the keys of dead run {run}",
                      data={"store": store.name, "run": run, "principals": principals})
    return swept


class StoreNotReady(RuntimeError):
    """A store's servers are not running here: they did not start (``store.launch.start`` refused)
    or do not answer (``store.health.ready_problems``' ``store_servers`` item). Raised by
    :func:`start_run_stores` with the store's own words; every other failure of the launch step (a
    refused admin key, a bucket or catalog schema the store lacks, a sweep, a key, the reconciler)
    is raised as itself."""


async def start_run_stores(run_id: str, keys: Any, monitor: Any) -> None:
    """The run's store launch step, before its preflight (coordinator ruling B): each local store
    ``store_config`` accepts is started (``store.launch.start``, idempotent: a running set is
    shared); every store must then be ready (``store.health.ready_problems``: servers, admin key,
    buckets, in the preflight's words); the keys of dead runs on this host are revoked
    (:func:`sweep_dead_runs`); and the store is reconciled (``store.reconcile.reconcile``) as the
    runtime's own reconciler login, issued here into ``keys`` (a ``store.keys.RunKeys``), its S3 key
    signing the pass (``s3=``), so on-disk edits are marked and "current" moved back before the
    preflight's checks. A store the configuration refuses is left to the preflight, which names why. A
    failure is recorded (``store.launch.error``) and raised; servers that do not start or answer are
    raised as :class:`StoreNotReady`."""
    import psycopg

    from auto_research.store import health, launch
    from auto_research.store.config import LocalStore
    from auto_research.store.reconcile import reconcile

    for store in _bound_stores():
        try:
            if isinstance(store, LocalStore):
                if health.refusals(store):
                    continue
                try:
                    record = await asyncio.to_thread(launch.start, store)
                except RuntimeError as exc:
                    raise StoreNotReady(str(exc)) from exc
                await _record(monitor, "store.launch", level=2,
                              summary=f"Store {store.name} serves ({store.s3_endpoint})",
                              data={"store": store.name, "host": record.get("host"), "pids": record.get("pids")})
            problems = await asyncio.to_thread(health.ready_problems, store)
            if problems:
                not_ready = StoreNotReady if problems[0].startswith("store_servers:") else RuntimeError
                raise not_ready(f"store {store.name} is not ready: {'; '.join(problems)}")
            await sweep_dead_runs(store, run_id, monitor)
            credentials = await keys.issue(store, RECONCILER_UNIT, "reconciler")
            async with await psycopg.AsyncConnection.connect(credentials.dsn(), autocommit=True) as conn:
                report = await reconcile(store, run=run_id, conn=conn, s3=credentials)
            await _record(monitor, "store.reconcile", level=2, summary=f"Store {store.name} reconciled",
                          data={"store": store.name, "report": _plain(report)})
        except Exception as exc:
            await _record(monitor, "store.launch.error", level=1,
                          summary=f"Store {store.name}: the run's launch step failed: {exc}",
                          data={"store": store.name, "error": repr(exc)})
            raise


def _plain(value: Any) -> Any:
    """``value`` as JSON-ready data for the ledger (a pydantic model or dataclass dumped)."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    import dataclasses

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json.loads(json.dumps(dataclasses.asdict(value), default=str))
    return json.loads(json.dumps(value, default=str))


async def issue_unit_keys(keys: Any, units: Sequence[str], monitor: Any) -> dict[str, Any]:
    """Each unit's keys to the default store (``store.config.DEFAULT_STORE``), issued into ``keys``
    with the role the store gives the unit (``roles``), as ``{unit: Credentials}`` for
    :func:`attach_worker_runtime` and :func:`attach_lead_runtime`. Recorded per unit
    (``store.credentials.issue``: role, principal, key id; never a secret). Empty when the run binds
    no store; raises when it binds stores without the default one (``store_config`` refuses that
    configuration at the preflight)."""
    from auto_research.store.config import DEFAULT_STORE

    bound = _bound_stores()
    store = next((s for s in bound if s.name == DEFAULT_STORE), None)
    if store is None:
        if bound:  # never a silent absence: the units would run without their store
            raise RuntimeError(f"the run's stores ({', '.join(s.name for s in bound)}) include no default "
                               f"store {DEFAULT_STORE!r}; name one of them {DEFAULT_STORE!r} in stores:")
        return {}
    out: dict[str, Any] = {}
    for unit in units:
        credentials = await keys.issue(store, unit)
        await _record(monitor, "store.credentials.issue", level=3,
                      summary=f"{unit} holds {credentials.role_class} keys to store {store.name}",
                      data={"store": store.name, "unit": unit, "role": credentials.role_class,
                            "principal": credentials.principal, "access_key_id": credentials.access_key_id})
        out[unit] = credentials
    return out


def _store_records(credentials: Any, reconciler: Any | None = None) -> Any:
    """The unit's view of the store's records (``store.records.StoreRecords``, S5) for its tool
    calls: its keys named by principal and run, its writable area the store's buckets when it is a
    producer, none when a reader; ``reconciler``: the runtime's own reconciler login to that store
    (:func:`run_reconciler`), through which the view reads the catalog and stamps flags."""
    from auto_research.store.records import StoreRecords, writable_area

    writable = writable_area([credentials.store]) if credentials.role_class == "producer" else ()
    return StoreRecords(agent=credentials.principal, run=credentials.run, writable=writable,
                        reconciler=reconciler)


def run_reconciler(keys: Any, store: str) -> Any | None:
    """The runtime's reconciler login to ``store`` that :func:`start_run_stores` issued into
    ``keys`` (a ``store.keys.RunKeys``), or None."""
    return next((c for c in keys.held() if c.unit == RECONCILER_UNIT and c.store == store), None)


class RunLaunch:
    """The run's launch step and its end, one object (S13a, main's ruling 4): what a run binds and
    starts before its preflight, and what it gives back when it ends, for ``run_research`` and for
    anything that drives roles outside the graph (a live cell), so neither restates the order.

    ``start()`` records the run's process (:func:`record_run_process`), reads the configuration once,
    binds the toolkit contract and the stores to the run (:func:`bind_run_contract`,
    :func:`bind_run_stores`) and takes :func:`start_run_stores` with the run's keys (``keys``, a
    ``store.keys.RunKeys``); it returns the configuration. ``hold(control)`` hands the keys to the
    run's stop channel (``RunControl.credentials``), so a stop's cleanup revokes them in its order.
    ``revoke()`` revokes every key (recorded, ``run.teardown.credentials_error`` on a failure, never
    raised; idempotent); ``release()`` lets go of the contract and the stores; ``end()`` does both,
    revoke first. As an async context manager it starts on entry (a failed start ends at once) and
    ends on exit, however the block ends. A store that cannot serve raises :class:`StoreNotReady`
    from ``start``, so a caller tells it from any other failure of the start."""

    def __init__(self, run_id: str, monitor: Any) -> None:
        from auto_research.store.keys import RunKeys

        self.run_id, self.monitor = run_id, monitor
        self.keys = RunKeys(run_id)
        self.orch: dict[str, Any] | None = None

    async def start(self) -> dict[str, Any]:
        record_run_process(self.run_id)  # its keys are swept by a later launch once this process is gone
        self.orch = load_orchestration_config()
        bind_run_contract(self.orch, self.run_id)
        bind_run_stores(self.orch, self.run_id)
        await start_run_stores(self.run_id, self.keys, self.monitor)
        return self.orch

    def hold(self, control: Any) -> None:
        control.credentials = self.keys

    async def revoke(self) -> None:
        await _closed("credentials", self.keys.revoke_all, self.monitor)

    def release(self) -> None:
        release_run_contract(self.run_id)
        release_run_stores(self.run_id)

    async def end(self) -> None:
        try:
            await self.revoke()
        finally:
            self.release()

    async def __aenter__(self) -> "RunLaunch":
        try:
            await self.start()
        except BaseException:
            await self.end()
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.end()


def attach_reviewer_runtime(
    reviewer: Any,
    *,
    run_id: str,
    toolkit_root: Path,
    run_monitor: Any | None = None,
    run_control: RunControl | None = None,
    retrieval: Any | None = None,
    web_source: Any | None = None,
) -> None:
    """Give the reviewer the identity, stop channel, tools, report folder, receipt store and
    monitor production gives it.

    The same seam as :func:`attach_lead_runtime`, for the same reason: a live cell driving a real
    reviewer outside the graph gets exactly production's wiring rather than a copy of it. Its key
    is ``reviewer`` (its address on the quick channel); ``retrieval`` defaults to the knowledge
    base (``SqliteRetrievalBackend()``) and ``web_source`` to ``WebSource()``, as ``run_research``
    passes them. Leaves a pre-wired key and ``tools`` alone and is best-effort for duck-typed fakes.
    The human reaches the reviewer through the quick channel (:func:`attach_channel`).
    """
    try:
        if getattr(reviewer, "agent_key", None) is None:
            reviewer.agent_key = "reviewer"
        if run_control is not None:
            reviewer.run_control = run_control
    except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
        pass
    if getattr(reviewer, "tools", None) is None:
        try:
            from auto_research.sources import WebSource

            reviewer.tools = readonly_tools_for(
                "reviewer",
                roots=readonly_roots_for(run_id, toolkit_root),
                retrieval=retrieval if retrieval is not None else SqliteRetrievalBackend(),
                web_source=web_source if web_source is not None else WebSource(),
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


def attach_librarian_runtime(
    io_agent: Any, *, run_id: str, run_control: RunControl | None = None
) -> None:
    """The librarian's key, working folder (its per-pass report JSONL), stop channel and the run's
    ONE receipt store, so its paper/web handles share counters with every other role's. The seam a
    live cell driving a real librarian uses, as :func:`attach_lead_runtime` is the lead's; a
    pre-wired field stays. A store that cannot open raises: a swallowed one would leave the
    librarian minting into a store of its own."""
    store = ReceiptStore.for_run(run_dir(run_id))
    for name, value in (("agent_key", "librarian_io"), ("report_dir", agent_dir(run_id, "librarian_io")),
                        ("receipts", store), ("run_control", run_control)):
        if value is None or getattr(io_agent, name, None) is not None:
            continue
        try:
            setattr(io_agent, name, value)
        except (AttributeError, TypeError):  # a duck-typed fake that does not take the field
            pass


def attach_lead_runtime(
    lead: Any,
    *,
    run_id: str,
    toolkit_root: Path,
    run_monitor: Any | None = None,
    run_control: RunControl | None = None,
    retrieval: Any | None = None,
    web_source: Any | None = None,
    catalog: Mapping[str, Any] | None = None,
) -> None:
    """Give the lead the identity, report folder, monitor, stop channel and tools production gives it.

    ``catalog``: store name → the lead's own login (:func:`issue_unit_keys`; the store's ``roles``
    make it a reader), through which its ``catalog_query`` reads each store's catalog.

    The analyst lead's wiring was spread across ~40 lines of ``run_request`` while the workers'
    had a seam of their own (:func:`attach_worker_runtime`, 2026-09-08), so anything driving a
    real lead outside the graph — a live drill, a probe — restated it by hand and measured its own
    scaffolding rather than production's. That is the defect that made a seat probe drive a worker
    with no sandbox: *a test below the layer the defects live in measures the test.*

    Deliberately mirrors ``run_request``'s order and its best-effort posture, including leaving a
    pre-wired ``tools`` alone, so routing the run path through this is behaviour-preserving.
    """
    try:
        lead.agent_key = LEAD_KEY
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
                catalog=catalog,
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


async def wire_lead(
    lead: Any,
    *,
    run_id: str,
    toolkit_root: Path,
    keys: Any,
    run_monitor: Any | None = None,
    run_control: RunControl | None = None,
    retrieval: Any | None = None,
    web_source: Any | None = None,
) -> None:
    """The analyst lead's wiring as ``run_research`` gives it, one call for the run path and for
    anything driving a real lead outside the graph (S13a, main's ruling): the lead's own login to
    the default store, issued into ``keys`` (the run's ``store.keys.RunKeys``, :func:`issue_unit_keys`),
    then :func:`attach_lead_runtime` with that catalog, the run's retrieval and web source
    (``retrieval`` defaults to the knowledge base's ``SqliteRetrievalBackend()`` and ``web_source``
    to ``WebSource()``, as :func:`attach_reviewer_runtime` defaults them). The lead reaches the human
    through the quick channel (:func:`attach_channel`) and its cycle (``run_cycle(human_handler=)``),
    never through this wiring (nor through :func:`attach_lead_runtime`)."""
    from auto_research.sources import WebSource

    lead_keys = await issue_unit_keys(keys, [LEAD_KEY], run_monitor)
    attach_lead_runtime(
        lead,
        run_id=run_id,
        toolkit_root=toolkit_root,
        run_monitor=run_monitor,
        run_control=run_control,
        retrieval=retrieval if retrieval is not None else SqliteRetrievalBackend(),
        web_source=web_source if web_source is not None else WebSource(),
        catalog={c.store: c for c in lead_keys.values()},
    )


def attach_worker_runtime(
    workers: Sequence[WorkerLike],
    *,
    run_id: str,
    registry: ToolkitRegistry,
    toolkit_root: Path,
    sandbox_settings: SandboxSettings | None = None,
    mcp_client: MCPToolClient | None = None,
    run_control: RunControl | None = None,
    credentials: dict[str, Any] | None = None,
    reconciler: Any | None = None,
) -> list[PersistentSandbox]:
    """Give each worker the identity, memory, sandbox and registry production gives it.

    ``credentials`` (``{unit: Credentials}``, :func:`issue_unit_keys`): each worker's store keys,
    handed to its REPLs only (never its shell, which every worker shares), and its view of the
    store's records (``worker.store_records``) for its tool calls; ``reconciler``: the runtime's
    reconciler login to that store (:func:`run_reconciler`), which the view reads the catalog and
    stamps flags through.
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
        worker.agent_key = key
        worker.report_dir = agent_dir(run_id, key)
        worker.report_dir.mkdir(parents=True, exist_ok=True)
        worker.receipts = ReceiptStore.for_run(run_dir(run_id))
        held = (credentials or {}).get(key)
        sandbox = PersistentSandbox(
            toolkit_root=toolkit_root,
            workdir=worker.report_dir,
            mcp_client=mcp_client,
            environment=settings.environment,
            environments=settings.catalogue,
            store_env=held.env() if held is not None else None,
        )
        worker.sandbox = sandbox
        worker.store_records = _store_records(held, reconciler) if held is not None else None
        worker.registry = registry
        sandboxes.append(sandbox)
        if run_control is not None:
            run_control.register_aborter(key, sandbox.abort)
    return sandboxes


def stamp_run_environments(run_id: str, *, analysis: Any = None) -> str:
    """Stamp the run's receipt store with the environment its in-process tools run in (the
    orchestrator's, ``host_environment``) and write that state's lock; with ``analysis`` (an
    ``Environment``), write its lock too, which the worker stamps on its receipts. Returns the
    store's default fingerprint. Raises when an environment cannot be read; ``run_research``
    records that as ``env.fingerprint.error`` and runs on with receipts unstamped.
    """
    from auto_research.env import env_lock_dir, host_environment
    from auto_research.sandbox.envs import fingerprint_and_lock

    store = ReceiptStore.for_run(run_dir(run_id))
    store.default_env = fingerprint_and_lock(host_environment(), env_lock_dir())
    if analysis is not None:
        fingerprint_and_lock(analysis, env_lock_dir())
    return store.default_env


def worker_mcp_client(
    workers: Sequence[WorkerLike],
    *,
    run_id: str,
    toolkit_root: Path,
    sandbox_settings: SandboxSettings | None = None,
) -> MCPToolClient:
    """The MCP client the workers' shell (``bash_exec``) runs on, unstarted; the caller starts it,
    loads it into the registry (``registry.load_mcp``) and shuts it down.

    The builtin server, inside the jail: the toolkit's default environment, the run folder as its
    working directory, and each worker's own folder writable (the shell runs each command in its
    caller's). ``run_research`` builds its client here, so a cell driving
    workers outside a full run gets the same shell rather than none or a copy.
    """
    settings = sandbox_settings or SandboxSettings.from_config()
    server = _builtin_server_config(
        settings.environment,
        toolkit_root,
        cwd=run_dir(run_id), probe=settings.probe,
        # The workers' own folders only (the shell runs each command in the caller's).
        writable_dirs=[
            agent_dir(run_id, f"analyst_worker_{index}")
            for index, worker in enumerate(workers)
            if isinstance(worker, AnalystWorkerAgent)
        ],
    )
    return MCPToolClient(servers=[server])


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
    toolkit_root = toolkit().root  # the process's bound toolkit contract, validated by the launch preflight
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


#: Why the launch built an environment, as the ledger says it (D1b).
_BUILT_BECAUSE = {
    "missing": "it had no build record",
    "stale": "its build record named another definition",
    "unjailed": "its record says it was built outside the jail",
    "interrupted": "its prefix was gone",
}


async def _reconcile_environments(toolkit_root: Path, monitor: Any) -> None:
    """The pipeline's environments against their committed definitions, before any unit runs (env
    plan B3c-5): missing builds made, drift rebuilt visibly, changed externals reported — each a
    ledger event. A problem is recorded, never the end of the launch; a stop kills the builds.
    While it runs, each fresh build and a 5-minute heartbeat are recorded (D1/D1b,
    ``merge_flow.run_builds``): run polevo-20261104 was silent 34 min while `ar-xspec` rebuilt."""
    from auto_research.sandbox.env_build import StoppableRunner
    from auto_research.sandbox.merge_flow import (
        PREVIOUS_IN_USE, reconcile_environments, restored_summary, run_builds,
    )

    runner = StoppableRunner()

    async def record(event: str, **fields: Any) -> None:
        await monitor.record("run", event, **fields)

    try:
        results = await run_builds(
            lambda on_start: reconcile_environments(toolkit_root, runner=runner, on_start=on_start),
            record=record, runner=runner,
        )
    except Exception as exc:  # noqa: BLE001 — environments are reported, never the launch's end
        await monitor.record("run", "env.reconcile.error", level=2,
                             summary=f"environments not reconciled: {type(exc).__name__}: {exc}",
                             data={"error": repr(exc)})
        return
    summaries = {
        "built": lambda r: f"built {r.get('name')} ({_BUILT_BECAUSE.get(r.get('cause'), r.get('cause'))})",
        "restored": restored_summary,
        "drift": lambda r: (f"ar-{r['env']} drifted from its definition: kept as {Path(r['kept']).name}, "
                            + (f"restored kept build {r['restored']}" if r.get("restored")
                               else "rebuilt from the definition") if r.get("rebuilt") else
                            f"ar-{r['env']} drifted from its definition; the rebuild failed "
                            f"({r.get('error')}), so the drifted prefix stays in use"),
        "external_changed": lambda r: f"ar-{r['env']}: declared externals changed: {', '.join(r['externals'])}",
        "failed": lambda r: f"ar-{r['env']}: {r.get('error')}" + (PREVIOUS_IN_USE if r.get("previous_in_use") else ""),
    }
    for result in results:
        if result["status"] in summaries:
            event = {"built": "env.build", "restored": "env.restored", "drift": "env.drift",
                     "external_changed": "env.external_changed",
                     "failed": "env.build.error"}[result["status"]]
            await monitor.record("run", event, level=3 if result["status"] in ("built", "restored") else 2,
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
                "note": heal_result.note,
            },
        )

    # Run the full launch checklist — which pings the channel — before any LLM (§9.8). The run reads
    # its configuration once, here, and binds its toolkit contract first, so the preflight, the
    # registry, the gate, the evidence predicate and the environments all read one contract.
    # The stores too (S4, ruling B): bound from the same configuration, each local one started and
    # reconciled before the preflight checks it; the run's store keys are held in `store_keys` from
    # here on and revoked whenever the run ends. One seam (`RunLaunch`) holds the step and its end,
    # so a cell driving roles outside the graph takes the same one.
    launch = RunLaunch(run_id, run_monitor)
    store_keys = launch.keys
    try:
        orch = await launch.start()
        await run_preflight(human_handler=human_handler, monitor=run_monitor)
    except BaseException:
        await launch.end()
        # A failed launch closes the channel the ping opened (D1c): it is all that runs yet.
        await _close_connector(human_handler, run_monitor)
        raise

    # From here on, what the setup opens is closed leaf to root if a later setup step fails or a
    # stop lands mid-setup (D1d). Once the graph is built the run's own teardown (the `finally`
    # below) owns each of them, and the stack lets go.
    setup = contextlib.AsyncExitStack()
    setup.push_async_callback(_close_connector, human_handler, run_monitor)
    # The store keys after every process that holds them has closed (leaf to root), before the channel.
    setup.push_async_callback(launch.revoke)
    run_control: RunControl | None = None
    _restore_signals = None
    sandboxes: list[PersistentSandbox] = []
    mcp_client: MCPToolClient | None = None
    held = contextlib.AsyncExitStack()  # the checkpointer, open until the run returns
    started = False  # `run.start` recorded: a failure from here on is the run's `run.error` (D1e)
    try:
        if max_iterations is None:
            max_iterations = int(orch.get("max_iterations", 3))
        debate_max_rounds = int(orch.get("debate_max_rounds", 3))
        tool_retry_cap = int(orch.get("tool_retry_cap", 3))
        toolkit_root = toolkit().root  # the process's bound toolkit contract, validated by the launch preflight
        await _reconcile_environments(toolkit_root, run_monitor)
        sandbox_settings = SandboxSettings.from_config(orch)
        # Every receipt names the environment it was produced in (env plan B1).
        try:
            # The analysis environment: the same object the REPL and the shell run in.
            stamp_run_environments(run_id, analysis=sandbox_settings.environment)
            env_error: Exception | None = None
        except Exception as exc:  # noqa: BLE001 — an unreadable env leaves receipts unstamped, never the run dead
            env_error = exc  # recorded after run.start, which opens the ledger
        disabled_tools: list[str] = orch.get("disabled_tools") or []
        default_formats: list[str] = orch.get("report_formats") or ["jsonl", "tex"]
        resolved_formats = _normalize_report_formats(report_formats, default_formats)

        # Run-wide control (HITL /steer, /stop). Threaded onto every role + the consensus strategy;
        # the command channel writes to it. A steer is read cooperatively by the ReAct loops; a stop
        # cancels the task registered here, so it needs no cooperation from anything.
        run_control = RunControl()
        launch.hold(run_control)  # the keys, revoked by the stop's cleanup too (leaf to root)
        run_control.register_run_task(asyncio.current_task())
        cleaned = False  # set once a stop's (or a channel failure's) cleanup has torn the run down
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
        attach_librarian_runtime(io_agent, run_id=run_id, run_control=run_control)

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
            run_control=run_control,
            retrieval=retrieval,
            web_source=web_source,
        )

        # V-4: a stable key + report folder for the lead's per-round adjudicated report JSONLs, its
        # monitor, its stop channel and its read-only tools — all through the one seam, so a live
        # drill driving a real lead outside the graph gets exactly this and cannot drift from it.
        # The lead reads the default store's catalog as itself: a login of its own, of the role the
        # store's `roles` gives it (reader by default: it runs no tool process, only catalog
        # queries the runtime runs on its behalf; S9).
        await wire_lead(
            lead,
            run_id=run_id,
            toolkit_root=toolkit_root,
            keys=store_keys,
            run_monitor=run_monitor,
            run_control=run_control,
            retrieval=retrieval,
            web_source=web_source,
        )

        _attach_monitor(reviewer, run_monitor)
        _attach_monitor(io_agent, run_monitor)
        for worker in workers:
            _attach_monitor(worker, run_monitor)

        # HITL /steer, /stop: the workers' stop channel (the reviewer, lead and librarian got theirs
        # from their seams above). Best-effort so duck-typed fakes are fine.
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

        # Start MCP client and index its tools into the registry (the builtin server in the
        # toolkit's default environment, so bash_exec can import the science stack).
        mcp_client = worker_mcp_client(
            workers, run_id=run_id, toolkit_root=toolkit_root, sandbox_settings=sandbox_settings,
        )
        setup.push_async_callback(_closed, "mcp", mcp_client.shutdown, run_monitor)  # a half-started one too
        await mcp_client.start()
        registry.load_mcp(mcp_client)

        if consensus is None:
            consensus = DebateToConverge(
                max_rounds=debate_max_rounds,
                monitor=run_monitor,
                registry=registry,
                tool_retry_cap=tool_retry_cap,
                # Phase-2 step-7 authoring/resume: the lead authors missing capability into
                # the toolkit on its authoring branch, then workers rerun (§9.10).
                tool_author_cap=int(orch.get("tool_author_cap", 3)),
                toolkit_root=toolkit_root,
                worktree_base=run_dir(run_id) / "authoring",
                direct_branch=toolkit().authoring_branch,
                human_handler=human_handler,
                run_control=run_control,
            )

        # Identity, memory, one persistent sandbox each and the registry — the wiring without which a
        # worker has no tool loop. `attach_worker_runtime` is the one definition of it, so anything
        # that drives a worker outside a full run (a probe, a benchmark) gets the same worker rather
        # than a look-alike. It also registers each sandbox's aborter, so a human /stop cancels an
        # in-flight tool call instead of waiting for a safe point hours away.
        # Each worker's own keys to the store, for its REPLs only (S4: one S3 user and one Postgres
        # login per unit per run).
        unit_keys = await issue_unit_keys(
            store_keys,
            [f"analyst_worker_{i}" for i, w in enumerate(workers) if isinstance(w, AnalystWorkerAgent)],
            run_monitor,
        )
        sandboxes = attach_worker_runtime(
            workers,
            run_id=run_id,
            registry=registry,
            toolkit_root=toolkit_root,
            sandbox_settings=sandbox_settings,
            mcp_client=mcp_client,
            run_control=run_control,
            credentials=unit_keys,
            reconciler=next((run_reconciler(store_keys, c.store) for c in unit_keys.values()), None),
        )
        for sandbox in sandboxes:
            setup.push_async_callback(_closed, "sandbox", sandbox.shutdown, run_monitor)

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
        started = True
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
        setup.push_async_callback(_closed, "checkpointer", held.aclose, run_monitor)
        saver = await held.enter_async_context(
            open_checkpointer(checkpoint_path(ledger_dir(run_id)))
        )
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
    except BaseException as exc:
        # The run ends here: it holds its contract and stores no longer (its keys are revoked below,
        # by the stop's cleanup or the setup's teardown).
        launch.release()
        # First, as the run's own teardown does (D8): a stop or a signal during the closes must
        # not cancel them and replace the setup's error with a bare cancel.
        if run_control is not None:
            run_control.register_run_task(None)
        if isinstance(exc, asyncio.CancelledError) and run_control is not None and run_control.stop_requested:
            # A stop during the setup takes a stop's path (D1e): the cleanup in this task, leaf to
            # root, recorded, its checklist sent, the connector closed last; the signal handlers
            # stay until it is done, as for a stop during the graph (a second signal is a no-op).
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            outer_pending = _spend_cancels(task)
            try:
                await _closed("checkpointer", held.aclose, run_monitor)
                await _interrupt_cleanup(
                    run_control, sandboxes=sandboxes, mcp_client=mcp_client,
                    human_handler=human_handler, monitor=run_monitor, outer_pending=outer_pending,
                )
            finally:
                if _restore_signals is not None:
                    _restore_signals()
            raise _user_interruption(run_control, run_id, run_monitor) from None
        if _restore_signals is not None:
            _restore_signals()
        try:
            if started and isinstance(exc, Exception):
                # Recorded as a failure inside the graph is (D1e), before the closes; a ledger
                # that cannot be written leaves the setup's error standing.
                try:
                    await run_monitor.record("run", "run.error", level=1, summary="Research run failed",
                                             data={"error": repr(exc)})
                except Exception:  # noqa: BLE001
                    pass
                await _record_run_summary(run_monitor)
        finally:
            await setup.aclose()
        raise
    setup.pop_all()  # the run is under way: the `finally` below closes these from here on
    try:

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
            # The graph is done: nothing may cancel this task from here on (a channel failure or
            # a stop arriving now is answered below or by the finished run, never by a cancel
            # landing in the teardown).
            run_control.register_run_task(None)
            if run_control.fatal is not None:
                # A channel task failed the run as its last node returned (D6).
                raise run_control.fatal
        except asyncio.CancelledError:
            fatal = run_control.fatal
            if fatal is not None and not run_control.stop_requested:
                # Ruling D6: a resumed seat's `ReactBudgetExhausted` cancelled the run task to
                # reach it. The interrupt's kill-and-repair runs (the authoring branch included),
                # recorded as the run's failure, and the run ends with it.
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                outer_pending = _spend_cancels(task)
                # Awaited in this task, not shielded into another: the cancel that delivered the
                # failure is spent, `fail` and `interrupt` send no second one, and the MCP client
                # (an anyio cancel scope) must be closed by the task that opened it.
                # Set first: an outer cancel the cleanup re-raises must not run the teardown twice.
                cleaned = True
                await _interrupt_cleanup(
                    run_control,
                    sandboxes=sandboxes,
                    mcp_client=mcp_client,
                    human_handler=human_handler,
                    monitor=run_monitor,
                    fatal=fatal,
                    outer_pending=outer_pending,
                )
                raise fatal from None
            if not run_control.stop_requested:
                raise  # not ours: an outer cancel (kernel shutdown, a caller's timeout)
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()  # the cancellation was the delivery mechanism; it ends here
            outer_pending = _spend_cancels(task)
            # A human stop (ruling D8: leaf to root). The cleanup runs in THIS task: the cancel
            # that delivered the stop is spent and `interrupt` sends no second one, and the MCP
            # client — an anyio cancel scope this task opened — can only be closed here. Shielding
            # it into another task failed that close and left the server running.
            cleaned = True  # first: an outer cancel the cleanup re-raises skips the `finally` teardown
            await _interrupt_cleanup(
                run_control,
                sandboxes=sandboxes,
                mcp_client=mcp_client,
                human_handler=human_handler,
                monitor=run_monitor,
                outer_pending=outer_pending,
            )
            raise _user_interruption(run_control, run_id, run_monitor) from None
        except Exception as exc:
            # The run is ending with this error: a /stop or a channel failure now must not cancel
            # its teardown and replace the error with a bare cancel (D8).
            run_control.register_run_task(None)
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
            run_control.register_run_task(None)
            if _restore_signals is not None:
                _restore_signals()
            if not cleaned:
                # A seat woken as the run ended (D5) would otherwise outlive it: its resume
                # restarts a sandbox and spends after `run.finish`. A stop's cleanup already did
                # all of this, bounded and recorded, and is not repeated.
                try:
                    await run_control.cancel_resumed(timeout=_INTERRUPT_STEP_TIMEOUT_S)
                except TimeoutError as exc:
                    await run_monitor.record(
                        "run", "run.teardown.timeout", level=2,
                        summary=f"Channel tasks outlived the run's teardown: {exc}",
                        data={"error": repr(exc)},
                    )
                for sandbox in sandboxes:
                    await _closed("sandbox", sandbox.shutdown, run_monitor)
                # The toolkit, before the tools server (leaf to root, D8): a coding session whose
                # close raised, or one still published, is closed again here (C2c).
                leftover = [run_control.authoring_session] if run_control.authoring_session else []
                leftover += run_control.unclosed_sessions
                run_control.authoring_session, run_control.unclosed_sessions = None, []
                for session in leftover:
                    try:
                        await asyncio.wait_for(session.aclose(), timeout=_INTERRUPT_RESET_TIMEOUT_S)
                    except Exception as exc:  # noqa: BLE001 — recorded; the teardown goes on
                        await run_monitor.record(
                            "run", "run.teardown.session_error", level=2,
                            summary=f"A coding session did not close at the run's end: {exc!r}",
                            data={"error": repr(exc)},
                        )
                await _closed("mcp", mcp_client.shutdown, run_monitor)
                # The store keys: every process holding them is gone (S4).
                await launch.revoke()
                # The channel last, as a stop's cleanup closes it (leaf to root, D8/D1c).
                await _close_connector(human_handler, run_monitor)
    finally:
        # Whatever ended the run (an outer cancel in the teardown, a stop step that timed out): no key
        # it issued outlives it. Idempotent: nothing left, nothing done.
        await launch.end()
        await _closed("checkpointer", held.aclose, run_monitor)  # as the run returns

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


async def _close_connector(human_handler: Any, monitor: Any) -> None:
    """Close the human-channel connector at a run's end or a failed launch (D1c); a close that
    raises is recorded, never the run's outcome. A handler with no close is left as it is."""
    closer = (getattr(human_handler, "aclose", None) or getattr(human_handler, "shutdown", None)
              or getattr(human_handler, "close", None))
    if closer is None:
        return
    try:
        closed = closer()
        if asyncio.iscoroutine(closed) or isinstance(closed, asyncio.Future):
            # Bounded as a stop's cleanup bounds it (a Telegram updater's stop can wait on one
            # last `getUpdates`).
            await asyncio.wait_for(closed, timeout=_INTERRUPT_STEP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — the run's own outcome stands
        try:
            await monitor.record("run", "run.teardown.connector_error", level=2,
                                 summary=f"The human-channel connector did not close: {exc!r}",
                                 data={"error": repr(exc)})
        except Exception:  # noqa: BLE001
            pass


async def _closed(what: str, close: Any, monitor: Any) -> None:
    """One teardown close (D1e): a close that raises is recorded as ``run.teardown.<what>_error``
    and never replaces the run's own outcome."""
    try:
        result = close()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
    except Exception as exc:  # noqa: BLE001 — the run's own outcome stands
        try:
            await monitor.record("run", f"run.teardown.{what}_error", level=2,
                                 summary=f"The run's {what} did not close: {exc!r}",
                                 data={"error": repr(exc)})
        except Exception:  # noqa: BLE001
            pass


def _user_interruption(run_control: RunControl, run_id: str, monitor: Any) -> UserInterruptionError:
    """What a stopped run raises to its caller: the reason, who stopped it, and its artifacts."""
    return UserInterruptionError(
        run_control.stop_reason,
        by=run_control.stopped_by or "human",
        artifacts={"run_dir": str(run_dir(run_id)), "ledger": str(monitor.jsonl_path or "")},
    )


async def _interrupt_cleanup(
    run_control: RunControl,
    *,
    sandboxes: Sequence[PersistentSandbox],
    mcp_client: MCPToolClient | None,
    human_handler: HumanHandler | None,
    monitor: RunMonitor,
    fatal: BaseException | None = None,
    outer_pending: bool = False,
) -> None:
    """Deterministic teardown for a human interrupt, leaf to root (ruling D8). **No model is
    called from here.** Runs in the run task, which opened the MCP client it closes.

    Order: the agents (resumed seats) → each sandbox and its children → the authoring branch
    (coder disconnected, tree reset, patch saved) → the MCP client → the ledger's closing record
    and summary → the landing checklist to the human (``notify``) → the HITL connector, last, since
    it is the channel. Every step is bounded and recorded (``run.interrupt.step`` /
    ``step_error`` / ``step_timeout`` / ``step_skipped`` with its reason); none is skipped without
    one. The checklist lists them in this order, ✓ or ✗ with the reason.

    Also the teardown of a run a channel failure ends (``fatal``, ruling D6): the same kill and
    repair, recorded as ``run.error`` rather than ``run.interrupted``.

    Why this order: *nothing may be able to write to the toolkit when the toolkit is repaired.*
    Resetting the authoring branch while the coder subprocess or a gate pytest run is still alive
    just lets them dirty it again a moment later. The repair is ``AuthoringSession.aclose()``'s
    existing failure path, reached here because its own ``finally`` sits inside the cancelled
    scope.

    Every step is bounded and guarded: a wedged step (a git lock, a D-state child on NFS, a
    connector waiting on the network) logs and yields to the next, because an interrupt that hangs
    in teardown is the failure this design exists to remove. A branch left dirty by a timeout is
    caught by the next launch's clean-tree preflight and by the resume-time heal.
    """

    steps: list[tuple[str, bool, str]] = []  # (what, done, detail), in teardown order
    # An outer cancel (a caller's timeout, a kernel interrupt) arriving mid-cleanup is held until
    # every step has run, then honoured: the cleanup is not shielded (it must run in this task),
    # so without this one cancel would abort it part-way with nothing recorded.
    me = asyncio.current_task()
    entry = me.cancelling() if me is not None else 0
    # An outer cancel that arrived together with the stop's own (the caller set it pending before
    # this ran) is honoured at the end too.
    outer: list[int] = [1] if outer_pending else []

    async def _step(what: str, name: str, coro, timeout: float | None = None) -> bool:
        """Run one bounded step; record it, and its line for the checklist, whatever happens."""
        limit = timeout or _INTERRUPT_STEP_TIMEOUT_S
        try:
            await asyncio.wait_for(coro, timeout=limit)
        except asyncio.CancelledError:
            if me is not None and me.cancelling() > entry:
                me.uncancel()
                outer.append(1)
                reason = "interrupted by an outer cancel; the cleanup went on"
            else:
                reason = "the step was cancelled"
            await _record(monitor, "run.interrupt.step_error", level=2,
                          summary=f"Interrupt cleanup step '{name}' cancelled — continuing",
                          data={"step": name, "error": reason})
            steps.append((what, False, reason))
            return False
        except (TimeoutError, asyncio.TimeoutError) as exc:
            # The step's own bound says what was still running; the outer bound says only that.
            detail = str(exc) or f"timed out after {limit:.0f}s"
            await _record(monitor, "run.interrupt.step_timeout", level=2,
                          summary=f"Interrupt cleanup step '{name}' timed out — continuing",
                          data={"step": name, "timeout_s": limit, "detail": detail})
            steps.append((what, False, detail))
            return False
        except Exception as exc:  # noqa: BLE001 — one failed step never blocks the rest
            await _record(monitor, "run.interrupt.step_error", level=2,
                          summary=f"Interrupt cleanup step '{name}' failed — continuing",
                          data={"step": name, "error": repr(exc)})
            steps.append((what, False, f"{type(exc).__name__}: {exc}"[:200]))
            return False
        await _record(monitor, "run.interrupt.step", level=3,
                      summary=f"Interrupt cleanup step '{name}' done", data={"step": name})
        steps.append((what, True, ""))
        return True

    async def _skipped(what: str, name: str, reason: str) -> None:
        await _record(monitor, "run.interrupt.step_skipped", level=3,
                      summary=f"Interrupt cleanup step '{name}' skipped: {reason}",
                      data={"step": name, "reason": reason})
        steps.append((what, False, f"skipped: {reason}"))

    # Leaf to root (ruling D8): what runs inside the run first, then what it runs in, the channel
    # to the human last. Every step is attempted and bounded; a failed or skipped one is recorded
    # with its reason and the next one still runs.
    #
    # 1 — the agents. Seats resumed by a message run their own loops beside the run task (E12);
    # the run task's own turns already ended with the cancel that delivered the stop.
    await _step("agents interrupted and closed", "cancel_resumed_seats",
                run_control.cancel_resumed(timeout=_INTERRUPT_STEP_TIMEOUT_S),
                timeout=_INTERRUPT_STEP_TIMEOUT_S + 1)

    # 2 — every process that can still write: each sandbox REPL and its descendants.
    if sandboxes:
        for index, sandbox in enumerate(sandboxes):
            await _step(f"sandbox {index} killed", f"kill_sandbox_{index}", sandbox.kill_now())
    else:
        await _skipped("sandboxes killed", "kill_sandboxes", "this run started no sandbox")

    # 3 — the toolkit: disconnect the coder, then reset the branch to the attempt's base ref
    # (saving the discarded work as a patch). Nothing can write to it any more.
    session = run_control.authoring_session
    if session is not None:
        run_control.authoring_session = None  # idempotent: never reset twice
        mark = _ledger_mark(monitor)
        raised: list[BaseException] = []

        async def _close() -> None:
            try:
                await session.aclose()
            except Exception as exc:
                raised.append(exc)
                raise

        closed = await _step("authoring branch reset", "authoring_reset", _close(),
                             timeout=_INTERRUPT_RESET_TIMEOUT_S)
        new = _ledger_since(monitor, mark)
        # `aclose` raises a failed disconnect only after its reset ran (C2b), so the reset and the
        # disconnect are two facts on the checklist (C2d). The reset line is read from the ledger
        # only when the close ran through: it returned, or it raised that disconnect error itself
        # (a timeout or a cancel mid-close keeps the step's own failure).
        dropped = [e for e in new if e.get("event") == "coder.disconnect.error"]
        through = closed or bool(
            raised and dropped and repr(raised[-1]) == (dropped[-1].get("data") or {}).get("error")
        )
        if through:
            # `aclose` reports a failed reset as an event, never a raise: read what it recorded.
            failed = [e for e in new if e.get("event", "").endswith(".error")
                      and e.get("event", "").startswith("coder.") and e not in dropped]
            patches = [e["data"]["patch"] for e in new
                       if e.get("event") == "coder.tree_reset" and (e.get("data") or {}).get("patch")]
            unsaved = [e for e in new if e.get("event") == "coder.tree_reset"
                       and (e.get("data") or {}).get("save_error")]
            if failed:
                steps[-1] = (steps[-1][0], False, failed[-1].get("summary", "reset failed"))
            elif unsaved:  # the tree was reset; its patch could not be saved (C2c)
                steps[-1] = (steps[-1][0], True, "reset; saving its patch failed")
            elif patches:
                steps[-1] = (steps[-1][0], True, f"patch saved: `{patches[-1]}`")
            elif not any(e.get("event") == "coder.tree_reset" for e in new):
                steps[-1] = (steps[-1][0], True, "nothing to reset: no unbanked changes")
            else:
                steps[-1] = (steps[-1][0], True, "")
        if dropped:
            steps.append(("coder disconnected", False, dropped[-1].get("summary", "disconnect failed")))
    else:
        await _skipped("authoring branch reset", "authoring_reset", "no tool was being authored")
    # Sessions whose close raised earlier (C2c): closed again, each its own bounded step.
    leftover, run_control.unclosed_sessions = run_control.unclosed_sessions, []
    for index, left in enumerate(leftover):
        await _step(f"unclosed coding session {index} closed", f"close_unclosed_{index}",
                    left.aclose(), timeout=_INTERRUPT_RESET_TIMEOUT_S)

    # 4 — the tools server (the shell): closed by this task, which opened it.
    if mcp_client is not None:
        await _step("MCP closed", "mcp_shutdown", mcp_client.shutdown())
    else:  # a stop during the setup, before the tools server was made (D1e)
        await _skipped("MCP closed", "mcp_shutdown", "this run started no tools server")

    # 4b — the store keys the run issued (S4): nothing that holds them runs any more.
    credentials = getattr(run_control, "credentials", None)
    if credentials is not None and credentials.pending():
        # Each key is a few IAM calls and a login's sessions ended (up to 5 s): the reset's budget.
        await _step("store keys revoked", "revoke_store_keys", credentials.revoke_all(),
                    timeout=_INTERRUPT_RESET_TIMEOUT_S)
    else:
        await _skipped("store keys revoked", "revoke_store_keys", "this run holds no store keys")

    # 5 — the record, then the checklist to the human while the channel is still open.
    if fatal is not None:
        await _record(
            monitor, "run.error", level=1, summary="Research run failed",
            data={"error": repr(fatal), "from": "channel"},
        )
    else:
        await _record(
            monitor, "run.interrupted", level=1,
            summary=f"Run interrupted by {run_control.stopped_by or 'human'} — no report composed",
            data={"reason": run_control.stop_reason, "by": run_control.stopped_by},
        )
    await _record_run_summary(monitor)
    # Commands still running when the channel closes are cancelled with it: listed, not silent.
    in_flight = getattr(human_handler, "in_flight_commands", None)
    cancelled = list(in_flight()) if callable(in_flight) else []
    if cancelled:
        await _record(monitor, "run.interrupt.commands_cancelled", level=3,
                      summary=f"Commands cancelled by the stop: {', '.join(cancelled)}",
                      data={"commands": cancelled})
    notify = getattr(human_handler, "notify", None)
    if notify is not None:
        await _step("checklist sent", "stop_checklist",
                    notify(_stop_checklist(steps, fatal, cancelled)))
    elif human_handler is not None:
        await _skipped("checklist sent", "stop_checklist", "the connector cannot post a notice")

    # 6 — the channel itself, last: nothing after it can reach the human.
    closer = (getattr(human_handler, "aclose", None) or getattr(human_handler, "shutdown", None)
              or getattr(human_handler, "close", None))
    if closer is not None:
        await _step("HITL connector closed", "hitl_shutdown", closer())
    elif human_handler is not None:
        await _skipped("HITL connector closed", "hitl_shutdown", "the connector has no close")
    if outer or (me is not None and me.cancelling() > entry):
        while me is not None and me.cancelling() > entry:
            me.uncancel()  # one CancelledError carries it; the count returns to where it was
        raise asyncio.CancelledError("an outer cancel arrived during the stop's cleanup")


def _ledger_mark(monitor: RunMonitor) -> tuple[int, int]:
    """Where the ledger ends now: (JSONL byte size, in-memory event count)."""
    path = getattr(monitor, "jsonl_path", None)
    size = path.stat().st_size if path is not None and path.exists() else 0
    return size, len(getattr(monitor, "events", ()))


def _ledger_since(monitor: RunMonitor, mark: tuple[int, int]) -> list[dict]:
    """The events recorded after ``mark``: from the JSONL (every level), else from memory."""
    path = getattr(monitor, "jsonl_path", None)
    if path is not None and path.exists():
        with path.open("rb") as handle:
            handle.seek(mark[0])
            tail = handle.read().decode("utf-8", errors="replace")
        events = []
        for line in tail.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events
    return [e.model_dump(mode="json") for e in list(getattr(monitor, "events", ()))[mark[1]:]]


def _spend_cancels(task: asyncio.Task | None) -> bool:
    """Spend any cancel still counted on ``task`` after the stop's own was delivered — an outer
    cancel (a caller's timeout) that landed with it — and say whether there was one, so the
    cleanup runs whole and honours it at the end."""
    pending = False
    while task is not None and task.cancelling() > 0:
        task.uncancel()
        pending = True
    return pending


def _stop_checklist(
    steps: list[tuple[str, bool, str]], fatal: BaseException | None, cancelled: list[str] = (),
) -> str:
    """The landing message: every teardown step so far, in order — ✓, ✗ with the reason it failed,
    or – with the reason it was skipped — then the commands the stop cancelled."""

    def line(what: str, done: bool, detail: str) -> str:
        if detail.startswith("skipped: "):
            return f"{what} – skipped ({detail.removeprefix('skipped: ')})"
        return f"{what} {'✓' if done else '✗'}" + (f" ({detail})" if detail else "")

    by = "the failure" if fatal is not None else "/stop"
    parts = [line(*step) for step in steps] + [
        f"{command} answer – cancelled by {by}" for command in cancelled
    ]
    head = (
        f"The run failed ({type(fatal).__name__}: {fatal}); cleanup:"
        if fatal is not None else "/stop landed; cleanup:"
    )
    ok = all(done or detail.startswith("skipped") for _, done, detail in steps)
    tail = "cleanup complete" if ok else "cleanup finished with failures"
    return f"{head}\n" + " → ".join([*parts, tail])


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
