# `auto_research.sandbox` — subprocess sandbox & tool registry

## Purpose

Two tightly coupled responsibilities:

1. **Tool registry** — discovers tools available in `research_toolkit` and
   describes them to analyst workers at dispatch time.
2. **Execution sandbox** — runs every tool call in an isolated subprocess with
   timeout, confined working directory, and `PYTHONPATH` limited to
   `research_toolkit`.

Tool-authoring (propose → branch → lead-review → human-approve → merge) is a
**Phase 2 stub** in `merge_flow.py`. Phase 2 implementation: Claude Agent SDK.
See [CLAUDE.md §8](../../../CLAUDE.md#8-runtime-tool-authoring--sandbox) and
[§13](../../../CLAUDE.md#13-phase-2-readiness).

## Architecture

```
sandbox/
├── registry.py    # ToolkitRegistry — manifest-based tool discovery
├── runner.py      # run_tool()     — subprocess sandbox
└── merge_flow.py  # propose_and_pr() — Phase 2 stub (NotImplementedError)
```

**`TOOL_MANIFEST` convention:** each research_toolkit package exposes a
module-level dict `TOOL_MANIFEST: dict[str, ToolSpec]` at its top level.
`ToolkitRegistry` discovers tools by importing these manifests lazily. Adding
a new tool to research_toolkit requires only adding it to `TOOL_MANIFEST` — no
changes to `auto_research`.

**Graph wiring:** `tool_dispatch_node` runs after `analysis_node`. It calls
`worker.request_tools(task)` on the first worker, checks each
`ToolCallRequest` against the registry, and populates `state["tool_queue"]`.
`tool_execution_node` then runs each queued tool via `runner.run_tool()`,
appending `ToolResult` objects to `state["tool_results"]` (accumulated via
`operator.add`). On failure, the worker is asked to revise its arguments (LLM
self-correction), up to `tool_retry_cap` attempts.

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

### `run_tool`

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
) -> ToolResult
```

- Spawns an isolated Python subprocess. When `analyze_env` is provided (a
  conda/venv prefix), uses `{analyze_env}/bin/python` as the interpreter so
  tools run in the analysis environment (e.g. one with XSPEC/HEASoft). Falls
  back to `sys.executable` (the orchestration env) when `None`.
- The subprocess inherits the parent process environment so HEADAS, CALDB, and
  LD_LIBRARY_PATH are available. PYTHONPATH is overridden to `toolkit_root`.
- `workdir` is created if absent; the subprocess `chdir`s there on startup.
- On timeout: `exit_code=124`, `stderr="Timed out after Ns"`.
- Figures returned by the tool as `{"figures": [...]}` in stdout are parsed
  into `ToolResult.figures` as `FigureRef` objects.

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

- External: stdlib only (`asyncio`, `subprocess`, `importlib`, `sys`).
- Internal: `auto_research.schemas` (`ToolCallRequest`, `ToolResult`,
  `FigureRef`).
- Depended on by: `auto_research.graph.builder` (nodes `tool_dispatch_node`
  and `tool_execution_node`).

## Dictionary-level reference

- `ToolSpec` — TypedDict with `module`, `fn`, `description`, `args_schema`.
- `ToolkitRegistry(toolkit_root)` — registry instance.
  - `.load()` — import `TOOL_MANIFEST` from each known package. Idempotent.
  - `.is_known(name)` → `bool` — True if name is in any loaded manifest.
  - `.list_tools()` → `list[str]` — names of all registered tools.
  - `.describe_tool(name)` → `str` — `"<name>: <description>"` for prompts.
  - `.spec(name)` → `ToolSpec` — raises `KeyError` if unknown.
  - `.tool_catalogue()` → `str` — multi-line listing for prompt injection.
- `run_tool(module, fn, args, *, timeout_s, workdir, toolkit_root, analyze_env=None, analyze_ini_sh=None)` →
  `ToolResult` — async subprocess sandbox entry point. `analyze_env` selects
  the Python interpreter; `analyze_ini_sh` (bash script path) is sourced to
  inject HEADAS/CALDB/LD_LIBRARY_PATH; PYTHONPATH is always overridden to
  toolkit_root.
- `_source_ini_env(ini_sh)` → `dict` — sources a bash init script via
  `bash -c 'source … && env -0'`, parses the result, and caches it. Falls back
  to `os.environ` if sourcing fails.
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
