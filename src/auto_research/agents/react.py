"""Tool providers and outcome types for the agent ReAct loop.

`AgentRole._react` ([roles.py](roles.py)) drives a provider-agnostic inner loop:
it calls the model with `tool_definitions()`, executes each returned call via
`execute(...)`, and feeds the results back so the model chains dependent calls to
completion. A `ToolProvider` is the uniform interface behind which each role's tool
surface lives — the analyst's science sandbox here (`SandboxToolProvider`); the
reviewer/lead read-only tools and the librarian's sources are added alongside.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from auto_research.observations import load_observations_config
from auto_research.sandbox.fs_guard import GOVERNANCE_BASENAMES, PathGuard
from auto_research.schemas import DocumentRef, FigureRef, ToolResult, document_from_file


# Figure-output arguments, by naming convention → the extension the toolkit expects.
# A toolkit tool exposing one of these args has its figure written under the worker's
# figures_dir (see SandboxToolProvider._redirect_figure_args). ``""`` = base/prefix path
# (the tool appends its own suffix, e.g. ``_DU1.png``); ``.png`` = full file path.
# Convention-keyed (not tool-keyed) so new toolkits inherit it for free.
_FIGURE_PATH_ARGS: dict[str, str] = {"save_path": ".png", "save_path_prefix": ""}

# Cap on distinct PDFs the reviewer may open in one ReAct loop. Each opened PDF is
# re-attached to every subsequent model call, so this bounds the re-sent token cost.
_OPEN_PAPER_CAP = 6


@dataclass
class ToolOutcome:
    """Result of one tool execution, as fed back into the ReAct conversation.

    ``text`` is what the model sees next turn; ``result`` is the full record (for
    memory/figure propagation) when the provider produces one. ``documents`` are
    attachments (e.g. a PDF opened via ``open_paper``) that the loop accumulates and
    re-attaches to subsequent model calls so the model can read them multimodally.
    """

    text: str
    is_error: bool = False
    figures: list[FigureRef] = field(default_factory=list)
    documents: list[DocumentRef] = field(default_factory=list)
    result: ToolResult | None = None


class ToolProvider(Protocol):
    """Uniform tool surface for the ReAct loop: describe tools, execute one."""

    def tool_definitions(self) -> list[dict]:
        """Provider-neutral ``{name, description, input_schema}`` definitions."""

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Execute the named tool with ``arguments`` and return its outcome."""


# ----------------------------------------------------------------------------
# Raise-to-human — one ask_human tool, shared by every ReAct role (§9.7/§9.8).
# ----------------------------------------------------------------------------

ASK_HUMAN_TOOL = "ask_human"
_ASK_HUMAN_KINDS = ("clarify_science", "clarify_code", "blocked")

# Lead-only design channel (§5.5): the analyst lead settles a toolkit design WITH the human
# before authoring. It routes through the same human_handler as ask_human but is **budget-free**
# in the ReAct loop (see AgentRole._react free_tools), so the lead can debate the design across
# as many rounds as it needs to converge without starving its evidence-gathering iterations.
PLAN_AND_DEBATE_TOOL = "plan_and_debate"


def ask_human_tool_def() -> dict:
    """The provider-neutral definition for the shared ``ask_human`` tool."""
    return {
        "name": ASK_HUMAN_TOOL,
        "description": (
            "Reach a real human for one clarification: an expert science or code question, "
            "or a hard block such as missing data, a broken dependency, or a contradictory "
            "request. Blocks until the human replies, so ask early and specifically. The human "
            "reads this on a phone — lead with the one question, then the few facts needed to "
            "answer it. Write 'message' as Markdown (a heading, short bullets, or a fenced code "
            "block when it helps); keep it brief."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Your question to the human, in Markdown. Lead with the question itself, "
                        "then any supporting facts (what you observed, what you already tried)."
                    ),
                },
                "kind": {
                    "type": "string",
                    "enum": list(_ASK_HUMAN_KINDS),
                    "description": (
                        "Why you are asking: 'clarify_science' for an expert science point, "
                        "'clarify_code' for a code or convention point, 'blocked' for a hard block."
                    ),
                },
            },
            "required": ["message"],
        },
    }


def plan_and_debate_tool_def() -> dict:
    """The definition for the lead-only, budget-free ``plan_and_debate`` design channel (§5.5).

    Distinct from ``ask_human`` so the ReAct loop can treat it as budget-free: the lead settles
    a toolkit design with the human before authoring, and these rounds do not count against the
    loop's iteration cap (the human ending the discussion is the stop).
    """
    return {
        "name": PLAN_AND_DEBATE_TOOL,
        "description": (
            "Debate a toolkit-design decision with the human to settle it before authoring — "
            "lay out the design report (proposal, options and their trade-offs, an interface "
            "sketch) and ask the one thing you need decided (which package to extend vs. start, "
            "the interface, the validation strategy, a trade-off). Blocks until the human "
            "replies. Use it as many rounds as you need to converge: it does not consume your "
            "tool-iteration budget. Write 'message' as Markdown — use headings, bullet lists, "
            "and fenced code blocks so a longer design reads cleanly. Settle the design here, "
            "then finalise the ToolSpec the coder will follow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Your design report and the decision you want settled, in Markdown — "
                        "proposal, options/trade-offs, an interface sketch, and the explicit ask."
                    ),
                },
            },
            "required": ["message"],
        },
    }


# ----------------------------------------------------------------------------
# Local data catalog — a read-only tool for the analyst worker + lead (§9.10 step 7).
# ----------------------------------------------------------------------------

DATA_CATALOG_TOOL = "data_catalog"


def data_catalog_tool_def() -> dict:
    """The provider-neutral definition for the shared read-only ``data_catalog`` tool."""
    return {
        "name": DATA_CATALOG_TOOL,
        "description": (
            "List locally-available observation data for a mission — which ObsIDs are already "
            "on disk, their processing level (HEASARC supplies up to level 2), and the data "
            "products produced from them — read from the mission data catalog. Use this BEFORE "
            "requesting data acquisition so you only acquire what is genuinely missing. Omit "
            "'mission' to list the configured missions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mission": {
                    "type": "string",
                    "description": "Mission key, e.g. 'ixpe', 'nicer', 'nustar'.",
                },
                "min_level": {
                    "type": "integer",
                    "description": "Only count ObsIDs available at or above this processing level.",
                },
            },
        },
    }


async def execute_data_catalog(arguments: Mapping[str, Any]) -> ToolOutcome:
    """Read the per-mission local data catalog (``<obs_db_path>/catalog.json``).

    A read-only window onto what is already local so a role requests *acquire* only for
    genuinely-missing data (CLAUDE.md §9.10). Absent catalog ⇒ "(no local data catalogued)".
    """
    from auto_research.observations import load_observations_config, read_catalog

    mission = str(arguments.get("mission") or "").strip()
    if not mission:
        try:
            cfg = load_observations_config()
        except Exception as exc:  # noqa: BLE001
            return ToolOutcome(text=f"could not load observations config: {exc}", is_error=True)
        return ToolOutcome(text="configured missions: " + (", ".join(cfg.missions) or "(none)"))

    try:
        catalog = read_catalog(mission)
    except Exception as exc:  # noqa: BLE001 — unknown mission / malformed catalog
        return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)

    raw_min = arguments.get("min_level")
    min_level = int(raw_min) if raw_min is not None else None
    lines: list[str] = []
    for obsid in catalog.obsids():
        if min_level is not None and not catalog.has(obsid, min_level=min_level):
            continue
        entry = catalog.entries[obsid]
        products = ", ".join(sorted({p.kind for p in entry.products})) or "(none)"
        lines.append(f"- {obsid}: max_level={entry.max_level}, products=[{products}]")
    body = "\n".join(lines) or "(no local data catalogued)"
    return ToolOutcome(text=f"Local {mission} data ({len(lines)} ObsIDs):\n{body}")


async def execute_ask_human(
    human_handler: Any, asked_by: str, arguments: Mapping[str, Any], *, model: str = ""
) -> ToolOutcome:
    """Build a ``HumanQuery`` from a tool call and block on the injected handler.

    Routes through ``hitl.resolve_human``, which raises ``HumanInputRequired`` when no
    handler is wired (the §9.8 headless fail-safe) — propagated to fail the run loudly
    rather than hang or fabricate.
    """
    from auto_research.hitl import resolve_human
    from auto_research.schemas import HumanQuery

    kind = str(arguments.get("kind") or "blocked")
    if kind not in _ASK_HUMAN_KINDS:
        kind = "blocked"
    query = HumanQuery(
        kind=kind,
        message=str(arguments.get("message") or "(no message provided)"),
        asked_by=asked_by or "agent",
        model=model or "",
    )
    response = await resolve_human(human_handler, query)
    return ToolOutcome(text=response.answer or "(human gave no answer)")


async def execute_plan_and_debate(
    human_handler: Any, asked_by: str, arguments: Mapping[str, Any], *, model: str = ""
) -> ToolOutcome:
    """Route a lead design-debate turn to the human (§5.5).

    Delivers the lead's Markdown design report to the injected handler (same path as
    ``ask_human`` — ``resolve_human`` raises ``HumanInputRequired`` headless). The budget-free
    treatment lives in the ReAct loop, not here; this just delivers the turn. Reuses the
    ``clarify_science`` HumanQuery kind.
    """
    from auto_research.hitl import resolve_human
    from auto_research.schemas import HumanQuery

    query = HumanQuery(
        kind="clarify_science",
        message=str(arguments.get("message") or "(no message provided)"),
        asked_by=asked_by or "agent",
        model=model or "",
    )
    response = await resolve_human(human_handler, query)
    return ToolOutcome(text=response.answer or "(human gave no answer)")


# ----------------------------------------------------------------------------
# Lead-only tool-authoring tools (§9.3, B-topology). The analyst lead drives a live
# AuthoringSession: it steers the Sonnet coder (drive_coder), submits to the deterministic
# gate + GPT co-lead review (submit_for_review), and — slice 4 — debates a review challenge
# (respond_to_review). Masked to the lead alone (a session is wired only on its provider).
# ----------------------------------------------------------------------------

DRIVE_CODER_TOOL = "drive_coder"
SUBMIT_TOOL = "submit_for_review"
RESPOND_TO_REVIEW_TOOL = "respond_to_review"


def drive_coder_tool_def() -> dict:
    """Lead-only: instruct the Sonnet coder and read back its work. The lead is the plan owner;
    the coder executes. Budget-free in the lead's authoring loop (set via ``free_tools``)."""
    return {
        "name": DRIVE_CODER_TOOL,
        "description": (
            "Instruct the coder (a Sonnet Claude-Code session) to write or revise the tool, then "
            "read back what it did. The coder executes your plan — give concrete, algorithm-level "
            "instructions (signature, the exact procedure, edge cases, the tests to add), not a "
            "restated goal. It keeps the working files across your instructions, so iterate: "
            "inspect its diff with read_file, then drive_coder again. When the tool and its tests "
            "are right, call submit_for_review. Write 'instructions' as Markdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "instructions": {
                    "type": "string",
                    "description": (
                        "What the coder should do next, in Markdown — concrete and algorithm-level. "
                        "On the first call, seed the full brief; afterwards, the specific change."
                    ),
                },
            },
            "required": ["instructions"],
        },
    }


def submit_tool_def() -> dict:
    """Lead-only: run the gate + co-lead review over the coder's current work; commit on
    gate-pass ∧ review-pass (the lead never self-approves)."""
    return {
        "name": SUBMIT_TOOL,
        "description": (
            "Submit the coder's current work to the deterministic gate and the independent GPT "
            "co-lead review. The gate is mechanical (determinism, no-fabrication, structure, the "
            "tool's own tests) — you can only satisfy it, by fixing the code via drive_coder. The "
            "review judges whether the plan and its tests are right and strict enough. On gate-pass "
            "AND review-pass the tool is committed automatically — you never self-approve. If the "
            "gate fails, fix and submit again; if the review rejects, either drive_coder to address "
            "it or respond_to_review to debate it. This is the ONLY way to finish: your authored "
            "tool is committed solely through this call — if your turn ends without calling it (or "
            "respond_to_review), the work is discarded as a failure. When the tool and its tests are "
            "right, call it; if after your best effort it genuinely cannot be made mergeable, say so "
            "explicitly in one line rather than stopping silently. Takes no arguments."
        ),
        "input_schema": {"type": "object", "properties": {}},
    }


async def execute_drive_coder(session: Any, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Drive one coder turn over the live ``AuthoringSession`` (B-topology: lead steers, the
    Sonnet coder executes). Returns the coder's narration for the lead to inspect."""
    instructions = str(arguments.get("instructions") or "").strip()
    if not instructions:
        return ToolOutcome(
            text="drive_coder needs 'instructions': what the coder should do next.", is_error=True
        )
    response = await session.drive(instructions)
    return ToolOutcome(
        text=(response or "").strip()
        or "(coder produced no narration; inspect the worktree with read_file, or submit_for_review "
        "to run the gate.)"
    )


async def execute_submit(session: Any) -> ToolOutcome:
    """Run the gate, then (gate-pass) the co-lead review, over the session's current tree; commit
    on gate-pass ∧ review-pass (§9.9 commit ⇔ gate ∧ review — the lead never self-approves).
    Returns a verdict the lead diagnoses: a gate failure (fix via drive_coder), a review rejection
    (drive_coder or respond_to_review), or a successful commit. Bounded by ``session.submit_cap``
    (each submit runs the gate twice + a GPT review — expensive)."""
    if session.submits_used >= session.submit_cap:
        return ToolOutcome(
            text=(
                f"You have used all {session.submit_cap} review submissions for this tool. Make a "
                "final call: if your last submit committed, you are done; otherwise this authoring "
                "attempt ends without a mergeable tool — an honest failure is an acceptable, "
                "correct outcome (do not fabricate to force a pass)."
            )
        )
    session.submits_used += 1
    gate = await session.run_gate_step()
    if not gate.passed:
        failed = [
            f"- [{c.layer}] {c.name}: {c.detail}"
            for c in (*gate.layer_a, *gate.layer_b)
            if not c.passed
        ]
        body = "\n".join(failed) or gate.reason
        return ToolOutcome(
            text=(
                "GATE FAILED — mechanical checks you can only satisfy. Fix the underlying cause via "
                f"drive_coder (never weaken a check to pass), then submit_for_review again:\n{body}"
            )
        )
    review = await session.run_review_step(gate)
    if not review.approved:
        issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
        return ToolOutcome(
            text=(
                "GATE PASSED, but the independent co-lead REVIEW REJECTED the tool:\n"
                f"{issues}\n\n{review.rationale}\n\nEither drive_coder to address these, or "
                "respond_to_review to defend the design if you believe a challenge is unfounded."
            )
        )
    sha = await session.commit(
        review.commit_summary.strip() or session.tool_spec.proposed_tool_name
    )
    return ToolOutcome(
        text=(
            f"COMMITTED {session.tool_spec.proposed_tool_name} → {sha[:10]}. Gate passed and the "
            "co-lead approved — the tool is on the authoring branch. Authoring for this tool is done."
        )
    )


def respond_to_review_tool_def() -> dict:
    """Lead-only: debate the co-lead's most recent rejection (§9.3 — the lead persuades, never
    overrides). Budget-free in the lead's loop; bounded by ``session.debate_cap``."""
    return {
        "name": RESPOND_TO_REVIEW_TOOL,
        "description": (
            "Respond to the co-lead's most recent review rejection — defend the design or a specific "
            "test choice when you believe the challenge is unfounded or already addressed. The "
            "independent GPT co-lead reconsiders your argument and re-issues its verdict: it concedes "
            "only if genuinely persuaded (a sound argument, or evidence the issue is already handled) "
            "and holds firm against pressure. If it is persuaded and the gate still passes, the tool "
            "commits. Use this for a genuine disagreement; when the co-lead is right, fix the code via "
            "drive_coder instead. Write 'message' as Markdown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Your argument to the co-lead, in Markdown — address its specific issues, "
                        "citing where the code/tests already handle them or why the concern is wrong."
                    ),
                },
            },
            "required": ["message"],
        },
    }


async def execute_respond_to_review(session: Any, arguments: Mapping[str, Any]) -> ToolOutcome:
    """Re-run the co-lead review with the lead's rebuttal in view (§9.3 debate). On a concede, the
    immovable gate is re-validated before committing (the tree may have changed); commit ⇔ gate ∧
    review still holds. Bounded by ``session.debate_cap``."""
    message = str(arguments.get("message") or "").strip()
    if not message:
        return ToolOutcome(
            text="respond_to_review needs 'message': your argument to the co-lead.", is_error=True
        )
    if session.last_gate is None or session.last_review is None:
        return ToolOutcome(
            text="There is no co-lead review to respond to yet — call submit_for_review first.",
            is_error=True,
        )
    if session.debates_used >= session.debate_cap:
        return ToolOutcome(
            text=(
                f"You have used all {session.debate_cap} debate turns. Either address the issues "
                "via drive_coder, or conclude — an honest failure is an acceptable outcome."
            )
        )
    session.debates_used += 1
    review = await session.run_review_step(session.last_gate, lead_rebuttal=message)
    if not review.approved:
        issues = "\n".join(f"- {i}" for i in review.issues) or review.rationale
        return ToolOutcome(
            text=(
                "The co-lead HELD its position after your rebuttal:\n"
                f"{issues}\n\n{review.rationale}\n\nAddress these via drive_coder, or accept the "
                "challenge and conclude (an honest failure is acceptable)."
            )
        )
    # Persuaded — re-validate the immovable gate (the tree may have changed) before committing.
    gate = await session.run_gate_step()
    if not gate.passed:
        failed = "\n".join(
            f"- [{c.layer}] {c.name}: {c.detail}"
            for c in (*gate.layer_a, *gate.layer_b)
            if not c.passed
        ) or gate.reason
        return ToolOutcome(
            text=(
                "The co-lead is persuaded, but the gate no longer passes (did the tree change?):\n"
                f"{failed}\nFix via drive_coder, then submit_for_review."
            )
        )
    sha = await session.commit(
        review.commit_summary.strip() or session.tool_spec.proposed_tool_name
    )
    return ToolOutcome(
        text=(
            f"The co-lead was PERSUADED and the gate passed — COMMITTED "
            f"{session.tool_spec.proposed_tool_name} → {sha[:10]}. Authoring for this tool is done."
        )
    )


# ----------------------------------------------------------------------------
# Analyst science tools — the existing ToolkitRegistry + PersistentSandbox.
# ----------------------------------------------------------------------------

_PROTOCOL_KEYS: frozenset[str] = frozenset(
    {"stdout", "stderr", "exit_code", "figures", "console_tail"}
)


def _observation_path_args() -> dict[str, str]:
    """Map tool path-args to configured observation roots, e.g. ``ixpe_path`` →
    the IXPE ``obs_db_path`` from ``config/observations.yaml``.

    Convention: mission ``<m>`` in observations.yaml fills a tool argument named
    ``<m>_path``.  Used to auto-supply instrument data paths so the model need not.
    """
    try:
        cfg = load_observations_config()
    except Exception:  # noqa: BLE001
        return {}
    return {f"{mission}_path": str(c.obs_db_path) for mission, c in cfg.missions.items()}


def _normalize_tool_raw(raw: dict) -> dict:
    """Ensure every tool result has a non-empty ``stdout`` for the model to read.

    Tools that return structured payloads without a ``stdout`` key (e.g.
    ``list_dir`` → ``{path, entries}``, ``load_data`` → nested indices dict)
    are invisible to the model otherwise.  When ``stdout`` is absent or empty,
    serialize the non-protocol payload keys as compact JSON. Also folds any
    captured ``console_tail`` (XSPEC/C-level prints) into ``stdout``.
    """
    stdout = raw.get("stdout") or ""
    if not stdout:
        extra = {k: v for k, v in raw.items() if k not in _PROTOCOL_KEYS}
        if extra:
            stdout = json.dumps(extra, indent=2, default=str)

    console = raw.get("console_tail") or ""
    if console:
        sep = "\n--- console ---\n"
        stdout = (stdout + sep + console) if stdout else (sep.lstrip() + console)

    return {**raw, "stdout": stdout}


class SandboxToolProvider:
    """Analyst tool surface: ``ToolkitRegistry`` tools run in a ``PersistentSandbox``.

    Mirrors the per-call routing/argument-resolution the old ``execute_tools`` did:
    map the provider-safe function name back to the dotted ``kind``, auto-inject
    ``cwd`` for ``bash_exec`` and instrument data paths for toolkit tools, then call
    the sandbox. The model self-corrects on errors via the ReAct loop (it sees the
    error text and re-issues), so there is no separate retry here. Every executed
    ``ToolResult`` accumulates in ``results`` for memory/peer-summary use.
    """

    def __init__(
        self,
        registry: Any,  # ToolkitRegistry — avoid import cycle
        sandbox: Any,  # PersistentSandbox
        *,
        cwd: Path | None = None,
        timeout_s: int = 300,
        human_handler: Any | None = None,
        asked_by: str = "analyst_worker",
        model: str = "",
    ) -> None:
        self.registry = registry
        self.sandbox = sandbox
        self.cwd = cwd
        # When a human_handler is wired, expose the shared ask_human tool (§9.7).
        self.human_handler = human_handler
        self.asked_by = asked_by
        self.model = model
        # Figures land in <worker scratch>/figures (under the runtime data dir),
        # never in /tmp or a repo. See _redirect_figure_args.
        self.figures_dir = (Path(cwd) / "figures") if cwd is not None else None
        self._figures_dir_ready = False
        self.timeout_s = timeout_s
        self.results: list[ToolResult] = []
        # Filesystem jail (CLAUDE.md §2/§12): the worker's builtin file tools are otherwise raw.
        # Inactive (allow-all) when agent_fs_allow is unset. Toolkit tools take obs/data paths and
        # run in the sandbox — not gated here (OS jail closes that, §12 / Loose ends).
        self._guard = PathGuard.from_config()

    def tool_definitions(self) -> list[dict]:
        defs = list(self.registry.tool_definitions())
        defs.append(data_catalog_tool_def())
        if self.human_handler is not None:
            defs.append(ask_human_tool_def())
        return defs

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == DATA_CATALOG_TOOL:
            return await execute_data_catalog(arguments)
        if name == ASK_HUMAN_TOOL:
            return await execute_ask_human(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        kind = self.registry.kind_for_function(name)
        args = dict(arguments)
        if kind == "mcp.builtin.bash_exec" and "cwd" not in args and self.cwd is not None:
            args["cwd"] = str(self.cwd)

        # Filesystem allow-list on the builtin file tools (the raw read/list/bash surface).
        # bash_exec is checked by the absolute paths it NAMES (best-effort); read_file/list_dir
        # by their target. Denies the auto_research repo + governance files (§2/§4).
        denied = None
        if kind == "mcp.builtin.bash_exec":
            denied = self._guard.forbidden_in_command(args.get("command", ""))
        elif kind in ("mcp.builtin.read_file", "mcp.builtin.list_dir"):
            target = args.get("path")
            if target is not None and not self._guard.is_allowed(target):
                denied = str(target)
        if denied is not None:
            return ToolOutcome(
                text=(
                    f"path not permitted: {denied} — outside the agent filesystem allow-list "
                    f"(research_toolkit / obsdb / analyst-env / HEASoft). The auto_research "
                    f"orchestration repo and governance files are off-limits (§2/§4)."
                ),
                is_error=True,
            )

        # Sandbox call timeout: the tool's declared ceiling (tool_config.yaml; default 300s),
        # the hard SIGTERM backstop that lets a long-running tool (e.g. a HEASARC download) run
        # beyond the protective 300s cap (calls over it die with exit 124) — see ToolkitRegistry.
        # It is fixed per tool, NOT model-tunable: a tool's *own* timeout argument (e.g. acquire's
        # astroquery timeout_s) is the caller's graceful runtime knob, and the ceiling is set a bit
        # above it so the tool times out cleanly before this hard kill.
        ceiling_fn = getattr(self.registry, "call_timeout_ceiling", None)
        call_timeout = ceiling_fn(kind) if ceiling_fn is not None else self.timeout_s

        t0 = time.monotonic()
        if not self.registry.is_known(kind):
            raw = {"exit_code": 127, "stderr": f"Tool not registered: {kind}", "stdout": ""}
        elif self.registry.is_mcp(kind):
            raw = await self.sandbox.call_mcp_tool(kind, args, timeout_s=call_timeout)
        else:
            spec = self.registry.spec(kind)
            schema_props = (spec.get("args_schema") or {}).get("properties", {})
            for arg_name, path in _observation_path_args().items():
                if arg_name in schema_props and not args.get(arg_name):
                    args[arg_name] = path
            self._redirect_figure_args(spec["fn"], args, schema_props)
            raw = await self.sandbox.call_tool(
                spec["module"], spec["fn"], args, timeout_s=call_timeout
            )
        elapsed = time.monotonic() - t0

        raw = _normalize_tool_raw(raw)
        figures = [FigureRef.model_validate(f) for f in raw.get("figures", [])]
        result = ToolResult(
            tool_name=kind,
            stdout=raw.get("stdout", ""),
            stderr=raw.get("stderr", ""),
            exit_code=raw.get("exit_code", 1),
            figures=figures,
            elapsed_s=elapsed,
        )
        self.results.append(result)
        if result.exit_code == 0:
            text = result.stdout or "(tool returned no output)"
        else:
            text = f"[exit {result.exit_code}]\n{result.stderr or result.stdout or '(no error detail)'}"
        return ToolOutcome(text=text, is_error=result.exit_code != 0, figures=figures, result=result)

    def _redirect_figure_args(self, fn: str, args: dict, schema_props: Mapping) -> None:
        """Force figure-output paths under the worker's ``figures_dir``.

        Keyed on the **arg-name convention** (``_FIGURE_PATH_ARGS``), not on specific
        tool kinds, so any toolkit tool that follows the convention places its PNGs
        under the run's figures dir (inside the runtime data dir) instead of /tmp,
        the repo, or wherever the model happened to name. The model's chosen basename
        is preserved — only the directory and the extension convention are enforced —
        so captions/provenance stay meaningful. A no-op when no ``cwd`` is attached
        (e.g. Studio) or the tool exposes no figure arg.
        """
        if self.figures_dir is None:
            return
        touched = False
        for arg_name, ext in _FIGURE_PATH_ARGS.items():
            if arg_name not in schema_props:
                continue
            value = args.get(arg_name)
            base = os.path.basename(value.strip()) if isinstance(value, str) and value.strip() else ""
            if not base:
                base = f"{fn}_{int(time.time() * 1000)}"
            low = base.lower()
            for known in (".png", ".pdf", ".jpg", ".jpeg", ".svg"):
                if low.endswith(known):
                    base = base[: -len(known)]
                    break
            args[arg_name] = str(self.figures_dir / f"{base}{ext}")
            touched = True
        if touched and not self._figures_dir_ready:
            self.figures_dir.mkdir(parents=True, exist_ok=True)
            self._figures_dir_ready = True


# ----------------------------------------------------------------------------
# Read-only inspection tools — reviewer / analyst-lead (in-process, no sandbox).
# ----------------------------------------------------------------------------


class ReadOnlyToolProvider:
    """Lightweight in-process tools for the reviewer/lead to investigate before deciding.

    ``read_file`` / ``list_dir`` are confined to ``roots`` (the run artifacts dir and
    the papers dir) so the agent cannot read arbitrary paths; ``text_search`` queries
    the local knowledge base via the retrieval backend. No science sandbox is involved.
    """

    def __init__(
        self,
        *,
        roots: Sequence[Path],
        retrieval: Any | None = None,
        web_source: Any | None = None,
        include_text_search: bool = True,
        include_data_catalog: bool = False,
        include_plan_and_debate: bool = False,
        authoring_session: Any | None = None,
        human_handler: Any | None = None,
        asked_by: str = "agent",
        model: str = "",
        max_chars: int = 8000,
    ) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.retrieval = retrieval
        # The analyst lead (only) drives tool authoring via a live AuthoringSession (§9.3
        # B-topology): drive_coder / submit_for_review. Masked to the lead alone — a session is
        # wired here only on the lead's provider; no other role sees the tools. None ⇒ disabled.
        self.authoring_session = authoring_session
        # The analyst lead (not the reviewer) gets the read-only data_catalog tool so it can
        # check local data availability before deciding to author an acquire tool (§9.10).
        self.include_data_catalog = include_data_catalog
        # The analyst lead (only) gets the budget-free plan_and_debate design channel (§5.5),
        # so it can settle a toolkit design with the human before authoring. Needs human_handler.
        self.include_plan_and_debate = include_plan_and_debate
        # When a human_handler is wired, expose the shared ask_human tool (§9.7).
        self.human_handler = human_handler
        self.asked_by = asked_by
        self.model = model
        # Optional Tavily/web QuerySource — when supplied (analyst lead), exposes a
        # web_search tool so the lead can consult authoritative documentation (mission
        # threads, calibration memos) before triaging a tool request or answering the
        # author, rather than guessing (CLAUDE.md §9.3 escalation ladder).
        self.web_source = web_source
        # The papers knowledge base is irrelevant to pure code review, so the §9.3
        # co-lead reviewer omits text_search (roots = the toolkit worktree instead).
        # text_search + open_paper are gated together as the knowledge-base toolset.
        self.include_text_search = include_text_search
        # Paths opened via open_paper this session — the reviewer's cited_paths are
        # intersected with this so a citation can only name a paper actually read.
        self.opened_paths: list[Path] = []
        self.max_chars = max_chars

    def _roots_text(self) -> str:
        return ", ".join(str(r) for r in self.roots)

    def tool_definitions(self) -> list[dict]:
        roots_text = self._roots_text()
        definitions = [
            {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file. Give an absolute path inside one of the permitted "
                    f"roots: {roots_text}."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the file, inside a permitted root.",
                        }
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "list_dir",
                "description": (
                    "List the entries of a directory. Give an absolute path inside one of the "
                    f"permitted roots: {roots_text}."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the directory, inside a permitted root.",
                        }
                    },
                    "required": ["path"],
                },
            },
        ]
        if self.include_text_search:
            definitions.append(
                {
                    "name": "text_search",
                    "description": (
                        "Full-text search the local knowledge base of ingested papers; returns "
                        "the file paths of the best matches."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Keywords or a phrase to match against ingested papers.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of paper paths to return (default 5).",
                            },
                        },
                        "required": ["query"],
                    },
                }
            )
            definitions.append(
                {
                    "name": "open_paper",
                    "description": (
                        "Open a local PDF (text + figures) to read it before citing. Pass a path "
                        "from text_search. The papers you open and actually use are exactly the "
                        "ones to list in the verdict's cited_paths; the report's References "
                        "section links to those files."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to a local PDF returned by text_search.",
                            }
                        },
                        "required": ["path"],
                    },
                }
            )
        if self.web_source is not None:
            definitions.append(
                {
                    "name": "web_search",
                    "description": (
                        "Search the web for authoritative documentation — mission analysis "
                        "threads, calibration memos, official references. Use it to confirm an "
                        "uncertain science or code point from a primary source."
                    ),
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Keywords describing the documentation you need.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of results to return (default 5).",
                            },
                        },
                        "required": ["query"],
                    },
                }
            )
        if self.include_data_catalog:
            definitions.append(data_catalog_tool_def())
        if self.human_handler is not None:
            definitions.append(ask_human_tool_def())
            if self.include_plan_and_debate:
                definitions.append(plan_and_debate_tool_def())
        if self.authoring_session is not None:
            definitions.append(drive_coder_tool_def())
            definitions.append(submit_tool_def())
            definitions.append(respond_to_review_tool_def())
        return definitions

    def _allowed(self, path: Path) -> bool:
        resolved = path.resolve()
        # §4: build-time governance files never enter runtime context, even inside a root
        # (research_toolkit carries its own CLAUDE.md/DOCS.md). Closes the reviewer/lead leak.
        if resolved.name in GOVERNANCE_BASENAMES:
            return False
        return any(resolved == root or root in resolved.parents for root in self.roots)

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == DATA_CATALOG_TOOL:
            return await execute_data_catalog(arguments)
        if name == ASK_HUMAN_TOOL:
            return await execute_ask_human(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        if name == PLAN_AND_DEBATE_TOOL:
            return await execute_plan_and_debate(
                self.human_handler, self.asked_by, arguments, model=self.model
            )
        if name == DRIVE_CODER_TOOL:
            if self.authoring_session is None:
                return ToolOutcome(text="authoring is not enabled for this role.", is_error=True)
            return await execute_drive_coder(self.authoring_session, arguments)
        if name == SUBMIT_TOOL:
            if self.authoring_session is None:
                return ToolOutcome(text="authoring is not enabled for this role.", is_error=True)
            return await execute_submit(self.authoring_session)
        if name == RESPOND_TO_REVIEW_TOOL:
            if self.authoring_session is None:
                return ToolOutcome(text="authoring is not enabled for this role.", is_error=True)
            return await execute_respond_to_review(self.authoring_session, arguments)
        if name in ("read_file", "list_dir"):
            target = Path(str(arguments.get("path", "")))
            if not self._allowed(target):
                return ToolOutcome(
                    text=(
                        f"path not permitted: {target}. Use an absolute path inside one of the "
                        f"permitted roots: {self._roots_text()}"
                    ),
                    is_error=True,
                )
            try:
                if name == "read_file":
                    text = target.read_text(encoding="utf-8", errors="replace")[: self.max_chars]
                else:
                    text = json.dumps(sorted(p.name for p in target.iterdir()))
            except OSError as exc:
                return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)
            return ToolOutcome(text=text or "(empty)")

        if name == "text_search":
            from auto_research.retrieval import text_search_paths

            try:
                paths = await text_search_paths(
                    str(arguments.get("query", "")),
                    limit=int(arguments.get("limit", 5)),
                    backend=self.retrieval,
                )
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                return ToolOutcome(
                    text=f"text_search failed: {type(exc).__name__}: {exc}", is_error=True
                )
            text = "\n".join(str(p) for p in paths) or "(no local matches)"
            return ToolOutcome(text=text)

        if name == "open_paper":
            target = Path(str(arguments.get("path", "")))
            resolved = target.resolve()
            if not self._allowed(target):
                return ToolOutcome(
                    text=(
                        f"path not permitted: {target}. Open a paper path inside one of the "
                        f"permitted roots: {self._roots_text()}"
                    ),
                    is_error=True,
                )
            if resolved not in self.opened_paths and len(self.opened_paths) >= _OPEN_PAPER_CAP:
                return ToolOutcome(
                    text=(
                        f"open_paper limit reached ({_OPEN_PAPER_CAP} papers). Decide and cite "
                        "from the papers already opened."
                    ),
                    is_error=True,
                )
            try:
                # Same %PDF- signature check the downloader uses, so only real PDFs attach.
                if not (target.is_file() and b"%PDF-" in target.read_bytes()[:1024]):
                    return ToolOutcome(
                        text=f"not a readable PDF: {target}", is_error=True
                    )
                doc = document_from_file(target)
            except OSError as exc:
                return ToolOutcome(text=f"{type(exc).__name__}: {exc}", is_error=True)
            if resolved not in self.opened_paths:
                self.opened_paths.append(resolved)
            return ToolOutcome(
                text=f"opened '{doc.title}' ({target}); the PDF is attached above.",
                documents=[doc],
            )

        if name == "web_search":
            if self.web_source is None:
                return ToolOutcome(text="web_search not available", is_error=True)
            # Provider faults (rate limit, network, Tavily error) degrade to an
            # error outcome rather than crashing the reviewer/lead ReAct loop.
            try:
                results = await self.web_source.search(
                    str(arguments.get("query", "")), limit=int(arguments.get("limit", 5))
                )
            except Exception as exc:  # noqa: BLE001
                return ToolOutcome(
                    text=f"web_search failed: {type(exc).__name__}: {exc}", is_error=True
                )
            lines = [f"- {r.title} {r.url}\n  {(r.abstract or '')[:300]}" for r in results]
            return ToolOutcome(text="\n".join(lines)[: self.max_chars] or "(no results)")

        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)


# ----------------------------------------------------------------------------
# Literature source tools — librarian I/O (in-process QuerySources).
# ----------------------------------------------------------------------------


class SourceToolProvider:
    """Literature-search tools for the librarian: external sources + local knowledge base.

    ``search_literature`` runs one configured ``QuerySource`` (arxiv/nasa_ads/web);
    ``text_search`` queries already-ingested papers. Accumulated results are exposed
    on ``results`` for downstream ingestion by the graph node.
    """

    def __init__(self, sources: Mapping[str, Any], *, retrieval: Any | None = None, max_chars: int = 6000) -> None:
        self.sources = dict(sources)
        self.retrieval = retrieval
        self.max_chars = max_chars
        self.results: list[Any] = []  # SearchResult accumulator

    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "search_literature",
                "description": (
                    "Search one external literature source for papers and add the hits to this "
                    "run's results. Sources: " + ", ".join(self.sources) + "."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": list(self.sources),
                            "description": "The literature source to query.",
                        },
                        "query": {
                            "type": "string",
                            "description": (
                                "A short keyword query of 4–8 terms; arXiv and ADS match on "
                                "keywords."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of papers to return (default 10).",
                        },
                    },
                    "required": ["source", "query"],
                },
            },
            {
                "name": "text_search",
                "description": (
                    "Search the local knowledge base of already-ingested papers; returns "
                    "matching paper paths."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords or a phrase to match against ingested papers.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of paper paths to return (default 5).",
                        },
                    },
                    "required": ["query"],
                },
            },
        ]

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        if name == "search_literature":
            source = self.sources.get(str(arguments.get("source", "")))
            if source is None:
                return ToolOutcome(text=f"unknown source: {arguments.get('source')}", is_error=True)
            # A source provider raising (network, rate limit, ADS 404, malformed
            # response) must not kill the whole ReAct loop/run — surface it as an
            # error outcome so the model can retry, switch source, or stop. Mirrors
            # the per-source guard in LibrarianIOAgent.search().
            try:
                results = await source.search(
                    str(arguments.get("query", "")), limit=int(arguments.get("limit", 10))
                )
            except Exception as exc:  # noqa: BLE001 — degrade one source, keep the loop alive
                return ToolOutcome(
                    text=f"source '{arguments.get('source')}' failed: {type(exc).__name__}: {exc}",
                    is_error=True,
                )
            self.results.extend(results)
            lines = [f"- {r.title} ({r.source}) {r.url}\n  {(r.abstract or '')[:300]}" for r in results]
            return ToolOutcome(text="\n".join(lines)[: self.max_chars] or "(no results)")

        if name == "text_search":
            from auto_research.retrieval import text_search_paths

            try:
                paths = await text_search_paths(
                    str(arguments.get("query", "")),
                    limit=int(arguments.get("limit", 5)),
                    backend=self.retrieval,
                )
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                return ToolOutcome(
                    text=f"local text_search failed: {type(exc).__name__}: {exc}", is_error=True
                )
            return ToolOutcome(text="\n".join(str(p) for p in paths) or "(no local matches)")

        return ToolOutcome(text=f"unknown tool: {name}", is_error=True)
