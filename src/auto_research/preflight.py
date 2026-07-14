"""Launch-time deterministic checklist.

Every check that can be decided **without an LLM** runs here, once, at the very start of a
run — before any model client is constructed — so a misconfigured run dies cheaply instead
of after burning tokens (the ``.env`` shape, the author↔reviewer provider decorrelation,
…). This is the single place the run's preconditions are asserted.

Design: a check is a zero-arg function that **raises on failure** and lives in its *own*
module (next to the thing it validates) — ``env.check_required_env``,
``sandbox.review.check_author_reviewer_decorrelation``, etc. ``build_default_preflight``
registers them into a :class:`Preflight`; ``run_checklist`` runs them all and aggregates
every failure into one error (so a single launch reports *all* misconfigurations, not just
the first). Adding a future check is one ``register(...)`` line — the loop never changes.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

# A check raises on failure (its exception message is surfaced) and returns None on success.
# It may be sync or async (the human-channel ping awaits a real reply).
CheckFn = Callable[[], "None | Awaitable[None]"]


class PreflightError(RuntimeError):
    """Raised by :meth:`Preflight.run_checklist` when one or more checks failed.

    The message aggregates every failed check so a single launch surfaces all
    misconfigurations at once.
    """


class Preflight:
    """An ordered registry of launch-time checks, run together by :meth:`run_checklist`."""

    def __init__(self) -> None:
        self._checks: list[tuple[str, CheckFn]] = []

    def register(self, name: str, check: CheckFn) -> CheckFn:
        """Register ``check`` under ``name`` (usable as a decorator); returns ``check``."""
        self._checks.append((name, check))
        return check

    def names(self) -> list[str]:
        """The registered check names, in run order."""
        return [name for name, _ in self._checks]

    async def run_checklist(self) -> None:
        """Run every registered check, aggregating failures into one ``PreflightError``.

        Each check is run independently so one failure never masks another; if any failed,
        the collected messages are raised together. Sync checks are called directly; async
        ones (e.g. the human-channel ping that awaits a real reply) are awaited.

        One progress line is printed per check (the project has no logging framework and the
        preflight runs before the monitor exists): ``[preflight] <name> …`` before, then
        ``ok``/``FAILED`` after. The in-flight ``…`` makes a blocking check legible — most
        usefully the ``human_channel`` ping, which would otherwise hang silently waiting for
        a human reply.
        """
        failures: list[str] = []
        for name, check in self._checks:
            print(f"  [preflight] {name} …", end="", flush=True)
            try:
                result = check()
                if inspect.isawaitable(result):
                    await result
                print(" ok", flush=True)
            except Exception as exc:  # noqa: BLE001 — any failure is a launch-blocking fault
                print(" FAILED", flush=True)
                failures.append(f"[{name}] {exc}")
        if failures:
            raise PreflightError(
                "Launch preflight failed — fix these before the run can start:\n"
                + "\n".join(f"  - {f}" for f in failures)
            )


def build_default_preflight(human_handler: Any | None = None) -> Preflight:
    """Assemble the default checklist by registering each module's own check.

    Imports are local so this module stays import-light and the check functions keep
    living next to what they validate (CLAUDE.md §4 modular ownership). When a
    ``human_handler`` is resolved (non-headless), the launch-time channel ping is added so
    a run starts only if the human-response channel actually replies (§9.8).
    """
    from auto_research.env import check_required_env
    from auto_research.observations import check_catalog_health
    from auto_research.sandbox.merge_flow import (
        check_toolkit_branch_clean,
        check_toolkit_git_identity,
        check_toolkit_root_configured,
    )
    from auto_research.sandbox.review import check_author_reviewer_decorrelation
    from auto_research.sandbox.runner import check_analyze_probe
    from auto_research.storage import check_database_health

    preflight = Preflight()
    preflight.register("required_env", check_required_env)
    preflight.register("database_health", check_database_health)
    preflight.register("author_reviewer_decorrelation", check_author_reviewer_decorrelation)
    preflight.register("toolkit_root_configured", check_toolkit_root_configured)
    preflight.register("toolkit_git_identity", check_toolkit_git_identity)
    preflight.register("toolkit_branch_clean", check_toolkit_branch_clean)
    preflight.register("catalog_health", check_catalog_health)
    preflight.register("analyze_probe", check_analyze_probe)
    if human_handler is not None:
        from auto_research.hitl import make_human_channel_check

        preflight.register("human_channel", make_human_channel_check(human_handler))
    return preflight


async def run_preflight(*, human_handler: Any | None = None) -> None:
    """Build the default checklist and run it — the launch-time entry point."""
    await build_default_preflight(human_handler=human_handler).run_checklist()
