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
