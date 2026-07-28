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
from typing import NamedTuple

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"
_ORCH_CONFIG = _REPO_ROOT / "config" / "orchestration.yaml"

# Each tuple is a group of aliases; at least one must be set.
# Derived from .env.example; optional vars (LangSmith, runtime dir) are excluded.
_REQUIRED: tuple[tuple[str, ...], ...] = (
    ("ANTHROPIC_API_KEY",),
    ("OPENAI_API_KEY",),
    ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    ("ADS_DEV_KEY", "ADS_API_TOKEN"),
    ("TAVILY_API_KEY",),
)


def check_required_env() -> None:
    """Raise RuntimeError listing every missing required env var.

    Each group is satisfied when any alias in that group is set and non-empty.
    Raises on the first call if anything is missing so the process dies before
    constructing any SDK client.
    """
    load_dotenv(_ENV_FILE, override=False)
    missing: list[str] = []
    for group in _REQUIRED:
        if not any(os.environ.get(k) for k in group):
            label = group[0] if len(group) == 1 else f"{group[0]} (or {group[1]})"
            missing.append(label)
    if missing:
        raise RuntimeError(
            "Required environment variables are not set. "
            "Copy .env.example to .env and fill in the values.\n"
            "Missing: " + ", ".join(missing)
        )


def _orch_config() -> dict:
    import yaml  # local import — yaml not needed at module load time

    try:
        with _ORCH_CONFIG.open("r", encoding="utf-8") as f:
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

    Anthropic-only, and BETA. **NOT LIVE TESTED as of 2026-07-22** (the beta path is exercised
    only once Anthropic API credits are restored — see ``tests/test_live_context_editing.py``).
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

    **NOT LIVE TESTED as of 2026-07-27** — ships disabled; flip ``tool_search_enabled`` true
    after the env-gated live tests pass on both providers.
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
