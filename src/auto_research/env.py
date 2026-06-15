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


def runtime_data_dir() -> Path:
    """Return the runtime data directory from config/orchestration.yaml.

    Key: ``runtime_dir``.  Defaults to ``~/.auto_research`` when absent.
    """
    import yaml  # local import — yaml not needed at module load time

    try:
        with _ORCH_CONFIG.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        config = {}
    return Path(config.get("runtime_dir", "~/.auto_research")).expanduser()
