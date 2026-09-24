"""Static guard: every terminal compose declares the SAME tools its own ReAct loop used.

**Why this exists.** The compose replays the loop's trajectory, whose provider items reference
the loop's tools — so the two must agree. On 2026-08-06 they did not: seven call sites were
patched to pass `tools=`, and two of them (`decide_authoring`, `clarify_for_author`) wrote
`tools=tools` where the loop had used `self.tools`. The suite stayed green because no
deterministic test drives those two methods, and the failure was a `NameError` that only
surfaced 80 minutes into a live run.

`test_no_undefined_names.py` catches the *undefined* half. This catches the half that a
correctly-spelled but WRONG name would slip through — `tools=self.sources` in a method whose
loop ran on `self.tools` is in scope, would run, and would declare the wrong tool set.

Reads the source rather than executing it: most of these methods need a sandbox, a registry and
a live-ish tool provider to reach their compose, which is precisely why they had no coverage.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROLES = Path(__file__).resolve().parents[1] / "src" / "auto_research" / "agents" / "roles.py"


def _kwarg_source(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name:
            return ast.unparse(kw.value)
    return None


def _compose_sites() -> list[tuple[str, int, str | None, str | None]]:
    """(method, lineno, tools= on the compose, tools= on the nearest preceding _react)."""
    tree = ast.parse(ROLES.read_text(encoding="utf-8"), str(ROLES))
    out: list[tuple[str, int, str | None, str | None]] = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        react_tools: str | None = None
        # SOURCE order, not walk order. `ast.walk` is breadth-first, so a `_react` nested one
        # level deeper than its compose (inside a `try:`, say) is visited AFTER it and the
        # pairing silently reads `None` — which is what this guard reports as a missing loop.
        # "Nearest preceding" is what the docstring promises, so sort by position and mean it.
        calls = sorted(
            (n for n in ast.walk(fn) if isinstance(n, ast.Call)),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for node in calls:
            attr = getattr(node.func, "attr", None)
            if attr == "_react":
                react_tools = _kwarg_source(node, "tools")
            elif attr == "_call_over_messages":
                out.append((fn.name, node.lineno, _kwarg_source(node, "tools"), react_tools))
    return out


COMPOSES = _compose_sites()


def test_every_compose_site_was_found() -> None:
    """A refactor that renames the compose helper must not silently empty this file.

    Two, down from seven: the analyst lead's five actions became branches of one ReAct loop that
    ends by filing a form, so its four composes (adjudicate, judge_convergence, decide_authoring,
    finalize_authoring_outcome) no longer exist — a conversation with no output schema cannot
    replay a trajectory under the wrong one. What remains are the reviewer's and the co-lead's.
    Lower the number only when a compose is genuinely deleted, never to make this pass.
    """
    # Was >= 4. `compose_latex_report` was retired by F5e (2026-09-09): the reviewer now authors
    # `report.tex` through `file_edit` + a validating `turn_end`, so there is no terminal compose
    # on that path to declare tools for. The remaining three are still guarded, which is the
    # property this file exists for — the floor moved because a site was deleted, never because a
    # site stopped satisfying the rule.
    # Was >= 3. The lead's author-clarification compose went with `clarify_for_author` on
    # 2026-09-12: the coder's question now enters the lead's OWN loop as an injected turn and is
    # answered with `drive_coder`, so there is no second conversation to compose a reply out of.
    # Was >= 2. The reviewer's verdict compose went on 2026-09-24: the reviewer writes its one
    # report inside its loop and files it through a validating `turn_end` (§2b), so the co-lead's
    # `review_tool` is the one compose left.
    assert len(COMPOSES) >= 1, f"expected the known compose sites, found {len(COMPOSES)}"


def test_the_leads_cycle_runs_every_react_on_one_shared_provider() -> None:
    """The lead's cycle re-enters ``_react`` several times; all of them must declare one provider.

    With no compose left, this is where the same hazard now lives. The cycle loops back for each
    stage and once per chain link, and a second provider built mid-cycle would change the declared
    tool set between re-entries — a replayed trajectory could then name a tool the request no
    longer declares, which is the "Tool reference 'x' not found in available tools" run-killer
    (2026-07-29). One object, swapped internally (the live ``authoring_session``), keeps the
    declarations constant; ``ReadOnlyToolProvider.tool_definitions`` gates the authoring tools on
    ``cycle.authoring_enabled`` rather than on that session for exactly this reason.
    """
    tree = ast.parse(ROLES.read_text(encoding="utf-8"), str(ROLES))
    cycle_methods = {"run_cycle", "_author_chain", "_drive_one_link"}
    seen: dict[str, set[str]] = {}
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if fn.name not in cycle_methods:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_react":
                seen.setdefault(fn.name, set()).add(_kwarg_source(node, "tools") or "<missing>")
    assert seen, "the lead's cycle methods were renamed — re-read roles.py"
    declared = {expr for exprs in seen.values() for expr in exprs}
    assert declared == {"provider"}, (
        "every _react in the lead's cycle must run on the one shared provider; found "
        f"{sorted(declared)} across {sorted(seen)}"
    )


def test_the_leads_cycle_has_no_compose() -> None:
    """The cycle files a form instead of composing, so it declares no output schema at all."""
    tree = ast.parse(ROLES.read_text(encoding="utf-8"), str(ROLES))
    offenders = [
        f"{fn.name}():{node.lineno}"
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name in {"run_cycle", "_author_chain", "_drive_one_link"}
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) in ("_call_over_messages", "_call")
    ]
    assert not offenders, (
        "the lead's cycle composed a structured answer; its answer is the filed form, and a "
        "conversation carrying no schema is what makes the re-entries safe: " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "method,lineno,compose_tools,react_tools",
    COMPOSES,
    ids=[f"{m}:{ln}" for m, ln, _, _ in COMPOSES],
)
def test_compose_declares_the_same_tools_as_its_loop(
    method: str, lineno: int, compose_tools: str | None, react_tools: str | None
) -> None:
    assert compose_tools is not None, (
        f"roles.py:{lineno} {method}() composes over a ReAct trajectory without declaring tools. "
        "The replayed turn's provider items reference them; omitting the declarations orphans "
        "those items (the 2026-07-29 and 2026-08-05 run-killers)."
    )
    assert react_tools is not None, (
        f"roles.py:{lineno} {method}() has a compose but no preceding _react(tools=...) — "
        "the pairing this guard relies on has changed; re-read the file."
    )
    assert compose_tools == react_tools, (
        f"roles.py:{lineno} {method}() declares `tools={compose_tools}` to the compose but ran "
        f"its loop on `tools={react_tools}`. The compose must declare the SAME tool set, or the "
        "trajectory it replays references tools the request never declared."
    )


def test_one_conversation_carries_exactly_one_schema() -> None:
    """No trajectory is replayed under a schema different from the one it was built for.

    Joining a history gathered while emitting schema A onto a request that asks for schema B is
    a shape we never send today — every role method builds a FRESH seed (`[system, payload]`)
    per action, and its tool-less fallback uses the same schema as its compose. Worth pinning:
    the adapters' strict-schema conversion and the provider-side parse are validated per schema,
    and a refactor that let two schemas share one conversation would be sending an untested
    shape without anything failing locally.
    """
    tree = ast.parse(ROLES.read_text(encoding="utf-8"), str(ROLES))
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        schemas = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in (
                "_call_over_messages",
                "_call",
            ):
                if len(node.args) > 1:
                    schemas.add(ast.unparse(node.args[1]))
        if len(schemas) > 1:
            offenders.append(f"{fn.name}() emits {sorted(schemas)}")
    assert not offenders, (
        "a role method emits more than one schema; if these share a conversation, a trajectory "
        "built under one schema is being replayed under another:\n" + "\n".join(offenders)
    )
