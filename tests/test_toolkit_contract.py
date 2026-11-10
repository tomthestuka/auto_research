"""The orchestration pairs with any toolkit that meets the contract (Phase 3 step 0a, checkpoint 1).

A minimal fixture toolkit (``tests/fixtures/toolkit.py``) is driven through the production entry
points, configured the way a run is (``AUTO_RESEARCH_CONFIG_DIR``): the registry loads it, the launch
checks pass on it, the gate passes a small change in it with its tests in the environment its package
names, and a worker's turn on it (its model scripted) mints its toolkit results as evidence. No
network. The gate and worker parts run in the bwrap jail (every runtime process does, D25); on a
host where bwrap cannot run they skip, and the checkpoint has not been shown.

The toolkit contract (``auto_research.toolkit_contract``, the ``toolkits:`` block) is driven by
the launch checks and by a toolkit declared under another name than ``research_toolkit``. The gate
runs as the toolkit's CI pipeline: every job reported with its status, and a ``data`` test it
skips fails the change.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

from auto_research import toolkit_contract
from auto_research.config_paths import CONFIG_DIR_ENV
from tests.fixtures import toolkit as fixture

PACKAGES = (fixture.PKG_NAME, fixture.OTHER_PKG)
ANALYSIS_PREFIX = Path(sys.prefix)  # this interpreter, with pytest: the analysis environment


def _forget_packages() -> None:
    """Drop any of the fixture's package names from this process's module cache (a test of another
    module may have imported a same-named one), so nothing is served from it."""
    for name in [m for m in sys.modules if m.split(".")[0] in PACKAGES]:
        del sys.modules[name]


@pytest.fixture
def make_toolkit(tmp_path, tmp_path_factory, monkeypatch):
    """``make_toolkit(toolkit=..., contract=..., **config overrides) -> root``: the fixture
    toolkit, and this process configured to run on it. ``contract``: fields of its ``toolkits:``
    entry to replace (``None`` leaves a field out)."""

    def make(*, toolkit: str = fixture.TOOLKIT_NAME, contract: dict | None = None, **overrides) -> Path:
        root = fixture.build_toolkit(tmp_path / "toolkit", env_base=ANALYSIS_PREFIX.name, toolkit=toolkit)
        if contract is not None:
            overrides["toolkits"] = _contract(root, **contract)
        config = fixture.write_config(tmp_path / "config", root, analysis_prefix=ANALYSIS_PREFIX,
                                      allow=(tmp_path_factory.getbasetemp(),), **overrides)
        monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
        toolkit_contract.reset()
        return root

    _forget_packages()
    yield make
    _forget_packages()


def _need_jail() -> None:
    """A toolkit tool routed to a pipeline environment runs only in the jail (and the fixture's
    configuration turns it on): skip where this host cannot run it, saying the checkpoint did not run."""
    from auto_research.sandbox.jail import jail_available

    ok, why = jail_available()
    if not ok:
        pytest.skip(f"step 0a checkpoint NOT run: it needs the bwrap jail, which cannot run here ({why})")


def _toolkit_checks():
    """The launch checks production registers for the toolkit (every ``toolkit_*`` name), as one
    checklist."""
    from auto_research.preflight import Preflight, build_default_preflight

    checks = Preflight()
    for name, check in build_default_preflight()._checks:
        if name.startswith("toolkit_"):
            checks.register(name, check)
    return checks


def _contract(root: Path, **fields) -> list[dict]:
    """The ``toolkits:`` block describing the fixture, with ``fields`` replaced (``None`` leaves a
    field out)."""
    entry = {"name": fixture.TOOLKIT_NAME, "root": str(root), "package_glob": "*_utils",
             "template_package": fixture.PKG_NAME, "authoring_branch": fixture.BRANCH,
             "default_env": fixture.ENV_ID}
    entry.update(fields)
    return [{k: v for k, v in entry.items() if v is not None}]


# ── the orchestrator runs no toolkit code (D25) ───────────────────────────────

#: The toolkit root the audit hook watches (``None``: off) and what it saw run from there.
_WATCH: dict = {"root": None, "seen": []}


def _audit(event: str, args: tuple) -> None:
    """Record an ``import``/``exec``/``compile`` of a file under the watched root. ``ast.parse``
    compiles as ``<unknown>``, so reading a manifest as data is not one."""
    root = _WATCH["root"]
    if root is None or event not in ("import", "exec", "compile"):
        return
    if event == "import":
        filename = args[1] if len(args) > 1 else None
    elif event == "exec":
        filename = getattr(args[0], "co_filename", None) if args else None
    else:
        filename = args[1] if len(args) > 1 else None
    if isinstance(filename, (str, bytes, os.PathLike)):
        try:
            if Path(os.fsdecode(filename)).resolve().is_relative_to(root):
                _WATCH["seen"].append((event, os.fsdecode(filename)))
        except (OSError, ValueError):
            pass


@pytest.fixture
def toolkit_code_watch():
    """``watch(root)`` turns on the process's audit hook for ``root`` (installed once: audit hooks
    cannot be removed) and returns the list of what ran from there; off again after the test."""
    if not getattr(_audit, "installed", False):
        sys.addaudithook(_audit)
        _audit.installed = True

    def watch(root: Path) -> list:
        _WATCH.update(root=Path(root).resolve(), seen=[])
        return _WATCH["seen"]

    yield watch
    _WATCH.update(root=None, seen=[])


def test_the_audit_watch_sees_toolkit_code_run_in_this_process(tmp_path, toolkit_code_watch):
    """The oracle below can fail: running a toolkit file here is seen."""
    import runpy

    (tmp_path / "tk").mkdir()
    (tmp_path / "tk" / "planted.py").write_text("VALUE = 1\n")
    seen = toolkit_code_watch(tmp_path / "tk")
    runpy.run_path(str(tmp_path / "tk" / "planted.py"))
    assert any(path.endswith("planted.py") for _, path in seen), seen


def test_the_launch_path_imports_no_toolkit_module_into_the_orchestrator(make_toolkit, tmp_path,
                                                                        toolkit_code_watch):
    """Preflight's toolkit and environment checks, the registry's load and reload, the catalog
    health check and the lead's catalog read: every toolkit module they need runs in the jail.
    Afterwards no module of this process comes from the toolkit root and the root is not on its
    path."""
    import asyncio

    import yaml

    from auto_research.agents import react
    from auto_research.preflight import Preflight, build_default_preflight
    from auto_research.sandbox.registry import ToolkitRegistry

    _need_jail()
    root = make_toolkit()
    (root / "catalog_utils" / "lib").mkdir(parents=True)
    (root / "catalog_utils" / "skills").mkdir()
    (root / "catalog_utils" / "__init__.py").write_text("from .skills import TOOL_MANIFEST\n")
    (root / "catalog_utils" / "skills" / "__init__.py").write_text(
        f"TOOL_MANIFEST = {{'{fixture.TOOLKIT_NAME}.catalog_utils.data_catalog': {{'module': "
        "'catalog_utils.lib.catalog_read', 'fn': 'data_catalog', 'description': 'local data', "
        "'args_schema': {'type': 'object', 'properties': {'obs_db_path': {'type': 'string', 'kind': 'path'}}}}}\n")
    (root / "catalog_utils" / "lib" / "__init__.py").write_text("")
    (root / "catalog_utils" / "README.md").write_text("# catalog_utils\n\nThe local data catalog.\n")
    (root / "catalog_utils" / "lib" / "catalog_read.py").write_text(
        "def data_catalog(obs_db_path, min_level=None):\n    return 'read ' + obs_db_path\n")
    (root / "obsdb_catalog").mkdir()
    (root / "obsdb_catalog" / "__init__.py").write_text("")
    # It leaves a mark beside the catalog it checked: the check really ran (in the jail).
    (root / "obsdb_catalog" / "healthcheck.py").write_text(
        "import os\ndef healthcheck(path):\n    open(os.path.join(path, 'checked'), 'w').close()\n    return True\n")
    import subprocess

    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                    "catalog"], check=True, capture_output=True)
    (tmp_path / "obsdb" / "DEMO").mkdir(parents=True)
    (tmp_path / "obsdb" / "DEMO" / "catalog.json").write_text('{"entries": {}}')
    from auto_research.config_paths import config_dir

    (config_dir() / "observations.yaml").write_text(yaml.safe_dump({"obs_db_root": str(tmp_path / "obsdb")}))
    resolved = root.resolve()

    def from_toolkit() -> list[str]:
        return sorted(name for name, module in list(sys.modules.items())
                      if getattr(module, "__file__", None)
                      and Path(module.__file__).resolve().is_relative_to(resolved))

    before = from_toolkit()
    seen = toolkit_code_watch(resolved)
    checks = Preflight()
    wanted = {"config_keys", "os_jail_available", "toolkit_contract", "toolkit_manifests", "catalog_health",
              "human_envs", "private_variables", "analysis_probe"}
    for name, check in build_default_preflight()._checks:
        if name in wanted:
            checks.register(name, check)
    asyncio.run(checks.run_checklist())
    registry = ToolkitRegistry(root)
    registry.load()
    registry.reload()
    assert f"{fixture.TOOLKIT_NAME}.catalog_utils.data_catalog" in registry.list_tools()
    out = asyncio.run(react.execute_data_catalog({"obs_db_path": str(tmp_path / "obsdb" / "DEMO")}))
    assert not out.is_error and out.text == f"read {tmp_path / 'obsdb' / 'DEMO'}", out.text
    assert (tmp_path / "obsdb" / "DEMO" / "checked").exists(), "the catalog health check never ran"
    assert seen == [], seen
    assert from_toolkit() == before == []
    assert not any(Path(entry).resolve() == resolved for entry in sys.path if entry)


# ── registry ─────────────────────────────────────────────────────────────────


def test_the_registry_loads_every_package_with_every_argument_kind(make_toolkit):
    from auto_research.provenance.inputs import ARG_KINDS, OUTPUT_SHAPES, declared_kinds, declared_outputs
    from auto_research.sandbox.registry import ToolkitRegistry

    root = make_toolkit()
    registry = ToolkitRegistry(root)
    registry.load()
    assert registry.import_errors == {} and registry.manifest_errors == []
    tools = registry.list_tools()
    assert {name.split(".")[1] for name in tools} == set(PACKAGES)
    schemas = [registry.spec(name)["args_schema"] for name in tools]
    assert {kind for s in schemas for kind in declared_kinds(s).values()} == set(ARG_KINDS)
    assert {shape for s in schemas for shape in declared_outputs(s).values()} == set(OUTPUT_SHAPES)
    assert {registry.tool_env(name) for name in tools} == {fixture.ENV_ID}
    assert {registry.call_timeout_ceiling(name) for name in tools} == {60}
    assert len(registry.tool_definitions()) == len(tools)


# ── launch checks ────────────────────────────────────────────────────────────


def test_the_launch_toolkit_checks_pass_on_the_fixture(make_toolkit):
    make_toolkit()
    checks = _toolkit_checks()
    assert {"toolkit_contract", "toolkit_git_identity", "toolkit_branch_clean",
            "toolkit_manifests"} <= set(checks.names())
    asyncio.run(checks.run_checklist())


def _commit_data_test(root: Path, name: str) -> None:
    """A committed test of the ``data`` class naming the data set ``name``."""
    (root / fixture.PKG_NAME / "tests" / "test_rows_on_data.py").write_text(
        f'import pytest\n\n\n@pytest.mark.data("{name}")\ndef test_rows():\n    assert True\n', encoding="utf-8")
    fixture.git(root, "add", "-A")
    fixture.git(root, "commit", "-qm", "a data test")


def test_the_launch_refuses_a_data_set_the_toolkits_tests_name_and_the_host_lacks(make_toolkit, tmp_path):
    """Read statically from the test sources (no test collected): a data set a test names must be
    declared in test_data, and every declared folder must exist."""
    from auto_research.preflight import PreflightError

    root = make_toolkit(test_data={"elsewhere": str(tmp_path)})
    _commit_data_test(root, "demo_rows")
    assert "toolkit_test_data" in set(_toolkit_checks().names())
    with pytest.raises(PreflightError, match=r"(?s)toolkit_test_data.*test_rows_on_data\.py names data set "
                                             r"'demo_rows'.*add `demo_rows: <folder>`"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_the_launch_refuses_a_declared_data_set_whose_folder_is_absent(make_toolkit, tmp_path):
    from auto_research.preflight import PreflightError

    make_toolkit(test_data={"demo_rows": str(tmp_path / "absent")})
    with pytest.raises(PreflightError, match=r"test_data\.demo_rows: .*absent is not a folder"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_the_data_sets_a_toolkit_names_are_read_from_every_spelling_of_the_mark(make_toolkit, tmp_path):
    """`mark` imported from pytest under any name, and a name bound to the data mark, are read like
    `pytest.mark.data`; a name that is not one string literal is refused, naming file and line."""
    from auto_research.preflight import PreflightError

    root = make_toolkit(test_data={"a": str(tmp_path), "b": str(tmp_path), "c": str(tmp_path)})
    (root / fixture.PKG_NAME / "tests" / "test_spellings.py").write_text(
        "import pytest\nfrom pytest import mark as m\n\nneeds = pytest.mark.data\nSET = 'c'\n\n\n"
        "@m.data('a')\ndef test_a():\n    pass\n\n\n@needs('b')\ndef test_b():\n    pass\n\n\n"
        "@pytest.mark.data(SET)\ndef test_c():\n    pass\n", encoding="utf-8")
    fixture.git(root, "add", "-A")
    fixture.git(root, "commit", "-qm", "spellings")
    needs, problems = toolkit_contract.data_needs()
    assert sorted(needs) == ["a", "b"]
    assert problems == [f"{fixture.PKG_NAME}/tests/test_spellings.py:18: data(...) takes the data set's name "
                        'as one string literal, data("<name>")'], problems
    with pytest.raises(PreflightError, match=r"test_spellings\.py:18"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_the_launch_passes_a_data_set_the_host_declares(make_toolkit, tmp_path):
    root = make_toolkit(test_data={"demo_rows": str(tmp_path)})
    _commit_data_test(root, "demo_rows")
    asyncio.run(_toolkit_checks().run_checklist())


def test_the_launch_checks_refuse_a_toolkit_with_uncommitted_files(make_toolkit):
    from auto_research.preflight import PreflightError

    root = make_toolkit()
    (root / fixture.PKG_NAME / "lib" / "stray.py").write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(PreflightError, match=r"(?s)toolkit_branch_clean.*demo_utils/lib/stray\.py"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_launch_refuses_a_manifest_key_outside_the_declared_toolkit_name(make_toolkit):
    from auto_research.preflight import PreflightError

    make_toolkit(contract={"name": "demo_toolkit"})  # its manifest keys say research_toolkit
    with pytest.raises(PreflightError, match=r"research_toolkit\.demo_utils\.\w+.*demo_toolkit"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_launch_refuses_a_template_package_the_toolkit_lacks(make_toolkit):
    from auto_research.preflight import PreflightError

    make_toolkit(contract={"template_package": "absent_utils"})
    with pytest.raises(PreflightError, match=r"template_package.*absent_utils"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_launch_refuses_a_default_environment_the_toolkit_does_not_define(make_toolkit):
    from auto_research.preflight import PreflightError

    make_toolkit(contract={"default_env": "absent"})
    with pytest.raises(PreflightError, match=r"default_env 'absent' has no definition"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_launch_refuses_a_toolkit_package_without_its_readme_briefing(make_toolkit):
    """The roles read a package's domain from its README.md (W6), so the launch refuses a package
    without one, naming it."""
    from auto_research.preflight import PreflightError

    root = make_toolkit()
    readme = next(p for p in sorted(root.glob("*_utils/README.md")))
    readme.unlink()
    with pytest.raises(PreflightError, match=rf"README.md.*{readme.parent.name}/README.md"):
        asyncio.run(_toolkit_checks().run_checklist())


def test_launch_refuses_a_toolkit_with_no_authoring_branch(make_toolkit):
    from auto_research.preflight import PreflightError

    make_toolkit(contract={"authoring_branch": None})
    with pytest.raises(PreflightError, match="authoring_branch"):
        asyncio.run(_toolkit_checks().run_checklist())


# ── the declared name and the evidence predicate (§2b) ───────────────────────


def _declare(tmp_path: Path, monkeypatch, name: str) -> None:
    """Configure this process with one toolkit declared under ``name`` (the ``toolkits:`` block only:
    the evidence predicate reads nothing else)."""
    config = tmp_path / f"config-{len(list(tmp_path.glob('config-*')))}"
    config.mkdir()
    entry = {"name": name, "root": str(tmp_path), "template_package": fixture.PKG_NAME,
             "authoring_branch": fixture.BRANCH, "default_env": fixture.ENV_ID}
    import yaml

    (config / "orchestration.yaml").write_text(yaml.safe_dump({"toolkits": [entry]}), encoding="utf-8")
    monkeypatch.setenv(CONFIG_DIR_ENV, str(config))
    toolkit_contract.reset()


#: Producers the harness mints, as the runtime writes them: every listed name, two MCP kinds and a
#: parsed document's figure.
def _harness_producers() -> list[str]:
    from auto_research.provenance.store import HARNESS_PRODUCERS

    return sorted({*HARNESS_PRODUCERS, "mcp.builtin.bash_exec", "mcp.builtin.read_file",
                   "mcp.other_utils.anything", "docling:paper.pdf"})


def test_a_toolkit_name_a_harness_producer_starts_with_is_refused_at_launch(tmp_path, monkeypatch):
    """B2: a name that equals or prefixes a harness producer, or is no identifier, never validates,
    so the launch refuses it before any receipt is judged."""
    from auto_research.preflight import PreflightError
    from auto_research.toolkit_contract import harness_names

    names = sorted(harness_names()) + ["mcp.builtin", "research_toolkit.", "", "a-b", "read file"]
    assert {"mcp", "read_file", "web_fetch", "input", "approve_edit"} <= set(names)
    for name in names:
        _declare(tmp_path, monkeypatch, name)
        with pytest.raises(PreflightError, match="toolkit_contract"):
            asyncio.run(_toolkit_checks().run_checklist())


def test_no_declarable_toolkit_name_makes_a_harness_receipt_evidence(tmp_path, monkeypatch):
    """B2, the invariant: under every name the contract accepts (here: every dotted segment of every
    harness producer that validates, and the fixture's), no receipt a harness producer minted is
    evidence; a tool of the declared toolkit is (the control)."""
    from pydantic import ValidationError

    from auto_research.provenance import ReceiptStore
    from auto_research.provenance.store import is_evidence
    from auto_research.toolkit_contract import ToolkitContract

    store = ReceiptStore(tmp_path / "receipts")
    producers = _harness_producers()
    receipts = [store.mint(source="analyst_worker_0", kind="result", produced_by=p, data=f"from {p}")
                for p in producers]
    candidates = {seg for p in producers for seg in re.split(r"[.:]", p)} | {fixture.TOOLKIT_NAME, "demo_toolkit"}
    accepted = []
    for name in sorted(candidates):
        try:
            ToolkitContract(name=name, root=tmp_path, template_package=fixture.PKG_NAME,
                            authoring_branch=fixture.BRANCH, default_env=fixture.ENV_ID)
        except ValidationError:
            continue
        accepted.append(name)
        _declare(tmp_path, monkeypatch, name)
        assert [r.produced_by for r in receipts if is_evidence(r)] == [], name
        control = store.mint(source="analyst_worker_0", kind="result",
                             produced_by=f"{name}.{fixture.PKG_NAME}.band_pa_summary", data=name)
        assert is_evidence(control), name
    assert {"builtin", "bash_exec", "other_utils", fixture.TOOLKIT_NAME} <= set(accepted)


#: Calls that mint a receipt, and the position of their producer argument when it is positional.
_MINTING = {"_receipted": 2, "_web_receipted": None, "receipt_call": None, "mint": None, "mint_input": None,
            "mint_paper_receipt": None, "mint_web_receipt": None}

#: The producer arguments whose value is computed at run time, each with why it mints no new harness
#: producer. A new site fails the test until it resolves statically or is added here with its reason.
_DYNAMIC_PRODUCERS = {
    ("agents/react.py", "execute", "kind"):
        "a worker's registry kind: a toolkit key (`<name>.<package>.<tool>`) or an `mcp.<server>.<tool>`",
    ("agents/react.py", "execute", "producer"):
        "ReadOnlyToolProvider: a `_RECEIPTED` tool name, or the catalog's toolkit kind (`_catalog_kind`)",
    ("schemas/artifacts.py", "to_figure_ref", "self.produced_by"): "copies a minted receipt's producer",
}


def _scan_producers() -> tuple[set[str], set[tuple[str, str, str]]]:
    """Every producer ``src/`` mints, read from its syntax: ``(literals, unresolved sites)``.

    Each minting call's producer argument (keyword ``produced_by``, or ``_receipted``'s positional
    name) and each ``produced_by`` default is resolved: a literal, a module or local constant
    (also assigned then passed), either arm of a conditional, an f-string's or ``.format``'s literal
    head up to ``:``, a parameter narrowed by an enclosing ``if <param> == X`` / ``in (X, Y)``, a
    minting wrapper's own parameter (its callers are scanned), ``_catalog_kind()`` (a toolkit kind).
    What does not resolve is returned as ``(file, function, expression)``."""
    import ast

    src = Path(__file__).resolve().parents[1] / "src" / "auto_research"
    found: set[str] = set()
    unresolved: set[tuple[str, str, str]] = set()
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
                  for t in n.targets if isinstance(t, ast.Name)}

        def enclosing(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node
            return None

        def narrowed(node, name):
            out, child = [], node
            while child in parents:
                parent = parents[child]
                if (isinstance(parent, ast.If) and child in parent.body and isinstance(parent.test, ast.Compare)
                        and getattr(parent.test.left, "id", None) == name):
                    for op, comp in zip(parent.test.ops, parent.test.comparators):
                        if isinstance(op, ast.Eq):
                            out.append(comp)
                        elif isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                            out.extend(comp.elts)
                child = parent
            return out

        def resolve(expr, depth=0):
            if depth > 8:
                return None
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                return {expr.value}
            if isinstance(expr, ast.IfExp):
                arms = [resolve(expr.body, depth + 1), resolve(expr.orelse, depth + 1)]
                return None if None in arms else arms[0] | arms[1]
            if isinstance(expr, ast.JoinedStr):
                head = expr.values[0] if expr.values else None
                return {head.value.split(":")[0]} if isinstance(head, ast.Constant) and ":" in head.value else None
            if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "format"
                    and isinstance(expr.func.value, ast.Constant)):
                head = expr.func.value.value.split("{")[0]
                return {head.split(":")[0]} if ":" in head else None
            if isinstance(expr, ast.Call) and getattr(expr.func, "id", None) == "_catalog_kind":
                return set()  # a toolkit kind
            if isinstance(expr, ast.Name):
                fn = enclosing(expr)
                if fn is not None:
                    assigned = [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
                                and any(getattr(t, "id", None) == expr.id for t in n.targets)]
                    if assigned:
                        values = [resolve(v, depth + 1) for v in assigned]
                        return None if None in values else set().union(*values)
                    if expr.id in {a.arg for a in fn.args.args + fn.args.kwonlyargs}:
                        if fn.name in _MINTING or expr.id == "produced_by":
                            return set()  # a minting wrapper's pass-through: its callers are scanned
                        comps = narrowed(expr, expr.id)
                        values = [resolve(c, depth + 1) for c in comps]
                        return None if not comps or None in values else set().union(*values)
                if expr.id in consts:
                    return {consts[expr.id]}
            return None

        def take(expr, where):
            values = resolve(expr)
            if values is None:
                fn = enclosing(where)
                unresolved.add((str(path.relative_to(src)), fn.name if fn else "<module>", ast.unparse(expr)))
            else:
                found.update(values)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                expr = next((k.value for k in node.keywords if k.arg == "produced_by"), None)
                if expr is None and _MINTING.get(name) is not None and len(node.args) > _MINTING[name]:
                    expr = node.args[_MINTING[name]]
                if expr is not None:
                    take(expr, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.args + node.args.kwonlyargs
                defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + node.args.defaults \
                    + node.args.kw_defaults
                for arg, default in zip(args, defaults):
                    if arg.arg == "produced_by" and default is not None:
                        take(default, default)
    return found, unresolved


def test_every_producer_the_harness_writes_is_a_listed_harness_producer():
    """L2 / #6: the list the name guard reads (``HARNESS_PRODUCERS``) covers every producer the code
    mints. The scan is closed: a producer argument that does not resolve from the syntax must be a
    listed run-time site (``_DYNAMIC_PRODUCERS``), and the one table such a site reads is checked here."""
    from auto_research.agents.react import DATA_CATALOG_TOOL, ReadOnlyToolProvider
    from auto_research.provenance.store import HARNESS_PRODUCERS

    found, unresolved = _scan_producers()
    assert {"read_file", "web_fetch", "web_search", "approve_edit", "toolspec", "dispatch", "docling",
            "use_env", "list_env", "read_paper", "research_request"} <= found
    assert sorted(found - HARNESS_PRODUCERS) == []
    assert unresolved == set(_DYNAMIC_PRODUCERS), sorted(unresolved ^ set(_DYNAMIC_PRODUCERS))
    # The read-only site's names: each receipted under its own name, the catalog under its kind.
    assert set(ReadOnlyToolProvider._RECEIPTED) - {DATA_CATALOG_TOOL} <= HARNESS_PRODUCERS


# ── the gate ─────────────────────────────────────────────────────────────────

_TOTALS_LIB = '''\
"""Total counts per band over a reduced product CSV."""

from pathlib import Path

from demo_utils.lib.demo_help import read_band_rows


def band_totals(csv_path, out_dir):
    """Total low and high counts, written to ``<out_dir>/band_totals.csv`` and declared as a product."""
    rows = read_band_rows(csv_path)
    low = sum(r["low_counts"] for r in rows)
    high = sum(r["high_counts"] for r in rows)
    table = Path(out_dir) / "band_totals.csv"
    table.write_text(f"band,counts\\nlow,{low!r}\\nhigh,{high!r}\\n", encoding="utf-8")
    return {"stdout": f"low={low!r} high={high!r}",
            "products": [{"path": str(table), "caption": "Total counts per band"}]}
'''

_TOTALS_ENTRY = '''\
    "%s.demo_utils.band_totals": {
        "module": "demo_utils.lib.demo_totals",
        "fn": "band_totals",
        "description": "Total counts per band of a reduced product CSV, as a table.",
        "args_schema": {
            "type": "object",
            "properties": {
                "csv_path": {"kind": "path", "type": "string", "description": "Path of the product CSV."},
                "out_dir": {"kind": "output", "shape": "folder", "type": "string",
                            "description": "Folder the table is written to."},
            },
            "required": ["csv_path", "out_dir"],
        },
    },
'''

_TOTALS_TEST = '''\
"""Checks for the band totals, on a marked synthetic product."""

import os

import pytest

from demo_utils.lib.demo_totals import band_totals
from demo_utils.tests.test_demo_summary import write_csv


def test_band_totals_sum_each_band(tmp_path):
    (tmp_path / "out").mkdir()
    out = band_totals(write_csv(tmp_path / "product.csv"), str(tmp_path / "out"))
    assert out["stdout"] == "low=1200.0 high=1900.0"
    with open(out["products"][0]["path"], encoding="utf-8") as handle:
        assert handle.read().splitlines()[1:] == ["low,1200.0", "high,1900.0"]


def test_the_tests_run_in_the_environment_the_package_names():
    assert os.environ.get("DEMO_STACK_READY") == "1"
%(extra)s'''

#: A test of the ``data`` class (W3): it needs a named data set the gate's configuration does not
#: provide here.
_DATA_TEST = '''

@pytest.mark.data("demo_rows")
def test_band_totals_on_the_named_data_set(tmp_path):
    assert band_totals is not None
'''


def _add_band_totals(root: Path, *, extra_test: str = "") -> None:
    """The small change the gate judges: one new tool in ``demo_utils``, its manifest entry and its
    tests, left uncommitted as an author leaves it."""
    lib = root / fixture.PKG_NAME / "lib" / "demo_totals.py"
    lib.write_text(_TOTALS_LIB, encoding="utf-8")
    manifest = root / fixture.PKG_NAME / "skills" / "__init__.py"
    text = manifest.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    manifest.write_text(text[:-2] + _TOTALS_ENTRY % fixture.TOOLKIT_NAME + "}\n", encoding="utf-8")
    (root / fixture.PKG_NAME / "tests" / "test_demo_totals.py").write_text(
        _TOTALS_TEST % {"extra": extra_test}, encoding="utf-8")


def _gate(root: Path):
    from auto_research.sandbox.gate import run_gate
    from auto_research.schemas import ToolSpec, ValidationDataset

    spec = ToolSpec(
        science_context="Total counts per band are the denominator of every band ratio.",
        expected_behavior="Sums each band over the rows of a product CSV and writes them as a table.",
        proposed_tool_name="band_totals", target_domain=fixture.PKG_NAME, disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="synth260817-4d1f7a", synthetic=True)],
    )
    return asyncio.run(run_gate(root, spec))


def test_the_gate_passes_a_small_change_with_its_tests_in_the_named_environment(make_toolkit):
    _need_jail()
    root = make_toolkit()
    _add_band_totals(root)
    report = _gate(root)
    assert report.passed, report.reason
    checks = {c.name: c for c in (*report.layer_a, *report.layer_b)}
    for name in ("structural.changes_allowed", "structural.env_resolves", "structural.argument_kinds",
                 "layer_b.pytest", "layer_b.regression", "determinism"):
        assert checks[name].passed, (name, checks[name].detail)
    returns = [c for c in report.layer_b if c.name.startswith("layer_b.returns")]
    assert returns and all(c.passed for c in returns), returns


def test_the_gate_fails_a_change_that_breaks_an_earlier_fast_test(make_toolkit):
    """The earlier fast tests of every package naming the environment run with the change: one
    that the change breaks in ``other_utils`` fails ``layer_b.regression``, naming the test."""
    _need_jail()
    root = make_toolkit()
    _add_band_totals(root)
    lib = root / fixture.OTHER_PKG / "lib" / "other_help.py"
    lib.write_text(lib.read_text(encoding="utf-8").replace("rows.extend(reader)", "rows.extend(list(reader)[:0])"),
                   encoding="utf-8")
    report = _gate(root)
    regression = next(c for c in report.layer_b if c.name == "layer_b.regression")
    assert not report.passed and not regression.passed
    assert "test_merge_keeps_every_row_in_order" in regression.detail, regression.detail


def test_the_gate_report_lists_every_job_with_its_status(make_toolkit):
    _need_jail()
    root = make_toolkit()
    _add_band_totals(root)
    report = _gate(root)
    jobs = report.jobs
    assert jobs and report.passed
    assert all(job.status in ("pass", "skip") for job in jobs), jobs
    assert all(job.reason for job in jobs if job.status == "skip"), jobs
    from auto_research.sandbox.gate import JOBS

    assert [job.name for job in jobs] == list(JOBS)
    assert {job.name: job.status for job in jobs}["regression"] == "pass"  # other_utils' earlier fast test


def test_a_data_test_whose_data_the_gate_lacks_fails_the_commit(make_toolkit):
    _need_jail()
    root = make_toolkit()
    _add_band_totals(root, extra_test=_DATA_TEST)
    report = _gate(root)
    assert not report.passed and "demo_rows" in report.reason, report.reason


def test_the_ci_pipeline_messages_a_failing_change_draws_state_positive_actions(make_toolkit):
    """W6: what the coder and the lead read from a failing pipeline (each failed check, the reason
    and the coder's feedback message) names the CI pipeline, states positive actions and cites no
    build-time governance. Two failing fixtures: a change that breaks an earlier fast test and names
    a data set the host lacks; and a change with a file outside every package."""
    from auto_research.sandbox.merge_flow import _gate_feedback_message
    from tests.test_prompts import model_text_problems

    _need_jail()
    root = make_toolkit()
    _add_band_totals(root, extra_test=_DATA_TEST)
    lib = root / fixture.OTHER_PKG / "lib" / "other_help.py"
    lib.write_text(lib.read_text(encoding="utf-8").replace("rows.extend(reader)", "rows.extend(list(reader)[:0])"),
                   encoding="utf-8")
    reports = [_gate(root)]
    (root / "notes.txt").write_text("a file outside every package\n", encoding="utf-8")
    reports.append(_gate(root))
    for report in reports:
        assert not report.passed
        texts = [c.detail for c in (*report.layer_a, *report.layer_b) if not c.passed]
        texts += [report.reason, _gate_feedback_message(report)]
        problems = [found for text in texts if (found := model_text_problems(text))]
        assert problems == [], problems


def _rename_key(root: Path, new_key: str) -> None:
    """Rename the summary tool's manifest key in ``demo_utils`` (left uncommitted)."""
    manifest = root / fixture.PKG_NAME / "skills" / "__init__.py"
    old = f'"{fixture.TOOLKIT_NAME}.{fixture.PKG_NAME}.{fixture.TOOL_NAME}"'
    text = manifest.read_text(encoding="utf-8")
    assert text.count(old) == 1
    manifest.write_text(text.replace(old, f'"{new_key}"'), encoding="utf-8")


@pytest.mark.parametrize("key", [
    f"{fixture.TOOLKIT_NAME}.{fixture.OTHER_PKG}.{fixture.TOOL_NAME}",  # the right name, another package
    f"{fixture.TOOLKIT_NAME}.{fixture.PKG_NAME}.extra.{fixture.TOOL_NAME}",  # four segments
])
def test_the_registry_serves_no_key_outside_name_package_tool_and_the_launch_refuses_it(make_toolkit, key):
    from auto_research.sandbox.registry import ToolkitRegistry, check_toolkit_manifests

    root = make_toolkit()
    _rename_key(root, key)
    registry = ToolkitRegistry(root)
    registry.load()
    assert key not in registry.list_tools() and [why.split(" ")[0] for why in registry.key_errors] == [key]
    with pytest.raises(RuntimeError, match=re.escape(key)):
        check_toolkit_manifests(root)


def test_the_gate_fails_a_change_whose_manifest_key_is_outside_the_package(make_toolkit):
    """M1: a coder's wrong key fails the gate's structural check, not the next launch."""
    _need_jail()
    root = make_toolkit()
    _add_band_totals(root)
    manifest = root / fixture.PKG_NAME / "skills" / "__init__.py"
    good = f'"{fixture.TOOLKIT_NAME}.demo_utils.band_totals"'
    manifest.write_text(manifest.read_text(encoding="utf-8").replace(
        good, f'"{fixture.TOOLKIT_NAME}.{fixture.OTHER_PKG}.band_totals"'), encoding="utf-8")
    report = _gate(root)
    keys = next(c for c in report.layer_a if c.name == "structural.manifest_keys")
    assert not report.passed and not keys.passed
    assert f"{fixture.TOOLKIT_NAME}.{fixture.OTHER_PKG}.band_totals" in keys.detail, keys.detail


def test_a_declared_package_glob_decides_the_packages_for_the_registry_and_the_gate(make_toolkit):
    """L5: with ``package_glob: demo_*`` only ``demo_utils`` is a package: the registry serves its tools
    alone, and the gate refuses a change in ``other_utils`` as outside every package."""
    from auto_research.sandbox.registry import ToolkitRegistry

    _need_jail()
    root = make_toolkit(contract={"package_glob": "demo_*"})
    registry = ToolkitRegistry(root)
    registry.load()
    assert {name.split(".")[1] for name in registry.list_tools()} == {fixture.PKG_NAME}
    _add_band_totals(root)
    stray = root / fixture.OTHER_PKG / "lib" / "stray.py"
    stray.write_text("X = 1\n", encoding="utf-8")
    report = _gate(root)
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and "other_utils/lib/stray.py" in allowed.detail and "demo_*" in allowed.detail


@pytest.mark.parametrize("glob", ["*", "**", "*/x_utils", "e*", "env?"])
def test_a_package_glob_reaching_beyond_package_folders_is_refused(tmp_path, glob):
    """L3: no bare ``*``, ``**`` or ``/``, and no glob matching the environment definitions folder."""
    from pydantic import ValidationError

    from auto_research.toolkit_contract import ToolkitContract

    with pytest.raises(ValidationError, match="package_glob"):
        ToolkitContract(name="demo_toolkit", root=tmp_path, package_glob=glob, template_package="envs_utils",
                        authoring_branch="ai", default_env="demo")


def test_package_and_toolkit_names_are_ascii_identifiers(tmp_path):
    """L4: a non-ASCII identifier is no package and no toolkit name (``str.isidentifier`` accepts it)."""
    from pydantic import ValidationError

    from auto_research.toolkit_contract import ToolkitContract

    contract = ToolkitContract(name="demo_toolkit", root=tmp_path, template_package="demo_utils",
                               authoring_branch="ai", default_env="demo")
    assert "dému_utils".isidentifier() and not contract.is_package("dému_utils")
    assert contract.key_problem("demo_toolkit.demo_utils.fit", "demo_utils") is None
    assert contract.key_problem("demo_toolkit.demo_utils.fït", "demo_utils") is not None
    with pytest.raises(ValidationError, match="ASCII"):
        ToolkitContract(name="démo", root=tmp_path, template_package="demo_utils", authoring_branch="ai",
                        default_env="demo")


# ── a worker's turn ──────────────────────────────────────────────────────────

_PRODUCT_CSV = (
    "# produced_by=contract_test\nobsid,low_counts,high_counts,pa_deg,pa_err_deg\n"
    "synth260817-4d1f7a,400.0,900.0,20.0,2.0\nsynth260817-9c3b02,500.0,700.0,26.0,3.0\n"
)


class _ScriptedModel:
    """The worker's model (outside the code under test): it issues ``steps`` one per turn, each
    ``step(tools, transcript, product) -> [(tool name suffix, arguments)]`` choosing among the tool
    names it was offered, then writes a report (citing its first result when ``cite``, placing the figure it
    made) and ends on ``turn_end``."""

    def __init__(self, steps, *, cite: bool = True):
        from types import SimpleNamespace

        self.config = SimpleNamespace(model="scripted")
        self._steps, self._cite, self._product, self._turn = list(steps), cite, "", 0

    async def call(self, messages, *, tools=None, **_kwargs):
        from auto_research.models import ModelResponse, ToolCall

        transcript = str(messages)
        self._turn += 1
        if self._steps:
            names = [t["name"] for t in tools or ()]
            calls = [ToolCall(id=f"c{self._turn}_{i}", name=next(n for n in names if n.endswith(suffix)),
                              arguments=arguments)
                     for i, (suffix, arguments) in enumerate(self._steps.pop(0)(names, transcript, self._product))]
            return ModelResponse(content="", stop="tool_use", tool_calls=calls)
        first = re.search(r"\[receipt ([\w.-]+:r\d+)", transcript).group(1)
        cited = f" {{{{{first}}}}}" if self._cite else ""
        if "write_report" not in transcript:
            return ModelResponse(content="", stop="tool_use", tool_calls=[ToolCall(
                id=f"c{self._turn}", name="write_report", arguments={"content": (
                    "# summary\nThe ratios are tabulated.\n"
                    f"# status\nsucceeded\n# findings\n- The per-row band ratios are tabulated{cited}.\n"
                    + ("# figures\n- [self:1]\n" if "[self:1]" in transcript else ""))})])
        return ModelResponse(content="", stop="tool_use", tool_calls=[
            ToolCall(id=f"c{self._turn}", name="turn_end", arguments={"summary": "reported"})])


def _handles(transcript: str, letter: str) -> list[str]:
    return list(dict.fromkeys(re.findall(rf"\banalyst_worker_0:{letter}\d+", transcript)))


def _worker_turn(root: Path, steps, run_id: str, tmp_path: Path, *, cite: bool = True):
    """One worker turn the way a run gives it: the launch reconciles the toolkit's environments
    (``_reconcile_environments``, recorded on the ledger), ``attach_worker_runtime`` gives the worker
    production's sandbox, registry and receipt store, and ``analyze`` runs its loop with the
    scripted model. Returns ``(report, store, source, ledger events)``."""
    import json

    from auto_research.agents.roles import AnalystWorkerAgent
    from auto_research.graph.runtime import (
        SandboxSettings, _reconcile_environments, attach_worker_runtime, stamp_run_environments,
    )
    from auto_research.monitor.events import RunMonitor
    from auto_research.sandbox.registry import ToolkitRegistry
    from auto_research.schemas import AnalysisTask

    ledger = tmp_path / "monitor.jsonl"
    asyncio.run(_reconcile_environments(root, RunMonitor(chatter=10, jsonl_path=ledger)))
    # The run's own stamping: the store's default fingerprint and the analysis environment's lock.
    stamp_run_environments(run_id, analysis=SandboxSettings.from_config().environment)
    events = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    worker = AnalystWorkerAgent(model=_ScriptedModel(steps, cite=cite), prompt="p")
    [sandbox] = attach_worker_runtime([worker], run_id=run_id, registry=ToolkitRegistry(root), toolkit_root=root)
    product = worker.report_dir / "product.csv"
    product.write_text(_PRODUCT_CSV, encoding="utf-8")
    worker.model._product = str(product)

    async def go():
        try:
            return await worker.analyze(AnalysisTask(question="Tabulate and plot the band ratios."))
        finally:
            await sandbox.shutdown()

    return asyncio.run(go()), worker.receipts, worker.agent_key, events


#: The worker's tool calls, one turn each: every argument kind, typed parameters with their reason,
#: settings, products, a figure, scratch, and one harness read.
_STEPS = [
    lambda names, text, csv: [("_write_band_table", {
        "csv_path": csv,
        "min_total_counts": {"value": 1000.0, "reason": "rows below 1000 counts are noise-dominated"}})],
    lambda names, text, csv: [("_plot_band_ratio", {
        "ratios": {"$ref": f"{_handles(text, 'r')[0]}#/ratios"}, "title": "Band ratio per row"})],
    lambda names, text, csv: [("_merge_tables", {"tables": [{"$ref": _handles(text, "d")[0]}]})],
    lambda names, text, csv: [("_count_rows", {
        "csv_path": csv, "min_counts": {"value": 1250, "reason": "the brighter row only"}, "label": "kept"})],
    lambda names, text, csv: [("read_file", {"path": csv})],
]


def test_a_worker_turn_on_the_fixture_mints_its_toolkit_results_as_evidence(make_toolkit, tmp_path):
    from auto_research.provenance.refs import call_arguments
    from auto_research.provenance.store import is_evidence

    _need_jail()
    root = make_toolkit()
    report, store, source, events = _worker_turn(root, _STEPS, tmp_path.name, tmp_path)
    assert [(e["event"], e["data"].get("env")) for e in events if e["event"].startswith("env.build")
            and e["event"] != "env.build.start"] == [("env.build", fixture.ENV_ID)], events
    assert report.status == "succeeded", report
    receipts = store.receipts(source=source)
    toolkit = [r for r in receipts if r.produced_by.startswith(f"{fixture.TOOLKIT_NAME}.")]
    harness = [r for r in receipts if r not in toolkit]
    assert sorted(r.kind for r in toolkit) == ["data", "data", "figure", "result", "result", "result", "result"]
    assert all(is_evidence(r) for r in toolkit), [(r.handle, r.produced_by) for r in toolkit]
    head = fixture.git(root, "rev-parse", "HEAD").strip()
    assert {r.toolkit_commit for r in toolkit} == {head}
    assert [r.produced_by for r in harness] == ["read_file"] and not is_evidence(harness[0])
    assert all(r.env for r in receipts), [(r.handle, r.env) for r in receipts]  # every receipt names its environment
    table, plot, merge, count = [r for r in toolkit if r.kind == "result"]
    assert table.handle in plot.parents and any(p.startswith(f"{source}:d") for p in merge.parents)
    assert call_arguments(store, table.handle)["min_total_counts"] == {
        "value": 1000.0, "reason": "rows below 1000 counts are noise-dominated"}
    assert store.text(count.handle).endswith("kept: 1") and not count.parents


def test_a_tool_of_a_toolkit_named_otherwise_is_minted_as_evidence(make_toolkit, tmp_path):
    from auto_research.provenance.store import is_evidence

    _need_jail()
    root = make_toolkit(toolkit="demo_toolkit", contract={"name": "demo_toolkit"})
    report, store, source, _ = _worker_turn(root, _STEPS[:1], tmp_path.name, tmp_path)
    assert report.status == "succeeded", report
    receipts = store.receipts(source=source)
    assert receipts and all(is_evidence(r) for r in receipts), [(r.handle, r.produced_by) for r in receipts]
