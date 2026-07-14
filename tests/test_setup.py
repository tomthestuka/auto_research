"""Deterministic tests for the installer's pure functions (increment 2).

Covers the config rendering + agent_fs_allow derivation + answer resolution — the parts
decidable without conda, prompts, or a live run.
"""

from __future__ import annotations

import pytest

import subprocess
import sys

from auto_research.setup import (
    Answers,
    SetupError,
    derive_fs_allow,
    domain_fs_paths,
    domain_probe,
    init_toolkit_git,
    package_specs,
    render_analyst_env,
    render_observations,
    render_orchestration,
    render_repl_init,
    scaffold_toolkit_package,
    stage_raw_code,
)


def test_derive_fs_allow_from_paths():
    vals = {
        "toolkit_root": "/x/research_toolkit",
        "obs_db_root": "/x/obs",
        "analyze_env": "/x/envs/analyze",
        "runtime_dir": "/x/ar_runtime",
    }
    got = derive_fs_allow(vals)
    assert got == ["/x/research_toolkit", "/x/obs", "/x/envs/analyze", "/x/ar_runtime", "/tmp"]


def test_derive_fs_allow_skips_placeholders_and_dedups_and_adds_extra():
    vals = {"toolkit_root": "/path/to/research_toolkit", "obs_db_root": "/x/obs", "runtime_dir": "/x/obs"}
    got = derive_fs_allow(vals, extra_paths=["/opt/heasoft", "/x/obs"])
    # placeholder toolkit_root dropped; /x/obs deduped; extra path included; /tmp appended
    assert got == ["/x/obs", "/opt/heasoft", "/tmp"]
    assert not any("research_toolkit" in p for p in got)


def test_render_orchestration_substitutes_and_preserves_comments():
    template = (
        "runtime_dir: /path/to/ar_runtime   # a comment\n"
        "analyze_probe: \"\"                 # probe\n"
        "max_iterations: 3\n"
        "agent_fs_allow:\n"
        "  - /path/to/research_toolkit\n"
        "  - /tmp\n"
        "\n"
        "report_formats: [tex]\n"
    )
    out = render_orchestration(
        template,
        {"runtime_dir": "/real/rt", "analyze_probe": "import xspec, xspec_utils"},
        ["/real/tk", "/real/rt", "/tmp"],
    )
    assert "runtime_dir: /real/rt   # a comment" in out  # value swapped, comment kept
    assert 'analyze_probe: "import xspec, xspec_utils"                 # probe' in out  # quoted
    assert "max_iterations: 3" in out  # untouched key preserved
    assert "  - /real/tk\n  - /real/rt\n  - /tmp\n" in out  # fs_allow block regenerated
    assert "research_toolkit" not in out  # old fs_allow item gone
    assert "report_formats: [tex]" in out  # trailing content after the block preserved


def test_render_observations_substitutes_root():
    template = "# header\nobs_db_root: /path/to/obs_db_root\n"
    out = render_observations(template, "/real/obs")
    assert "obs_db_root: /real/obs" in out
    assert "# header" in out


def test_render_analyst_env_boot_commands():
    entries = [
        {"kind": "env_var", "var": "HEADAS", "value": "/opt/heasoft"},
        {"kind": "shell_init", "path": "$HEADAS/headas-init.sh"},
        {"kind": "license_tool", "name": "Gaussian", "binary_dir": "/opt/g16",
         "license_var": "LM_LICENSE_FILE", "license_value": "27000@lic.host"},
        {"kind": "conda_pkg", "spec": "numpy=2.3"},  # not a shell line
    ]
    out = render_analyst_env(entries)
    assert 'export HEADAS="/opt/heasoft"' in out
    assert 'source "$HEADAS/headas-init.sh"' in out  # $VAR expansion preserved (double-quoted)
    assert 'export PATH="/opt/g16:$PATH"' in out
    assert 'export LM_LICENSE_FILE="27000@lic.host"' in out
    assert "numpy" not in out  # package specs don't go in the shell hook


def test_render_repl_init_from_snippets():
    entries = [
        {"kind": "python_init", "code": "import xspec\nFit.query = 'no'"},
        {"kind": "env_var", "var": "X", "value": "1"},  # ignored here
    ]
    out = render_repl_init(entries)
    assert "import xspec" in out and "Fit.query = 'no'" in out
    assert out.startswith("#")  # header comment


def test_domain_fs_paths_collects_roots():
    entries = [
        {"kind": "env_var", "var": "CALDB", "value": "/man1/CALDB"},
        {"kind": "env_var", "var": "REL", "value": "not_a_path"},
        {"kind": "license_tool", "binary_dir": "/opt/g16", "license_value": "/opt/lic/g16.lic"},
        {"kind": "python_init", "code": "x=1", "fs_paths": ["/data/relxill"]},
    ]
    got = domain_fs_paths(entries)
    assert got == ["/man1/CALDB", "/opt/g16", "/opt/lic/g16.lic", "/data/relxill"]
    assert "not_a_path" not in got


def test_domain_probe_dedupes_and_joins():
    entries = [{"probe": "import xspec"}, {"probe": "import xspec; import numpy"}, {}]
    assert domain_probe(entries, existing="import sys") == "import sys; import xspec; import numpy"


def test_package_specs_split():
    entries = [
        {"kind": "conda_pkg", "spec": "astropy=7.1"},
        {"kind": "pip_pkg", "spec": "ixpeobssim==33.0.0"},
        {"kind": "env_var", "var": "X", "value": "1"},
    ]
    assert package_specs(entries) == (["astropy=7.1"], ["ixpeobssim==33.0.0"])


def test_answers_preset_and_default_and_missing():
    a = Answers({"runtime_dir": "/preset"}, interactive=False)
    assert a.path("runtime_dir", "?", "/default") == "/preset"  # preset wins
    assert a.path("papers_dir", "?", "/default") == "/default"  # falls back to default
    with pytest.raises(SetupError, match="missing answer"):
        a.path("obs_db_root", "?")  # no preset, no default, non-interactive → fail loud


def test_scaffold_toolkit_package_mirrors_layout_and_imports(tmp_path):
    scaffold_toolkit_package(tmp_path, "demo")
    pkg = tmp_path / "demo_utils"
    assert (pkg / "__init__.py").exists()
    assert (pkg / "lib" / "demo_help.py").exists()
    assert (pkg / "skills" / "__init__.py").exists()  # pure-data TOOL_MANIFEST
    assert (pkg / "skills" / "session.py").exists()
    assert (pkg / "tests" / "test_demo.py").exists()
    # The pure-data ai-half imports in a bare env (no science stack) and exposes a dict manifest.
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib

        mod = importlib.import_module("demo_utils")
        assert isinstance(mod.TOOL_MANIFEST, dict) and mod.TOOL_MANIFEST == {}
    finally:
        sys.path.remove(str(tmp_path))
        for m in [k for k in list(sys.modules) if k.startswith("demo_utils")]:
            del sys.modules[m]


def test_init_toolkit_git_creates_ai_branch(tmp_path):
    scaffold_toolkit_package(tmp_path, "demo")
    init_toolkit_git(tmp_path, branch="ai")
    assert (tmp_path / ".git").exists()
    branch = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "ai"


def test_stage_raw_code_copies_outside_toolkit(tmp_path):
    src = tmp_path / "raw"
    src.mkdir()
    (src / "myscript.py").write_text("print('hi')", encoding="utf-8")
    runtime = tmp_path / "rt"
    dest = stage_raw_code(src, runtime, "demo_raw")
    assert dest == runtime / "staging" / "demo_raw"
    assert (dest / "myscript.py").read_text() == "print('hi')"
