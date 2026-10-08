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

### `envs.py` — software environments as objects (env plan B1, handoff [F18])

An environment is a conda prefix + its activation (init script, REPL boot file) + the externals the
activation points at (human-declared, stamped, not rebuilt). Domain-neutral; reads no config.

```python
@dataclass(frozen=True)
class External: name: str; root: Path; stamps: tuple[str, ...]
@dataclass(frozen=True)
class Environment:
    name: str; prefix: Path; owner: str = "human"
    activate: Path | None; boot: Path | None; externals: tuple[External, ...]
    python -> Path                       # prefix/bin/python
    activation_env() -> dict[str, str]   # the activation sourced clean, or os.environ without one
fingerprint(env) -> str                  # sha256 over conda url#md5, pip name==version+RECORD hash,
                                         # activation script + the vars it sets, boot file, external stamps;
                                         # cached on conda-meta/history, site-packages, script/stamp mtimes
lock(env) -> dict                        # conda explicit list (@EXPLICIT url#md5), pip pins, activation, externals
write_lock(env, lock_dir) -> Path        # <lock_dir>/<fingerprint>.json, written once
fingerprint_and_lock(env, lock_dir) -> str   # what a receipt stamps: never a fingerprint without a lock
```

The application builds them: `auto_research.env.analysis_environment(config)` (from `analyze_env`,
`analyze_ini_sh`, `repl_init_py`, `env_externals`), `host_environment()` (the orchestrator's own),
`env_lock_dir()` (`<runtime>/conda/locks`). The REPL (`PersistentSandbox(environment=)`), the shell
server (`SandboxSettings.environment` → `_env_spelling`), the gate's Layer-B run, the author's shell
(`merge_flow._author_env`), the preflight probe and the jail probe all read the analysis
environment through this one object; `runner.run_tool` (one-shot, ad hoc) still takes its keys.

A lock (`<fingerprint>.json`) carries the components the fingerprint hashes (`components`), a conda
explicit list, pip requirements (`name==version`, `name @ <url>` or `-e <url>` from
`direct_url.json`), the activation script's and boot file's text, and the external stamps. Values
the activation builds from HOME/USER are normalised (`$HOME`), so the identity is not per-user.
Every file the fingerprint reads is opened non-blocking and must be a regular file (a FIFO or
device reads as absent, never hangs the call); text reads are capped (records 16 MB, `INSTALLER`
1 KB, `direct_url.json` 1 MB), and a hashed file over 64 MB is identified by `size:<n>` instead.
A **pipeline** environment's activation is coder-written and never runs in the runtime: its
fingerprint takes the script's bytes only, `describe` lists no `activation_sets` for it, and
`Environment.activation_env()` raises for it; it is sourced only inside a jail (the shell:
`react._in_environment`; the REPL when routing lands, B3b). Human activations are sourced clean
in the runtime as before.
Known limits: `.egg-info` pip installs are not listed; a file edited in place inside site-packages
does not move the cache key; NFS attribute caching can delay a change made on another host.

### `env_build.py` — pipeline-owned environment definitions (env plan B3a)

A definition is a folder `<toolkit>/envs/<name>/` holding only plain files (no links, no other
hard links, nothing else): `env.yaml` (purpose, `base`, `externals`), `env.lock` (conda explicit:
conda-forge URLs with md5 or `sha256:`, this machine's platform or noarch, one line per package),
`env.pip` (wheels conda-forge lacks, each `name==version --hash=sha256:<hex>`), `activate.sh`,
`boot.py`. Lock and pip files are plain ASCII with `\n` line ends. **Built** (a lock, no base) →
created by the runtime into `<conda_dir>/envs/ar-<name>` (a name a human can type; a rebuild moves the previous build aside to `ar-<name>.prev-<sha8>`, keeping only the latest); **derived** (a human `base`, no
lock, no `env.pip` — it installs nothing) → that prefix, read-only, with its own activation and
boot file.

```python
read_definition(folder) -> EnvDefinition          # env.yaml read safely (no link/FIFO, size cap, no anchors/aliases); wrong types read as empty
validate(folder, *, base_envs, externals) -> list[str]   # stages a private copy, checks the copy; reasons as fixes (lines quoted ≤48 chars, YAML errors by line)
build(defn, *, conda_dir, conda_exe, base_envs, externals, jailed, runner=subprocess.run, timeout_s=3600) -> Environment
build_record(conda_dir, prefix) -> dict | None     # {"definition_sha256", "jailed", "name", "activate", "boot", "externals"} after a verified build; a derived one has "base" instead of "jailed"
built_environments(conda_dir, externals, base_envs=None) -> list[Environment]   # every finished build, latest per name, activation only from activation/; a derived record resolves its base in base_envs (skipped if gone)
build_policy(*, conda_exe, staged, target, work) -> JailPolicy   # one build's jail
forget(conda_dir, name) -> bool                    # drop ar-<name>'s record (definition deleted); the prefix stays
installed_dists(prefix) -> list[str]               # <lib>/<name>.dist-info of every installed distribution, sorted
replay(lock, *, conda_dir, conda_exe, jailed, runner=subprocess.run, timeout_s=3600) -> dict   # B4: a recorded state's conda part rebuilt into replay/<fp16>, compared
recorded_dists(conda_dir, prefix) -> list[str] | None   # the baseline written at build (sidecar built/<prefix>.dists)
StoppableRunner()                                  # subprocess.run stand-in; .stop() kills what it runs (own session each)
conda_root(conda_exe) -> Path                      # the installation, as spelled; refused if not one or if it holds $HOME
host_subdir() -> str                               # conda's platform name for this machine
verify(defn, prefix)                               # stages the definition, then BuildMismatch unless records == the lock and pins installed
```

`build`, under `<conda_dir>/.install.lock` (opened `O_NOFOLLOW`, outside every build's reach):

1. `conda_dir` and its `envs/`, `built/`, `staging/` must be real folders of this user (no links);
2. the definition's files are copied once (`O_NOFOLLOW | O_NONBLOCK`, plain file re-checked on the
   descriptor; the folder opened once `O_DIRECTORY | O_NOFOLLOW` and every file read relative to
   it) into a private `<conda_dir>/staging/build-*/def/<name>/` — `def/` keeps it apart from the
   build's jail-writable `tmp`, `home`, `pkgs` whatever the definition is named; **the copy** is validated, hashed
   (full sha256), built from and verified — a definition
   changed after review is never installed;
3. a prefix whose record `<conda_dir>/built/<prefix>.json` (written only by the runtime, temp +
   replace, `O_NOFOLLOW`) names this sha256 — and, for a jailed caller, says `jailed` in a
   `conda_dir` no unjailed build has ever run in (`built/unjailed.json`, sticky) — is verified and
   reused (a reused prefix failing `verify` raises every time: a loud tamper signal). Before a
   rebuild the old record is deleted first, so an interrupted rebuild is never reused. The sticky
   taint has no automatic reset: delete `built/unjailed.json` by hand once the `conda_dir` is
   trusted again (until then every jailed build re-downloads); anything else at that path is removed (read-only leftovers made
   writable first) and an empty target created;
4. `conda config --show pkgs_dirs` must be exactly this build's own cache;
5. every command's output goes to `staging/build-*/output.log` (no build binds it), never into
   memory; errors quote its last 400 bytes;
6. `conda create --yes --quiet --prefix … --file <staged lock>` (explicit: no solver), then
   `pip install --isolated --no-deps --no-cache-dir --require-hashes --only-binary=:all:`;
7. `verify`, then the record; the staging folder (copy, cache, home, `/tmp`) is removed whatever
   happened.

`jailed` is required. With it, every command runs under `build_policy`: conda's installation and
the staged copy read-only; only the target prefix and the build's own cache and home writable;
private `/tmp`; network on. No bwrap → refused. `jailed=False` is the caller's explicit choice,
written into the record. The process environment is only `_BUILD_PASS` (PATH, locale, proxies, CA
bundle; a proxy URL with credentials is visible to package scripts — accepted) plus `HOME`,
`CONDA_PKGS_DIRS`, `CONDA_ALWAYS_COPY=true`, `CONDA_CHANNELS=conda-forge`,
`CONDA_REGISTER_ENVS=false`, `CONDA_SAFETY_CHECKS=enabled`, `CONDA_EXTRA_SAFETY_CHECKS=true`,
`CONDA_SSL_VERIFY=true`, `PYTHONNOUSERSITE=1`. `verify` checks records, not file contents: a
prefix is written only by its own build, and the records live in that prefix, so `verify` is a
consistency check, not a defence against the build's own scripts. Definition files are capped at
4 MB, records and dist-info files at 4 MB (64 MB in all), every read `O_NOFOLLOW | O_NONBLOCK` on a regular
file. A pinned wheel is matched by its `<name>-<version>.dist-info` folder and METADATA together.
Error text quotes ≤48 chars per value and ≤400 chars of build output. It also refuses a conda
record that is a link, not a regular file or not a JSON object, a `pyvenv.cfg` (prefix or `bin/`)
or `bin/python*._pth` (each moves Python's standard library; the only start-up files looked for),
and a `lib` or `site-packages` that is a link. It does **not** look for start-up files (`.pth`,
`sitecustomize`, …) a build's package script may drop outside its records — ruling 2026-10-06: they
escape the fingerprint, an accepted residual risk (CLAUDE.md §9.6). Its cost is bounded: record
count checked against the lock first (listing capped at 200,000), records streamed one at a time
(4 MB each, 64 MB in all with the pinned METADATA files), site-packages listed only when there are
pins (≤4 real `lib/python*` folders, links not followed), a second install of a pin refused the
moment it is listed, before anything is read. `verify` also checks the staged lock and pip files first. Problem lists
from `validate` stop at 20 with a count of the rest; unknown keys are quoted short; a definition
folder with many files is one refusal. Inside the jail the network is fully on (localhost included) and the conda
installation is readable — accepted. `Environment.activate`/`boot` point at a runtime-owned,
read-only copy of the reviewed bytes, `<conda_dir>/activation/<sha16>/{activate.sh,boot.py}`
(content-addressed; built and derived alike; an existing copy is reused only if it is a regular
file of this user with one link and no write bit, else replaced), never the toolkit file: the toolkit is writable to an
author during authoring, and the fingerprint sources the activation outside any jail. A script that
locates files relative to itself sees that folder. `env.yaml` anchors and aliases are refused (a
524-byte alias nest reached a 4 GB `MemoryError`); any parse failure (deep nesting, an over-long
integer, an unknown tag) is a one-line refusal naming at most the line; `env.yaml` is capped at 16 KB and refused unread
past 64 brackets (PyYAML's scanner is superlinear on nested flow openers). Staging for `validate`/`verify`/derived runs
under the host temp root; no jailed agent binds it (reviewed) — `build` re-stages under
`conda_dir/staging`. The install lock relies on `flock` over NFS (lockd). Agents never run conda. App side:
`auto_research.env.agent_conda_dir()`, `conda_executable()` (config `conda_exe`, `$CONDA_EXE`,
PATH). Live-verified 2026-10-06: a conda-exported lock rebuilt exactly, jailed, `~/.conda`
untouched (`tests/test_env_build.py`, opt-in `AR_LIVE_ENV_BUILD=1`).

**Solving and pinning (B3c-3).** `solve(specs, *, conda_exe, work, jailed, runner, timeout_s)` →
`env.lock` text: a conda-forge-only `create --dry-run --json` into a throwaway prefix with an
empty package cache, its `LINK` order mapped to `FETCH`'s URL and md5; specs must be plain
constraints (`_SPEC`), nothing is installed. `pin_wheels(python, requirements, *, work, jailed,
runner)` → `env.pip` lines: each `name==version` downloaded as one binary wheel with the
candidate's own Python (`--isolated --no-deps --only-binary=:all:`), hashed here. Both run with
the build's environment and, jailed, only `work` writable.

**Build on commit (B3c-4).** A direct-branch `AuthoringSession.commit` builds every environment the
commit concerns (`merge_flow.committed_environments(toolkit_root, manifest)`: each changed, undeleted
definition and the `env:` of each changed package) into `env.agent_conda_dir()` with the configured
human base, declared externals and the jail setting (`build_committed_environments`), before the
caller reloads the registry; each result is a ledger event (`env.build` / `env.build.error`: env,
name, prefix, definition digest, seconds, commit sha) and the lead's commit answer carries
`session.env_note` ("Environment built: ar-x." or why it did not build; the commit stands). Worktree
mode builds nothing. `env.env_catalogue` then lists the build, so `use_env` can choose it. A
commit deleting a definition drops its record (`env_build.forget`; ledger `env.forget`), so the
catalogue stops listing it; its prefix stays for a process still running in it. A reused prefix
whose activation, boot file or externals changed gets its record rewritten. The build runs on an
`env_build.StoppableRunner` (each command in its own session), which a stop kills; the candidate
tools use one too, killed by `CandidateEnvs.close()`. Any failure after the commit is a note, never
an exception (the commit stands). Accepted residuals: a built→derived switch leaves the old prefix
on disk without a record; after any unjailed build the sticky taint makes every jailed commit
rebuild from scratch; the build reads the live tree just after the commit (a write in between is
sabotage-only); a rebuild is visible at once to a new path lookup (the shell binds the whole
`envs` folder), so a run sharing the runtime dir that uses `ar-x` while another rebuilds it may run
a half-built prefix. The gate admits a deleted definition (`structural.env_definition.<name>`)
unless a package anywhere in the toolkit still names it.

**Launch reconcile and drift (B3c-5).** `merge_flow.reconcile_environments(toolkit_root, *, runner)`
runs at every launch after preflight (`graph.runtime._reconcile_environments`, on a
`StoppableRunner`): each definition on the toolkit branch is compared with its build. A missing or
stale record is built (a commit build that was stopped). A definition that does not validate is
`failed` and its build left alone. A built prefix that fails `verify`, or whose installed
distributions differ from the baseline recorded at its build (`installed_dists`, sidecar
`built/<prefix>.dists`; a `pip install` by hand), has drifted: its state's lock (activation
included) is written to the lock store, the prefix kept as `ar-<id>.drift-<fp8>`, and a fresh build
made from the unchanged definition; if that build fails the drifted prefix and its record are put
back and stay in use. Every other definition goes through `build()`, which reuses an intact prefix
and refreshes its record (an activation-only commit whose build was stopped), or redoes a build the
jail setting no longer admits. A built environment whose declared externals' stamps changed is
reported and the new stamps recorded (`external_stamps`; an external cannot be rebuilt). With the
jail on and the unjailed taint present, one `tainted` result says nothing is reused until a human
removes the marker. Ledger: `env.build`, `env.drift`, `env.external_changed`, `env.build.error`,
`env.tainted`, `env.reconcile.error`; nothing stops the launch. Derived environments are only
recorded (their prefix is the human's), their declared externals stamped like a built one's. A stop
leaves the remaining environments untouched. Residuals: reconcile checks without the install lock,
so a run sharing the runtime dir may race its record rewrite; a build from before the dist
baseline takes its first baseline from its current state; a rebuild forced by the jail rule reads
as `ok` (the `tainted` notice covers the usual cause). Units are told nothing yet:
per-call drift detection and the tool-result notice come with routing (B3b), when units run tools
in these environments.

**Replay (B4).** `replay(lock, *, conda_dir, conda_exe, jailed, runner, timeout_s)` rebuilds the
conda part of a recorded state (an `envs.lock`, as the lock store keeps one per fingerprint) into
`<conda_dir>/replay/<fp16>`: the explicit list installed as it is, jailed like a build, from a
private cache; Anaconda's channel accepted beside conda-forge (the human's environments use it),
every line with its checksum and this machine's platform or noarch; a prefix already holding the
recorded packages is reused, a finished one that differs (a package installed to debug) is kept
aside as `<fp16>.changed-<hex6>`, and a lock recording a package without a URL, or one twice, is
refused. Report: `conda_match`, `missing`, `extra`, `pip_not_replayed` (the
lock names pip versions, not files). App side: `env.replay_environment(fingerprint)` reads the
lock store (refusing a lock that does not hash to the fingerprint it is filed under), replays into
`agent_conda_dir()/replay/`, writes the recorded activation and boot file beside the prefix with
their `owner` (a pipeline environment's are coder-written: source them in a jail), and says for
each recorded external whether it is `same`, `changed` or `undeclared` now. Live-verified
2026-10-06: a recorded conda-forge env replayed and matched (`AR_LIVE_ENV_BUILD=1`); a review
replayed `.tar.bz2`, `.conda` and `repo.anaconda.com` lines with matching records.

**Layer B in the named environment (B3c-2).** `run_gate` groups the changed tests by the
environment their package's `tool_config.yaml` names (`_run_layer_b`, read from the judged
export). Each named definition is built from the judged bytes into the gate's own conda dir
(`agent_conda_dir()/gate`: never listed in the catalogue, reused while its digest is unchanged;
`_gate_environment`, a `StoppableRunner` a stop kills; a derived one's human base activation is
copied there so the jail can reach it) and the default runner runs pytest with that environment's
Python through `envs.in_environment` (`keep`: the return recorder's variables and `PYTHONPATH`), so
a coder-written activation is sourced only inside the jail, which binds the prefix and the
activation copy read-only (`_jailed_pytest(extra_ro=)`). A build failure is a failed
`layer_b.env.<id>`; tests of a package naming none run in the analysis environment as before; one
merged outcome; each group's return recorder watches only its own environment's tools. Gate builds
live in one folder per definition digest (`gate/<sha16>/`, so concurrent gates never move one
another's build) and a folder unused for a day is removed under one prune lock; a derived
environment's copied base activation lives inside its digest folder. In the environment path the jail's
private `PFILES` folder is handed to the launcher, which restores it after the activation. An
injected test runner receives `environment=` for a named group. Residuals: the build is outside the
per-run test timeout (only conda's own 3600 s per command bounds it); a changed tool whose package
has no tests in the changeset and names an environment no test group runs in is not watched.

**Routing (B3b).** A package names its tested environment in `tool_config.yaml` (`env: <id>`;
`ToolkitRegistry.tool_env(kind)`, cleared on `reload`). `tool_definitions` declares an optional
`env` argument on every toolkit tool (unless it has one of its own) and appends "Runs in its tested
environment ar-<id>." to a routed tool's description. `PersistentSandbox.call_tool(...,
environment=)` runs a call in another environment on its own REPL from a per-worker pool (started at
its first call; same workdir, toolkit and jail policy; label `repl-<name>`; replaced when the
catalogue's record changes; closed by `restart` (a commit reload) and `shutdown`, killed by
`abort`/`kill_now`; a failure or an environment-change restart of the worker's own REPL keeps them).
A pool REPL in a pipeline environment whose activation is the runtime's copy (under
`agent_conda_dir()`) starts through `envs.in_environment` inside the jail, its prefix and its
coder-written activation and boot files bound read-only as files (and refused unjailed when the jail
is configured); a derived one with no activation of its own sources its
human base's as the analysis REPL does. `call_environment`, `call_stamp` and the notice then name
the pool REPL's. The worker side is `react.SandboxToolProvider._route_environment` (agents/DOCS).

### `coder/env_tools.py` — the coder's environment tools (env plan B3c-3)

`CandidateEnvs(worktree, cand_dir)` holds one authoring attempt's tools, served by both harnesses'
escalation servers: `env_solve(env_id, specs)` writes `envs/<env_id>/env.lock`;
`env_pin_wheels(env_id, requirements)` writes `env.pip` (needs an activated candidate);
`cand_env_activate(env_id)` validates and builds the worktree definition into
`cand_dir/conda` and fills `cand_dir/active/bin` with one wrapper per executable, each the same
launcher a worker's `use_env` shell uses (`envs.in_environment(environment, command, toolkit_root,
pass_args=True)`: `/usr/bin/env -i` with the jail's home, temp, parameter folder, locale, identity
and proxies; the activation sourced, an activation that fails or exits reported with exit 97; the
jail's writable `PFILES` half restored; the candidate first on `PATH`; the worktree on
`PYTHONPATH`). The activation is the worktree's `activate.sh`, or the built one when the definition
has none (a derived environment's base activation). A core command the candidate lacks (`python`,
`pytest`, `pip`) answers so with exit 127 instead of falling through to the analysis environment.
Conda's own `etc/conda/activate.d` hooks are not run (as for the REPL and `use_env`). With no id, or after a failed build, the folder is emptied.
The coder runs no conda. The runtime writes `env.lock`/`env.pip` only into a plain folder inside
the worktree (no link on the way; written by replace) and nothing after `close()`, which
`AuthoringSession.aclose` calls first; each attempt has its own folder
(`CandidateEnvs.attempt_dir(<worktree_base>/envs_cand, tool)` → `<tool>-<hex8>`, earlier ones removed
best-effort), since a build orphaned by a stop can still be writing. Solves, pins and builds are
serialised. Accepted residual: with the jail off the candidate is writable to the coder (a `pip
install` would land in it); production runs jailed, and unjailed confinement is best-effort (§13).
Also accepted (low): a copied base activation that sources a sibling by a relative path fails
loudly (exit 97; the configured one uses absolute paths); the wrappers put the worktree's parent
on `PYTHONPATH` as `use_env` does while the gate's Layer B does not, so an
`import research_toolkit.<pkg>` test passes in the candidate and fails the gate visibly.
`env_pin_wheels` needs a candidate built from the current `env.lock` and answers a derived
definition (no `env.pip`) directly. Activations are serialised.
Codex is told `tool_timeout_sec = 3600` for the escalation server (its default is 60 s; a build
takes minutes). A session whose artifacts dir lies in the toolkit gets no environment tools. `cand_dir` is `<worktree_base>/envs_cand/<tool>`, read-only in the coder's jail
(`coder_policy(readonly=)`), `active/bin` first on its `PATH` (`_author_env(active_bin=)`), and
removed by `AuthoringSession.aclose` (`cleanup`). The seed lists each package's `env:`, the
definitions under `envs/`, the human base and declared externals (`merge_flow._environment_context`).
Live-verified 2026-10-06: solve `python=3.12` → build → plain `python` runs in the candidate
(`tests/test_env_tools.py`, opt-in `AR_LIVE_ENV_BUILD=1`).

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
  --proc --dev --unshare-{user,pid,ipc,uts} --die-with-parent --new-session
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
- `PersistentSandbox(toolkit_root, workdir, *, environment=None, environments=None, label="repl", default_timeout_s=300, mcp_client=None, analyze_env=None, analyze_ini_sh=None, repl_init_py=None)` —
  long-lived REPL subprocess per analyst worker, in `environment` (an `envs.Environment`; the
  legacy `analyze_env`/`analyze_ini_sh`/`repl_init_py` spell the same thing). The environment's
  `boot` file is exec'd once at REPL boot for domain **session** setup, passed via
  `AR_REPL_INIT_PY`, with fd 1 on stderr while it runs; a failure is swallowed by the REPL, so a
  boot file reports its own on stderr. For the human environment the boot file is config
  `repl_init_py`, which lives in this repo and is not mounted in the jail (F21); a pipeline
  environment's boot file is the toolkit's `envs/<id>/boot.py` (copied under the agent conda dir and
  bound read-only), so `ar-xspec` calls get the XSPEC session and relxill. A call with
  `environment=` another environment runs in a pool REPL of its own (`label`).
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
  - `jail.coder_policy(*, label, extra_rw, readonly=())` → `JailPolicy` — the author-harness policy
    (`readonly`: folders bound read-only on top of a writable root, created first — the attempt's
    candidate environments). An author
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
- `coder.escalation_server.EscalationServer(ask_lead, ask_human, *, env_tools=None)` — a Streamable-HTTP MCP endpoint
  serving the coder's `message(to, text)` tool (plus the three environment tools when `env_tools`
  is given; `claude_sdk._make_escalation_server(ask_lead, ask_human, env_tools)` likewise) (`MESSAGE_DESCRIPTION`, `route_message`) from
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
- 2026-10-06 — **Env plan B1: environments as objects.** New `sandbox/envs.py` (`Environment`,
  `External`, `fingerprint`, `lock`, `write_lock`, `fingerprint_and_lock`); `auto_research.env`
  gains `analysis_environment`, `host_environment`, `env_lock_dir`; config gains `env_externals`
  (HEASoft build record, CALDB indexes). The six readers of the analysis-env keys read the one
  object; `PersistentSandbox(environment=)` (legacy kwargs build one). `_source_ini_env` caches on
  the script's mtime too. Measured: the real analysis env fingerprints in 2.6 s cold, 1.5 ms cached
  (176 conda + 5 pip packages, 29 activation vars, 2 externals).
- 2026-10-06 — **B1 review fixes.** Fingerprint and lock come from one computation (`_state`), so a
  lock always hashes to its name; lock temp files are unique per writer (threads raced); activation
  values built from HOME/USER are normalised; locks embed the activation script and boot file text
  and pip sources from `direct_url.json`; the cache key adds `conda-meta`'s own mtime;
  `analysis_environment` expands `~`; the gate and preflight keep sourcing an init script that has
  no prefix configured; the shell server is built from the environment object.
- 2026-10-06 — **B1 second review fixes.** HOME is normalised only as a whole leading path (a raw
  replace rewrote `libc` for USER `li`; USER/LOGNAME are no longer touched; sourcing under a
  placeholder HOME was rejected because HEASoft's setup then stops early). A state whose activation
  errored is not cached. `fingerprint_and_lock` checks the lock file each call (no in-process
  memo). Pip locks spell a VCS install `name @ git+<url>@<commit>`. The task input and files a human
  sends carry no environment. `run_research` fingerprints `SandboxSettings.environment`, the object
  the REPL and shell run in. Known edge: a HOME that prefixes unrelated paths (`/opt`).
- 2026-10-06 — B1 third review (clean): a HOME with a trailing slash normalises to the same
  identity; `fingerprint_and_lock` raises for a state whose activation failed (the worker then
  leaves its receipt unstamped); pip VCS locks keep `#subdirectory=`. Known edges recorded in
  `_activation`: values built from USER, HOME embedded mid-value, a resolved HOME spelling.
- 2026-10-06 — **Env plan B2a.** `PersistentSandbox` records the environment fingerprint it booted
  on and, before each call, restarts first if the fingerprint changed (`_restart_if_environment_changed`),
  so no call runs on old in-memory modules over new on-disk packages. `envs.describe(env, detail=)`
  is what `list_env` shows.
- 2026-10-06 — **B2a review fixes.** A restart the environment check starts is contained: a failure
  comes back as the call's error (`_EnvironmentRestartFailed`), and a state whose activation failed
  never triggers one (`_current_fingerprint` returns None). A restart is announced on that call
  (`env_notice` → a `[environment changed …: the REPL was restarted and its session state cleared]`
  line after the receipt header), since tools that build on a session (a loaded dataset, a fit)
  lose it. The REPL records `booted_fingerprint` (lock written at boot), and its receipts stamp
  that: the state it actually ran on, not one that changed during a long call.
- 2026-10-06 — **B2a second review fixes.** The restart notice travels out of band
  (`PersistentSandbox.take_notice()`, read once by the provider), never in the result dict — a tool
  returning a key of that name had written into the runtime header; `env_notice` is not a protocol
  key. The REPL fingerprints before sourcing its activation (`booted_fingerprint`) and stamps it only
  once the lock is on disk (`booted_lock_written`), so an unwritable lock dir no longer disables the
  restart check. Shell (MCP) receipts are unstamped: the shell server keeps the activation captured
  at its launch and is shared by every worker.
- 2026-10-06 — B2a third review (clean), lows applied: `start()` clears `booted_lock_written` first;
  `call_tool` fixes `call_stamp` (the state the call runs in) before running, so a timeout restart
  inside the call cannot restamp it; fingerprints print as `env:<hex12>` everywhere a model reads one
  (header, notice, `list_env`, the turn-start brief); receipt headers collapse captions/file names
  to one line.
- 2026-10-06 — **Env plan B3a:** `sandbox/env_build.py` (definition read/validate, runtime builder with the safe layout and an install lock, lock verification); `env.agent_conda_dirs`, `env.conda_executable`. Live-verified build.
- 2026-10-06 — **Env plan B3a hardened (review 1):** `env.pip` takes only `name==version --hash=sha256:…`, installed `--isolated --require-hashes --only-binary=:all:`; the build runs jailed (`build_policy`, `wrap=`) with an allow-listed environment and a private home (`CONDA_REGISTER_ENVS=false`); refuses a merged condarc `pkgs_dirs`; a completion marker `.ar-built` checked under the install lock (an interrupted prefix is rebuilt); `verify` checks pip pins and `sha256:` lock lines; locks refused for a foreign platform or a repeated package; `env.yaml` field types checked. Live: jailed rebuild exact, `~/.conda/environments.txt` untouched.
- 2026-10-06 — **Env plan B3a hardened (review 2):** `build(conda_dir=…, jailed=…)` (required) replaces `envs_dir/pkgs_dir/wrap`; the definition is staged once and the copy validated/built/verified (TOCTOU); per-build cache, home and `/tmp`, only the target prefix writable; records in `<conda_dir>/built/` (`build_record`) and the install lock outside every jail, both `O_NOFOLLOW`; definition folders refuse links, hard links and stray files, rejected lines quoted short, lock/pip ASCII only; `conda_root` refuses a non-installation or one containing `$HOME`; conda safety checks pinned on; `verify` compares versions with `packaging` and refuses Python distributions nothing names. `env.agent_conda_dirs` → `agent_conda_dir`.
- 2026-10-06 — **Env plan B3a hardened (review 3):** a jailed caller never reuses a prefix built unjailed; a derived definition with `env.pip` is refused; `validate` itself stages and checks a copy (no check-then-read on a live folder), YAML errors reported by line only; staging opens `O_NONBLOCK` (a FIFO cannot hang the lock) and is cleaned on every refusal; `conda_dir` folders must be real folders of this user; leftover read-only prefixes are removed; records store the full definition sha256; `verify` refuses malformed or linked conda records and unowned `*.egg-info`/`*.pth`/`sitecustomize.py`. The jail test asserts on the real `jail_argv`.
- 2026-10-06 — **Env plan B3a hardened (review 4 + peer review):** `jail_argv` adds `--new-session` for every jailed process (TIOCSTI into the parent terminal reproduced with `legacy_tiocsti=1`; bwrap stays in the caller's group, so group kills and `--die-with-parent` still hold); `verify` stages the definition (a linked `env.lock` is never read or quoted) and reads records and dist-info files bounded, `O_NOFOLLOW | O_NONBLOCK`, regular only; pins matched by dist-info folder + METADATA; `sitecustomize*`/`usercustomize*` in the stdlib folder and as packages refused; `_remove_tree` handles untraversable folders (retry once per path); a sticky `built/unjailed.json` disables jailed reuse in a `conda_dir` an unjailed build ran in; derived environments come from one staged read; prefix id 16 hex; definition files capped at 4 MB; non-string YAML keys refused, not crashed on; build output quoted ≤400 chars. Reviewed (5).
- 2026-10-06 — **Env plan B3a hardened (review 5):** a rebuild deletes the old build record before touching the prefix (an interrupted rebuild was reusable); `read_definition` reads `env.yaml` like staging (no link/FIFO, size cap) and `_load_yaml` refuses YAML anchors/aliases (alias bomb) and reports parse errors by line only; non-string values read as empty instead of `str()`; activation and boot file are a content-addressed runtime copy of the reviewed bytes (`<conda_dir>/activation/`), not the live toolkit file; duplicate-package check O(n); base/external names quoted short; docs: `<id16>`, staging layout, taint reset by hand.
- 2026-10-06 — **Env plan B3a hardened (review 6):** staging moved to `build-*/def/<name>/` (a definition named `tmp` was staged into the folder bound writable at the jail's `/tmp` and could be rewritten between conda and pip — verified with real bwrap); the definition folder is read through one `O_DIRECTORY | O_NOFOLLOW` descriptor; build output goes to a log file and only its tail is quoted (`_call`); `verify` refuses a record count different from the lock before reading, caps bytes, precomputes owned folders, refuses linked dist-info and non-string record URLs; activation copies reused only when ours, single-link and read-only; YAML parse failures of every kind are short refusals.
- 2026-10-06 — **Env plan B3a hardened (review 7):** `verify` indexes only the top entry of each owned path (the whole-ancestor index was quadratic: a 200 KB record hit `MemoryError`); record listing stops one past the lock's count; dist-info reads share the byte budget and several installs of one pin are refused before reading; caps lowered to 16 MB/record, 128 MB total from measured real environments; `env.yaml` capped at 16 KB and refused past 64 brackets; `build_record` reads bounded and non-blocking; a non-list `pkgs_dirs` and a `..` folder name are refusals, not crashes; `_read_plain`'s cap is read at call time. No CPU/memory/process limit on build processes: accepted by ruling 2026-10-06 (reviewed, hash-pinned conda-forge packages, jailed).
- 2026-10-06 — **Env plan B3a hardened (review 8):** `verify` finds start-up candidates first and streams records against them (memory one record; constant work per path — the per-folder scan was quadratic in the number of `lib/python*` folders, and kept every record's file list: 3.2 GB measured); `lib-dynload` and `lib/python*.zip` covered; folder listings streamed and capped at 200,000 entries; more than 4 `lib/python*` folders refused; record budget 64 MB; non-string record paths skipped; `RecursionError` in a record is a mismatch; public `verify` checks the staged lock/pip first; `validate` problem lists capped at 20 (+ count), lock/pip checked one line at a time (a 4 MB bad lock made 2 M problems, 1 GB), unknown keys quoted short, a crowded definition folder one refusal.
- 2026-10-06 — **Env plan B3a hardened (review 9):** `lib/python*` links counted with folders (≤4) before any is followed, and followed one `readlink` hop to a sibling by bare name (a chain of 4,000 links took 48 s to resolve); `pyvenv.cfg` and `bin/pyvenv.cfg` are start-up candidates (they move the standard library); `lib`, `site-packages` and `lib-dynload` refused as links; ≤10,000 candidates; records capped at 4 MB each; `conda-meta` listing capped; an unlistable folder or missing `conda-meta` is a `BuildMismatch`. Review loop stopped here by the user's instruction (2 rounds after review 7, not converged).
- 2026-10-06 — **Env plan B3a: start-up-file hunt dropped (human ruling).** `verify` no longer lists start-up candidates or reads RECORD files; it checks the conda records against the lock, the pins (dist-info folder + METADATA, one install each), and refuses a `pyvenv.cfg`. Removed `_startup_candidates`, `_top_key`, `_SITE_ENTRIES`, `_MAX_CANDIDATES`; added `_pin_dist_infos`. A file a build drops outside its records is an accepted residual risk.
- 2026-10-06 — **Env plan B3a final review (10):** a second dist-info claiming a pin is refused as soon as it is listed (decoys were gathered first: ~500 MB measured); `lib` and `site-packages` refused as links again (the refusal had gone with the hunt); `bin/python*._pth` joins `pyvenv.cfg`; a test pins `--new-session`. `envs.py` fingerprint reads are non-blocking, regular-file only and bounded (`_read_regular`; `_file_sha` streams, files over 64 MB identified as `size:<n>`): a FIFO in a dist-info passed `verify` and then hung the fingerprint.
- 2026-10-06 — **Env plan B2b (`use_env`):** build records carry the environment's name, activation, boot file and externals; `env_build.built_environments` reads them back (the catalogue's pipeline half); `envs.shell_variables` gives a shell's clean start in an environment (never the runtime's own variables). `PersistentSandbox(environments=)` takes the catalogue callable and holds `shell_environment` (the worker's `use_env` choice, kept across rounds).
- 2026-10-06 — **B2b review fix:** `envs.shell_variables` removed — a built environment's activation is now sourced inside the jailed shell at command time (`react._in_environment`), never in the runtime: sourced there, a coder-written activation's values could carry what it read outside the jail into the agent's shell (reproduced). `shell_environment` holds the chosen name, resolved again on every call.
- 2026-10-06 — **B2b re-review fix:** a pipeline environment's activation is never sourced in the runtime — `list_env` → `describe` → fingerprint had run a coder-written `activate.sh` outside the jail with the user's reach, and its variable names could carry data out (reproduced: a secret hex-encoded into a name). Fingerprint by script bytes only, no `activation_sets` listed, `activation_env()` refuses. The shell wrapper traps an activation that exits and clears shell options it set.
- 2026-10-06 — **B2b review 3 lows:** a pipeline prefix's files are read for the fingerprint without following links (`_read_regular`/`_file_sha` `follow=False`), so a link a build planted cannot bring an outside file into a listing.
- 2026-10-06 — **Env plan B3c-1: the gate admits environment definitions.** `gate._gated` = a `*_utils` file or `envs/<name>/{env.yaml,env.lock,env.pip,activate.sh,boot.py}`; `collect_targets` (hence the review diff and `approve_edit`'s staging) and `structural.changes_allowed` use it. New Layer-A checks `structural.env_definition.<name>` (`check_env_definitions`): each touched definition validates (`env_build.validate` against the configured human base and declared externals) and ships with a touched package whose `tool_config.yaml` names it (`env: <name>`, read by `_package_env`). `env.lock`/`env.pip` skip the prose scan (pinned URL/hash lists, validated line by line). Layer B in the candidate environment is B3c-2.
- 2026-10-06 — **Env plan B3c-1 (review fixes + naming ruling):** gated deletions are staged and shown (`gate.collect_deleted`; `approve_edit`, `review.build_diff_bundle`); the comment lines of `env.lock`/`env.pip` are scanned as prose; `structural.env_resolves`: every package the session works on (the brief's `target_domain` and every touched package) that names an env in `tool_config.yaml` must resolve to `envs/<env_id>/env.yaml`, or the gate fails naming the fix (a typo is retried like any gate failure). Built envs are `ar-<name>` with no id suffix (`EnvDefinition.build_id` removed); a rebuild moves the previous finished build aside to `.prev-<sha8>`.
- 2026-10-06 — **B3c-1 re-review fixes:** an env name in `tool_config.yaml` must be a name (`env_build._NAME`), not a path (`../x`, `/abs`, `x/` resolved before); a non-string or unparseable value fails resolution visibly. The gate entry unstages everything (`reset --mixed <base_ref>` always, not only when HEAD moved): a change staged by the coder would otherwise be committed whole while the gate and review read the work tree. `structural.changes_allowed` refuses a symbolic link among the changes or on their path. Changes are read with `git status -z` (`gate._status`), so non-ASCII names are judged by their real name. A rebuild renames the finished build aside first (`.prev-<sha8>-<rand6>`), then drops the record, then clears older `.prev` folders, so a crash never destroys the current build.
- 2026-10-06 — **B3c-1 review 3 fixes ([G3] closed):** the gate records `GateReport.manifest` (`{path: sha256 | "deleted"}` of every judged file, `gate.changeset_manifest`, bytecode caches excluded); `approve_edit(manifest=)` unstages the index, refuses if any judged file changed since, and stages exactly the manifest (`AuthoringSession.commit` passes the last gate's). A new Layer-A check `structural.tests_left_tree_unchanged` fails a gate whose tests changed the changeset. `changes_allowed` refuses a nested repository (`dir/` entry); `_TOOLKIT_PKG_RE` is anchored to top-level packages (`obsdb_catalog/x_utils/` is not a package); `_status` reads work-tree renames correctly; the gate entry unstages in worktree mode too, and a failed unstage is a failed gate; the failure reset cleans with `-ffd` (nested repositories too); the review bundle survives a binary file.
- 2026-10-06 — **B3c-1 review 4 fixes:** Layer B runs on a clean export (`gate._export_judged`: `git archive HEAD` plus exactly the manifest's files, each re-checked), so an ignored file (a sourceless `.pyc`, a self-ignoring `.gitignore`, anything the toolkit's `.gitignore` hides) can no longer be imported by the tests while staying out of the commit — measured before: a hidden module ran and a clean checkout of the commit failed. Manifest entries carry the file mode (`<mode>:<sha256>`; a link is `link:<target>`), so a judged file swapped for a link or made executable stops the commit. Tests added for the unstaged index, the session passing its manifest, and a test reaching the real worktree by absolute path. Accepted: `commit()` without a manifest (only injected stub gates; the real gate never passes with an empty one) keeps the old staging.
- 2026-10-06 — **B3c-1 review 5 fixes:** the Layer-B export writes `HEAD` from raw objects (`git ls-tree` + `cat-file --batch`), never `git archive`, so a committed `.gitattributes` (`export-ignore`, `export-subst`) cannot make the tested tree differ from the committed one; committed links are kept as links (no crash on one pointing outside). The gate's test jail masks the real worktree (`JailPolicy.masked`: an empty read-only tmpfs on top of every bind; `jail_argv` emits `--tmpfs … --remount-ro …`), so a test cannot import an unjudged file from it by absolute path. `tests_left_tree_unchanged`'s message now says what it catches (edits while the gate ran).
- 2026-10-06 — **B3c-1 review 6 fixes:** the export replaces a committed link on a judged file's path with a real folder before writing (as `git add` sees it), so a write never follows a link out of the export; a link replaced by a folder is recorded `deleted` (`collect_deleted`) and committed as a removal (`approve_edit` uses `git rm --cached` for removals, never `git add` of a path that is now a folder); each exported file is hashed from the bytes actually copied (one read); a missing or non-blob HEAD object is a refusal, not a crash; `write_launcher` keeps every policy field (`dataclasses.replace`), the mask included. Tests pin the mask wiring (the default runner's `masked=worktree`, `--tmpfs … --remount-ro` in the argv), HEAD's executable bits, and the stale-entry refusal.
- 2026-10-06 — **B3c-1 review 7 fixes:** a gated name holds no glob character (`*?[\\`; `changes_allowed` refuses it), and `approve_edit` stages with `git --literal-pathspecs` — a deleted `[ab].py` had removed `a.py` and `b.py` from the commit. Only `.pyc`/`.pyo` inside `__pycache__` count as interpreter caches (a `.py` placed there is judged). `GateReport.manifest` is `None` unless a real gate recorded it; `commit()` uses the strict path whenever one exists, even empty. Dead `rmtree` branch removed from the export. Tests pin a judged deletion committed from the manifest, a committed file link replaced by a file, and literal staging.
- 2026-10-06 — **B3c-1 review 8 fixes:** `approve_edit(manifest=, base=)` stages the judged bytes with no git filters (`merge_flow._stage_judged`: each file read once, hash checked, `hash-object -w --no-filters`, `update-index --cacheinfo`; deletions `--force-remove`) — `git add` had applied a `.gitattributes` encoding and committed code where a comment was tested; `.gitattributes`/`.gitmodules` are never gated. `GateReport.base` is the HEAD the gate judged (the export is written from it) and the commit refuses if HEAD moved. `AuthoringSession` records the changeset the co-lead reviewed (`_reviewed_manifest`) and refuses a commit whose gated changeset differs; `respond_to_review` waits for the coder like a submit; a refused commit returns an error to the lead (no exception escapes). A folder replaced by a file can pass the export.
- 2026-10-06 — **B3c-1 review 9 fixes:** `_stage_judged` applies deletions first (a file replacing a folder could never be committed); `merge_flow.purge_bytecode(toolkit_root)` deletes `*.pyc|*.pyo` inside the `*_utils` packages after every commit and every failure reset — git ignores bytecode, so a planted `.pyc` matching its source's header would have run in workers in place of the reviewed code (other ignored files, possibly the human's, are untouched); the commit runs no repository hook (`core.hooksPath=/dev/null`, `--no-verify`); a gate that cannot read the changeset (an unreadable file, a failed git call) is a failed `GateReport`, and a review whose changeset was unreadable can never be claimed by a commit. gate_bench's G3 probe (`commit_sweep`) now commits through the production path (`manifest` + `base`); a refusal counts as caught.
- 2026-10-06 — **B3c-1 review 10 fixes:** `purge_bytecode` covers the whole toolkit tree (`.git` aside), not only `*_utils/` — a planted `obsdb_catalog/__pycache__/*.pyc` or a sourceless root `json.pyc` (the root is first on workers' `PYTHONPATH`) was imported after a commit; `gate._git` decodes with `surrogateescape` (and `core.quotePath=false`), so a latin-1 byte in a diff or a file name is a failed check, not a crash; `run_gate_step` also turns a `ValueError` into a failed gate; a review in a real repository whose changeset cannot be read can never be claimed by a commit. gate_bench's report renders a refused commit sweep.
- 2026-10-06 — **B3c-1 review 11 fixes:** `purge_bytecode` resolves the toolkit root (it is a link on this host), makes each folder readable to its owner before listing it (a folder made unlistable hid a `.pyc` Python still loaded) and raises on any error; `commit()` purges **before** staging, so a purge that cannot finish is an honest no-commit (it had been a commit reported as "not committed" and then reset away). Worker REPLs and the shell keep bytecode in their own private folder (`PYTHONPYCACHEPREFIX`: the jail's `/tmp/ar-pycache`, or the process's private tmp when unjailed), so in-tree `__pycache__` is never read. gate_bench's `commit_sweep` records any commit error as refused.
- 2026-10-06 — **B3c-1 review 12 fixes:** `merge_flow._git_in` decodes with `surrogateescape` (a non-UTF-8 byte in a rejected attempt's diff had stopped the failure reset before it cleaned the tree, leaving rejected code for workers to load); `_save_diff_and_reset` runs `reset --hard`, `clean -ffd` and `purge_bytecode` in a `finally`, so saving the patch can never stop the reset; patches are written byte-exact (`errors="surrogateescape"`), which `git apply` restores; `gate._head_file` decodes the same way.
- 2026-10-06 — **B3c-1 review 13 fix:** the failure reset first makes every toolkit folder its owner's again (the `purge_bytecode` walk), then resets and cleans — a folder the coder made read-only or unlistable had stopped `git reset`/`git clean` and left rejected code where workers import it.
- 2026-10-06 — **B3c-1 review 14 fix:** `purge_bytecode` walks with an explicit stack (a 1,200-deep folder chain had overflowed the recursion and stopped the reset); the reset's first purge cannot block the reset for any reason.
- 2026-10-06 — **B3c-1 review 15 fix:** a direct-branch session purges bytecode on close even when no tracked file changed, and the resume heal purges before returning "clean" — a sourceless `numpy.pyc` left at the toolkit root (git-ignored, first on workers' `PYTHONPATH`) had survived both.
- 2026-10-06 — **B3c-1 review 16 fix:** a `.gitignore` git does not track (`gate.untracked_ignore_files`; pytest's own `.pytest_cache/.gitignore` aside) is a stray change at the gate, and is deleted (`merge_flow.remove_untracked_ignores`) before every failure reset, on every direct-branch session close and in the heal — so what it hid becomes untracked, seen by `pending_changes`, and cleaned. A coder-written self-ignoring `json/.gitignore` had hidden a `json` package that shadowed the standard library for every worker.
- 2026-10-06 — **B3c-1 review 17 fix:** `untracked_ignore_files` lists every untracked `.gitignore`, ignored or not (one that hides only its neighbour survived a reset whose `git add -A` failed), and a `.gitignore` is never a gated file (like `.gitattributes`): the coder cannot commit one that would hide future files from every check.
- 2026-10-06 — **B3c-1 review 18 fix:** the `.gitignore` removal and the bytecode purge run as separate steps (removal first) in the reset, on close and in the heal — one purge failing on an over-long path had skipped the removal and left a hidden `json` package behind a successful-looking reset. `_dirty_paths` reports a failing `git status` as dirty, never clean.
- 2026-10-06 — **B3c-1 review 19 fixes:** `remove_untracked_ignores` makes each folder on the way its owner's before unlinking (a hidden package in a folder made read-only had survived close and heal); `review._git` decodes with `surrogateescape` (a latin-1 byte crashed a rebuttal review); a gated name must be valid UTF-8.
- 2026-10-06 — **B3c-1 review 20 fix:** `remove_untracked_ignores` first restores owner access across the tree (`_restore_owner_access`: explicit stack, skips what it cannot open, never raises) — git could not list a folder made unlistable (mode 000/100), so a self-ignoring `.gitignore` inside it went unfound and its package survived close, reset and heal.
- 2026-10-06 — **B3c-1 review 21 fix:** the tree sweep (`_restore_owner_access`) also deletes FIFOs, sockets and devices (no toolkit holds one; a FIFO named `.gitignore` blocked every git call and hung the gate, the close and the heal) and runs at the gate's entry and in `pending_changes`; every git call has a timeout (`_GIT_TIMEOUT_S` = 300 s; `gate._git` likewise), a hang becoming an honest error.
- 2026-10-06 — **B3c-1 review 22 fix:** the tree sweep deletes every nested `.git` below the root (a `.git` file pointing at a crafted git dir hung `git status`); `_dirty_paths` sweeps first and reads a timeout as dirty; every git subprocess in `merge_flow` and `gate` has a timeout, and a git call the runtime times out has its `index.lock` cleared (a stale lock had blocked the reset).
- 2026-10-06 — **B3c-1 review 23 fix:** the review step sweeps the tree before git walks it (a rebuttal review had run on an unswept tree); `gate._clear_index_lock` (shared by `gate._git` and `merge_flow._git_in`) clears the lock of a timed-out call, also in a linked worktree; `review._git` has a timeout; `_added_lines` diffs with `--text` (a file over `core.bigFileThreshold` showed as binary and went unscanned); authoring handlers catch `SubprocessError` (timeouts included) as honest errors.
- 2026-10-06 — **B3c-1 review 24 fix:** `_added_lines` is hunk-aware: a `\ No newline at end of file` marker no longer shifts the line numbers (appending to one of the eight real toolkit files without a final newline left the first added line unscanned), and an added line starting with `++` is counted. Untracked `.gitattributes` files are removed before resets like `.gitignore`; the review's diff uses `--text` and clears a timed-out call's lock.
- 2026-10-06 — **B3c-1 review 25 fix:** the no-fabrication scans count lines as git does (`\n` only; `splitlines` also broke on form feeds, `\r`, U+2028, shifting added text out of scope), and a string constant is in scope when any physical line it spans was added, then scanned whole (a `\n` escape inside an added string let its second part escape). `returns.watched_tools` watches a tool whose source holds a lone CR (spans cannot be mapped to git's lines) and finds tools defined in a package `__init__.py`.
- 2026-10-06 — **B3c-1 review 26 fix:** comments are scanned whole on their git line (a form feed, vertical tab, U+2028 or lone CR inside one had split it and hidden its tail), and bytes literals are scanned like strings. Accepted: whole-string scoping can fail an edit that touches one line of a long pre-existing string holding a banned phrase (none in today's toolkit, measured).
- 2026-10-06 — **B3c-1 review 27 fix:** a lone carriage return in a gated file is a hard failure (`no_fabrication.line_endings`): Python reads it as a line break and git does not, so every AST scan scoped to added lines looked at the wrong lines. The real toolkit holds none.
- 2026-10-06 — **B3c-1 review 28 fix:** the AST scans scope a node by every line it spans (`_touches`), not only its first — an added continuation line (`.random.normal(x)`) or a body replaced by `pass` under an unchanged `def` line escaped; a placeholder body is flagged when its `def` line or its body changed, never because some other line of a long function did.
- 2026-10-06 — **B3c-1 review 29 fix:** a file nested too deeply to parse or walk (a `RecursionError`/`MemoryError` in a scan) is a failed `no_fabrication.syntax` check, and `run_gate_step` also turns those errors into a failed gate; `returns.watched_tools` watches a tool whose source cannot be parsed. Measured: 1,725 one-line edits across every real toolkit function produced no false hard-fail.
- 2026-10-06 — **B3c-1 review 30 fixes:** `returns.watched_tools` watches a key re-pointed to another module or function and a tool whose decorator line was added (its span now starts at the first decorator); a submission `state.json` (in the coder-writable folder) of any shape other than a non-empty list of submission records is ignored, its patch must be a plain file name in its own folder, and `_update_last_submission` can never raise; `gate._git` decodes bytes itself (text mode turned a lone CR into a line break).
- 2026-10-06 — **B3c-1 review 31 fixes:** a source-encoding declaration other than UTF-8 in a gated `.py` hard-fails (`no_fabrication.encoding`; Python decodes by the cookie at import, the scans read UTF-8, and under `utf-7` a comment is code); `returns.watched_tools` watches a tool with no top-level `def` (defined under `if`/`try`); a restored submission's notes accept only a list of issue texts.
- 2026-10-06 — **B3c-1 closed.** Threat model made explicit: the gate defends against an honest coder under pressure (fabrication) and against the gate/commit inconsistencies real tooling produces; deliberate sabotage by a coder crafting artifacts (forged bytecode, FIFOs, nested-repository traps, encoding cookies) is bounded by the jail, the read-only `.git`, the co-lead review and the human merge, and is an accepted residual beyond the guards already built.
- 2026-10-06 — **Env plan B3c-3: the coder's environment tools.** `env_build.solve` / `pin_wheels`; `coder/env_tools.CandidateEnvs` (`env_solve`, `env_pin_wheels`, `cand_env_activate`) on both escalation servers (Claude allow-list `mcp__escalation__env_solve|env_pin_wheels|cand_env_activate`); candidates built into `<worktree_base>/envs_cand/<tool>`, read-only in the author jail (`coder_policy(readonly=)`, `CodexBackend(jail_readonly=)`), `active/bin` first on the author's `PATH` (`_author_env(active_bin=)`), removed at session close. Seed lists environments (`_environment_context`); `prompts/coder.md` gains *Your package's software environment*. Live-verified.
- 2026-10-06 — **B3c-3 review fixes:** candidate wrappers start from a clean environment and stub missing `python`/`pytest`/`pip` (exit 127) rather than inherit the analysis environment; a failed rebuild empties the wrappers; `CandidateEnvs.close()` (first in `aclose`) stops late writes into the reset tree; a new attempt clears stale wrappers; definition writes refuse a linked or outside folder and replace atomically; `SubprocessError` (timeouts) is a tool answer; activations serialised; Codex `tool_timeout_sec = 3600` on the escalation server; the Claude jail overlay is passed explicitly (`_build_options(jail_readonly=)`) and absent when the session has no env tools; `_author_env` keeps the candidate first on `PATH` when sourcing fails. Tool descriptions: a solve rewrites the whole lock; a wheel installs without dependencies.
- 2026-10-06 — **B3c-3 review 2 fixes:** `react._in_environment` moved to `envs.in_environment` (react imports it) and gained `pass_args`; the candidate wrappers are built on it, so they report an activation that exits, restore the jail's `PFILES` half and reset shell state exactly as `use_env` does. The launcher calls `/usr/bin/env` by path (a candidate shipping `env` made every wrapper call itself) and keeps `USER`, `LOGNAME`, `LC_*`, `TZ` and proxies. A derived candidate without its own `activate.sh` sources the base activation. Each attempt gets its own candidate folder; solve, pin and activate share one lock; definition writes use a unique temp name.
- 2026-10-06 — **B3c-3 review 3 fixes:** `in_environment` restores the command's arguments after the activation (`source` shares `"$@"`), names its marker per run (`…-$$`; a wrapper runs concurrently), removes it with `/usr/bin/rm`, and reports a failed or exiting activation once ("did not finish", exit 97). A derived candidate whose base activation lies outside the coder's jail (the human `analyze_ini_sh`, in this repository) sources a copy in the candidate folder; a jailed test runs it.
- 2026-10-06 — **B3c-3 review 4 (clean) lows:** `env_pin_wheels` needs a candidate built from the current `env.lock` and answers a derived definition directly; `prompts/coder.md` asks for absolute paths in `activate.sh` (the runtime sources a copy). Residuals recorded (relative sourcing in a copied base activation; the parent folder on the candidate's `PYTHONPATH`).
- 2026-10-06 — **Env plan B3c-4: build on commit.** `merge_flow.committed_environments` / `build_committed_environments`; `AuthoringSession.commit` builds them after a direct-branch commit (`_build_environments`, ledger `env.build` / `env.build.error`, `env_note` appended to the lead's commit answer). A derived build now writes its record too (with `base`), and `built_environments(..., base_envs)` resolves it against the configured human environment, so derived environments are listed (`env.env_catalogue` passes the analysis environment).
- 2026-10-06 — **B3c-4 review fixes:** a reused build rewrites its record when activation, boot file or externals changed (`_record_fields`); `forget` + `merge_flow.deleted_environments` drop a deleted definition from the catalogue; `StoppableRunner` lets a stop kill a commit build and the candidate tools' conda/pip; `_build_environments` turns any failure into the lead's note.
- 2026-10-06 — **B3c-4 review 2 fixes:** a stop during a commit build kills it and records `env.build.error` ("stopped"; shielded), the launch drift check (B3c-5) building what is missing; `StoppableRunner` reaps with `wait()` after a timeout or error (an escaped child holding the pipes had held the call past its timeout).
- 2026-10-06 — **B3c-4 review 3 fixes:** `gate.check_env_definitions` admits a deleted definition unless a toolkit package still names it (it had refused every deletion, so `forget` never ran); a stopped commit build starts no further environment; the rebuild comment and residuals say a new path lookup sees the new build at once.
- 2026-10-06 — **B3c-4 review 4 (clean) low:** `env.forget` is recorded as each record is dropped, before any build; a failure note names what was removed. Residual: `propose_and_pr` (the standalone measurement path) leaves `env_note` to the ledger.
- 2026-10-06 — **Env plan B3c-5: launch reconcile and drift.** `merge_flow.reconcile_environments` (missing/stale builds, drift kept as `ar-<id>.drift-<fp8>` with its lock saved and rebuilt, external stamp changes recorded and reported); `graph.runtime._reconcile_environments` runs it after preflight with ledger events.
- 2026-10-06 — **B3c-5 review fixes:** a definition that does not validate is `failed` and never treated as drift (an editor swap file had renamed an intact build away); drift also covers distributions installed or removed by hand (baseline `installed_dists` recorded at build); a failed drift rebuild restores the drifted prefix and record; every intact definition goes through `build()` (record refreshed, jail rule applied); the drift lock carries the recorded activation; a `tainted` notice; events name the cause.
- 2026-10-06 — **B3c-5 review 2 (clean) lows:** a stop ends reconcile before the next environment (a stop mid-drift had renamed a prefix and crashed); a current record with its prefix gone is rebuilt with `cause: missing`; an external added to or removed from a definition re-baselines silently; derived environments' externals are stamped and reported too.
- 2026-10-06 — **Env plan B4: replay.** `env_build.replay` (a lock's conda part rebuilt into its own prefix and compared; Anaconda and conda-forge hosts with checksums; reuse; unjailed taint as a build) and `env.replay_environment(fingerprint)` (lock store → replay, recorded activation/boot written beside it, externals compared). Live-verified on a conda-forge env.
- 2026-10-06 — **B4 review (clean) lows:** a changed replay prefix is kept aside, not deleted; url-less or duplicate recorded packages are refused; `replay_environment` refuses a lock that does not hash to its fingerprint and reports the activation's `owner`.
- 2026-10-06 — **Env plan B3c-2: Layer B in the named environment.** `gate._run_layer_b` / `_gate_environment`; `_default_test_runner(environment=)` runs the tests through `envs.in_environment(keep=)`; `_jailed_pytest(extra_ro=)`. `tests/conftest.py` points `agent_conda_dir()` at a temporary folder for every test (a gate test had built into `~/.auto_research`).
- 2026-10-06 — **B3c-2 review fixes:** watched tools per environment group (another environment's tool had failed the recorder's import); gate builds per definition digest with a one-day prune (concurrent gates had shared one prefix); the private `PFILES` folder in the environment path; `TestRunner` and gate_bench's `_tests_off_runner` accept the new keywords.
- 2026-10-06 — **B3c-2 review 2 (clean) lows:** the copied base activation lives in its digest folder and is written atomically; one prune lock; labelled determinism details per group; the two-group test asserts both recorders passed.
- 2026-10-06 — **Env plan B3b: routing.** `ToolkitRegistry.tool_env` + the `env` argument and description line in `tool_definitions`; `PersistentSandbox` REPL pool (`call_tool(environment=)`, `label=`, `call_environment`, pool closed by `restart`/`shutdown`, killed by `abort`/`kill_now`); pipeline activations sourced in the jail by the launcher.
- 2026-10-06 — **B3b review fixes:** a REPL not yet running is started before its call's stamp is taken (a first routed call, and the first after a restart, had gone unstamped); a pipeline environment refuses to start unjailed when the jail is configured; its activation and boot files are bound as files, not their folders; `tests/conftest.py` also points `env_lock_dir()` at the temporary folder.
- 2026-10-06 — **B3b review 2 fixes:** a coder-written boot file under a human activation is bound and refused unjailed like a coder-written activation; a failure or environment-change restart of the worker's own REPL keeps the pool (`_restart_own`); `abort`/`kill_now` iterate a copy of the pool; a provider test over a real jailed REPL pool.
- 2026-10-06 — **B3b review 3 fixes:** a pool REPL's launcher sends the activation's output to stderr (`envs.in_environment(quiet=True)`; a banner had broken the protocol on every call); a REPL that ends unexpectedly attaches its stderr tail to the call's error (`_stderr_tail`: "activation … did not finish").
- 2026-10-06 — **B3b review 4 fixes:** the REPL runs its boot file with fd 1 on stderr (a C-level XSPEC banner had broken the protocol on every call, analysis REPL included); `_stderr_tail` keeps the newest bytes. Residuals: a boot file that raises is swallowed (as before); a write to a REPL that already died reports without the tail.
- 2026-10-07 — **Build jail under a symlinked path (B5 live cell):** `build_policy` and `pin_wheels` bind the staged definition and the candidate prefix as spelled, so the jail recreates the spelling conda and pip are handed; a resolved-only bind left the staged `env.lock` unreachable whenever the conda dir is reached through a link (`/data` → `/nfsdata1` here, which every production runtime dir is), and every candidate build failed in a second. Tests build and pin, jailed, under a symlinked folder outside `/tmp`. `prompts/analyst_lead.md` and `prompts/analyst_worker.md` say when to ask for a package environment.
- 2026-10-07 — **`verify` record caps raised (B5 live cell):** `_MAX_RECORD` 64 MB, `_MAX_RECORDS_TOTAL` 1 GB. A conda record lists every file its package installs; libboost-headers' exceeded the old 4 MB cap and failed every Boost build at `verify`, after its install. Test: a 7 MB record verifies.
- 2026-10-07 — **REPL on Python < 3.11 (B5 live cell):** the REPL no longer passes `-P` (3.11+ only; a Python 3.9 pipeline environment's pool REPL died at launch on it); the script's first statement removes the working folder from `sys.path` instead, on every version. Test: a REPL on Python 3.9 starts and a package planted in its working folder does not shadow the standard library. Residual: the shell server keeps `-P` (its source opens with a `__future__` import, so no prelude), which needs the analysis interpreter at 3.11+ (3.13 today).
- 2026-10-07 — **REPL protocol line limit (B5 live cell):** the REPL subprocess is read with a 256 MB line limit (`_PROTOCOL_LINE_LIMIT`); asyncio's default 64 KB failed every larger tool return. Test: a 1 MB return arrives whole.
- 2026-10-07 — **T1 / B6 docs sweep:** the `PersistentSandbox` entry names the `environment` / pool
  signature and where each environment's boot file comes from. No code change. Jailed probe through
  the module API (`env_build.build` into the runtime conda dir, `PersistentSandbox.call_tool(...,
  environment=ar-xspec)`): `xspec.Model("relxillCp")` builds, so the toolkit's `boot.py` runs in a
  routed REPL (F21 narrowed to the default REPL).
