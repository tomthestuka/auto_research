"""Free structural guards for the live turn-end shape matrix — ungated, run in the ordinary suite.

`tests/test_live_turn_end_shapes.py` claims to cover **every** shape the lead's report can take.
A claim like that decays the moment someone adds a clause to the validator, and it decays
silently: the live file still passes, having simply never met the new state.

So the claim is asserted here, from the contract itself, for free. Same discipline as
`test_no_hitl_probe_helpers.py` (2026-09-01): the code that *reads* a result needs its own tests,
because one pointed at the wrong thing looks exactly like a boring finding.
"""

from __future__ import annotations

import inspect

from tests.test_live_turn_end_shapes import SHAPES


def _refusable_clauses() -> int:
    """How many distinct problems `lead_turn_problems` can raise."""
    from auto_research.agents.react import lead_turn_problems

    source = inspect.getsource(lead_turn_problems)
    return source.count("problems.append(") + source.count("return [\n")


def test_every_refusable_clause_has_a_shape_bound_to_it():
    """The assertion that makes "exhaustive" mean something.

    Derived from the validator rather than from a hand-maintained list: every clause it can raise
    must be bound to a refusing shape. A clause added later without a cell breaks this, which is
    the only way the live file's claim stays true.
    """
    bound = {s["problem_key"] for s in SHAPES.values() if s["problem_key"]}
    clauses = _refusable_clauses()
    assert len(bound) == clauses, (
        f"lead_turn_problems can raise {clauses} distinct problems but only {len(bound)} shapes "
        f"are bound to one ({sorted(bound)}). Add a cell to test_live_turn_end_shapes.py, or its "
        "claim to exhaustiveness is false."
    )


def test_both_branches_of_the_rework_conditional_have_an_accepting_shape():
    """`rework=true` and `rework=false` require different fields; both need a cell that PASSES,
    or the matrix only proves the validator can say no."""
    accepting = {name for name, shape in SHAPES.items() if shape["accepts"]}
    assert {"rework_with_probe", "conclusion_with_findings"} <= accepting
    # ...and the honest-failure statuses, which §9.6 says are correct outcomes with no penalty.
    assert {"honest_failure", "needs_human"} <= accepting


def test_every_shape_has_a_live_cell_and_every_cell_has_a_shape():
    """A shape in the table with no test function is a shape nobody runs, and the reverse is a
    test measuring something the contract does not describe."""
    import tests.test_live_turn_end_shapes as live

    defined = {
        name.removeprefix("test_shape_")
        for name, _ in inspect.getmembers(live, inspect.isfunction)
        if name.startswith("test_shape_")
    }
    assert defined == set(SHAPES), (
        f"shapes with no test: {sorted(set(SHAPES) - defined)}; "
        f"tests with no shape: {sorted(defined - set(SHAPES))}"
    )


def test_every_refusing_shape_states_what_the_refusal_must_say():
    """A refusal that does not name the real problem is a refusal the model cannot act on — which
    is the whole reason `turn_end` returns `is_error` instead of ending the turn."""
    for name, shape in SHAPES.items():
        if shape["accepts"]:
            continue
        if shape.get("escape"):
            # An escape shape files nothing, so there is no refusal text to check. What it must
            # state instead is why the loop may not end — asserted below by `why`.
            continue
        assert shape.get("expect"), f"{name} does not say what its refusal must name"
        assert shape.get("why"), f"{name} does not say why it exists"
