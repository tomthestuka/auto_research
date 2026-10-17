"""Launch-time environment validation and config-derived paths.

``check_required_env()`` is called at startup before any SDK clients are
constructed. It loads the project ``.env`` via python-dotenv, then validates
required keys derived from .env.example shape.

``runtime_data_dir()`` reads the runtime data directory from
config/orchestration.yaml so the path is not duplicated across env and config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, NamedTuple

from dotenv import load_dotenv

from auto_research.config_paths import models_yaml, orchestration_yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The secrets file stays with the repository whatever a run is configured with: an API key is a
# property of the machine, not of the experiment, and copying one into a per-run directory would
# multiply the number of places a key can be left behind.
_ENV_FILE = _REPO_ROOT / ".env"

# Each tuple is a group of aliases; at least one must be set. These are the keys every run needs
# whatever models it is configured with; the model keys are derived from `models.yaml` instead
# (``_model_key_groups``), so a run requires exactly the vendors it actually calls.
_REQUIRED: tuple[tuple[str, ...], ...] = (
    ("ADS_DEV_KEY", "ADS_API_TOKEN"),
    ("TAVILY_API_KEY",),
)

# Additional spellings a vendor also accepts, beyond its standard `<PROVIDER>_API_KEY`. Only what
# cannot be derived lives here — see `provider_key_aliases`.
_PROVIDER_EXTRA_KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "google": ("GEMINI_API_KEY",),
}


def provider_key_aliases(provider: str) -> tuple[str, ...]:
    """The env variables that satisfy ``provider``, its standard name first.

    Used when a member does not name a `key_env` explicitly. A member's own `key_env` always
    wins, which is how a harness points at a plan-specific endpoint with a differently-named
    variable.

    The standard name is **derived by the same function the key pool and the author harnesses
    use** (`models.keys.env_var_names`), not restated in a table here. A table that merely repeats
    a format rule is a place for two readers to disagree, and on 2026-09-12 they did: this module
    derived `DEEPSEEK_API_KEY` for a coder member carrying no `key_env`, while `CodexBackend`
    derived nothing — so the launch check passed on exactly the variable the author never
    received, and every Codex turn 401'd. One deriver, so that cannot recur.

    An unrecognised provider string yields nothing, as before: the roles' YAML is read raw here,
    so a name no adapter implements must not conjure a requirement out of a format rule.
    """
    from typing import get_args

    from auto_research.models.keys import env_var_names
    from auto_research.models.types import ProviderName

    if provider not in get_args(ProviderName):
        return ()
    return (env_var_names(provider)[0], *_PROVIDER_EXTRA_KEY_ALIASES.get(provider, ()))

# Kept so a config that cannot be read still demands the historical set rather than nothing.
_FALLBACK_MODEL_KEYS: tuple[tuple[str, ...], ...] = (
    ("ANTHROPIC_API_KEY",),
    ("OPENAI_API_KEY",),
    ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
)


def _model_key_groups() -> tuple[tuple[tuple[str, ...], ...], dict[tuple[str, ...], str]]:
    """The key variables ``config/models.yaml`` implies, with who needs each.

    A run calls exactly the members its roles name, so requiring a key for a provider nobody is
    configured with fails a perfectly good launch, and *not* requiring one a member does name
    (the vendor keys the coder harnesses use, `key_env`) lets the run die later inside a
    subprocess instead of at launch. Both directions are wrong in the same way: the requirement
    belongs to the configuration, not to a list written once.

    Returns the alias groups to require and, for each, a human-readable note naming the roles
    and members that need it — a missing key is much easier to fix when the message says which
    member wanted it.
    """
    import yaml  # local import — yaml is not needed at module load time

    try:
        with models_yaml().open("r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return _FALLBACK_MODEL_KEYS, {}

    roles = config.get("roles")
    if not isinstance(roles, dict):
        return _FALLBACK_MODEL_KEYS, {}

    wanted: dict[tuple[str, ...], list[str]] = {}
    for role, group in roles.items():
        members = (group or {}).get("members") if isinstance(group, dict) else None
        for member in members or []:
            if not isinstance(member, dict):
                continue
            explicit = member.get("key_env")
            aliases = (
                (str(explicit),)
                if explicit
                else provider_key_aliases(str(member.get("provider") or ""))
            )
            if not aliases:
                continue
            label = f"{role}/{member.get('name') or member.get('model') or '?'}"
            wanted.setdefault(aliases, []).append(label)

    if not wanted:
        return _FALLBACK_MODEL_KEYS, {}
    notes = {aliases: ", ".join(sorted(set(users))) for aliases, users in wanted.items()}
    return tuple(wanted), notes


def check_required_env() -> None:
    """Raise RuntimeError listing every missing required env var.

    Two sources: the keys every run needs (literature sources), and the key variables
    ``config/models.yaml`` implies — one per provider a role is configured with, or the exact
    variable a member names with ``key_env``. Each group is satisfied when any alias in it is set
    and non-empty. Raises before any SDK client is constructed, and names the member that wanted
    each missing key.
    """
    load_dotenv(_ENV_FILE, override=False)
    model_groups, notes = _model_key_groups()
    missing: list[str] = []
    for group in (*_REQUIRED, *model_groups):
        if any(os.environ.get(key) for key in group):
            continue
        label = group[0] if len(group) == 1 else f"{group[0]} (or {group[1]})"
        used_by = notes.get(group)
        missing.append(f"{label} [{used_by}]" if used_by else label)
    if missing:
        raise RuntimeError(
            "Required environment variables are not set. "
            "Copy .env.example to .env and fill in the values.\n"
            "Missing: " + ", ".join(missing)
        )


def _orch_config() -> dict:
    import yaml  # local import — yaml not needed at module load time

    try:
        with orchestration_yaml().open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def runtime_data_dir() -> Path:
    """Return the runtime data directory from config/orchestration.yaml.

    Key: ``runtime_dir``.  Defaults to ``~/.auto_research`` when absent.
    """
    return Path(_orch_config().get("runtime_dir", "~/.auto_research")).expanduser()


def papers_dir() -> Path:
    """Return the paper artifact directory from config/orchestration.yaml.

    Key: ``papers_dir``.  Defaults to ``<runtime_dir>/papers`` when absent.
    """
    cfg = _orch_config()
    if "papers_dir" in cfg:
        return Path(cfg["papers_dir"]).expanduser()
    return runtime_data_dir() / "papers"


def toolkit_root() -> Path:
    """Return the ``research_toolkit`` checkout from config/orchestration.yaml.

    Key: ``toolkit_root``. Empty when absent (callers that need it to import a
    toolkit read — e.g. the in-process ``catalog_utils`` data-catalog read — handle
    an unreadable root gracefully).
    """
    return Path(_orch_config().get("toolkit_root", "")).expanduser()


class ProviderRetryPolicy(NamedTuple):
    """Retry budget for transient LLM-provider failures (``provider_retry_*`` keys)."""

    max_retries: int
    base_cooldown_s: float
    max_cooldown_s: float


_RETRY_DEFAULTS = ProviderRetryPolicy(max_retries=8, base_cooldown_s=60.0, max_cooldown_s=600.0)


def provider_retry_policy() -> ProviderRetryPolicy:
    """Transient-provider retry policy from config/orchestration.yaml.

    Keys (the "Internet stability" section): ``provider_retry_max`` (default 8),
    ``provider_retry_base_cooldown_s`` (default 60), ``provider_retry_max_cooldown_s``
    (default 600). A missing or malformed value falls back per-field, so an older
    config keeps working.
    """
    cfg = _orch_config()

    def _read(key: str, fallback, cast):
        try:
            return cast(cfg.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return ProviderRetryPolicy(
        max_retries=_read("provider_retry_max", _RETRY_DEFAULTS.max_retries, int),
        base_cooldown_s=_read(
            "provider_retry_base_cooldown_s", _RETRY_DEFAULTS.base_cooldown_s, float
        ),
        max_cooldown_s=_read(
            "provider_retry_max_cooldown_s", _RETRY_DEFAULTS.max_cooldown_s, float
        ),
    )


class ContextEditingConfig(NamedTuple):
    """Anthropic provider-native context-editing settings (``context_mgmt_*`` keys).

    The ``clear_tool_uses`` strategy clears the *oldest tool RESULTS* server-side once a
    request crosses ``trigger_tokens`` input tokens, keeping the last ``keep_tool_uses``
    tool results verbatim and clearing at least ``clear_at_least_tokens`` per pass so the
    (unavoidable) prompt-cache invalidation is amortized over enough reclaimed tokens to be
    worth it. It is a context-window pressure valve for long ReAct loops, not a cost lever —
    with prompt caching, raw history is already cheap and any clear breaks the cached prefix.

    Anthropic-only, and BETA. **Live-verified 2026-07-28** (``tests/test_live_context_editing.py``);
    re-run that whenever the dated beta identifiers move.
    """

    enabled: bool
    beta_header: str
    strategy: str
    trigger_tokens: int
    clear_at_least_tokens: int
    keep_tool_uses: int


_CONTEXT_EDITING_DEFAULTS = ContextEditingConfig(
    enabled=False,
    beta_header="context-management-2025-06-27",
    strategy="clear_tool_uses_20250919",
    trigger_tokens=120000,
    clear_at_least_tokens=20000,
    keep_tool_uses=3,
)


def context_editing_config() -> ContextEditingConfig:
    """Anthropic context-editing policy from config/orchestration.yaml.

    Keys: ``context_mgmt_enabled`` (default False — a config lacking the key keeps the
    non-beta path, byte-identical to before), ``context_mgmt_trigger_tokens`` (default
    120000), ``context_mgmt_clear_at_least_tokens`` (default 20000), ``context_mgmt_keep_tool_uses``
    (default 3). The protocol strings ``context_mgmt_beta_header`` and ``context_mgmt_strategy``
    are also overridable here (they carry Anthropic's dated beta identifiers, updatable without
    code when Anthropic bumps them) but default in code and stay out of the sample config to
    keep it clean. Missing/malformed values fall back per-field, so an older config keeps working.
    """
    cfg = _orch_config()

    def _read(key: str, fallback, cast):
        try:
            return cast(cfg.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return ContextEditingConfig(
        enabled=bool(cfg.get("context_mgmt_enabled", _CONTEXT_EDITING_DEFAULTS.enabled)),
        beta_header=_read(
            "context_mgmt_beta_header", _CONTEXT_EDITING_DEFAULTS.beta_header, str
        ),
        strategy=_read("context_mgmt_strategy", _CONTEXT_EDITING_DEFAULTS.strategy, str),
        trigger_tokens=_read(
            "context_mgmt_trigger_tokens", _CONTEXT_EDITING_DEFAULTS.trigger_tokens, int
        ),
        clear_at_least_tokens=_read(
            "context_mgmt_clear_at_least_tokens",
            _CONTEXT_EDITING_DEFAULTS.clear_at_least_tokens,
            int,
        ),
        keep_tool_uses=_read(
            "context_mgmt_keep_tool_uses", _CONTEXT_EDITING_DEFAULTS.keep_tool_uses, int
        ),
    )


class ToolSearchConfig(NamedTuple):
    """Provider-native tool-search / deferred-tool-loading policy.

    Toolkit tool definitions are withheld from the model's context until it discovers them
    through the provider's server-side search tool, which appends the definition inline in
    the conversation **tail** and so leaves the cached prefix intact. Harness tools stay
    resident (at least one non-deferred tool is required by both providers).

    Anthropic (``tool_search_tool_bm25_20251119``) hides a deferred tool entirely; OpenAI
    (``tool_search``, gpt-5.4+) keeps its name and description resident and withholds only
    the parameter schema. Google has no equivalent and keeps today's fully-loaded behaviour.

    BM25 over embeddings is deliberate: the injected tool catalogue already gives the model
    the exact tool name, so search runs as an exact-name lookup rather than the semantic
    recall regime tool-retrieval benchmarks show models failing at.

    **Live-verified 2026-07-28 on both providers** (``tests/test_live_tool_search.py``): resident
    input fell 19,074 → 3,789 tokens on OpenAI and 35,208 → 9,003 on Anthropic against the real
    manifest, the model searched rather than stalling, and an expanded definition lands inside
    the conversation cache breakpoint (so it is cache-read on every later turn). Ships enabled.
    """

    enabled: bool
    anthropic_tool: str
    openai_tool: str


_TOOL_SEARCH_DEFAULTS = ToolSearchConfig(
    enabled=False,
    anthropic_tool="tool_search_tool_bm25_20251119",
    openai_tool="tool_search",
)


def tool_search_config() -> ToolSearchConfig:
    """Tool-search policy from config/orchestration.yaml.

    Keys: ``tool_search_enabled`` (default False — a config lacking the key keeps every tool
    resident, byte-identical to before). The provider tool-type strings
    ``tool_search_anthropic_tool`` / ``tool_search_openai_tool`` carry dated provider
    identifiers and are overridable without a code change; they default in code and stay out
    of the sample config to keep it clean. Missing/malformed values fall back per-field.
    """
    cfg = _orch_config()

    def _read(key: str, fallback, cast):
        try:
            return cast(cfg.get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    return ToolSearchConfig(
        enabled=bool(cfg.get("tool_search_enabled", _TOOL_SEARCH_DEFAULTS.enabled)),
        anthropic_tool=_read(
            "tool_search_anthropic_tool", _TOOL_SEARCH_DEFAULTS.anthropic_tool, str
        ),
        openai_tool=_read("tool_search_openai_tool", _TOOL_SEARCH_DEFAULTS.openai_tool, str),
    )


def react_grace_rounds() -> int:
    """Rounds a role gets to summarise after its ReAct budget runs out (``react_grace_rounds``).

    CLAUDE.md §2b: exhausting a cap is not an exit. A loop that hits its budget is not finished —
    it is out of time — so it is told so and given a bounded window to compose and file. Failing
    to file inside that window is **fatal to the run**, because the alternative is the silent
    unfiled return that discarded a completed 14-quantity reproduction (2026-09-15) and that the
    ledger could not distinguish from a role never asked to run.

    Applies only where a terminal tool exists. A loop with no terminal tool (the librarian, the
    co-lead's review, ``drive_authoring``) has no exit to force and its exhaustion is a
    legitimate end, so it keeps the old behaviour.
    """
    cfg = _orch_config()
    try:
        value = int(cfg.get("react_grace_rounds", 20))
    except (TypeError, ValueError):
        return 20
    return value if value >= 0 else 20


def coder_wait_limits_s() -> tuple[int, int, int]:
    """``(floor, default, max)`` seconds for the lead's blocking ``wait_coder``.

    Keys ``coder_wait_floor_s`` / ``coder_wait_default_s`` / ``coder_wait_max_s``. A requested
    timeout below the floor is RAISED to it rather than refused: a refusal comes back at once and
    costs the lead a model turn, which is how a poll limit only moved the spam onto other tools
    (slot 6, 2026-09-15: 114 refused polls, 47 mid-turn steers and 31 refused submits at a 3-10 s
    cadence). This is the shape Codex's ``wait_agent`` uses (clamp up to a minimum, error only
    above the maximum). Read at call time, so a config edit takes effect on the next wait.

    A malformed value falls back to its default; the three are then ordered so that
    ``floor <= default <= max`` always holds.
    """
    cfg = _orch_config()

    def _read(key: str, fallback: int) -> int:
        try:
            value = int(cfg.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    floor = _read("coder_wait_floor_s", 60)
    maximum = max(_read("coder_wait_max_s", 1800), floor)
    default = min(max(_read("coder_wait_default_s", 300), floor), maximum)
    return floor, default, maximum


def sandbox_call_timeout_default() -> int:
    """Global default sandbox per-tool-call timeout (seconds) from config/orchestration.yaml.

    Key: ``sandbox_call_timeout_s`` (default 300). This is the protective short cap a tool
    call is SIGTERM-killed at when nothing else raises it; a package's ``tool_config.yaml``
    overrides it per tool (see ``auto_research.sandbox.registry``).
    """
    try:
        return int(_orch_config().get("sandbox_call_timeout_s", 300))
    except (TypeError, ValueError):
        return 300


def analysis_environment(config: dict | None = None) -> "Environment | None":
    """The configured analysis environment as one object (handoff [F18], env plan B1).

    Today's human environment: ``analyze_env`` (the conda prefix), ``analyze_ini_sh`` (its
    activation), ``repl_init_py`` (the REPL boot file) and ``env_externals`` (software outside conda
    the activation points at, stamped by the files listed). ``None`` when no prefix is configured.
    Every reader of those keys builds its process from this object.
    """
    from auto_research.sandbox.envs import Environment

    config = config if config is not None else _orch_config()
    prefix = config.get("analyze_env")
    if not prefix:
        return None
    externals = tuple(declared_externals(config).values())
    activate = config.get("analyze_ini_sh") or None
    boot = config.get("repl_init_py") or None
    return Environment(
        name=Path(str(prefix)).name, prefix=Path(str(prefix)).expanduser(), owner="human",
        activate=Path(str(activate)).expanduser() if activate else None,
        boot=Path(str(boot)).expanduser() if boot else None,
        externals=externals,
    )


def declared_externals(config: dict | None = None) -> "dict[str, External]":
    """The software outside conda the human declares (``env_externals``), by name."""
    from auto_research.sandbox.envs import External

    config = config if config is not None else _orch_config()
    return {
        str(name): External(name=str(name), root=Path(str(spec.get("root", ""))).expanduser(),
                            stamps=tuple(str(s) for s in (spec.get("stamps") or ())))
        for name, spec in (config.get("env_externals") or {}).items()
    }


def env_catalogue(config: dict | None = None) -> "list[Environment]":
    """Every environment a unit may list and use: the configured analysis environment (human) and
    each environment the coding pipeline has built (``agent_conda_dir()``), externals resolved from
    the human's ``env_externals``."""
    from auto_research.sandbox.env_build import built_environments

    config = config if config is not None else _orch_config()
    analysis = analysis_environment(config)
    built = built_environments(agent_conda_dir(), declared_externals(config),
                               {analysis.name: analysis} if analysis else {})
    return ([analysis] if analysis else []) + [env for env in built if not analysis or env.name != analysis.name]


def host_environment() -> "Environment":
    """The orchestrator's own environment: where in-process tools (harness tools, the lead's
    in-process toolkit calls) run. No activation, no externals."""
    import sys

    from auto_research.sandbox.envs import Environment

    return Environment(name=Path(sys.prefix).name, prefix=Path(sys.prefix), owner="human")


def env_lock_dir() -> Path:
    """Where environment locks live: under the runtime dir, outside every agent's reach."""
    return runtime_data_dir() / "conda" / "locks"


def agent_conda_dir() -> Path:
    """Where pipeline-built environments live (``envs/``), with their build records (``built/``)
    and staging: under the runtime dir, apart from the human's own envs and package cache."""
    return runtime_data_dir() / "conda"


def conda_executable() -> str:
    """The conda binary the runtime builds with: config ``conda_exe``, else ``$CONDA_EXE``, else
    the one on PATH."""
    import shutil

    configured = _orch_config().get("conda_exe") or os.environ.get("CONDA_EXE") or shutil.which("conda")
    if not configured:
        raise RuntimeError("no conda executable: set conda_exe in orchestration.yaml or CONDA_EXE")
    return str(configured)


def replay_environment(fingerprint: str, *, runner: Any = None) -> dict:
    """Rebuild the environment state a receipt stamps (its ``env`` fingerprint) from the lock store,
    into a prefix of its own under ``agent_conda_dir()/replay/`` (env plan B4), jailed when the jail
    is on. The report (:func:`sandbox.env_build.replay`) adds each recorded external's state now
    (``same``, ``changed``, ``undeclared``: an external cannot be rebuilt) and writes the recorded
    activation and boot file beside the prefix (``<prefix>.activate.sh`` / ``.boot.py``); ``owner``
    says whose they are (a pipeline environment's are coder-written: source them in a jail). A lock
    that does not hash to the fingerprint it is filed under is refused."""
    import json
    import re
    import subprocess

    from auto_research.sandbox.env_build import replay
    from auto_research.sandbox.envs import _external_stamp
    from auto_research.sandbox.jail import jail_enabled

    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint or ""):
        raise ValueError("give the full 64-character fingerprint a receipt stamps")
    path = env_lock_dir() / f"{fingerprint}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no lock for {fingerprint[:12]} in {env_lock_dir()}")
    import hashlib

    lock = json.loads(path.read_text(encoding="utf-8"))
    components = lock.get("components") if isinstance(lock, dict) else None
    if (not isinstance(components, dict) or lock.get("fingerprint") != fingerprint
            or hashlib.sha256(json.dumps(components, sort_keys=True).encode("utf-8")).hexdigest() != fingerprint):
        raise ValueError(f"the lock filed under {fingerprint[:12]} does not hash to it; it is not that state")
    report = replay(lock, conda_dir=agent_conda_dir(), conda_exe=conda_executable(),
                    jailed=jail_enabled(), runner=runner or subprocess.run)
    current = declared_externals()
    report["externals"] = {
        name: ("undeclared" if name not in current else
               "same" if _external_stamp(current[name]) == recorded else "changed")
        for name, recorded in (lock.get("externals") or {}).items()
    }
    report["owner"] = lock.get("owner")  # "pipeline": the activation is coder-written; source it jailed
    for key, suffix in (("activation_script", ".activate.sh"), ("boot_file", ".boot.py")):
        if isinstance(lock.get(key), str):
            target = Path(report["prefix"] + suffix)
            target.write_text(lock[key], encoding="utf-8")
            report[key] = str(target)
    return report
