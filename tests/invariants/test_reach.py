"""Freezes CLAUDE.md §9.3.5 (reach is monotone down the tree) and §9.7 (subagents by grant).

Nothing of it exists before Phase 3 step 5, so each cell is a strict xfail on the condition that
``auto_research.units`` holds no ``Run`` yet: it fails today on the missing module, and when step 5
builds the module the condition lifts and the cell must pass, unchanged in what it asserts (a module
built in another shape fails the cell, a hard failure). The cells state the target through the API it
implies (``auto_research.units``: ``Reach``, ``Grant``, ``UnitDefinition``, ``Run``, ``SpawnRefused``),
reached only through the helpers below, which step 5 adapts in one place.

Spawn rights are the GRANT alone (§9.7 "on/off by grant"): a definition with no grant has no spawn
tool, and ``Reach`` holds the filesystem, network and environments. Reach, spawn rights and budgets
of a child are a subset of its parent's (§9.3.5): a grant names only definitions its parent's grant
names and may only lower the caps. The caps belong to the RUN (§9.7): ``Run(max_agents=, max_depth=)``;
``max_agents`` counts every unit instance the run creates, the starting unit included
(``max_agents=2`` admits one child), and ``max_depth`` counts generations from the starting unit
(depth 0); every unit of one run shares the count, and a second run counts afresh. The cells keep
each refusal beside its accepted counterpart.
"""

from __future__ import annotations

import importlib
import importlib.util

import pytest

STEP_5 = "built in Phase 3 step 5: subagents, kind A (grants, monotone reach, depth and count caps)"


def _units():
    """The step-5 module (one adaptation point for a renamed symbol)."""
    return importlib.import_module("auto_research.units")


def _built() -> bool:
    """Whether step 5 has landed: the module exists and spawns."""
    try:
        return hasattr(_units(), "Run")
    except ModuleNotFoundError:
        return False


not_built = pytest.mark.xfail(condition=not _built(), strict=True, reason=STEP_5)


def _reach(tmp_path, **over):
    """A reach: read-only toolkit, one writable folder, no network, one environment."""
    base = {"read": (tmp_path / "toolkit",), "write": (tmp_path / "scratch",), "network": False,
            "environments": ("demo",)}
    return _units().Reach(**{**base, **over})


def _run(*, max_agents=3, max_depth=2):
    """A run holding the caps every unit of it shares."""
    return _units().Run(max_agents=max_agents, max_depth=max_depth)


def _parent(tmp_path, *, children=("child", "grandchild"), **caps):
    """A unit definition granted ``children`` (``caps``: a grant's own ``max_agents``, ``max_depth``)."""
    units = _units()
    return units.UnitDefinition(name="parent", reach=_reach(tmp_path),
                                grant=units.Grant(children=children, **caps))


def _child(parent, name="child", grant=None, **reach):
    """A definition holding the parent's reach narrowed by ``reach`` (nothing narrowed by default)."""
    units = _units()
    extra = {} if grant is None else {"grant": grant}
    return units.UnitDefinition(name=name, reach=parent.reach.narrowed(**reach), **extra)


def _start(run, definition):
    """The run's starting unit (depth 0, the first instance of the count)."""
    return run.start(definition)


def _spawn(run, parent_unit, definition):
    """``definition`` spawned by ``parent_unit`` inside ``run``: a unit, or ``SpawnRefused``."""
    return run.spawn(parent_unit, definition)


def _wider(tmp_path, field):
    """The reach field ``field`` asking for more than ``_reach`` holds."""
    return {"network": {"network": True},
            "environments": {"environments": ("demo", "other")},
            "read": {"read": (tmp_path / "toolkit", tmp_path / "elsewhere")},
            "write": {"write": (tmp_path / "scratch", tmp_path / "elsewhere")}}[field]


@not_built
@pytest.mark.parametrize("field", ["network", "environments", "read", "write"])
def test_a_child_asking_for_more_reach_than_its_parent_is_refused_at_spawn(field, tmp_path):
    # §9.3.5 a child's filesystem reach, network and environments are a subset of its parent's
    units = _units()
    parent = _parent(tmp_path)
    run = _run()
    top = _start(run, parent)
    wider = units.UnitDefinition(name="child", reach=_reach(tmp_path, **_wider(tmp_path, field)))
    with pytest.raises(units.SpawnRefused, match="reach"):
        _spawn(run, top, wider)
    # The accepted counterpart: a child holding a subset of the parent's reach spawns.
    assert _spawn(run, top, _child(parent, write=())).definition.name == "child"


@not_built
@pytest.mark.parametrize("cap", ["max_agents", "max_depth"])
def test_a_grant_asking_for_larger_budgets_than_the_run_holds_is_refused_at_spawn(cap, tmp_path):
    # §9.3.5 a child's budgets are a subset of its parent's; §9.7 a grant sets lower caps, never higher
    units = _units()
    parent = _parent(tmp_path)
    run = _run(max_agents=3, max_depth=3)
    top = _start(run, parent)
    held = {"max_agents": 3, "max_depth": 3}[cap]
    greedy = units.Grant(children=("grandchild",), **{cap: held + 1})
    with pytest.raises(units.SpawnRefused, match=cap):
        _spawn(run, top, _child(parent, grant=greedy))
    # The accepted counterpart: a grant with a lower cap spawns.
    modest = units.Grant(children=("grandchild",), **{cap: held - 1})
    assert _spawn(run, top, _child(parent, grant=modest)).definition.name == "child"


@not_built
def test_a_child_cannot_be_granted_a_definition_its_parent_may_not_spawn(tmp_path):
    # §9.3.5 a child's spawn rights are a subset of its parent's
    units = _units()
    parent = _parent(tmp_path, children=("child", "grandchild"))
    run = _run()
    top = _start(run, parent)
    beyond = units.Grant(children=("sibling",))  # the parent's grant does not name it
    with pytest.raises(units.SpawnRefused, match="grant"):
        _spawn(run, top, _child(parent, grant=beyond))
    # The accepted counterpart: a grant naming a definition the parent may spawn.
    within = units.Grant(children=("grandchild",))
    assert _spawn(run, top, _child(parent, grant=within)).definition.name == "child"


@not_built
def test_a_unit_without_a_grant_has_no_spawn_right(tmp_path):
    # §9.7 on/off by grant: a definition with no grant spawns nothing
    units = _units()
    granted = _parent(tmp_path)
    ungranted = units.UnitDefinition(name="parent", reach=_reach(tmp_path))
    run = _run()
    with pytest.raises(units.SpawnRefused, match="grant"):
        _spawn(run, _start(run, ungranted), _child(granted))
    # The accepted counterpart: the same child spawns under a definition that grants it.
    other = _run()
    assert _spawn(other, _start(other, granted), _child(granted)).definition.name == "child"


@not_built
def test_a_definition_the_grant_does_not_name_is_refused_no_general_purpose_child(tmp_path):
    # §9.7 a grant names the definitions a unit may spawn; there is no general-purpose child
    units = _units()
    parent = _parent(tmp_path)
    run = _run()
    top = _start(run, parent)
    with pytest.raises(units.SpawnRefused, match="grant"):
        _spawn(run, top, _child(parent, name="stranger"))
    assert _spawn(run, top, _child(parent)).definition.name == "child"  # the named definition spawns


@not_built
def test_the_agent_count_is_run_wide_counts_the_starting_unit_and_refuses_the_spawn_past_it(tmp_path):
    # §9.7 max_agents counts every unit instance the run creates, the starting unit included
    units = _units()
    parent = _parent(tmp_path)
    run = _run(max_agents=3, max_depth=2)  # the parent, a child and one more
    top = _start(run, parent)
    child = _spawn(run, top, _child(parent, grant=units.Grant(children=("grandchild",))))
    # A descendant's spawn draws on the same count as its parent's.
    assert _spawn(run, child, _child(parent, name="grandchild")).definition.name == "grandchild"
    with pytest.raises(units.SpawnRefused, match="max_agents"):
        _spawn(run, top, _child(parent))
    # The accepted counterpart: another run counts afresh.
    other = _run(max_agents=3, max_depth=2)
    assert _spawn(other, _start(other, parent), _child(parent)).definition.name == "child"


@not_built
def test_the_depth_cap_refuses_a_grandchild_past_it(tmp_path):
    # §9.7 max_depth counts generations from the starting unit; a child spawns only if its own definition grants it
    units = _units()

    def tree(max_depth):
        parent = _parent(tmp_path)
        run = _run(max_agents=3, max_depth=max_depth)
        # The child's own definition grants a grandchild: only the depth cap can refuse it.
        child = _spawn(run, _start(run, parent), _child(parent, grant=units.Grant(children=("grandchild",))))
        return run, child, _child(parent, name="grandchild")

    run, child, grandchild = tree(max_depth=1)
    with pytest.raises(units.SpawnRefused, match="max_depth"):
        _spawn(run, child, grandchild)
    # The accepted counterpart: one more level of depth lets the same grandchild spawn.
    run, child, grandchild = tree(max_depth=2)
    assert _spawn(run, child, grandchild).definition.name == "grandchild"
