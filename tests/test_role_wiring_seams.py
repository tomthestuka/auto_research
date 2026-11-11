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
    # The lead's and reviewer's read-only loops are built through their own seams, which
    # `run_research` calls (the lead's through `wire_lead`, which calls `attach_lead_runtime`), so a
    # factory counts as the run path's when one of them calls it.
    seams = {"wire_lead", "attach_reviewer_runtime"}
    for seam in seams:
        callers = {owners.get(line) for _, line in _calls_to(tree, (seam,))}
        assert "run_research" in callers, f"{seam} is not called by run_research"
    lead_seam = {owners.get(line) for _, line in _calls_to(tree, ("attach_lead_runtime",))}
    assert "wire_lead" in lead_seam, "wire_lead does not wire the lead through attach_lead_runtime"
    for factory in FACTORIES:
        callers = {owners.get(line) for _, line in _calls_to(tree, (factory,))}
        assert callers & ({"run_research", "attach_lead_runtime"} | seams), (
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
    """The probe's lead is wired by `wire_lead` (run_research's lead wiring, which builds its loop
    through `readonly_tools_for`) and its worker by `attach_worker_runtime`."""
    source = PROBE.read_text(encoding="utf-8")
    for factory in ("wire_lead", "attach_worker_runtime"):
        assert factory in source, f"the seat probe never calls {factory}"


def test_the_lead_and_reviewer_loops_differ_the_way_production_says():
    """The two read-only loops are not interchangeable, and the difference is the point.

    The lead investigates toolkit source and the stores' catalogs; the reviewer is the paper
    surface. A probe that gave the lead the reviewer's loop would measure a role the pipeline does
    not have, and nothing would fail. Both are offered the same login; only the lead declares it.
    """
    from auto_research.graph.runtime import readonly_tools_for
    from auto_research.store.keys import Credentials

    login = {"db": Credentials(store="db", unit="analyst_lead", run="r", role_class="reader",
                               principal="ar_r_analyst_lead_0", access_key_id="k", secret_access_key="s",
                               pg_password="p")}
    lead = readonly_tools_for("analyst_lead", roots=[REPO], catalog=login)
    reviewer = readonly_tools_for("reviewer", roots=[REPO], catalog=login)
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
    assert {"attach_lead_runtime", "wire_lead"} <= defined, "the lead has no wiring seam in runtime.py"
    owners = _enclosing_functions(tree)
    callers = {owners.get(line) for _, line in _calls_to(tree, ("wire_lead",))}
    assert "run_research" in callers, (
        f"wire_lead is not called by run_research (callers: {callers or 'none'}) — a "
        "seam the run path does not use describes a role production no longer builds"
    )
    attached = {owners.get(line) for _, line in _calls_to(tree, ("attach_lead_runtime",))}
    assert "wire_lead" in attached and "run_research" not in attached, (
        f"run_research wires the lead beside wire_lead (attach_lead_runtime callers: {attached})"
    )


def test_the_stop_drill_wires_its_lead_through_the_seam():
    """The live drill must not restate the lead's wiring; it must ask for production's."""
    drill = Path(__file__).with_name("test_live_stop_mid_drive.py")
    tree = _tree(drill)
    called = {name for name, _ in _calls_to(tree, ("wire_lead", "attach_lead_runtime", "readonly_tools_for"))}
    assert called == {"wire_lead"}, f"the drill hand-rolls the lead's wiring: {sorted(called)}"

    # The tell-tale of a hand-rolled equivalent is assigning the attributes the seam owns.
    source = drill.read_text(encoding="utf-8")
    for restated in ("lead.agent_key =", "lead.report_dir =", "lead.tools =", "lead.monitor ="):
        assert restated not in source, (
            f"the drill sets {restated.strip()} itself instead of letting the seam do it"
        )


def test_the_librarian_has_a_wiring_seam_and_run_research_uses_it():
    """`attach_librarian_runtime` is the run path's own librarian wiring (2026-10-28), so a live
    cell driving a real librarian asks for production's rather than restating it."""
    tree = _tree(RUNTIME)
    owners = _enclosing_functions(tree)
    callers = {owners.get(line) for _, line in _calls_to(tree, ("attach_librarian_runtime",))}
    assert "run_research" in callers, f"attach_librarian_runtime is not called by run_research ({callers})"


#: The fields the role seams own: a live cell that sets one itself restates production's wiring.
_SEAM_OWNED = {"agent_key", "report_dir", "tools", "receipts", "receipt_source", "monitor",
               "run_control", "channel"}


def _restated_fields(tree: ast.AST) -> list[str]:
    """Every seam-owned field the code assigns (``x.f = …``, ``x.y.f = …``, ``x.f += …``,
    ``setattr(x, "f", …)``, ``x.__dict__.update(f=…)``), with its line."""
    found: list[str] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:  # the field assigned is the target's last attribute
            for sub in (target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]):
                if isinstance(sub, ast.Attribute) and sub.attr in _SEAM_OWNED:
                    found.append(f"{sub.attr} (line {node.lineno})")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name == "setattr" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and node.args[1].value in _SEAM_OWNED:
                found.append(f"setattr {node.args[1].value} (line {node.lineno})")
            if name == "update" and isinstance(node.func, ast.Attribute) and \
                    getattr(node.func.value, "attr", "") == "__dict__":
                found += [f"__dict__ {k.arg} (line {node.lineno})" for k in node.keywords if k.arg in _SEAM_OWNED]
    return found


@pytest.mark.parametrize("cell, builder, role", [
    ("test_live_turn_end_shapes.py", "_wired_lead", "wire_lead"),
    ("test_live_librarian_shapes.py", "_drive", "attach_librarian_runtime"),
])
def test_the_shape_matrices_wire_their_role_through_the_seams_and_restate_nothing(cell, builder, role):
    """User rule 2026-10-28: tests use only module endpoints, never a hand-rolled equivalent. The
    builder calls the seams, and nowhere in the file is a field the seams own assigned by hand."""
    tree = _tree(Path(__file__).with_name(cell))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == builder)
    called = {name for name, _ in _calls_to(node, (role, "attach_channel"))}
    assert called == {role, "attach_channel"}, f"{cell}:{builder} does not wire through {role} and attach_channel"
    restated = _restated_fields(tree)
    assert not restated, f"{cell} sets seam-owned fields itself: {restated}"


def test_the_restatement_guard_sees_the_evasions_a_review_found():
    """Review 2026-11-03: the substring guard passed setattr, no-space assignment, aliasing and
    __dict__.update. The AST guard must see every one."""
    for code in ('setattr(lead, "agent_key", "x")', 'lead.agent_key="x"', "l = lead\nl.tools = None",
                 'lead.__dict__.update(receipts=1)', "lead.tools.receipts = s", "lead.run_control += 1"):
        assert _restated_fields(ast.parse(code)), code
    assert not _restated_fields(ast.parse("lead.tools.execute = record\nlead.model = counting"))


def test_run_research_wires_the_librarian_only_through_its_seam():
    tree = _tree(RUNTIME)
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_request")
    inline = [
        f"{sub.attr} (line {node.lineno})" for node in ast.walk(run) if isinstance(node, ast.Assign)
        for target in node.targets for sub in ast.walk(target)
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "io_agent"
        and sub.attr in _SEAM_OWNED
    ]
    assert not inline, f"run_request sets the librarian's fields inline: {inline}"


def test_the_librarian_seam_wires_identity_folder_stop_channel_and_the_runs_one_store(tmp_path, monkeypatch):
    from auto_research.agents.roles import LibrarianIOAgent
    from auto_research.graph import runtime
    from auto_research.provenance import ReceiptStore
    from auto_research.run_control import RunControl

    runs = tmp_path / "runtime" / "runs"  # the production layout: the ledger lands beside runs/
    monkeypatch.setattr(runtime, "agent_dir", lambda run_id, key: runs / run_id / key)
    monkeypatch.setattr(runtime, "run_dir", lambda run_id: runs / run_id)
    control = RunControl()
    agent = LibrarianIOAgent(sources={}, storage=None)
    runtime.attach_librarian_runtime(agent, run_id="r1", run_control=control)
    assert (agent.agent_key, agent.report_dir, agent.run_control) == ("librarian_io", runs / "r1" / "librarian_io", control)
    assert agent.receipts is ReceiptStore.for_run(runs / "r1")
    assert tmp_path in agent.receipts.root.parents  # nothing written outside this test's folder

    prewired = LibrarianIOAgent(sources={}, storage=None)
    prewired.agent_key, prewired.report_dir = "librarian_x", tmp_path / "mine"
    runtime.attach_librarian_runtime(prewired, run_id="r1")
    assert (prewired.agent_key, prewired.report_dir) == ("librarian_x", tmp_path / "mine")  # a pre-wired field stays


def test_no_live_cell_builds_a_seat_without_its_call_settings():
    """A bare `SeatOverride(provider, model)` drops the seat's pinned settings: an Anthropic seat then
    runs without thinking at the API's 1024-token fallback, which cut every long report draft of the
    2026-11-03 shape re-run mid-argument. Live cells use `tests.seats.seat` (pinned settings) or pass
    `parameters=` explicitly."""
    bare = []
    for path in sorted(Path(__file__).parent.glob("test_live_*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "SeatOverride" \
                    and not any(k.arg == "parameters" for k in node.keywords):
                bare.append(f"{path.name}:{node.lineno}")
    assert not bare, f"seats built without their call settings: {bare}"


# ---------------------------------------------------------------- W9c (2026-11-09): the seams the seat cells lacked

#: The live cells that drove these seams by hand before W9c (a shell server, a reviewer key, an
#: admitted list); each now asks production for them.
_W9C_CELLS = ("test_live_ref_chain.py", "test_live_provenance_html.py", "test_live_reviewer_paths.py")


class _NoModel:
    """A seat's model; the seams under test never call it."""


def _runs_in(tmp_path, monkeypatch):
    from auto_research import env as app_env
    from auto_research.graph import runtime

    runs = tmp_path / "runtime" / "runs"
    monkeypatch.setattr(runtime, "agent_dir", lambda run_id, key: runs / run_id / key)
    monkeypatch.setattr(runtime, "run_dir", lambda run_id: runs / run_id)
    monkeypatch.setattr(app_env, "agent_conda_dir", lambda: tmp_path / "conda")
    return runs


def test_run_research_builds_the_workers_shell_only_through_its_seam():
    """The workers' shell server is built in one place, `worker_mcp_client`, and the run path uses it."""
    tree = _tree(RUNTIME)
    owners = _enclosing_functions(tree)
    outside = [(name, line, owners.get(line)) for name, line in _calls_to(tree, ("_builtin_server_config", "MCPToolClient"))
               if owners.get(line) != "worker_mcp_client"]
    assert not outside, f"the shell server is built outside worker_mcp_client: {outside}"
    callers = {owners.get(line) for _, line in _calls_to(tree, ("worker_mcp_client",))}
    assert "run_research" in callers, f"run_research does not call worker_mcp_client ({callers})"


def test_the_workers_shell_seam_builds_the_builtin_server_for_the_workers_folders(tmp_path, monkeypatch):
    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.graph.runtime import SandboxSettings, worker_mcp_client
    from auto_research.sandbox import jail

    monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")  # the argv only; nothing starts

    runs = _runs_in(tmp_path, monkeypatch)
    ini = tmp_path / "activate.sh"
    ini.write_text("export A=1\n")
    (tmp_path / "toolkit").mkdir()
    import sys

    from auto_research.sandbox.envs import Environment

    settings = SandboxSettings(environment=Environment(name="host", prefix=Path(sys.prefix), activate=ini),
                               probe="probe-command")
    workers = [AnalystWorkerAgent(model=_NoModel(), prompt="p"), object(),
               AnalystWorkerAgent(model=_NoModel(), prompt="p")]
    client = worker_mcp_client(workers, run_id="r1", toolkit_root=tmp_path / "toolkit",
                               sandbox_settings=settings)
    [server] = client._servers
    assert server["name"] == "builtin" and server["cwd"] == str(runs / "r1")
    assert server["env"]["AR_ENV_PROBE"] == "probe-command"
    # Each worker's own folder is made writable (created for the bind); a seat that is not a worker gets none.
    assert (runs / "r1" / "analyst_worker_0").is_dir() and (runs / "r1" / "analyst_worker_2").is_dir()
    assert not (runs / "r1" / "analyst_worker_1").exists()
    assert Path(server["command"]).name == "bwrap"  # the workers' shell runs inside the jail


def test_the_reviewer_seam_wires_key_stop_channel_and_productions_web_tools(tmp_path, monkeypatch):
    from auto_research.agents.roles import ReviewerAgent
    from auto_research.graph import runtime
    from auto_research.provenance import ReceiptStore
    from auto_research.retrieval import SqliteRetrievalBackend
    from auto_research.run_control import RunControl
    from auto_research.sources import WebSource

    runs = _runs_in(tmp_path, monkeypatch)
    control = RunControl()
    reviewer = ReviewerAgent(model=_NoModel(), prompt="p")
    runtime.attach_reviewer_runtime(reviewer, run_id="r1", toolkit_root=tmp_path / "toolkit", run_control=control)
    assert (reviewer.agent_key, reviewer.run_control) == ("reviewer", control)
    assert reviewer.report_dir == runs / "r1" / "reviewer"
    assert reviewer.receipts is ReceiptStore.for_run(runs / "r1")
    # A caller that passes neither still gets production's knowledge base and web source.
    assert isinstance(reviewer.tools.retrieval, SqliteRetrievalBackend)
    assert isinstance(reviewer.tools.web_source, WebSource)

    mine, web = SqliteRetrievalBackend(), WebSource()
    prewired = ReviewerAgent(model=_NoModel(), prompt="p")
    prewired.agent_key = "reviewer_x"
    runtime.attach_reviewer_runtime(prewired, run_id="r1", toolkit_root=tmp_path / "toolkit",
                                    retrieval=mine, web_source=web)
    assert prewired.agent_key == "reviewer_x" and prewired.run_control is None  # a pre-wired key stays
    assert prewired.tools.retrieval is mine and prewired.tools.web_source is web


def test_run_research_keys_the_reviewer_only_through_its_seam():
    tree = _tree(RUNTIME)
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_research")
    inline = [
        f"{sub.attr} (line {node.lineno})" for node in ast.walk(run) if isinstance(node, ast.Assign)
        for target in node.targets for sub in ast.walk(target)
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "reviewer"
        and sub.attr in _SEAM_OWNED
    ]
    assert not inline, f"run_research sets the reviewer's fields inline: {inline}"


def test_a_reviewer_turn_keeps_the_scope_it_admitted(tmp_path, monkeypatch):
    """`ReviewerAgent.review` derives what it admits once (`reviewer_admitted_receipts`) and keeps it
    as `admitted_receipts`: the request's receipt, the literature's, what the analysis report cites,
    its figures' receipts and the human's files. Kept even when the turn's model call fails, since the
    scope is set before the loop; a check outside the turn reads it and mints nothing."""
    import asyncio

    from auto_research.agents.roles import ModelCallFailed, ReviewerAgent
    from auto_research.graph import runtime
    from auto_research.models.types import ModelHandle, RoleModelConfig
    from auto_research.schemas import AnalystReport, FigureRef, ResearchRequest
    from tests.test_provider_containment import _BrokenAdapter

    _runs_in(tmp_path, monkeypatch)
    reviewer = ReviewerAgent(model=ModelHandle(
        role="reviewer", config=RoleModelConfig(name="fake", provider="anthropic", model="fake-rev"),
        provider=_BrokenAdapter(ValueError("offline"))), prompt="p")
    runtime.attach_reviewer_runtime(reviewer, run_id="r1", toolkit_root=tmp_path / "toolkit")
    store = reviewer.receipts
    result = store.mint(source="analyst_lead", kind="result", produced_by="research_toolkit.t.fit", data="PD = 4.0")
    png = tmp_path / "plot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfigure")
    figure = store.mint(source="analyst_lead", kind="figure", produced_by="research_toolkit.t.plot", path=png,
                        caption="PD", mime_type="image/png")
    report = AnalystReport(summary=f"The fit gives {{{{{result.handle}#/pd}}}}.",
                           figures=[FigureRef(path=png, caption="PD", mime_type="image/png",
                                              produced_by="t.plot", sha256=figure.sha256,
                                              created_at="2026-11-09T00:00:00Z")])
    request = ResearchRequest(question="What is the polarization degree?")

    with pytest.raises(ModelCallFailed):
        asyncio.run(reviewer.review(request, arrived="analysts", literature_receipts={"lib:p1": None},
                                    report=report, human_files=["input:h9"]))
    (minted,) = [r for r in store.receipts(source="input") if r.produced_by == "research_request"]
    assert reviewer.admitted_receipts == [minted.handle, "lib:p1", result.handle, figure.handle, "input:h9"]


def test_reviewer_review_builds_its_scope_from_the_admitted_receipts():
    """One definition: `ReviewerAgent.review` derives its scope with `reviewer_admitted_receipts`."""
    roles = REPO / "src" / "auto_research" / "agents" / "roles.py"
    tree = _tree(roles)
    review = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "review"
                  and any(isinstance(p, ast.ClassDef) and p.name == "ReviewerAgent" and n in p.body
                          for p in ast.walk(tree)))
    assert len(_calls_to(review, ("reviewer_admitted_receipts",))) == 1, "ReviewerAgent.review derives its scope inline"
    assert len(_calls_to(review, ("_request_receipt",))) == 1  # the request's receipt is derived once


def _hand_built_admitted(tree: ast.AST) -> list[int]:
    """Lines where an admitted scope is built from a literal list rather than read from the agent:
    an `admitted=` keyword, or an assignment to `admitted` / `[...]["admitted"]`, whose value is a
    list (or comprehension) or names no `admitted_receipts`."""
    def names_the_kept_list(value: ast.AST) -> bool:
        return any(isinstance(n, ast.Attribute) and n.attr == "admitted_receipts" for n in ast.walk(value)) or \
            any(isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "admitted"
                for n in ast.walk(value))

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "admitted" and not names_the_kept_list(node.value):
            found.append(node.value.lineno)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in (target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]):
                    named = (isinstance(sub, ast.Name) and sub.id == "admitted") or (
                        isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Constant)
                        and sub.slice.value == "admitted")
                    if named and not names_the_kept_list(node.value):
                        found.append(node.lineno)
    return found


def test_the_hand_built_admitted_guard_sees_a_literal_list():
    assert _hand_built_admitted(ast.parse('out["admitted"] = [a, b]'))
    assert _hand_built_admitted(ast.parse('ScopedLookup(s, admitted=[*x, y.handle])'))
    assert _hand_built_admitted(ast.parse('admitted = reviewer_admitted_receipts("input:h1")'))
    assert not _hand_built_admitted(ast.parse('out["admitted"], out["receipts"] = reviewer.admitted_receipts, s'))
    assert not _hand_built_admitted(ast.parse('ScopedLookup(s, admitted=out["admitted"])'))


@pytest.mark.parametrize("cell", _W9C_CELLS)
def test_the_w9c_cells_build_no_shell_key_or_admitted_list_by_hand(cell):
    tree = _tree(Path(__file__).with_name(cell))
    assert not _calls_to(tree, ("_builtin_server_config", "MCPToolClient")), f"{cell} builds a shell server itself"
    keyed = [field for field in _restated_fields(tree) if field.startswith("agent_key")]
    assert not keyed, f"{cell} keys a seat itself: {keyed}"
    # Every reviewer runs its configured seat (no model passed), as `run_research` builds it.
    explicit = [line for name, line in _calls_to(tree, ("ReviewerAgent",))
                for node in ast.walk(tree) if isinstance(node, ast.Call) and node.lineno == line
                and any(k.arg == "model" for k in node.keywords)]
    assert not explicit, f"{cell} seats a reviewer by hand at {explicit}"
    assert not _hand_built_admitted(tree), f"{cell} builds an admitted scope by hand at {_hand_built_admitted(tree)}"
    if cell == "test_live_reviewer_paths.py":
        source = Path(__file__).with_name(cell).read_text(encoding="utf-8")
        assert ".admitted_receipts" in source, "the reviewer cells do not read the scope the turn kept"


def test_the_launch_stamps_the_runs_default_environment_through_its_seam(tmp_path, monkeypatch):
    """`stamp_run_environments` is the run path's own stamping (env plan B1): the run's store takes the
    orchestrator's fingerprint, and that state's lock is written beside the others."""
    from auto_research import env as app_env
    from auto_research.graph import runtime
    from auto_research.provenance import ReceiptStore

    runs = _runs_in(tmp_path, monkeypatch)
    monkeypatch.setattr(app_env, "env_lock_dir", lambda: tmp_path / "locks")
    fingerprint = runtime.stamp_run_environments("r1")
    assert len(fingerprint) == 64 and ReceiptStore.for_run(runs / "r1").default_env == fingerprint
    assert any((tmp_path / "locks").iterdir())

    tree = _tree(RUNTIME)
    owners = _enclosing_functions(tree)
    assert "run_research" in {owners.get(line) for _, line in _calls_to(tree, ("stamp_run_environments",))}
    outside = [line for _, line in _calls_to(tree, ("fingerprint_and_lock",))
               if owners.get(line) != "stamp_run_environments"]
    assert not outside, f"runtime.py fingerprints an environment outside its seam: {outside}"


def test_each_run_binds_the_contract_of_the_configuration_it_was_launched_with(tmp_path, monkeypatch):
    """A run reads its configuration once and binds its toolkit contract before its preflight; a
    second run in the same process with a changed configuration sees that configuration's contract
    whole, in its preflight and in the readers of the default environment (chosen over refusing:
    every other setting is read per run too)."""
    import asyncio

    from auto_research import toolkit_contract
    from auto_research.env import default_definition
    from auto_research.graph import runtime

    seen = []

    class _Stop(Exception):
        pass

    async def preflight(*, human_handler=None, monitor=None):
        seen.append((toolkit_contract.toolkit().root, default_definition().purpose))
        raise _Stop  # the run ends at its preflight: what was bound is all this test asks

    def config(name: str) -> dict:
        root = tmp_path / name
        (root / "envs" / "analysis").mkdir(parents=True)
        (root / "envs" / "analysis" / "env.yaml").write_text(f"purpose: {name}'s\nbase: host\n")
        return {"toolkits": [{"name": "research_toolkit", "root": str(root), "template_package": "demo_utils",
                              "authoring_branch": "ai", "default_env": "analysis"}]}

    monkeypatch.setattr(runtime, "run_preflight", preflight)
    for name in ("first", "second"):
        monkeypatch.setattr(runtime, "load_orchestration_config", lambda *a, name=name, **k: config(name))
        with pytest.raises(_Stop):
            asyncio.run(runtime.run_research("q", human_channel="none", monitor_jsonl=tmp_path / f"{name}.jsonl"))
    assert seen == [(tmp_path / "first", "first's"), (tmp_path / "second", "second's")]
    assert toolkit_contract.toolkit().root == tmp_path / "second"


def test_a_run_started_while_another_holds_the_contract_cannot_change_it(tmp_path, monkeypatch):
    """A second in-process run whose configuration declares another contract is refused while the
    first still runs (its readers would see the contract change under them); one with the same
    contract shares it; once the first has finished, a changed configuration binds."""
    import asyncio

    from auto_research import toolkit_contract
    from auto_research.graph import runtime

    class _Stop(Exception):
        pass

    def config(name: str) -> dict:
        return {"toolkits": [{"name": "research_toolkit", "root": str(tmp_path / name), "template_package": "demo_utils",
                              "authoring_branch": "ai", "default_env": "analysis"}]}

    configs: list[dict] = []
    nested: list[object] = []

    async def preflight(*, human_handler=None, monitor=None):
        if not nested:  # the first run, at its preflight: start others in this process meanwhile
            for name in ("second", "first"):
                configs.append(config(name))
                try:
                    await runtime.run_research("q", human_channel="none", monitor_jsonl=tmp_path / f"n{len(nested)}.jsonl")
                except (RuntimeError, _Stop) as exc:
                    nested.append(exc)
            assert toolkit_contract.toolkit().root == tmp_path / "first"  # unchanged under the first run
        raise _Stop

    monkeypatch.setattr(runtime, "run_preflight", preflight)
    monkeypatch.setattr(runtime, "load_orchestration_config", lambda *a, **k: configs.pop(0))
    configs.append(config("first"))
    with pytest.raises(_Stop):
        asyncio.run(runtime.run_research("q", human_channel="none", monitor_jsonl=tmp_path / "first.jsonl"))
    refused, shared = nested
    assert isinstance(refused, RuntimeError) and "another toolkit contract" in str(refused)
    assert isinstance(shared, _Stop)  # the same contract: it reached its own preflight
    configs.append(config("third"))  # every run has finished: a changed configuration binds
    with pytest.raises(_Stop):
        asyncio.run(runtime.run_research("q", human_channel="none", monitor_jsonl=tmp_path / "third.jsonl"))
    assert toolkit_contract.toolkit().root == tmp_path / "third" and not runtime._CONTRACT_RUNS
