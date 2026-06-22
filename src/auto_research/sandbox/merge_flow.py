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
from typing import Any

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


def check_toolkit_branch_clean(root: Path | None = None, branch: str | None = None) -> None:
    """Launch-time preflight (no LLM): assert ``research_toolkit`` is on the authoring branch
    and has a clean working tree (CLAUDE.md §9.9, step 7).

    Direct-branch authoring commits straight onto ``branch`` (default ``ai``) in
    ``toolkit_root``. A wrong branch would commit authored tools to the wrong place; a dirty
    tree means a **terminal** authoring failure left edits behind (we never reset — §9.4) and
    the human must inspect/clean before the next run, lest unrelated changes get swept into the
    next atomic commit. Tolerant when ``root`` isn't a git checkout (mirrors
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
        raise RuntimeError(
            f"research_toolkit ({root}) has uncommitted changes on {branch!r}. A prior tool "
            f"authoring may have failed terminally and left edits for you to inspect (§9.9). "
            f"Review and clean before the next run:\n"
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


def _build_structure_context(toolkit_root: Path) -> str:
    """Assemble the local-structure context the author is seeded with.

    Runtime-safe sources only (CLAUDE.md §4): the real ``xspec_utils`` template layout +
    its top-level source, plus the configured mission observation paths. Never reads any
    ``DOCS.md``/``CLAUDE.md``.
    """
    parts: list[str] = []
    template = Path(toolkit_root) / "xspec_utils"
    if template.is_dir():
        tree = sorted(
            str(p.relative_to(toolkit_root))
            for p in template.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )
        parts.append("# Template package layout (mirror this exactly):\n" + "\n".join(tree))
        for rel in ("xspec_utils/__init__.py", "xspec_utils/skills/__init__.py"):
            src = Path(toolkit_root) / rel
            if src.is_file():
                parts.append(f"# {rel}\n```python\n{src.read_text(encoding='utf-8')}\n```")

    obs = _observation_paths()
    if obs:
        lines = "\n".join(f"- {mission}: {path}" for mission, path in obs.items())
        parts.append(
            "# Mission observation paths (validation ObsIDs live under these; synthetic "
            "fixtures are marked `synth…` ObsIDs, never toolkit code):\n" + lines
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
    async def handler(question: str, *, kind: str = "clarify_code", context: str = "") -> str:
        answer = lead.clarify_for_author(question, kind=kind, context=context)
        if inspect.isawaitable(answer):
            answer = await answer
        answer = str(answer)
        await _emit(
            monitor, "coder.ask_lead", level=4,
            summary=f"coder asked the lead: {_cap_text(question, 80)}",
            data={"kind": kind, "question": _cap_text(question), "context": _cap_text(context),
                  "answer": _cap_text(answer)},
        )
        return answer

    return handler


def _make_ask_human_handler(
    human_handler: HumanHandler | None, *, model: str = "", monitor: Any | None = None
) -> Callable[..., Awaitable[str]]:
    async def handler(question: str, *, kind: str = "blocked", context: str = "") -> str:
        query = HumanQuery(
            kind=kind if kind in ("clarify_science", "clarify_code", "blocked") else "blocked",
            question=question or "(no question provided)",
            context=context,
            asked_by="coder",
            model=model or "",
        )
        await _emit(
            monitor, "coder.ask_human", level=3,
            summary=f"coder raised to the human ({query.kind}): {_cap_text(question, 80)}",
            data={"kind": query.kind, "question": _cap_text(question), "context": _cap_text(context)},
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

    _q_schema = {
        "type": "object",
        "properties": {"question": {"type": "string"}, "context": {"type": "string"}},
        "required": ["question"],
    }

    @tool("ask_lead", "Ask the analyst lead a code/convention/which-lib-function question.", _q_schema)
    async def _ask_lead(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ask_lead(args["question"], context=args.get("context", ""))
        return {"content": [{"type": "text", "text": answer}]}

    @tool("ask_human", "Escalate a genuine expert-science unknown to the real human.", _q_schema)
    async def _ask_human(args: dict[str, Any]) -> dict[str, Any]:
        answer = await ask_human(args["question"], context=args.get("context", ""))
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


# Captured so ``propose_and_pr``'s ``approve_edit`` parameter (a test seam) can default to
# the real implementation without the local name shadowing the module-level function.
_DEFAULT_APPROVE_EDIT = approve_edit


def _make_path_confiner(worktree: Path):
    """A ``can_use_tool`` callback denying file writes outside the worktree (best-effort).

    OS-level jailing is deferred (§12); the worktree cwd is already isolated under the
    runtime data dir. This is a backstop against an edit/write escaping the worktree.
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
    from claude_agent_sdk import ClaudeAgentOptions

    kwargs: dict[str, Any] = dict(
        cwd=str(worktree),
        model=coder_model,
        system_prompt=system_prompt,
        setting_sources=[],  # SDK-only: ignore any filesystem .claude/settings
        permission_mode="acceptEdits",
        can_use_tool=_make_path_confiner(worktree),
        mcp_servers={"escalation": escalation_server},
        allowed_tools=[
            "Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "mcp__escalation__ask_lead", "mcp__escalation__ask_human",
        ],
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
    detail = "\n".join(f"- [{c.layer}] {c.name}: {c.detail}" for c in failed) or gate.reason
    return (
        "The deterministic gate REJECTED your tool. Fix these and re-run pytest:\n"
        f"{detail}\n\nDo not fabricate or weaken a check to pass — fix the cause."
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
    isolated worktree, and a terminal failure leaves the tree dirty for the human. ``None``
    keeps the isolated-worktree behaviour (tests/future use).

    ``colead`` is the GPT co-lead reviewer (an ``AnalystColeadAgent``); when ``None`` the
    default ``run_review`` builds one from config, routed to a member whose provider differs
    from the Claude author's (``review.make_review_hook``). Injectable seams
    (``run_gate``/``run_review``/``worktree_factory``/``sdk_client_factory``/``approve_edit``)
    let the loop and the commit be exercised deterministically without a live LLM or git.
    """
    toolkit_root = Path(toolkit_root)
    worktree_base = Path(worktree_base)
    coder_model = coder_model or _resolve_coder_model()
    if run_gate is None:
        from auto_research.sandbox.gate import run_gate as _real_gate

        run_gate = _real_gate
    if run_review is None:
        from auto_research.sandbox.review import make_review_hook

        run_review = make_review_hook(colead=colead, author_provider=_resolve_coder_provider())
    gate_rework_cap = gate_rework_cap if gate_rework_cap is not None else _cap("tool_author_gate_rework_cap", 3)
    review_rework_cap = review_rework_cap if review_rework_cap is not None else _cap("tool_author_review_rework_cap", 2)
    max_turns = max_turns if max_turns is not None else _cap("tool_author_max_turns", 50)
    worktree_factory = worktree_factory or _create_worktree
    sdk_client_factory = sdk_client_factory or _default_sdk_client_factory
    approve_edit = approve_edit or _DEFAULT_APPROVE_EDIT

    # Direct-branch mode (runtime authoring, §9.9): the author works in ``toolkit_root`` on the
    # ambient ``ai`` branch instead of an isolated worktree. The runtime is passive about
    # checkout — it asserts the precondition (also enforced at launch by the preflight) and
    # never switches branches. On terminal failure the dirty tree is left for the human (no
    # reset). The isolated-worktree path stays the default for tests + future use.
    if direct_branch is not None:
        check_toolkit_branch_clean(toolkit_root, direct_branch)
        branch = direct_branch
        worktree = toolkit_root
    else:
        branch = f"tool/{tool_spec.proposed_tool_name}-{uuid.uuid4().hex[:8]}"
        worktree = worktree_factory(toolkit_root, worktree_base, branch)

    structure_context = _build_structure_context(toolkit_root)
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

    async def _gated() -> GateReport:
        await _emit(monitor, "gate.start", level=5, summary="deterministic gate running")
        g = await run_gate(worktree, tool_spec)
        failed = [f"[{c.layer}] {c.name}" for c in (*g.layer_a, *g.layer_b) if not c.passed]
        await _emit(
            monitor, "gate.result", level=4,
            summary=f"gate {'passed' if g.passed else 'rejected'}",
            data={"passed": g.passed, "failing_checks": failed, "report_hash": g.report_hash,
                  "reason": _cap_text(g.reason)},
        )
        return g

    await client.connect()
    try:
        await _emit(
            monitor, "coder.session_start", level=4,
            summary=f"authoring {tool_spec.proposed_tool_name} on branch {branch}",
            data={"tool_spec_id": tool_spec.id, "tool": tool_spec.proposed_tool_name,
                  "branch": branch, "disposition": tool_spec.disposition},
        )
        await _drive_author(client, _initial_message(tool_spec, structure_context), monitor=monitor)

        gate_attempts = 0
        review_attempts = 0
        gate = await _gated()
        while True:
            if not gate.passed:
                if gate_attempts >= gate_rework_cap:
                    return MergeFlowResult(
                        tool_spec_id=tool_spec.id, status="failed", branch=branch,
                        gate_report=gate, reason=f"gate rework cap exhausted: {gate.reason}",
                    )
                gate_attempts += 1
                await _drive_author(client, _gate_feedback_message(gate), monitor=monitor)
                gate = await _gated()
                continue

            review = await run_review(worktree, tool_spec, gate)
            await _emit(
                monitor, "review.result", level=4,
                summary=f"co-lead {'approved' if review.approved else 'rejected'}",
                data={"approved": review.approved, "issues": [_cap_text(i, 200) for i in review.issues],
                      "checks_adequate": review.checks_adequate, "rationale": _cap_text(review.rationale)},
            )
            if not review.approved:
                if review_attempts >= review_rework_cap:
                    return MergeFlowResult(
                        tool_spec_id=tool_spec.id, status="failed", branch=branch,
                        gate_report=gate, review=review,
                        reason=f"review rework cap exhausted: {review.rationale}",
                    )
                review_attempts += 1
                await _drive_author(client, _review_feedback_message(review), monitor=monitor)
                # A code change can break the gate — re-run it before re-review.
                gate = await _gated()
                continue

            # Commit ⇔ gate-pass ∧ co-lead review-pass (§9.9). The co-lead drafts the commit
            # subject; fall back to the tool name if it left it empty.
            message = review.commit_summary.strip() or tool_spec.proposed_tool_name
            commit_sha = approve_edit(worktree, message=message)
            await _emit(
                monitor, "commit", level=3,
                summary=f"committed {tool_spec.proposed_tool_name} → {commit_sha[:10]}",
                data={"commit_sha": commit_sha, "branch": branch, "message": _cap_text(message, 200)},
            )
            return MergeFlowResult(
                tool_spec_id=tool_spec.id, status="committed", branch=branch,
                commit_sha=commit_sha, gate_report=gate, review=review,
                reason="gate-pass and co-lead review-pass",
            )
    except HumanInputRequired:
        raise  # §9.8 fail-safe must reach the caller — never silently become a "failed".
    except Exception as exc:  # noqa: BLE001
        # A gate/author/review error (e.g. a git hiccup, a decode error) is an honest failure,
        # not a reason to crash the whole research run. The lead surfaces it per §9.6/§9.7.
        return MergeFlowResult(
            tool_spec_id=tool_spec.id, status="failed", branch=branch,
            reason=f"authoring aborted by unexpected error: {type(exc).__name__}: {exc}",
        )
    finally:
        await client.disconnect()
