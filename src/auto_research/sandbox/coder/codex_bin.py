"""Locate the Codex CLI the Codex author harness drives.

The SDK talks JSON-RPC to a ``codex app-server`` subprocess, so the binary is a real dependency
of the harness. The SDK ships a version-matched one as a hard dependency
(``openai-codex-cli-bin``) and resolves it itself, which is the arrangement we want: SDK and
app-server share a protocol, and a CLI from a different release can move that protocol underneath
the client.

So the harness does not pin a path — it lets the SDK resolve its own binary. This module exists
for the launch check, and for the escape hatch of pointing at a specific build through
``CODEX_BIN``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def configured_codex_bin() -> str | None:
    """An explicitly chosen binary, or ``None`` to let the SDK resolve its own."""
    explicit = os.environ.get("CODEX_BIN")
    if not explicit:
        return None
    if Path(explicit).is_file() and os.access(explicit, os.X_OK):
        return explicit
    raise ValueError(f"$CODEX_BIN points at {explicit!r}, which is not an executable file.")


def resolve_codex_bin() -> str:
    """Return the Codex CLI path the harness would use, or raise naming the fix.

    Resolution follows the SDK's own, so the check verifies what a run will actually launch:
    an explicit ``CODEX_BIN`` first, then the SDK's resolver (which finds the binary shipped
    beside it), then ``PATH``.
    """
    explicit = configured_codex_bin()
    if explicit:
        return explicit

    try:
        from openai_codex.client import CodexConfig, _resolve_codex_bin

        return str(_resolve_codex_bin(CodexConfig()))
    except Exception:  # noqa: BLE001 — fall through to PATH and report from there
        pass

    found = shutil.which("codex")
    if found:
        return found

    raise ValueError(
        "the Codex CLI was not found: neither the `openai-codex` SDK's own bundled binary nor a "
        "`codex` on PATH. Both are pinned in environment.yml; run "
        "`conda env update -n auto_research -f environment.yml`, or set $CODEX_BIN to an existing "
        "build."
    )
