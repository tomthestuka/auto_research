"""Free structural guards for the librarian's live shape matrix — ungated, run in the ordinary suite.

``tests/test_live_librarian_shapes.py`` claims to cover every path out of the librarian's loop. That
claim decays silently the moment a clause is added to its validator, so it is asserted here from
the validator's own source: every clause ``LibrarianToolProvider.turn_problems`` and the shared
``check_bindings`` can raise is bound to a live shape or named unreachable with a reason.
"""

from __future__ import annotations

import inspect

from tests.test_live_librarian_shapes import SHAPES, UNREACHABLE

#: One key per clause, in source order: the librarian's own clauses, then ``check_bindings``'s.
#: The count is checked against the source, so a new clause without a key fails.
LIBRARIAN_CLAUSES = (
    "no_report", "empty_summary", "success_without_findings", "finding_without_claim",
    "finding_without_support", "support_not_opened",
)
BINDING_CLAUSES = (
    "unresolved", "flagged_quote", "attribution_without_locator", "quote_without_text",
    "shell_echoed_number", "misquoted_span", "ungrounded_number",
)


def _clause_count(function, marker: str) -> int:
    source = inspect.getsource(function)
    return source.count(marker) + source.count("return [\n")


def test_the_clause_keys_match_the_validators_source():
    from auto_research.agents.librarian_tools import LibrarianToolProvider
    from auto_research.provenance.bindings import check_bindings

    assert _clause_count(LibrarianToolProvider.turn_problems, "problems.append(") == len(LIBRARIAN_CLAUSES)
    assert inspect.getsource(check_bindings).count("result.problems.append(") == len(BINDING_CLAUSES)


def test_every_clause_has_a_live_shape_or_a_stated_reason_it_cannot_arise():
    bound = {s["problem_key"] for s in SHAPES.values() if s["problem_key"]}
    for key in (*LIBRARIAN_CLAUSES, *BINDING_CLAUSES):
        assert key in bound or key in UNREACHABLE, (
            f"clause {key!r} has no live shape and no reason it is unreachable — add a cell to "
            "test_live_librarian_shapes.py, or its claim to exhaustiveness is false"
        )
    assert not (bound & set(UNREACHABLE)), "a clause cannot be both covered and unreachable"
    assert set(UNREACHABLE) <= set(BINDING_CLAUSES) | set(LIBRARIAN_CLAUSES)


def test_every_shape_has_a_live_cell_and_every_cell_has_a_shape():
    import tests.test_live_librarian_shapes as live

    defined = {
        name.removeprefix("test_shape_")
        for name, _ in inspect.getmembers(live, inspect.isfunction)
        if name.startswith("test_shape_")
    }
    assert defined == set(SHAPES), (
        f"shapes with no test: {sorted(set(SHAPES) - defined)}; "
        f"tests with no shape: {sorted(defined - set(SHAPES))}"
    )


def test_both_outcomes_and_every_escape_are_covered():
    accepting = {n for n, s in SHAPES.items() if s["accepts"]}
    assert {"succeeded_with_findings", "honest_failure"} <= accepting
    escapes = {n for n, s in SHAPES.items() if s.get("escape")}
    assert escapes == {"escape_by_prose", "escape_by_claiming_done", "escape_by_idling"}


def test_every_refusing_shape_states_what_its_refusal_must_name():
    for name, shape in SHAPES.items():
        if shape["accepts"] or shape.get("escape"):
            continue
        assert shape.get("expect"), f"{name} does not say what its refusal must name"
        assert shape.get("why"), f"{name} does not say why it exists"
