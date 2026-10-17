"""Tool-authoring harness — Phase 2 step 2 (CLAUDE.md §9.3/§9.4/§9.9).

``propose_and_pr`` spawns a Sonnet coding sub-agent (the Claude Agent SDK) that writes
both toolkit halves (``lib/`` human-half + ``skills/TOOL_MANIFEST`` ai-half) and tests in
an **isolated git worktree** on ``research_toolkit``, then drives the bounded rework loop:

    L0  author self-correction (inside the SDK session, ``max_turns``)
    L1  escalation when blocked — web search · message(to=lead|human) (an independent path
        to the human, so the lead is never the sole oracle)
    L2  deterministic gate rejection → re-drive the warm session (``gate_rework_cap``)
    L3  provider-decorrelated co-lead review rejection → re-drive (``review_rework_cap``)
    L4  cap exhausted → no commit; return failed/needs_human so the lead raises to the
        human for instructions (§9.6/§9.7) — never fabricate.

The orchestration is **code, never the LLM**. The deterministic gate is real as of step 3
(``gate.run_gate``, the default ``run_gate`` hook); the provider-decorrelated co-lead review
is real as of step 4 (``review.make_review_hook``, the default ``run_review`` hook). All
hooks remain injectable so the loop is exercised deterministically in tests. ``approve_edit``
— the only place git commits — is real as of step 6: on gate-pass ∧ review-pass it stages
**exactly** the files the gate verified (``gate.collect_targets``, explicit-path, never
``git add -A``) and lands one atomic commit on the working branch, whose subject is the
co-lead's ``commit_summary``. The runtime never touches master; a human merges out of band.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from auto_research.config_paths import orchestration_yaml
from auto_research.agents.prompts import load_prompt
from auto_research.hitl import HumanHandler, HumanInputRequired, resolve_human
from auto_research.models.types import RoleModelConfig
from auto_research.sandbox.coder.claude_sdk import (  # noqa: F401 — re-exported: the Claude
    ClaudeSdkBackend,                                # harness's own wires live in that module,
    _build_options,                                  # and callers/tests still import them here
    _default_sdk_client_factory,
    _drive_author,
    _make_escalation_server,
    _make_fs_guard_hook,
    _make_path_confiner,
    _record_coder_cost,
    _with_live_anthropic_key,
)
from auto_research.sandbox.coder.events import cap_text, emit
from auto_research.sandbox.gate import _clear_index_lock
from auto_research.schemas import GateReport, HumanQuery, MergeFlowResult, ToolReviewVerdict, ToolSpec

# Type aliases for the injectable hooks (stubs in step 2; real impls in steps 3/4).
GateHook = Callable[[Path, ToolSpec], Awaitable[GateReport]]
ReviewHook = Callable[[Path, ToolSpec, GateReport], Awaitable[ToolReviewVerdict]]

# ``HumanInputRequired`` and ``HumanHandler`` are the §9.8 raise-to-human types — now owned
# by ``auto_research.hitl`` and re-exported here for back-compat (the author's ask_human
# path used them first; ``tests/test_merge_flow.py`` imports the name from here).


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _load_orchestration(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def _orchestration_config() -> dict:
    """Load and cache the configured ``orchestration.yaml`` (best-effort).

    Cached **on the resolved path** rather than on nothing: an override is a per-process decision
    set before the process starts, so in a real run the key never changes, but keying on it means a
    test that points the process elsewhere is not served the previous directory's answer.
    """
    return _load_orchestration(orchestration_yaml())


def _cap(key: str, default: int) -> int:
    return int(_orchestration_config().get(key, default))


def _resolve_toolkit_root() -> Path:
    """The ``research_toolkit`` checkout the runtime commits authored tools into (§9.9)."""
    return Path(_orchestration_config().get("toolkit_root", "")).expanduser()


def check_toolkit_root_configured() -> None:
    """Launch-time preflight (no LLM): assert ``toolkit_root`` is configured and is a directory.

    Every runtime path hint derives from ``config/orchestration.yaml: toolkit_root`` — the
    coder's structure context (its cwd + the template layout), the reviewer/lead read-only
    roots, and the ``ToolkitRegistry`` discovery root. A missing or wrong key would send
    agents hunting for the checkout (and silently mask the fault behind a stale default), so
    we fail loudly at launch with a precise message. Runs before ``check_toolkit_git_identity``
    so a misconfigured root reports as such rather than as a confusing "not a git checkout".
    Registered in ``auto_research.preflight``.
    """
    raw = str(_orchestration_config().get("toolkit_root", "")).strip()
    if not raw:
        raise RuntimeError(
            "toolkit_root is not set in config/orchestration.yaml — set it to the "
            "research_toolkit checkout path (where the runtime authors tools)."
        )
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise RuntimeError(
            f"toolkit_root {str(root)!r} (config/orchestration.yaml) is not a directory — "
            "set it to the research_toolkit checkout path."
        )


def check_toolkit_git_identity() -> None:
    """Launch-time preflight (no LLM): assert ``research_toolkit`` has a git commit identity.

    ``approve_edit`` relies on the **ambient** git identity (CLAUDE.md §9.9). If ``user.name``
    or ``user.email`` is unset there, the runtime's atomic commit would fail mid-flow — so we
    fail loudly at launch with a precise remediation instead. Registered in
    ``auto_research.preflight``.
    """
    root = _resolve_toolkit_root()
    if not (root / ".git").exists() and not root.is_dir():
        raise RuntimeError(
            f"toolkit_root {root!r} (config/orchestration.yaml) is not a git checkout; "
            "the runtime cannot commit authored tools there."
        )
    missing = [
        key
        for key in ("user.name", "user.email")
        if not subprocess.run(
            ["git", "-C", str(root), "config", "--get", key],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        ).stdout.strip()
    ]
    if missing:
        raise RuntimeError(
            f"research_toolkit ({root}) has no git commit identity ({', '.join(missing)} unset). "
            f"Set it so runtime tool commits don't fail, e.g.:\n"
            f"  git -C {root} config user.name 'auto_research'\n"
            f"  git -C {root} config user.email 'auto-research@localhost'"
        )


def _resolve_tool_author_branch() -> str:
    """The branch the runtime authors tools on directly (CLAUDE.md §9.9; default ``ai``)."""
    return str(_orchestration_config().get("tool_author_branch", "ai"))


def _current_branch(root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        ).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""  # unknown: never the authoring branch


def _dirty_paths(root: Path) -> list[str]:
    """The paths making ``root`` dirty, or empty when it is clean.

    Named rather than counted (N4): the unclean-branch error used to print remediation commands
    and no paths, so slot 5's dirtying file (2026-09-08) is **permanently undetermined** — the
    trial's clone was swept at teardown and the only record said "has uncommitted changes".

    ``-uall`` for the same reason: plain ``--porcelain`` collapses a new directory to its own
    name, so an untracked file inside one would still not be identified (this is why
    ``gate.collect_all_changes`` uses it too).
    """
    _restore_owner_access(root)  # a FIFO or a nested-repository trap would hang the status
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, errors="surrogateescape", timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _clear_index_lock(root)
        return ["<git status timed out>"]
    if proc.returncode != 0:  # a status git could not produce is never a clean tree
        return [f"<git status failed: {proc.stderr.strip()[:200]}>"]
    return [line[3:].strip() for line in proc.stdout.splitlines() if line.strip()]


def _is_dirty(root: Path) -> bool:
    return bool(_dirty_paths(root))


class ToolkitBranchUnclean(RuntimeError):
    """The authoring branch has uncommitted changes when a clean, committable tree was required.

    A ``RuntimeError`` subclass so the launch preflight still aggregates it normally, but a
    distinct type so the **mid-run** authoring path can catch it specifically and degrade to an
    honest §9.6 terminal instead of crashing the run (a generic ``except RuntimeError`` would
    mask real bugs). At resume time the branch is healed first (``heal_toolkit_branch_if_dirty``)
    so this should not fire; it is the defense-in-depth for a tree dirtied mid-run.
    """


def check_toolkit_branch_clean(root: Path | None = None, branch: str | None = None) -> None:
    """Launch-time preflight (no LLM): assert ``research_toolkit`` is on the authoring branch
    and has a clean working tree (CLAUDE.md §9.9, step 7).

    Direct-branch authoring commits straight onto ``branch`` (default ``ai``) in
    ``toolkit_root``. A wrong branch would commit authored tools to the wrong place. A dirty
    tree **at launch** means a prior run was interrupted before its in-run auto-reset could run
    (``propose_and_pr`` resets a non-committed attempt back to HEAD — §9.9) or a human left
    edits; either way the human inspects/cleans before the next run, lest unrelated changes get
    swept into the next atomic commit. Tolerant when ``root`` isn't a git checkout (mirrors
    :func:`check_toolkit_git_identity`) so it only bites a real configured run. Prints precise
    remediation so the aggregated preflight error is actionable.
    """
    root = Path(root) if root is not None else _resolve_toolkit_root()
    branch = branch or _resolve_tool_author_branch()
    if not (root / ".git").exists():
        return  # not a git checkout — check_toolkit_git_identity already reports this
    current = _current_branch(root)
    if current != branch:
        raise RuntimeError(
            f"research_toolkit ({root}) is on branch {current!r}, not the authoring branch "
            f"{branch!r}. Switch before running, e.g.:\n  git -C {root} checkout {branch}"
        )
    if _is_dirty(root):
        raise ToolkitBranchUnclean(
            f"research_toolkit ({root}) has uncommitted changes on {branch!r}. A prior tool "
            f"authoring may have failed terminally and left edits for you to inspect (§9.9).\n"
            f"Uncommitted paths:\n" + "".join(f"  {p}\n" for p in _dirty_paths(root))
            + f"Review and clean before the next run, or re-run with resume=True to auto-heal:\n"
            f"  git -C {root} status\n"
            f"  git -C {root} restore . && git -C {root} clean -fd   # discard, if appropriate"
        )


def _resolve_coder_model() -> str:
    """The model id of the configured ``coder`` member (no hardcoded id, §6).

    A convenience over :func:`~auto_research.sandbox.coder.harness.coder_member` for callers that
    want only the id. The authoring flow itself carries the whole member, so the model, the
    provider that decides review routing, and the harness that spawns the author can never come
    from three separate config reads that disagree.
    """
    from auto_research.sandbox.coder.harness import coder_member

    return coder_member().model


# ----------------------------------------------------------------------------
# Structure context — runtime-safe only (real toolkit source + config), never DOCS.md
# ----------------------------------------------------------------------------


def _build_structure_context(workdir: Path) -> str:
    """Assemble the local-structure context the author is seeded with.

    Runtime-safe sources only (CLAUDE.md §4): the real ``xspec_utils`` template layout +
    its top-level source, plus the configured mission observation paths. Never reads any
    ``DOCS.md``/``CLAUDE.md``.

    ``workdir`` is the author's actual working directory (its cwd) — the ``research_toolkit``
    checkout it edits (``toolkit_root`` itself in direct-branch mode, the worktree otherwise).
    Its absolute path is stated up front and every template path is printed **absolute**, so
    the author never has to guess the toolkit root or hunt for the validation data: it used to
    receive relative paths with no root stated and burned turns probing wrong roots
    (``/root/…`` → EACCES → ``find /``). The mission observation paths are the absolute
    locations of the validation ObsIDs the tool's tests run against.
    """
    workdir = Path(workdir)
    parts: list[str] = [
        f"# Working directory (your cwd): {workdir}\n"
        "This absolute path is the research_toolkit checkout you edit; every tool package "
        "lives directly under it, and the paths below are absolute — use them verbatim."
    ]
    template = workdir / "xspec_utils"
    if template.is_dir():
        tree = sorted(
            str(p)
            for p in template.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
        parts.append("# Template package layout (mirror xspec_utils exactly):\n" + "\n".join(tree))
        for rel in ("xspec_utils/__init__.py", "xspec_utils/skills/__init__.py"):
            src = workdir / rel
            if src.is_file():
                parts.append(f"# {src}\n```python\n{src.read_text(encoding='utf-8')}\n```")

    obs = _observation_paths()
    if obs:
        lines = "\n".join(f"- {mission}: {path}" for mission, path in obs.items())
        parts.append(
            "# Mission observation paths — your validation ObsIDs live under these absolute "
            "directories (synthetic fixtures are marked `synth…` ObsIDs, never toolkit code):\n"
            + lines
        )
    environments = _environment_context(workdir)
    if environments:
        parts.append(environments)
    return "\n\n".join(parts)


def _environment_context(workdir: Path) -> str:
    """The environments a coder can name or layer on (env plan B3c-3): each package's ``env:``, the
    definitions under ``envs/``, the human environments a definition may name as ``base`` and the
    declared externals. Names only, from the tree and config."""
    from auto_research.sandbox.gate import _package_env

    named = {
        package.name: env for package in sorted(workdir.glob("*_utils"))
        if package.is_dir() and (env := _package_env(package))
    }
    definitions = sorted(p.name for p in (workdir / "envs").glob("*") if p.is_dir()) \
        if (workdir / "envs").is_dir() else []
    try:
        from auto_research.env import analysis_environment, declared_externals

        human = analysis_environment()
        externals = sorted(declared_externals())
    except Exception:  # noqa: BLE001 — names for the seed only; a config gap leaves them out
        human, externals = None, []
    lines = [f"- package {pkg}: env `{env}`" for pkg, env in named.items()]
    lines.append(f"- definitions under envs/: {', '.join(definitions) or 'none yet'}")
    if human is not None:
        lines.append(f"- human environment a definition may use as `base`: {human.name}")
    if externals:
        lines.append(f"- declared externals: {', '.join(externals)}")
    return "# Software environments (see your system prompt's environment section):\n" + "\n".join(lines)


def _observation_paths() -> dict[str, str]:
    try:
        from auto_research.observations import load_observations_config

        cfg = load_observations_config()
    except Exception:
        return {}
    return {name: str(m.obs_db_path) for name, m in cfg.missions.items()}


# ----------------------------------------------------------------------------
# Monitor emit helpers — make the authoring flow (coder stream, escalations, gate/
# review/commit) legible in the run's JSONL. Defined in ``coder.events`` so an author
# harness records under the same names without importing the flow that drives it.
# ----------------------------------------------------------------------------

_cap_text = cap_text
_emit = emit


# ----------------------------------------------------------------------------
# Escalation handlers (L1) — directly unit-testable, wrapped as MCP tools below
# ----------------------------------------------------------------------------


def _make_ask_lead_handler(
    session_ref: Callable[[], Any], *, lead: Any | None = None, monitor: Any | None = None
) -> Callable[..., Awaitable[str]]:
    """The coder's ``message(to="lead")``: post the text to the lead's inbox and return at once.

    **Non-blocking by design.** It used to call ``lead.clarify_for_author``, which opened a
    SECOND ReAct conversation on the lead — running concurrently with the lead's own turn, on the
    same shared tool provider, with none of the context of the brief it was answering about.
    Measured live 2026-09-12: 100 clarification tool calls interleaved with 76 cycle tool calls,
    an answer 326 s later, and a coder that had timed out and moved on.

    Now nobody waits on anybody. The text lands in the lead's inbox (E12) — the same queue every
    text a seat receives travels, drained at the lead's next safe boundary — and wakes a lead
    parked in ``wait_for_reply``. The session marks the question open, so the lead's
    ``drive_coder`` answer steers the turn the coder is still running.
    """

    async def handler(message: str, *, kind: str = "clarify_code") -> str:
        session = session_ref()
        if session is None or lead is None:
            return "The lead is not reachable from this session."
        control = lead.ensure_run_control()
        key = lead.agent_key
        if getattr(session, "_steer_event", None) is None and hasattr(session, "watch_steers"):
            session.watch_steers(control.steer_event(key))
        control.add_steer(key, message, sender="coder", kind="message")
        session.mark_question_open()
        await _emit(
            monitor, "coder.ask_lead", level=4,
            summary=f"coder asked the lead: {_cap_text(message, 80)}",
            data={"kind": kind, "message": message},
        )
        return "delivered. The lead reads it at its next step; its answer reaches you in your turn."

    return handler


# What the author is told when it raises to a human on an unattended run. Reached as a tool
# ERROR, never as an answer: `resolve_human` raising is what MCP turns into an `isError` result,
# so nothing a human did not say is ever recorded as a human reply (no `.answer` event fires).
# Deliberately domain-neutral — the toolkit is not tied to one field, so this names channels
# rather than kinds of document (cf. CLAUDE.md §4 on runtime prompts).
UNATTENDED_GUIDANCE = (
    "No attending human on this run — this question reaches nobody, so ground the decision "
    "yourself and keep going.\n"
    "1. SEARCH THE WEB FIRST, and search it hard: this is the strongest resource you have here. "
    "Look for how others have solved this, for worked examples, and for the authoritative "
    "documentation of the library, format, tool or instrument involved. Read the primary source "
    "rather than a summary of it, and search again with different wording before concluding "
    "nothing exists.\n"
    "2. Ask the analyst lead with `message(to=\"lead\")` for the domain or convention call — the lead is "
    "reachable and knows this codebase.\n"
    "3. Reason to the best answer the evidence supports, and state the assumption you made — "
    "in the code, and in the test that covers it.\n"
    "When it stays genuinely unresolvable after all three, say so plainly and stop."
)


def _make_ask_human_handler(
    human_handler: HumanHandler | None,
    *,
    session_ref: Callable[[], Any] | None = None,
    lead: Any | None = None,
    model: str = "",
    monitor: Any | None = None,
) -> Callable[..., Awaitable[str]]:
    """Serve the coder's ``message(to="human")``: send it, return at once, deliver the reply later.

    Non-blocking like every other text (E12). The question travels the run's seat router (the
    lead's, or one made for this session), and the human's answer reaches the coder through
    ``session.send`` — steered into the running turn, or as its next turn when it has stopped.

    **Unattended** (no handler wired, the §9.8 headless fail-safe) is still classified HERE,
    before the MCP boundary flattens every exception to ``str(exc)``: the author is told what to
    do instead (``UNATTENDED_GUIDANCE``), ledger event ``coder.ask_human.unattended``, and the call
    stays a tool **error**, so nothing a human did not say is ever recorded as a reply. A channel
    fault now arrives later, as a notice delivered the same way an answer would be.
    """
    async def handler(message: str, *, kind: str = "blocked") -> str:
        from auto_research.hitl.router import SeatRouter

        query = HumanQuery(
            kind=kind if kind in ("clarify_science", "clarify_code", "blocked") else "blocked",
            message=message or "(no message provided)",
            asked_by="coder",
            model=model or "",
        )
        await _emit(
            monitor, "coder.ask_human", level=3,
            summary=f"coder raised to the human ({query.kind}): {_cap_text(message, 80)}",
            data={"kind": query.kind, "message": message},
        )
        router = getattr(lead, "channel", None) if lead is not None else None
        if router is None or router.human_handler is None:
            from auto_research.run_control import RunControl

            control = lead.ensure_run_control() if lead is not None else RunControl()
            router = SeatRouter(control, human_handler, monitor=monitor)

        async def deliver(text: str, _handles: tuple[str, ...], entry_kind: str) -> None:
            session = session_ref() if session_ref is not None else None
            label = "[Reply from the human]" if entry_kind == "reply" else "[Notice]"
            if entry_kind == "reply":
                await _emit(
                    monitor, "coder.ask_human.answer", level=3,
                    summary="human answered the coder", data={"answer": text},
                )
            if session is not None and hasattr(session, "send"):
                await session.send(f"{label} {text}", mid_turn=True)

        try:
            router.ask_human(query, deliver=deliver)
        except HumanInputRequired as exc:
            await _emit(
                monitor, "coder.ask_human.unattended", level=3,
                summary="coder raised to a human on an unattended run; told to ground it itself",
                data={"kind": query.kind, "headline": query.headline},
            )
            raise HumanInputRequired(UNATTENDED_GUIDANCE) from exc
        return (
            "Sent to the human. Keep working: the reply reaches you inside your turn, or as your "
            "next turn if you have stopped."
        )

    return handler




# ----------------------------------------------------------------------------
# Worktree + SDK session (the I/O edges — injectable for tests)
# ----------------------------------------------------------------------------


def _create_worktree(toolkit_root: Path, worktree_base: Path, branch: str) -> Path:
    """Create an isolated git worktree on a new branch (runtime git — §9.9, not §3).

    This is application code the runtime executes on ``research_toolkit``; it never
    touches master and never runs in the ``auto_research`` repo.
    """
    worktree_base.mkdir(parents=True, exist_ok=True)
    worktree_path = worktree_base / branch.replace("/", "_")
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=str(toolkit_root),
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )
    return worktree_path


#: Longest one git call may take before it is an error (a toolkit tree is small; a hang is not work).
_GIT_TIMEOUT_S = 300


def _git_in(worktree: Path, *args: str) -> str:
    """Run one git command inside ``worktree`` (runtime git — §9.9, not §3). Raises on
    non-zero exit with stderr in the message."""
    # surrogateescape: a file holding a byte that is not UTF-8 shows up raw in a diff; strict
    # decoding stopped the failure reset before it cleaned the tree (B3c-1 review 12). A timeout:
    # a git call that hangs is an error, never a run that never ends.
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(worktree), capture_output=True, text=True,
            errors="surrogateescape", timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        _clear_index_lock(Path(worktree))  # the killed git cannot release it; nothing else holds it
        raise RuntimeError(f"git {' '.join(args[:3])} timed out after {_GIT_TIMEOUT_S}s in {worktree}") from exc
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {worktree}: {out.stderr.strip()}")
    return out.stdout


def _restore_owner_access(toolkit_root: Path) -> None:
    """Give the owner rwx on every folder under ``toolkit_root`` (``.git`` aside) and delete every
    entry that is neither a regular file, a folder nor a link — a FIFO, socket or device, which
    no toolkit holds and which hangs git (a FIFO named ``.gitignore`` blocked every git call,
    B3c-1 review 21). An explicit stack, skipping what it cannot open or change: best-effort, it
    never raises, so no single folder stops what comes after it."""
    import stat

    root = Path(toolkit_root).resolve()
    stack = [str(root)]
    while stack:
        folder = stack.pop()
        try:
            info = os.lstat(folder)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                continue
            if info.st_mode & 0o700 != 0o700:
                os.chmod(folder, info.st_mode | 0o700)
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name == ".git" and folder != str(root):
                        # A nested repository is never judged work, and one pointing at a crafted
                        # git dir hung every git call (B3c-1 review 22): gone, whatever its kind.
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                import shutil

                                shutil.rmtree(entry.path, ignore_errors=True)
                            else:
                                os.unlink(entry.path)
                        except OSError:
                            pass
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if not (folder == str(root) and entry.name == ".git"):
                            stack.append(entry.path)
                    elif not (entry.is_file(follow_symlinks=False) or entry.is_symlink()):
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass
        except OSError:
            continue


def remove_untracked_ignores(toolkit_root: Path) -> list[str]:
    """Delete every ``.gitignore`` git does not track (``gate.untracked_ignore_files``), so the
    files it hid become plain untracked files that the reset's ``clean`` removes and that
    ``pending_changes`` sees. Returns what was removed."""
    from auto_research.sandbox.gate import untracked_ignore_files

    root = Path(toolkit_root)
    # git cannot list a folder it cannot read: owner access everywhere first, or a `.gitignore`
    # inside an unlistable folder is never found (B3c-1 review 20).
    _restore_owner_access(root)
    try:
        found = untracked_ignore_files(root)
    except (subprocess.CalledProcessError, OSError):
        return []  # not a repository: nothing git could hide
    except subprocess.TimeoutExpired:
        _clear_index_lock(root)
        raise
    import stat

    for rel in found:
        # Each folder on the way made its owner's again first: a folder the coder made read-only
        # would refuse the unlink and keep hiding what it hides (B3c-1 review 19).
        folder = root
        for part in Path(rel).parts[:-1]:
            folder = folder / part
            info = folder.lstat()
            if stat.S_ISDIR(info.st_mode) and info.st_mode & 0o700 != 0o700:
                folder.chmod(info.st_mode | 0o700)
        (root / rel).unlink(missing_ok=True)
    return found


def purge_bytecode(toolkit_root: Path) -> None:
    """Delete every ``*.pyc``/``*.pyo`` in the toolkit tree (``.git`` aside). Git ignores bytecode,
    so no gate, review or commit ever sees it, yet an import prefers a ``.pyc`` whose header
    matches its source, and a sourceless one at the toolkit root (first on workers'
    ``PYTHONPATH``) shadows any module — a planted one would run in place of the reviewed code
    (B3c-1 reviews 9-11). Each folder is made readable and writable to its owner before it is
    listed (a folder the coder made unlistable hid a ``.pyc`` Python still loaded), and any error
    raises: a purge that could not finish must stop the commit. Bytecode is regenerated on the next
    import; nothing else is touched (other ignored files may be the human's)."""
    import stat

    def clear(top: str) -> None:
        # An explicit stack, not recursion: a coder's `mkdir -p` a thousand levels deep must not
        # overflow the walk (B3c-1 review 14).
        stack = [top]
        while stack:
            folder = stack.pop()
            info = os.lstat(folder)
            if stat.S_ISLNK(info.st_mode):
                continue
            if info.st_mode & 0o700 != 0o700:
                os.chmod(folder, info.st_mode | 0o700)
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if not (folder == top and entry.name == ".git"):
                            stack.append(entry.path)
                    elif entry.name.endswith((".pyc", ".pyo")):
                        os.unlink(entry.path)

    # Resolved: the configured root may itself be a link (it is on this host); links *inside* the
    # tree are not followed.
    root = Path(toolkit_root).resolve()
    if root.is_dir():  # no tree, no bytecode
        clear(str(root))

def _stage_judged(worktree: Path, manifest: dict[str, str]) -> None:
    """Put exactly the judged bytes in the index: each file read once, its hash checked against the
    manifest, written as a blob with no filters (``hash-object --no-filters``) and entered by path
    (``update-index --cacheinfo``). ``git add`` would apply ``.gitattributes`` conversions (an
    encoding, ``ident``, a filter) and commit bytes nobody scanned or tested (B3c-1 review 8)."""
    import hashlib
    import stat

    # Deletions first (as the export does): a file replacing a folder can only enter the index
    # once the folder's files have left it.
    for rel, entry in sorted(manifest.items(), key=lambda kv: (kv[1] != "deleted", kv[0])):
        if entry == "deleted":
            _git_in(worktree, "update-index", "--force-remove", "--", rel)
            continue
        if entry.startswith("link:"):
            raise RuntimeError(f"{rel} is a link; links are not committed")
        fd = os.open(worktree / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            data = handle.read() if stat.S_ISREG(info.st_mode) else b""
        mode = stat.S_IMODE(info.st_mode)
        if f"{mode:o}:{hashlib.sha256(data).hexdigest()}" != entry:
            raise RuntimeError(f"the changeset changed after the gate judged it: [{rel!r}]")
        blob = subprocess.run(["git", "-C", str(worktree), "hash-object", "-w", "--no-filters", "--stdin"],
                              input=data, capture_output=True, check=True,
                              timeout=_GIT_TIMEOUT_S).stdout.decode().strip()
        git_mode = "100755" if mode & 0o111 else "100644"
        _git_in(worktree, "update-index", "--add", "--cacheinfo", f"{git_mode},{blob},{rel}")


def approve_edit(
    worktree: Path, *, message: str, manifest: dict[str, str] | None = None, base: str = "",
) -> str:
    """Land one atomic commit per tool on the worktree's branch (CLAUDE.md §9.9, step 6).

    The **only** place the runtime commits. Stages **exactly** the files the gate verified
    (``gate.collect_targets`` — every changed/new file under a ``*_utils`` package),
    explicit-path (``git add -- <paths>``, never ``git add -A``), so worker scratch outside a
    ``*_utils`` package can never leak into the commit. Commits with ``message`` as the
    subject on the branch the worktree is already checked out on, and returns the new commit
    sha.

    Pure git mechanics: it does **not** re-assert gate-/review-pass — the commit pipeline in
    ``propose_and_pr`` only reaches here on gate-pass ∧ review-pass (§9.9: commit ⇔ gate-pass
    ∧ co-lead review-pass). The runtime never merges and never touches master.
    """
    from auto_research.sandbox.gate import changeset_manifest, collect_deleted, collect_targets

    worktree = Path(worktree)
    _git_in(worktree, "reset", "-q")  # the index holds only what is staged below
    if manifest is not None:
        # [G3]: exactly what the gate judged, unchanged since — or no commit at all.
        if base and _git_in(worktree, "rev-parse", "HEAD").strip() != base:
            raise RuntimeError("HEAD moved after the gate judged the changeset; submit again")
        current = changeset_manifest(worktree)
        changed = sorted(k for k in set(manifest) | set(current) if manifest.get(k) != current.get(k))
        if changed:
            raise RuntimeError(f"the changeset changed after the gate judged it: {changed[:10]}")
        _stage_judged(worktree, manifest)
    else:
        rels = sorted(str(p.relative_to(worktree)) for p in collect_targets(worktree))
        # Deletions the gate saw are part of the change: `git add -- <path>` records a removal.
        rels += collect_deleted(worktree)
        if rels:
            _git_in(worktree, "--literal-pathspecs", "add", "--", *rels)
    # No repository hooks: a hook could change the index after the judged bytes were staged.
    _git_in(worktree, "-c", "core.hooksPath=/dev/null", "commit", "--no-verify", "-m", message)
    return _git_in(worktree, "rev-parse", "HEAD").strip()


async def _reset_direct_tree(
    toolkit_root: Path,
    *,
    base_ref: str | None = None,
    artifact_dir: Path | None,
    monitor: Any | None = None,
    tool_spec: Any | None = None,
) -> None:
    """Reset the direct-branch toolkit checkout to ``base_ref`` after a **non-committed**
    authoring attempt (CLAUDE.md §9.9). The only place besides :func:`approve_edit` the runtime
    touches the toolkit tree — its counterpart on the failure path: a failed attempt leaves the
    live ``toolkit_root`` checkout dirty (modified ``lib``/``TOOL_MANIFEST`` + new untracked
    files), and — if the coder ran git itself despite the deny-rule — with an illegitimate
    commit. ``base_ref`` is HEAD recorded at the attempt's start (per-attempt, by
    :func:`propose_and_pr`); resetting to it (rather than ``HEAD``) **drops any coder commit**
    while preserving an earlier round's successful commit, and clears the dirty tree so
    re-running workers never see a phantom tool and the next attempt's clean-tree precondition
    holds. Falls back to ``HEAD`` when ``base_ref`` is unset. Before discarding, the attempt's
    full diff against ``base_ref`` (tracked edits, new untracked content, **and** any coder
    commit) is saved as a ``.patch`` under ``artifact_dir`` (the run's authoring dir, outside the
    toolkit) so the rejected work stays inspectable. Best-effort: any failure is logged and
    swallowed — cleanup must never mask the caller's result or re-raise.
    """
    toolkit_root = Path(toolkit_root)
    reset_to = base_ref or "HEAD"
    try:
        sid = getattr(tool_spec, "id", None) or "tool"
        patch_path = _save_diff_and_reset(
            toolkit_root, reset_to, artifact_dir,
            patch_prefix="tool_author_failed", spec_id=str(sid),
        )
        await _emit(
            monitor, "coder.tree_reset", level=4,
            summary=f"reset {toolkit_root.name} to {reset_to[:10]} after a non-committed authoring attempt",
            data={"patch": str(patch_path) if patch_path else None},
        )
    except Exception as exc:  # noqa: BLE001 — cleanup must never mask the caller's result
        await _emit(
            monitor, "coder.tree_reset.error", level=3,
            summary=f"working-tree reset failed: {type(exc).__name__}: {exc}",
            data={"error": repr(exc)},
        )


def _save_diff_and_reset(
    toolkit_root: Path,
    reset_to: str,
    artifact_dir: Path | None,
    *,
    patch_prefix: str,
    spec_id: str = "tool",
) -> Path | None:
    """Save the working-tree diff vs ``reset_to`` as a ``.patch`` under ``artifact_dir`` (outside
    the toolkit, so the artifact never re-dirties the tree), then ``git reset --hard reset_to`` +
    ``git clean -fd``. Returns the patch path (or ``None`` when there was nothing to save).

    The shared core of the failed-attempt reset (:func:`_reset_direct_tree`) and the resume-time
    heal (:func:`heal_toolkit_branch_if_dirty`). Everything is staged first so the diff captures
    new untracked files (and any coder commit, diffed against ``reset_to``); the reset unstages
    it. Raises on a git failure — the caller decides whether to swallow it.
    """
    toolkit_root = Path(toolkit_root)
    patch_path: Path | None = None
    try:
        if artifact_dir is not None:
            artifact_dir = Path(artifact_dir)
            ad, tr = artifact_dir.resolve(), toolkit_root.resolve()
            if ad != tr and tr not in ad.parents:
                _git_in(toolkit_root, "add", "-A")
                diff = _git_in(toolkit_root, "diff", "--cached", reset_to)
                if diff.strip():
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    patch_path = artifact_dir / f"{patch_prefix}_{spec_id}_{uuid.uuid4().hex[:8]}.patch"
                    patch_path.write_text(diff, encoding="utf-8", errors="surrogateescape")
    finally:
        # The reset runs whatever happened to the patch: keeping the rejected work inspectable
        # never outranks removing it from the tree workers import. Folders first made the owner's
        # again (the purge's walk does it): a folder the coder made read-only or unlistable
        # stopped `reset` and `clean` (B3c-1 review 13).
        # Each step on its own: one failing (a path too long to walk) never skips the other.
        try:
            remove_untracked_ignores(toolkit_root)  # what it hid is cleaned below
        except Exception:  # noqa: BLE001 — nothing may stand between a rejected attempt and its reset
            pass
        try:
            purge_bytecode(toolkit_root)
        except Exception:  # noqa: BLE001 — the reset below reports what it still cannot undo
            pass
        _git_in(toolkit_root, "reset", "--hard", reset_to)
        _git_in(toolkit_root, "clean", "-ffd")  # -ff: a nested repository a coder left goes too
        # An error here reaches `_reset_direct_tree`'s handler and its monitor event; workers read
        # no in-tree bytecode anyway (PYTHONPYCACHEPREFIX).
        purge_bytecode(toolkit_root)
    return patch_path


_BASE_REF_FILE = "current_base_ref"


def _is_ancestor(root: Path, ref: str, head: str) -> bool:
    """Whether ``ref`` names a commit that ``head`` descends from (or is)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", ref, head],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    return proc.returncode == 0


def _base_ref_path(worktree_base: Path) -> Path:
    """Where the per-attempt authoring base-ref is persisted, so a resume after a hard-kill can
    reset the branch to exactly the killed attempt's start.

    **Beside the ledger** when the authoring dir is a run's (``runs/<run>/authoring``): that dir is
    writable to the Claude author's jail, and a resume runs ``git reset`` to whatever this file
    says, so a forged ref would move the branch outside the gate (F27, 2026-10-06). An authoring dir
    outside a runs tree (a standalone benchmark trial) keeps it there.
    """
    base = Path(worktree_base)
    if base.name == "authoring" and base.parent.parent.name == "runs":
        from auto_research.sandbox.workspace import record_beside

        return record_beside(base.parent, f"authoring_{_BASE_REF_FILE}", legacy_ok=False)
    return base / _BASE_REF_FILE


def _read_persisted_base_ref(run_directory: Path) -> str | None:
    """Read the base-ref persisted by :func:`build_authoring_session` for the last attempt, or None."""
    try:
        text = _base_ref_path(Path(run_directory) / "authoring").read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


# ----------------------------------------------------------------------------
# Submission snapshots — the checkpoint just before the gate scans (§9.9)
# ----------------------------------------------------------------------------
#
# Coding is the run's most expensive chunk, and everything between "coder starts" and
# "approve_edit commits" was volatile: a crash anywhere in an attempt — including a review-time
# provider failure on an already gate-passed tree — reset the tree to the base ref and forced a
# full re-authoring. A *submission* is the natural durability boundary: the tree state the lead
# judged worth gating. So every submit persists the working tree as a patch (plus the verdicts
# it earned) under the run's authoring dir, and a resumed attempt for the same tool on the same
# base ref restores the latest submission and continues, re-running the gate on the restored
# tree — no stored verdict is ever trusted (the checkpoint sits BEFORE the scan by design, so
# ``commit ⇔ gate ∧ review`` stays defined over the live tree).

_SUBMISSIONS_DIR = "submissions"


def _submissions_dir(worktree_base: Path, tool_name: str) -> Path:
    return Path(worktree_base) / _SUBMISSIONS_DIR / tool_name


def _load_snapshot_state(snap_dir: Path) -> dict | None:
    try:
        state = json.loads((snap_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    # The folder is writable to the coder: a state of any other shape is no state at all.
    submits = state.get("submits") if isinstance(state, dict) else None
    if not (isinstance(submits, list) and submits and all(isinstance(e, dict) for e in submits)):
        return None
    return state


def _write_snapshot_state(snap_dir: Path, state: dict) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")


def _working_tree_patch(worktree: Path, base_ref: str) -> str:
    """The full diff of the working tree vs ``base_ref`` — tracked edits AND untracked files —
    leaving the tree byte-identical: stage everything, diff the index, unstage."""
    _git_in(worktree, "add", "-A")
    try:
        return _git_in(worktree, "diff", "--cached", base_ref)
    finally:
        _git_in(worktree, "reset", "--quiet", base_ref)


def save_submission_snapshot(
    worktree: Path, worktree_base: Path, tool_spec: ToolSpec, *, base_ref: str, branch: str,
) -> dict | None:
    """Persist the current working tree as this tool's latest submission. Returns the new
    submit entry (its verdict fields filled in later), or None when there is nothing to save.
    Best-effort by the caller's choice — a snapshot failure must never block a submit."""
    snap_dir = _submissions_dir(worktree_base, tool_spec.proposed_tool_name)
    diff = _working_tree_patch(worktree, base_ref)
    if not diff.strip():
        return None
    state = _load_snapshot_state(snap_dir) or {
        "tool_name": tool_spec.proposed_tool_name,
        "target_domain": tool_spec.target_domain,
        "tool_spec_id": tool_spec.id,
        "branch": branch,
        "base_ref": base_ref,
        "submits": [],
    }
    if state.get("base_ref") != base_ref:
        # A chain-mate committed since these snapshots were taken: they can never be restored,
        # so start this tool's record fresh at the current base.
        state = {**state, "base_ref": base_ref, "submits": []}
    index = len(state["submits"]) + 1
    patch_name = f"submit_{index:02d}.patch"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / patch_name).write_text(diff, encoding="utf-8", errors="surrogateescape")
    entry = {"index": index, "patch": patch_name, "gate_passed": None, "review_approved": None,
             "review_issues": []}
    state["submits"].append(entry)
    _write_snapshot_state(snap_dir, state)
    return entry


def _update_last_submission(worktree_base: Path, tool_name: str, **fields: Any) -> None:
    """Record a gate/review outcome onto the latest submission's state (best-effort)."""
    snap_dir = _submissions_dir(Path(worktree_base), tool_name)
    try:
        state = _load_snapshot_state(snap_dir)
        if state is None:
            return
        state["submits"][-1].update(fields)
        _write_snapshot_state(snap_dir, state)
    except Exception:  # noqa: BLE001 — a record of the outcome never decides the outcome
        pass


def find_submission_snapshot(
    worktree_base: Path, tool_name: str, *, base_ref: str
) -> tuple[Path, dict] | None:
    """The latest restorable submission for ``tool_name``: its state must record exactly the
    current ``base_ref`` — the condition that makes a restore safe (the patch re-applies onto
    the very tree state it was taken from, chains included)."""
    snap_dir = _submissions_dir(Path(worktree_base), tool_name)
    state = _load_snapshot_state(snap_dir)
    if state is None or state.get("base_ref") != base_ref:
        return None
    name = state["submits"][-1].get("patch")
    if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
        return None  # a plain file name inside this snapshot folder, nothing else
    patch = snap_dir / name
    return (patch, state) if patch.is_file() and not patch.is_symlink() else None


def restore_submission_snapshot(worktree: Path, patch: Path) -> None:
    """Apply the persisted submission onto the clean tree at its base ref (working-tree write
    only — never a commit; ``approve_edit`` stays the sole commit path). Raises on failure so
    the caller can discard the snapshot and start clean."""
    _git_in(worktree, "apply", "--whitespace=nowarn", str(patch))


def discard_submission_snapshots(worktree_base: Path, tool_name: str) -> None:
    """Remove a tool's snapshot record (on commit — the work now lives in git — or on a failed
    restore). Best-effort."""
    import shutil

    shutil.rmtree(_submissions_dir(Path(worktree_base), tool_name), ignore_errors=True)


class BranchHealResult(NamedTuple):
    """Outcome of a resume-time authoring-branch heal (for a monitor event)."""

    healed: bool
    reason: str  # "clean" | "healed" | "not_git" | "wrong_branch" | "error: <repr>"
    base_ref: str | None = None
    patch_path: Path | None = None


def committed_environments(toolkit_root: Path, manifest: dict[str, str]) -> list[str]:
    """The environment ids a commit concerns (env plan B3c-4): every definition it changed (and did
    not delete) and the environment every package it changed names in ``tool_config.yaml``."""
    from auto_research.sandbox.env_build import _NAME
    from auto_research.sandbox.gate import _ENV_DEF_RE, _TOOLKIT_PKG_RE, _package_env

    toolkit_root = Path(toolkit_root)
    ids: set[str] = set()
    for path in manifest:
        if match := _ENV_DEF_RE.match(path):
            ids.add(match.group(1))
        elif _TOOLKIT_PKG_RE.match(path):
            env = _package_env(toolkit_root / path.split("/", 1)[0])
            if env and _NAME.fullmatch(env):
                ids.add(env)
    return sorted(i for i in ids if (toolkit_root / "envs" / i / "env.yaml").is_file())


def deleted_environments(toolkit_root: Path, manifest: dict[str, str]) -> list[str]:
    """The environment ids whose definition a commit deleted (its ``env.yaml`` is gone)."""
    from auto_research.sandbox.gate import _ENV_DEF_RE

    ids = {m.group(1) for path, entry in manifest.items()
           if entry == "deleted" and (m := _ENV_DEF_RE.match(path)) and path.endswith("/env.yaml")}
    return sorted(i for i in ids if not (Path(toolkit_root) / "envs" / i / "env.yaml").exists())


def build_committed_environments(
    toolkit_root: Path, env_ids: list[str], *, runner: Any = None,
) -> list[dict[str, Any]]:
    """Build each committed definition into the runtime's agent conda dir (``ar-<env_id>``), as
    the catalogue and ``use_env`` read them; a rebuild moves the previous build aside. One result
    per id: ``ok``, the environment name, its definition digest, seconds, or the error. Blocking."""
    from auto_research.env import agent_conda_dir, analysis_environment, conda_executable, declared_externals
    import subprocess as _subprocess

    from auto_research.sandbox.env_build import build, read_definition
    from auto_research.sandbox.jail import jail_enabled

    runner = runner or _subprocess.run
    human = analysis_environment()
    bases = {human.name: human} if human is not None else {}
    externals = declared_externals()
    results: list[dict[str, Any]] = []
    for env_id in env_ids:
        if getattr(runner, "stopped", False):
            break  # a stop ends the remaining builds before any moves its predecessor aside
        started = time.monotonic()
        entry: dict[str, Any] = {"env": env_id}
        try:
            definition = read_definition(Path(toolkit_root) / "envs" / env_id)
            entry["definition_sha256"] = definition.digest()
            environment = build(definition, conda_dir=agent_conda_dir(), conda_exe=conda_executable(),
                                base_envs=bases, externals=externals, jailed=jail_enabled(), runner=runner)
            entry.update(ok=True, name=environment.name, prefix=str(environment.prefix))
        except Exception as exc:  # noqa: BLE001 — a failed build is reported; the commit stands
            entry.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[-600:]}")
        entry["seconds"] = round(time.monotonic() - started, 1)
        results.append(entry)
    return results


def reconcile_environments(toolkit_root: Path, *, runner: Any = None) -> list[dict[str, Any]]:
    """Every environment definition on the toolkit branch against its build, at launch (B3c-5).

    - a definition that does not validate → ``failed``; its build is left as it is;
    - a record missing or naming another definition (a commit build that was stopped, a definition
      changed by hand) → built (``cause``: ``missing`` / ``stale``);
    - a built prefix that no longer holds what its lock names, or whose installed distributions
      differ from those recorded at its build (**drift**: only a human can cause it, e.g. ``pip
      install`` by hand) → that state's lock saved in the lock store, the prefix kept as
      ``ar-<id>.drift-<fp8>``, a fresh build from the unchanged definition; when that build fails
      the drifted prefix is put back and stays in use;
    - every other definition goes through :func:`env_build.build`, which reuses an intact prefix and
      refreshes its record (activation, boot file, externals; a build the jail setting no longer
      admits is redone);
    - a built environment whose declared externals' stamps changed → reported and recorded.

    One result per definition (``status``: ``ok``, ``built``, ``drift``, ``external_changed``,
    ``failed``), plus ``tainted`` when an unjailed build ran in the conda dir and the jail is on.
    Blocking; never raises for one environment."""
    from auto_research.env import agent_conda_dir, analysis_environment, declared_externals
    from auto_research.sandbox import env_build as B
    from auto_research.sandbox.envs import _external_stamp
    from auto_research.sandbox.jail import jail_enabled

    envs_dir = Path(toolkit_root) / "envs"
    ids = sorted(p.name for p in envs_dir.iterdir() if (p / "env.yaml").is_file()) if envs_dir.is_dir() else []
    conda_dir = agent_conda_dir()
    externals = declared_externals()
    human = analysis_environment()
    bases = {human.name: human} if human is not None else {}
    results: list[dict[str, Any]] = []
    if jail_enabled() and os.path.lexists(conda_dir / "built" / f"{B._UNJAILED}.json"):
        results.append({"env": "*", "status": "tainted", "marker": str(conda_dir / "built" / f"{B._UNJAILED}.json")})
    causes: dict[str, str | None] = {}
    for env_id in ids:
        if getattr(runner, "stopped", False):
            break  # a stop leaves the remaining environments exactly as they are
        prefix = conda_dir / "envs" / f"ar-{env_id}"
        try:
            problems = B.validate(envs_dir / env_id, base_envs=bases, externals=externals)
            if problems:
                results.append({"env": env_id, "status": "failed", "error": "; ".join(problems)[-600:]})
                continue
            definition = B.read_definition(envs_dir / env_id)
            record = B.build_record(conda_dir, prefix)
            current = record is not None and record.get("definition_sha256") == definition.digest()
            if definition.base or not current or not prefix.is_dir():
                causes[env_id] = ("stale" if record is not None and not current else
                                  "missing" if record is None or not definition.base else None)
                continue
            drift = None
            try:
                B.verify(definition, prefix)
            except B.BuildMismatch as exc:
                drift = str(exc)[-1500:]
            if drift is None and (before := B.recorded_dists(conda_dir, prefix)) is not None:
                now = B.installed_dists(prefix)
                if now != before:
                    added, removed = sorted(set(now) - set(before)), sorted(set(before) - set(now))
                    drift = f"installed distributions changed: added {added[:20]}; removed {removed[:20]}"
            if drift is None:
                causes[env_id] = None
                continue
            results.append(_rebuild_drifted(toolkit_root, env_id, prefix, record, drift, runner=runner,
                                            conda_dir=conda_dir, externals=externals, bases=bases))
        except Exception as exc:  # noqa: BLE001 — one environment's trouble is reported, never the launch's
            results.append({"env": env_id, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[-600:]}"})
    built = build_committed_environments(toolkit_root, list(causes), runner=runner) if causes else []
    for entry in built:
        cause = causes.get(entry["env"])
        status = ("built" if cause else "ok") if entry["ok"] else "failed"
        result = {**entry, "status": status, **({"cause": cause} if cause else {})}
        if entry["ok"]:
            changed = _external_changes(conda_dir, conda_dir / "envs" / f"ar-{entry['env']}", externals, B, _external_stamp)
            if changed and status == "ok":
                result = {"env": entry["env"], "status": "external_changed", "externals": changed}
        results.append(result)
    return results


def _external_changes(conda_dir: Path, prefix: Path, externals: dict, B: Any, stamp: Any) -> list[str]:
    """Which declared externals of a built environment changed since last recorded; the record
    then holds the new stamps (an external cannot be rebuilt, only reported)."""
    record = B.build_record(conda_dir, prefix)
    if record is None:
        return []
    names = [n for n in record.get("externals") or [] if isinstance(n, str) and n in externals]
    stamps = {n: stamp(externals[n])["stamps"] for n in names}
    seen = record.get("external_stamps")
    if seen != stamps:
        B._write_record(conda_dir, prefix, {**record, "external_stamps": stamps})
    if not isinstance(seen, dict):
        return []
    # An external added to or removed from the definition is a new baseline, not a change.
    return sorted(n for n in set(seen) & set(stamps) if seen[n] != stamps[n])


def _rebuild_drifted(toolkit_root: Path, env_id: str, prefix: Path, record: dict, drift: str, *,
                     runner: Any, conda_dir: Path, externals: dict, bases: dict) -> dict[str, Any]:
    """Save the drifted state's lock, keep the prefix aside, build fresh; on a failed build put the
    drifted prefix and its record back, so the environment stays usable (and is reported)."""
    from auto_research.env import env_lock_dir
    from auto_research.sandbox import env_build as B
    from auto_research.sandbox.envs import Environment, write_lock

    listed = next((e for e in B.built_environments(conda_dir, externals, bases) if e.name == f"ar-{env_id}"), None)
    fp = write_lock(listed or Environment(name=f"ar-{env_id}", prefix=prefix, owner="pipeline"),
                    env_lock_dir()).stem  # the drifted state stays rebuildable, activation included
    kept = prefix.with_name(f"{prefix.name}.drift-{fp[:8]}")
    if kept.exists():
        kept = kept.with_name(f"{kept.name}-{uuid.uuid4().hex[:6]}")
    prefix.rename(kept)
    builds = build_committed_environments(toolkit_root, [env_id], runner=runner)
    built = builds[0] if builds else {"ok": False, "error": "stopped before the rebuild"}
    result = {"env": env_id, "status": "drift", "diff": drift, "fingerprint": fp, "rebuilt": built["ok"]}
    if built["ok"]:
        return {**result, "kept": str(kept), "name": built["name"]}
    if os.path.lexists(prefix):
        B._remove_tree(prefix)
    kept.rename(prefix)
    B._write_record(conda_dir, prefix, record)
    return {**result, "error": built["error"]}


def heal_toolkit_branch_if_dirty(
    run_directory: Path,
    *,
    root: Path | None = None,
    branch: str | None = None,
) -> BranchHealResult:
    """Resume-time repair (§12): tidy a dirty authoring branch left by a hard-kill *during
    authoring* (whose in-process failure reset never ran), so a resumed run's first authoring
    attempt does not die on :func:`check_toolkit_branch_clean` — the unrecoverable restart loop.

    Runs the **same** cleanup a failed authoring session runs: reset the branch to the per-attempt
    base-ref persisted by :func:`build_authoring_session` (:func:`_base_ref_path`, beside the ledger;
    falls back to ``HEAD`` when absent), after saving the discarded work as a
    ``resume_heal_*.patch``. Resetting to the recorded base-ref (not blindly to ``HEAD``) drops the
    killed attempt's mess **and** any commit it slipped, while preserving tools committed earlier
    in the chain.

    No-op on a clean tree, a non-git ``root``, or a wrong current branch (a config problem the
    preflight surfaces loudly). Best-effort/tolerant — a git failure returns ``reason="error: …"``
    rather than raising, so resume falls through to the (loud) preflight check instead of crashing
    here. Sync (git is subprocess-sync); the caller records a ``run.resume.heal`` monitor event
    from the returned result once its monitor exists.
    """
    root = Path(root) if root is not None else _resolve_toolkit_root()
    branch = branch or _resolve_tool_author_branch()
    if not (root / ".git").exists():
        return BranchHealResult(False, "not_git")
    try:
        remove_untracked_ignores(root)  # files a coder-written `.gitignore` hid become dirty
        purge_bytecode(root)  # git ignores bytecode: "clean" must not keep a planted `.pyc`
        if not _is_dirty(root):
            return BranchHealResult(False, "clean")
        if _current_branch(root) != branch:
            return BranchHealResult(False, "wrong_branch")
        base_ref = _read_persisted_base_ref(run_directory)
        if base_ref is not None and not _is_ancestor(root, base_ref, "HEAD"):
            # A base ref the branch never passed through is not an attempt's start: refuse it
            # rather than reset the branch there (F27).
            return BranchHealResult(False, f"refused: base ref {base_ref[:12]} is not an ancestor of HEAD")
        reset_to = base_ref or "HEAD"
        patch_path = _save_diff_and_reset(
            root, reset_to, Path(run_directory) / "authoring", patch_prefix="resume_heal",
        )
        return BranchHealResult(True, "healed", base_ref, patch_path)
    except Exception as exc:  # noqa: BLE001 — never crash the launch; the preflight is the loud backstop
        return BranchHealResult(False, f"error: {exc!r}")


# Captured so ``propose_and_pr``'s ``approve_edit`` parameter (a test seam) can default to
# the real implementation without the local name shadowing the module-level function.
_DEFAULT_APPROVE_EDIT = approve_edit






#: Environment-variable prefixes of the operator's own editor/agent tooling, dropped from the
#: author's environment (see ``_author_env``).
_HOST_TOOLING_PREFIXES = ("CLAUDE", "COPILOT", "VSCODE")


def _author_pfiles(worktree_base: Path | None, toolkit_root: Path) -> Path | None:
    """The Claude author's private PFILES folder: in its artifacts dir, never inside the toolkit.

    A caller with no artifacts dir falls back to the toolkit root as ``worktree_base``; a folder
    there would dirty the tree the gate scans, so that case gets none.
    """
    if worktree_base is None:
        return None
    base = Path(worktree_base).resolve()
    tree = Path(toolkit_root).resolve()
    if base == tree or base.is_relative_to(tree):
        return None
    return base / "pfiles"


def _author_env(
    toolkit_root: Path, pfiles_dir: Path | None = None, active_bin: Path | None = None,
) -> dict[str, str] | None:
    """The environment the SDK author's ``Bash`` runs in — **identical to how the analyst
    sandbox and the gate execute toolkit code** (CLAUDE.md §9.4): the analyst conda interpreter
    first on ``PATH`` (so ``python``/``pytest`` carry the full science stack — ``xspec`` via the
    sourced HEASoft env, ``ixpeobssim`` from the conda env), HEASoft/CALDB sourced from
    ``analyze_ini_sh``, and ``toolkit_root`` on ``PYTHONPATH``. Without this the author would run
    in the orchestration env (no science stack) and could not run the real tests it authors —
    only the gate could — so it would be blind to import-time failures (§9.4 the author and the
    analysts are the only code-runners; both must share the analyst env).

    Best-effort: returns ``None`` (inherit the parent env) when the analyst env isn't configured
    or can't be sourced, so unit tests and science-stack-free setups still build options.

    ``active_bin`` (env plan B3c-3) goes first on ``PATH``: the folder ``cand_env_activate`` fills
    with wrappers for a candidate environment, empty until then, so plain ``python``/``pytest``
    follow the coder's activation without a restart.
    """
    import os

    cfg = _orchestration_config()
    from auto_research.env import analysis_environment

    environment = analysis_environment(cfg)
    ini_sh = cfg.get("analyze_ini_sh")
    if environment is None and not ini_sh:
        if active_bin is None:
            return None
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_HOST_TOOLING_PREFIXES)}
        env["PATH"] = f"{active_bin}:" + env.get("PATH", "")
        return env
    try:
        from auto_research.sandbox.runner import _source_ini_env

        if environment is not None:
            env = environment.activation_env()
        else:  # an init script with no prefix configured
            env = dict(_source_ini_env(ini_sh))
    except Exception:  # noqa: BLE001 — inherit rather than fail the whole options build
        if active_bin is None:
            return None
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_HOST_TOOLING_PREFIXES)}
        env["PATH"] = f"{active_bin}:" + env.get("PATH", "")
        return env
    # Host-tooling variables ride in from the terminal that launched the run and are none of the
    # author's business. Measured 2026-09-03 (run fbb93f26, ledger seq 922): the coder ran
    # `env | grep -i path` and read `CLAUDE_CODE_EXECPATH=…/anthropic.claude-code-…/claude` and a
    # Copilot variable out of it — constant across benchmark arms, so not condition-bearing, but
    # host identity reaching an actor all the same.
    env = {k: v for k, v in env.items() if not k.upper().startswith(_HOST_TOOLING_PREFIXES)}
    if environment is not None:
        env["PATH"] = f"{environment.prefix / 'bin'}:" + env.get("PATH", "")
    if active_bin is not None:
        env["PATH"] = f"{active_bin}:" + env.get("PATH", "")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(toolkit_root) + (":" + existing_pp if existing_pp else "")
    if pfiles_dir is not None:
        # Its own HEASoft parameter folder (F23), inside a place the author's jail lets it write.
        from auto_research.sandbox.runner import with_private_param_dir

        env = with_private_param_dir(env, pfiles_dir)
    return env












# ----------------------------------------------------------------------------
# Author seed / feedback messages
# ----------------------------------------------------------------------------


def _initial_message(tool_spec: ToolSpec, structure_context: str) -> str:
    return (
        "Author the tool described by this ToolSpec brief into the toolkit, following your "
        "system prompt. Write both halves and the tests, run pytest until green, then stop.\n\n"
        f"## ToolSpec\n```json\n{tool_spec.model_dump_json(indent=2)}\n```\n\n"
        f"## Local structure context\n{structure_context}"
    )


def _restored_note(entry: dict) -> str:
    """The seed-message section for a restored submission: what the tree already holds and the
    feedback it last earned, so the coder continues rather than restarts."""
    lines = [
        "## Restored prior work",
        f"A previous session for this tool was interrupted after submission {entry.get('index')}. "
        "The working tree ALREADY CONTAINS that submission's files — inspect them first and "
        "build on them.",
    ]
    if entry.get("gate_passed") is True:
        lines.append("That submission passed the deterministic gate.")
    elif entry.get("gate_passed") is False:
        lines.append("That submission was rejected by the deterministic gate.")
    issues = entry.get("review_issues")
    # Written in a coder-writable folder: only a list of texts is a list of issues.
    issues = [str(i)[:300] for i in issues] if isinstance(issues, list) else []
    if issues:
        lines.append("The independent reviewer's outstanding feedback on it:")
        lines += [f"- {i}" for i in issues]
    lines.append("Address the outstanding feedback, re-run pytest until green, then stop.")
    return "\n".join(lines)


def _gate_feedback_message(gate: GateReport) -> str:
    failed = [c for c in (*gate.layer_a, *gate.layer_b) if not c.passed]
    detail = "\n".join(f"- {c.name}: {c.detail}" for c in failed) or gate.reason
    # When the structural checks fail, the tool's tests are not run yet — warn the coder so a
    # partial "looks done" fix isn't submitted before the remaining feedback can surface.
    tests_not_run = (not gate.layer_b) and any(not c.passed for c in gate.layer_a)
    tail = (
        "\n\nYour tests have not run yet — they run only after the checks above pass, so more "
        "feedback may follow once you fix these."
        if tests_not_run else ""
    )
    return (
        "The gate rejected your tool. Fix EVERY issue listed below before you resubmit — a "
        "partial fix is rejected again — then re-run pytest in the worktree:\n"
        f"{detail}\n\nNever fabricate data or results; fix the underlying cause rather than "
        f"weakening a check to pass.{tail}"
    )


def _review_feedback_message(review: ToolReviewVerdict) -> str:
    issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
    return (
        "The independent co-lead reviewer REJECTED your tool. Address these and re-run "
        f"pytest:\n{issues}\n\n{review.rationale}"
    )


# ----------------------------------------------------------------------------
# The harness
# ----------------------------------------------------------------------------


class AuthoringSession:
    """A live tool-authoring session over one ``ToolSpec``: a connected Sonnet (Claude-SDK)
    coder on a toolkit branch, plus the deterministic gate and provider-decorrelated co-lead
    review exposed as **discrete, separately-callable steps** (§9.3/§9.4).

    :func:`propose_and_pr` drives these primitives in the bounded auto-loop (back-compat for
    tests and any non-lead caller). The analyst lead's authoring ReAct tools
    (``drive_coder``/``submit``/``respond_to_review``) drive the *same* primitives directly, so
    the lead — the plan owner — diagnoses each gate/review verdict itself rather than the run
    terminating. Build via :func:`build_authoring_session`; ``await connect()`` before use and
    ``await aclose()`` in a ``finally``.
    """

    def __init__(
        self,
        *,
        tool_spec: ToolSpec,
        backend: Any,
        worktree: Path,
        branch: str,
        base_ref: str | None,
        structure_context: str,
        run_gate: GateHook,
        run_review: ReviewHook,
        approve_edit: Callable[..., str],
        direct_branch: str | None,
        toolkit_root: Path,
        worktree_base: Path,
        submit_cap: int = 5,
        debate_cap: int = 3,
        monitor: Any | None = None,
        coder_model: str = "",
        restored_submission: dict | None = None,
        env_tools: Any | None = None,
    ) -> None:
        self.tool_spec = tool_spec
        self._env_tools = env_tools  # the attempt's candidate environments (B3c-3), cleared at close
        self.worktree = worktree
        self.branch = branch
        self._closed = False  # aclose() is idempotent — the interrupt path may beat the finally
        # The second screen (B1): the coder's in-flight instruction, if any, and what it left.
        self._drive_task: asyncio.Task | None = None
        self._drive_status = "idle"
        self._drive_result: str | None = None
        self._drive_error: str | None = None
        self._init_channel()
        # Lead-driven loop state (§9.3 B-topology): the most recent gate/review/commit, so a
        # caller (the lead's drive_authoring loop) can build the terminal MergeFlowResult from
        # session state; submit_cap bounds the expensive submit_for_review calls.
        self.submit_cap = submit_cap
        self.submits_used = 0
        self.debate_cap = debate_cap  # bounds respond_to_review (lead↔co-lead) turns
        self.debates_used = 0
        self.last_gate: GateReport | None = None
        #: The changeset the co-lead's latest review saw (its files' identities); a commit must
        #: hold exactly it.
        self._reviewed_manifest: dict[str, str] | None = None
        self.last_review: ToolReviewVerdict | None = None
        # The durable record that this session landed a commit, and which one: set by commit(),
        # never cleared, read by `_authoring_result` to build the terminal MergeFlowResult. It
        # deliberately does NOT gate the failure reset — "this session ever committed" and "the
        # work now in the tree is banked" are different questions, and a single latch answering
        # both is what let a rejected tool survive on disk and become callable (F1, 2026-09-09).
        # The reset asks `pending_changes()`; there is no separate boolean, because two fields
        # encoding one fact is how the meanings drifted apart in the first place.
        self.last_commit_sha: str | None = None
        #: What building the last commit's environments came to, for the lead (B3c-4); "" if none.
        self.env_note = ""
        self._backend = backend
        self._base_ref = base_ref
        self._structure_context = structure_context
        self._run_gate = run_gate
        self._run_review = run_review
        self._approve_edit = approve_edit
        self._direct_branch = direct_branch
        self._toolkit_root = toolkit_root
        self._worktree_base = worktree_base
        self._monitor = monitor
        self._coder_model = coder_model  # attributes the SDK session's own spend on the cost meter
        # A prior interrupted attempt's latest submission, already applied to the working tree
        # by build_authoring_session; carried so the seed message and the ledger say so.
        self.restored_submission = restored_submission

    @property
    def initial_message(self) -> str:
        """The seed turn: the ToolSpec brief + runtime-safe local structure context; when a
        prior attempt's submission was restored, a section saying what is already in the tree
        and what feedback it last earned."""
        message = _initial_message(self.tool_spec, self._structure_context)
        if self.restored_submission:
            message += "\n\n" + _restored_note(self.restored_submission)
        return message

    async def connect(self) -> None:
        await self._backend.connect()
        await _emit(
            self._monitor, "coder.session_start", level=4,
            summary=f"authoring {self.tool_spec.proposed_tool_name} on branch {self.branch}",
            data={"tool_spec_id": self.tool_spec.id, "tool": self.tool_spec.proposed_tool_name,
                  "branch": self.branch, "disposition": self.tool_spec.disposition},
        )
        if self.restored_submission:
            await _emit(
                self._monitor, "authoring.snapshot.restored", level=3,
                summary=(
                    f"restored submission {self.restored_submission.get('index')} of "
                    f"{self.tool_spec.proposed_tool_name} into the working tree"
                ),
                data={"tool": self.tool_spec.proposed_tool_name,
                      "restored_from": self.restored_submission},
            )

    async def drive(self, message: str) -> str:
        """Send one instruction to the warm coder session and drain its response (L0 inside).

        Which author harness answers is the backend's business (``coder.backend``); this flow is
        defined over the working tree, so the gate, the review and the commit are identical
        whichever one it is.

        **Blocking.** :meth:`start_drive` is the non-blocking form the lead uses (B1) — this one
        remains because ``drive_authoring`` and the seed call still want to wait.
        """
        return await self._backend.drive(message)

    # ── the second screen: the coder runs BESIDE the lead (B1) ────────────────
    #
    # `drive` blocks for as long as the coder works, which for a real link is minutes. The lead
    # spent that time doing nothing, and the loop it was in could not be re-entered. `start_drive`
    # hands the instruction over and returns at once; the lead keeps investigating, and parks in
    # `wait_for_event` (the lead's `wait_coder`) when it has nothing else to do. `submit_for_review`
    # stays BLOCKING by ruling — the gate runs pytest twice through a blocking `subprocess.run`,
    # and the CI framing says you wait for the ticket.

    def _init_channel(self) -> None:
        """State for the lead<->coder channel: the wake event a blocking wait parks on, messages
        queued for the coder's next turn, and the clocks a wait reports.

        Separate from ``__init__`` so a test double built with ``object.__new__`` gets the same
        state from the same line production runs.
        """
        # Set whenever there is something the lead should hear: a coder turn ended, the coder
        # asked a question, a drive failed. A waiter clears it before it parks.
        self._wake = asyncio.Event()
        # Messages for the coder's NEXT turn. Queued by default rather than steered into the
        # running one: slot 6 (2026-09-15) steered its coder 47 times mid-turn, as a way of
        # staying busy while it could not wait.
        self._queued: list[str] = []
        # True from the coder's `message(to="lead")` until something reaches it, so the lead's reply is
        # delivered inside the running turn however it is sent — the coder may be waiting on it.
        self._question_open = False
        # The lead's steer event on RunControl (see `watch_steers`), or None when unattended.
        self._steer_event: asyncio.Event | None = None
        self._drive_started_at: float | None = None

    def watch_steers(self, event: asyncio.Event | None) -> None:
        """Wake a pending :meth:`wait_for_event` when a human steer for the lead arrives.

        The wait only returns; the steer itself is still delivered by the lead's loop at its next
        safe boundary (the A2 hold rule), so this adds no injection site.
        """
        self._steer_event = event

    async def send(self, message: str, *, mid_turn: bool = False) -> str:
        """Say one thing to the coder, whatever state it is in — the ONE lead->coder path.

        Picks the primitive from the coder's state rather than making the lead pick. An idle
        coder is given a new turn. A working coder has the message **queued** for its next turn,
        unless ``mid_turn`` is set or the coder has an open question, in which case it is
        *steered* into the turn it is running (an answer must reach a coder that may be waiting
        on it). The queued-by-default split is Codex's: a queued follow-up is delivered at the
        next boundary, and redirecting a running turn is a separate, explicit act.

        Returns ``"started"`` | ``"queued"`` | ``"steered"`` | ``"answered"`` — what the tool
        reports back.
        """
        if self.drive_status == "working":
            if mid_turn or self._question_open:
                answered = self._question_open
                steered = await self._backend.steer(message)
                if steered:
                    self._question_open = False
                    return "answered" if answered else "steered"
                # The turn ended between the check and the steer. Falling through is right: the
                # message still needs delivering, and the coder is now between turns.
            if self.drive_status == "working":
                self._queued.append(message)
                await _emit(
                    self._monitor, "coder.queued", level=5,
                    summary=f"queued for the coder's next turn: {_cap_text(message, 60)}",
                    data={"text": message, "queued": len(self._queued)},
                )
                return "queued"
        self.start_drive(message)
        return "started"

    def mark_question_open(self) -> None:
        """The coder asked the lead something (its text is in the lead's inbox, E12): the lead's
        next ``drive_coder`` is delivered inside the running turn, since it is most likely the
        answer, and a lead parked in :meth:`wait_for_event` wakes."""
        self._question_open = True
        self._wake.set()

    def start_drive(self, message: str) -> None:
        """Send one instruction and return immediately; the coder answers in the background.

        One drive at a time: a second instruction while the coder is working would interleave two
        conversations on one warm session, which no harness defines. The caller is told rather
        than silently queued — :meth:`send` is the path that queues.
        """
        if self._drive_task is not None and not self._drive_task.done():
            raise RuntimeError("the coder is already working on an instruction")
        self._drive_status = "working"
        self._question_open = False
        self._drive_started_at = time.monotonic()
        self._drive_task = asyncio.create_task(self._drive_in_background(message))

    async def _drive_in_background(self, message: str) -> str:
        """The background half of :meth:`start_drive`.

        Runs the instruction, then any messages queued meanwhile as the NEXT turn, in the same
        task — so the coder stays ``working`` across the handover and a second task never
        exists. Each finished turn's narration is kept unread and wakes the lead's wait.

        Cost attribution needs nothing special: the meter is a ``ContextVar`` and a task created
        here inherits the context that created it, so the coder's spend lands in the run total
        exactly as it did when the drive blocked (B7).
        """
        while True:
            try:
                text = await self._backend.drive(message)
            except asyncio.CancelledError:
                self._drive_status = "cancelled"
                self._queued = []
                raise
            except Exception as exc:  # noqa: BLE001 — a dead coder is a result, not a crash
                # Never let a backend failure escape into the event loop as an unretrieved
                # exception: the lead has to be TOLD the coder died. A Claude-harness session
                # whose credential ran out returned its billing text with `is_error` discarded on
                # 2026-09-09, and the lead spent three drives steering a corpse at $0.
                self._drive_status = "stopped"
                self._drive_error = repr(exc)
                self._drive_result = f"[harness error] the coder turn failed: {exc}"
                self._queued = []
                self._wake.set()
                return self._drive_result
            self._drive_result = text if self._drive_result is None else (
                f"{self._drive_result}\n\n--- next turn ---\n\n{text}"
            )
            self._wake.set()
            if not self._queued:
                break
            message = "\n\n".join(self._queued)
            delivered, self._queued = len(self._queued), []
            self._question_open = False
            self._drive_started_at = time.monotonic()
            await _emit(
                self._monitor, "coder.queued.delivered", level=5,
                summary=f"started the coder's next turn on {delivered} queued message(s)",
                data={"delivered": delivered},
            )
        self._drive_status = "stopped"
        return text

    # ── the blocking wait (replaces polling) ─────────────────────────────────

    def _ready_reason(self) -> str | None:
        """What a wait would return right now, or None when there is nothing yet."""
        if self._drive_error is not None:
            return "failed"
        if self._drive_result is not None:
            return "turn_ended"
        if self._steer_event is not None and self._steer_event.is_set():
            return "steer"
        if self.drive_status != "working":
            return "idle"
        return None

    async def wait_for_event(self, timeout_s: float) -> str:
        """Park until there is something for the lead, or ``timeout_s`` passes.

        Returns ``"turn_ended"`` (narration unread), ``"failed"``, ``"steer"`` (a message reached
        the lead's inbox — a human's, another seat's, or the coder's own question, E12), ``"idle"``
        (nothing is running and nothing is unread) or ``"timeout"``. Returns at once when
        something is already waiting. Consumes nothing: the caller reads what it needs with
        :meth:`take_drive_result`, and the inbox is drained by the lead's loop.

        A cancel (``/stop``) propagates out of the awaited event as it does from any await.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max(0.0, timeout_s)
        reason: str | None = None
        while True:
            reason = self._ready_reason()
            if reason is not None:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                reason = "timeout"
                break
            self._wake.clear()
            waiters = [asyncio.ensure_future(self._wake.wait())]
            if self._steer_event is not None:
                waiters.append(asyncio.ensure_future(self._steer_event.wait()))
            try:
                await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
        await _emit(
            self._monitor, "coder.wait", level=5,
            summary=f"lead waited {loop.time() - started:.0f}s → {reason}",
            data={"reason": reason, "waited_s": round(loop.time() - started, 1),
                  "timeout_s": timeout_s, "drive_status": self.drive_status},
        )
        return reason

    def current_instruction_elapsed_s(self) -> float | None:
        """Seconds the coder has spent on the turn it is running now; None when it is not."""
        if self.drive_status != "working" or self._drive_started_at is None:
            return None
        return time.monotonic() - self._drive_started_at

    def has_unread_result(self) -> bool:
        """Whether a finished turn's narration (or a failure) is waiting to be read."""
        return self._drive_result is not None or self._drive_error is not None

    @property
    def drive_status(self) -> str:
        """``idle`` | ``working`` | ``stopped`` | ``cancelled`` — what the coder is doing now."""
        if self._drive_task is None:
            return "idle"
        if not self._drive_task.done():
            return "working"
        return self._drive_status

    def take_drive_result(self) -> tuple[str | None, str | None]:
        """The finished drive's ``(text, error)``, consumed once so it is reported once."""
        text, error = self._drive_result, self._drive_error
        self._drive_result = self._drive_error = None
        return text, error

    async def _cancel_drive(self) -> None:
        """Cancel the background drive and AWAIT it, before anything else tears down.

        Ordering is the point (B2). `aclose` disconnects the backend and may reset the worktree;
        a drive still in flight would be writing into that tree while it is being repaired, and
        `RunControl.interrupt` cancels only the registered run task — an unowned task created
        here is not in that set. So it is cancelled and awaited first, explicitly.
        """
        task = self._drive_task
        self._drive_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    def pending_changes(self) -> bool:
        """Whether the tree holds work this attempt has not yet landed.

        False right after :meth:`commit` (the tree is clean and HEAD *is* the base ref), and
        True again as soon as the coder edits something. Read by ``execute_submit`` so a submit
        with nothing new is answered rather than executed: the gate would scan an empty
        changeset and ``approve_edit`` would then run ``git commit`` with nothing staged, which
        exits non-zero and raises out of the tool call — an [F6] run-killer, since ``_react``
        does not catch tool exceptions.

        Best-effort: a git failure reports True, so an unreadable tree falls through to the
        gate (which reports its own verdict) instead of silently refusing a real submission.
        """
        _restore_owner_access(self.worktree)  # a FIFO would hang the status below
        try:
            if _git_in(self.worktree, "status", "--porcelain").strip():
                return True
            if self._base_ref is None:
                return True
            return _git_in(self.worktree, "rev-parse", "HEAD").strip() != self._base_ref
        except RuntimeError:
            return True

    async def run_gate_step(self) -> GateReport:
        """Run the deterministic gate over the current tree (§9.4). First applies the §9.9
        backstop: if the coder slipped in a commit (HEAD moved off this attempt's base), undo it
        with ``reset --mixed`` so its work returns to the working tree and the scans see it. The
        gate is domain-blind and mechanical — a lead can only *satisfy* it (via :meth:`drive`),
        never argue it."""
        await _emit(self._monitor, "gate.start", level=5, summary="deterministic gate running")
        # Owner access everywhere and no FIFO/socket/device in the tree before git walks it.
        await asyncio.to_thread(_restore_owner_access, self.worktree)
        # Always, not only when HEAD moved: anything already staged in the index would be
        # committed whole while the gate and the review read the work tree (B3c-1 review: a
        # staged edit passed unscanned, a staged rename deleted a protected file). After this
        # only unstaged edits, deletions and new files remain. A reset that fails is the gate's
        # failure, never a silent pass over a tree it could not settle.
        try:
            head = _git_in(self.worktree, "rev-parse", "HEAD").strip()
        except (RuntimeError, OSError):
            head = None  # not a repository (a test double): nothing is staged, nothing to settle
        try:
            if head is not None or self._base_ref is not None:
                _git_in(self.worktree, "reset", "--mixed", "-q", self._base_ref or "HEAD")
        except RuntimeError as exc:
            g = GateReport(passed=False, reason=f"the worktree could not be unstaged before the gate: {exc}")
            self.last_gate = g
            await _emit(self._monitor, "gate.result", level=4, summary="gate rejected",
                        data={"passed": False, "failing_checks": ["unstage"], "reason": g.reason})
            return g
        if self._base_ref is not None:
            if head != self._base_ref:
                await _emit(
                    self._monitor, "coder.committed", level=4,
                    summary="coder ran git itself — auto-reverted the commit to uncommitted changes",
                    data={"head_was": head, "base_ref": self._base_ref},
                )
        # THE CHECKPOINT: persist this submission before anything scans it, so a crash anywhere
        # downstream (the gate, a review-provider failure, a kill) costs a resume the review at
        # most, never the authoring. Best-effort — a snapshot failure must never block a submit.
        if self._base_ref is not None:
            try:
                entry = save_submission_snapshot(
                    self.worktree, self._worktree_base, self.tool_spec,
                    base_ref=self._base_ref, branch=self.branch,
                )
                if entry is not None:
                    await _emit(
                        self._monitor, "authoring.snapshot.saved", level=5,
                        summary=f"submission {entry['index']} of "
                                f"{self.tool_spec.proposed_tool_name} checkpointed",
                        data={"tool": self.tool_spec.proposed_tool_name, "index": entry["index"]},
                    )
            except Exception as exc:  # noqa: BLE001 — the submit matters more than its snapshot
                await _emit(
                    self._monitor, "authoring.snapshot.error", level=4,
                    summary=f"submission snapshot failed: {type(exc).__name__}: {exc}",
                    data={"error": repr(exc)},
                )
        try:
            g = await self._run_gate(self.worktree, self.tool_spec)
        except (OSError, ValueError, subprocess.SubprocessError, RecursionError, MemoryError) as exc:
            # An unreadable file or a failed git call is the gate's failure, reported to the lead,
            # never an exception that ends the authoring (§9.3.8).
            g = GateReport(passed=False, reason=f"the gate could not read the changeset: {type(exc).__name__}: {exc}")
        failed = [f"[{c.layer}] {c.name}" for c in (*g.layer_a, *g.layer_b) if not c.passed]
        await _emit(
            self._monitor, "gate.result", level=4,
            summary=f"gate {'passed' if g.passed else 'rejected'}",
            data={"passed": g.passed, "failing_checks": failed, "report_hash": g.report_hash,
                  "reason": g.reason},
        )
        self.last_gate = g
        if self._base_ref is not None:
            failed = [f"[{c.layer}] {c.name}" for c in (*g.layer_a, *g.layer_b) if not c.passed]
            _update_last_submission(
                self._worktree_base, self.tool_spec.proposed_tool_name,
                gate_passed=g.passed, gate_failing_checks=failed,
            )
        return g

    async def run_review_step(
        self, gate: GateReport, *, lead_rebuttal: str | None = None
    ) -> ToolReviewVerdict:
        """Run the provider-decorrelated co-lead review over the diff + gate result (§9.3 step 4).
        The review is *debatable* (unlike the gate): when ``lead_rebuttal`` is set the co-lead
        **reconsiders** its prior verdict (``self.last_review``) in light of the lead's argument
        (the §9.3 debate) — but ``commit ⇔ gate-pass ∧ review-pass`` holds; the lead persuades,
        never overrides."""
        from auto_research.sandbox.gate import changeset_manifest

        # As at the gate's entry: owner access, no FIFO/socket/device or nested repository before
        # git walks the tree (a rebuttal review had run on an unswept tree, B3c-1 review 23).
        await asyncio.to_thread(_restore_owner_access, self.worktree)
        try:
            self._reviewed_manifest = changeset_manifest(self.worktree)
        except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError, subprocess.TimeoutExpired):
            if (self.worktree / ".git").exists():  # a real repository: never a silent off-switch
                self._reviewed_manifest = {"<unreadable at review>": ""}
            else:
                self._reviewed_manifest = None  # not a repository (a test double): nothing to bind
        except (OSError, ValueError):
            # Unreadable now: no commit may claim this review (never a silent off-switch).
            self._reviewed_manifest = {"<unreadable at review>": ""}
        if lead_rebuttal is not None:
            # Debate turn: the co-lead reconsiders its prior verdict given the lead's argument.
            # Only this path needs the extended hook kwargs, so the plain auto-loop / submit path
            # stays compatible with a 3-arg injected review hook (tests, non-lead callers).
            review = await self._run_review(
                self.worktree, self.tool_spec, gate,
                prior_review=self.last_review, lead_rebuttal=lead_rebuttal,
            )
        else:
            review = await self._run_review(self.worktree, self.tool_spec, gate)
        await _emit(
            self._monitor, "review.result", level=4,
            summary=f"co-lead {'approved' if review.approved else 'rejected'}",
            data={"approved": review.approved, "issues": list(review.issues),
                  "checks_adequate": review.checks_adequate, "rationale": review.rationale},
        )
        self.last_review = review
        if self._base_ref is not None:
            _update_last_submission(
                self._worktree_base, self.tool_spec.proposed_tool_name,
                review_approved=review.approved,
                review_issues=[_cap_text(i, 300) for i in review.issues],
            )
        return review

    async def commit(self, message: str) -> str:
        """Land one atomic commit (§9.9 ``approve_edit``) — the **only** place the runtime
        commits. Caller guarantees gate-pass ∧ review-pass. Records the commit on
        :attr:`last_commit_sha`, and **advances the per-attempt base ref**
        (in memory and on disk) to the commit it just landed, so nothing downstream mistakes an
        approved commit for a coder-slipped one.

        Advancing the base ref is also what makes the commit "banked" for
        :meth:`pending_changes`, and therefore what makes :meth:`aclose` reset only the work that
        came *after* this commit rather than skipping the reset entirely (F1)."""
        manifest = self.last_gate.manifest if self.last_gate is not None else None
        # A real gate always records one (possibly empty); only an injected stub gate has none.
        if manifest is not None and self._reviewed_manifest is not None and manifest != self._reviewed_manifest:
            # commit ⇔ gate ∧ review over the SAME tree: a re-gate after a rebuttal may have judged
            # files the co-lead never saw (B3c-1 review 8).
            raise RuntimeError("the changeset changed after the co-lead reviewed it; submit again")
        try:
            # Before staging: a purge that cannot finish stops the commit, honestly.
            purge_bytecode(self.worktree)
        except OSError as exc:
            raise RuntimeError(f"stray bytecode in the toolkit could not be cleared: {exc}") from exc
        commit_sha = (self._approve_edit(self.worktree, message=message, manifest=manifest,
                                         base=self.last_gate.base)
                      if manifest is not None else self._approve_edit(self.worktree, message=message))
        self.last_commit_sha = commit_sha
        # The base ref means "the commit this attempt's work sits on", so once approve_edit lands
        # one, that commit IS the base. Advancing it here corrects all three readers at once:
        # the §9.9 gate-entry backstop below (which reverts on a moved HEAD and cannot otherwise
        # tell this legitimate commit from a coder's illegitimate one — a later submit_for_review
        # reverted an approved commit, observed live), `aclose`'s failure reset (which would
        # otherwise reset *behind* a legitimate commit), and the resume-time heal, which does a
        # `reset --hard` to the persisted value after a hard kill.
        self._base_ref = commit_sha
        if self._direct_branch is not None and self._worktree_base is not None:
            try:
                _base_ref_path(self._worktree_base).write_text(commit_sha, encoding="utf-8")
            except OSError:
                pass  # best-effort, exactly as the initial write in build_authoring_session
        # The committed work now lives in git; the snapshot record has nothing left to restore.
        discard_submission_snapshots(self._worktree_base, self.tool_spec.proposed_tool_name)
        await _emit(
            self._monitor, "commit", level=3,
            summary=f"committed {self.tool_spec.proposed_tool_name} → {commit_sha[:10]}",
            data={"tool": self.tool_spec.proposed_tool_name, "commit_sha": commit_sha,
                  "branch": self.branch, "message": message},
        )
        self.env_note = await self._build_environments(manifest)
        return commit_sha

    async def _build_environments(self, manifest: dict[str, str] | None) -> str:
        """Build the environments a direct-branch commit concerns (B3c-4) before the registry
        reloads, so the catalogue lists them; the note for the lead. Worktree mode (tests,
        benchmarks) commits to a branch nobody runs, and builds nothing."""
        if self._direct_branch is None or not manifest:
            return ""
        from auto_research.sandbox.env_build import StoppableRunner, forget

        runner = StoppableRunner()
        dropped: list[str] = []
        try:
            env_ids = committed_environments(self.worktree, manifest)
            deleted = deleted_environments(self.worktree, manifest)
            if deleted:
                from auto_research.env import agent_conda_dir

                for env_id in deleted:
                    if forget(agent_conda_dir(), env_id):  # recorded as it happens, whatever follows
                        dropped.append(env_id)
                        await _emit(self._monitor, "env.forget", level=3,
                                    summary=f"ar-{env_id} removed from the catalogue",
                                    data={"commit_sha": self.last_commit_sha, "env": env_id})
            results = (await asyncio.to_thread(build_committed_environments, self.worktree, env_ids, runner=runner)
                       if env_ids else [])
        except asyncio.CancelledError:
            runner.stop()  # a stop ends the build's conda and pip, not only this coroutine
            # The commit stands without its environments: said in the ledger; the launch drift
            # check (B3c-5) builds a committed definition whose record is missing or stale.
            await asyncio.shield(_emit(
                self._monitor, "env.build.error", level=3,
                summary="stopped while building the commit's environments; they are built at the next launch",
                data={"commit_sha": self.last_commit_sha, "error": "stopped"}))
            raise
        except Exception as exc:  # noqa: BLE001 — the commit has landed; a build problem is a note
            await _emit(self._monitor, "env.build.error", level=3,
                        summary=f"environments not built: {type(exc).__name__}: {_cap_text(str(exc), 200)}",
                        data={"commit_sha": self.last_commit_sha, "error": repr(exc)})
            removed = f" Removed from the catalogue: {', '.join(f'ar-{i}' for i in dropped)}." if dropped else ""
            return (f" The commit's environments were not built ({type(exc).__name__}: "
                    f"{_cap_text(str(exc), 300)}); the commit stands. State this in your report.{removed}")
        for result in results:
            await _emit(
                self._monitor, "env.build" if result["ok"] else "env.build.error", level=3,
                summary=(f"built {result['name']} in {result['seconds']}s" if result["ok"]
                         else f"ar-{result['env']} did not build: {_cap_text(result['error'], 200)}"),
                data={"commit_sha": self.last_commit_sha, **result},
            )
        built = [r["name"] for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        note = f" Environment{'s' if len(built) > 1 else ''} built: {', '.join(built)}." if built else ""
        if dropped:
            note += f" Removed from the catalogue (definition deleted): {', '.join(f'ar-{i}' for i in dropped)}."
        for result in failed:
            note += (f" The environment ar-{result['env']} did not build ({_cap_text(result['error'], 300)}); "
                     "the commit stands. State this in your report.")
        return note

    async def aclose(self) -> None:
        """Disconnect the coder; on a direct-branch attempt holding **unbanked** work, reset the
        tree to the per-attempt base ref (§9.9 failure counterpart to ``approve_edit`` — saves a
        patch artifact first, drops any coder-landed commit, leaves a clean tree at the right
        base).

        The guard is :meth:`pending_changes`, not :attr:`committed` (F1, 2026-09-09). The old
        guard asked *"did this session ever commit?"*, which is a latch that never clears, so a
        session that committed one tool and then kept working on a rejected second one exited
        **without** resetting. Its edits — ``lib/`` code, the ``skills/`` wrapper and a
        ``TOOL_MANIFEST`` entry — then stayed on disk, and because ``ToolkitRegistry.reload``
        discovers tools from the *tree* rather than from git, the rejected tool became callable
        and its figures reached an accepted report (slot 2, 2026-09-08; the same shape produced a
        scored quantity in slot 3). Asking the tree instead makes the reset fire on exactly the
        work that was never banked, whether or not a commit preceded it.

        A commit advances the base ref, so right after one ``pending_changes()`` is False and no
        reset happens; the empty-diff case also writes no patch artifact
        (``_save_diff_and_reset`` returns ``None``), so a clean close stays silent.

        Idempotent: a human interrupt closes the session from shielded cleanup (its normal
        ``finally`` is inside the cancelled scope), and the ``finally`` may still run afterwards —
        a second reset would discard a base ref's worth of nothing, but the disconnect and the
        patch artifact would be repeated for no reason.
        """
        if self._closed:
            return
        self._closed = True
        if self._env_tools is not None:
            self._env_tools.close()  # a tool still running writes nothing into the tree being reset
        # B2: the background drive dies before the backend goes away and before the tree is
        # touched, so a coder cannot be writing into a worktree that is being reset under it.
        await self._cancel_drive()
        await self._backend.disconnect()
        if self._direct_branch is not None:
            # Git ignores bytecode and anything an untracked `.gitignore` hides, so `pending_changes`
            # alone would miss a planted `numpy.pyc` or a hidden `json/` package (B3c-1 reviews
            # 15-16): bytecode goes, and what was hidden becomes visible to the check and the reset.
            for step in (remove_untracked_ignores, purge_bytecode):  # each on its own
                try:
                    await asyncio.to_thread(step, self._toolkit_root)
                except Exception as exc:  # noqa: BLE001 — reported like a failed reset, never raised
                    await _emit(self._monitor, "coder.tree_reset.error", level=3,
                                summary=f"{step.__name__} failed: {type(exc).__name__}: {exc}",
                                data={"error": repr(exc)})
        if self._direct_branch is not None and self.pending_changes():
            await _reset_direct_tree(
                self._toolkit_root, base_ref=self._base_ref, artifact_dir=self._worktree_base,
                monitor=self._monitor, tool_spec=self.tool_spec,
            )
        if self._env_tools is not None:
            # The candidate environments end with the attempt; a committed definition is built
            # again from the branch (B3c-4).
            try:
                await asyncio.to_thread(self._env_tools.cleanup)
            except Exception as exc:  # noqa: BLE001 — reported, never raised from close
                await _emit(self._monitor, "coder.env_cand.cleanup_error", level=3,
                            summary=f"candidate environments not removed: {type(exc).__name__}: {exc}",
                            data={"error": repr(exc)})


def _build_backend(
    *,
    member: RoleModelConfig,
    worktree: Path,
    toolkit_root: Path,
    worktree_base: Path | None = None,
    system_prompt: str,
    ask_lead: Callable[..., Awaitable[str]],
    ask_human: Callable[..., Awaitable[str]],
    max_turns: int,
    monitor: Any | None,
    sdk_client_factory: Callable[[Any], Any],
    env_tools: Any | None = None,
) -> Any:
    """Build the author harness ``member`` names (§6, ``coder.harness``).

    Both backends receive the same brief, the same working tree, the same escalation handlers and
    the same filesystem allow-list; what differs is the mechanism each harness offers for them.
    The Claude SDK hosts the escalation tools in-process and gates the filesystem with a hook; the
    Codex app-server takes an MCP endpoint by URL and enforces the allow-list as writable roots at
    the OS level (see :mod:`auto_research.sandbox.coder.codex_sdk`).

    ``member`` is passed in rather than read from config here so one caller can author with a
    member the config does not name, which is what lets a benchmark sweep the author across vendors
    while every other part of the flow stays byte-identical.

    ``env_tools`` (a :class:`~auto_research.sandbox.coder.env_tools.CandidateEnvs`) adds the
    environment tools to the escalation server, its ``active/bin`` first on the author's ``PATH``
    and its folder read-only in the author's jail (env plan B3c-3).
    """
    coder_model = member.model
    active_bin = env_tools.active_bin if env_tools is not None else None
    cand_ro = (str(env_tools.cand_dir),) if env_tools is not None else ()
    if member.harness == "codex":
        from auto_research.sandbox.coder.codex_sdk import CodexBackend
        from auto_research.sandbox.coder.escalation_server import EscalationServer
        from auto_research.sandbox.fs_guard import PathGuard, load_fs_allowlist

        # The allow-list becomes this harness's writable roots, with the working tree always in
        # it: one configured list, enforced by the OS rather than by a hook.
        roots = [str(Path(root)) for root in load_fs_allowlist()]
        if str(worktree) not in roots:
            roots.append(str(worktree))
        return CodexBackend(
            member=member,
            worktree=worktree,
            system_prompt=system_prompt,
            guard=PathGuard.from_config(),
            escalation=EscalationServer(ask_lead, ask_human, env_tools=env_tools),
            env=_author_env(toolkit_root, active_bin=active_bin),  # author's shell == analyst/gate
            # execution env; its private PFILES is set by the backend, which knows its home (F23)
            writable_roots=roots,
            jail_readonly=cand_ro,
            monitor=monitor,
        )

    options = _build_options(
        worktree=worktree, coder_model=coder_model, system_prompt=system_prompt,
        escalation_server=_make_escalation_server(ask_lead, ask_human, env_tools), max_turns=max_turns,
        # author's Bash == analyst/gate code-execution env; its PFILES in its writable artifacts dir
        env=_author_env(toolkit_root, _author_pfiles(worktree_base, toolkit_root), active_bin),
        worktree_base=worktree_base,    # where the jail launcher is written, when the jail is on
        jail_readonly=cand_ro,
    )
    return ClaudeSdkBackend(sdk_client_factory(options), monitor=monitor, coder_model=coder_model)


def build_authoring_session(
    tool_spec: ToolSpec,
    *,
    lead: Any,
    toolkit_root: Path,
    worktree_base: Path,
    colead: Any | None = None,
    human_handler: HumanHandler | None = None,
    coder: RoleModelConfig | None = None,
    allow_correlated_review: bool = False,
    run_gate: GateHook | None = None,
    run_review: ReviewHook | None = None,
    max_turns: int | None = None,
    submit_cap: int | None = None,
    debate_cap: int | None = None,
    direct_branch: str | None = None,
    worktree_factory: Callable[[Path, Path, str], Path] | None = None,
    sdk_client_factory: Callable[[Any], Any] | None = None,
    approve_edit: Callable[..., str] | None = None,
    monitor: Any | None = None,
) -> AuthoringSession:
    """Resolve config + hooks, choose worktree/direct-branch mode (§9.9), build the author harness
    (seeded via ``coder`` prompt + escalation tools), and return a **not yet connected**
    :class:`AuthoringSession`. Setup only — the sole git touches are the direct-branch clean-tree
    precondition and the per-attempt base-ref record. ``await connect()`` on the returned session
    before driving it.

    ``coder`` is the author member; ``None`` means the one ``config/models.yaml`` names, which is
    every production caller. It carries **all three** author facts at once — the model id recorded
    and priced, the provider the review is routed away from, and the harness that spawns the author
    process — so they cannot be resolved separately and disagree. A benchmark passes a member
    directly to sweep the author across vendors with the rest of the flow byte-identical.

    ``allow_correlated_review`` is a **measurement** switch, off in production: it permits a co-lead
    sharing the author's provider, which the trust boundary otherwise refuses (§9.3). Set it only to
    measure what decorrelation is worth; the launch preflight still guards the configured roles.
    """
    toolkit_root = Path(toolkit_root)
    worktree_base = Path(worktree_base)
    if coder is None:
        from auto_research.sandbox.coder.harness import coder_member

        coder = coder_member()
    coder_model = coder.model
    if run_gate is None:
        from auto_research.sandbox.gate import run_gate as _real_gate

        run_gate = _real_gate
    if run_review is None:
        from auto_research.sandbox.review import make_review_hook

        # Forward the monitor: without it the co-lead built from config gets `monitor=None`,
        # so its ReAct actions (tool calls, web searches, its reasoning steps) never reach the
        # run record — only the AuthoringSession's own `review.result` verdict does. That made
        # the one provider-decorrelated check in `commit ⇔ gate ∧ review` the only actor whose
        # work was invisible.
        run_review = make_review_hook(
            colead=colead, author_provider=coder.provider, monitor=monitor,
            allow_correlated=allow_correlated_review,
        )
    max_turns = max_turns if max_turns is not None else _cap("tool_author_max_turns", 50)
    submit_cap = submit_cap if submit_cap is not None else _cap("tool_author_submit_cap", 5)
    debate_cap = debate_cap if debate_cap is not None else _cap("tool_author_debate_cap", 3)
    worktree_factory = worktree_factory or _create_worktree
    sdk_client_factory = sdk_client_factory or _default_sdk_client_factory
    approve_edit = approve_edit or _DEFAULT_APPROVE_EDIT

    # Direct-branch mode (runtime authoring, §9.9): the author works in ``toolkit_root`` on the
    # ambient ``ai`` branch instead of an isolated worktree. The runtime is passive about
    # checkout — it asserts the precondition (also enforced at launch by the preflight) and
    # never switches branches. The isolated-worktree path stays the default for tests + future use.
    base_ref: str | None = None
    if direct_branch is not None:
        check_toolkit_branch_clean(toolkit_root, direct_branch)
        branch = direct_branch
        worktree = toolkit_root
        # Record HEAD for THIS attempt (per-attempt, not once at launch): a failed authoring
        # resets the tree back to exactly here (§9.9), dropping any commit the coder lands —
        # while preserving an earlier round's successful approve_edit commit in a multi-tool run.
        base_ref = _git_in(toolkit_root, "rev-parse", "HEAD").strip()
        # Persist it so a resume after a hard-kill *during* this attempt can heal the branch to
        # exactly here (heal_toolkit_branch_if_dirty), matching the in-process failure reset.
        try:
            worktree_base.mkdir(parents=True, exist_ok=True)
            _base_ref_path(worktree_base).write_text(base_ref, encoding="utf-8")
        except OSError:
            pass  # best-effort; heal falls back to HEAD if the file is unreadable
        # Restore an interrupted attempt's latest submission (the pre-gate checkpoint): only
        # onto the exact base ref it was taken from, so chains and healed trees stay safe. A
        # restore failure discards the record and starts clean — the snapshot is an economy,
        # never a dependency.
        restored_submission = None
        found = find_submission_snapshot(
            worktree_base, tool_spec.proposed_tool_name, base_ref=base_ref
        )
        if found is not None:
            patch, state = found
            try:
                restore_submission_snapshot(toolkit_root, patch)
                restored_submission = state["submits"][-1]
            except Exception:  # noqa: BLE001 — an unappliable snapshot must not block authoring
                discard_submission_snapshots(worktree_base, tool_spec.proposed_tool_name)
    else:
        branch = f"tool/{tool_spec.proposed_tool_name}-{uuid.uuid4().hex[:8]}"
        worktree = worktree_factory(toolkit_root, worktree_base, branch)
        restored_submission = None

    structure_context = _build_structure_context(worktree)
    system_prompt = load_prompt("coder")
    # The handler is built before the session it parks questions on, so it reaches it late. A
    # one-slot holder rather than a forward reference: the backend captures the handler at
    # construction and must not capture a session that does not exist yet.
    session_slot: list[Any] = [None]
    ask_lead = _make_ask_lead_handler(lambda: session_slot[0], lead=lead, monitor=monitor)
    ask_human = _make_ask_human_handler(
        human_handler, session_ref=lambda: session_slot[0], lead=lead, model=coder_model,
        monitor=monitor,
    )
    # The attempt's candidate environments (B3c-3): in the artifacts dir, never in the tree the gate
    # scans, removed when the session closes.
    from auto_research.sandbox.coder.env_tools import CandidateEnvs

    # A caller with no artifacts dir passes the toolkit as `worktree_base`; candidates there would
    # dirty the tree the gate scans, so that session has no environment tools (as `_author_pfiles`).
    cand_root = worktree_base / "envs_cand"
    inside = [root.resolve() for root in (toolkit_root, worktree)]
    env_tools = None if any(cand_root.resolve().is_relative_to(root) for root in inside) else \
        CandidateEnvs(worktree, CandidateEnvs.attempt_dir(cand_root, tool_spec.proposed_tool_name))
    backend = _build_backend(
        member=coder,
        env_tools=env_tools,
        worktree=worktree,
        toolkit_root=toolkit_root,
        worktree_base=worktree_base,
        system_prompt=system_prompt,
        ask_lead=ask_lead,
        ask_human=ask_human,
        max_turns=max_turns,
        monitor=monitor,
        sdk_client_factory=sdk_client_factory,
    )
    session = AuthoringSession(
        tool_spec=tool_spec, backend=backend, worktree=worktree, branch=branch, base_ref=base_ref,
        structure_context=structure_context, run_gate=run_gate, run_review=run_review,
        approve_edit=approve_edit, direct_branch=direct_branch, toolkit_root=toolkit_root,
        worktree_base=worktree_base, submit_cap=submit_cap, debate_cap=debate_cap, monitor=monitor,
        coder_model=coder_model, restored_submission=restored_submission, env_tools=env_tools,
    )
    session_slot[0] = session
    return session


async def propose_and_pr(
    tool_spec: ToolSpec,
    *,
    lead: Any,
    toolkit_root: Path,
    worktree_base: Path,
    colead: Any | None = None,
    human_handler: HumanHandler | None = None,
    coder: RoleModelConfig | None = None,
    allow_correlated_review: bool = False,
    run_gate: GateHook | None = None,
    run_review: ReviewHook | None = None,
    gate_rework_cap: int | None = None,
    review_rework_cap: int | None = None,
    max_turns: int | None = None,
    direct_branch: str | None = None,
    worktree_factory: Callable[[Path, Path, str], Path] | None = None,
    sdk_client_factory: Callable[[Any], Any] | None = None,
    approve_edit: Callable[..., str] | None = None,
    monitor: Any | None = None,
) -> MergeFlowResult:
    """Author a tool from a ``ToolSpec`` and drive the bounded rework loop (§9.3).

    Returns a :class:`MergeFlowResult`. ``committed`` ⇔ gate-pass ∧ co-lead review-pass — one
    atomic commit landed on ``branch`` via ``approve_edit`` (step 6), and ``commit_sha`` is
    its real sha. ``failed`` / ``needs_human`` ⇒ no commit and the lead raises to the human
    (§9.7). Raises :class:`HumanInputRequired` if the author needs a human and none is wired
    (headless fail-safe).

    ``direct_branch`` selects runtime authoring mode (§9.9): when set, the author works in
    ``toolkit_root`` on that ambient branch (default ``ai``, asserted clean) instead of an
    isolated worktree, and a **non-committed** attempt auto-resets the tree back to HEAD (a
    patch artifact of the discarded work is saved under ``worktree_base`` first). ``None``
    keeps the isolated-worktree behaviour (tests/future use).

    ``coder`` is the author member (``None`` = the configured one, every production caller) and
    ``allow_correlated_review`` is the measurement-only switch that permits a same-provider
    co-lead; both are documented on :func:`build_authoring_session`.

    ``colead`` is the GPT co-lead reviewer (an ``AnalystColeadAgent``); when ``None`` the
    default ``run_review`` builds one from config, routed to a member whose provider differs
    from the author's (``review.make_review_hook``). Injectable seams
    (``run_gate``/``run_review``/``worktree_factory``/``sdk_client_factory``/``approve_edit``)
    let the loop and the commit be exercised deterministically without a live LLM or git.
    """
    gate_rework_cap = gate_rework_cap if gate_rework_cap is not None else _cap("tool_author_gate_rework_cap", 3)
    review_rework_cap = review_rework_cap if review_rework_cap is not None else _cap("tool_author_review_rework_cap", 2)
    session = build_authoring_session(
        tool_spec, lead=lead, toolkit_root=toolkit_root, worktree_base=worktree_base,
        colead=colead, human_handler=human_handler, coder=coder,
        allow_correlated_review=allow_correlated_review, run_gate=run_gate,
        run_review=run_review, max_turns=max_turns, direct_branch=direct_branch,
        worktree_factory=worktree_factory, sdk_client_factory=sdk_client_factory,
        approve_edit=approve_edit, monitor=monitor,
    )

    await session.connect()
    try:
        await session.drive(session.initial_message)
        gate_attempts = 0
        review_attempts = 0
        gate = await session.run_gate_step()
        while True:
            if not gate.passed:
                if gate_attempts >= gate_rework_cap:
                    return MergeFlowResult(
                        tool_spec_id=tool_spec.id, status="failed", branch=session.branch,
                        gate_report=gate, reason=f"gate rework cap exhausted: {gate.reason}",
                    )
                gate_attempts += 1
                await session.drive(_gate_feedback_message(gate))
                gate = await session.run_gate_step()
                continue

            review = await session.run_review_step(gate)
            if not review.approved:
                if review_attempts >= review_rework_cap:
                    return MergeFlowResult(
                        tool_spec_id=tool_spec.id, status="failed", branch=session.branch,
                        gate_report=gate, review=review,
                        reason=f"review rework cap exhausted: {review.rationale}",
                    )
                review_attempts += 1
                await session.drive(_review_feedback_message(review))
                # A code change can break the gate — re-run it before re-review.
                gate = await session.run_gate_step()
                continue

            # Commit ⇔ gate-pass ∧ co-lead review-pass (§9.9). The co-lead drafts the commit
            # subject; fall back to the tool name if it left it empty.
            message = review.commit_summary.strip() or tool_spec.proposed_tool_name
            commit_sha = await session.commit(message)
            return MergeFlowResult(
                tool_spec_id=tool_spec.id, status="committed", branch=session.branch,
                commit_sha=commit_sha, gate_report=gate, review=review,
                reason="gate-pass and co-lead review-pass",
            )
    except HumanInputRequired:
        raise  # §9.8 fail-safe must reach the caller — never silently become a "failed".
    except Exception as exc:  # noqa: BLE001
        # A gate/author/review error (e.g. a git hiccup, a decode error) is an honest failure,
        # not a reason to crash the whole research run. The lead surfaces it per §9.6/§9.7.
        return MergeFlowResult(
            tool_spec_id=tool_spec.id, status="failed", branch=session.branch,
            reason=f"authoring aborted by unexpected error: {type(exc).__name__}: {exc}",
        )
    finally:
        # §9.9 failure counterpart to approve_edit: disconnect the coder and, on a non-committed
        # direct-branch attempt, reset the tree to the per-attempt base ref (a phantom tool never
        # surfaces to re-running workers; any coder-landed commit is dropped). See AuthoringSession.aclose.
        await session.aclose()
