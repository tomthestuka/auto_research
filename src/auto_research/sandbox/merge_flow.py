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
from auto_research.sandbox.coder.events import cap_text, coder_text_cap, emit
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
# review/commit) legible in the run's JSONL. Defined in ``coder.events`` so an author
# harness records under the same names without importing the flow that drives it.
# ----------------------------------------------------------------------------

_coder_text_cap = coder_text_cap
_cap_text = cap_text
_emit = emit


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
    "2. Ask the analyst lead with `ask_lead` for the domain or convention call — the lead is "
    "reachable and knows this codebase.\n"
    "3. Reason to the best answer the evidence supports, and state the assumption you made — "
    "in the code, and in the test that covers it.\n"
    "When it stays genuinely unresolvable after all three, say so plainly and stop."
)


def _make_ask_human_handler(
    human_handler: HumanHandler | None, *, model: str = "", monitor: Any | None = None
) -> Callable[..., Awaitable[str]]:
    """Serve the author's ``ask_human`` and classify what happens when it cannot be answered.

    Both harnesses expose ``ask_human`` over MCP, and every MCP stack turns an exception raised
    in a tool handler into an ``isError`` result carrying only ``str(exc)``. That flattens three
    different situations into one shape the author cannot tell apart, so the classification has
    to happen HERE, before the boundary:

    * **unattended** (no handler wired, the §9.8 headless fail-safe) — expected, and the author
      is told what to do instead (``UNATTENDED_GUIDANCE``); ledger event ``coder.ask_human.unattended``.
    * **channel fault** (the connector raised, or returned the wrong type) — a real fault that
      used to be indistinguishable from an unattended run in both the author's view and the
      ledger; ledger event ``coder.ask_human.error``, and the author is pointed at ``ask_lead``.

    Both re-raise so the call stays a tool **error**: an unanswered question is never dressed up
    as an answer, and no ``coder.ask_human.answer`` event is written for either.
    """
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
        try:
            response = await resolve_human(human_handler, query)
        except HumanInputRequired as exc:
            await _emit(
                monitor, "coder.ask_human.unattended", level=3,
                summary="coder raised to a human on an unattended run; told to ground it itself",
                data={"kind": query.kind, "headline": query.headline},
            )
            raise HumanInputRequired(UNATTENDED_GUIDANCE) from exc
        except Exception as exc:  # noqa: BLE001 — a broken channel is a fault, not an absent human
            await _emit(
                monitor, "coder.ask_human.error", level=1,
                summary=f"the human channel failed for the coder: {type(exc).__name__}",
                data={"kind": query.kind, "headline": query.headline,
                      "error": f"{type(exc).__name__}: {_cap_text(str(exc), 500)}"},
            )
            raise RuntimeError(
                f"the human channel failed ({type(exc).__name__}: {exc}), so this question "
                "reached nobody. The analyst lead is still reachable — use `ask_lead` — and the "
                "web search tools still work; ground the decision from those and state the "
                "assumption you made."
            ) from exc
        await _emit(
            monitor, "coder.ask_human.answer", level=3,
            summary="human answered the coder",
            data={"answer": _cap_text(response.answer)},
        )
        return response.answer

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
        return state if isinstance(state, dict) and state.get("submits") else None
    except (OSError, ValueError):
        return None


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
    (snap_dir / patch_name).write_text(diff, encoding="utf-8")
    entry = {"index": index, "patch": patch_name, "gate_passed": None, "review_approved": None,
             "review_issues": []}
    state["submits"].append(entry)
    _write_snapshot_state(snap_dir, state)
    return entry


def _update_last_submission(worktree_base: Path, tool_name: str, **fields: Any) -> None:
    """Record a gate/review outcome onto the latest submission's state (best-effort)."""
    snap_dir = _submissions_dir(Path(worktree_base), tool_name)
    state = _load_snapshot_state(snap_dir)
    if state is None:
        return
    state["submits"][-1].update(fields)
    try:
        _write_snapshot_state(snap_dir, state)
    except OSError:
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
    patch = snap_dir / state["submits"][-1]["patch"]
    return (patch, state) if patch.is_file() else None


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
    issues = entry.get("review_issues") or []
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
    ) -> None:
        self.tool_spec = tool_spec
        self.worktree = worktree
        self.branch = branch
        self.committed = False  # flips True only after commit() lands; gates the failure reset
        self._closed = False  # aclose() is idempotent — the interrupt path may beat the finally
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
        whichever one it is."""
        return await self._backend.drive(message)

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
        g = await self._run_gate(self.worktree, self.tool_spec)
        failed = [f"[{c.layer}] {c.name}" for c in (*g.layer_a, *g.layer_b) if not c.passed]
        await _emit(
            self._monitor, "gate.result", level=4,
            summary=f"gate {'passed' if g.passed else 'rejected'}",
            data={"passed": g.passed, "failing_checks": failed, "report_hash": g.report_hash,
                  "reason": _cap_text(g.reason)},
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
        if self._base_ref is not None:
            _update_last_submission(
                self._worktree_base, self.tool_spec.proposed_tool_name,
                review_approved=review.approved,
                review_issues=[_cap_text(i, 300) for i in review.issues],
            )
        return review

    async def commit(self, message: str) -> str:
        """Land one atomic commit (§9.9 ``approve_edit``) — the **only** place the runtime
        commits. Caller guarantees gate-pass ∧ review-pass. Sets :attr:`committed` so
        :meth:`aclose` skips the failure-path tree reset."""
        commit_sha = self._approve_edit(self.worktree, message=message)
        self.committed = True
        self.last_commit_sha = commit_sha
        # The committed work now lives in git; the snapshot record has nothing left to restore.
        discard_submission_snapshots(self._worktree_base, self.tool_spec.proposed_tool_name)
        await _emit(
            self._monitor, "commit", level=3,
            summary=f"committed {self.tool_spec.proposed_tool_name} → {commit_sha[:10]}",
            data={"tool": self.tool_spec.proposed_tool_name, "commit_sha": commit_sha,
                  "branch": self.branch, "message": _cap_text(message, 200)},
        )
        return commit_sha

    async def aclose(self) -> None:
        """Disconnect the coder; on a non-committed direct-branch attempt, reset the tree to the
        per-attempt base ref (§9.9 failure counterpart to ``approve_edit`` — saves a patch
        artifact first, drops any coder-landed commit, leaves a clean tree at the right base).

        Idempotent: a human interrupt closes the session from shielded cleanup (its normal
        ``finally`` is inside the cancelled scope), and the ``finally`` may still run afterwards —
        a second reset would discard a base ref's worth of nothing, but the disconnect and the
        patch artifact would be repeated for no reason.
        """
        if self._closed:
            return
        self._closed = True
        await self._backend.disconnect()
        if self._direct_branch is not None and not self.committed:
            await _reset_direct_tree(
                self._toolkit_root, base_ref=self._base_ref, artifact_dir=self._worktree_base,
                monitor=self._monitor, tool_spec=self.tool_spec,
            )


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
    """
    coder_model = member.model
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
            escalation=EscalationServer(ask_lead, ask_human),
            env=_author_env(toolkit_root),  # author's shell == analyst/gate execution env
            writable_roots=roots,
            monitor=monitor,
        )

    options = _build_options(
        worktree=worktree, coder_model=coder_model, system_prompt=system_prompt,
        escalation_server=_make_escalation_server(ask_lead, ask_human), max_turns=max_turns,
        env=_author_env(toolkit_root),  # author's Bash == analyst/gate code-execution env
        worktree_base=worktree_base,    # where the jail launcher is written, when the jail is on
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
    ask_lead = _make_ask_lead_handler(lead, monitor=monitor)
    ask_human = _make_ask_human_handler(human_handler, model=coder_model, monitor=monitor)
    backend = _build_backend(
        member=coder,
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
    return AuthoringSession(
        tool_spec=tool_spec, backend=backend, worktree=worktree, branch=branch, base_ref=base_ref,
        structure_context=structure_context, run_gate=run_gate, run_review=run_review,
        approve_edit=approve_edit, direct_branch=direct_branch, toolkit_root=toolkit_root,
        worktree_base=worktree_base, submit_cap=submit_cap, debate_cap=debate_cap, monitor=monitor,
        coder_model=coder_model, restored_submission=restored_submission,
    )


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
