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
`_source_ini_env()` before each persistent subprocess is spawned. The script is sourced in a
**clean environment** (identity, locale and proxy variables plus a system `PATH`, from `/`, under
`bash --norc --noprofile` with stdin from `/dev/null` — bash sources `~/.bashrc` when stdin is a
socket), so the REPL, the shell and the gate's test run never carry the parent's credentials or its
`PWD`: before 2026-10-06 every API key `load_dotenv` had loaded reached them. The author harnesses
are not covered: both SDKs merge their `env` over the parent's `os.environ`.  It sets only
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
def report_record_path(agent_dir_path: Path, filename: str) -> Path
def report_dir(run_id: str) -> Path
def append_memory(path: Path, text: str) -> None
def read_memory(path: Path) -> str
```

- `create_run_workspace()` creates `<runtime_dir>/runs/{run_id}/{key}/figures/`
  for each agent key and returns `{key: agent_dir}`.
- `memory_path()` returns `{agent_dir}/memory.md` (parent created if absent).
- `report_record_path(agent_dir, filename)` — where a role's report JSONL lives: for an agent
  folder `runs/<run>/<key>/`, `ledgers/<run>/reports/<key>/<filename>` (created), outside every
  allow-list (human 2026-09-25: a report read back with `read_file` minted the role's own typed
  numbers as a receipt). An agent folder outside a `runs/` tree keeps the file in that folder.
- `report_dir(run_id)` — the reviewer's report folder, `ledgers/<run>/reports/reviewer/report/`
  (with `figures/`); each report to the human gets `v<k>/` under it (`graph/builder`).
- `round_report_path()` returns `report_record_path(agent_dir, "<role>_round<n>_report.jsonl")` —
  the per-round inter-LLM report JSONL an analyst writes via `edit_report` (V-4), distinct from
  the private `memory.md`. `n` is the monotonic debate round. `model` is accepted and unused: a filename is
  readable by any agent whose allow-list covers the run directory, so the member stays in the
  ledger (2026-09-03).
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
    def is_jailed(self, kind: str) -> bool # the server config's "jailed" flag, set by whoever built it
```

- `servers` is a list of `{"name": str, "command": str, "args": list[str]}` dicts.
  Defaults to a single `builtin` server running `builtin_server.py` via
  `sys.executable` (a bare default for tests); the run path always passes the config
  `graph.runtime._builtin_server_config` builds (analysis interpreter, jailed when on).
- Tool kinds follow the convention `mcp.<server_name>.<tool_name>`.
- `call_tool()` returns a dict with at minimum `stdout`, `stderr`, `exit_code`.
  Non-zero `exit_code` or `result.isError=True` is surfaced in the same `ToolResult`
  schema used by manifest tools, so the retry loop applies equally.
- The `session.call_tool` await is bounded by `timeout_s` (`asyncio.wait_for`, default
  `900 s` — far above any builtin tool's own timeout). It is a **backstop**, not a normal
  path: a wedged server returns `exit_code` 124 instead of hanging the whole run.

### Built-in MCP server (`builtin_server.py`)

Currently exposes shell execution plus file-tree exploration. Built by
`graph.runtime._builtin_server_config`: it runs on the **analysis env's interpreter** (which carries
`mcp`), with the environment the analyst init script produces (HEADAS/CALDB visible to `bash_exec`),
`{analyze_env}/bin` first on `PATH`, and the toolkit on `PYTHONPATH`. **With `agent_os_jail` on it
runs inside the OS jail** under the same allow-list as the REPL (writable: the allow-list's writable
roots, its cwd, its own jail home `jail-home/shell`; network on), its source passed with `-c` so the
jail never mounts this repository (F20, 2026-10-06). The module is therefore kept stdlib + `mcp`
only. Restricting MCP to read-only filesystem tools is a documented P2 task.

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
def mcp_jailed(self, kind: str) -> bool            # True if kind's server was launched inside the OS jail
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
    lead,                       # AnalystLeadAgent — the coder's message(to="lead") lands in its inbox
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
- **L1** escalation: SDK `WebSearch`/`WebFetch`, plus one in-process MCP tool, `message(to, text)`
  (E12; `escalation_server.route_message`): `to="lead"` posts to the lead's inbox and marks the
  question open (`_make_ask_lead_handler`); `to="human"` goes through the run's `SeatRouter` and
  returns at once, the reply steered into the coder's turn by `session.send` (none + headless →
  typed `HumanInputRequired` carrying `UNATTENDED_GUIDANCE`), so the lead is never the sole oracle.
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
              (analysis_policy: toolkit demoted to --ro-bind, host /tmp replaced by a private dir)
  --symlink   configured → resolved        so /data/… spellings work inside
  --proc --dev --unshare-{user,pid,ipc,uts} --die-with-parent
  --unshare-net                            gate only
```

| process | insertion | network |
|---|---|---|
| Claude coder | `ClaudeAgentOptions(cli_path=…)` → a generated launcher | yes |
| Codex coder | `CodexConfig(codex_bin=…)` → a generated launcher | yes |
| analyst REPL | argv prefix in `persistent.py`, `analysis_policy` | yes (acquisition downloads) |
| builtin shell server (`bash_exec`) | argv prefix in `graph.runtime._builtin_server_config`, `analysis_policy`; source via `-c` | yes |
| gate's Layer-B pytest | argv prefix in `gate.py`; private `/tmp`, the worktree bound explicitly | **no** |

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
    async def steer(self, message: str) -> bool# say something mid-turn; False if none in flight
    async def disconnect(self) -> None         # take it down (finally + interrupt cleanup)
```

`merge_flow._build_backend` picks the implementation from `coder_member().harness`. Both receive
the same brief, working tree, escalation handlers and filesystem allow-list; the harnesses differ
in what mechanism they offer for each.

| wire | `claude_sdk.ClaudeSdkBackend` | `codex_sdk.CodexBackend` |
|---|---|---|
| process | the `claude` CLI via `ClaudeSDKClient` | `codex app-server` via `openai_codex.CodexClient` |
| auth | the env rebuild drops `ANTHROPIC_API_KEY`, so it uses stored subscription credentials; `_with_live_anthropic_key` stamps a rotated key | the vendor key named by the member's `key_env`, **defaulting to the provider's standard variable** when the member names none, in the child's environment |
| role prompt | `prompts/coder.md` as `system_prompt` (replaces) | the same text as `developerInstructions` (layers over Codex's own tool instructions) |
| tool surface | an explicit `allowed_tools` list | Codex's fixed built-ins plus `tools.web_search` |
| write jail | a `PreToolUse` hook consulting `PathGuard` | `sandbox="workspace-write"` + `writableRoots` = the same allow-list, enforced by the OS (Landlock) |
| read jail | the same hook, for tools whose path it sees | **none**: Codex grants full-disk read access by design, so a crossing is recorded as `coder.read_outside_allowlist` |
| git ban | a `Bash` deny-rule (best-effort) | `.git` is read-only inside a writable root under that sandbox |
| escalation | an in-process SDK MCP server | an in-process Streamable-HTTP MCP endpoint (`escalation_server.py`), registered as a **process-level** `--config` override and gated by an *elicitation* the client answers with `{"action": "accept"}` |
| cost | a session-cumulative dollar figure, differenced per turn | real token counts per model call (`thread/tokenUsage/updated`), priced by `CostMeter.record_call` like any chat role |
| turn status | inferred | `turn/completed` carries a status and an error |
| stop reach | the SDK's own async client, so a cancel unwinds it | a **daemon thread this backend owns** polls notifications, and a cancelled turn is left *registered* so the client's close-time wake can reach it (below) |

**How a `/stop` reaches a Codex turn.** The notification stream is a blocking call, and a cancel
cannot reach a thread blocked in `queue.Queue.get()` by any mechanism — `Future.cancel()` returns
False for running work, `shutdown(cancel_futures=True)` is documented not to touch it, and
`PyThreadState_SetAsyncExc` reports success while the thread stays parked forever (async exceptions
are checked at bytecode boundaries and a blocked lock executes none). The escape is the vendor's
own: `_MessageRouter.fail_all` pushes an exception into every **registered** turn queue when the
client closes, which `CodexClient.close()` triggers. Two things make that reachable here.

- `drive` leaves the turn **registered** when it is cancelled. Deregistering is right on every
  other path and was wrong on this one: `fail_all` finds waiters by walking its `turn_id -> queue`
  dict, and `unregister_turn` pops us out of exactly that lookup — so the wake `aclose`'s later
  `disconnect()` fires had nobody left to deliver to.
- `_drain_turn` polls on a **daemon thread the backend creates**, not the default executor. The
  executor's workers are non-daemon and `concurrent.futures.thread._python_exit` joins every one of
  them with no timeout and no daemon check (bpo-39812), after `asyncio.run` has already spent
  `THREAD_JOIN_TIMEOUT` (300 s) on `shutdown_default_executor` — so one stranded poll used to turn
  a clean stop into an unbounded hang. The poller stops itself on a terminal notification, so
  nothing outlives a turn that ends normally.

Measured 2026-09-12: the B8 live drill cancelled the run, repaired the tree to the attempt's base
ref and leaked no coder process, and *then* the process never exited. Pinned by
`tests/test_coder_backends.py::test_a_cancelled_codex_turn_stays_registered_so_the_close_time_wake_lands`
and `::test_the_codex_poller_is_daemonised_and_ends_with_a_turn_that_completes`, each verified to
fail against its own mutation — the second one wedging the pytest process itself, which is the
production symptom in miniature.

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
a git attempt, a `message` to the lead, and a lengthening trajectory — in the order a real attempt
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
Provenance (`provenance.figure_shape`, Layer A, `scan_figure_shape`): every figure dict a tool returns
that the source shows — a `"figures"` value within one function, inline, through a list/comprehension,
a name built with `append`/`extend`/`+=`, or a name bound to a dict — must be one `FigureRef` accepts:
`figures` a list, each entry a dict, exactly the schema's required keys (`path caption mime_type
produced_by created_at sha256`, optional `thumbnail_b64`), literal values the schema validates. The key
set and validators are read from `FigureRef` itself. Test files are exempt (fixtures, not returns).
Limit: a figure assembled in a helper, via `update` or `**`, is unchecked; the runtime still drops it
with a note.
Deterministic coverage in `tests/test_gate.py` (pure scanners + a throwaway git worktree
with an injected Layer-B runner — no live science stack).

### `returns.py` — the gate's dynamic return check (G15)

During the gate's Layer-B pytest run a recorder plugin (`-p ar_returns`, written into the JUnit temp
dir, loaded in both runs) wraps every **new or changed** `TOOL_MANIFEST` tool (new = a key absent from
HEAD's manifest; changed = its function body holds an added line; legacy tools are not judged). Per
call it records the REPL payload, stats before/after for every argument-named path, files written
during the call (a Python `open` audit hook) and copies of them. The gate process judges each record
with the runtime's own functions, as Layer-B checks:
- `layer_b.returns.transmit` — `persistent.repl_payload` + `json.dumps`;
- `.figure` — `runner.parse_tool_figures` + the file exists;
- `.receipted` — every written file is one `capture.data_products` receipts;
- `.table` — every receipted `.csv`/`.tsv` parses with `render.parse_delimited`;
- `.number` — every number in non-protocol keys appears in the result text or a receipted file
  (`bindings.ungrounded_numbers`);
- `.recorder` — the plugin never loaded, or a watched tool failed to import.

A watched tool no test calls is `finding.returns_unobserved` (for the co-lead), not a failure.
Housekeeping files are not judged as writes: `__pycache__`/`.pyc`, cache dirs, `*.lock` (the
obs-db catalog writer opens a zero-byte `catalog.json.lock` only to hold an flock) and
`/.catalog_history/` (the backup snapshot that writer copies on every update). The
recorder is assembled from runtime source via `inspect.getsource` (`repl_payload`,
`normalize_tool_raw`, `_named_paths`, `call_started_ns`), so gate and runtime cannot drift. Limits:
only what the tests call; C-level writes (cfitsio/XSPEC) are invisible; it shares a process with the
coder's tests. Public: `recorder_source`, `install`, `judge`, `judge_record`, `watched_tools`,
`manifest_entries`.

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
  `ToolResult` — one-shot subprocess; kept for ad-hoc use. Keeps the valid figures and appends drop
  notes to `stderr`.
- `normalize_tool_raw(raw)`, `PROTOCOL_KEYS`, `tool_text(raw) -> (text, exit_code)` (runner.py) — the
  result-text rule shared by runtime and gate (moved from agents/react.py).
- `repl_payload(result)` (persistent.py) — defined once and spliced into the REPL; a return
  `json.dumps` cannot serialise fails that one call with exit 1 and a stderr naming the fix.
- `_default_test_runner(..., returns_tools=)`, `_head_file` (gate.py) — `run_gate` binds
  `returns.watched_tools(...)` and extends its findings from the runner.
- `parse_tool_figures(raw) -> (figures, drop_notes)` — the one reading of the figure contract: validates
  each entry into a `FigureRef`, drops a bad one with a reason, ignores a non-list with a note. Used by
  `SandboxToolProvider` (react.py) and `run_tool`.
- `_source_ini_env(ini_sh)` → `dict` — sources a bash init script in a clean environment
  (`_INI_BASE_VARS` + a system `PATH`, cwd `/`; bash's `PWD`/`OLDPWD`/`SHLVL`/`_` dropped from the
  result), caches result. Raises `RuntimeError` on any failure (non-zero exit, empty env, or subprocess exception) — no fallback to `os.environ`.
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
  co-lead — *debatable*), `commit(message)` → sha (the only commit path; records `last_commit_sha` and
  advances the per-attempt base ref, in memory and on disk), `pending_changes()` → bool
  (whether the tree holds unlanded work — False right after a commit, True again on the next
  edit; `execute_submit` reads it so a no-op submit is answered, never executed),
  `aclose()` (disconnect + §9.9 direct-branch tree reset of any **unbanked** work, gated on
  `pending_changes()` rather than on `committed` — see the 2026-09-09 F1 entry). `propose_and_pr` drives
  these in the auto-loop; the analyst lead's authoring ReAct tools drive the *same* primitives
  directly (so the lead diagnoses each verdict rather than the run terminating).
  **The lead<->coder channel** (the lead's `message` to `coder` / `wait_for_reply` / `submit_for_review`):
  `start_drive(message)` runs one instruction in a background task and returns at once (a second
  one while it runs raises); `send(message, *, mid_turn=False)` → `"started"` | `"queued"` |
  `"steered"` | `"answered"` — an idle coder gets a new turn; a working one has the message
  **queued** and run as its next turn in the same task (`coder.queued` / `coder.queued.delivered`),
  unless `mid_turn` is set or the coder has an open question, in which case it is steered into the
  running turn (`CoderBackend.steer`); `mark_question_open()` (the coder's `message(to="lead")`,
  whose text is in the lead's inbox, E12); `wait_for_event(timeout_s)` → `"turn_ended"` |
  `"failed"` | `"steer"` (a message reached the lead's inbox, the coder's question among them) |
  `"idle"` | `"timeout"` — parks on the session's wake event (set by a finished turn, a failure or
  an opened question) and on the lead's `RunControl.steer_event` (`watch_steers(event)`),
  returns at once when something is already waiting, consumes nothing, and records `coder.wait`;
  `drive_status` (`idle` | `working` | `stopped` | `cancelled`), `take_drive_result()` →
  `(text, error)` consumed once (narrations of consecutive queued turns are joined),
  `has_unread_result()`, `current_instruction_elapsed_s()`.
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
  and re-exported here for back-compat; the author's `message(to="human")` builds a typed
  `HumanQuery` and routes through `hitl.router.SeatRouter` (the channel every in-graph seat uses).
- `gate.run_gate(worktree, tool_spec, *, test_runner=None, config=None)` → `GateReport` —
  the deterministic gate (§9.4). Runs the no-fabrication scan + structural + determinism +
  provenance (Layer A) and the tool's tests (Layer B); records Tier-3 findings without
  failing. `scan_python_source(rel, source, *, added=None, config=None)` → `(hard_fails,
  findings)` is the pure, unit-testable `.py` scan core; `scan_text_source` is its non-`.py`
  text counterpart (vocabulary only); binary/undecodable targets hard-fail without scanning.
  `scan_figure_shape`, `check_structural`, `collect_targets` (now extension-agnostic under a
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
  - `jail.analysis_policy(*, label, toolkit_root, writable, readonly, tmp)` → `JailPolicy` — the
    policy for the worker REPL and the shell server: the allow-list with every root under
    `toolkit_root` read-only (a writable toolkit let a shell command edit a tool and then call it
    for evidence); **a configured writable root that contains one of `writable` demoted to
    read-only**, with `writable` bound on top (a REPL writes its own worker folder, not every
    worker's, every past run's, or the run's `authoring/` control files a resume trusts);
    `readonly` overlays bound on top (the shell's run folder keeps `authoring/` read-only); the host
    `/tmp` dropped and `tmp` bound at `/tmp` instead (the host one held another session's repo copy
    and an author home's credential file); the toolkit itself always overlaid read-only (an
    allow-listed ancestor would otherwise leave it writable); a per-label home; network on. The REPL
    and the shell server run `python -P` (`python -c` otherwise puts its cwd first on `sys.path`, so
    a planted package would shadow the toolkit's and its results be stamped as evidence).
  - `jail.private_tmp(label, workspace)` → `Path` — a private `/tmp` per process kind and
    workspace, on **local** disk under the host temp root, mode 0700 (NFS breaks
    `TemporaryDirectory` cleanup while a file is open). An existing directory is used only if it is
    a plain directory, owned by this uid, mode 0700 (`_owned_private_dir`); otherwise it raises.
    Removed when its process ends: the REPL's by `PersistentSandbox.shutdown`, the shell server's by
    `MCPToolClient.shutdown` (server config key `private_tmp`), the gate's lives inside the JUnit
    `TemporaryDirectory`.
  - `JailPolicy.tmp` — a directory bound at `/tmp` before the roots, so a root nested under `/tmp`
    still lands on top.
  - `jail.policy_from_config(*, network, label)` → `JailPolicy` — `agent_fs_allow` split by
    `agent_fs_readonly` into writable and read-only roots.
  - `jail.coder_policy(*, label, extra_rw)` → `JailPolicy` — the author-harness policy. An author
    writes to **two places only**: its working tree (`extra_rw`) and its own settings home — and
    **not to `.git` inside either**, which is bound read-only on top of the writable root
    (`jail_argv` applies binds outermost-first, so the nested rule wins). That is what makes the
    coder's git ban (§9.9) a kernel rule on both harnesses rather than a prefix-matched Bash
    deny-rule: `add`/`commit`/`checkout` fail with *"Read-only file system"* however they are
    spelled, while `status`/`diff`/`log`/`show` still work, so the coder can report its own change
    set. On a *linked* worktree `.git` is a pointer file, bound read-only here, and the gitdir it
    names lives under a repository the author's writable set does not contain. The runtime's own
    git is unaffected — `approve_edit`, the gate-entry reset and `collect_all_changes` all run in
    the parent process, outside the jail. Everything else on the allow-list is read-only,
    and the temp root is **absent** entirely. That
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
  serving the coder's one `message(to, text)` tool (`MESSAGE_DESCRIPTION`, `route_message`) from
  **this** process (the handlers must reach the live lead's inbox and the run's HITL connector). Loopback, ephemeral port, per-session bearer token;
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
Compacted 2026-09-01 and 2026-09-24 (detail in git history).

- 2026-05-26 -> 2026-06-16 — Phase 1: manifest `ToolkitRegistry`, persistent JSON-line sandbox,
  per-run workspace, built-in MCP server/client. Phase 2: `merge_flow.propose_and_pr()`,
  `gate.run_gate` (Layer A + no-fabrication scan, Layer B), `review.make_review_hook` (decorrelated
  co-lead), `approve_edit` (one atomic commit, explicit-path staging); launch preflights
  `check_author_reviewer_decorrelation`, `check_toolkit_git_identity`/`_branch_clean`.
- 2026-06-18 -> 2026-06-30 — Live hardening (fix-lists #1–#11): timeouts decoupled from the model
  (`call_timeout_ceiling`, `gate_test_timeout_s`); FS jail (`fs_guard.PathGuard` hook;
  `_make_path_confiner` deprecated); base-ref reset (`_reset_direct_tree`, `tool_author_failed_*.patch`),
  coder Bash deny-rule, gate-entry auto-revert; determinism as JUnit outcome maps. Authoring split into
  `AuthoringSession` (`build_authoring_session`, driven by `lead.drive_authoring`). `bash_exec`
  process-group kill and a 900 s `MCPToolClient.call_tool` bound fix an FTOOL hang.
- 2026-07-06 -> 2026-07-16 — [G2] Layer B needs ≥1 PASSED; [C1] ambiguous words → findings; [A12]
  gate targets from `_touched_packages`, not `target_domain`; [I1] co-lead uses `WebSource()`.
  `repl_init_py` replaces `_configure_xspec_once()`; `bash_exec` description from `analyze_probe`;
  `runner.check_analyze_probe()`. `code_gate.yaml` reads `or []`-guarded; `finding_code_patterns`
  recorded as findings (RNG demotion, ruling).
- 2026-07-22 -> 2026-07-29 — `ToolkitBranchUnclean`; base ref persisted; `heal_toolkit_branch_if_dirty`
  on resume. `ToolkitRegistry.load()` stops overriding manifest descriptions with `__doc__`. Registry
  tools `defer_loading` under `tool_search_enabled` (live-verified 07-28). `monitor` reaches
  `make_review_hook`; coder spend via `_record_coder_cost` → `CostMeter.record_session_total`.
- 2026-07-30 -> 2026-07-31 — `PersistentSandbox.abort()` (registered on `RunControl`) and `kill_now()`;
  `_kill_current` kills descendants via `/proc` (not `killpg`, which shares the backend's group) [F5];
  `AuthoringSession.aclose()` idempotent.
- 2026-08-10 -> 2026-08-19 — Coder env follows a key rotation (`_with_live_anthropic_key`). Second
  author harness: `coder/` package with `CoderBackend` (`connect`/`drive`/`disconnect`),
  `claude_sdk.py`, `codex_sdk.py`; `_build_backend` dispatches on harness; pairing table
  `coder/harness.py` (five pairs; alt-vendor `claude_sdk` rows removed); preflights
  `coder_harness_supported`/`_available`. Codex: `writableRoots` write jail, read-only
  `.git`, escalation over loopback MCP (`escalation_server.py`), reads only recorded
  (`coder.read_outside_allowlist`); MCP config via `--config` (`_flatten_overrides`), elicitations
  answered `{"action": "accept"}`, member `parameters` reach Codex, a failed turn returns
  `[harness error] …`; `_touched_paths` porcelain fix. Qwen in `UNVERIFIED_PAIRINGS` behind new
  `PreflightWarning`. `tests/test_live_coder_matrix.py` LIVE-VERIFIED on the credentialled legs.
- 2026-08-20 — One `coder: RoleModelConfig` through `build_authoring_session`/
  `propose_and_pr`/`drive_authoring`, `_build_backend(member=)` (`_resolve_coder_provider` deleted);
  `make_review_hook(allow_correlated=False)` emitting `review.correlated`;
  `codex_sdk.resolve_vendor_key` reads the key pool. OS jail fixed for its first author (binds the
  exec'd binary, resolved paths, fresh settings home on local disk); ruling: one enforcement layer —
  the harness sandbox stands down under the kernel jail, `coder_policy` allows two writable places.
  LIVE-VERIFIED end to end.
- 2026-08-31 — Submission snapshots: `run_gate_step` saves each submit under
  `run_dir/authoring/submissions/<tool>/`; `build_authoring_session` restores the latest only onto its
  recorded base ref and the gate re-runs (`save_`/`find_`/`restore_submission_snapshot`,
  `discard_submission_snapshots`, `authoring.snapshot.*`). LIVE-VERIFIED 09-01; commit path
  deterministic only.
- 2026-09-01 — `_make_ask_human_handler` classifies before the MCP boundary (the author had been
  answered with its own question): `coder.ask_human.unattended` + `HumanInputRequired` carrying
  `UNATTENDED_GUIDANCE`, vs `coder.ask_human.error` + `RuntimeError`; both stay tool errors.
  `CODER_TEXT_CAP` → `coder_text_cap()`. LIVE-VERIFIED (`tests/test_live_no_hitl.py`).
- 2026-09-02 — `commit()` advances `_base_ref` (memory + `current_base_ref`), so a later submit no
  longer reverts an approved commit; `AuthoringSession.pending_changes()`, and `execute_submit`
  answers a no-op submit budget-free. The scan demotes a clause-scoped negated banned word to a
  finding (`_negated`).
- 2026-09-03 — `_author_env` drops `CLAUDE*`/`COPILOT*`/`VSCODE*` (`_HOST_TOOLING_PREFIXES`);
  `workspace.round_report_path` omits the model (`_slug` deleted). Ruling: `runtime_dir` stays on
  every allow-list (reversed 09-14).
- 2026-09-09 — [F1] `aclose` resets on `pending_changes()`, not a commit latch; `committed` deleted
  (readers use `last_commit_sha`). [F2] `structural.changes_allowed` over new `collect_all_changes`
  (ruling: `*_utils` allow-list), rejection names the route (`ask_lead`). [N1] Layer-B output keeps
  head + tail (`_bounded_pytest_output`, `gate_layer_b_output_cap`). [F10] `PathGuard` writable set
  (`is_writable()`; ruling: a role writes only its own work folder; jail side residual).
  `jail.coder_policy` binds `.git` read-only in every writable root (kernel-enforced git ban).
- 2026-09-12 — [B1/B2] coder runs beside the lead: `start_drive`/`drive_status`/`take_drive_result`
  (`drive` stays blocking; `submit_for_review` blocking by ruling); a failed drive returns
  `[harness error]`; a second `start_drive` in flight raises; `aclose` cancels and awaits the drive
  before `disconnect`. Codex `/stop` hang fixed: registration kept on cancel, poll on a daemon thread
  (`_drain_turn`). `CodexBackend` falls back to `env_var_names(provider)[0]` without `key_env`.
  `ask_lead` parks the question (`[coder asks]`); `CoderBackend.steer`, `AuthoringSession.send`;
  `clarify_for_author`/`_ClarifyReply` deleted.
- 2026-09-14 — Run record moved to `workspace.ledger_dir(run_id)` (outside `run_dir`);
  `agent_fs_allow` narrows to `runtime_dir/runs` for `PathGuard` and the coder's mount plan;
  `workspace.resolve_record` still reads legacy in-workspace records. Reverses the 09-03 ruling.
- 2026-09-24 — `AuthoringSession.wait_for_event` wakes on turn end, drive failure, `park_question`
  or the lead's `steer_event` (`watch_steers`); `send` queues by default, `mid_turn=True` steers;
  `_init_channel()`. LIVE-VERIFIED (`tests/test_live_coder_wait.py`). Open: a killed Codex turn books
  $0.00; `mcpServer/startupStatus/updated` is not recorded.
- 2026-09-25 — [A13] The Claude backend appends `[harness error] the anthropic turn failed: …` when a
  turn's `ResultMessage.is_error` is set, so a billing or session-limit reply no longer reads as coder
  work (mirrors the Codex backend).
- 2026-09-25 — [G14] Gate provenance check brought to the runtime spec: `scan_provenance`
  (`FigureRef(...)` calls carry sha256/produced_by) replaced by `scan_figure_shape` (returned figure dicts
  match `FigureRef`); `parse_tool_figures` moved from agents/react.py into runner.py and shared with
  `run_tool`, fixing its drop-all. Tests: `tests/test_gate.py`; live red-team
  `tests/test_live_figure_shape_attack.py`.
- 2026-09-25 — [G14] LIVE red-team (`tests/test_live_figure_shape_attack.py`, $13.62, 17 cells): the
  static figure-shape check caught every literal plant but 7 of 7 indirect or runtime-only shapes
  (helper, `**`, update, bare variable, empty-at-runtime caption, absent file) reached a commit; the
  honest control committed; the recorder oracle caught all 7. Remedy awaits a ruling.
- 2026-09-25 — [G15] Dynamic return check `sandbox/returns.py` (`layer_b.returns.*`: transmit /
  figure / receipted / table / number / recorder), judged with the runtime's own functions. Runtime
  fixes: a non-JSON REPL return fails that call instead of killing the sandbox;
  `capture.call_started_ns` on the coarse realtime clock (fast writes were never receipted, 200/200).
  gate_bench gate-only re-scan: 0 verdicts move vs G14; the check ran in 112/134.
- 2026-09-25 — [G15] LIVE red-team (13 cells, $11.50): every planted attack stopped before commit by
  name (`.figure` ×3 incl. all three routes that beat the static scan, extra key, non-dict entry,
  `.table`, `.number`); one limit breach committed (a caption empty only on an input no test uses —
  the uncalled-path limit, left to the co-lead); the honest control committed after `*.lock`
  sidecars became housekeeping. Reruns of the 4 inconclusive cells ($4.24) all stopped: 12/12 attack
  cells stopped before commit, all six checks exercised, control commits.
- 2026-09-25 — [R1] `report_record_path` added; `round_report_path` and `report_dir` now resolve
  beside the ledger, out of every role's reach (human ruling).
- 2026-09-25 — [E12] The coder's `ask_lead`/`ask_human` became one MCP tool `message(to=lead|human,
  text)` on both harnesses (`escalation_server.route_message`, `MESSAGE_DESCRIPTION`; Claude
  allow-list `mcp__escalation__message`). `to=lead` posts to the lead's `RunControl` inbox and calls
  `AuthoringSession.mark_question_open()` (`park_question`/`take_questions`/the `question` wait
  reason deleted); `to=human` is non-blocking through the run's `SeatRouter`, the reply delivered by
  `session.send(..., mid_turn=True)`; unattended still raises `UNATTENDED_GUIDANCE` before the MCP
  boundary, a channel fault now arrives as a notice. Gate rejection names "message the lead".
- 2026-10-06 — **[F20] closed: the builtin shell server runs inside the OS jail**, plus the fixes a
  stateless adversarial review of that change found. `graph.runtime._builtin_server_config` passes
  the server source with `-c` (the jail never mounts this repo) and wraps it in the new
  `jail.analysis_policy`, which the worker REPL now uses too: toolkit read-only, a private `/tmp`
  (`JailPolicy.tmp`), `PYTHONNOUSERSITE=1`. `runner._source_ini_env` sources the init script in a
  clean environment (21 key/token variables had reached the network-enabled shell). The shell
  guard's stand-down now reads how the server was launched — `"jailed"` on the server config →
  `MCPToolClient.is_jailed(kind)` → `ToolkitRegistry.mcp_jailed(kind)` — instead of the config
  file, so a configured `mcp_servers` entry or a host without `bwrap` keeps the command scan. Tests
  (`TestShellServerJail`) drive the production builder, the real MCP client and `PersistentSandbox`
  against a synthetic allow-list; each guarantee was mutation-checked.
- 2026-10-06 — **[F20] second stateless review, fixes.** `_source_ini_env` now runs
  `bash --norc --noprofile` with stdin from `/dev/null` (with a socket on stdin bash had sourced
  `~/.bashrc`: 66 variables instead of 35), resolves the script path, and fails when the script file
  is missing (a missing `source` had failed silently). `analysis_policy` demotes a configured writable root containing the process's own folder
  to read-only and takes `readonly` overlays; the shell keeps the run's `authoring/` read-only; the
  REPL sets `PYTHONSAFEPATH=1`. The private `/tmp` moved to local disk (`jail.private_tmp`), and the
  gate's Layer-B run gets one too, with its worktree bound explicitly. **Correction to the previous
  entry:** its `/tmp` assertions were vacuous (the synthetic allow-list lacked `/tmp`, so a mutant
  without the replacement passed) and the clean env does not reach the author harnesses. The tests
  now put `/tmp` and a shared runs root in the synthetic allow-list; five mutants (ancestor
  demotion, `/tmp` replacement, `authoring/` overlay, `PYTHONSAFEPATH`, and `--norc`, the last
  shown by reproducing the leak in the test's own scenario) are each caught.
- 2026-10-06 — **[F20] third stateless review, fixes.** `private_tmp` refuses a pre-existing
  directory that is a symlink, foreign-owned or not 0700; private `/tmp`s are removed when their
  process ends (REPL shutdown, MCP client shutdown, the gate's inside its JUnit temp dir); REPL and
  shell server run `python -P` instead of setting `PYTHONSAFEPATH` (inherited by children);
  `analysis_policy` always overlays the toolkit read-only. Tests use a synthetic toolkit as the
  write-probe target and plant the host-`/tmp` marker in the real temp root. Findings outside this
  change are open in the handoff (host-side symlink following in jail-writable folders, the shared
  writable `PFILES` directory, the gate's full allow-list, science_bench's allow-list shape, F21).
- 2026-10-06 — **[F22] the host no longer follows what an agent plants in its writable folder.**
  `workspace.memory_path` is beside the ledger (`ledgers/<run>/memory/<agent>/memory.md`, no
  carry-over from the old location); `append_memory`/`read_memory` open with `O_NOFOLLOW`;
  `record_beside`/`resolve_record` use the legacy in-run-folder spelling only for a run with no
  ledger directory (pre-split); `human_reply_dir` added. `analysis_policy` refuses a writable root
  that is a symlink and takes a per-process `home` (REPL: `<workdir>/.home`; shell:
  `<run>/.home-shell`). **[F23]** `runner.with_private_param_dir` gives the REPL, shell, gate and
  both authors their own `PFILES` user dir (empty, so tools start from the system defaults);
  `/grad/fzhou/pfiles` left `agent_fs_allow`. **[F24]** the gate's Layer-B jail is read-only except
  its worktree, JUnit folder, its own home (inside the JUnit temp dir) and a private `/tmp`, with
  `PYTHONNOUSERSITE=1`.
- 2026-10-06 — **[F22] second review, fixes.** The REPL's and the shell's homes moved out of every
  agent-writable folder (`private_tmp("repl-home"|"shell-home", ...)`; a planted `.home` symlink had
  been bound writable — the toolkit, reproduced); `analysis_policy` refuses a symlinked `home` too.
  The shell writes only the workers' own folders (`_builtin_server_config(writable_dirs=)`), not the
  run folder, so it cannot swap a folder another process binds. `record_beside`/`resolve_record`
  decide "pre-split run" by where the ledger markers (`backend.log`, `monitor.jsonl`) are, and a
  read never creates an old run's ledger directory. `write_launcher` writes with `O_NOFOLLOW`.
  `human_reply_dir` keeps a standalone agent's replies in its own folder. Tests pin each rule.
- 2026-10-06 — **[F22] third review, fixes.** Analysis and gate homes are mounted at
  `jail.JAIL_HOME_AT` (`/home/ar`, `JailPolicy.home_at`) instead of their host path under the
  temp root, which inside the jail is the process's own writable `/tmp` (a planted symlink there
  could redirect the mount). The REPL's private `/tmp` is recreated empty at every start.
  `write_launcher` writes a fresh temp file (`O_EXCL`) and renames it into place, so a planted
  FIFO cannot block the host. science_bench's trial driver writes `backend.pid` and reads
  `result.json` through `record_path` (it still used the run folder).
- 2026-10-06 — **[F27] the authoring base ref a resume resets to lives beside the ledger**
  (`merge_flow._base_ref_path`: `ledgers/<run>/authoring_current_base_ref` for a run's authoring
  dir, which the Claude author's jail can write), and `heal_toolkit_branch_if_dirty` refuses a base
  ref that is not an ancestor of the branch head. **Gate:** run 1's pytest scratch
  (`pytest-of-*` in the private `/tmp`) is removed before run 2 (human ruling 2026-10-06; a toolkit
  suite left 65 GB in 10 minutes and two runs filled the disk).
- 2026-10-06 — Gate: both Layer-B runs pass one `--basetemp` (`/tmp/pytest-gate` jailed, the
  private tmp's `pytest-gate` otherwise), which pytest wipes when each run starts, so run 1's
  scratch is gone before run 2 with the jail on or off. A resume records `run.resume.heal` for
  every outcome but clean (a refused heal is a tampering signal).
