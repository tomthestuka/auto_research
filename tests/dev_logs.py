"""Helpers for writing local development test logs."""

from datetime import UTC, datetime
from pathlib import Path


def dev_log_path(step: str, stem: str, suffix: str) -> Path:
    """Return a timestamped path under logs/dev/<step>/."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path("logs") / "dev" / step / f"{timestamp}_{stem}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
