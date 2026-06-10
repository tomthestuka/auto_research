"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auto_research.env import check_required_env
from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.builder import IOAgentLike, ReviewerLike, build_graph
from auto_research.monitor import RunMonitor
from auto_research.models import resolve
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend
from auto_research.sandbox.mcp_client import MCPToolClient
from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.sandbox.workspace import create_run_workspace, memory_path
from auto_research.schemas import ResearchRequest, ReviewVerdict
from auto_research.storage import Storage


DEFAULT_WORKER_COUNT = 2
DEFAULT_ORCHESTRATION_CONFIG = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"


@dataclass(frozen=True)
class ResearchRun:
    """Result object returned by the monitored library entry."""

    request: ResearchRequest
    verdict: ReviewVerdict
    final_state: dict[str, Any]
    monitor: RunMonitor
    research_report: Any = None  # ResearchReport | None — avoids circular import at class def
    report_jsonl: Path | None = None
    report_latex: Path | None = None
    report_pdf: Path | None = None

    def markdown(self) -> str:
        """Return the grouped monitor report."""

        return self.monitor.render_markdown(grouped=True)


def load_orchestration_config(path: str | Path = DEFAULT_ORCHESTRATION_CONFIG) -> dict:
    """Load iteration caps from orchestration.yaml."""
    with Path(path).open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _builtin_server_config(
    analyze_env: Path | None,
    analyze_ini_sh: str | None,
    toolkit_root: Path,
) -> dict:
    """Build the default builtin-MCP-server config dict.

    When ``analyze_env`` is set, the server subprocess is spawned from that
    env's Python interpreter so ``bash_exec`` commands can import astropy and
    xspec_utils.  The subprocess environment is populated from the analyst init
    script (HEADAS, CALDB, LD_LIBRARY_PATH) plus PYTHONPATH=toolkit_root so the
    research_toolkit packages are importable without the REPL.
    """
    from auto_research.sandbox.builtin_server import __file__ as _bs_file
    from auto_research.sandbox.runner import _source_ini_env

    builtin_path = str(Path(_bs_file).resolve())
    if analyze_env:
        python = str(analyze_env / "bin" / "python")
    else:
        import sys
        python = sys.executable

    # Build env from the analyst ini script — same pattern as PersistentSandbox.
    # Do NOT inherit os.environ: production envs may have conda activations or
    # other PATH/PYTHONPATH state we don't want.  analyze_ini_sh is the
    # authoritative source; its bash subprocess captures the clean HEASoft env
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

    return {
        "name": "builtin",
        "command": python,
        "args": [builtin_path],
        "env": env,
    }


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
    toolkit_root = Path(orch.get("toolkit_root", "/data/fzhou/research_toolkit"))
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
) -> ReviewVerdict:
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
    """Normalize report_formats to a validated list."""
    _valid = {"jsonl", "tex"}
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [value]
    unknown = set(value) - _valid
    if unknown:
        raise ValueError(f"Unknown report_formats: {sorted(unknown)}. Valid: {sorted(_valid)}")
    return list(value)


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
) -> ResearchRun:
    """Run the graph and return the verdict, final state, and monitor.

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
    """

    check_required_env()

    if isinstance(request, str):
        request = ResearchRequest(question=request)

    run_id = uuid.uuid4().hex
    run_monitor = monitor or RunMonitor(chatter=chatter, jsonl_path=monitor_jsonl)
    orch = load_orchestration_config()

    if max_iterations is None:
        max_iterations = int(orch.get("max_iterations", 3))
    debate_max_rounds = int(orch.get("debate_max_rounds", 3))
    tool_retry_cap = int(orch.get("tool_retry_cap", 3))
    toolkit_root = Path(orch.get("toolkit_root", "/data/fzhou/research_toolkit"))
    analyze_env_str = orch.get("analyze_env")
    analyze_env = Path(analyze_env_str) if analyze_env_str else None
    analyze_ini_sh: str | None = orch.get("analyze_ini_sh") or None
    disabled_tools: list[str] = orch.get("disabled_tools") or []
    report_retry_cap = int(orch.get("report_retry_cap", 2))
    default_formats: list[str] = orch.get("report_formats") or ["jsonl", "tex"]
    resolved_formats = _normalize_report_formats(report_formats, default_formats)

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
            AnalystWorkerAgent(model=handle, monitor=run_monitor, worker_index=i)
            for i, handle in enumerate(worker_handles)
        ]

    # Create per-agent workspace folders
    analyst_keys = [f"analyst_worker_{i}" for i in range(len(workers))]
    agent_keys = ["reviewer", "analyst_lead"] + analyst_keys
    create_run_workspace(run_id, agent_keys)

    # Assign memory paths to AnalystWorkerAgent instances (best-effort)
    for i, worker in enumerate(workers):
        if isinstance(worker, AnalystWorkerAgent):
            worker.memory_path = memory_path(run_id, analyst_keys[i])

    _attach_monitor(reviewer, run_monitor)
    _attach_monitor(io_agent, run_monitor)
    _attach_monitor(lead, run_monitor)
    for worker in workers:
        _attach_monitor(worker, run_monitor)

    registry = ToolkitRegistry(toolkit_root, disabled_tools=disabled_tools)

    # Start MCP client and index its tools into the registry.
    # When mcp_servers is empty/unset, build the default builtin-server config
    # using the analysis env python so bash_exec can import astropy/xspec_utils.
    mcp_server_configs: list[dict] | None = orch.get("mcp_servers") or None
    if not mcp_server_configs:
        mcp_server_configs = [_builtin_server_config(analyze_env, analyze_ini_sh, toolkit_root)]
    mcp_client = MCPToolClient(servers=mcp_server_configs)
    await mcp_client.start()
    registry.load_mcp(mcp_client)

    if consensus is None:
        consensus = DebateToConverge(
            max_rounds=debate_max_rounds,
            monitor=run_monitor,
            registry=registry,
            tool_retry_cap=tool_retry_cap,
        )

    # Spawn one persistent sandbox per AnalystWorkerAgent
    sandboxes: list[PersistentSandbox] = []
    for i, worker in enumerate(workers):
        if isinstance(worker, AnalystWorkerAgent):
            sandbox = PersistentSandbox(
                toolkit_root=toolkit_root,
                workdir=memory_path(run_id, analyst_keys[i]).parent,
                analyze_env=analyze_env,
                analyze_ini_sh=analyze_ini_sh,
                mcp_client=mcp_client,
            )
            worker.sandbox = sandbox
            sandboxes.append(sandbox)

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
        },
    )

    # Start all sandboxes before the graph runs
    for sandbox in sandboxes:
        await sandbox.start()

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
        storage=storage,
        report_retry_cap=report_retry_cap,
        report_formats=resolved_formats,
    )

    try:
        final_state: dict[str, Any] = await graph.ainvoke(
            {
                "request": request,
                "literature": [],
                "report": None,
                "verdict": None,
                "verdicts": [],
                "lit_iterations": 0,
                "analysis_iterations": 0,
                "max_iterations": max_iterations,
            }
        )
    except Exception as exc:
        await run_monitor.record(
            "run",
            "run.error",
            level=1,
            summary="Research run failed",
            data={"error": repr(exc)},
        )
        raise
    finally:
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
        },
    )
    return ResearchRun(
        request=request,
        verdict=verdict,
        final_state=final_state,
        monitor=run_monitor,
        research_report=final_state.get("research_report"),
        report_jsonl=final_state.get("report_jsonl"),
        report_latex=final_state.get("report_latex"),
        report_pdf=final_state.get("report_pdf"),
    )


def _attach_monitor(target: object, monitor: RunMonitor) -> None:
    if hasattr(target, "monitor") and getattr(target, "monitor", None) is None:
        try:
            setattr(target, "monitor", monitor)
        except Exception:
            pass
