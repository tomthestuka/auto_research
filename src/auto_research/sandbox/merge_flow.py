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
is real as of step 4 (``review.make_review_hook``, the default ``run_review`` hook). Both
remain injectable so the loop is exercised deterministically in tests. ``approve_edit`` — the
only place git commits — is step 6; here a successful run returns a placeholder ``commit_sha``
and lands nothing. The runtime never touches master; a human merges out of band.
"""

from __future__ import annotations

import inspect
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
# Escalation handlers (L1) — directly unit-testable, wrapped as MCP tools below
# ----------------------------------------------------------------------------


def _make_ask_lead_handler(lead: Any) -> Callable[..., Awaitable[str]]:
    async def handler(question: str, *, kind: str = "clarify_code", context: str = "") -> str:
        answer = lead.clarify_for_author(question, kind=kind, context=context)
        if inspect.isawaitable(answer):
            answer = await answer
        return str(answer)

    return handler


def _make_ask_human_handler(human_handler: HumanHandler | None) -> Callable[..., Awaitable[str]]:
    async def handler(question: str, *, kind: str = "blocked", context: str = "") -> str:
        query = HumanQuery(
            kind=kind if kind in ("clarify_science", "clarify_code", "blocked") else "blocked",
            question=question or "(no question provided)",
            context=context,
            asked_by="coder",
        )
        response = await resolve_human(human_handler, query)  # raises HumanInputRequired if None
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


def _build_options(*, worktree: Path, coder_model: str, system_prompt: str, escalation_server, max_turns: int):
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
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


def _default_sdk_client_factory(options):
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)


async def _drive_author(client: Any, message: str) -> str:
    """Send one turn to the warm author session and drain its response (L0 runs inside)."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    await client.query(message)
    chunks: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in getattr(msg, "content", []) or []:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
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
    worktree_factory: Callable[[Path, Path, str], Path] | None = None,
    sdk_client_factory: Callable[[Any], Any] | None = None,
) -> MergeFlowResult:
    """Author a tool from a ``ToolSpec`` and drive the bounded rework loop (§9.3).

    Returns a :class:`MergeFlowResult`. ``committed`` ⇔ gate-pass ∧ co-lead review-pass
    (the commit itself is step 6 — here ``commit_sha`` is a placeholder). ``failed`` /
    ``needs_human`` ⇒ no commit and the lead raises to the human (§9.7). Raises
    :class:`HumanInputRequired` if the author needs a human and none is wired (headless
    fail-safe).

    ``colead`` is the GPT co-lead reviewer (an ``AnalystColeadAgent``); when ``None`` the
    default ``run_review`` builds one from config, routed to a member whose provider differs
    from the Claude author's (``review.make_review_hook``). Injectable seams
    (``run_gate``/``run_review``/``worktree_factory``/``sdk_client_factory``) let the loop be
    exercised deterministically without a live LLM.
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

    branch = f"tool/{tool_spec.proposed_tool_name}-{uuid.uuid4().hex[:8]}"
    worktree = worktree_factory(toolkit_root, worktree_base, branch)

    structure_context = _build_structure_context(toolkit_root)
    system_prompt = load_prompt("coder")
    escalation_server = _make_escalation_server(
        _make_ask_lead_handler(lead), _make_ask_human_handler(human_handler)
    )
    options = _build_options(
        worktree=worktree, coder_model=coder_model, system_prompt=system_prompt,
        escalation_server=escalation_server, max_turns=max_turns,
    )
    client = sdk_client_factory(options)

    await client.connect()
    try:
        await _drive_author(client, _initial_message(tool_spec, structure_context))

        gate_attempts = 0
        review_attempts = 0
        gate = await run_gate(worktree, tool_spec)
        while True:
            if not gate.passed:
                if gate_attempts >= gate_rework_cap:
                    return MergeFlowResult(
                        tool_spec_id=tool_spec.id, status="failed", branch=branch,
                        gate_report=gate, reason=f"gate rework cap exhausted: {gate.reason}",
                    )
                gate_attempts += 1
                await _drive_author(client, _gate_feedback_message(gate))
                gate = await run_gate(worktree, tool_spec)
                continue

            review = await run_review(worktree, tool_spec, gate)
            if not review.approved:
                if review_attempts >= review_rework_cap:
                    return MergeFlowResult(
                        tool_spec_id=tool_spec.id, status="failed", branch=branch,
                        gate_report=gate, review=review,
                        reason=f"review rework cap exhausted: {review.rationale}",
                    )
                review_attempts += 1
                await _drive_author(client, _review_feedback_message(review))
                # A code change can break the gate — re-run it before re-review.
                gate = await run_gate(worktree, tool_spec)
                continue

            return MergeFlowResult(
                tool_spec_id=tool_spec.id, status="committed", branch=branch,
                commit_sha="<placeholder: approve_edit lands the commit in step 6>",
                gate_report=gate, review=review,
                reason="gate-pass and co-lead review-pass",
            )
    finally:
        await client.disconnect()
