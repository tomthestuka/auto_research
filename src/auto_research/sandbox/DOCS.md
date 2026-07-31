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
  human_handler=None, coder_model=None, run_gate=None, run_review=None, approve_edit=None, …)`
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
  co-lead — *debatable*), `commit(message)` → sha (the only commit path; sets `committed`),
  `aclose()` (disconnect + §9.9 non-committed direct-branch tree reset). `propose_and_pr` drives
  these in the auto-loop; the analyst lead's authoring ReAct tools drive the *same* primitives
  directly (so the lead diagnoses each verdict rather than the run terminating).
- `build_authoring_session(tool_spec, *, lead, toolkit_root, worktree_base, colead=None,
  human_handler=None, coder_model=None, run_gate=None, run_review=None, max_turns=None,
  direct_branch=None, worktree_factory=None, sdk_client_factory=None, approve_edit=None,
  monitor=None)` → `AuthoringSession` — resolves config/hooks, picks worktree vs direct-branch
  mode (§9.9), builds the seeded SDK client, returns a **not-yet-connected** session (`await
  connect()` before use). Setup only — the sole git touches are the direct-branch clean-tree
  precondition and the per-attempt base-ref record.
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
- 2026-05-26 -> 2026-06-12 — **Phase 1 sandbox/tooling** built and hardened: manifest-based
  `ToolkitRegistry` (disabled-tool filtering, provider-native tool defs), persistent JSON-line sandbox
  (fd capture, XSPEC non-interactive boot), per-run workspace/memory/figures layout, built-in MCP
  server/client, config-driven runtime paths.
- 2026-06-12 -> 2026-06-16 — **Phase 2 steps 0–7 built** — the author→gate→review→commit flow, writes
  pinned under the runtime data dir. `merge_flow.propose_and_pr()` (isolated-worktree / `direct_branch`
  Sonnet author, runtime-safe context, `ask_lead`/`ask_human`, bounded L0–L4 rework, typed no-commit
  failure); `gate.run_gate` (Layer-A structural/provenance/determinism + no-fabrication scan, Layer-B
  test runner, extension-agnostic over `*_utils`); `review.make_review_hook` (provider-decorrelated GPT
  co-lead); `approve_edit` (one atomic commit, explicit-path staging, never `git add -A`); launch
  preflights (`check_author_reviewer_decorrelation`, `check_toolkit_git_identity`/`_branch_clean`).
- 2026-06-18 -> 2026-06-26 — **Live-run hardening (fix-lists #1–#11).** Gate survives slow/erroring
  tests (`TimeoutExpired` caught; `propose_and_pr` exception backstop → `status="failed"`); SDK author's
  `Bash` runs in the analyst code-exec env; `bash_exec` rescoped read-only; timeouts decoupled from the
  model (`call_timeout_ceiling`, `gate_test_timeout_s`); `propose_and_pr(monitor=…)` streams events; FS
  jail (`fs_guard.PathGuard` + `agent_fs_allow` `PreToolUse` hook, `_make_path_confiner` deprecated); git
  discipline (auto-reset to per-attempt `base_ref` via `_reset_direct_tree` + `tool_author_failed_*.patch`,
  coder Bash deny-rule, gate-entry auto-revert); gate fixes (`_manifest_keys` handles annotated manifests;
  stub/not-implemented → co-lead finding; determinism = per-test JUnit outcome maps not a byte-compare,
  `_strip_volatile` removed; reproducibility a science-tool `test_repro…` check).
- 2026-06-29 -> 2026-06-30 — **Authoring refactor (A1) + `bash_exec` hang fix.** Coder escalation tools
  take one Markdown `message`; `propose_and_pr`'s inline loop decomposed into an `AuthoringSession`
  (`drive`/`run_gate_step`/`run_review_step`/`commit`/`aclose`, via `build_authoring_session`) it now
  thinly drives; the session gained `submit_cap`/`debate_cap` + the co-lead debate path (`lead_rebuttal`),
  and consensus drives authoring via `lead.drive_authoring` (commit still ⇔ gate ∧ review). `bash_exec`
  now runs `stdin=DEVNULL, start_new_session=True` + process-group kill on timeout (its description demands
  non-interactive invocation), and `MCPToolClient.call_tool` bounds the await (`asyncio.wait_for`, 900 s) —
  fixing a whole-run hang from an interactive FTOOL wedging the MCP command channel.
- 2026-07-06 — **Gate Layer-B fixes.** [G2] Layer-B passes only if pytest exits 0 AND ≥1 test PASSED
  (`_layer_b_verdict`); an all-SKIPPED suite fails with a specific reason instead of merging an untested
  tool. [C1] bare ambiguous words (`synthetic`/`synthesized`/`illustrative`) moved from hard-fail to soft
  `finding_vocabulary` via `code_gate.yaml` (the co-lead still judges them).
- 2026-07-09 — **Gate derives the manifest/timeout target from the diff, not the brief ([A12]).**
  `structural.tool_manifest_entry` and `_gate_test_timeout` no longer build paths from the unvalidated
  `tool_spec.target_domain`; new `_touched_packages` derives the `*_utils` packages the changeset touched,
  the manifest search runs over those, and the timeout is the `max` over their `tool_config.yaml`. Removes
  the run-#12 doomed-loop; the rejection now lists the manifest keys actually present. `_gate_test_timeout`
  signature `(worktree, targets)`.
- 2026-07-13 — **[I1] co-lead `web_search`: dropped the best-effort resolver.** `_resolve_web_source`
  removed; the co-lead uses `web_source or WebSource()` directly (`TAVILY_API_KEY` is a required preflight
  var and `WebSource`'s client is now lazy, so construction can't fail on a missing key).
- 2026-07-14 — **Install-script increment 1: domain-agnostic REPL-init + env-probe.**
  `PersistentSandbox` gained a `repl_init_py` param — a config-pointed Python file exec'd once at REPL
  boot (via `AR_REPL_INIT_PY`, best-effort) for domain session setup; it **replaces the hardcoded
  XSPEC/relxill `_configure_xspec_once()`** in `_REPL_SCRIPT` (the XSPEC body now ships as
  `repl_init.py.example`), closing the deepest domain leak S1/P3. `builtin_server` builds the `bash_exec`
  description at startup via `_bash_exec_description()`, injecting the concrete probe from
  `AR_ANALYZE_PROBE` (config `analyze_probe`, wired by `graph.runtime._builtin_server_config`) and
  neutralizing the FITS/xspec hardcodes. New `runner.check_analyze_probe()` preflight check runs the
  configured probe under `analyze_env` (skips when unset). `graph.runtime` threads `repl_init_py` +
  `analyze_probe` from config. Tests: `tests/test_sandbox_hooks.py` (8).
- 2026-07-16 — **Scan config robustness + RNG demoted to a finding (human policy change).**
  (1) Every `code_gate.yaml` list read is now `or []`-guarded: an emptied YAML section parses
  to None and means "nothing to flag in that category" — it crashed the scan (`list(None)`
  TypeError) before. (2) `finding_code_patterns` is dual-purpose: entries naming a built-in
  `literal_array_*` detector switch it on; any other entry is a dotted code pattern matched
  like the hard tier (AST attribute/name nodes, once per line, hard match wins) but recorded
  as a `no_fabrication.code_pattern_finding`. Which list a pattern sits in decides severity —
  previously the finding tier silently ignored non-detector entries, so the human's RNG
  demotion (Monte-Carlo tools need randomness inside them; co-lead judges intent) did nothing.
  Hard-tier check name generalized `no_fabrication.rng` → `no_fabrication.code_pattern`.
  Tests: `tests/test_gate.py` (emptied-sections, finding-tier RNG, hard-tier machinery kept).
- 2026-07-22 — **Durable-resume Stage 2: authoring-branch self-heal (§12).** `check_toolkit_branch_clean`
  now raises the distinct `ToolkitBranchUnclean` (a `RuntimeError` subclass) on a dirty tree, so the
  mid-run authoring path can catch it specifically and degrade to an honest §9.6 terminal (in
  `consensus/strategy._maybe_author_and_resume`) instead of an uncaught crash. `build_authoring_session`
  persists the per-attempt base-ref to `<worktree_base>/current_base_ref` (`_base_ref_path`). New
  `heal_toolkit_branch_if_dirty(run_dir, root=, branch=) -> BranchHealResult`: on resume, resets a dirty
  authoring branch to the persisted base-ref (fallback HEAD) — dropping the killed attempt's mess + any
  slipped commit while keeping earlier committed tools — saving the discard as `resume_heal_*.patch`;
  no-op on clean/non-git/wrong-branch; best-effort (returns `reason="error: …"`, never raises). The reset
  core is factored into a shared `_save_diff_and_reset` (used by `_reset_direct_tree` too). `run_research`
  calls the heal before the launch preflight on `resume=True`. Deterministic tests `tests/test_resume_heal.py`
  (real git) + `tests/test_consensus_authoring.py::test_mid_run_unclean_branch_degrades_to_honest_terminal`.

- **2026-07-27** — **Removed the docstring override in `ToolkitRegistry.load()`; the manifest is now
  the agent-facing interface verbatim.** Since 2026-06-01 (`56e89f9`, Phase 1, before tool authoring
  existed) `load()` replaced each `TOOL_MANIFEST` description with the backing function's `__doc__`
  whenever the module imported successfully. That made sense when manifests were thin stubs and the
  `lib/` docstrings held the documentation, but Phase 2 inverted it: `prompts/coder.md` instructs the
  coder that the manifest entry *is* the agent-facing interface, and the GPT co-lead reviews that text
  in the diff — yet the runtime discarded it for every import-clean tool, with the winner decided by
  the tool's dependency footprint (an invisible, unchosen criterion). The override was undocumented
  and untested, and no manifest entry relied on its fallback. Now the description is taken from the
  manifest as authored; `lib/` docstrings stay the human-half documentation (§9.2). Measured effect
  with the reshaped `research_toolkit` manifests: model-visible tool-block context drops a further
  ~1,400 tokens (4 tools were showing 1,678 tokens of docstring in place of 265 tokens of brief).
  New test `tests/test_registry.py::test_manifest_description_wins_over_function_docstring`.

- **2026-07-27** — **Tool search / lazy tool loading (provider-native), shipping disabled.**
  `SandboxToolProvider.tool_definitions()` now marks every *registry* tool `defer_loading: true`
  when `config/orchestration.yaml: tool_search_enabled` is set; harness tools (`ask_human`,
  `web_search`/`web_fetch`, `read_file`/`list_dir`/`bash_exec`, the report tools) stay resident,
  which also satisfies both providers' "≥1 non-deferred tool" requirement. No per-tool
  exceptions — `data_catalog` defers like the rest. The adapters declare the provider's
  server-side search tool (Anthropic `tool_search_tool_bm25_20251119`, OpenAI `tool_search`,
  gpt-5.4+; Google has no equivalent and keeps today's fully-loaded behaviour), and a discovered
  tool's definition is appended inline in the conversation **tail**, so the cached prefix is
  untouched and the tool stays callable for the rest of the conversation without re-searching.
  Supporting pieces: `models/types.py` gains `AssistantToolUse.provider_blocks` /
  `ModelResponse.provider_blocks` — an opaque per-provider block carried through `_react` replay,
  since the API re-expands referenced definitions from history and dropping them would silently
  de-activate a discovered tool (**the same seam [I8] needs for Gemini's `thought_signature`**);
  the Anthropic `cache_control` breakpoint now lands on the last **non-deferred** tool (a deferred
  tool carrying it is a 400); and `models/providers/_tool_search.py` adds a **tombstone** guard —
  a tool referenced in history but missing from the catalog gets a stub definition instead of a 400
  (**deleted 2026-07-30**: a code trace showed a live conversation's catalog cannot shrink under it,
  so the guarded state is unreachable — see `models/DOCS.md`). Measured on `gpt-5.5` with the real 25-tool
  manifest: **19,004 → 2,382 resident tokens (−87%)**. Shipped `tool_search_enabled: false`, then
  **live-verified on both providers 2026-07-28 and flipped true** (resident input 19,074 → 3,789 on
  gpt-5.5, 35,208 → 9,003 on claude-sonnet-5); deterministic coverage in `tests/test_tool_search.py`
  (+12), live round-trip in `tests/test_live_tool_search.py` (env-gated).
  Prompts updated: `analyst_worker.md` teaches the search step,
  `coder.md` states the one-line-brief / prose-in-`args_schema` split for new tools.
- 2026-07-29 — **The co-lead's actions now reach the run ledger.** `build_authoring_session` called `review.make_review_hook(colead=…, author_provider=…)` without forwarding its `monitor`, so a co-lead built from config was constructed with `monitor=None`. Both ends of the wiring already existed (`make_review_hook` accepts `monitor`, `AnalystColeadAgent` accepts `monitor`) — only the kwarg at the seam was missing, and the symptom was subtle: the `AuthoringSession` records its own `review.result` verdict, so reviews *looked* logged while every action behind the verdict (tool calls, `web_search`, the reasoning steps) was silently discarded. That left the provider-decorrelated half of `commit ⇔ gate ∧ review` as the only actor in the flow with no audit trail — the opposite of what a trust-boundary check should be. One-line fix plus a regression test asserting the hook is built with the run monitor.
- 2026-07-29 — **The coder's Agent-SDK spend now reaches the run's cost record.** The coder is a separate process making its own API calls, so it never passes through `ModelHandle.call`/`traced_model_call` — its dollar cost appeared only as an unaggregated `cost_usd` field on each `coder.turn_end` event and reached no total anywhere (historically the largest unaccounted line; ~$9 in one observed session). `_drive_author` now feeds each `ResultMessage` to `_record_coder_cost` → `CostMeter.record_session_total`, which folds the SDK's **session-cumulative** figure in as a per-turn delta (verified monotonic against real logs) and records it as a `cost.call` event with `price_source="claude_sdk"`; the turn event gains `cost_usd_turn`. `AuthoringSession` carries `coder_model` for the attribution, and the `commit` event now carries the `tool` name so the end-of-run summary can name the tools that landed. Cost recording is guarded — it never breaks an authoring turn.

- 2026-07-30 — **`PersistentSandbox.abort()`** — the on-demand form of the timeout path, and nothing
  more: it kills the REPL child so the in-flight `readline` ends in EOF, `call_tool` returns its
  existing non-zero "sandbox subprocess exited unexpectedly" result (the caller gets a real result for
  the call it made, which is what keeps a tool_use/tool_result transcript valid), and the next
  `call_tool` spawns a fresh REPL at entry so nothing inherits a dead one. Returns whether the sandbox
  was actually busy. `run_research` registers each worker's `abort` on `RunControl` under the worker's
  `agent_key`, which is how a human `/stop` ends a 25-minute download instead of waiting it out — the
  exact behaviour observed by hand on 2026-07-29, now a first-class path.

- 2026-07-31 — **The sandbox kill takes the descendants with it.** `_kill_current` killed only the
  REPL, so whatever it had shelled out — an XSPEC fit, a multi-GB HEASARC download — kept running
  and kept writing into the obs-db after the caller believed the sandbox was gone (the §13 [F5]
  note). It now collects descendants from `/proc/<pid>/task/*/children` (`_descendant_pids`,
  breadth-first) and SIGKILLs them deepest-last before killing the REPL. Deliberately **not**
  `killpg`: the REPL is spawned without `start_new_session` and shares the *backend's* process
  group, so a group kill would take the run down with it — and that shared group is what lets an
  external `kill -9 -<pgid>` reach the sandboxes when our own cleanup never runs (the §12 hard-kill
  fallback), so it stays. New `kill_now()` is the human-interrupt entry point: no shutdown
  handshake, no 5 s grace, because the next cleanup step (the toolkit-branch reset) is only correct
  once nothing can write. `AuthoringSession.aclose()` is now idempotent (`_closed`), since the
  interrupt path closes the session from shielded cleanup and `drive_authoring`'s `finally` may
  still run afterwards. Tests: `tests/test_interrupt.py`.
