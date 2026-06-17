"""Deterministic tests for ToolkitRegistry dynamic discovery + reload (step 7).

Builds throwaway ``*_utils`` packages on disk under a temp toolkit_root, so no science
stack or research_toolkit checkout is needed.
"""

import sys
import textwrap

import pytest

from auto_research.sandbox.registry import ToolkitRegistry, _discover_manifest_packages


def _write_pkg(root, name, tools: dict[str, str]):
    """Create ``<root>/<name>/`` with a skills TOOL_MANIFEST exposing ``tools`` (tool→desc)."""
    pkg = root / name
    (pkg / "lib").mkdir(parents=True, exist_ok=True)
    (pkg / "skills").mkdir(parents=True, exist_ok=True)
    (pkg / "lib" / "__init__.py").write_text("")
    (pkg / "lib" / "help.py").write_text("def do(**kw):\n    return kw\n")
    entries = ",\n".join(
        f'    "{tk}": {{"module": "{name}.lib.help", "fn": "do", '
        f'"description": "{desc}", "args_schema": {{"type": "object", "properties": {{}}}}}}'
        for tk, desc in tools.items()
    )
    (pkg / "skills" / "__init__.py").write_text(
        "TOOL_MANIFEST = {\n" + entries + "\n}\n"
    )
    (pkg / "__init__.py").write_text(
        textwrap.dedent(f"from {name}.skills import TOOL_MANIFEST\n")
    )
    return pkg


@pytest.fixture
def cleanup_modules():
    before = set(sys.modules)
    yield
    for mod in set(sys.modules) - before:
        if mod.split(".")[0].endswith("_utils"):
            sys.modules.pop(mod, None)


def test_discovers_star_utils_packages(tmp_path):
    _write_pkg(tmp_path, "alpha_utils", {"alpha_utils.t1": "first"})
    (tmp_path / "not_a_pkg_utils").mkdir()  # no __init__.py → ignored
    (tmp_path / "plain_dir").mkdir()
    assert _discover_manifest_packages(tmp_path) == ["alpha_utils"]


def test_load_indexes_manifest_tools(tmp_path, cleanup_modules):
    _write_pkg(tmp_path, "alpha_utils", {"alpha_utils.t1": "first tool"})
    reg = ToolkitRegistry(tmp_path)
    reg.load()
    assert "alpha_utils.t1" in reg.list_tools()


def test_reload_picks_up_modified_manifest(tmp_path, cleanup_modules):
    _write_pkg(tmp_path, "beta_utils", {"beta_utils.t1": "first"})
    reg = ToolkitRegistry(tmp_path)
    reg.load()
    assert set(reg.list_tools()) == {"beta_utils.t1"}

    # Extend the manifest (extend_existing) and reload.
    _write_pkg(tmp_path, "beta_utils", {"beta_utils.t1": "first", "beta_utils.t2": "second"})
    reg.reload()
    assert set(reg.list_tools()) == {"beta_utils.t1", "beta_utils.t2"}


def test_reload_discovers_new_package(tmp_path, cleanup_modules):
    _write_pkg(tmp_path, "gamma_utils", {"gamma_utils.t1": "g1"})
    reg = ToolkitRegistry(tmp_path)
    reg.load()
    assert set(reg.list_tools()) == {"gamma_utils.t1"}

    # A brand-new package (new_package disposition, e.g. heasarc_utils) appears on reload.
    _write_pkg(tmp_path, "delta_utils", {"delta_utils.t1": "d1"})
    reg.reload()
    assert set(reg.list_tools()) == {"gamma_utils.t1", "delta_utils.t1"}


def test_reload_preserves_mcp_and_disabled(tmp_path, cleanup_modules):
    _write_pkg(tmp_path, "eps_utils", {"eps_utils.t1": "keep", "eps_utils.t2": "drop"})
    reg = ToolkitRegistry(tmp_path, disabled_tools=["eps_utils.t2"])

    class FakeMCP:
        def list_kinds(self):
            return ["mcp.builtin.bash_exec"]

        def describe(self, kind):
            return "run bash"

        def schema(self, kind):
            return {"type": "object", "properties": {}}

    reg.load_mcp(FakeMCP())
    reg.load()
    assert reg.is_mcp("mcp.builtin.bash_exec")
    assert "eps_utils.t2" not in reg.list_tools()  # disabled filtered

    reg.reload()
    assert reg.is_mcp("mcp.builtin.bash_exec")  # MCP index preserved across reload
    assert "eps_utils.t2" not in reg.list_tools()  # disabled still filtered
    assert "eps_utils.t1" in reg.list_tools()
