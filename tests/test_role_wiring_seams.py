"""A role's wiring has ONE definition, and a probe must use it rather than rebuild it.

**The defect this exists to prevent, measured 2026-09-08.** The seat probe's worker cell built an
`AnalystWorkerAgent` by hand and attached no sandbox and no registry. `analyze()` runs its inner
ReAct tool loop only when both are present and otherwise falls back to a plain structured call, so
that cell drove a worker that could not call a tool — and the failure it produced was
*unattributable*: a model that will not drive a loop and a model that was never given one look
identical from outside. The lead cell was wrong more quietly, building a read-only tool loop that
differed from production's by five arguments.

The 2026-08-06 lesson at a new address: a live test below the layer the defects live in measures
its own scaffolding. So `graph.runtime` owns `readonly_tools_for`, `readonly_roots_for` and
`attach_worker_runtime`; `run_research` calls them; and anything that drives a role outside a full
run calls them too.

These checks are static and free. They cannot prove a caller wired a role *correctly*, but they do
prove there is only one place that decides what correct is.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "src" / "auto_research" / "graph" / "runtime.py"
PROBE = REPO / "tests" / "test_live_seat_probe.py"

#: The constructors whose argument lists decide what a role can do, and which therefore belong
#: behind a factory rather than being spelled out per caller.
WIRING_CALLS = ("ReadOnlyToolProvider", "PersistentSandbox")

#: The factories that own them, in `graph.runtime`.
FACTORIES = ("readonly_tools_for", "attach_worker_runtime")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _enclosing_functions(tree: ast.Module) -> dict[int, str]:
    """Line number -> the name of the innermost function that contains it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner[line] = node.name
    return owner


def _calls_to(tree: ast.Module, names: tuple[str, ...]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute) else None
        )
        if name in names:
            found.append((name, node.lineno))
    return found


def test_the_factories_exist_and_are_exported():
    tree = _tree(RUNTIME)
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for factory in (*FACTORIES, "readonly_roots_for"):
        assert factory in defined, f"{factory} is not defined at module level in runtime.py"


@pytest.mark.parametrize("call,factory", list(zip(WIRING_CALLS, FACTORIES, strict=True)))
def test_runtime_constructs_the_wiring_only_inside_its_factory(call, factory):
    """One definition. A second construction in the run path is a second answer to the same
    question, and the two drift silently because both look right in isolation."""
    tree = _tree(RUNTIME)
    owners = _enclosing_functions(tree)
    outside = [
        (name, line, owners.get(line, "<module>"))
        for name, line in _calls_to(tree, (call,))
        if owners.get(line) != factory
    ]
    assert not outside, (
        f"{call} is constructed outside {factory} in runtime.py: {outside}. Route it through the "
        f"factory so the run path and anything probing it wire the role the same way."
    )


def test_run_research_actually_calls_both_factories():
    """The factories must be the run path's own wiring, not a parallel API for tests.

    If `run_research` stopped calling them, they would keep passing the checks above while
    describing a role production no longer builds — the worst version of this failure, because a
    probe would then be measuring a fiction with a factory's authority behind it.
    """
    tree = _tree(RUNTIME)
    owners = _enclosing_functions(tree)
    # The lead's and reviewer's read-only loops are built through their own attach seams, which
    # `run_research` calls, so a factory counts as the run path's when either calls it.
    seams = {"attach_lead_runtime", "attach_reviewer_runtime"}
    for seam in seams:
        callers = {owners.get(line) for _, line in _calls_to(tree, (seam,))}
        assert "run_research" in callers, f"{seam} is not called by run_research"
    for factory in FACTORIES:
        callers = {owners.get(line) for _, line in _calls_to(tree, (factory,))}
        assert callers & ({"run_research"} | seams), (
            f"{factory} is not called by run_research (callers: {callers or 'none'})"
        )


def test_the_seat_probe_does_not_hand_roll_a_role_loop():
    """The probe gates a paid batch, so its wiring may not be its own.

    Scoped to the probe rather than to every live test: six older live tests still construct a
    `ReadOnlyToolProvider` directly, and two of them back published measurements, so changing what
    they build would change what those numbers mean. They are recorded as a known gap rather than
    silently rewritten.
    """
    tree = _tree(PROBE)
    hand_rolled = _calls_to(tree, WIRING_CALLS)
    assert not hand_rolled, (
        f"the seat probe builds role wiring itself at {hand_rolled}; call the runtime factories"
    )


def test_the_seat_probe_uses_the_factories_it_should():
    source = PROBE.read_text(encoding="utf-8")
    for factory in (*FACTORIES, "readonly_roots_for"):
        assert factory in source, f"the seat probe never calls {factory}"


def test_the_lead_and_reviewer_loops_differ_the_way_production_says():
    """The two read-only loops are not interchangeable, and the difference is the point.

    The lead investigates toolkit source and the observation archive; the reviewer is the paper
    surface. A probe that gave the lead the reviewer's loop would measure a role the pipeline does
    not have, and nothing would fail.
    """
    from auto_research.graph.runtime import readonly_tools_for

    lead = readonly_tools_for("analyst_lead", roots=[REPO])
    reviewer = readonly_tools_for("reviewer", roots=[REPO])
    lead_tools = {definition["name"] for definition in lead.tool_definitions()}
    reviewer_tools = {definition["name"] for definition in reviewer.tool_definitions()}

    assert lead_tools != reviewer_tools
    assert any("catalog" in name for name in lead_tools), "the lead lost the data catalog"
    assert not any("catalog" in name for name in reviewer_tools)
    # Headless, neither gets a human channel: a role must not be handed a tool nobody can answer.
    assert not any("human" in name or "debate" in name for name in lead_tools | reviewer_tools)


def test_an_unknown_role_is_refused_rather_than_given_a_default_loop():
    from auto_research.graph.runtime import readonly_tools_for

    with pytest.raises(ValueError):
        readonly_tools_for("analyst_worker", roots=[REPO])


# ---------------------------------------------------------------- the lead's own wiring (2026-09-12)


def test_the_lead_has_a_wiring_seam_and_run_research_uses_it():
    """`attach_lead_runtime` must exist and be the run path's own wiring, like the worker seam.

    The lead's identity, report folder, monitor, stop channel and tools were spread across ~40
    lines of `run_request` while the workers had a seam of their own, so a live drill driving a
    real lead restated all of it by hand — the shape that made a seat probe drive a worker with
    no sandbox and produce an unattributable failure (2026-09-08).
    """
    tree = _tree(RUNTIME)
    defined = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "attach_lead_runtime" in defined, "the lead has no wiring seam in runtime.py"
    owners = _enclosing_functions(tree)
    callers = {owners.get(line) for _, line in _calls_to(tree, ("attach_lead_runtime",))}
    assert "run_research" in callers, (
        f"attach_lead_runtime is not called by run_research (callers: {callers or 'none'}) — a "
        "seam the run path does not use describes a role production no longer builds"
    )


def test_the_stop_drill_wires_its_lead_through_the_seam():
    """The live drill must not restate the lead's wiring; it must ask for production's."""
    drill = Path(__file__).with_name("test_live_stop_mid_drive.py")
    tree = _tree(drill)
    called = {name for name, _ in _calls_to(tree, ("attach_lead_runtime", "readonly_tools_for"))}
    assert "attach_lead_runtime" in called, "the drill hand-rolls the lead's wiring"

    # The tell-tale of a hand-rolled equivalent is assigning the attributes the seam owns.
    source = drill.read_text(encoding="utf-8")
    for restated in ("lead.agent_key =", "lead.report_dir =", "lead.tools =", "lead.monitor ="):
        assert restated not in source, (
            f"the drill sets {restated.strip()} itself instead of letting the seam do it"
        )
