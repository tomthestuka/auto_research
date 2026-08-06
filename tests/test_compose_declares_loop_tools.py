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
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            attr = getattr(target, "attr", None)
            if attr == "_react":
                react_tools = _kwarg_source(node, "tools")
            elif attr == "_call_over_messages":
                out.append((fn.name, node.lineno, _kwarg_source(node, "tools"), react_tools))
    return out


COMPOSES = _compose_sites()


def test_every_compose_site_was_found() -> None:
    """A refactor that renames the compose helper must not silently empty this file."""
    assert len(COMPOSES) >= 7, f"expected the known compose sites, found {len(COMPOSES)}"


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
