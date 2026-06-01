# `auto_research.sandbox` — subprocess sandbox, tool registry, and run workspace

## Purpose

Three tightly coupled responsibilities:

1. **Tool registry** — discovers tools available in `research_toolkit` and
   describes them to analyst workers at dispatch time.
2. **Execution sandbox** — runs every tool call in a **persistent subprocess**
   per analyst worker so XSPEC session state (loaded spectra, fitted models,
   plots) accumulates across all debate rounds and rework cycles.
3. **Run workspace** — per-run and per-agent folder layout under
   `$AUTO_RESEARCH_RUNTIME_DIR/runs/{run_id}/`; per-agent `memory.md` files
   record tool call history as bare terminal recordings.

Tool-authoring (propose → branch → lead-review → human-approve → merge) is a
**Phase 2 stub** in `merge_flow.py`. Phase 2 implementation: Claude Agent SDK.
See [CLAUDE.md §8](../../../CLAUDE.md#8-runtime-tool-authoring--sandbox) and
[§13](../../../CLAUDE.md#13-phase-2-readiness).

## Architecture

```
sandbox/
├── registry.py       # ToolkitRegistry — manifest-based tool discovery
├── persistent.py     # PersistentSandbox — long-lived REPL subprocess per worker
├── runner.py         # run_tool()     — one-shot subprocess (kept for ad-hoc use)
├── workspace.py      # per-run/agent folder layout and memory.md utilities
├── analyst_env.sh    # dedicated headless analyst env (HEADAS, CALDB, no cruft)
├── builtin_server.py # MCP stdio server: list_dir, read_file (file-tree only)
├── mcp_client.py     # MCPToolClient — manages stdio MCP server sessions
└── merge_flow.py     # propose_and_pr() — Phase 2 stub (NotImplementedError)
```

**`TOOL_MANIFEST` convention:** each research_toolkit package exposes a
module-level dict `TOOL_MANIFEST: dict[str, ToolSpec]` at its top level.
`ToolkitRegistry` discovers tools by importing these manifests lazily. Adding
a new tool to research_toolkit requires only adding it to `TOOL_MANIFEST` — no
changes to `auto_research`.

**Tool execution wiring:** Tool execution now lives inside `DebateToConverge`
rather than in graph nodes.  After each round of `analyze()` calls, each
worker with an attached `PersistentSandbox` calls `execute_tools()`, which
calls `request_tools()` (LLM), runs each request through the sandbox, and
appends a bare terminal recording to the worker's private `memory.md`.
Truncated per-worker summaries are then injected into `task.instructions` for
the next debate round so workers can reason from each other's results.  The
error-feedback retry loop (up to `tool_retry_cap` attempts) lives inside
`AnalystWorkerAgent.execute_tools()`.

**`is_alive` flag:** if a worker's subprocess exits unexpectedly, `is_alive`
becomes `False` and `execute_tools()` returns immediately — the worker
continues analyzing (via memory) but stops calling tools for the rest of the
session.

## Interfaces / Contracts

### `ToolSpec` (TypedDict)

```python
class ToolSpec(TypedDict):
    module: str       # importable module path, e.g. "xspec_utils.lib.NuSTAR"
    fn: str           # callable name within the module
    description: str  # human-readable; injected into analyst worker prompt
    args_schema: dict # JSON Schema for the args dict
```

### `ToolkitRegistry`

```python
class ToolkitRegistry:
    def __init__(self, toolkit_root: Path) -> None: ...
    def load(self) -> None                           # idempotent
    def is_known(self, tool_name: str) -> bool
    def list_tools(self) -> list[str]
    def describe_tool(self, tool_name: str) -> str
    def spec(self, tool_name: str) -> ToolSpec       # raises KeyError if unknown
    def tool_catalogue(self) -> str                  # multi-line for prompt injection
```

Invariants:
- `load()` prepends `toolkit_root` to `sys.path` only during import, then
  removes it. No ambient path pollution.
- Tools in packages not in `_MANIFEST_MODULES` are silently skipped; add the
  package name to `_MANIFEST_MODULES` to enable discovery.

### `PersistentSandbox`

```python
class PersistentSandbox:
    def __init__(
        self,
        toolkit_root: Path,
        workdir: Path,
        *,
        analyze_env: Path | None = None,
        analyze_ini_sh: str | None = None,
        default_timeout_s: int = 300,
    ) -> None: ...
    async def start(self) -> None          # idempotent; spawn the REPL subprocess
    async def call_tool(
        self, module, fn, args, *, timeout_s=None
    ) -> dict                              # send one call; return result dict
    async def shutdown(self) -> None       # graceful stop; kill if needed
    @property
    def is_alive(self) -> bool             # False after death; not recoverable
```

- Spawns one long-lived subprocess that runs a JSON-line REPL loop.  The subprocess
  stays alive across all debate rounds and rework cycles; XSPEC session state persists.
- `call_tool()` sends `{"module": ..., "fn": ..., "args": {...}}\n` to stdin and reads
  one JSON response line from stdout.  On timeout or death `is_alive` becomes `False`.
- Uses `{analyze_env}/bin/python` when supplied; sources `analyze_ini_sh`
  (`analyst_env.sh`) via `_source_ini_env()` for HEADAS/CALDB/LD_LIBRARY_PATH.
  PYTHONPATH is overridden to `toolkit_root`.

### Workspace utilities

```python
def create_run_workspace(run_id: str, agent_keys: list[str]) -> dict[str, Path]
def memory_path(run_id: str, agent_key: str) -> Path
def figures_dir(run_id: str, agent_key: str) -> Path
def run_dir(run_id: str) -> Path
def append_memory(path: Path, text: str) -> None
def read_memory(path: Path) -> str
```

- `create_run_workspace()` creates `$AUTO_RESEARCH_RUNTIME_DIR/runs/{run_id}/{key}/figures/`
  for each agent key and returns `{key: agent_dir}`.
- `memory_path()` returns `{agent_dir}/memory.md` (parent created if absent).
- `append_memory()` appends bare terminal text; `read_memory()` returns contents or `""`.

### `run_tool` (one-shot, kept for ad-hoc use)

```python
async def run_tool(
    module: str,
    fn: str,
    args: dict,
    *,
    timeout_s: int = 300,
    workdir: Path,
    toolkit_root: Path,
    analyze_env: Path | None = None,
    analyze_ini_sh: str | None = None,
) -> ToolResult
```

Not used by the main orchestration loop (replaced by `PersistentSandbox`).
Kept for standalone testing and ad-hoc tool invocation.

### `propose_and_pr` (Phase 2 stub)

```python
async def propose_and_pr(
    request: ToolCallRequest,
    *,
    lead: object,
    toolkit_repo: str = "tomthestuka/research_toolkit",
) -> str  # PR URL
```

Always raises `NotImplementedError` in Phase 1. Phase 2: Claude Agent SDK
writes the tool, lead reviews, human approves merge on GitHub.

## Dependencies

- External: stdlib only (`asyncio`, `subprocess`, `importlib`, `sys`, `json`).
- Internal: `auto_research.schemas` (`ToolCallRequest`, `ToolResult`, `FigureRef`).
- Depended on by: `auto_research.agents.roles` (`AnalystWorkerAgent.execute_tools()`);
  `auto_research.graph.runtime` (sandbox lifecycle); `auto_research.consensus`
  (tool execution inside `DebateToConverge._run_tools()`).

## Dictionary-level reference

- `ToolSpec` — TypedDict with `module`, `fn`, `description`, `args_schema`.
- `ToolkitRegistry(toolkit_root)` — registry instance.
  - `.load()` — import `TOOL_MANIFEST` from each known package. Idempotent.
  - `.is_known(name)` → `bool` — True if name is in any loaded manifest.
  - `.list_tools()` → `list[str]` — names of all registered tools.
  - `.describe_tool(name)` → `str` — `"<name>: <description>"` for prompts.
  - `.spec(name)` → `ToolSpec` — raises `KeyError` if unknown.
  - `.tool_catalogue()` → `str` — multi-line listing for prompt injection.
- `PersistentSandbox(toolkit_root, workdir, *, analyze_env, analyze_ini_sh, default_timeout_s)` —
  long-lived REPL subprocess per analyst worker.
  - `.start()` — spawn; idempotent.
  - `.call_tool(module, fn, args, *, timeout_s)` → `dict` — one tool call.
  - `.shutdown()` — graceful stop.
  - `.is_alive` → `bool` — False after death; not recoverable.
- `create_run_workspace(run_id, agent_keys)` → `dict[str, Path]` — create workspace folders.
- `memory_path(run_id, agent_key)` → `Path` — path to agent's `memory.md`.
- `figures_dir(run_id, agent_key)` → `Path` — path to agent's `figures/` dir.
- `run_dir(run_id)` → `Path` — path to run's root dir.
- `append_memory(path, text)` — append to memory.md.
- `read_memory(path)` → `str` — read memory.md or return `""`.
- `run_tool(module, fn, args, *, timeout_s, workdir, toolkit_root, analyze_env=None, analyze_ini_sh=None)` →
  `ToolResult` — one-shot subprocess; kept for ad-hoc use.
- `_source_ini_env(ini_sh)` → `dict` — sources a bash init script, caches result.
- `propose_and_pr(request, *, lead, toolkit_repo)` → `str` — Phase 2 stub;
  raises `NotImplementedError`.

## Changelog

- 2026-05-28 — Step 11 implemented: `ToolkitRegistry`, `run_tool()`, and
  `propose_and_pr()` Phase 2 stub. `TOOL_MANIFEST` convention established.
  `merge_flow.py` raises `NotImplementedError` (Claude Agent SDK in Phase 2).
- 2026-05-26 — Folder seeded during step 0.
- 2026-05-28 — Added `analyze_env` parameter to `run_tool()`. Subprocess now
  uses `{analyze_env}/bin/python` when provided, and inherits the parent
  process environment (HEADAS, CALDB, LD_LIBRARY_PATH, etc.) with PYTHONPATH
  overridden to toolkit_root. Configured via `analyze_env` in
  `config/orchestration.yaml`.
- 2026-05-28 — Added `analyze_ini_sh` parameter to `run_tool()` and
  `_source_ini_env()` helper. When provided, the bash init script is sourced
  once (NUL-separated `env` capture, cached) to inject HEADAS, CALDB,
  LD_LIBRARY_PATH, etc. into the subprocess env before PYTHONPATH override.
  Configured via `analyze_ini_sh` in `config/orchestration.yaml`.
- 2026-05-28 — Replaced one-shot graph-node tool dispatch with persistent
  per-worker sandbox.  Added `persistent.py` (`PersistentSandbox`): long-lived
  REPL subprocess; JSON-line stdin/stdout protocol; `is_alive` property; no
  subprocess recovery on death.  Added `workspace.py`: per-run/agent folder
  creation, `memory_path()`, `figures_dir()`, `append_memory()`, `read_memory()`.
  Tool execution moved into `DebateToConverge._run_tools()` via
  `AnalystWorkerAgent.execute_tools()`.  `run_tool()` retained for ad-hoc use.
  `sandbox/__init__.py` updated to export new classes.
