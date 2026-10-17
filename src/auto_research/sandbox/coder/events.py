"""Monitor emission shared by the authoring flow and every author harness.

Kept apart from both so a harness backend can record into the run ledger without importing the
flow that drives it. Event names are harness-independent on purpose: a run's ledger reads the
same whether the author was the Claude Agent SDK or the Codex app-server, which is what makes
two harnesses comparable in the same campaign.
"""

from __future__ import annotations

from typing import Any


def cap_text(text: str, limit: int) -> str:
    """Trim text for a one-line summary or a model-visible note, stating how much was dropped.

    Ledger data fields are recorded whole (2026-10-17); this bounds only short previews.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit:,} chars]"


async def emit(
    monitor: Any | None,
    event: str,
    *,
    level: int,
    summary: str,
    data: dict[str, Any] | None = None,
    actor: str = "coder",
) -> None:
    """Record one authoring event; logging must never break the authoring flow."""
    if monitor is None:
        return
    try:
        await monitor.record(
            "tooling", event, level=level, summary=summary, actor=actor, data=data or {}
        )
    except Exception:  # noqa: BLE001
        pass
