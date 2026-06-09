"""Launch-time environment validation.

Called at startup before any SDK clients are constructed.  Derives required
keys from the shape documented in .env.example — never reads .env itself.
"""

from __future__ import annotations

import os

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
