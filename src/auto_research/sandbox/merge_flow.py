"""Tool-authoring harness — Phase 2 step 2 (CLAUDE.md §9.3/§9.4/§9.9).

``propose_and_pr`` spawns a Sonnet coding sub-agent (the Claude Agent SDK) that writes
both toolkit halves (``lib/`` human-half + ``skills/TOOL_MANIFEST`` ai-half) and tests in
an **isolated git worktree** on ``research_toolkit``, then drives the bounded rework loop:

    L0  author self-correction (inside the SDK session, ``max_turns``)
    L1  escalation when blocked — web search · ask_lead · ask_human (an independent path
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

import inspect
import json
import subprocess
import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import yaml

from auto_research.agents.prompts import load_prompt
from auto_research.hitl import HumanHandler, HumanInputRequired, resolve_human
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


@lru_cache(maxsize=1)
def _orchestration_config() -> dict:
    path = Path(__file__).resolve().parents[3] / "config" / "orchestration.yaml"
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


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
            capture_output=True, text=True,
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
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()


def _is_dirty(root: Path) -> bool:
    return bool(
        subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout.strip()
    )


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
            f"authoring may have failed terminally and left edits for you to inspect (§9.9). "
            f"Review and clean before the next run, or re-run with resume=True to auto-heal:\n"
            f"  git -C {root} status\n"
            f"  git -C {root} restore . && git -C {root} clean -fd   # discard, if appropriate"
        )


def _resolve_coder_model() -> str:
    """Resolve the model id for the ``coder`` role from config (no hardcoded id, §6)."""
    from auto_research.models import resolve

    return resolve("coder").handles[0].config.model


def _resolve_coder_provider() -> str:
    """Resolve the ``coder`` (author) provider from config — the decorrelation key (§9.3).

    The co-lead reviewer is routed to a member whose provider differs from this, so the
    author's own model family never approves the merge candidate. No hardcoded string (§6).
    """
    from auto_research.models import resolve

    return resolve("coder").handles[0].config.provider


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
    return "\n\n".join(parts)


def _observation_paths() -> dict[str, str]:
    try:
        from auto_research.observations import load_observations_config

        cfg = load_observations_config()
    except Exception:
        return {}
    return {name: str(m.obs_db_path) for name, m in cfg.missions.items()}


# ----------------------------------------------------------------------------
# Monitor emit helpers — make the authoring flow (coder stream, escalations, gate/
# review/commit) legible in the run's JSONL. team="tooling"; no-op when monitor is None.
# ----------------------------------------------------------------------------

_CODER_TEXT_CAP = 2_000


def _cap_text(text: str, limit: int = _CODER_TEXT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit:,} chars]"


async def _emit(
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


# ----------------------------------------------------------------------------
# Escalation handlers (L1) — directly unit-testable, wrapped as MCP tools below
# ----------------------------------------------------------------------------


def _make_ask_lead_handler(lead: Any, *, monitor: Any | None = None) -> Callable[..., Awaitable[str]]:
    async def handler(message: str, *, kind: str = "clarify_code") -> str:
        answer = lead.clarify_for_author(message, kind=kind)
        if inspect.isawaitable(answer):
            answer = await answer
        answer = str(answer)
        await _emit(
            monitor, "coder.ask_lead", level=4,
            summary=f"coder asked the lead: {_cap_text(message, 80)}",
            data={"kind": kind, "message": _cap_text(message), "answer": _cap_text(answer)},
        )
        return answer

    return handler


def _make_ask_human_handler(
    human_handler: HumanHandler | None, *, model: str = "", monitor: Any | None = None
) -> Callable[..., Awaitable[str]]:
    async def handler(message: str, *, kind: str = "blocked") -> str:
        query = HumanQuery(
            kind=kind if kind in ("clarify_science", "clarify_code", "blocked") else "blocked",
            message=message or "(no message provided)",
            asked_by="coder",
            model=model or "",
        )
        await _emit(
            monitor, "coder.ask_human", level=3,
            summary=f"coder raised to the human ({query.kind}): {_cap_text(message, 80)}",
            data={"kind": query.kind, "message": _cap_text(message)},
        )
        response = await resolve_human(human_handler, query)  # raises HumanInputRequired if None
        await _emit(
            monitor, "coder.ask_human.answer", level=3,
            summary="human answered the coder",
            data={"answer": _cap_text(response.answer)},
        )
        return response.answer

    return handler


def _make_escalation_server(
    ask_lead: Callable[..., Awaitable[str]], ask_human: Callable[..., Awaitable[str]]
):
    """Build the in-process MCP server exposing ``ask_lead`` and ``ask_human`` (§9.7)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    _msg_schema = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Your question, in Markdown — the ask first, then any needed facts.",
            }
        },
        "required": ["message"],
    }

    @tool(
        "ask_lead",
        "Ask the analyst lead a code/convention/which-lib-function question (Markdown).",
        _msg_schema,
    )
    async def _ask_lead(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ask_lead(args["message"])
        return {"content": [{"type": "text", "text": answer}]}

    @tool(
        "ask_human",
        "Escalate a genuine expert-science unknown to the real human (Markdown).",
        _msg_schema,
    )
    async def _ask_human(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ask_human(args["message"])
        return {"content": [{"type": "text", "text": answer}]}

    return create_sdk_mcp_server("escalation", tools=[_ask_lead, _ask_human])


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
    )
    return worktree_path


def _git_in(worktree: Path, *args: str) -> str:
    """Run one git command inside ``worktree`` (runtime git — §9.9, not §3). Raises on
    non-zero exit with stderr in the message."""
    out = subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {worktree}: {out.stderr.strip()}")
    return out.stdout


def approve_edit(worktree: Path, *, message: str) -> str:
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
    from auto_research.sandbox.gate import collect_targets

    worktree = Path(worktree)
    rels = sorted(str(p.relative_to(worktree)) for p in collect_targets(worktree))
    _git_in(worktree, "add", "--", *rels)
    _git_in(worktree, "commit", "-m", message)
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
    if artifact_dir is not None:
        artifact_dir = Path(artifact_dir)
        ad, tr = artifact_dir.resolve(), toolkit_root.resolve()
        if ad != tr and tr not in ad.parents:
            _git_in(toolkit_root, "add", "-A")
            diff = _git_in(toolkit_root, "diff", "--cached", reset_to)
            if diff.strip():
                artifact_dir.mkdir(parents=True, exist_ok=True)
                patch_path = artifact_dir / f"{patch_prefix}_{spec_id}_{uuid.uuid4().hex[:8]}.patch"
                patch_path.write_text(diff, encoding="utf-8")
    _git_in(toolkit_root, "reset", "--hard", reset_to)
    _git_in(toolkit_root, "clean", "-fd")
    return patch_path


_BASE_REF_FILE = "current_base_ref"


def _base_ref_path(worktree_base: Path) -> Path:
    """Where the per-attempt authoring base-ref is persisted (under the run's authoring dir), so a
    resume after a hard-kill can reset the branch to exactly the killed attempt's start."""
    return Path(worktree_base) / _BASE_REF_FILE


def _read_persisted_base_ref(run_directory: Path) -> str | None:
    """Read the base-ref persisted by :func:`build_authoring_session` for the last attempt, or None."""
    try:
        text = _base_ref_path(Path(run_directory) / "authoring").read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


class BranchHealResult(NamedTuple):
    """Outcome of a resume-time authoring-branch heal (for a monitor event)."""

    healed: bool
    reason: str  # "clean" | "healed" | "not_git" | "wrong_branch" | "error: <repr>"
    base_ref: str | None = None
    patch_path: Path | None = None


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
    base-ref persisted by :func:`build_authoring_session` (``run_dir/authoring/current_base_ref``;
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
        if not _is_dirty(root):
            return BranchHealResult(False, "clean")
        if _current_branch(root) != branch:
            return BranchHealResult(False, "wrong_branch")
        base_ref = _read_persisted_base_ref(run_directory)
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


def _make_path_confiner(worktree: Path):
    """DEPRECATED — do not use for confinement (see CLAUDE.md "Loose ends").

    A ``can_use_tool`` callback that denied file writes outside the worktree. The official
    Claude Agent SDK permission flow runs ``can_use_tool`` **last** and **skips it** under
    ``permission_mode="acceptEdits"`` / when an ``allowed_tools`` rule matches — so it never
    fired here (it could not gate reads or Bash at all). Superseded by the ``PreToolUse`` hook
    :func:`_make_fs_guard_hook` (runs first, fires for every tool call) enforcing the shared
    :class:`~auto_research.sandbox.fs_guard.PathGuard` allow-list. Kept for reference; unwired.
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    write_tools = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
    root = worktree.resolve()

    async def can_use_tool(name: str, args: dict[str, Any], ctx: Any) -> Any:
        if name in write_tools:
            target = args.get("file_path") or args.get("path") or args.get("notebook_path")
            if target is not None:
                resolved = Path(str(target)).resolve()
                if resolved != root and root not in resolved.parents:
                    return PermissionResultDeny(message=f"path outside worktree: {target}")
        return PermissionResultAllow()

    return can_use_tool


_FILE_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep"}


def _make_fs_guard_hook(guard):
    """A Claude Agent SDK ``PreToolUse`` hook enforcing the shared filesystem allow-list
    (:class:`~auto_research.sandbox.fs_guard.PathGuard`) on the coder.

    The correct gate: ``PreToolUse`` runs **first** in the permission flow and fires for **every**
    tool call (unlike ``can_use_tool`` — see the deprecated :func:`_make_path_confiner`). File
    tools are checked by their path argument (exact); ``Bash`` by the absolute paths *named* in its
    command (best-effort — a subprocess's implicit reads are invisible to the hook; the complete
    fix is the OS-level jail, §12 / Loose ends). The system's ``auto_research`` repo and governance
    files are not on the allow-list, so they are denied (§2/§4). Fail-open on an unexpected internal
    error so a guard bug never bricks authoring.
    """

    async def hook(input_data, tool_use_id, context):  # noqa: ANN001 — SDK callback signature
        try:
            tool = input_data.get("tool_name", "")
            ti = input_data.get("tool_input") or {}
            denied: str | None = None
            if tool in _FILE_TOOLS:
                target = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
                if target is not None and not guard.is_allowed(target):
                    denied = str(target)
            elif tool == "Bash":
                denied = guard.forbidden_in_command(ti.get("command", ""))
            if denied is not None:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"{denied} is outside the coder's filesystem allow-list "
                            f"(research_toolkit / obsdb / analyst-env / HEASoft); the auto_research "
                            f"orchestration repo and governance files are off-limits (§2/§4)."
                        ),
                    }
                }
        except Exception:  # noqa: BLE001 — a guard bug must never brick authoring (fail-open)
            return {}
        return {}

    return hook


def _author_env(toolkit_root: Path) -> dict[str, str] | None:
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
    """
    import os

    cfg = _orchestration_config()
    analyze_env = cfg.get("analyze_env")
    ini_sh = cfg.get("analyze_ini_sh")
    if not analyze_env and not ini_sh:
        return None
    try:
        from auto_research.sandbox.runner import _source_ini_env

        env = dict(_source_ini_env(ini_sh)) if ini_sh else dict(os.environ)
    except Exception:  # noqa: BLE001 — inherit rather than fail the whole options build
        return None
    if analyze_env:
        env["PATH"] = f"{Path(analyze_env) / 'bin'}:" + env.get("PATH", "")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(toolkit_root) + (":" + existing_pp if existing_pp else "")
    return env


def _build_options(*, worktree: Path, coder_model: str, system_prompt: str, escalation_server, max_turns: int, env: dict[str, str] | None = None):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    from auto_research.sandbox.fs_guard import PathGuard

    # Filesystem jail: a PreToolUse hook (runs first, fires for every tool call) enforcing the
    # shared config allow-list. Replaces the deprecated can_use_tool confiner, which the SDK
    # skips under acceptEdits/allow-rules. Inactive when agent_fs_allow is unset (allow-all).
    fs_guard_hook = _make_fs_guard_hook(PathGuard.from_config())
    kwargs: dict[str, Any] = dict(
        cwd=str(worktree),
        model=coder_model,
        system_prompt=system_prompt,
        setting_sources=[],  # SDK-only: ignore any filesystem .claude/settings
        permission_mode="acceptEdits",
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="Read|Write|Edit|MultiEdit|NotebookEdit|Glob|Grep|Bash",
                    hooks=[fs_guard_hook],
                )
            ]
        },
        mcp_servers={"escalation": escalation_server},
        allowed_tools=[
            "Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "mcp__escalation__ask_lead", "mcp__escalation__ask_human",
        ],
        # The coder never issues git (§9.9): staging/commit is the runtime's job
        # (``approve_edit`` only, after gate-pass ∧ review-pass). Deny-rules are evaluated
        # first and apply under acceptEdits; prefix-matching is best-effort (pipes/subshells
        # can slip), so the per-attempt base-ref reset (``_reset_direct_tree``) is the robust
        # backstop that drops any commit a coder still lands. Scoped to the coder by being on
        # the coder's own options, not global settings.
        disallowed_tools=["Bash(git:*)", "Bash(git)"],
        max_turns=max_turns,
    )
    if env:  # full sourced env (analyst conda + HEASoft); omit when unset so the SDK inherits.
        kwargs["env"] = env
    return ClaudeAgentOptions(**kwargs)


def _default_sdk_client_factory(options):
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


async def _drive_author(client: Any, message: str, *, monitor: Any | None = None) -> str:
    """Send one turn to the warm author session and drain its response (L0 runs inside).

    When a ``monitor`` is wired, each streamed block is mirrored to the run's JSONL so the
    otherwise-opaque SDK author session is legible: tool calls (file edits, bash, ask_*),
    tool results, narration, and the per-turn end summary. Logging never breaks the drain.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    await client.query(message)
    chunks: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
                    if block.text.strip():
                        await _emit(monitor, "coder.text", level=8,
                                    summary=_cap_text(block.text.strip(), 80),
                                    data={"text": _cap_text(block.text)})
                elif isinstance(block, ToolUseBlock):
                    await _emit(
                        monitor, "coder.tool_use", level=7,
                        summary=f"coder → {block.name}",
                        data={"tool": block.name,
                              "input": _cap_text(json.dumps(block.input, default=str, sort_keys=True))},
                    )
        elif isinstance(msg, UserMessage):
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, ToolResultBlock):
                    text = block.content if isinstance(block.content, str) else json.dumps(
                        block.content, default=str
                    )
                    await _emit(
                        monitor, "coder.tool_result", level=7,
                        summary=f"coder tool result{' (error)' if block.is_error else ''}",
                        data={"is_error": bool(block.is_error), "text": _cap_text(text)},
                    )
        elif isinstance(msg, ResultMessage):
            await _emit(
                monitor, "coder.turn_end", level=5,
                summary=f"coder turn ended ({msg.num_turns} turns)",
                data={"num_turns": msg.num_turns, "stop_reason": getattr(msg, "stop_reason", None),
                      "cost_usd": getattr(msg, "total_cost_usd", None),
                      "is_error": bool(getattr(msg, "is_error", False))},
            )
    return "\n".join(chunks)


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
        client: Any,
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
    ) -> None:
        self.tool_spec = tool_spec
        self.worktree = worktree
        self.branch = branch
        self.committed = False  # flips True only after commit() lands; gates the failure reset
        # Lead-driven loop state (§9.3 B-topology): the most recent gate/review/commit, so a
        # caller (the lead's drive_authoring loop) can build the terminal MergeFlowResult from
        # session state; submit_cap bounds the expensive submit_for_review calls.
        self.submit_cap = submit_cap
        self.submits_used = 0
        self.debate_cap = debate_cap  # bounds respond_to_review (lead↔co-lead) turns
        self.debates_used = 0
        self.last_gate: GateReport | None = None
        self.last_review: ToolReviewVerdict | None = None
        self.last_commit_sha: str | None = None
        self._client = client
        self._base_ref = base_ref
        self._structure_context = structure_context
        self._run_gate = run_gate
        self._run_review = run_review
        self._approve_edit = approve_edit
        self._direct_branch = direct_branch
        self._toolkit_root = toolkit_root
        self._worktree_base = worktree_base
        self._monitor = monitor

    @property
    def initial_message(self) -> str:
        """The seed turn: the ToolSpec brief + runtime-safe local structure context."""
        return _initial_message(self.tool_spec, self._structure_context)

    async def connect(self) -> None:
        await self._client.connect()
        await _emit(
            self._monitor, "coder.session_start", level=4,
            summary=f"authoring {self.tool_spec.proposed_tool_name} on branch {self.branch}",
            data={"tool_spec_id": self.tool_spec.id, "tool": self.tool_spec.proposed_tool_name,
                  "branch": self.branch, "disposition": self.tool_spec.disposition},
        )

    async def drive(self, message: str) -> str:
        """Send one instruction to the warm coder session and drain its response (L0 inside)."""
        return await _drive_author(self._client, message, monitor=self._monitor)

    async def run_gate_step(self) -> GateReport:
        """Run the deterministic gate over the current tree (§9.4). First applies the §9.9
        backstop: if the coder slipped in a commit (HEAD moved off this attempt's base), undo it
        with ``reset --mixed`` so its work returns to the working tree and the scans see it. The
        gate is domain-blind and mechanical — a lead can only *satisfy* it (via :meth:`drive`),
        never argue it."""
        await _emit(self._monitor, "gate.start", level=5, summary="deterministic gate running")
        if self._base_ref is not None:
            head = _git_in(self.worktree, "rev-parse", "HEAD").strip()
            if head != self._base_ref:
                try:
                    _git_in(self.worktree, "reset", "--mixed", self._base_ref)
                except RuntimeError:
                    pass
                await _emit(
                    self._monitor, "coder.committed", level=4,
                    summary="coder ran git itself — auto-reverted the commit to uncommitted changes",
                    data={"head_was": head, "base_ref": self._base_ref},
                )
        g = await self._run_gate(self.worktree, self.tool_spec)
        failed = [f"[{c.layer}] {c.name}" for c in (*g.layer_a, *g.layer_b) if not c.passed]
        await _emit(
            self._monitor, "gate.result", level=4,
            summary=f"gate {'passed' if g.passed else 'rejected'}",
            data={"passed": g.passed, "failing_checks": failed, "report_hash": g.report_hash,
                  "reason": _cap_text(g.reason)},
        )
        self.last_gate = g
        return g

    async def run_review_step(
        self, gate: GateReport, *, lead_rebuttal: str | None = None
    ) -> ToolReviewVerdict:
        """Run the provider-decorrelated co-lead review over the diff + gate result (§9.3 step 4).
        The review is *debatable* (unlike the gate): when ``lead_rebuttal`` is set the co-lead
        **reconsiders** its prior verdict (``self.last_review``) in light of the lead's argument
        (the §9.3 debate) — but ``commit ⇔ gate-pass ∧ review-pass`` holds; the lead persuades,
        never overrides."""
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
            data={"approved": review.approved, "issues": [_cap_text(i, 200) for i in review.issues],
                  "checks_adequate": review.checks_adequate, "rationale": _cap_text(review.rationale)},
        )
        self.last_review = review
        return review

    async def commit(self, message: str) -> str:
        """Land one atomic commit (§9.9 ``approve_edit``) — the **only** place the runtime
        commits. Caller guarantees gate-pass ∧ review-pass. Sets :attr:`committed` so
        :meth:`aclose` skips the failure-path tree reset."""
        commit_sha = self._approve_edit(self.worktree, message=message)
        self.committed = True
        self.last_commit_sha = commit_sha
        await _emit(
            self._monitor, "commit", level=3,
            summary=f"committed {self.tool_spec.proposed_tool_name} → {commit_sha[:10]}",
            data={"commit_sha": commit_sha, "branch": self.branch,
                  "message": _cap_text(message, 200)},
        )
        return commit_sha

    async def aclose(self) -> None:
        """Disconnect the coder; on a non-committed direct-branch attempt, reset the tree to the
        per-attempt base ref (§9.9 failure counterpart to ``approve_edit`` — saves a patch
        artifact first, drops any coder-landed commit, leaves a clean tree at the right base)."""
        await self._client.disconnect()
        if self._direct_branch is not None and not self.committed:
            await _reset_direct_tree(
                self._toolkit_root, base_ref=self._base_ref, artifact_dir=self._worktree_base,
                monitor=self._monitor, tool_spec=self.tool_spec,
            )


def build_authoring_session(
    tool_spec: ToolSpec,
    *,
    lead: Any,
    toolkit_root: Path,
    worktree_base: Path,
    colead: Any | None = None,
    human_handler: HumanHandler | None = None,
    coder_model: str | None = None,
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
    """Resolve config + hooks, choose worktree/direct-branch mode (§9.9), build the Claude-SDK
    coder client (seeded via ``coder`` prompt + escalation MCP tools), and return a **not yet
    connected** :class:`AuthoringSession`. Setup only — the sole git touches are the
    direct-branch clean-tree precondition and the per-attempt base-ref record. ``await connect()``
    on the returned session before driving it."""
    toolkit_root = Path(toolkit_root)
    worktree_base = Path(worktree_base)
    coder_model = coder_model or _resolve_coder_model()
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
            colead=colead, author_provider=_resolve_coder_provider(), monitor=monitor
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
    else:
        branch = f"tool/{tool_spec.proposed_tool_name}-{uuid.uuid4().hex[:8]}"
        worktree = worktree_factory(toolkit_root, worktree_base, branch)

    structure_context = _build_structure_context(worktree)
    system_prompt = load_prompt("coder")
    escalation_server = _make_escalation_server(
        _make_ask_lead_handler(lead, monitor=monitor),
        _make_ask_human_handler(human_handler, model=coder_model, monitor=monitor),
    )
    options = _build_options(
        worktree=worktree, coder_model=coder_model, system_prompt=system_prompt,
        escalation_server=escalation_server, max_turns=max_turns,
        env=_author_env(toolkit_root),  # author's Bash == analyst/gate code-execution env
    )
    client = sdk_client_factory(options)
    return AuthoringSession(
        tool_spec=tool_spec, client=client, worktree=worktree, branch=branch, base_ref=base_ref,
        structure_context=structure_context, run_gate=run_gate, run_review=run_review,
        approve_edit=approve_edit, direct_branch=direct_branch, toolkit_root=toolkit_root,
        worktree_base=worktree_base, submit_cap=submit_cap, debate_cap=debate_cap, monitor=monitor,
    )


async def propose_and_pr(
    tool_spec: ToolSpec,
    *,
    lead: Any,
    toolkit_root: Path,
    worktree_base: Path,
    colead: Any | None = None,
    human_handler: HumanHandler | None = None,
    coder_model: str | None = None,
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

    ``colead`` is the GPT co-lead reviewer (an ``AnalystColeadAgent``); when ``None`` the
    default ``run_review`` builds one from config, routed to a member whose provider differs
    from the Claude author's (``review.make_review_hook``). Injectable seams
    (``run_gate``/``run_review``/``worktree_factory``/``sdk_client_factory``/``approve_edit``)
    let the loop and the commit be exercised deterministically without a live LLM or git.
    """
    gate_rework_cap = gate_rework_cap if gate_rework_cap is not None else _cap("tool_author_gate_rework_cap", 3)
    review_rework_cap = review_rework_cap if review_rework_cap is not None else _cap("tool_author_review_rework_cap", 2)
    session = build_authoring_session(
        tool_spec, lead=lead, toolkit_root=toolkit_root, worktree_base=worktree_base,
        colead=colead, human_handler=human_handler, coder_model=coder_model,
        run_gate=run_gate, run_review=run_review, max_turns=max_turns, direct_branch=direct_branch,
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
