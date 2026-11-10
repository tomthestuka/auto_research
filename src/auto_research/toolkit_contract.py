"""The toolkit contract: what the orchestration knows about a toolkit, read from one place.

Every fact the orchestration relies on about a toolkit (its declared name, where it lives, which
top-level folders are its packages, the package an author mirrors, the branch the runtime commits
onto, how its fast tests are selected) is a field of :class:`ToolkitContract`. The contracts come
from the ``toolkits:`` block of ``orchestration.yaml``, a list holding one toolkit until Phase 3
step 2 gives units their own (CLAUDE.md §9.5). Code reads these facts through this module only.

The declared name is the trust boundary's input: a receipt is a toolkit result, and so evidence,
when its producer starts ``<name>.`` of a declared toolkit (``is_toolkit_producer``; the registry
serves only manifest keys ``<name>.<package>.<tool>``).
A name that equals or prefixes a producer the harness mints would widen evidence to what a role
only read (CLAUDE.md §2b), so such a name is refused when the contract is built.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from auto_research.config_paths import orchestration_yaml


#: The toolkit's top-level folder of environment definitions (``envs/<name>/``): never a package.
RESERVED_FOLDER = "envs"


#: The variable naming a data set's root in a test run: ``TOOLKIT_DATA_<NAME>`` for ``data("<name>")``.
DATA_VARIABLE_PREFIX = "TOOLKIT_DATA_"

_DATA_NAME = re.compile(r"[a-z][a-z0-9_]*")


class ToolkitTestClasses(BaseModel):
    """The toolkit's test classes, by the markers its root ``conftest.py`` declares: unmarked (fast),
    ``slow``, ``live`` and ``data("<name>")``. The gate runs a change's own tests of every class but
    ``live``, and the earlier ``fast`` ones; a ``data`` test it skips fails the change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fast: str = Field("not slow and not live", min_length=1,
                      description="The tests the gate reruns on every change, for regression (a pytest -m "
                                  "expression).")
    live: str = Field("live", min_length=1,
                      description="The tests that reach the network (a pytest -m expression); the gate's test "
                                  "run has none, so it deselects them.")
    data: str = Field("data", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
                      description="The marker of a test that reads a named data set, data(\"<name>\"): its root "
                                  "is the folder TOOLKIT_DATA_<NAME> names, absent → skipped with the reason.")


class ToolkitContract(BaseModel):
    """One toolkit, as the orchestration pairs with it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="The toolkit's declared name: the first segment of every manifest key "
                                  "and of every producer its tools mint.")
    root: Path = Field(description="The toolkit checkout.")
    package_glob: str = Field("*_utils", min_length=1,
                              description="Which top-level folders of the root are tool packages.")
    template_package: str = Field(description="The package an author is shown as the layout to mirror.")
    authoring_branch: str = Field(min_length=1,
                                  description="The branch the runtime commits authored tools onto.")
    default_env: str = Field(description="The environment (a folder under the toolkit's envs/) a unit's own "
                                         "REPL and shell run in, and where a package naming none runs and "
                                         "is tested.")
    test_classes: ToolkitTestClasses = Field(default_factory=ToolkitTestClasses)

    @field_validator("root")
    @classmethod
    def _expand(cls, root: Path) -> Path:
        return root.expanduser()

    @field_validator("default_env")
    @classmethod
    def _env_is_a_definition_name(cls, env: str) -> str:
        from auto_research.sandbox.env_build import _NAME

        if not _NAME.fullmatch(env):
            raise ValueError(f"default_env {env!r} is not an environment name (a folder under envs/)")
        return env

    @field_validator("name")
    @classmethod
    def _name_grounds_only_toolkit_tools(cls, name: str) -> str:
        if not (name.isascii() and name.isidentifier()):
            raise ValueError(f"toolkit name {name!r} is not an ASCII Python identifier")
        reserved = harness_names()
        if name in reserved:
            raise ValueError(f"toolkit name {name!r} is a producer the harness mints (one of "
                             f"{', '.join(sorted(reserved))}); name the toolkit otherwise")
        return name

    @model_validator(mode="after")
    def _template_is_a_package(self) -> "ToolkitContract":
        glob = self.package_glob
        if "/" in glob or "**" in glob or glob == "*" or fnmatch.fnmatchcase(RESERVED_FOLDER, glob):
            raise ValueError(f"package_glob {glob!r} names top-level package folders only: no '/' or "
                             f"'**', not every folder, and not the environment definitions folder "
                             f"{RESERVED_FOLDER!r}")
        if not self.is_package(self.template_package):
            raise ValueError(f"template_package {self.template_package!r} does not match "
                             f"package_glob {self.package_glob!r}")
        return self

    def is_package(self, name: str) -> bool:
        """Whether a top-level folder name is one of this toolkit's packages."""
        return name.isascii() and name.isidentifier() and fnmatch.fnmatchcase(name, self.package_glob)

    def packages(self, root: Path | None = None) -> list[str]:
        """The package folders under ``root`` (default: the toolkit's): top-level folders matching
        ``package_glob``."""
        root = Path(root) if root is not None else self.root
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.glob(self.package_glob) if p.is_dir() and self.is_package(p.name))

    def owns(self, produced_by: str) -> bool:
        """Whether a producer is a tool of this toolkit: it starts ``<name>.`` (the registry serves
        only keys ``<name>.<package>.<tool>``, ``key_problem``)."""
        return produced_by.startswith(f"{self.name}.")

    def key_problem(self, key: str, package: str) -> str | None:
        """Why a manifest key ``package`` declares is not ``<name>.<package>.<tool>``; None if it is."""
        parts = key.split(".")
        if (len(parts) == 3 and parts[0] == self.name and parts[1] == package
                and parts[2].isascii() and parts[2].isidentifier()):
            return None
        return f"{key} (declared by {package}; a key there reads {self.name}.{package}.<tool>)"


def harness_names() -> frozenset[str]:
    """The first segment of every producer the harness mints (``provenance.store.HARNESS_PRODUCERS``):
    the names a toolkit may not take."""
    from auto_research.provenance.store import HARNESS_PRODUCERS

    return frozenset(p.split(".", 1)[0].split(":", 1)[0] for p in HARNESS_PRODUCERS)


def _parse(config: dict, where: str) -> list[ToolkitContract]:
    block = config.get("toolkits")
    if not isinstance(block, list) or len(block) != 1:
        raise ValueError(f"{where}: `toolkits:` is a list of exactly one toolkit "
                         f"(found {type(block).__name__ if not isinstance(block, list) else len(block)})")
    return [ToolkitContract.model_validate(entry) for entry in block]


#: The contract this process runs on: bound by the first successful load from the configured
#: ``orchestration.yaml`` (the launch preflight, in a run) and kept for the process's life, so the
#: evidence predicate and every reader see one contract. ``reset`` unbinds it.
_BOUND: list[ToolkitContract] | None = None


def toolkits(config: dict | None = None) -> list[ToolkitContract]:
    """The declared toolkits. Without ``config``: the process's bound contract, bound on first use
    from the configured ``orchestration.yaml``. With ``config``: that configuration's block, parsed
    and never bound (a reader of another configuration, e.g. a benchmark's trial). Raises when the
    block is absent or a field is invalid."""
    global _BOUND
    if config is not None:
        return _parse(config, "orchestration config")
    if _BOUND is None:
        path = orchestration_yaml()
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except OSError as exc:
            raise ValueError(f"{path}: cannot read the toolkit contract ({exc})") from exc
        _BOUND = _parse(data, str(path))
    return _BOUND


def toolkit(config: dict | None = None) -> ToolkitContract:
    """The one declared toolkit (``toolkits`` holds exactly one until Phase 3 step 2)."""
    return toolkits(config)[0]


def bind(config: dict) -> list[ToolkitContract]:
    """Bind the process to ``config``'s ``toolkits:`` block (validated) and return it: for a caller
    that holds the configuration as data, e.g. a test that stands a configuration in for the file."""
    global _BOUND
    _BOUND = _parse(config, "orchestration config")
    return _BOUND


def reset() -> None:
    """Unbind the process's contract; the next read binds the configured one again. For a process
    whose configuration changed (a test pointing ``AUTO_RESEARCH_CONFIG_DIR`` elsewhere)."""
    global _BOUND
    _BOUND = None


def is_toolkit_producer(produced_by: str) -> bool:
    """Whether a producer is a tool of a declared toolkit (``provenance.store.is_evidence``)."""
    return any(contract.owns(produced_by) for contract in toolkits())


def data_variable(name: str) -> str:
    """The variable a test run reads data set ``name``'s root from (``TOOLKIT_DATA_<NAME>``)."""
    return DATA_VARIABLE_PREFIX + name.upper()


def test_data(config: dict | None = None) -> dict[str, Path]:
    """The host's data sets for the toolkit's ``data`` tests: ``orchestration.yaml`` ``test_data``,
    ``{name: folder}`` (the toolkit names a need, the host satisfies it by name). Raises on a name
    that is not lowercase ``[a-z][a-z0-9_]*`` or a folder that is not an absolute path."""
    if config is None:
        with orchestration_yaml().open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    block = config.get("test_data") or {}
    if not isinstance(block, dict):
        raise ValueError(f"test_data is a mapping of data set name to folder (found {type(block).__name__})")
    out: dict[str, Path] = {}
    for name, folder in block.items():
        if not (isinstance(name, str) and _DATA_NAME.fullmatch(name)):
            raise ValueError(f"test_data: {name!r} is not a data set name (lowercase letters, digits and '_')")
        if not (isinstance(folder, str) and Path(folder).expanduser().is_absolute()):
            raise ValueError(f"test_data: {name}: {folder!r} is not an absolute path")
        out[name] = Path(folder).expanduser()
    return out


def data_needs(contract: "ToolkitContract | None" = None) -> tuple[dict[str, list[str]], list[str]]:
    """``({data set name: [files naming it]}, problems)``: every ``data("<name>")`` mark in the
    toolkit's package sources, read statically (no test is collected or imported). A mark is
    ``pytest.mark.<data>(...)``, ``mark.<data>(...)`` with ``mark`` imported from pytest (under any
    name), or a name bound to either (``needs = pytest.mark.<data>``); ``<data>`` is the contract's
    ``test_classes.data``. A mark whose name is not one positional string literal is a problem,
    ``file:line``. A source that does not parse is skipped: the gate judges it."""
    import ast

    contract = contract or toolkit()
    marker = contract.test_classes.data
    needs: dict[str, list[str]] = {}
    problems: list[str] = []
    for package in contract.packages():
        for path in sorted((contract.root / package).rglob("*.py")):
            try:
                tree = ast.parse(path.read_bytes())
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                continue
            rel = str(path.relative_to(contract.root))
            # Names bound to pytest's `mark`, then names bound to the data mark itself.
            marks = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                     and n.module == "pytest" for a in n.names if a.name == "mark"}

            def is_mark(node) -> bool:
                return ((isinstance(node, ast.Attribute) and node.attr == "mark")
                        or (isinstance(node, ast.Name) and node.id in marks))

            def is_data(node) -> bool:
                return isinstance(node, ast.Attribute) and node.attr == marker and is_mark(node.value)

            aliases = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and is_data(n.value)
                       for t in n.targets if isinstance(t, ast.Name)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and (is_data(node.func) or (
                        isinstance(node.func, ast.Name) and node.func.id in aliases))):
                    continue
                first = node.args[0] if len(node.args) == 1 and not node.keywords else None
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    problems.append(f"{rel}:{node.lineno}: {marker}(...) takes the data set's name as one "
                                    f"string literal, {marker}(\"<name>\")")
                    continue
                if rel not in needs.setdefault(first.value, []):
                    needs[first.value].append(rel)
    return needs, problems


def check_toolkit_test_data() -> None:
    """Launch-time preflight (no LLM, no test collected): every data set the toolkit's tests name
    (``data_needs``) is one ``test_data`` declares, and every declared folder exists, so the gate's
    test runs find each one."""
    declared = test_data()
    problems = []
    for contract in toolkits():
        needs, unreadable = data_needs(contract)
        problems += unreadable
        problems += [f"{', '.join(files)} name{'s' if len(files) == 1 else ''} data set {name!r}, which "
                     f"orchestration.yaml test_data does not declare: add `{name}: <folder>` to test_data"
                     for name, files in sorted(needs.items()) if name not in declared]
    problems += [f"test_data.{name}: {folder} is not a folder" for name, folder in declared.items()
                 if not folder.is_dir()]
    if problems:
        raise RuntimeError("; ".join(problems))


def check_toolkit_contract() -> None:
    """Launch-time preflight (no LLM): the ``toolkits:`` block validates, each root is a directory and
    holds its template package, its default environment's definition and a ``README.md`` at its root and
    in every package (the domain briefing the roles read), and ``test_data``'s names and
    folders are well formed (that each folder exists, and that every data set the tests name is
    declared, is ``check_toolkit_test_data``'s, preflight ``toolkit_test_data``).
    Registered in ``auto_research.preflight``; the manifest keys are checked with the packages' import
    (``sandbox.registry.check_toolkit_manifests``)."""
    try:
        test_data()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeError(f"orchestration.yaml: {exc}") from exc
    for contract in toolkits():
        if not contract.root.is_dir():
            raise RuntimeError(f"toolkit {contract.name}: root {str(contract.root)!r} is not a directory; "
                               "set it to the toolkit checkout")
        if not (contract.root / contract.template_package / "__init__.py").is_file():
            raise RuntimeError(f"toolkit {contract.name}: template_package {contract.template_package!r} "
                               f"is not a package of {contract.root} (a folder with an __init__.py)")
        definition = contract.root / RESERVED_FOLDER / contract.default_env / "env.yaml"
        if not definition.is_file():
            raise RuntimeError(f"toolkit {contract.name}: default_env {contract.default_env!r} has no "
                               f"definition ({definition} is missing)")
        # The roles read the toolkit's domain from its README files (the prompts point to them, W6).
        missing = [str(folder / "README.md") for folder in
                   (contract.root, *(contract.root / p for p in contract.packages()))
                   if not (folder / "README.md").is_file()]
        if missing:
            raise RuntimeError(f"toolkit {contract.name}: the roles read each package's README.md as its "
                               f"briefing, and these are missing: {', '.join(missing)}")
