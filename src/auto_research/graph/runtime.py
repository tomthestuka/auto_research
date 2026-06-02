"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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

    # Inherit current process env, overlay HEASoft/CALDB vars from ini script,
    # then set PYTHONPATH so toolkit packages are importable.
    env = dict(os.environ)
    if analyze_ini_sh:
        env.update(_source_ini_env(analyze_ini_sh))
    env["PYTHONPATH"] = str(toolkit_root)
    # Ensure the analysis env's bin is first on PATH for `python` resolution.
    if analyze_env:
        env["PATH"] = str(analyze_env / "bin") + os.pathsep + env.get("PATH", "")

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

    storage = Storage()
    worker_group = resolve("analyst_worker")
    workers = [AnalystWorkerAgent(model=h) for h in worker_group.handles[:DEFAULT_WORKER_COUNT]]
    registry = ToolkitRegistry(toolkit_root)

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
    )
    return run.verdict


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
) -> ResearchRun:
    """Run the graph and return the verdict, final state, and monitor.

    ``max_iterations`` and ``debate_max_rounds`` are read from
    ``config/orchestration.yaml`` when not supplied explicitly.

    Missing agent dependencies are constructed from ``config/models.yaml``.
    Local retrieval uses ``SqliteRetrievalBackend`` by default (no LLM call).
    ``chatter`` follows the monitor's 1-10 verbosity scale; 10 records the full
    agent/tool/consensus narrative.

    A per-run workspace is created under
    ``$AUTO_RESEARCH_RUNTIME_DIR/runs/{run_id}/`` for each agent.  One
    ``PersistentSandbox`` is spawned per analyst worker; all sandboxes are torn
    down after the graph completes or raises.
    """

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

    registry = ToolkitRegistry(toolkit_root)

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
    return ResearchRun(request=request, verdict=verdict, final_state=final_state, monitor=run_monitor)


def _attach_monitor(target: object, monitor: RunMonitor) -> None:
    if hasattr(target, "monitor") and getattr(target, "monitor", None) is None:
        try:
            setattr(target, "monitor", monitor)
        except Exception:
            pass
