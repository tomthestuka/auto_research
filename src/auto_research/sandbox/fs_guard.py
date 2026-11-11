"""Shared filesystem allow-list for every runtime agent (CLAUDE.md §2/§12).

A single, config-driven boundary for the file access that runs **in the runtime process**, where
no OS jail reaches: the ReAct roles' ``read_file``/``list_dir``/``file_edit`` (the
``agents/react.py`` tool providers) and which paths may be receipted (``provenance.capture``).
Every subprocess (a REPL, the shell, an author harness, the gate's tests, a build) runs inside the
OS jail (``sandbox/jail.py``), which binds the same list. The system's own ``auto_research`` repo
is **not** on the list, so the runtime can never read its orchestration code or governance files —
the §2/§4 trust boundary, enforced rather than assumed.

The allow-list is the **complete** set of absolute roots in ``config/orchestration.yaml:
agent_fs_allow`` (portability: one list to edit per machine — it must include ``toolkit_root``
and ``runtime_dir`` so agents can read their own workspace, plus the analyst
conda env, HEASoft ``HEADAS``/``CALDB``, the folders of declared private variables, and ``/tmp``). An **empty/unset**
list disables the guard (allow-all) so environments that have not opted in keep working — a
populated list opts into the guard.

:meth:`PathGuard.is_allowed` (reads) and :meth:`PathGuard.is_writable` (writes) are exact, for a
target path the runtime sees. Both deny the governance basenames ``CLAUDE.md``/``DOCS.md``/
``AGENTS.md`` even inside an allowed root (§4 — these never enter runtime context).
"""

from __future__ import annotations

from pathlib import Path

# §4: build-time governance files must never enter runtime context — deny them everywhere,
# even inside an allowed root (research_toolkit carries its own CLAUDE.md/DOCS.md).
GOVERNANCE_BASENAMES = frozenset({"CLAUDE.md", "DOCS.md", "AGENTS.md"})


def load_fs_allowlist(config: dict | None = None) -> list[Path]:
    """Resolved roots from ``orchestration.yaml: agent_fs_allow`` (empty when unset)."""
    if config is None:
        from auto_research.env import _orch_config

        config = _orch_config()
    roots = config.get("agent_fs_allow") or []
    return [Path(str(r)).expanduser().resolve() for r in roots]


class PathGuard:
    """Decide whether a filesystem path stays within the allow-list.

    ``roots`` is the **readable** allow-list (resolved on construction). An **empty** ``roots``
    means the guard is **inactive** (everything allowed) so un-configured environments are
    unaffected.

    ``writable`` is the separate, narrower set a role may **write** to (F10, 2026-09-09). The
    guard used to make no read/write distinction at all, which was fine only while every graph
    role was read-only — F5e gives the reviewer `file_edit` on its own `report.tex`, and once one
    formerly read-only role becomes a writer, "read-only role" stops being the boundary and *its
    own work product* becomes it. Human ruling: **the only writable folder for a role is its own
    work folder.** Empty ``writable`` means this guard grants no write at all, which is the right
    default for every role that does not need one.

    The model of the two sets is asymmetric on purpose, and `jail.coder_policy` is the worked
    precedent: the author harness reads everything on the allow-list and writes only its worktree
    and its own home. Reads stay broad because a role must be able to read its own run logs
    (human ruling 2026-09-03, `runtime_dir` stays readable); writes are what F10 narrows.
    """

    def __init__(self, roots, writable=()) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.writable = [Path(w).resolve() for w in writable]

    @classmethod
    def from_config(cls, config: dict | None = None, writable=()) -> "PathGuard":
        return cls(load_fs_allowlist(config), writable=writable)

    @property
    def active(self) -> bool:
        return bool(self.roots)

    def is_writable(self, path) -> bool:
        """True if ``path`` may be **written** by this role.

        Independent of :meth:`is_allowed`: a role reads its run directory and writes one folder
        inside it. A role with no declared writable set can write nothing, and an inactive guard
        (no roots at all, i.e. unconfigured) grants writes the same way it grants reads.
        """
        if not self.roots:
            return True
        if not self.writable:
            return False
        try:
            p = Path(str(path)).expanduser().resolve()
        except Exception:  # noqa: BLE001 — an unresolvable path is not writable
            return False
        if p.name in GOVERNANCE_BASENAMES:
            return False
        return any(p == root or root in p.parents for root in self.writable)

    def is_allowed(self, path) -> bool:
        """True if ``path`` is inside an allowed root and is not a governance file. When the
        guard is inactive (no roots), always True."""
        if not self.roots:
            return True
        try:
            p = Path(str(path)).expanduser().resolve()
        except Exception:  # noqa: BLE001 — an unresolvable path is not allowed
            return False
        if p.name in GOVERNANCE_BASENAMES:
            return False
        return any(p == root or root in p.parents for root in self.roots)
