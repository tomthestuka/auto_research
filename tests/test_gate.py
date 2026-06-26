"""Deterministic tests for the Phase-2 step-3 mergeability gate (CLAUDE.md §9.4).

The scanners are pure functions over source text, so most cases need no worktree. The
``run_gate`` integration tests build a throwaway git repo in ``tmp_path`` and inject a
fake Layer-B test runner — no live science stack, no live LLM.
"""

import asyncio
import subprocess

import pytest

from auto_research.sandbox import gate
from auto_research.sandbox.gate import (
    check_structural,
    collect_targets,
    run_gate,
    scan_provenance,
    scan_python_source,
    scan_text_source,
)
from auto_research.schemas import CheckResult, ToolSpec, ValidationDataset


# ── pure scanner: false-positive landmines that MUST stay clean ─────────────────

FP_LANDMINE = '''
"""Module docstring.

Example:
    >>> e_bins = [2., 3., 4., 5., 6., 8.]
"""
import numpy as np


def fit(spec_idx):
    e = np.array(Plot.x(spec_idx))           # wrap REAL xspec output
    m1.relxillCp.Index1.values = [3.0, 0.1, 3.0, 3.0, 5.0, 9.0]
    height_ratios = [2] + [1] * 3
    group = np.zeros(len(e), dtype=np.int16)
    return e
'''


def test_fp_landmine_source_has_no_hard_fails():
    hard, findings = scan_python_source("lib/xspec_help.py", FP_LANDMINE)
    assert hard == [], [c.detail for c in hard]
    # attribute-assignment param vectors are not findings either
    assert findings == [], [c.detail for c in findings]


# ── pure scanner: vocabulary hard-fails ─────────────────────────────────────────


def test_vocabulary_synthetic_and_example_data_fail():
    src = 'x = 1  # build synthetic data for now\ndef g():\n    """returns example data"""\n    return 2\n'
    hard, _ = scan_python_source("lib/x.py", src)
    phrases = {c.detail.split("— ")[-1] for c in hard}
    assert any("synthetic" in p for p in phrases)
    assert any("example data" in p for p in phrases)
    assert any("for now" in p for p in phrases)
    assert all(c.name == "no_fabrication.vocabulary" for c in hard)


def test_vocabulary_whole_word_avoids_collisions():
    # "dummy variable" (statistics) / "toy model" (physics) must NOT trip — only the
    # banned *phrases* (dummy data / toy data) do. ("synthetic" anywhere IS banned.)
    src = '"""Uses a dummy variable in a toy model to regress the spectrum."""\n'
    hard, _ = scan_python_source("lib/x.py", src)
    assert hard == [], [c.detail for c in hard]


def test_synth_obsid_line_is_allowlisted():
    src = 'def test_g():\n    load("synth240611-abcdef")  # synthetic fixture\n    assert True\n'
    hard, _ = scan_python_source("tests/test_x.py", src)
    assert hard == [], [c.detail for c in hard]


def test_incompleteness_markers_are_findings_not_hard_fails():
    # "stub"/"not implemented" are Tier-3 finding_vocabulary (recorded for the co-lead, not a
    # hard-fail) — a legitimate test stub or honest not-implemented branch must not reject.
    src = '"""A stub fixture; the slow path is not implemented here."""\n'
    hard, findings = scan_python_source("tests/test_x.py", src)
    assert hard == [], [c.detail for c in hard]
    phrases = {c.detail.split("— ")[-1] for c in findings}
    assert any("stub" in p for p in phrases) and any("not implemented" in p for p in phrases)
    assert all(c.name == "no_fabrication.vocabulary_finding" and c.passed for c in findings)


# ── pure scanner: RNG hard-fails (seeded or not) ────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        "import numpy as np\ndef g():\n    np.random.seed(0)\n    return 1\n",
        "import numpy as np\ndef g():\n    return np.random.normal(0, 1, 5)\n",
        "from numpy.random import default_rng\ndef g():\n    return default_rng().normal()\n",
        "import random\ndef g():\n    return random.uniform(0, 1)\n",
        "from scipy import stats\ndef g():\n    return stats.norm.rvs(size=3)\n",
    ],
)
def test_any_rng_is_hard_fail(src):
    hard, _ = scan_python_source("lib/x.py", src)
    assert any(c.name == "no_fabrication.rng" for c in hard), src


def test_rng_reported_once_per_line():
    src = "import numpy as np\ndef g():\n    np.random.seed(0)\n"
    hard, _ = scan_python_source("lib/x.py", src)
    rng = [c for c in hard if c.name == "no_fabrication.rng"]
    assert len(rng) == 1


# ── pure scanner: placeholder bodies ────────────────────────────────────────────


def test_placeholder_bodies_fail():
    src = "def a():\n    pass\ndef b():\n    ...\ndef c():\n    raise NotImplementedError\n"
    hard, _ = scan_python_source("lib/x.py", src)
    names = [c for c in hard if c.name == "no_fabrication.placeholder_body"]
    assert len(names) == 3


def test_abstractmethod_empty_body_is_not_placeholder():
    src = "from abc import abstractmethod\nclass P:\n    @abstractmethod\n    def m(self):\n        ...\n"
    hard, _ = scan_python_source("lib/x.py", src)
    assert not any(c.name == "no_fabrication.placeholder_body" for c in hard)


# ── pure scanner: Tier-3 literal-array findings (NOT hard-fails) ─────────────────


def test_literal_arrays_are_findings_not_fails():
    src = (
        "import matplotlib.pyplot as plt\n"
        "import pandas as pd\n"
        "def g():\n"
        "    plt.plot([1, 2, 3], [4, 5, 6])\n"
        "    df = pd.DataFrame({'a': [1, 2, 3]})\n"
        "    return [1.0, 2.0, 3.0]\n"
    )
    hard, findings = scan_python_source("lib/x.py", src)
    assert hard == [], [c.detail for c in hard]
    kinds = {c.name for c in findings}
    assert kinds == {
        "finding.literal_array_return",
        "finding.literal_array_plot",
        "finding.literal_array_dataframe",
    }
    assert all(c.passed for c in findings)  # findings do not fail


# ── pure scanner: provenance ────────────────────────────────────────────────────


def test_figureref_missing_provenance_fails():
    src = 'def g():\n    return FigureRef(path="p.png", caption="c")\n'
    out = scan_provenance("lib/x.py", src)
    assert out and not out[0].passed
    assert "sha256" in out[0].detail and "produced_by" in out[0].detail


def test_figureref_with_provenance_passes():
    src = 'def g():\n    return FigureRef(path="p.png", sha256="ab", produced_by="g")\n'
    assert scan_provenance("lib/x.py", src) == []


def test_no_figureref_is_vacuously_clean():
    assert scan_provenance("lib/x.py", "def g():\n    return 1\n") == []


# ── added-line scoping ──────────────────────────────────────────────────────────


def test_only_added_lines_are_scanned():
    src = "# synthetic data line one\n# representative data line two\n"
    hard, _ = scan_python_source("lib/x.py", src, added={2})
    # line 1 (synthetic) is pre-existing → ignored; line 2 (representative) is added.
    details = [c.detail for c in hard]
    assert any("representative data" in d for d in details)
    assert not any("synthetic" in d for d in details)


# ── scan_text_source: non-Python text files (e.g. shell reduction scripts) ──────


def test_text_scan_clean_shell_passes():
    src = "#!/bin/bash\nnicerl2 indir=$1 clobber=yes\necho done\n"
    hard, finds = scan_text_source("lib/reduce.sh", src)
    assert hard == [] and finds == []


def test_text_scan_banned_vocabulary_hard_fails():
    src = "#!/bin/bash\n# generate synthetic data for now\necho fake\n"
    hard, _ = scan_text_source("lib/reduce.sh", src)
    assert any(c.name == "no_fabrication.vocabulary" and not c.passed for c in hard)


def test_text_scan_synth_obsid_line_allowlisted():
    src = "#!/bin/bash\n# load synth240611-abcdef (synthetic fixture)\ncp data $OUT\n"
    hard, _ = scan_text_source("lib/reduce.sh", src)
    assert hard == []


# ── structural checks ───────────────────────────────────────────────────────────


def _spec(**kw):
    base = dict(
        science_context="ctx",
        expected_behavior="beh",
        proposed_tool_name="my_tool",
        target_domain="demo_utils",
        disposition="extend_existing",
        validation_data=[ValidationDataset(mission="IXPE", obsid="0001")],
    )
    base.update(kw)
    return ToolSpec(**base)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "research_toolkit"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _write_pkg(repo, *, with_test=True, manifest_name="my_tool", lib_body="    return load(obsid)\n",
               manifest_annotated=False):
    pkg = repo / "demo_utils"
    (pkg / "lib").mkdir(parents=True, exist_ok=True)
    (pkg / "skills").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("from .skills import TOOL_MANIFEST\n")
    (pkg / "lib" / "__init__.py").write_text("")
    (pkg / "lib" / "demo_help.py").write_text(f"def my_tool(obsid):\n{lib_body}")
    # `xspec_utils` declares the manifest with a type annotation (`TOOL_MANIFEST: dict = {...}`);
    # the gate must extract keys from that annotated form too (ast.AnnAssign).
    lhs = "TOOL_MANIFEST: dict" if manifest_annotated else "TOOL_MANIFEST"
    (pkg / "skills" / "__init__.py").write_text(
        '%s = {\n  "research_toolkit.demo_utils.%s": {\n'
        '    "module": "demo_utils.lib.demo_help", "fn": "my_tool",\n'
        '    "description": "d", "args_schema": {}}}\n' % (lhs, manifest_name)
    )
    if with_test:
        (pkg / "lib" / "test_demo.py").write_text(
            'def test_my_tool():\n    assert my_tool("synth240611-aa") is not None\n'
        )
    return pkg


def test_structural_missing_test_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo, with_test=False)
    targets = collect_targets(repo)
    out = check_structural(repo, _spec(), targets)
    by = {c.name: c for c in out}
    assert not by["structural.test_present"].passed
    assert by["structural.allowed_paths"].passed
    assert by["structural.tool_manifest_entry"].passed


def test_structural_missing_manifest_entry_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo, manifest_name="other_tool")
    targets = collect_targets(repo)
    out = {c.name: c for c in check_structural(repo, _spec(), targets)}
    assert not out["structural.tool_manifest_entry"].passed


def test_structural_all_pass(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    targets = collect_targets(repo)
    out = {c.name: c for c in check_structural(repo, _spec(), targets)}
    assert all(c.passed for c in out.values())


def test_structural_annotated_manifest_entry_found(tmp_path):
    """`TOOL_MANIFEST: dict = {...}` (annotated, as xspec_utils declares it) is recognised —
    the manifest-entry check must not depend on a plain (un-annotated) assignment."""
    repo = _make_repo(tmp_path)
    _write_pkg(repo, manifest_annotated=True)
    targets = collect_targets(repo)
    out = {c.name: c for c in check_structural(repo, _spec(), targets)}
    assert out["structural.tool_manifest_entry"].passed


# ── run_gate integration (fake Layer-B runner; real temp git worktree) ──────────


async def _pass_runner(worktree, test_paths):
    return {
        "results": [CheckResult(name="layer_b.pytest", layer="B", passed=True, detail="")],
        "deterministic": True,
        "determinism_detail": "",
    }


async def _fail_runner(worktree, test_paths):
    return {
        "results": [CheckResult(name="layer_b.pytest", layer="B", passed=False, detail="1 failed")],
        "deterministic": True,
    }


def test_run_gate_clean_tool_passes(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason
    assert report.report_hash
    assert any(c.name == "layer_b.pytest" and c.passed for c in report.layer_b)


def test_run_gate_fabrication_fails_with_reason(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo, lib_body="    # build synthetic data for now\n    return [1.0, 2.0]\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed
    assert "synthetic" in report.reason or "vocabulary" in report.reason


def test_run_gate_rng_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo, lib_body="    import numpy as np\n    return np.random.normal()\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed
    assert any(c.name == "no_fabrication.rng" for c in report.layer_a)


def test_run_gate_missing_test_fails_before_running_tests(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo, with_test=False)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed
    assert not report.layer_b  # tests skipped because structure failed


def test_run_gate_layer_b_failure_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_fail_runner))
    assert not report.passed
    assert any(c.name == "layer_b.pytest" and not c.passed for c in report.layer_b)


def test_run_gate_records_findings_without_failing(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo, lib_body="    import matplotlib.pyplot as plt\n    plt.plot([1,2],[3,4])\n    return load(obsid)\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason
    assert any(c.name.startswith("finding.") for c in report.findings)


def test_run_gate_nondeterministic_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)

    async def flaky(worktree, test_paths):
        return {
            "results": [CheckResult(name="layer_b.pytest", layer="B", passed=True)],
            "deterministic": False,
            "determinism_detail": "outputs differ",
        }

    report = asyncio.run(run_gate(repo, _spec(), test_runner=flaky))
    assert not report.passed
    assert any(c.name == "determinism" and not c.passed for c in report.layer_a)


# ── non-Python files in the toolkit diff (shell scripts, binaries) ──────────────


def test_run_gate_tracks_and_passes_clean_shell_script(tmp_path):
    repo = _make_repo(tmp_path)
    pkg = _write_pkg(repo)
    (pkg / "lib" / "reduce.sh").write_text("#!/bin/bash\nnicerl2 indir=$1 clobber=yes\n")
    # collect_targets now tracks the .sh too (not only .py).
    assert any(str(p).endswith("reduce.sh") for p in collect_targets(repo))
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason


def test_run_gate_shell_script_fabrication_fails(tmp_path):
    repo = _make_repo(tmp_path)
    pkg = _write_pkg(repo)
    (pkg / "lib" / "reduce.sh").write_text("#!/bin/bash\n# placeholder: synthetic data for now\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed
    assert any(c.name == "no_fabrication.vocabulary" and not c.passed for c in report.layer_a)


def test_run_gate_binary_file_hard_fails(tmp_path):
    repo = _make_repo(tmp_path)
    pkg = _write_pkg(repo)
    (pkg / "lib" / "blob.dat").write_bytes(b"\x00\x01\x02real\x00bytes")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed
    assert any(c.name == "no_fabrication.binary" and not c.passed for c in report.layer_a)
