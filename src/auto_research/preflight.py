"""Launch-time deterministic checklist.

Every check that can be decided **without an LLM** runs here, once, at the very start of a
run — before any model client is constructed — so a misconfigured run dies cheaply instead
of after burning tokens (the ``.env`` shape, the author↔reviewer provider decorrelation,
…). This is the single place the run's preconditions are asserted.

Design: a check is a zero-arg function that **raises on failure** and lives in its *own*
module (next to the thing it validates). A check may also raise :class:`PreflightWarning` for a
precondition that is satisfiable but unproven — the launch continues and the caution is recorded — ``env.check_required_env``,
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


class PreflightWarning(RuntimeError):
    """Raised *by a check* to report a caution that must not block the launch.

    Some preconditions are satisfiable but unproven — a configuration this project has never
    exercised live, for instance. Blocking such a run would be wrong (it may well work) and
    passing silently would be worse (a first-time failure then looks like a defect in the flow).
    So a check raises this instead: the launch continues, the line reads ``WARNING``, and the
    caution lands on the run ledger where a later reader of the record can see it was known in
    advance.
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

    async def run_checklist(self, *, monitor: Any | None = None) -> None:
        """Run every registered check, aggregating failures into one ``PreflightError``.

        Each check is run independently so one failure never masks another; if any failed,
        the collected messages are raised together. Sync checks are called directly; async
        ones (e.g. the human-channel ping that awaits a real reply) are awaited.

        One progress line is printed per check: ``[preflight] <name> …`` before, then
        ``ok``/``FAILED`` after. The in-flight ``…`` makes a blocking check legible — most
        usefully the ``human_channel`` ping, which would otherwise hang silently waiting for
        a human reply.

        When ``monitor`` is supplied every check is **also** recorded onto the run ledger, so
        the launch state becomes part of the run's own record rather than terminal scrollback
        a detached or notebook run never sees. Levels are chosen so the significant view stays
        readable: each passing check is level 5 (detail), a failure is level 1 (it kills the
        launch), and one level-3 tally closes the checklist out either way.
        """
        failures: list[str] = []
        warnings: list[str] = []
        for name, check in self._checks:
            print(f"  [preflight] {name} …", end="", flush=True)
            try:
                result = check()
                if inspect.isawaitable(result):
                    await result
                print(" ok", flush=True)
                await _record(monitor, f"preflight.{name}", level=5,
                              summary=f"Preflight {name}: ok", data={"check": name, "ok": True})
            except PreflightWarning as warning:
                # Satisfiable but unproven: the run proceeds and the caution is recorded, so a
                # later failure is read against something that was flagged, not as a surprise.
                print(f" WARNING: {warning}", flush=True)
                warnings.append(f"[{name}] {warning}")
                await _record(monitor, f"preflight.{name}", level=2,
                              summary=f"Preflight {name}: WARNING — {warning}",
                              data={"check": name, "ok": True, "warning": str(warning)})
            except Exception as exc:  # noqa: BLE001 — any failure is a launch-blocking fault
                print(" FAILED", flush=True)
                failures.append(f"[{name}] {exc}")
                await _record(monitor, f"preflight.{name}", level=1,
                              summary=f"Preflight {name}: FAILED",
                              data={"check": name, "ok": False, "error": str(exc)})
        passed = len(self._checks) - len(failures)
        tally = f"Preflight: {passed}/{len(self._checks)} checks passed"
        if warnings:
            tally += f", {len(warnings)} warning(s)"
        await _record(
            monitor, "preflight.summary", level=3, summary=tally,
            data={"passed": passed, "total": len(self._checks),
                  "failed": [f.split("]")[0].lstrip("[") for f in failures],
                  "warnings": warnings},
        )
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
    from auto_research.graph.checkpoint import check_checkpointer_available
    from auto_research.models.keys import check_api_key_fallbacks
    from auto_research.observations import check_catalog_health
    from auto_research.sandbox.merge_flow import (
        check_toolkit_branch_clean,
        check_toolkit_git_identity,
        check_toolkit_root_configured,
    )
    from auto_research.sandbox.coder import (
        check_coder_harness_available,
        check_coder_harness_supported,
    )
    from auto_research.sandbox.jail import (
        check_os_jail_available,
        check_os_jail_harness,
        check_os_jail_probe,
    )
    from auto_research.sandbox.review import check_author_reviewer_decorrelation
    from auto_research.sandbox.runner import check_analyze_probe
    from auto_research.storage import check_database_health

    preflight = Preflight()
    preflight.register("required_env", check_required_env)
    preflight.register("api_key_fallbacks", check_api_key_fallbacks)
    preflight.register("checkpointer_available", check_checkpointer_available)
    preflight.register("database_health", check_database_health)
    preflight.register("author_reviewer_decorrelation", check_author_reviewer_decorrelation)
    preflight.register("coder_harness_supported", check_coder_harness_supported)
    preflight.register("coder_harness_available", check_coder_harness_available)
    preflight.register("os_jail_available", check_os_jail_available)
    preflight.register("os_jail_probe", check_os_jail_probe)
    # The probe execs the analysis interpreter; this execs the author's own CLI. The first jailed
    # sweep passed the probe and lost every trial to an unbound harness binary (2026-08-20).
    preflight.register("os_jail_harness", check_os_jail_harness)
    preflight.register("toolkit_root_configured", check_toolkit_root_configured)
    preflight.register("toolkit_git_identity", check_toolkit_git_identity)
    preflight.register("toolkit_branch_clean", check_toolkit_branch_clean)
    preflight.register("catalog_health", check_catalog_health)
    preflight.register("analyze_probe", check_analyze_probe)
    if human_handler is not None:
        from auto_research.hitl import make_human_channel_check

        preflight.register("human_channel", make_human_channel_check(human_handler))
    return preflight


async def _record(monitor: Any | None, event: str, *, level: int, summary: str, data: dict) -> None:
    """Record a preflight event when a monitor is present; never let logging break a launch.

    Guarded because preflight's job is to decide whether the run may start — a monitor fault
    (disk full, a stub without ``record``) must not become the thing that stops it. Mirrors
    the ``RunMonitor.record`` guard rationale in [J2].
    """
    if monitor is None:
        return
    try:
        await monitor.record("run", event, level=level, summary=summary, data=data)
    except Exception:  # noqa: BLE001 — observability never blocks the launch decision
        pass


async def run_preflight(*, human_handler: Any | None = None, monitor: Any | None = None) -> None:
    """Build the default checklist and run it — the launch-time entry point.

    ``monitor`` is optional so the standalone callers (``setup.py``'s smoke test, an ad-hoc
    check) keep working unchanged; ``run_research`` passes its live monitor so the launch
    checklist lands in the run ledger.
    """
    await build_default_preflight(human_handler=human_handler).run_checklist(monitor=monitor)
