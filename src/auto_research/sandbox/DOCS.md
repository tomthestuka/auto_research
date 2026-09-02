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
    description: str  # agent-facing; injected into analyst worker prompt
    args_schema: dict # JSON Schema for the args dict
```

**The manifest is the agent-facing interface, verbatim.** `description` and `args_schema` are
taken from `skills/TOOL_MANIFEST` exactly as authored — the backing `lib/` function's
docstring never replaces them. This is the §9.2 two-halves split: `skills/` is the ai-half,
`lib/` docstrings are the human-half. (Until 2026-07-27 `load()` overwrote the description
with `fn.__doc__` whenever the module imported, which silently discarded the coder-authored,
co-lead-reviewed description for any import-clean tool and made the agent-facing text depend
on a tool's dependency footprint. Pinned by
`tests/test_registry.py::test_manifest_description_wins_over_function_docstring`.)

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
def round_report_path(agent_dir_path: Path, *, role: str, model: str, round_n: int) -> Path
def append_memory(path: Path, text: str) -> None
def read_memory(path: Path) -> str
```

- `create_run_workspace()` creates `<runtime_dir>/runs/{run_id}/{key}/figures/`
  for each agent key and returns `{key: agent_dir}`.
- `memory_path()` returns `{agent_dir}/memory.md` (parent created if absent).
- `round_report_path()` returns `{agent_dir}/<role>_<model-slug>_round<n>_report.jsonl` — the
  per-round inter-LLM report JSONL an analyst writes via `edit_report` (V-4), distinct from the
  private `memory.md`. `n` is the monotonic debate round; the model label is slugified (`_slug`).
- `append_memory()` appends bare terminal text; `read_memory()` returns contents or `""`.

### `MCPToolClient`

```python
class MCPToolClient:
    def __init__(self, servers: list[dict] | None = None) -> None: ...
    async def start(self) -> None          # spawn server subprocesses; establish sessions
    def list_kinds(self) -> list[str]      # all registered MCP tool kinds
    def describe(self, kind: str) -> str   # description string for a kind
    def schema(self, kind: str) -> dict    # input JSON schema for a kind
    async def call_tool(self, kind: str, arguments: dict, *, timeout_s: float | None = 900.0) -> dict
    async def shutdown(self) -> None       # close all sessions; terminate servers
```

- `servers` is a list of `{"name": str, "command": str, "args": list[str]}` dicts.
  Defaults to a single `builtin` server running `builtin_server.py` via
  `sys.executable`.
- Tool kinds follow the convention `mcp.<server_name>.<tool_name>`.
- `call_tool()` returns a dict with at minimum `stdout`, `stderr`, `exit_code`.
  Non-zero `exit_code` or `result.isError=True` is surfaced in the same `ToolResult`
  schema used by manifest tools, so the retry loop applies equally.
- The `session.call_tool` await is bounded by `timeout_s` (`asyncio.wait_for`, default
  `900 s` — far above any builtin tool's own timeout). It is a **backstop**, not a normal
  path: a wedged server returns `exit_code` 124 instead of hanging the whole run.

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
  Runs the command with **stdin closed** (`DEVNULL`) in its **own session/process group**
  (`start_new_session=True`); on timeout the whole group is SIGKILLed (`_kill_process_group`),
  returning `exit_code` 124. Both matter: a prompting/forking grandchild (e.g. an interactive
  HEASoft FTOOL) can neither read this server's MCP stdio transport (which would starve the
  protocol and wedge the run) nor outlive the timeout. The tool description also instructs the
  model to invoke commands non-interactively (all parameters supplied up front). The description
  is **built at server startup** by `_bash_exec_description()` and is **domain-neutral**: the
  concrete stack-probe example is injected from `AR_ANALYZE_PROBE` (config `analyze_probe`, wired
  by `graph.runtime._builtin_server_config`); absent ⇒ a generic "import your toolkit package"
  line. No domain string (FITS/xspec) is hardcoded in source.
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
    coder: RoleModelConfig | None = None,   # the author member; None = the `coder` role in models.yaml
    allow_correlated_review: bool = False, # MEASUREMENT ONLY: permit a same-provider co-lead
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

**The coder authenticates from its own environment.** It is the Claude Agent SDK — a separate
process — so an Anthropic API-key rotation inside the orchestrator (a rate limit or a spent
balance; see `models/DOCS.md`) does not reach it. `_with_live_anthropic_key` stamps the live key
onto the options' `env` **only when it has actually rotated**, so the ordinary path is
byte-identical (`None` stays `None` and the SDK keeps inheriting the parent environment). A
session already in flight keeps the key it launched with — the SDK owns that subprocess's
environment for its lifetime.

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

### `jail.py` — the OS-level filesystem jail (§12)

`PathGuard` states the allow-list to the models and refuses the calls whose paths it can see. It
cannot see everything: a shell command's implicit reads are invisible to a command-string scan, and
the Codex harness grants full-disk **read** access by design — a DeepSeek coder read this repo's own
`coder/codex_sdk.py`, `sandbox/DOCS.md`, the test harnessing it and `~/.codex/config.toml`
unprompted (2026-08-19, 36 recorded crossings). This module closes that: the same allow-list,
enforced by the kernel.

```
bwrap, unprivileged (user namespaces + Landlock)
  --ro-bind   agent_fs_readonly            conda env · HEASoft · CALDB
  --bind      agent_fs_allow minus those   toolkit · obs-db · pfiles · runtime dir · /tmp
  --symlink   configured → resolved        so /data/… spellings work inside
  --proc --dev --unshare-{user,pid,ipc,uts} --die-with-parent
  --unshare-net                            gate only
```

| process | insertion | network |
|---|---|---|
| Claude coder | `ClaudeAgentOptions(cli_path=…)` → a generated launcher | yes |
| Codex coder | `CodexConfig(codex_bin=…)` → a generated launcher | yes |
| analyst REPL | argv prefix in `persistent.py` | yes (acquisition downloads) |
| gate's Layer-B pytest | argv prefix in `gate.py` | **no** |

Wrapping each harness's *binary* rather than its command line keeps us out of the vendors'
argv: whatever they pass next release still lands inside the jail.

**Four rules the jail learned by failing, all pinned in `tests/test_jail.py`:**

- **Outermost symlink only.** bwrap creates a mount point's parents itself, so once an outer path
  exists in the jail as a symlink, creating anything beneath it fails with "Can't mkdir parents".
  All 30 Layer-B suites died there twice — first from recreating symlinked *ancestors*, then from a
  nested configured root.
- **Outermost bind first.** A later bind lands on top of an earlier one, so a read-only subtree
  inside a writable root only means anything when the parent is applied first; otherwise the order
  entries happen to appear in a config file decides which rule wins.
- **The allow-list was never a mount plan.** Every file in PyXspec's package directory is a symlink
  four levels up into a sibling of `$HEADAS`, so jailing exactly the configured roots yielded an
  importable-looking namespace package and an `ImportError` on the first real symbol. `agent_fs_allow`
  needed the HEASoft *release root* as well. A guard that inspects named paths could never notice
  this; `check_os_jail_probe` now runs the configured `analyze_probe` inside the jail at launch.
- **Die with the parent.** The jailed process is pid 1 of its own namespace, so the interrupt path's
  kill has to take the whole tree with it (§9.9).

Two failure modes are deliberately different. A missing `bwrap` or a host that forbids user
namespaces **warns** and runs unjailed, because that is the configuration every earlier run used.
A jail that breaks the science stack **fails the launch**, because otherwise every tool call and
every Layer-B test fails one at a time.

### `coder/` — the author harnesses (which process writes the code)

The `coder` role in `config/models.yaml` names both a model and the **harness** that drives it, so
a run can be authored by the Claude Agent SDK or by the Codex app-server without any change to the
authoring flow. What the flow needs from an author is three methods; everything else about a
harness is the backend's business.

```python
class CoderBackend(Protocol):        # coder/backend.py
    async def connect(self) -> None            # bring the author process up
    async def drive(self, message: str) -> str # one turn; streams to the ledger, books its spend
    async def disconnect(self) -> None         # take it down (finally + interrupt cleanup)
```

`merge_flow._build_backend` picks the implementation from `coder_member().harness`. Both receive
the same brief, working tree, escalation handlers and filesystem allow-list; the harnesses differ
in what mechanism they offer for each.

| wire | `claude_sdk.ClaudeSdkBackend` | `codex_sdk.CodexBackend` |
|---|---|---|
| process | the `claude` CLI via `ClaudeSDKClient` | `codex app-server` via `openai_codex.CodexClient` |
| auth | the env rebuild drops `ANTHROPIC_API_KEY`, so it uses stored subscription credentials; `_with_live_anthropic_key` stamps a rotated key | the vendor key named by the member's `key_env`, in the child's environment |
| role prompt | `prompts/coder.md` as `system_prompt` (replaces) | the same text as `developerInstructions` (layers over Codex's own tool instructions) |
| tool surface | an explicit `allowed_tools` list | Codex's fixed built-ins plus `tools.web_search` |
| write jail | a `PreToolUse` hook consulting `PathGuard` | `sandbox="workspace-write"` + `writableRoots` = the same allow-list, enforced by the OS (Landlock) |
| read jail | the same hook, for tools whose path it sees | **none**: Codex grants full-disk read access by design, so a crossing is recorded as `coder.read_outside_allowlist` |
| git ban | a `Bash` deny-rule (best-effort) | `.git` is read-only inside a writable root under that sandbox |
| escalation | an in-process SDK MCP server | an in-process Streamable-HTTP MCP endpoint (`escalation_server.py`), registered as a **process-level** `--config` override and gated by an *elicitation* the client answers with `{"action": "accept"}` |
| cost | a session-cumulative dollar figure, differenced per turn | real token counts per model call (`thread/tokenUsage/updated`), priced by `CostMeter.record_call` like any chat role |
| turn status | inferred | `turn/completed` carries a status and an error |

Both stream the same `coder.*` events (`coder.session_start`, `coder.text`, `coder.tool_use`,
`coder.tool_result`, `coder.turn_end`), which is what makes two harnesses comparable inside one
campaign; `coder.read_outside_allowlist` is Codex-only by construction.

**Not every pairing is proven.** `UNVERIFIED_PAIRINGS` marks the ones configured correctly and
never run against the real service, and `check_coder_harness_supported` raises a
`PreflightWarning` at launch when the configured coder is one of them — the run proceeds and the
caution lands on the ledger, so a first-time failure reads against a known gap. Today that is
**`dashscope` + `codex` (Qwen): NOT LIVE TESTED.** Every attempt on 2026-08-19 returned HTTP 400
`AccessDenied.Unpurchased` across three models and all three documented hosts; the pay-as-you-go
host recognised the key (the CN and token-plan hosts answered `InvalidApiKey`), so the wiring is
right and the account was not entitled. QwenCloud gates pay-as-you-go on a complete billing
profile (payment method plus name, address and phone verification) rather than on buying credits.
Two traps for whoever finishes it: the Coding Plan issues `sk-sp-…` keys on a Chat-Completions
endpoint current Codex refuses, and the Token Plan uses a different key **and** host.

**Which pairings exist** (`coder/harness.py`): a harness speaks one wire dialect, so
`HARNESS_ENDPOINTS` is the table of legal `(provider, harness)` pairs, each non-native row
carrying the endpoint that vendor documents for that harness (DeepSeek, Z.AI GLM and Alibaba Model
Studio publish both an Anthropic-dialect and a Responses endpoint; Anthropic publishes no Responses
endpoint and OpenAI no Anthropic-dialect one, so those two pairs are absent). Two launch checks run
before any model is constructed: `check_coder_harness_supported` (the pair exists, a non-native pair
names its endpoint, the key variable is set) and `check_coder_harness_available` (the harness's CLI
resolves — asked of each SDK rather than probed on `PATH`).

**Live coverage is a matrix over the legs** (`tests/test_live_coder_matrix.py`, env-gated). One warm
session per `(vendor, harness)` pair is walked through every stimulus the flow can send — the seed
brief, a gate rejection, a co-lead rejection, a free-text lead instruction, a governance-file read,
a git attempt, an `ask_lead` escalation, and a lengthening trajectory — in the order a real attempt
produces them, because the history is part of the state under test. The stimuli are **recorded, not
invented**: `tests/fixtures/coder_history.json` is frozen out of real run ledgers by
`tmp_scripts/extract_coder_history.py`. Assertions sit on our side of the boundary (ledger event
names, the working files surviving across turns, HEAD not moving, the escalation handler being
reached in-process, spend booked, the governance marker never entering the transcript) and never
judge model quality. A leg skips when its credential is absent, and the read-boundary expectation
is declared per leg rather than branched inside the test, so the Claude-enforces / Codex-audits
asymmetry is visible in the roster.

**Measured, not assumed** (2026-08-19, live probe): under `approvalPolicy="untrusted"` with
`approvalsReviewer="user"` a *patch* is offered to the client's approval handler and a denial is
honoured, but a shell command the sandbox already permits arrives with **no** approval request. The
Codex handler is therefore a second line over patches and sandbox escalations, not a gate on every
command; commands are confined by the writable-roots sandbox and audited by the read event.

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
  banned-vocabulary scan only — code patterns/placeholder/provenance are Python-AST concepts);
  **binary / undecodable** → a `no_fabrication.binary` hard-fail (data never belongs in the
  toolkit, §9.5) that is fed back to the author through the rework loop, never an exception.
  The **structural** checks (`check_structural`) assert: only `*_utils` paths were touched; a
  `test_*`/`*_test` file is among them; and a `TOOL_MANIFEST` entry for `proposed_tool_name`
  exists in the `skills/__init__.py` of one of the packages the **changeset actually touched**
  (`_touched_packages`, derived from the diff) — **never** at a path built from the brief's
  advisory `target_domain`. A descriptive `target_domain` label there once built a nonexistent
  manifest path and rejected forever with a misleading "no entry" message, un-fixable in-run
  ([A12]); deriving the search from the diff removes that whole failure class, and the rejection
  now lists the manifest keys actually present per touched package so a name mismatch is
  diagnosable in round 1. `fix_existing` (a lib-only edit) still passes — its package is touched,
  so its existing entry is found — while a name colliding with an *untouched* package cannot
  false-pass.
- **Layer B (the tool's own tests):** `pytest` in the worktree (via the injectable
  `test_runner`; the default runs the analyst interpreter twice for determinism). **Layer B
  passes only if pytest exits 0 AND ≥1 test PASSED** (`_layer_b_verdict`): `rc==0` alone also
  holds when every test is SKIPPED (validation data absent / hardcoded / off-host path), which
  would merge a tool that never ran — an all-SKIPPED/empty outcome map fails with a specific
  "not verified (§9.4)" reason, not a silent pass. **Determinism is a per-test-outcome compare,
  not a byte compare:** each run writes a JUnit XML report (`--junitxml`), parsed into a
  `{test_id: PASSED/FAILED/ERROR/SKIPPED}` map; the run is deterministic ⇔ the two maps are equal. This ignores log/timing/stdout volatility (chatty
  science tooling — growing INFO filelists, banners) that the old `_strip_volatile` byte compare
  false-flagged, and catches a test *outcome* that flips run-to-run. Each pytest invocation is
  bounded by `_gate_test_timeout` — `max` of `orchestration.yaml: gate_test_timeout_s` (default
  900; no clamp) and every `tool_config.yaml: gate_test_timeout_s` declared by a package the
  **changeset touched** (derived from the diff, not the brief's advisory `target_domain` — [A12]),
  so one pytest run over an authored chain honours the slowest package's ceiling. **Reproducibility is not gate-
  enforced:** for a science tool the lead plans a `test_repro…` test (`proposed_checks`), the
  coder writes it, and the co-lead reviews it — it runs as a normal Layer-B test, so a result
  that wobbles outside tolerance flips its verdict and the determinism map catches it.

The **no-fabrication scan** (`scan_python_source`, pure over source text) is split into
precision tiers so it stays clean on real toolkit code (full of legitimate hardcoded
numeric arrays — XSPEC param vectors, energy bins):

- **Tier 1 HARD-FAIL** — banned **vocabulary** in comments/docstrings/strings: explicit
  fabrication phrases (`example data`, `fake data`, `in operational mode this would`,
  `placeholder`, `TODO`, … whole-word/phrase matched); dotted **code patterns** listed in
  `hard_fail_code_patterns` (matched against attribute/name AST nodes, one report per
  line); **placeholder function bodies** (`pass`/`...`/`raise NotImplementedError`).
- **Tier 3 FINDING** — recorded on `GateReport.findings` (does **not** flip `passed`); the
  step-4 co-lead judges intent: numeric-literal arrays `return`ed / plotted / framed;
  **bare ambiguous descriptors** (`synthetic`, `synthesized`, `illustrative`, `stub`, `not
  implemented`) that recur in honest §9.5 disclosures — soft-flagged, not blocked (C1);
  and dotted **code patterns** listed in `finding_code_patterns` — that list is
  dual-purpose: an entry naming a built-in literal-array detector switches it on, any
  other entry is matched like the hard tier but recorded as a finding. **Which list a
  pattern sits in decides its severity.** Policy since 2026-07-16: the RNG patterns
  (`np.random`, `default_rng`, `.rvs`, …) live in the finding tier — Monte-Carlo tools
  need randomness inside them, so RNG is co-lead-judged, not auto-rejected. A hard match
  suppresses a duplicate finding on the same line; an emptied YAML section reads as
  "nothing to flag in that category" (never a crash).

All scan lists live in **`config/code_gate.yaml`** (tunable without a code edit). A line
referencing a marked `synth…` ObsID is allowlisted so §9.5 fixtures stay legal.
`GateReport.passed ⇔ all Layer-A hard checks pass ∧ ≥1 Layer-B test ran ∧ all pass`
(an untested tool is never mergeable). `report_hash` seeds the §9.9 commit message.
Provenance: a `FigureRef(...)` the tool constructs must pass `sha256` + `produced_by`.
Deterministic coverage in `tests/test_gate.py` (pure scanners + a throwaway git worktree
with an injected Layer-B runner — no live science stack).

### `review.make_review_hook` (Phase 2 step 4 — provider-decorrelated co-lead review)

```python
def make_review_hook(colead=None, *, author_provider: str, web_source=None,
                     monitor=None, allow_correlated: bool = False) -> ReviewHook
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
- **`allow_correlated`** is the one way past that boundary and it is opt-in, off by default, and
  set by no production caller. It exists to *measure* what decorrelation is worth: the sweep runs
  the same brief with a same-provider reviewer and compares. With it set, a `None` colead takes the
  role's first member instead of a decorrelated one, and an injected same-provider colead is
  permitted. Every review under it records a **`review.correlated`** event (level 3, actor
  `analyst_colead`, naming the author provider and the reviewer model) rather than passing silently,
  because a verdict reached that way does not carry the guarantee the same verdict carries in
  production. The launch preflight still asserts the configured roles are decorrelated, so enabling
  it cannot change how a research run behaves.
- **`monitor` is forwarded by `build_authoring_session`**, so the co-lead built from config
  records its ReAct actions (tool calls, `web_search`, reasoning steps) onto the run ledger.
  Until 2026-07-29 that kwarg was dropped at the call site: the co-lead got `monitor=None` and
  only the `AuthoringSession`'s own `review.result` verdict was visible, making the single
  provider-decorrelated check in `commit ⇔ gate ∧ review` the one actor whose work left no
  trace. Regression-locked by
  `test_merge_flow.py::test_build_authoring_session_forwards_monitor_to_the_review_hook`.
- **Diff bundle** (`build_diff_bundle`): reuses `gate.collect_targets` for the file set — new
  files in full, modified files as unified `git diff` (per-file capped, `_DIFF_FILE_CAP`).
- **The call**: `AnalystColeadAgent.review_tool(tool_spec, diff=…, gate=…, tools=…)`, with a
  `ReadOnlyToolProvider` scoped to the **worktree** (`include_text_search=False`) +
  `web_search` (a default `WebSource()` when `web_source` is `None`), so the co-lead
  investigates surrounding code/conventions before its verdict.

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
- `PersistentSandbox(toolkit_root, workdir, *, analyze_env, analyze_ini_sh, repl_init_py, default_timeout_s)` —
  long-lived REPL subprocess per analyst worker. `repl_init_py` (config `repl_init_py`) is a path
  exec'd once at REPL boot for domain **session** setup (imports, non-interactive settings, model
  loads); passed to the subprocess via `AR_REPL_INIT_PY` and run best-effort (failures swallowed).
  Empty/absent ⇒ bare REPL. This replaced the formerly-hardcoded XSPEC/relxill boot, so the sandbox
  core carries no domain code (S1); the XSPEC body now lives in `sandbox/repl_init.py.example`.
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
  human_handler=None, coder=None, allow_correlated_review=False, run_gate=None, run_review=None,
  approve_edit=None, …)`
  → `MergeFlowResult` — the author harness: spawns the Sonnet Claude-SDK author in an
  isolated worktree and drives the L0–L4 bounded rework loop. `run_gate` defaults to the real
  `gate.run_gate`; `run_review` to the real `review.make_review_hook`; `approve_edit` to the
  real atomic commit. On gate-pass ∧ review-pass it sets the commit subject to
  `review.commit_summary` (falling back to `tool_spec.proposed_tool_name`), calls
  `approve_edit`, and returns `status="committed"` with the real `commit_sha`. All hooks stay
  injectable for deterministic tests. **Now a thin driver over `AuthoringSession`** (below): it
  builds the session, then runs the L2/L3 auto-loop calling `drive`/`run_gate_step`/
  `run_review_step`/`commit`. Behaviour is byte-identical to the pre-refactor inline loop.
- `AuthoringSession` — a live authoring session over one `ToolSpec`: a connected Sonnet
  (Claude-SDK) coder on a toolkit branch, with the gate and co-lead review exposed as
  **discrete, separately-callable steps** so a caller can drive them à la carte. Methods:
  `initial_message` (property — the ToolSpec-brief seed turn), `connect()`, `drive(message)`
  (one warm-session coder turn → response text; L0 self-correction inside), `run_gate_step()` →
  `GateReport` (applies the §9.9 commit-backstop, then the deterministic gate — mechanical, only
  *satisfiable*), `run_review_step(gate)` → `ToolReviewVerdict` (the provider-decorrelated
  co-lead — *debatable*), `commit(message)` → sha (the only commit path; sets `committed` and
  advances the per-attempt base ref, in memory and on disk), `pending_changes()` → bool
  (whether the tree holds unlanded work — False right after a commit, True again on the next
  edit; `execute_submit` reads it so a no-op submit is answered, never executed),
  `aclose()` (disconnect + §9.9 non-committed direct-branch tree reset). `propose_and_pr` drives
  these in the auto-loop; the analyst lead's authoring ReAct tools drive the *same* primitives
  directly (so the lead diagnoses each verdict rather than the run terminating).
- `build_authoring_session(tool_spec, *, lead, toolkit_root, worktree_base, colead=None,
  human_handler=None, coder=None, allow_correlated_review=False, run_gate=None, run_review=None,
  max_turns=None, direct_branch=None, worktree_factory=None, sdk_client_factory=None,
  approve_edit=None, monitor=None)` → `AuthoringSession` — resolves config/hooks, picks worktree vs
  direct-branch mode (§9.9), builds the seeded author harness, returns a **not-yet-connected**
  session (`await connect()` before use). Setup only — the sole git touches are the direct-branch
  clean-tree precondition and the per-attempt base-ref record.
  `coder` is the author **member** (`RoleModelConfig`), `None` meaning the one `models.yaml` names,
  which is every production caller. It carries all three author facts at once — the model id
  recorded and priced, the provider the review is routed away from, and the harness that spawns the
  author process — so they cannot be resolved separately and disagree; `_resolve_coder_provider` is
  gone and `_resolve_coder_model()` is now a thin accessor over `coder_member()`. A benchmark passes
  a member directly to sweep the author across vendors with the rest of the flow byte-identical.
  `allow_correlated_review` is a **measurement** switch, off in production and set by no production
  caller: it permits a co-lead sharing the author's provider, which the trust boundary otherwise
  refuses (§9.3), and the launch preflight still guards the configured roles. See
  `review.make_review_hook`.
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
  script); no AST, so code patterns/placeholder/provenance do not apply and `findings` is always empty.
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
  `*_utils` package), `_touched_packages` (the `*_utils` packages the changeset touched,
  derived from `collect_targets`), and `_manifest_keys` are its helpers. Scan lists load from
  `config/code_gate.yaml`. When no `test_runner` is injected, the default is the real
  `_default_test_runner` with the per-package timeout bound via `partial` (so an injected
  runner keeps its `(worktree, test_paths)` signature).
- `gate._default_test_runner(worktree, test_paths, *, timeout_s=900)` → `dict` — the real
  Layer-B runner: runs pytest twice with the analyst interpreter (`--junitxml` to a throwaway
  temp dir), `timeout_s` per invocation. Returns `{results, deterministic, determinism_detail}`;
  `results` carries the `layer_b.pytest` `CheckResult` (`passed ⇔ rc1==0 ∧ ≥1 PASSED`, via
  `_layer_b_verdict`), determinism from the
  two per-test outcome maps.
- `gate._parse_junit_outcomes(junit_path)` → `dict[str,str]` — pure: parse a JUnit XML report
  into `{"<classname>::<name>": PASSED/FAILED/ERROR/SKIPPED}` (read from each `<testcase>`'s
  child element). Returns `{}` on an absent/unparseable report.
- `gate._outcomes_determinism(map1, map2)` → `(bool, str)` — pure: deterministic ⇔ the two maps
  are identical (keys + values); the detail names every diverging test. Catches an outcome flip
  and the same-count/different-tests swap a bare return-code compare misses.
- `gate._gate_test_timeout(worktree, targets)` → `int` — resolve the per-pytest-invocation
  Layer-B timeout: `max` of `orchestration.yaml: gate_test_timeout_s` (default 900) and each
  `<worktree>/<pkg>/tool_config.yaml: gate_test_timeout_s` for every package the changeset
  touched (`_touched_packages`), so one pytest run over an authored chain honours the slowest
  package's ceiling. No clamp (trusted infra), coder-settable; values are read (never imported)
  from the worktree. Derived from the diff, not the brief's advisory `target_domain` ([A12]).
- `jail.wrap(argv, policy)` → `list[str]` — `argv` prefixed with the jail, or unchanged when the
  jail is off or unavailable. The one call every insertion point makes.
  - `jail.policy_from_config(*, network, label)` → `JailPolicy` — `agent_fs_allow` split by
    `agent_fs_readonly` into writable and read-only roots.
  - `jail.coder_policy(*, label, extra_rw)` → `JailPolicy` — the author-harness policy. An author
    writes to **two places only**: its working tree (`extra_rw`) and its own settings home;
    everything else on the allow-list is read-only, and the temp root is **absent** entirely. That
    tightening is what makes it safe for the Codex harness to stand its own sandbox down inside the
    jail: the surrendered `workspace-write` confined writes to the working tree, and so does this,
    while the observation database becomes read-only so no author can write a data file where a
    fabricated result would live. The temp root is dropped rather than demoted because trial working
    trees live under it, and a readable one would let one author read another's solution — which
    invalidates a repeated-trial measurement instead of breaking anything visibly.
    `~/.claude/.credentials.json` is bound read-only **at the address the CLI will read**, not at its
    own path: the jail replaces `$HOME`, so a same-path bind leaves it somewhere the process never
    looks (every Claude author died that way, 2026-08-20).
  - `jail.coder_home(label)` → `Path` — a writable `$HOME` for one author, on **local disk** rather
    than under the runtime dir like `jail_home`: both harness CLIs keep state in SQLite and the
    runtime dir is an NFS mount, where it cannot take locks. Bound writable by the policy itself, so
    it does not depend on the allow-list naming the temp root.
  - `jail.codex_settings_home(label)` → `Path` — the fresh `CODEX_HOME` a jailed Codex author gets,
    under its `coder_home`. The default is the operator's own directory, which is not in the jail.
  - `jail.binary_reachable_in_jail(binary, *, label)` → `(bool, str)` — whether a path is executable
    *inside* the jail, tested by `test -x` in the namespace rather than by running the vendor CLI
    (a `--version` may authenticate or reach the network).
  - `jail.harness_binary(harness)` → `str | None` — the CLI a harness execs, asked of each SDK
    rather than probed on `PATH`, since both ship a bundled binary `which` cannot see.
  - `jail.jail_argv(policy)` → `list[str]` — the bwrap prefix; `[]` when bwrap is absent.
  - `jail.write_launcher(path, target, policy)` → `Path` — a `#!/bin/sh` wrapper that execs
    `target` inside the jail, passing `"$@"` through; what the harnesses' binary-path options get.
    **Binds the binary's own directory read-only and execs the resolved path**: both harnesses ship
    their CLI inside the orchestration environment, a tree the allow-list deliberately does not name,
    so nothing else would bind it — and bind and exec must agree on one spelling, since a resolved
    bind with an unresolved exec fails `execvp` on a host reached through a symlink.
  - `jail.jail_home(label)` → `Path` — a writable `$HOME` per process kind under the runtime dir.
    Authors use `coder_home` instead (SQLite on NFS).
  - `jail.jail_available()` → `(bool, str)` — runs a trivial jailed command, because user
    namespaces can be present and administratively denied.
  - `jail.check_os_jail_available()` — launch **warning** when the jail is configured on a host
    that cannot run it. Registered in `auto_research.preflight`.
  - `jail.check_os_jail_probe()` — launch **failure** when the configured `analyze_probe` does not
    run inside the jail. Registered in `auto_research.preflight`.
  - `jail.check_os_jail_harness()` — launch **failure** when the configured author harness's CLI is
    not reachable inside the jail. The probe above execs the *analysis interpreter*, which is why it
    passed while every author died; this one execs the author's own binary. Registered in
    `auto_research.preflight`; the authoring benchmark runs it per leg, since a sweep uses harnesses
    `models.yaml` does not name.
- `coder.backend.CoderBackend` — the author-harness protocol: `connect()`, `drive(message) -> str`,
  `disconnect()`. A backend also streams `coder.*` events and books its own spend.
- `coder.harness.HARNESS_ENDPOINTS` → `dict[(provider, harness), str | None]` — the legal
  pairings; the value is the endpoint that vendor documents for that harness, or `None` when the
  harness is native to it. A pair absent from the table does not exist.
  - `coder.harness.coder_member()` → `RoleModelConfig` — the single configured `coder` member;
    raises when the role carries more than one (one harness drives one model).
  - `coder.harness.check_coder_harness_supported()` — launch preflight: the pair exists, a
    non-native pair names its endpoint, and the member's `key_env` is set. Registered in
    `auto_research.preflight`.
  - `coder.harness.check_coder_harness_available()` — launch preflight: the configured harness's
    CLI resolves (asked of the SDK, not probed on `PATH`). Registered in `auto_research.preflight`.
- `coder.codex_bin.resolve_codex_bin()` → `str` — the Codex CLI a run would launch: `$CODEX_BIN`,
  else the SDK's own resolver (the version-matched binary it ships), else `PATH`.
  `configured_codex_bin()` returns only the explicit override, so the backend can pass `None` and
  let the SDK resolve its own.
- `coder.claude_sdk.ClaudeSdkBackend(client, *, monitor, coder_model)` — the Claude Agent SDK
  harness. The client is injected, which keeps `sdk_client_factory` usable as a test seam. The
  SDK-specific helpers (`_build_options`, `_make_fs_guard_hook`, `_make_escalation_server`,
  `_drive_author`, `_record_coder_cost`, `_with_live_anthropic_key`, and the deprecated
  `_make_path_confiner`) live here and are re-exported from `merge_flow` for callers and tests.
- `coder.codex_sdk.CodexBackend(*, member, worktree, system_prompt, guard, escalation=None, …)` —
  the Codex app-server harness. `approve(method, params)` is its in-process approval handler;
  `_config_payload()` builds the thread config (writable roots, the vendor provider block, the
  escalation MCP entry); usage from each `thread/tokenUsage/updated` is booked through
  `CostMeter.record_call`.
  - `coder.codex_sdk.resolve_vendor_key(provider, key_env)` → `(key, slot)` — the key an author
    launches with plus a **log-safe** slot name (`primary`/`fallback1`/`env`/`unset`), never the
    value. For a provider in `keys.ROTATING_PROVIDERS` whose member names that provider's standard
    primary variable, this reads the **live** key from the process key pool rather than the raw
    environment variable, because by the time an author is spawned a chat role may already have
    rotated off a spent primary. A member naming a plan-specific variable, and every harness-only
    vendor, reads its own variable. Used by both `_build_client` (the child-environment overlay,
    where a pooled key overrides an inherited one while an environment-sourced key leaves an
    inherited value alone) and `_login`, and shared with the launch checks so a probe cannot report
    on a credential the run does not use.
- `coder.escalation_server.EscalationServer(ask_lead, ask_human)` — a Streamable-HTTP MCP endpoint
  serving the two escalation tools from **this** process (the handlers must reach the live lead
  object and the run's HITL connector). Loopback, ephemeral port, per-session bearer token;
  `url`, `env()` and `token_env` are what a harness needs, `start()`/`aclose()` its lifecycle.
- `coder.events.emit` / `cap_text` / `coder_text_cap()` — the shared ledger emission, so a backend
  records without importing the flow that drives it. `merge_flow._emit`/`_cap_text` alias them.
- `merge_flow._build_backend(...)` → a `CoderBackend` — the harness dispatch, on
  `coder_member().harness`. For Codex it turns the `agent_fs_allow` list into `writableRoots` and
  builds the escalation endpoint; for Claude it builds the SDK options and wraps the client.
- `review.make_review_hook(colead=None, *, author_provider, web_source=None, monitor=None,
  allow_correlated=False)` → the default `run_review` hook (§9.3 step 4); the
  provider-decorrelated co-lead review. `allow_correlated` is measurement-only (above).
  - `review.select_decorrelated_member(group, author_provider)` → `ModelHandle` — picks the
    `analyst_colead` member with `provider != author_provider`; raises if none exists.
  - `review.build_diff_bundle(worktree)` → `str` — new files in full + modified files as
    unified diffs (reuses `gate.collect_targets`; per-file capped).
  - `review.check_author_reviewer_decorrelation()` — launch-time preflight assertion (no LLM)
    that the `coder` author and `analyst_colead` reviewer are provider-decorrelated; raises
    via `select_decorrelated_member`. Registered in `auto_research.preflight`.

## Changelog
Compacted 2026-09-01: entries through 2026-08-31 folded into themed ranges (detail in git history).

- 2026-05-26 -> 2026-06-16 — **Phase 1 sandbox/tooling, then the whole Phase 2 authoring flow.**
  Manifest-based `ToolkitRegistry` (disabled-tool filtering, provider-native tool defs), persistent
  JSON-line sandbox (fd capture, non-interactive XSPEC boot), per-run workspace/memory/figures layout,
  built-in MCP server/client, config-driven runtime paths. Then, with all writes pinned under the runtime
  data dir: `merge_flow.propose_and_pr()` (isolated-worktree / `direct_branch` Sonnet author, runtime-safe
  context, `ask_lead`/`ask_human`, bounded L0–L4 rework, typed no-commit failure); `gate.run_gate`
  (Layer-A structural/provenance/determinism + no-fabrication scan, Layer-B test runner, extension-agnostic
  over `*_utils`); `review.make_review_hook` (provider-decorrelated GPT co-lead); `approve_edit` (one atomic
  commit, explicit-path staging, never `git add -A`); launch preflights
  (`check_author_reviewer_decorrelation`, `check_toolkit_git_identity`/`_branch_clean`).
- 2026-06-18 -> 2026-06-30 — **Live-run hardening (fix-lists #1–#11), the authoring refactor, and the
  `bash_exec` hang fix.** Gate survives slow/erroring tests (`TimeoutExpired` caught; `propose_and_pr`
  exception backstop → `status="failed"`); the SDK author's `Bash` runs in the analyst code-exec env;
  `bash_exec` rescoped read-only; timeouts decoupled from the model (`call_timeout_ceiling`,
  `gate_test_timeout_s`); `propose_and_pr(monitor=…)` streams events; the FS jail landed
  (`fs_guard.PathGuard` + `agent_fs_allow` `PreToolUse` hook, `_make_path_confiner` deprecated); git
  discipline (auto-reset to the per-attempt `base_ref` via `_reset_direct_tree` +
  `tool_author_failed_*.patch`, coder Bash deny-rule, gate-entry auto-revert); gate fixes (annotated
  manifests; stub/not-implemented → co-lead finding; determinism = per-test JUnit outcome maps, not a
  byte compare). The inline authoring loop decomposed into an `AuthoringSession`
  (`drive`/`run_gate_step`/`run_review_step`/`commit`/`aclose` via `build_authoring_session`) with
  `submit_cap`/`debate_cap` + the co-lead debate path, driven by `lead.drive_authoring` (commit still ⇔
  gate ∧ review); coder escalation tools take one Markdown `message`. `bash_exec` runs
  `stdin=DEVNULL, start_new_session=True` with a process-group kill on timeout, and
  `MCPToolClient.call_tool` bounds its await (900 s) — fixing a whole-run hang from an interactive FTOOL
  wedging the MCP command channel.
- 2026-07-06 -> 2026-07-16 — **Gate corrections, the domain-agnostic REPL boot, and the RNG demotion.**
  [G2] Layer-B passes only if pytest exits 0 **and** ≥1 test PASSED (an all-SKIPPED suite fails rather
  than merging an untested tool). [C1] bare ambiguous words (`synthetic`/`illustrative`) moved from
  hard-fail to soft `finding_vocabulary`. **[A12]** `structural.tool_manifest_entry` and
  `_gate_test_timeout` derive their `*_utils` targets from `_touched_packages` (the changeset) rather
  than the unvalidated `tool_spec.target_domain`, removing the run-#12 doomed loop. [I1] the co-lead uses
  `WebSource()` directly (lazy client, key is a preflight var), dropping `_resolve_web_source`.
  **Installer increment 1:** `PersistentSandbox` gained `repl_init_py` — a config-pointed file exec'd once
  at REPL boot — **replacing the hardcoded XSPEC/relxill `_configure_xspec_once()`** (the deepest domain
  leak), and `builtin_server` builds the `bash_exec` description from the configured `analyze_probe`; new
  `runner.check_analyze_probe()` preflight. **Scan config robustness:** every `code_gate.yaml` list read is
  `or []`-guarded (an emptied section used to crash the scan), and `finding_code_patterns` became
  dual-purpose so a dotted pattern there is matched like the hard tier but recorded as a finding — which
  list a pattern sits in decides severity, and without this the human's RNG demotion (Monte-Carlo tools
  need randomness; the co-lead judges intent) did nothing.
- 2026-07-22 -> 2026-07-29 — **Branch self-heal, the tool-context diet, and two audit-trail fixes.**
  Durable-resume Stage 2: `check_toolkit_branch_clean` raises the distinct `ToolkitBranchUnclean` so the
  mid-run path degrades to an honest §9.6 terminal; `build_authoring_session` persists the per-attempt
  base-ref; new `heal_toolkit_branch_if_dirty` resets a dirty authoring branch to it on resume (saving a
  `resume_heal_*.patch`, best-effort, sharing `_save_diff_and_reset` with `_reset_direct_tree`).
  **The docstring override in `ToolkitRegistry.load()` was removed** — since Phase 1 it had replaced each
  `TOOL_MANIFEST` description with the backing function's `__doc__` whenever the module imported cleanly,
  which Phase 2 inverts (the manifest entry *is* the agent-facing interface the coder authors and the
  co-lead reviews), with the winner decided by the tool's dependency footprint; −~1,400 model-visible
  tokens. **Tool search**: `SandboxToolProvider.tool_definitions()` marks every registry tool
  `defer_loading: true` under `tool_search_enabled` while harness tools stay resident (also satisfying
  both providers' "≥1 non-deferred tool" rule); 19,004 → 2,382 resident tokens, live-verified and enabled
  2026-07-28. Prompts updated (`analyst_worker.md` teaches the search step; `coder.md` the one-line-brief
  split). Audit trail: `build_authoring_session` now forwards `monitor` into `make_review_hook` (a dropped
  kwarg had left the provider-decorrelated half of `commit ⇔ gate ∧ review` with no record of its actions
  behind its verdict), and the coder's Agent-SDK spend reaches the cost record via
  `_record_coder_cost` → `CostMeter.record_session_total` (session-cumulative, folded in as per-turn
  deltas, `price_source="claude_sdk"`) — historically the largest unaccounted line.
- 2026-07-30 -> 2026-07-31 — **The sandbox can be killed on demand, and the kill takes descendants with
  it.** `PersistentSandbox.abort()` is the on-demand form of the timeout path: it kills the REPL so the
  in-flight `readline` ends in EOF, `call_tool` returns its ordinary non-zero result (the caller gets a
  real result, which is what keeps a tool_use/tool_result transcript valid), and the next call spawns a
  fresh REPL; `run_research` registers each worker's `abort` on `RunControl`, which is how a `/stop` ends
  a 25-minute download. `_kill_current` then gained descendants: it collects them from
  `/proc/<pid>/task/*/children` and SIGKILLs deepest-last, since an XSPEC fit or a HEASARC download used
  to keep writing into the obs-db after the caller believed the sandbox was gone (§13 [F5]). Deliberately
  **not** `killpg` — the REPL shares the *backend's* process group, so a group kill would take the run
  down, and that shared group is exactly what lets an external `kill -9 -<pgid>` reach the sandboxes when
  our cleanup never runs (the §12 hard-kill fallback). New `kill_now()` is the human-interrupt entry point
  (no handshake, no grace — the next cleanup step, the toolkit-branch reset, is only correct once nothing
  can write), and `AuthoringSession.aclose()` is idempotent since the interrupt path closes it from
  shielded cleanup. Tests: `tests/test_interrupt.py`.

- 2026-08-10 -> 2026-08-19 — **The coder subprocess follows a key rotation, then a second author
  harness and the live matrix that vetted it.** **Key rotation:** `_build_options` runs its `env`
  through `_with_live_anthropic_key`, stamping `ANTHROPIC_API_KEY` **only when the live key
  (`models/keys.py`) differs from the process environment's** — no rotation ⇒ options unchanged,
  connected sessions unaffected
  (`tests/test_provider_key_failover.py::test_coder_env_{is_untouched_while_the_primary_key_is_live,carries_the_rotated_key}`).
  **Second harness:** new `coder/` package. `CoderBackend` (`connect`/`drive`/`disconnect`) is
  everything the authoring flow needs from an author, so gate, review, commit, per-attempt base ref and
  failure reset are untouched by which process wrote the code; `claude_sdk.py` holds today's SDK wires
  verbatim (still re-exported from `merge_flow`, callers and tests unchanged), `codex_sdk.py` adds the
  Codex app-server, `merge_flow._build_backend` dispatches on `coder_member().harness`,
  `AuthoringSession` holds a backend. Legal pairings are a table (`coder/harness.py`), each non-native
  row carrying the endpoint its vendor documents (verified 2026-08-19 against DeepSeek's, Z.AI's and
  Alibaba Model Studio's own pages); Anthropic publishes no Responses endpoint and OpenAI no
  Anthropic-dialect one, so those pairs are **absent rather than rejected**. Preflights
  `coder_harness_supported` / `coder_harness_available` run before any model is built. **Codex seams:**
  the allow-list becomes `writableRoots` under `workspace-write` (the OS-enforced write jail deferred
  since Phase 2 began), `.git` read-only inside it, so the git ban is structural, not a deny-rule; escalation moves to an in-process Streamable-HTTP MCP endpoint (`escalation_server.py`,
  loopback + per-session bearer token — `ask_lead`/`ask_human` must reach the live lead object and the
  run's HITL connector); **reads are the regression**, full-disk read access being documented design, so
  a crossing is only recorded as `coder.read_outside_allowlist` and the preventive form is the §12 OS
  jail. Measured, not assumed (live probe ~$0.02): under `approvalPolicy="untrusted"` +
  `approvalsReviewer="user"` a patch approval reaches the client handler and a denial is honoured, but a
  shell command the sandbox already permits arrives with **no** approval request (the handler is a
  second line, not the command gate); `developerInstructions` and `baseInstructions` are both honoured,
  the role prompt riding as the former to layer over Codex's own tool account **rather than replacing
  it**; the Python SDK *merges* `CodexConfig.env` into the inherited environment (the TypeScript SDK
  documents replacement); per-model-call token classes price Codex-leg spend by the same code as every
  chat role. `tests/test_coder_backends.py` (25, incl. live-shaped approval payloads and a real loopback
  MCP round-trip) + env-gated `tests/test_live_coder_matrix.py`; suite 967 → 995.
  **The matrix.** The three `claude_sdk` alt-vendor rows are gone (that backend sets no base URL or auth
  token, so a member naming one would have silently authored on Claude); five pairs remain, the module
  recording why the other three are absent. `tests/test_live_coder_matrix.py` walks one warm session per
  leg through nine stimuli in real-attempt order, frozen out of real ledgers
  (`tests/fixtures/coder_history.json` via `tmp_scripts/extract_coder_history.py`: a 2 kB lead
  instruction, a `no_fabrication.vocabulary` rejection, a three-issue co-lead rejection, a coder
  question); legs skip on a missing credential. Its first invocation died on a missing import — the
  `NameError` class the 2026-08-06 guard prevents, in a file that guard did not scan — so
  `tests/test_no_undefined_names.py` now scans `tests/` and `benchmarks/`, finding
  `tests/test_live_agents.py` still constructing a `LibrarianSearcherAgent` for a deleted role (suite
  995 → 1093, ~98 cases). **Four Codex defects, none visible to a deterministic test:** (1) MCP
  servers are read when the app-server **process** starts, not per thread, so a server passed in
  `thread_start(config=…)` never connected — the config now rides as flattened `--config` overrides
  (`_flatten_overrides`, TOML literals); (2) an MCP tool call arrives as an **elicitation**
  (`mcpServer/elicitation/request`, `_meta.codex_approval_kind = "mcp_tool_call"`) answered in the MCP
  spec's `{"action": "accept"}`, so the `{"decision": …}` used for patch approvals reads as a rejection
  ("user rejected MCP tool call") with our handler never reached, and a server the runtime did not
  configure is now declined; (3) command-parse paths are **relative** to the command's cwd — 47 false
  crossings; (4) `cwd` is a `RootModel` whose `str()` yields `root='/tmp/…'` — 40 more. Test-design
  finding: demanding an *edit* asks a throwaway toolkit to contain the files a recorded rejection names,
  so cells assert **delivery** and record `tree_changed` as data (`test_coder_backends.py` 25 → 33).
  **Vendor keys.** DeepSeek and Z.AI answered on the first turn; Qwen refused every attempt with HTTP
  400 `AccessDenied.Unpurchased` across three models and all three documented hosts — entitlement, not
  wiring (the pay-as-you-go host recognised the key; the CN and token-plan hosts said `InvalidApiKey`).
  Recorded three ways: `UNVERIFIED_PAIRINGS` in `coder/harness.py`; a new **`PreflightWarning`** tier so
  `check_coder_harness_supported` cautions at launch without blocking (ledger level 2; the summary
  counts warnings apart from failures); and a live-matrix skip carrying the vendor's own refusal — a
  **classification, not a convenience**. A failed Codex turn had returned an empty string the lead reads
  as a silent no-op; it now returns `[harness error] …` carrying the vendor's message. Suite 1108 → 1112.
  **Final sheet** (`logs/dev/coder-matrix/`, ten cells per leg):

  | leg | model | usage | cost | reads outside the allow-list |
  |---|---|---|---|---|
  | `claude_sdk`+anthropic | `claude-sonnet-5` | 11 calls | $1.69 | **0 — refused** |
  | `codex`+openai | `gpt-5.6-terra` | 30 calls | $0.354 | 2 |
  | `codex`+deepseek | `deepseek-v4-flash` | 100 calls | $0.041 | **36** |
  | `codex`+zai | `glm-5.3` | 69 calls | $0.527 | 2 |
  | `codex`+dashscope | `qwen3.7-plus` | — | — | skipped: account not entitled |

  Both credentialled legs green (6m46s): escalation round-trips end to end on each harness (`ask_lead`
  3 / `ask_human` 2 on Claude, 3 / 1 on Codex — model → localhost MCP → in-process handler → answer),
  neither harness let the coder commit, spend booked on both ($1.84 notional on the subscription, $1.29
  real). **Read asymmetry, measured:** the Claude leg refused the governance file (zero crossings), the
  Codex leg read it with exactly **2** recorded, against the false counts before the path fixes. **The DeepSeek row is the finding:** unprompted it read `coder/codex_sdk.py`,
  `sandbox/DOCS.md`, `tests/test_live_coder_matrix.py`, that test's fixture, the installed Codex SDK
  sources and `~/.codex/config.toml` — the §2/§4 boundary as a measurement, denied on Claude,
  recorded-and-allowed on Codex, exactly what the §12 `bwrap` jail blocks. Four more defects found by
  running it: `glm-5.3` refuses every turn unless a reasoning effort is named ("This model always
  engages in thinking and cannot be disabled; please use low, high, or max"), so a member's `parameters`
  now flow into the Codex config; litellm knows `qwen3.7-plus` with no rates, pricing a leg at $0.00
  under an authoritative-looking `source="litellm"` that shadowed `priceMTk` (a rate-less entry is now
  treated as absent); `_touched_paths` sliced git porcelain at a fixed offset, clipping a path's first
  character; three assertions measured model compliance rather than the harness contract (a reply's
  wording, a tool *called* rather than *offered*, a priced rather than a booked cost) — the contract is
  what the matrix asserts now, behaviour is recorded. Suite 1112 → 1115.

- 2026-08-20 -> 2026-08-31 — **The author became a parameter, the OS jail met its first author, and an
  authoring attempt became durable at the gate.** **(1) One coder member replaces three config reads:**
  `build_authoring_session`, `propose_and_pr` and `AnalystLeadAgent.drive_authoring` take
  `coder: RoleModelConfig | None`; `_build_backend` takes `member=` instead of `coder_model=`; model id,
  review-routing provider and spawning harness come from one object so they cannot disagree;
  `_resolve_coder_provider` deleted, `_resolve_coder_model` a thin accessor; `test_coder_backends.py`
  proves dispatch by *passing* members rather than monkeypatching ambient config. **(2)
  `make_review_hook(..., allow_correlated=False)`** — the only way past the decorrelation boundary,
  opt-in, set by no production caller, so the sweep can measure what decorrelation is worth; every
  review under it emits `review.correlated`. **(3)
  `coder.codex_sdk.resolve_vendor_key`** — the Codex author read its vendor key from the raw environment
  variable at two places, so it always launched on the **primary** even after a chat role had rotated
  off a spent one (found with the OpenAI primary out of credits, `429 credit_balance_exhausted`, while
  the configured fallback answered): worse than the §13 note that a session in flight keeps its key,
  because it never *started* from the live one. One shared resolver now reads the pool for a rotating
  provider whose member names that provider's standard primary variable, the raw variable otherwise (a
  plan-specific variable must not be shadowed; harness-only vendors have no cited taxonomy so they never
  rotate); a pooled key overrides an inherited environment value, an environment-sourced one does not.
  Suite 1136 → 1160.
  **The jail's first author** failed four ways, each hidden behind the last, all at startup for $0:
  the launcher **never bound the binary it execs** (both CLIs ship inside the orchestration environment
  the allow-list deliberately does not name — `bwrap` failed `execvp` on all 40 trials; it now binds the
  binary's parent read-only); **bind and exec disagreed on the path spelling** (the bind resolved
  symlinks, the exec did not, so the Claude author still failed while Codex survived only because for it
  the two coincide — and the launch check resolved too, reporting "reachable" while the launcher failed:
  *a check that normalises differently from the thing it checks is not a check*); **Codex was pointed at
  the operator's own settings directory**, absent from the jail, now a fresh one; **both CLIs keep state
  in SQLite while the runtime dir is NFS**, which cannot lock, so author homes moved to local disk. Two
  defects of the fix: the re-addressed credential was built from the unresolved runtime path while the
  home is bound resolved, and a label from the clone's *directory name* collided across concurrent
  authors (every trial clones into a directory called `repo`). **Then the structural one: this harness's
  own sandbox IS bubblewrap**, so jailing it nests the same mechanism — Codex cannot spawn
  `codex-linux-sandbox`, every shell command is rejected, and the author reaches the gate having read
  and written nothing, reading as a weak model rather than broken plumbing; the coder diagnosed it and
  escalated, the honest-failure path working. Resolved as **one enforcement layer**: the harness sandbox
  stands down when the kernel jail is on, and `coder_policy` tightens the author to two writable places
  (working tree, own settings home), *stricter* than the `workspace-write` it replaces — the observation
  database is read-only and the temp root absent, so one author cannot read another's solution. **Live-verified end to end:** two concurrent authors per harness starting jailed,
  the Claude pair authenticating from the subscription and answering, and a full production trial
  authoring both toolkit halves plus tests and passing the deterministic gate on its second submission.
  `tests/test_jail.py` 19 → 28; suite 1136 → 1176. **Lesson recorded:** a hand-built probe is not the
  production path — mine failed with the jail *off* too.
  **Submission snapshots (2026-08-31)** close the volatile stretch between "coder starts" and
  `approve_edit`, where any crash (incl. a review-provider failure on a gate-passed tree) reset the
  branch to the base ref and forced a full re-authoring — ~$27–45 lost to save a ~$1–3 review.
  `AuthoringSession.run_gate_step` persists the working tree at every submit, after the §9.9
  coder-commit backstop and before the scan, as `run_dir/authoring/submissions/<tool>/submit_NN.patch` +
  `state.json` (base ref, branch, spec id, and — appended as they land — the gate outcome and the
  co-lead's issues); every submission is kept, failed ones included. `build_authoring_session`
  (direct-branch mode) restores the latest snapshot **only when its recorded base ref equals current
  HEAD** via `git apply` (a working-tree write; `approve_edit` stays the sole commit path), marks the
  session `restored_submission`, and seeds the coder with a "Restored prior work" section carrying the
  last feedback (continuation over tree + feedback, not a restart). The gate re-runs on the restored
  tree: no stored verdict is ever trusted and `commit ⇔ gate ∧ review` stays defined over the live
  tree. `commit()` discards the tool's snapshots; an unappliable snapshot is
  discarded and authoring starts clean (an economy, never a dependency); worktree (non-direct) mode is
  unchanged. Snapshots live under the run's authoring dir, outside both repos, so the failure reset,
  `heal_toolkit_branch_if_dirty` and the clean-tree preflight neither see nor destroy them. Events
  `authoring.snapshot.saved` (L5) / `.restored` (L3) / `.error` (L4); new `save_submission_snapshot`,
  `find_submission_snapshot`, `restore_submission_snapshot`, `discard_submission_snapshots`,
  `_working_tree_patch`, `_update_last_submission`; `tests/test_merge_flow.py` covers the
  checkpoint→reset→restore round-trip incl. the seed section, the moved-base-ref refusal, commit cleanup
  and corrupt-patch discard (41/41 + adjacent 278 green). **LIVE-VERIFIED 2026-09-01**
  (`tests/test_live_snapshot_resume.py`, production functions only, real Sonnet coder + real gate + real
  GPT co-lead, the frozen run-46ed6671 brief on a clone at its base SHA): phase 1 authored to a gate
  pass; the in-flight review was cancelled with no cleanup (a hard kill's shape);
  `heal_toolkit_branch_if_dirty` reset the branch; the rebuilt session restored submission 1 and the new
  coder continued on the restored tree (submits 2–4, each gate-judged, two full co-lead reviews), ending
  in an honest no-commit within the 2-round review cap — a correct outcome; **the commit path stays
  covered deterministically, not live.** 48:40 wall. Three attempts, each a finding: (1) a clone nested
  under `/tmp/pytest-of-*` is deleted by the coder's own pytest runs rotating that basetemp (keep
  worktrees out of pytest's basetemp — [F7] family); (2) live tests must `load_dotenv()`; (3) a revoked
  subscription OAuth token surfaces as ordinary coder text ("Failed to authenticate…") and burns gate
  rounds on an empty tree — the [A13] shape, with an early-abort guard on auth-failure replies a
  recorded candidate. Attempt 1 also live-demonstrated the honest-failure moat: worktree destroyed
  mid-attempt, the coder escalated, diagnosed the deletion, found the tool already shipped in the real
  toolkit, and stopped rather than fabricate.

- 2026-09-01 — **The author's `ask_human` answered with the author's own question; three
  different failures were one indistinguishable string.** Both harnesses expose `ask_human` over
  MCP, and every MCP stack turns an exception raised inside a tool handler into an `isError`
  result carrying only `str(exc)` (`mcp/server/lowlevel/server.py:583`;
  `claude_agent_sdk/_internal/query.py:694`). `resolve_human` raises `HumanInputRequired(
  query.headline)`, so a coder asking *"Which calibration epoch applies?"* was answered with
  *"Which calibration epoch applies?"* — measured, not inferred. It reads as a harness bug,
  invites a retry, and burns author turns, which is the coder-pressure path. Worse, the catch is
  `except Exception`, so a **missing** handler, a **broken connector** (`ConnectionError`) and a
  **wrong-typed** handler (`TypeError`) all arrived identically at the author *and* in the ledger
  (`coder.ask_human` fires before the call; `coder.ask_human.answer` simply never appears) — a
  live channel fault was indistinguishable from a healthy unattended run.
  `_make_ask_human_handler` now **classifies before the boundary**: unattended →
  `coder.ask_human.unattended` (L3) and a re-raised `HumanInputRequired` carrying the new
  `UNATTENDED_GUIDANCE`; channel fault → `coder.ask_human.error` (L1) and a `RuntimeError` naming
  the fault. Both stay a tool **error**, so an unanswered question is never dressed as an answer
  and no `.answer` event is written for either; the typed `HumanInputRequired` still reaches any
  caller that catches it, so the §9.8 contract and `author_bench/trial.py`'s fail-safe are
  unchanged. `UNATTENDED_GUIDANCE` sends the author to the **web first and hard** (the strongest
  resource it has with nobody to ask), then `ask_lead`, then to reason and state the assumption
  in the code and its test — and is **domain-neutral by construction**, naming channels rather
  than kinds of document, because `prompts/coder.md` and the toolkit are not tied to one field.
  Also: `CODER_TEXT_CAP` became `coder_text_cap()`, read from config at call time (see the root
  `DOCS.md` entry of the same date). Tests: `tests/test_coder_backends.py` 46 → 49, plus the
  new live probe `tests/test_live_no_hitl.py` (`AUTO_RESEARCH_LIVE_NO_HITL=1`), which drives
  the real author backend through three cells — unattended, channel fault, attended control —
  and asserts the mirror regression against **what the author says it received**, so the whole
  MCP round trip is covered rather than the handler alone. **LIVE-VERIFIED 2026-09-01, first
  run, no rework:** the three arms produced `.unattended` / `.error` / `.answer` with no
  `.answer` in either failure arm, and the author **distinguished the two failures unprompted**
  — naming one a `ConnectionError` outage and the other nobody being there — which is what the
  classification exists to make possible and what one flattened string could not support. It
  reported receiving the guidance rather than its own question (the mirror is dead), recited
  the three steps back, and then **acted on their ordering**, choosing `ask_lead` over the web
  with a stated reason. In the fault arm it said outright *"I will not guess silently or
  hardcode a calibration epoch choice without grounding it."* All three arms refused to invent
  a `ToolSpec` to work from. Note what this does **not** cover: the leg drives the backend
  directly, so no lead, no gate and no commit are exercised — end-to-end authoring is
  `test_live_loader_authoring.py`'s job, not this file's.

- 2026-09-01 (same day) — **What the audit found in the surrounding wiring, recorded because it
  overturned a plan premise.** Withholding the tool (the "option (b)" a benchmark plan was about
  to choose) **was already the code** for three of the four asking roles: `ask_human` is appended
  only when a handler is wired (`react.py:1196` worker; `react.py:1904-1906` lead and reviewer)
  and the lead's authoring loop passes `include_plan_and_debate=human_handler is not None`
  (`roles.py:1460`), so headless the worker, lead and reviewer **cannot ask** and
  `HumanInputRequired` is unreachable from them. The corresponding "catch it at the trial
  boundary" arm was therefore **dead code**: `author_bench/trial.py:411`'s `except
  HumanInputRequired` has never fired across ~180 trial records (every terminal is
  `committed`/`failed`/`error`; zero `needs_human`), and that file's docstring claim to the
  contrary is wrong. The author was the only live exposure, which is what the entry above fixes.

- **2026-09-02 (second) — an approved commit becomes the attempt's base ref, and a negated banned
  word is a judged finding.**
  **`commit()` advances `_base_ref`** (in memory and in `run_dir/authoring/current_base_ref`).
  The base ref means "the commit this attempt's work sits on", so once `approve_edit` lands one,
  that commit *is* the base. It was never advanced, and three readers took the stale value: the
  §9.9 gate-entry backstop reverts on `HEAD != _base_ref` without consulting `self.committed`, so
  it could not tell an approved commit from a coder-slipped one and **a `submit_for_review` after a
  successful commit reverted that commit**; `aclose`'s failure reset would reset *behind* a
  legitimate commit; and `heal_toolkit_branch_if_dirty` does a `reset --hard` to the persisted
  value, so a hard kill after a commit would have healed the tool off the branch. Observed live
  (block 0 attempt 5, link 2): `1523349` -> reverted -> `49bbc53` -> reverted -> `da507d7`, three
  full gate+review revolutions at ~$0.71 each, self-limiting only because the next link re-recorded
  the base ref. **Pre-existing, not from the forms refactor** (`drive_authoring` had the same
  exposure since `c2253d5`); the refactor multiplies it, N links giving N post-commit
  opportunities over one conversation. Guarding the revert with `if not self.committed` would fix
  the first reader and leave the other two wrong.
  **Advancing the base ref alone is not enough, and the first draft of this fix was worse than the
  bug.** With the revert gone, a post-commit `submit_for_review` scans an *empty* changeset and
  then asks `approve_edit` to `git commit` with nothing staged — a non-zero exit that raises
  straight out of the tool call, and `_react` does not catch tool exceptions ([F6]). Caught by
  replaying the ledger's actual sequence through `execute_submit` rather than calling
  `run_gate_step` directly, which is what the first test did. So `AuthoringSession.
  pending_changes()` reports whether the tree holds unlanded work (clean **and** `HEAD == base_ref`
  ⇒ nothing pending), and `execute_submit` answers a no-op submit with a model-visible "already
  committed, nothing has changed" instead of executing it — **budget-free**, since answering a
  question is not a submission, where the live run spent two of its five on this. The guard keys
  on *nothing changed*, never on *already committed*, so a genuine follow-up after a commit still
  gates, reviews and lands its increment. Four tests in `tests/test_merge_flow.py`, each verified
  against the unfixed code: with only the guard removed the second submit raises, and with the
  base-ref advance removed as well it reverts.
  **The no-fabrication scan demotes a negated banned word** to the co-lead-judged finding tier
  (`_negated`, applied in both `scan_python_source` and `scan_text_source`). A banned word inside
  an explicit negation is the §9.5 discipline being *stated* — "the tool can never fabricate a
  benchmark", "without any fabricated fixture" — and hard-failing it rejects a tool for
  documenting the rule the scan enforces. The measurement that forced it: across every production
  ledger on disk, **4 of 27 gate runs failed and all 4 were vocabulary failures**, every sampled
  instance a negation; nothing else has ever hard-failed Layer A in production. Cost is one of five
  submissions per firing, roughly once per authoring run. **Scope is the clause, not a token
  window**, and that distinction was measured rather than argued: `gate_bench`'s `fab_shell_script`
  plants `# where the reduction has not run yet, emit fake data for now`, where the negation
  governs a different clause, and the proximity rule first proposed demotes it — voiding one of the
  six mutants the published catch-rate table attributes to the scan. `tmp_scripts/
  gate_bench_negation_rescan.py` re-scans the whole 134-entry corpus offline (LLM-free, $0) and
  reports **0 moved** under the shipped predicate and **1 moved (`fab_shell_script`) under
  `--naive`**, which is what proves the check can see a move at all. A missed negation degrades to
  today's hard fail; a spurious cue degrades to a finding the co-lead reads.
