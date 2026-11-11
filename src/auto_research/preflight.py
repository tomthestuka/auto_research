"""Launch-time deterministic checklist.

Every launch precondition runs here, once, at the very start of a run, so a misconfigured run dies
cheaply instead of after burning tokens. All but one are decided **without an LLM**; the last,
``member_media`` (Z1e), sends each configured chat member one tiny image and one tiny PDF, its cost
on the ledger (the ``.env`` shape, the author↔reviewer provider decorrelation,
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
from pathlib import Path
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


#: ``orchestration.yaml`` keys that retired, each with what to do instead (no aliases, D16).
RETIRED_KEYS: dict[str, str] = {
    "agent_os_jail": "is retired (every runtime process runs in the jail); delete the key",
    "mcp_servers": "is retired (the workers' shell is the jailed builtin server); delete the key",
    "toolkit_root": "is retired; set toolkits[].root",
    "tool_author_branch": "is retired; set toolkits[].authoring_branch",
    "analyze_env": "is retired; declare the base in human_envs and name toolkits[].default_env",
    "analyze_ini_sh": "is retired; write the default environment's envs/<id>/activate.sh in the toolkit",
    "repl_init_py": "is retired; write the default environment's envs/<id>/boot.py in the toolkit",
    "analyze_probe": "is retired; write the default environment's envs/<id>/env.yaml probe",
    "gate_readonly_roots": "is retired; list the data sets under test_data",
}


def check_config_keys() -> None:
    """``orchestration.yaml`` holds no retired key (each named with what to do instead) and an
    ``agent_fs_allow`` that lists roots narrower than the filesystem: the jail mounts that list and
    the runtime's own reads are confined to it, so an empty list (read as allow-all) or one naming
    ``/`` is refused."""
    from auto_research.env import _orch_config

    config = _orch_config()
    problems = [f"`{key}` {RETIRED_KEYS[key]}" for key in RETIRED_KEYS if key in config]
    allow = config.get("agent_fs_allow") or []
    if not allow:
        problems.append("list in `agent_fs_allow` the roots runtime processes may reach")
    elif any(Path(str(entry)).expanduser().resolve() == Path("/") for entry in allow):
        problems.append("name in `agent_fs_allow` the folders runtime processes need, in place of `/`")
    if problems:
        raise RuntimeError("orchestration.yaml: " + "; ".join(problems))


def check_private_variables() -> None:
    """Every ``env_externals.<name>.private`` entry is well formed (``env.private_variables``) and
    the toolkit's default environment's activation (sourced as its processes source it,
    ``runner.analysis_activation_env``) sets the variable with its separator, so the folder is
    replaced as declared; a misspelt or mismatched declaration fails at launch, not silently."""
    from auto_research.env import _orch_config, private_variables
    from auto_research.sandbox.runner import analysis_activation_env

    config = _orch_config()
    private = private_variables(config)
    if not private:
        return
    activated = analysis_activation_env(config)
    owner = {var: name for name, spec in (config.get("env_externals") or {}).items()
             for var in ((spec or {}).get("private") or {})}
    problems = [
        f"env_externals.{owner[var]}.private: {var}: the default environment's activation "
        + (f"sets it to {activated[var]!r}, without the separator {sep!r}" if var in activated else "leaves it unset")
        + "; declare the variable and separator the activation sets"
        for var, sep in private.items() if sep not in activated.get(var, "")
    ]
    if problems:
        raise RuntimeError("; ".join(problems))


def build_default_preflight(human_handler: Any | None = None) -> Preflight:
    """Assemble the default checklist by registering each module's own check.

    Imports are local so this module stays import-light and the check functions keep
    living next to what they validate (CLAUDE.md §4 modular ownership). When a
    ``human_handler`` is resolved (non-headless), the launch-time channel ping is added so
    a run starts only if the human-response channel actually replies (§9.8).
    """
    from auto_research.env import check_human_envs, check_required_env
    from auto_research.graph.checkpoint import check_checkpointer_available
    from auto_research.models.capabilities import check_member_capabilities
    from auto_research.models.media_probe import check_member_media
    from auto_research.models.keys import check_api_key_fallbacks
    from auto_research.models.providers._doc_parse import check_document_parser_ready
    from auto_research.sandbox.merge_flow import check_toolkit_branch_clean, check_toolkit_git_identity
    from auto_research.sandbox.coder import (
        check_coder_harness_available,
        check_coder_harness_supported,
    )
    from auto_research.sandbox.jail import (
        check_os_jail_available,
        check_os_jail_harness,
    )
    from auto_research.sandbox.registry import check_toolkit_manifests
    from auto_research.sandbox.review import check_author_reviewer_decorrelation
    from auto_research.sandbox.runner import check_analysis_probe
    from auto_research.storage import check_database_health
    from auto_research.store.health import (
        check_store_admin,
        check_store_buckets,
        check_store_catalog,
        check_store_config,
        check_store_servers,
    )
    from auto_research.store.reconcile import check_store_integrity
    from auto_research.store.ci import check_ci_leftovers
    from auto_research.toolkit_contract import check_toolkit_contract, check_toolkit_test_data

    preflight = Preflight()
    preflight.register("config_keys", check_config_keys)
    preflight.register("required_env", check_required_env)
    preflight.register("api_key_fallbacks", check_api_key_fallbacks)
    preflight.register("member_capabilities", check_member_capabilities)
    preflight.register("document_parser_ready", check_document_parser_ready)
    preflight.register("checkpointer_available", check_checkpointer_available)
    preflight.register("database_health", check_database_health)
    preflight.register("author_reviewer_decorrelation", check_author_reviewer_decorrelation)
    preflight.register("coder_harness_supported", check_coder_harness_supported)
    preflight.register("coder_harness_available", check_coder_harness_available)
    preflight.register("os_jail_available", check_os_jail_available)
    # The analysis probe (below) execs the default environment's interpreter; this execs the
    # author's own CLI. The first jailed sweep passed the probe and lost every trial to an unbound
    # harness binary (2026-08-20).
    preflight.register("os_jail_harness", check_os_jail_harness)
    preflight.register("toolkit_contract", check_toolkit_contract)
    preflight.register("toolkit_test_data", check_toolkit_test_data)
    preflight.register("toolkit_git_identity", check_toolkit_git_identity)
    preflight.register("toolkit_branch_clean", check_toolkit_branch_clean)
    preflight.register("toolkit_manifests", check_toolkit_manifests)
    # The data stores (step 0b): a local store's servers are started here, then each is checked.
    preflight.register("store_config", check_store_config)
    preflight.register("store_servers", check_store_servers)
    preflight.register("store_admin", check_store_admin)
    preflight.register("store_buckets", check_store_buckets)
    preflight.register("store_catalog", check_store_catalog)
    preflight.register("store_integrity", check_store_integrity)
    preflight.register("store_ci_leftovers", check_ci_leftovers)  # a notice, never a failure
    # The toolkit's environments, after its contract: human bases declared, then the default
    # environment's activation (private variables) and probe, both run as a unit's REPL starts.
    preflight.register("human_envs", check_human_envs)
    preflight.register("private_variables", check_private_variables)
    preflight.register("analysis_probe", check_analysis_probe)
    # The one paid check (Z1e): one tiny image and one tiny PDF per distinct chat member, through the
    # real call. Last, so a launch that fails a free check spends nothing.
    preflight.register("member_media", check_member_media)
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
