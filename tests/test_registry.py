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


def _write_tool_config(root, name, body: str):
    """Write ``<root>/<name>/tool_config.yaml`` with raw YAML ``body``."""
    (root / name / "tool_config.yaml").write_text(textwrap.dedent(body))


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


def test_manifest_description_wins_over_function_docstring(tmp_path, cleanup_modules):
    """The manifest entry IS the agent-facing interface (§9.2): ``skills/TOOL_MANIFEST`` is
    the ai-half and ``lib/`` docstrings are the human-half, so a ``lib/`` docstring never
    replaces the authored description. Until 2026-07-27 the registry overrode the manifest
    with ``fn.__doc__`` whenever the module imported, which silently discarded the coder's
    (and the co-lead's reviewed) description for any import-clean tool.
    """
    _write_pkg(tmp_path, "docs_utils", {"docs_utils.t1": "the authored brief"})
    # Give the backing lib function a docstring — the human-half documentation.
    (tmp_path / "docs_utils" / "lib" / "help.py").write_text(
        'def do(**kw):\n    """Long human-half docstring for a person reading lib/."""\n'
        "    return kw\n"
    )
    reg = ToolkitRegistry(tmp_path)
    reg.load()
    assert reg.spec("docs_utils.t1")["description"] == "the authored brief"
    # ...and it reaches the model-facing definitions too (names are sanitized there).
    descriptions = [d["description"] for d in reg.tool_definitions()]
    assert "the authored brief" in descriptions


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


def test_call_timeout_ceiling_defaults_to_global(tmp_path, cleanup_modules):
    from auto_research.env import sandbox_call_timeout_default

    default = sandbox_call_timeout_default()  # from config/orchestration.yaml (ships 300)
    assert default == 300
    _write_pkg(tmp_path, "alpha_utils", {"alpha_utils.t1": "first"})  # no tool_config.yaml
    reg = ToolkitRegistry(tmp_path)
    reg.load()
    assert reg.call_timeout_ceiling("alpha_utils.t1") == default
    assert reg.call_timeout_ceiling("mcp.builtin.bash_exec") == default  # unknown/MCP → default
    assert reg.call_timeout_ceiling("nope") == default


def test_tool_config_sets_per_tool_ceiling(tmp_path, cleanup_modules):
    _write_pkg(tmp_path, "zeta_utils", {"zeta_utils.acquire": "dl", "zeta_utils.peek": "p"})
    _write_tool_config(
        tmp_path,
        "zeta_utils",
        """
        call_timeout_s:
          acquire: 1800
        default_call_timeout_s: 120
        """,
    )
    reg = ToolkitRegistry(tmp_path)
    reg.load()
    assert reg.call_timeout_ceiling("zeta_utils.acquire") == 1800   # explicit
    assert reg.call_timeout_ceiling("zeta_utils.peek") == 120        # package default
    # large values are honored as-is (no hard clamp — trusted infra)
    _write_tool_config(tmp_path, "zeta_utils", "call_timeout_s:\n  acquire: 21600\n")
    reg.reload()
    assert reg.call_timeout_ceiling("zeta_utils.acquire") == 21600   # honored, not clamped


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
