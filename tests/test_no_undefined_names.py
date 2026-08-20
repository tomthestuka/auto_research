"""Static guard: no source line reads a name that is not in scope.

**Why this exists.** On 2026-08-06 a one-word slip — `tools=tools` where the local was
`self.tools` — passed the whole 670-test suite and killed a live run 80 minutes and $5.36 in,
at the *lead's* `decide_authoring`, because no deterministic test exercises that particular
compose call site. A `NameError` is invisible until its branch runs, and the branches that run
only deep inside a live run are exactly the expensive ones to discover this way.

A linter would catch it (pyflakes F821), but none is installed in the env, and a test cannot
depend on one that is not. This walks the AST with proper scope rules — module, function,
class, closure, comprehension and lambda — so a nested function reading an enclosing local is
fine, which is what makes a naive version useless here (183 false positives on this package).

Annotations are skipped: `from __future__ import annotations` is on package-wide, so an
annotation is a string that never evaluates (`react.py` legitimately annotates with a
`Sequence` it does not import).
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "src" / "auto_research"
# The tests are scanned too. A live test's slip is the same defect wearing a cheaper hat: on
# 2026-08-19 a missing import in the coder matrix wasted a live invocation, and a test that only
# runs behind an env flag has even fewer chances to be caught than a rare source branch.
SCANNED = sorted(PACKAGE.rglob("*.py")) + sorted((REPO / "tests").glob("*.py")) + sorted(
    (REPO / "benchmarks").rglob("*.py")
)
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__"}


def _strip_annotations(node: ast.AST) -> None:
    """Blank every annotation in-place — they are strings under PEP 563 and never evaluate."""
    for n in ast.walk(node):
        if isinstance(n, (ast.arg, ast.AnnAssign)):
            n.annotation = None
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.returns = None


def _bound_directly_in(scope_node: ast.AST) -> set[str]:
    """Names this scope binds, without descending into nested scopes."""
    names: set[str] = set()
    if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope_node.args
        names |= {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
        if args.vararg:
            names.add(args.vararg.arg)
        if args.kwarg:
            names.add(args.kwarg.arg)
    stack = list(ast.iter_child_nodes(scope_node))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)          # the name binds here; the body is its own scope
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _undefined_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    _strip_annotations(tree)
    found: list[str] = []

    def visit(scope_node: ast.AST, enclosing: set[str], where: str) -> None:
        scope = enclosing | _bound_directly_in(scope_node)
        # Comprehension and lambda targets are bound in their own inner scope; treating them as
        # visible here is deliberate over-permission — this guard hunts typos, not shadowing.
        for node in ast.walk(scope_node):
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for gen in node.generators:
                    scope |= {
                        t.id for t in ast.walk(gen.target) if isinstance(t, ast.Name)
                    }
            elif isinstance(node, ast.Lambda):
                scope |= _bound_directly_in(node)

        stack = list(ast.iter_child_nodes(scope_node))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(node, scope, node.name)
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                if node.id not in scope and not hasattr(builtins, node.id):
                    found.append(
                        f"{path.name}:{node.lineno} reads undefined '{node.id}' in {where}()"
                    )
            stack.extend(ast.iter_child_nodes(node))

    visit(tree, set(_MODULE_DUNDERS), "<module>")
    return found


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: str(p.relative_to(REPO)))
def test_module_has_no_undefined_names(path: Path) -> None:
    problems = _undefined_names(path)
    assert not problems, "\n".join(problems)


def test_the_guard_catches_the_bug_that_motivated_it(tmp_path: Path) -> None:
    """The exact 2026-08-06 slip: a method passing `tools` where the local is `self.tools`."""
    module = tmp_path / "slip.py"
    module.write_text(
        "class Lead:\n"
        "    def __init__(self):\n"
        "        self.tools = None\n"
        "    async def decide(self):\n"
        "        return await self.compose(tools=tools)\n",
        encoding="utf-8",
    )
    assert any("undefined 'tools'" in p for p in _undefined_names(module))
