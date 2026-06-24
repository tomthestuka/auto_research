"""Shared filesystem allow-list for every runtime agent (CLAUDE.md §2/§12).

A single, config-driven boundary that every runtime LLM agent's file access is checked
against — the Phase-2 coder (via a Claude Agent SDK ``PreToolUse`` hook, ``merge_flow``) and the
ReAct reviewer/lead/workers (via the ``agents/react.py`` tool providers). The system's own
``auto_research`` repo is **not** on the list, so the runtime can never read its orchestration
code or governance files — the §2/§4 trust boundary, enforced rather than assumed.

The allow-list is the **complete** set of absolute roots in ``config/orchestration.yaml:
agent_fs_allow`` (portability: one list to edit per machine — it must include ``toolkit_root``
and ``runtime_dir`` so agents can read their own workspace, plus the obs-db root, the analyst
conda env, HEASoft ``HEADAS``/``CALDB``, the local PFILES dir, and ``/tmp``). An **empty/unset**
list disables the guard (allow-all) so environments that have not opted in keep working — a
populated list opts into the jail.

Two enforcement helpers:
- :meth:`PathGuard.is_allowed` — exact, for tools whose target path we can see (``Read``/
  ``Write``/``Edit``/``Glob``/``Grep`` and toolkit-tool path args). Also denies the governance
  basenames ``CLAUDE.md``/``DOCS.md``/``AGENTS.md`` even inside an allowed root (§4 — these never
  enter runtime context).
- :meth:`PathGuard.forbidden_in_command` — **best-effort**, for a shell command string: it sees
  only the *named* absolute paths, not the files a subprocess opens (so a legit ``python``/``xspec``
  command's implicit conda/HEASoft reads are invisible — that is fine, those roots are allow-listed
  for *named* paths and for the future OS jail). Complete shell confinement is the deferred
  OS-level jail (§12 / Loose ends).
"""

from __future__ import annotations

import re
from pathlib import Path

# §4: build-time governance files must never enter runtime context — deny them everywhere,
# even inside an allowed root (research_toolkit carries its own CLAUDE.md/DOCS.md).
GOVERNANCE_BASENAMES = frozenset({"CLAUDE.md", "DOCS.md", "AGENTS.md"})

# Absolute-path tokens in a shell command — best-effort (no spaces/quotes handling).
_ABS_PATH_TOKEN = re.compile(r"/[\w./+\-]+")


def load_fs_allowlist(config: dict | None = None) -> list[Path]:
    """Resolved roots from ``orchestration.yaml: agent_fs_allow`` (empty when unset)."""
    if config is None:
        from auto_research.env import _orch_config

        config = _orch_config()
    roots = config.get("agent_fs_allow") or []
    return [Path(str(r)).expanduser().resolve() for r in roots]


class PathGuard:
    """Decide whether a filesystem path / shell command stays within the allow-list.

    ``roots`` is the allow-list (resolved on construction). An **empty** ``roots`` means the
    guard is **inactive** (everything allowed) so un-configured environments are unaffected.
    """

    def __init__(self, roots) -> None:
        self.roots = [Path(r).resolve() for r in roots]

    @classmethod
    def from_config(cls, config: dict | None = None) -> "PathGuard":
        return cls(load_fs_allowlist(config))

    @property
    def active(self) -> bool:
        return bool(self.roots)

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

    def forbidden_in_command(self, command: str) -> str | None:
        """Return the first absolute-path token in ``command`` that is outside the allow-list
        (or names a governance file), else ``None``. Best-effort — see module docstring. When
        the guard is inactive, always ``None``."""
        if not self.roots or not command:
            return None
        for tok in _ABS_PATH_TOKEN.findall(command):
            tok = tok.rstrip(".,;:")  # trailing punctuation isn't part of the path
            if not self.is_allowed(tok):
                return tok
        return None
