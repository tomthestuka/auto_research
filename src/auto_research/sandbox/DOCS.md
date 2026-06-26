# `auto_research.sandbox` — subprocess sandbox, tool registry, and run workspace

## Purpose

Three tightly coupled responsibilities:

1. **Tool registry** — discovers tools available in `research_toolkit` and
   describes them to analyst workers at dispatch time.
2. **Execution sandbox** — runs every tool call in a **persistent subprocess**
   per analyst worker so XSPEC session state (loaded spectra, fitted models,
   plots) accumulates across all debate rounds and rework cycles.
3. **Run workspace** — per-run and per-agent folder layout under
   `<runtime_dir>/runs/{run_id}/` (`runtime_dir` from `config/orchestration.yaml`); per-agent `memory.md` files
   record tool call history as bare terminal recordings.

Tool-authoring (propose → branch → gate → review → commit → human-merge) lives in
`merge_flow.py` (author harness + rework loop, step 2; **`approve_edit` atomic commit, step
6**), `gate.py` (the deterministic mergeability gate, step 3), and `review.py` (the
provider-decorrelated co-lead review, step 4). On gate-pass ∧ review-pass, `approve_edit`
lands one atomic commit on the working branch; the runtime never merges to master.
See [CLAUDE.md §9](../../../CLAUDE.md#9-phase-2--tool-authoring-the-active-phase).

## Architecture

```
sandbox/
├── registry.py       # ToolkitRegistry — manifest-based tool discovery
├── persistent.py     # PersistentSandbox — long-lived REPL subprocess per worker
├── runner.py         # run_tool()     — one-shot subprocess (kept for ad-hoc use)
├── workspace.py      # per-run/agent folder layout and memory.md utilities
├── analyst_env.sh    # dedicated headless analyst env (HEADAS, CALDB, no cruft)
├── builtin_server.py # MCP stdio server: bash_exec, list_dir, read_file
├── mcp_client.py     # MCPToolClient — manages stdio MCP server sessions
├── merge_flow.py     # propose_and_pr() — author harness + rework loop (step 2);
│                     #   approve_edit() — atomic commit (step 6); git-identity preflight
├── gate.py           # run_gate() — deterministic mergeability gate (step 3)
└── review.py         # make_review_hook() — provider-decorrelated co-lead review (step 4)
```

**`TOOL_MANIFEST` convention:** each research_toolkit package exposes a
module-level dict `TOOL_MANIFEST: dict[str, ToolSpec]` at its top level.
`ToolkitRegistry` discovers tools by importing these manifests lazily. Adding
a new tool to research_toolkit requires only adding it to `TOOL_MANIFEST` — no
changes to `auto_research`.

**`tool_config.yaml` convention (optional, per package):** a pure-data YAML at a
`<domain>_utils/` package root, read (never imported) by the registry. Today it declares
per-tool **sandbox call-timeout ceilings** — the sandbox SIGTERM-kills a tool call at this
many seconds (exit 124) regardless of any timeout inside the tool, so a genuinely slow tool
(e.g. a HEASARC download) must raise its ceiling above the global default:
```yaml
call_timeout_s:        # seconds, keyed by the tool's short name (manifest key's last segment)
  acquire: 1800
default_call_timeout_s: 300   # optional per-package fallback for tools not listed
```
Precedence: per-tool `call_timeout_s` > per-package `default_call_timeout_s` > the global
`orchestration.yaml: sandbox_call_timeout_s` (default 300). No upper clamp — this is trusted
human/coder-set infra, never LLM input. This keeps timeout policy **modular** (it travels with
the package; the orchestration layer never names toolkit tools) and **hand-editable** (a YAML
file, not a Python `TOOL_MANIFEST` dict). A call may request a shorter timeout via a numeric
`timeout_s` argument, but never one longer than the ceiling (`SandboxToolProvider.execute`).

**Tool execution wiring:** Tool execution lives inside each
`AnalystWorkerAgent.analyze()` call. The worker's inner ReAct loop uses
`SandboxToolProvider`, which exposes `ToolkitRegistry.tool_definitions()` and MCP
tool schemas to provider-native function calling, dispatches returned calls into
`PersistentSandbox`, and appends bare terminal recordings to the worker's private
`memory.md`. `DebateToConverge` reads each worker's `last_tool_results` and injects
truncated peer summaries into later rounds.

**`is_alive` flag and recovery:** if a worker's subprocess exits unexpectedly
or times out, the failing `call_tool()` returns a non-zero result and
`PersistentSandbox` starts a fresh REPL before subsequent tool calls.  The new
process has a clean XSPEC session, so analysts must reload any state they need.

**MCP tool path:** alongside `TOOL_MANIFEST` tools, analyst workers may call
tools served by stdio MCP servers.  `builtin_server.py` runs as a subprocess
and currently exposes `bash_exec`, `list_dir`, and `read_file`. Restricting MCP to
read-only file-tree exploration is deferred beyond Phase 2.
`MCPToolClient`
manages the server sessions and exposes tools to the registry under the naming
convention `mcp.<server_name>.<tool_name>` (e.g. `mcp.builtin.list_dir`).
`PersistentSandbox.call_mcp_tool()` forwards calls through the attached client.
`ToolkitRegistry.load_mcp(client)` indexes MCP tools alongside manifest tools
so the single `tool_catalogue()` string given to workers describes both.
`SandboxToolProvider` dispatches MCP tools via `registry.is_mcp(kind)`, so they use
the same ReAct and memory-recording path as manifest tools.

**Analyst env script:** `analyst_env.sh` is a minimal bash script sourced by
`_source_ini_env()` before each persistent subprocess is spawned.  It sets only
the analysis variables needed headlessly (HEADAS, CALDB, CALDBCONFIG, CALDBALIAS,
GEOMAG_PATH) with no SSH agent, gnome-keyring, or interactive conda activation.
`config/orchestration.yaml::analyze_ini_sh` points at this file.

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
    def call_timeout_ceiling(self, tool_name) -> int # sandbox per-call timeout (tool_config.yaml override, else orchestration.yaml default; no clamp)
    def tool_catalogue(self) -> str                  # multi-line text listing
    def tool_definitions(self) -> list[dict]         # provider-neutral function defs
    def kind_for_function(self, function_name) -> str  # reverse the name sanitization
```

`tool_definitions()` returns `{"name", "description", "input_schema"}` for every
toolkit tool (its `args_schema`) and MCP tool (its `inputSchema`), for
provider-native **function calling** in `AgentRole._react`.
Because provider function names must match `^[A-Za-z0-9_-]{1,64}$`, the dotted
`kind` is sanitized (dots → `_`) for `name`; `kind_for_function()` maps a
returned tool-call name back to the canonical `kind` the sandbox routes on.

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
    async def restart(self) -> None        # kill current subprocess and spawn fresh
    async def call_tool(
        self, module, fn, args, *, timeout_s=None
    ) -> dict                              # send one call; return result dict
    async def shutdown(self) -> None       # graceful stop; kill if needed
    @property
    def is_alive(self) -> bool             # True while current subprocess is running
```

- Spawns one long-lived subprocess that runs a JSON-line REPL loop.  The subprocess
  stays alive across all debate rounds and rework cycles; XSPEC session state persists.
- At subprocess boot, the REPL imports PyXSPEC when available and sets
  `Fit.query = "no"`, `Xset.allowPrompting = False`, and `Xset.chatter = 5`
  exactly once for that process.  The first two match the reference notebook
  behavior and prevent XSPEC file-name prompts from blocking the JSON protocol;
  `chatter = 5` lowers XSPEC console verbosity to reduce captured-output volume.
- `call_tool()` sends `{"module": ..., "fn": ..., "args": {...}}\n` to stdin and reads
  one JSON response line from stdout.  On timeout or death it kills the current
  process, starts a replacement, and returns the failed result for the call that
  triggered recovery.
- **Protocol integrity:** the REPL keeps a private duplicate of the original stdout fd
  for JSON responses only, and redirects fd 1/2 to a capture file (OS level) for the
  duration of each tool call.  This prevents tool/C-level console output (XSPEC,
  ixpeobssim) from polluting the protocol stream — otherwise one noisy call corrupts
  the line parser and kills the sandbox.  A truncated `console_tail` of captured output
  is attached to the result dict for diagnosis.
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

- `create_run_workspace()` creates `<runtime_dir>/runs/{run_id}/{key}/figures/`
  for each agent key and returns `{key: agent_dir}`.
- `memory_path()` returns `{agent_dir}/memory.md` (parent created if absent).
- `append_memory()` appends bare terminal text; `read_memory()` returns contents or `""`.

### `MCPToolClient`

```python
class MCPToolClient:
    def __init__(self, servers: list[dict] | None = None) -> None: ...
    async def start(self) -> None          # spawn server subprocesses; establish sessions
    def list_kinds(self) -> list[str]      # all registered MCP tool kinds
    def describe(self, kind: str) -> str   # description string for a kind
    def schema(self, kind: str) -> dict    # input JSON schema for a kind
    async def call_tool(self, kind: str, arguments: dict) -> dict
    async def shutdown(self) -> None       # close all sessions; terminate servers
```

- `servers` is a list of `{"name": str, "command": str, "args": list[str]}` dicts.
  Defaults to a single `builtin` server running `builtin_server.py` via
  `sys.executable`.
- Tool kinds follow the convention `mcp.<server_name>.<tool_name>`.
- `call_tool()` returns a dict with at minimum `stdout`, `stderr`, `exit_code`.
  Non-zero `exit_code` or `result.isError=True` is surfaced in the same `ToolResult`
  schema used by manifest tools, so the retry loop applies equally.

### Built-in MCP server (`builtin_server.py`)

Currently exposes shell execution plus file-tree exploration. Runs in the
`auto_research` Python env (not the analysis env) and inherits the parent
process environment so HEADAS/CALDB paths are visible to `bash_exec` commands.
Restricting MCP to read-only filesystem tools is a documented P2 task.

Tools:
- `bash_exec(command, cwd=None, timeout_s=60)` → `{"stdout": str, "stderr": str, "exit_code": int}`
  — run a shell command through the MCP server process. Its tool description scopes it to
  **read-only inspection** (listings, headers, shapes, paths, env probes); real analysis steps and
  any persisted result go through toolkit tools (a missing capability is raised as a `tool_request`).
- `list_dir(path)` → `{"path": str, "entries": list[str]}` — alphabetically sorted
  directory listing.
- `read_file(path, max_bytes=65536)` → `{"content": str, "truncated": bool, "size_bytes": int}`
  — read a text file up to `max_bytes`; UTF-8 with `errors="replace"`.

Kind strings: `mcp.builtin.bash_exec`, `mcp.builtin.list_dir`,
`mcp.builtin.read_file`.

### `PersistentSandbox.call_mcp_tool`

```python
async def call_mcp_tool(
    self,
    kind: str,
    args: dict,
    *,
    timeout_s: int | None = None,
) -> dict
```

Forwards a tool call to the attached `MCPToolClient`.  Returns an error dict
(`exit_code=127`) if no client is configured.  Timeout is not enforced at this
layer — the MCP session itself handles it.

### `ToolkitRegistry.load_mcp` / `is_mcp`

```python
def load_mcp(self, client: MCPToolClient) -> None  # index MCP tools into registry
def is_mcp(self, kind: str) -> bool                # True if kind is an MCP tool
```

Call `load_mcp(client)` after `client.start()` to merge MCP kinds into the registry.
`tool_catalogue()` includes both manifest and MCP tools in one string.
`is_mcp()` is used by `SandboxToolProvider` to route dispatch to `call_mcp_tool()`.

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
Kept for standalone testing and ad-hoc tool invocation.  Its public API remains
async, but internally it uses bounded synchronous `subprocess.run()` for stable
stdin/timeout behavior in the local Python environment.

### Isolation test

`AUTO_RESEARCH_LIVE_SANDBOX_ISOLATION_TESTS=1 pytest tests/test_sandbox_isolation.py -s`
starts two `PersistentSandbox` instances with the configured analyst env, loads
IXPE obsid `02008401` in the first subprocess, and verifies that
`xspec_status()` in the second subprocess reports an empty XSPEC session. Logs
are written under `logs/dev/step-11/`.

`AUTO_RESEARCH_LIVE_SANDBOX_POLICY_TESTS=1 pytest tests/test_live_sandbox_policy.py -s`
starts two configured analyst subprocesses in the live HEASoft/PyXSPEC
environment and verifies both boot with `Fit.query == "no"` and
`Xset.allowPrompting is False`.

### `propose_and_pr` (Phase 2 step 2 — author harness + rework loop)

```python
async def propose_and_pr(
    tool_spec: ToolSpec,
    *,
    lead,                       # AnalystLeadAgent — ask_lead handler (clarify_for_author)
    toolkit_root: Path,
    worktree_base: Path,        # isolated worktree location (under the runtime data dir)
    colead=None,                # AnalystColeadAgent — the GPT co-lead reviewer (step 4); built from config if None
    human_handler=None,         # §9.8 injected (HumanQuery)->HumanResponse (auto_research.hitl); None+headless → HumanInputRequired
    coder_model: str | None = None,   # resolved from the `coder` role in models.yaml
    run_gate=None,              # injectable hook; defaults to the REAL gate (gate.run_gate, step 3)
    run_review=None,            # injectable hook; defaults to the REAL review (review.make_review_hook, step 4)
    gate_rework_cap=None, review_rework_cap=None, max_turns=None,  # from orchestration.yaml
    worktree_factory=None, sdk_client_factory=None,  # seams for deterministic tests
) -> MergeFlowResult
```

Spawns the Sonnet author (Claude Agent SDK) in an isolated git **worktree/branch** on
`research_toolkit`, seeds it with the `ToolSpec` + runtime-safe structure context
(`xspec_utils` source + `prompts/coder.md` + obs paths — never `DOCS.md`/`CLAUDE.md`), and
drives the bounded rework loop:

- **L0** author self-correction inside the SDK session (`max_turns`).
- **L1** escalation: SDK `WebSearch`/`WebFetch`, plus two in-process MCP tools — `ask_lead`
  (→ `lead.clarify_for_author`) and `ask_human` (→ injected `human_handler`; none + headless
  → typed `HumanInputRequired`), so the lead is never the sole oracle.
- **L2** gate rejection → `GateReport` fed back into the warm session (`gate_rework_cap`).
- **L3** co-lead review rejection → `ToolReviewVerdict` fed back; a code change re-runs the
  gate before re-review (`review_rework_cap`).
- **L4** cap exhausted → `MergeFlowResult(status="failed")`, **no commit**, for the lead to
  raise to the human (§9.6/§9.7).

The orchestration is **code, never the LLM**. The gate/review are **injectable hooks**: the
gate defaults to the **real deterministic gate** (`gate.run_gate`, step 3); the review
defaults to the **real provider-decorrelated co-lead** (`review.make_review_hook`, step 4).
All hooks stay injectable so the loop is exercised deterministically in
`tests/test_merge_flow.py` (which injects a passing gate, review, and `approve_edit` — the
real gate/review/commit need a git worktree + a live model and are covered by
`tests/test_gate.py` / `tests/test_review.py` / the real-git `approve_edit` test in
`tests/test_merge_flow.py`). On gate-pass ∧ co-lead review-pass a successful run lands one
atomic commit via `approve_edit` (step 6) and returns `status="committed"` with the real
`commit_sha`; the runtime never touches master. `HumanInputRequired` is the typed headless
fail-safe (a run never silently hangs).

In **direct-branch mode** (`direct_branch="ai"`, the runtime default — the author works the
live `toolkit_root` checkout, not an isolated worktree), a **non-committed** terminal exit
(any of the `failed` returns, the unexpected-error path, or a re-raised `HumanInputRequired`)
**auto-resets that tree to HEAD** in `finally` (`_reset_direct_tree`: `git reset --hard HEAD`
+ `git clean -fd`), after first saving the discarded attempt's full diff as a
`tool_author_failed_<spec-id>_<rand>.patch` under `worktree_base` (the run's `authoring/`
dir, outside the toolkit). This is the failure-path counterpart to `approve_edit`: it stops a
rejected attempt's edits from surfacing a phantom, un-registered tool to re-running workers
and from tripping the next attempt's clean-tree precondition. The reset is best-effort (a git
failure is logged via `coder.tree_reset.error` and swallowed, never masking the result). The
launch preflight (`check_toolkit_branch_clean`) still guards a dirty tree at startup — e.g. a
run killed before `finally` ran.

### `gate.run_gate` (Phase 2 step 3 — the deterministic mergeability gate)

```python
async def run_gate(worktree: Path, tool_spec: ToolSpec, *,
                   test_runner=None, config=None) -> GateReport
```

The **trust boundary** (§9.4) — code, not an LLM opinion. Two layers:

- **Layer A (generic, domain-blind):** the **no-fabrication scan**, **structural** checks,
  **determinism**, **provenance** — all over the worktree's added/new files under a `*_utils`
  package (enumerated by `collect_targets` via `git status -uall` + per-file `git diff`).
  `collect_targets` tracks **every** changed file under a `*_utils` package, not only `.py`
  (shell reduction scripts and other text artifacts are part of the tool and must be gated
  and staged too, §9.2). Each target is dispatched by type: `.py` → the full AST scan
  (`scan_python_source`) + provenance; other **text** (e.g. `.sh`) → `scan_text_source` (the
  banned-vocabulary scan only — RNG/placeholder/provenance are Python-AST concepts);
  **binary / undecodable** → a `no_fabrication.binary` hard-fail (data never belongs in the
  toolkit, §9.5) that is fed back to the author through the rework loop, never an exception.
- **Layer B (the tool's own tests):** `pytest` in the worktree (via the injectable
  `test_runner`; the default runs the analyst interpreter twice for determinism). **Determinism
  is a per-test-outcome compare, not a byte compare:** each run writes a JUnit XML report
  (`--junitxml`), parsed into a `{test_id: PASSED/FAILED/ERROR/SKIPPED}` map; the run is
  deterministic ⇔ the two maps are equal. This ignores log/timing/stdout volatility (chatty
  science tooling — growing INFO filelists, banners) that the old `_strip_volatile` byte compare
  false-flagged, and catches a test *outcome* that flips run-to-run. Each pytest invocation is
  bounded by `_gate_test_timeout` (per-package `tool_config.yaml: gate_test_timeout_s` >
  `orchestration.yaml: gate_test_timeout_s` > 900; no clamp). **Reproducibility is not gate-
  enforced:** for a science tool the lead plans a `test_repro…` test (`proposed_checks`), the
  coder writes it, and the co-lead reviews it — it runs as a normal Layer-B test, so a result
  that wobbles outside tolerance flips its verdict and the determinism map catches it.

The **no-fabrication scan** (`scan_python_source`, pure over source text) is split into
precision tiers so it stays clean on real toolkit code (full of legitimate hardcoded
numeric arrays — XSPEC param vectors, energy bins):

- **Tier 1 HARD-FAIL** — banned **vocabulary** in comments/docstrings/strings
  (`synthetic`, `example data`, `placeholder`, `TODO`, … whole-word/phrase matched);
  **any RNG**, seeded or not (`np.random`, `default_rng`, `.rvs`, …) — §9.5 made absolute;
  **placeholder function bodies** (`pass`/`...`/`raise NotImplementedError`).
- **Tier 3 FINDING** — numeric-literal arrays `return`ed / plotted / framed: recorded on
  `GateReport.findings` (does **not** flip `passed`); the step-4 co-lead judges intent.

All scan lists live in **`config/code_gate.yaml`** (tunable without a code edit). A line
referencing a marked `synth…` ObsID is allowlisted so §9.5 fixtures stay legal.
`GateReport.passed ⇔ all Layer-A hard checks pass ∧ ≥1 Layer-B test ran ∧ all pass`
(an untested tool is never mergeable). `report_hash` seeds the §9.9 commit message.
Provenance: a `FigureRef(...)` the tool constructs must pass `sha256` + `produced_by`.
Deterministic coverage in `tests/test_gate.py` (pure scanners + a throwaway git worktree
with an injected Layer-B runner — no live science stack).

### `review.make_review_hook` (Phase 2 step 4 — provider-decorrelated co-lead review)

```python
def make_review_hook(colead=None, *, author_provider: str,
                     web_source=None, monitor=None) -> ReviewHook
def select_decorrelated_member(group, author_provider: str) -> ModelHandle
def build_diff_bundle(worktree: Path) -> str
```

The **second, independent** check on a merge candidate (§9.3 step 4): the gate is the door,
the co-lead review is the pre-filter the door also requires (`committed ⇔ gate-pass ∧
review-pass`). `make_review_hook` returns the default `run_review` hook (signature
`(worktree, tool_spec, gate) -> ToolReviewVerdict`):

- **Decorrelation** (`select_decorrelated_member`): routes to an `analyst_colead` member whose
  `provider != author_provider` (the author is the Claude SDK, so the reviewer is GPT). Raises
  `ValueError` when no eligible member exists — a co-lead sharing the author's provider is a
  broken trust boundary and fails loudly. An injected `colead` is asserted decorrelated too.
- **Diff bundle** (`build_diff_bundle`): reuses `gate.collect_targets` for the file set — new
  files in full, modified files as unified `git diff` (per-file capped, `_DIFF_FILE_CAP`).
- **The call**: `AnalystColeadAgent.review_tool(tool_spec, diff=…, gate=…, tools=…)`, with a
  `ReadOnlyToolProvider` scoped to the **worktree** (`include_text_search=False`) + optional
  `web_search`, so the co-lead investigates surrounding code/conventions before its verdict.

Heavy agent/source imports are deferred into the functions (mirroring `merge_flow`), so
importing the sandbox package never pulls the agent stack. Deterministic coverage in
`tests/test_review.py` (decorrelation routing, the diff bundle over a throwaway git repo, and
the hook mapping a fake-model verdict — no live LLM).

## Dependencies

- External: stdlib (`asyncio`, `subprocess`, `importlib`, `sys`, `json`);
  `mcp` (FastMCP server + ClientSession for `builtin_server.py` and `mcp_client.py`).
- Internal: `auto_research.schemas` (`ToolCallRequest`, `ToolResult`, `FigureRef`;
  `ToolSpec`, `GateReport`, `ToolReviewVerdict`, `MergeFlowResult` for `merge_flow`);
  `auto_research.agents.prompts.load_prompt` (the `coder` prompt); `claude_agent_sdk`
  (the author harness); for `review.py` (deferred imports): `auto_research.agents`
  (`AnalystColeadAgent`, `ReadOnlyToolProvider`), `auto_research.models.resolve`,
  `auto_research.sources.WebSource`, and `gate.collect_targets`.
- Depended on by: `auto_research.agents.react` / `auto_research.agents.roles`
  (`SandboxToolProvider` and `AnalystWorkerAgent.analyze()`); `auto_research.graph.runtime`
  (sandbox lifecycle); `auto_research.consensus` (peer tool-result summaries).

## Dictionary-level reference

- `ToolSpec` — TypedDict with `module`, `fn`, `description`, `args_schema`.
- `ToolkitRegistry(toolkit_root)` — registry instance.
  - `.load()` — import `TOOL_MANIFEST` from each known package. Idempotent.
  - `.is_known(name)` → `bool` — True if name is in any loaded manifest.
  - `.list_tools()` → `list[str]` — names of all registered tools.
  - `.describe_tool(name)` → `str` — `"<name>: <description>"` for prompts.
  - `.spec(name)` → `ToolSpec` — raises `KeyError` if unknown.
  - `.tool_catalogue()` → `str` — multi-line text listing of all tools.
  - `.tool_definitions()` → `list[dict]` — provider-neutral function definitions
    (`{name, description, input_schema}`) for native function calling.
  - `.kind_for_function(name)` → `str` — map a sanitized tool-call name back to
    the canonical dotted `kind`.
- `PersistentSandbox(toolkit_root, workdir, *, analyze_env, analyze_ini_sh, default_timeout_s)` —
  long-lived REPL subprocess per analyst worker.
  - `.start()` — spawn; idempotent.
  - `.restart()` — kill the current subprocess, if any, and spawn a fresh REPL.
  - `.call_tool(module, fn, args, *, timeout_s)` → `dict` — one tool call.
  - `.shutdown()` — graceful stop.
  - `.is_alive` → `bool` — True while the current subprocess is running.
- `create_run_workspace(run_id, agent_keys)` → `dict[str, Path]` — create workspace folders.
- `memory_path(run_id, agent_key)` → `Path` — path to agent's `memory.md`.
- `figures_dir(run_id, agent_key)` → `Path` — path to agent's `figures/` dir.
- `run_dir(run_id)` → `Path` — path to run's root dir.
- `append_memory(path, text)` — append to memory.md.
- `read_memory(path)` → `str` — read memory.md or return `""`.
- `run_tool(module, fn, args, *, timeout_s, workdir, toolkit_root, analyze_env=None, analyze_ini_sh=None)` →
  `ToolResult` — one-shot subprocess; kept for ad-hoc use.
- `_source_ini_env(ini_sh)` → `dict` — sources a bash init script, caches result. Raises `RuntimeError` on any failure (non-zero exit, empty env, or subprocess exception) — no fallback to `os.environ`.
- `analyst_env.sh` — bash script setting HEADAS, CALDB, CALDBCONFIG, CALDBALIAS,
  GEOMAG_PATH. Sourced by `_source_ini_env()`. No interactive cruft.
  `config/orchestration.yaml::analyze_ini_sh` points at this file.
- `MCPToolClient(servers)` — manages stdio MCP server sessions.
  - `.start()` — spawn servers and establish sessions.
  - `.list_kinds()` → `list[str]` — all `mcp.<name>.<tool>` kind strings.
  - `.describe(kind)` → `str` — description for a kind.
  - `.schema(kind)` → `dict` — input JSON schema (from the server's `inputSchema`).
  - `.call_tool(kind, arguments)` → `dict` — call one MCP tool.
  - `.shutdown()` — close sessions and terminate servers.
- `builtin_server.py` — MCP stdio server with shell and file-tree tools.
  - `bash_exec(command, cwd=None, timeout_s=60)` → `{stdout, stderr, exit_code}`.
  - `list_dir(path)` → `{path, entries}`.
  - `read_file(path, max_bytes)` → `{content, truncated, size_bytes}`.
  Kind prefix: `mcp.builtin.*`.
- `PersistentSandbox.call_mcp_tool(kind, args, *, timeout_s)` → `dict` —
  forward one MCP call; returns error dict if no client attached.
- `ToolkitRegistry.load_mcp(client)` — index MCP tools from a started client.
- `ToolkitRegistry.is_mcp(kind)` → `bool` — True if kind is an MCP tool.
- `propose_and_pr(tool_spec, *, lead, toolkit_root, worktree_base, colead=None,
  human_handler=None, coder_model=None, run_gate=None, run_review=None, approve_edit=None, …)`
  → `MergeFlowResult` — the author harness: spawns the Sonnet Claude-SDK author in an
  isolated worktree and drives the L0–L4 bounded rework loop. `run_gate` defaults to the real
  `gate.run_gate`; `run_review` to the real `review.make_review_hook`; `approve_edit` to the
  real atomic commit. On gate-pass ∧ review-pass it sets the commit subject to
  `review.commit_summary` (falling back to `tool_spec.proposed_tool_name`), calls
  `approve_edit`, and returns `status="committed"` with the real `commit_sha`. All hooks stay
  injectable for deterministic tests.
- `approve_edit(worktree, *, message)` → `str` — Phase 2 step 6, the **only** place the
  runtime commits (§9.9). Stages **exactly** `gate.collect_targets(worktree)`'s paths
  (explicit-path `git add -- …`, never `git add -A`, so scratch outside a `*_utils` package
  can't leak), commits with `message` on the branch the worktree is already on, and returns
  the new commit sha (`git rev-parse HEAD`). Pure git mechanics — it does not re-assert
  gate-/review-pass (the pipeline guarantees it). The runtime never merges/touches master.
- `_reset_direct_tree(toolkit_root, *, artifact_dir, monitor=None, tool_spec=None)` (async) —
  the failure-path counterpart to `approve_edit`. Called from `propose_and_pr`'s `finally` on a
  **non-committed** direct-branch exit: saves the discarded attempt's full diff (tracked edits
  + new untracked content, via `git add -A` → `git diff --cached HEAD`) to a
  `tool_author_failed_*.patch` under `artifact_dir` when that dir is outside `toolkit_root`,
  then resets the live checkout with `git reset --hard HEAD` + `git clean -fd` (no `-x`:
  gitignored caches don't trip the dirty check). Best-effort — any git failure is emitted as
  `coder.tree_reset.error` and swallowed so cleanup never masks the caller's result.
- `check_toolkit_git_identity()` — launch-time preflight (no LLM): asserts the
  `toolkit_root` checkout (`config/orchestration.yaml`) has both `user.name` and `user.email`
  set, so the runtime's atomic commit can't fail mid-flow on a missing ambient git identity.
  Registered in `auto_research.preflight`.
- `scan_text_source(rel, source, *, added=None, config=None)` → `(hard_fails, findings)` —
  the banned-vocabulary no-fabrication scan for a **non-Python text** file (e.g. a shell
  script); no AST, so RNG/placeholder/provenance do not apply and `findings` is always empty.
- `HumanInputRequired(RuntimeError)` — typed headless fail-safe raised when the author
  needs a human but no `human_handler` is wired (§9.8). Now defined in `auto_research.hitl`
  and re-exported here for back-compat; the author's `ask_human` builds a typed `HumanQuery`
  and routes through `hitl.resolve_human` (the same channel every in-graph role uses).
- `gate.run_gate(worktree, tool_spec, *, test_runner=None, config=None)` → `GateReport` —
  the deterministic gate (§9.4). Runs the no-fabrication scan + structural + determinism +
  provenance (Layer A) and the tool's tests (Layer B); records Tier-3 findings without
  failing. `scan_python_source(rel, source, *, added=None, config=None)` → `(hard_fails,
  findings)` is the pure, unit-testable `.py` scan core; `scan_text_source` is its non-`.py`
  text counterpart (vocabulary only); binary/undecodable targets hard-fail without scanning.
  `scan_provenance`, `check_structural`, `collect_targets` (now extension-agnostic under a
  `*_utils` package), and `_manifest_keys` are its helpers. Scan lists load from
  `config/code_gate.yaml`. When no `test_runner` is injected, the default is the real
  `_default_test_runner` with the per-package timeout bound via `partial` (so an injected
  runner keeps its `(worktree, test_paths)` signature).
- `gate._default_test_runner(worktree, test_paths, *, timeout_s=900)` → `dict` — the real
  Layer-B runner: runs pytest twice with the analyst interpreter (`--junitxml` to a throwaway
  temp dir), `timeout_s` per invocation. Returns `{results, deterministic, determinism_detail}`;
  `results` carries the `layer_b.pytest` `CheckResult` (`passed ⇔ rc1==0`), determinism from the
  two per-test outcome maps.
- `gate._parse_junit_outcomes(junit_path)` → `dict[str,str]` — pure: parse a JUnit XML report
  into `{"<classname>::<name>": PASSED/FAILED/ERROR/SKIPPED}` (read from each `<testcase>`'s
  child element). Returns `{}` on an absent/unparseable report.
- `gate._outcomes_determinism(map1, map2)` → `(bool, str)` — pure: deterministic ⇔ the two maps
  are identical (keys + values); the detail names every diverging test. Catches an outcome flip
  and the same-count/different-tests swap a bare return-code compare misses.
- `gate._gate_test_timeout(worktree, tool_spec)` → `int` — resolve the per-pytest-invocation
  Layer-B timeout: per-package `<worktree>/<target_domain>/tool_config.yaml: gate_test_timeout_s`
  > `orchestration.yaml: gate_test_timeout_s` > 900. No clamp (trusted infra), coder-settable;
  the per-package value is read (never imported) from the worktree.
- `review.make_review_hook(colead=None, *, author_provider, web_source=None, monitor=None)` →
  the default `run_review` hook (§9.3 step 4); the provider-decorrelated co-lead review.
  - `review.select_decorrelated_member(group, author_provider)` → `ModelHandle` — picks the
    `analyst_colead` member with `provider != author_provider`; raises if none exists.
  - `review.build_diff_bundle(worktree)` → `str` — new files in full + modified files as
    unified diffs (reuses `gate.collect_targets`; per-file capped).
  - `review.check_author_reviewer_decorrelation()` — launch-time preflight assertion (no LLM)
    that the `coder` author and `analyst_colead` reviewer are provider-decorrelated; raises
    via `select_decorrelated_member`. Registered in `auto_research.preflight`.

## Changelog
- 2026-05-26 -> 2026-06-12 — Phase 1 sandbox/tooling built and hardened:
  manifest-based `ToolkitRegistry`, disabled-tool filtering, provider-native tool
  definitions, persistent JSON-line sandbox with fd capture and XSPEC non-interactive boot,
  per-run workspace/memory/figures layout, built-in MCP server/client, and config-driven
  runtime paths.
- 2026-06-12 -> 2026-06-16 — **Phase 2 steps 0–7 built.** Step 0 pinned worker scratch,
  figure outputs, and the builtin MCP cwd under the runtime data dir (writes stay outside both
  repos). The tool-authoring path was assembled: `merge_flow.propose_and_pr()` (isolated-worktree
  Sonnet author, runtime-safe structure context, `ask_lead`/`ask_human` escalation, bounded L0–L4
  rework loop, typed no-commit failure); `gate.py::run_gate` (Layer-A structural/provenance/
  determinism + configurable no-fabrication scan with non-failing Tier-3 findings, Layer-B test
  runner); `review.py::make_review_hook` (provider-decorrelated GPT co-lead review backing the
  default `run_review` hook — decorrelation routing, diff bundle, `AnalystColeadAgent` over a
  worktree-scoped `ReadOnlyToolProvider`); `approve_edit(worktree, *, message)` (the one atomic
  commit per tool, explicit-path staging of exactly `gate.collect_targets`, never `git add -A`,
  returns a real `commit_sha`, subject from the co-lead's `commit_summary`). The gate became
  extension-agnostic under `*_utils` packages (`collect_targets`, `scan_text_source` for non-`.py`
  text, binary/undecodable hard-fail). Step 5 moved `HumanInputRequired`/`HumanHandler` to
  `auto_research.hitl` (re-exported; `ask_human` routes through `hitl.resolve_human`). Step 7 made
  `ToolkitRegistry` discover `*_utils` packages dynamically + `reload()`, and added
  `propose_and_pr(direct_branch=…)` (author on the ambient `ai` branch in `toolkit_root`). Launch
  preflights added: `check_author_reviewer_decorrelation`, `check_toolkit_git_identity`,
  `check_toolkit_branch_clean`. Deterministic coverage in `tests/test_{merge_flow,gate,review,
  registry,preflight}.py`.
- 2026-06-18 -> 2026-06-22 — **Authoring-path hardening + analysis-is-tool-driven scoping.** The
  gate/authoring path survives a slow or erroring test: `gate._default_test_runner` catches
  `subprocess.TimeoutExpired` as a Layer-B failure (rc 124, over the 900 s cap → normal rejection)
  and `propose_and_pr` has an exception backstop that turns any unexpected gate/author/review error
  into `MergeFlowResult(status="failed")` (re-raising only `HumanInputRequired`); determinism stays
  a console-output proxy (`_strip_volatile` over two pytest runs) by design. The SDK author's `Bash`
  now runs in the **analyst code-execution env** (`merge_flow._author_env`: analyst conda interpreter
  + HEASoft/CALDB from `analyze_ini_sh` + `toolkit_root` on `PYTHONPATH`, passed as
  `ClaudeAgentOptions.env`; falls back to the parent env when unconfigured) so it can run the real
  science tests it authors. `bash_exec`'s model-facing description was rescoped to **read-only
  inspection** (the analyst team authors no analysis code — real steps run through toolkit tools, a
  missing capability becomes a `tool_request`); behavior unchanged.
- 2026-06-22 — **Per-tool sandbox call-timeout.** A live run exposed that the sandbox
  SIGTERM-kills every tool call at a flat ~300s (exit 124), so a slow HEASARC download could never
  complete no matter what timeout the tool set internally. Now layered: the global default lives in
  `orchestration.yaml: sandbox_call_timeout_s` (default 300, via `env.sandbox_call_timeout_default`);
  a package's optional `tool_config.yaml` (`call_timeout_s` per tool, `default_call_timeout_s` per
  package) overrides it. `ToolkitRegistry` reads it (`_load_tool_config`) and exposes
  `call_timeout_ceiling(tool)` (precedence per-tool > package-default > global; **no upper clamp** —
  trusted human/coder infra); `SandboxToolProvider.execute` uses it directly as the per-call timeout
  (falls back to `self.timeout_s` for registries without the method). **Decoupled from the model:**
  the ceiling is a fixed hard backstop, never surfaced to the LLM and not model-tunable; a tool's
  *own* timeout arg (e.g. `acquire`'s astroquery `timeout_s`, in its `args_schema`) is the caller's
  graceful knob, passed through untouched, and the backstop is set above it so the tool times out
  cleanly first. Chosen over an orchestration→tool-name map (would couple orchestration to toolkit
  internals) and a `TOOL_MANIFEST` field (hard to hand-edit). `research_toolkit/heasarc_utils/
  tool_config.yaml` sets `acquire` above its 1800s function timeout. Deterministic tests in
  `tests/test_registry.py`.
- 2026-06-22 — **Authoring flow is now legible in the run JSONL.** `propose_and_pr` gained a
  `monitor` param (consensus passes `self.monitor`); it threads into `_drive_author` (the SDK
  author session, previously drained silently — now emits per-block `coder.tool_use`/
  `coder.tool_result`/`coder.text`/`coder.turn_end`), the `ask_lead`/`ask_human` handler factories
  (`coder.ask_lead`/`coder.ask_human` with the Q&A), and wraps gate/review/commit
  (`coder.session_start`, `gate.start`/`gate.result`, `review.result`, `commit`). All on team
  `tooling`, capped via `_cap_text` (renamed from a clash with the config `_cap`), no-op when
  `monitor` is None. Fixes the ~20-min blind gap where neither the JSONL nor the terminal showed
  whether the coder was working, blocked, or the gate was running. Tests in `tests/test_merge_flow.py`.
- 2026-06-23 — **Auto-reset the toolkit tree after a non-committed authoring attempt** (the
  failure-path counterpart to `approve_edit`). `propose_and_pr` now tracks a `committed` flag and,
  in `finally` for direct-branch mode, calls the new best-effort `_reset_direct_tree`
  (`git reset --hard HEAD` + `git clean -fd`) on any non-committed exit — after saving the
  discarded diff as a `tool_author_failed_*.patch` under `worktree_base`. Fixes the live-run bug
  where a gate-rejected attempt left the live `ai` checkout dirty, which (a) surfaced a phantom,
  un-registered tool to re-running workers (`[exit 127] Tool not registered`) and (b) made a 2nd
  authoring attempt's `check_toolkit_branch_clean` raise an unhandled `RuntimeError` → `run.error`
  → whole-run crash. Docstrings updated (`propose_and_pr`, `check_toolkit_branch_clean`: the
  runtime is no longer "never reset" — it self-heals in-run; the launch preflight still guards a
  dirty tree at startup). Minimal scope: graceful-degrade of the in-flow precondition and
  worktree-mode orphan pruning are tracked as separate follow-ups. Five new tests in
  `tests/test_merge_flow.py`, plus an **env-gated live test** `tests/test_live_merge_flow.py`
  (`AUTO_RESEARCH_LIVE_MERGE_FLOW_TESTS=1`): a reusable `ScriptedCoder` (drives chosen "bad diffs"
  through the `sdk_client_factory` seam) runs the **real** `gate.run_gate` against a throwaway
  clone of `toolkit_root` on a fresh `ai` branch and asserts gate-reject → tree-reset → patch
  artifact — the shortcut for live-testing deep authoring-flow changes without an LLM.
- 2026-06-23 — **Filesystem jail (todo #3).** New `fs_guard.py`: `PathGuard` + `load_fs_allowlist`
  — the shared, config-driven allow-list (`orchestration.yaml: agent_fs_allow`) every runtime agent's
  file access is checked against (`is_allowed` exact; `forbidden_in_command` best-effort for shell
  command strings; denies governance basenames `CLAUDE.md`/`DOCS.md`/`AGENTS.md` even inside a root;
  empty list = inactive). `merge_flow._build_options` now installs `_make_fs_guard_hook` as a
  **`PreToolUse` hook** (runs first, fires for every tool call) — the correct gate; the old
  `can_use_tool` `_make_path_confiner` is **deprecated/unwired** (the SDK skips `can_use_tool` under
  `acceptEdits`/allow-rules, so it never fired for reads/Bash). So `auto_research` + governance files
  are denied to the coder. Env-gated live battery test `tests/test_live_fs_guard.py`
  (`AUTO_RESEARCH_LIVE_FS_GUARD_TESTS=1`) launches a real coder and asserts the hook actually fires.
  Bash confinement is best-effort (command-string only) — OS jail completes it (CLAUDE.md §12/§13).
- 2026-06-24 — **Coder path-hints (fix-list #2).** `_build_structure_context` now takes the author's
  actual working dir (`worktree`, passed at the call site) instead of `toolkit_root`, states it up
  front as `# Working directory (your cwd): <abs>`, and prints the template layout as **absolute**
  paths (was `relative_to`, with no root stated — the coder used to guess `/root/…` → EACCES →
  `find /`). Mission observation paths (validation ObsIDs) are reframed as the absolute dirs they are.
  New launch preflight `check_toolkit_root_configured` (registered before `check_toolkit_git_identity`):
  asserts `toolkit_root` is set in `config/orchestration.yaml` and is a directory, failing loudly
  instead of letting a stale default mask a misconfig. New env-gated live test
  `tests/test_live_path_hints.py` (`AUTO_RESEARCH_LIVE_PATH_HINT_TESTS=1`): seeds a real coder with
  the actual structure context (paths NOT restated in the prompt) and asserts — from the recorded
  tool stream — a clean `ls` of the toolkit root + every obsdb path at their absolute paths, with no
  hunting. Unit coverage in `test_merge_flow.py`/`test_preflight.py`.
- 2026-06-25 — **`gate._manifest_keys` now handles annotated `TOOL_MANIFEST` (fix-list #11).** It
  matched only a plain `ast.Assign` (`TOOL_MANIFEST = {...}`), but `xspec_utils` declares
  `TOOL_MANIFEST: dict = {...}` — an `ast.AnnAssign` — so it extracted **zero** keys and
  `structural.tool_manifest_entry` reported "no entry" for *every* extend-`xspec_utils` tool,
  regardless of the key written (the root cause of the 2026-06-25 P1 authoring failure: the coder's
  manifest entry was correct but invisible to the gate). Now matches both `ast.Assign` (`.targets`)
  and `ast.AnnAssign` (`.target`). New test `tests/test_gate.py::test_structural_annotated_manifest_entry_found`
  (and a `manifest_annotated` flag on the `_write_pkg` fixture); `pytest tests/test_gate.py` green (34).
- 2026-06-25 — **Coder git-discipline hardening (fix-list #6/#7).** The runtime now contains a coder
  that issues git itself (the observed §9.9 breach), two layers: (1) a **deny-rule** on the coder's
  own options — `_build_options` adds `disallowed_tools=["Bash(git:*)", "Bash(git)"]` (coder-scoped,
  evaluated first, applies under acceptEdits; best-effort prefix match). (2) **`_reset_direct_tree`
  resets to a per-attempt base ref**, not `HEAD`: `propose_and_pr` records `base_ref = git rev-parse
  HEAD` right after `check_toolkit_branch_clean` (per-attempt, so a failed later round preserves an
  earlier round's successful commit) and the failure-path reset does `reset --hard <base_ref>` +
  saves the discarded diff vs `base_ref` (capturing any coder commit) — so an illegitimate commit is
  **dropped**, where `reset --hard HEAD` would have kept it (the phantom-committed-tool hole). Falls
  back to `HEAD` when `base_ref` is unset (worktree mode). `prompts/coder.md` now tells the coder to
  leave git to the runtime and `ask_lead` instead of working around it, and its three misleading
  "commit" verbs (the deliverable, not git) were reworded to "include"/"write". Tests:
  `test_merge_flow.test_direct_branch_failed_drops_coder_commit` (a committing gate → HEAD back at
  base, file gone, commit captured in the patch) + env-gated live `test_live_fs_guard.test_live_coder_git_denied`
  (real coder told to `git commit` lands no commit). CLAUDE.md §9.9/§13 updated. Residual: a run
  killed mid-attempt can skip the reset `finally` (§12 Ctrl-C hardening). `pytest tests/test_merge_flow.py
  tests/test_gate.py` green (65).
- 2026-06-25 — **`finding_vocabulary` Tier-3 scan tier (fix-list #9a).** `stub`/`stubbed`/
  `not implemented`/`to be implemented` moved out of `hard_fail_vocabulary` into a new
  `config/code_gate.yaml: finding_vocabulary` — they legitimately appear in real mergeable code
  (a test stub, an honest not-implemented branch) and were causing false hard-fails (the 2026-06-25
  run rejected a test for the word `'stub'`). `gate.scan_python_source`/`scan_text_source` now load
  `finding_vocabulary` and record matches as a non-failing `no_fabrication.vocabulary_finding`
  (Tier-3, routed to the co-lead like the literal-array landmine) instead of hard-failing.
  `TODO`/`FIXME`/`XXX`/`HACK` stay hard-fail. Test `test_gate.test_incompleteness_markers_are_findings_not_hard_fails`;
  `pytest tests/test_gate.py` green (35).
- 2026-06-25 — **Gate-entry coder-commit auto-revert (fix-list #8).** The gate's change-detection
  keyed on `git status` (uncommitted), so a coder that committed (slipping the deny-rule) blinded it
  into a confusing empty-changeset rejection. Now, as the **first action when arriving at the gate**,
  `merge_flow._gated` compares HEAD to the per-attempt `base_ref`; if it moved, the coder ran git
  itself, so the runtime does `git reset --mixed base_ref` (un-commits, leaving the work in the
  working tree) and emits a `coder.committed` monitor event — then `run_gate` scans the recovered
  changes normally. The coder never touches git, its work is preserved, and the only real commit
  remains `approve_edit`'s (post gate+review), so `commit ⇔ gate ∧ review` holds. Best-effort (a
  reset failure falls through to the empty-changeset rejection + the finally reset); direct mode only
  (`base_ref` set). No change to `gate.py`/`run_gate` signature (test stubs untouched). Test
  `test_merge_flow.test_direct_branch_coder_commit_auto_reverted_before_gate` (HEAD advances by
  exactly one — approve_edit's — and the `coder.committed` event fires). `pytest tests/test_merge_flow.py`
  green (32).
- 2026-06-26 — **Gate determinism = per-test outcomes, not byte-identical output (handoff #2,
  the confirmed authoring blocker).** `_default_test_runner` now runs pytest twice with
  `--junitxml` (to a throwaway temp dir), parses each report into a `{test_id: outcome}` map via
  the new pure `_parse_junit_outcomes`, and declares the run deterministic ⇔ the two maps are
  equal (new pure `_outcomes_determinism`, naming any diverging test). `_strip_volatile` and the
  byte-compare are removed. This fixes the false-positive that rejected a *correct* tool 4× and
  exhausted the rework cap on the 2026-06-26 run (a benign growing `xpselect` INFO filelist tripped
  the byte compare though 18/18 tests passed both runs); it still catches a test *outcome* that
  flips run-to-run, including the same-count/different-tests swap a bare `rc==rc` misses. Layer-B
  pass stays `rc1==0`; the map governs determinism only. The hardcoded 900s pytest timeout became
  config: `_gate_test_timeout` resolves per-package `tool_config.yaml: gate_test_timeout_s` >
  `orchestration.yaml: gate_test_timeout_s` > 900 (no clamp, coder-settable), bound into the
  default runner via `partial` so injected runners keep their `(worktree, test_paths)` signature.
  Reproducibility is **not** gate-enforced — it is an LLM-planned check for **science tools** only
  (`test_repro…`, call-twice / injection-recovery within tolerance): the lead plans it in
  `proposed_checks`, the coder writes it, the co-lead reviews it against the plan; it runs as a
  normal Layer-B test so a wobble outside tolerance flips its verdict and the determinism map
  catches it. Prompts updated (`coder.md`, `analyst_lead.md`, `analyst_colead.md`); CLAUDE.md §9.4
  reworded. New tests in `tests/test_gate.py` (junit parse, the three determinism cases, timeout
  precedence).
