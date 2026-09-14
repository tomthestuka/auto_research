"""LangGraph checkpointer wiring for durable mid-run resume (§12).

A run compiles its graph with an ``AsyncSqliteSaver`` writing to ``checkpoint.sqlite`` inside
the run's own dir, keyed by the deterministic ``run_id`` as ``thread_id``. LangGraph snapshots
``ResearchState`` after every node (with ``durability="sync"`` the snapshot is flushed before
the next node starts), so a process death loses at most the in-flight node, which re-runs on
resume; everything the finished nodes produced (verdicts, iteration counts, committed tools,
written report artifacts) is restored.

The sqlite saver ships in ``langgraph-checkpoint-sqlite`` (pip; not on conda-forge). Real runs
**require** it — :func:`check_checkpointer_available` is a launch preflight so a missing dep
fails cheaply and loudly, never a silent non-durable run. Tests inject an in-memory saver
(``langgraph.checkpoint.memory.MemorySaver``) instead, so the deterministic suite needs no pip
package.

This module deliberately imports nothing from ``auto_research.graph`` (builder/runtime) so the
preflight can import the availability check without pulling the graph stack.
"""

from __future__ import annotations

from pathlib import Path

_INSTALL_HINT = (
    "durable resume needs 'langgraph-checkpoint-sqlite' (pip; not on conda-forge). Install "
    "it with:  conda run -n auto_research pip install 'langgraph-checkpoint-sqlite>=3.1'  "
    "(or `conda env update -n auto_research -f environment.yml`)."
)


def check_checkpointer_available() -> None:
    """Preflight check: the async sqlite saver must import (real runs are always durable).

    Raises ``RuntimeError`` with an install hint when the pip package is absent, so a
    misconfigured run fails at launch before any model spend rather than running un-durable.
    """
    try:
        import langgraph.checkpoint.sqlite.aio  # noqa: F401
    except Exception as exc:  # noqa: BLE001 — any import failure blocks the launch loudly
        raise RuntimeError(_INSTALL_HINT) from exc


def checkpoint_path(run_directory: Path) -> Path:
    """The run's checkpoint DB path — a sibling of ``backend.pid`` / ``monitor.jsonl``."""
    run_directory.mkdir(parents=True, exist_ok=True)
    return run_directory / "checkpoint.sqlite"


def open_checkpointer(path: Path):
    """Return the ``AsyncSqliteSaver`` async context manager for ``path`` (lazy import).

    Usage::

        async with open_checkpointer(path) as saver:
            graph = build_graph(..., checkpointer=saver)
            ...

    Raises ``RuntimeError`` with an install hint if the pip package is absent — real runs
    never silently drop durability (the launch preflight normally catches this first).
    """
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_INSTALL_HINT) from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(path))


__all__ = ["check_checkpointer_available", "checkpoint_path", "open_checkpointer"]
