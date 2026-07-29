"""High-level entry point for the Phase 1 research-loop graph."""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auto_research.preflight import run_preflight
from auto_research.hitl import HumanHandler, resolve_handler
from auto_research.agents import (
    AnalystLeadAgent,
    AnalystWorkerAgent,
    LibrarianIOAgent,
    ReviewerAgent,
)
from auto_research.consensus import ConsensusStrategy, DebateToConverge, LeadLike, WorkerLike
from auto_research.graph.builder import IOAgentLike, ReviewerLike, build_graph
from auto_research.graph.checkpoint import checkpoint_path, open_checkpointer
from auto_research.run_control import RunControl
from auto_research.monitor import RunMonitor
from auto_research.models import resolve
from auto_research.retrieval import RetrievalBackend, SqliteRetrievalBackend
from auto_research.sandbox.mcp_client import MCPToolClient
from auto_research.sandbox.persistent import PersistentSandbox
from auto_research.sandbox.registry import ToolkitRegistry
from auto_research.agents.react import ReadOnlyToolProvider
from auto_research.agents.roles import _model_label
from auto_research.sandbox.workspace import agent_dir, create_run_workspace, memory_path, run_dir
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
    path = Path(path)
    if not path.exists():
        raise RuntimeError(
            f"Config not found: {path}. Run `bash install.sh` from the repo root, or copy "
            f"{path.name}.example to {path.name} and edit it."
        )
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _builtin_server_config(
    analyze_env: Path | None,
    analyze_ini_sh: str | None,
    toolkit_root: Path,
    cwd: Path | None = None,
    analyze_probe: str | None = None,
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

    return {
        "name": "builtin",
        "command": python,
        "args": [builtin_path],
        "env": env,
        # Pin the server process cwd under the runtime data dir so nothing it does
        # relative to its own cwd can touch either repo (per-call bash_exec still
        # gets its own injected worker cwd; this is the by-construction backstop).
        **({"cwd": str(cwd)} if cwd is not None else {}),
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
    human_handler: HumanHandler | None = None,
    human_channel: str | None = None,
    run_id: str | None = None,
    resume: bool = False,
) -> ResearchRun:
    """Run the graph and return the verdict, final state, and monitor.

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
    run_monitor = monitor or RunMonitor(chatter=chatter, jsonl_path=monitor_jsonl)
    if resume and heal_result is not None and heal_result.healed:
        await run_monitor.record(
            "run", "run.resume.heal", level=1,
            summary="Resume: healed a dirty authoring branch left by a prior hard-kill",
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
    analyze_env_str = orch.get("analyze_env")
    analyze_env = Path(analyze_env_str) if analyze_env_str else None
    analyze_ini_sh: str | None = orch.get("analyze_ini_sh") or None
    repl_init_py: str | None = orch.get("repl_init_py") or None
    analyze_probe: str | None = orch.get("analyze_probe") or None
    disabled_tools: list[str] = orch.get("disabled_tools") or []
    report_retry_cap = int(orch.get("report_retry_cap", 2))
    default_formats: list[str] = orch.get("report_formats") or ["jsonl", "tex"]
    resolved_formats = _normalize_report_formats(report_formats, default_formats)

    # Run-wide control (HITL /steer, /stop). Threaded onto every role + the consensus strategy +
    # the graph edges below; the command channel (Stage 3.4) writes to it, the agents/graph read it.
    run_control = RunControl()

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
    agent_keys = ["reviewer", "analyst_lead"] + analyst_keys
    create_run_workspace(run_id, agent_keys)

    # Assign memory paths to AnalystWorkerAgent instances (best-effort). agent_key + report_dir
    # give the worker a stable figure-catalog source name and a folder for its per-round report
    # JSONLs (V-4); the lead addresses each worker's catalog by this key.
    for i, worker in enumerate(workers):
        if isinstance(worker, AnalystWorkerAgent):
            worker.memory_path = memory_path(run_id, analyst_keys[i])
            worker.agent_key = analyst_keys[i]
            worker.report_dir = agent_dir(run_id, analyst_keys[i])

    # Give the reviewer and lead a read-only inspection loop (run artifacts +
    # knowledge base), unless a caller pre-wired their own tools.
    from auto_research.sources import WebSource
    from auto_research.sources.downloads import papers_dir

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
    readonly_roots = [run_dir(run_id), papers_dir(), toolkit_root]
    if getattr(reviewer, "tools", None) is None:
        try:
            reviewer.tools = ReadOnlyToolProvider(
                roots=readonly_roots, retrieval=retrieval, web_source=web_source,
                human_handler=human_handler, asked_by="reviewer",
                model=_model_label(reviewer.model) or "",
            )
        except Exception:  # noqa: BLE001 - tools are optional; never block the run
            pass
    if getattr(lead, "tools", None) is None:
        try:
            lead.tools = ReadOnlyToolProvider(
                roots=readonly_roots, retrieval=retrieval, web_source=web_source,
                include_data_catalog=True,
                # §5.5: only the lead gets the budget-free plan_and_debate design channel, to
                # settle a toolkit design with the human before authoring (decide_authoring).
                include_plan_and_debate=True,
                # The paper knowledge base is the reviewer's surface, not the lead's:
                # the lead investigates toolkit source + the data catalog, not papers.
                include_text_search=False,
                human_handler=human_handler, asked_by="analyst_lead",
                model=_model_label(lead.model) or "",
            )
        except Exception:  # noqa: BLE001
            pass

    # V-4: give the lead a stable key + report folder for its per-round adjudicated report JSONLs.
    try:
        lead.agent_key = "analyst_lead"
        lead.report_dir = agent_dir(run_id, "analyst_lead")
    except Exception:  # noqa: BLE001 — duck-typed fakes may not accept it
        pass

    _attach_monitor(reviewer, run_monitor)
    _attach_monitor(io_agent, run_monitor)
    _attach_monitor(lead, run_monitor)
    for worker in workers:
        _attach_monitor(worker, run_monitor)

    # HITL /steer, /stop: give every role a stable steer key (workers/lead already keyed above)
    # and the shared RunControl its ReAct loop consults. Best-effort so duck-typed fakes are fine.
    for target, key in (
        (reviewer, "reviewer"), (io_agent, "librarian_io"), (lead, "analyst_lead")
    ):
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

        try:
            status_handle = resolve("status_monitor").handles[0]
        except Exception:  # noqa: BLE001 — optional role; /status falls back to a deterministic digest
            status_handle = None
        from auto_research.hitl.commands import CommandContext, make_status_answer

        jsonl_path = getattr(run_monitor, "jsonl_path", None)
        status_answer = (
            make_status_answer(status_handle, jsonl_path) if status_handle is not None else None
        )
        try:
            human_handler.attach_commands(
                CommandContext(
                    run_control=run_control,
                    roster=_roster,
                    monitor_jsonl_path=jsonl_path,
                    status_answer=status_answer,
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
                analyze_env, analyze_ini_sh, toolkit_root,
                cwd=run_dir(run_id), analyze_probe=analyze_probe,
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

    # Spawn one persistent sandbox per AnalystWorkerAgent and give it the registry
    # so its analyze() can run the inner ReAct tool loop (SandboxToolProvider).
    sandboxes: list[PersistentSandbox] = []
    for i, worker in enumerate(workers):
        if isinstance(worker, AnalystWorkerAgent):
            sandbox = PersistentSandbox(
                toolkit_root=toolkit_root,
                workdir=memory_path(run_id, analyst_keys[i]).parent,
                analyze_env=analyze_env,
                analyze_ini_sh=analyze_ini_sh,
                repl_init_py=repl_init_py,
                mcp_client=mcp_client,
            )
            worker.sandbox = sandbox
            worker.registry = registry
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
        "lit_iterations": 0,
        "analysis_iterations": 0,
        "max_iterations": max_iterations,
    }
    config = {"configurable": {"thread_id": run_id}}
    async with open_checkpointer(checkpoint_path(run_dir(run_id))) as saver:
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
            checkpointer=saver,
            run_control=run_control,
        )

        try:
            graph_input: Any = initial_state
            if resume:
                existing = await saver.aget_tuple(config)
                if existing is not None:
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
