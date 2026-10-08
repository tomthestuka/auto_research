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
    scan_figure_shape,
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


def test_vocabulary_explicit_fabrication_phrases_fail():
    src = 'x = 1  # build example data for now\ndef g():\n    """returns fake data"""\n    return 2\n'
    hard, _ = scan_python_source("lib/x.py", src)
    phrases = {c.detail.split("— ")[-1] for c in hard}
    assert any("example data" in p for p in phrases)
    assert any("fake data" in p for p in phrases)
    assert any("for now" in p for p in phrases)
    assert all(c.name == "no_fabrication.vocabulary" for c in hard)


def test_emptied_config_sections_scan_as_nothing_to_flag():
    # A human-tuned code_gate.yaml may empty a whole category (e.g. every RNG pattern
    # moved to findings). YAML parses an empty key to None; the scan treats that as
    # "nothing to flag in this category", never a crash.
    emptied = {
        "hard_fail_vocabulary": None,
        "finding_vocabulary": None,
        "hard_fail_code_patterns": None,
        "finding_code_patterns": None,
        "allowlist_obsid_tokens": None,
    }
    src = 'x = np.random.normal(0, 1)  # fake data for now\n'
    hard, findings = scan_python_source("lib/x.py", src, config=emptied)
    assert hard == [] and findings == []


def test_synthetic_is_soft_finding_not_hard_fail():
    # C1: bare "synthetic"/"illustrative" recur in honest §9.5 disclosures, so they are Tier-3
    # findings (co-lead judges intent), not hard-fails. Explicit fabrication phrases stay hard.
    src = '"""Loads the synthetic fixture (§9.5); an illustrative example follows."""\n'
    hard, findings = scan_python_source("lib/x.py", src)
    assert hard == [], [c.detail for c in hard]
    found = {c.detail.split("— ")[-1] for c in findings}
    assert any("synthetic" in p for p in found) and any("illustrative" in p for p in found)


def test_vocabulary_whole_word_avoids_collisions():
    # "dummy variable" (statistics) / "toy model" (physics) must NOT trip — only the
    # banned *phrases* (dummy data / toy data) do.
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


# ── pure scanner: code patterns (severity = which config list holds the pattern) ──
# Policy since 2026-07-16: RNG lives in `finding_code_patterns` — a co-lead-judged
# finding, not a hard-fail — because Monte-Carlo tools need randomness inside them.


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
def test_any_rng_is_finding_not_hard_fail(src):
    hard, findings = scan_python_source("lib/x.py", src)
    assert not any(c.name == "no_fabrication.code_pattern" for c in hard), src
    assert any(c.name == "no_fabrication.code_pattern_finding" for c in findings), src


def test_rng_finding_reported_once_per_line():
    src = "import numpy as np\ndef g():\n    np.random.seed(0)\n"
    _, findings = scan_python_source("lib/x.py", src)
    flagged = [c for c in findings if c.name == "no_fabrication.code_pattern_finding"]
    assert len(flagged) == 1


def test_hard_tier_pattern_still_hard_fails():
    # The hard machinery is intact: a pattern listed in `hard_fail_code_patterns`
    # rejects, and the hard match suppresses a duplicate finding for the same line.
    cfg = {
        "hard_fail_code_patterns": ["np.random"],
        "finding_code_patterns": ["np.random"],
    }
    src = "import numpy as np\ndef g():\n    return np.random.normal()\n"
    hard, findings = scan_python_source("lib/x.py", src, config=cfg)
    assert any(c.name == "no_fabrication.code_pattern" for c in hard)
    assert not any(c.name == "no_fabrication.code_pattern_finding" for c in findings)


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


# ── pure scanner: provenance (the figure dicts a tool returns match FigureRef) ──

_FIG = ('"path": save_path, "caption": "c", "mime_type": "image/png", "produced_by": "t", '
        '"created_at": "2026-01-01T00:00:00Z", "sha256": sha')

# The plotter shapes the toolkit's own templates use (xspec_utils plot_fit / plot_iqu): clean.
CLEAN_FIGURE_SHAPES = {
    "inline": f"def p(save_path, sha):\n    return {{'figures': [{{{_FIG}}}], 'stdout': 'x'}}\n",
    "appended": (f"def p(paths, sha):\n    figures = []\n    for save_path in paths:\n"
                 f"        figures.append({{{_FIG}}})\n    return {{'figures': figures}}\n"),
    "named_dict": (f"def p(save_path, sha):\n    fig = {{{_FIG}}}\n"
                   f"    return {{'figures': [fig]}}\n"),
    "comprehension": f"def p(paths, sha):\n    return {{'figures': [{{{_FIG}}} for save_path in paths]}}\n",
    "subscript_set": (f"def p(save_path, sha):\n    out = {{}}\n    out['figures'] = [{{{_FIG}}}]\n"
                      f"    return out\n"),
    "thumbnail": f"def p(save_path, sha):\n    return {{'figures': [{{{_FIG}, 'thumbnail_b64': None}}]}}\n",
    "no_figures": "def p():\n    return {'stdout': 'x', 'figures': []}\n",
    "opaque_spread": "def p(base):\n    return {'figures': [{**base, 'caption': 'c'}]}\n",
}

# Every problem kind the check reports, one seeded breach each: (source, detail substring). Each
# seeded tool is `p(save_path, sha)`, so the runtime half of the contract can call it too.
SEEDED_FIGURE_BREACHES = {
    "figures_not_a_list": ("def p(save_path, sha):\n    return {'figures': {" + _FIG + "}}\n",
                           "'figures' is a dict"),
    "figures_a_string": ("def p(save_path, sha):\n    return {'figures': f'{save_path}'}\n",
                         "'figures' is a string"),
    "named_not_a_list": ("def p(save_path, sha):\n    figures = {" + _FIG + "}\n"
                         "    return {'figures': figures}\n", "'figures' is a dict"),
    "entry_not_a_dict": ("def p(save_path, sha):\n    return {'figures': [f'{save_path}']}\n",
                         "a figure entry is a string"),
    "missing_key": ("def p(save_path, sha):\n    return {'figures': [{'path': save_path, 'caption': 'c'}]}\n",
                    "missing ['created_at', 'mime_type', 'produced_by', 'sha256']"),
    "missing_key_appended": ("def p(save_path, sha):\n    figs = []\n    for q in [save_path]:\n"
                             "        figs.append({'path': q, 'caption': 'c', 'mime_type': 'image/png',"
                             " 'produced_by': 't', 'created_at': 'x'})\n    return {'figures': figs}\n",
                             "missing ['sha256']"),
    "extra_key": ("def p(save_path, sha):\n    return {'figures': [{" + _FIG + ", 'dpi': 150}]}\n",
                  "keys FigureRef rejects ['dpi']"),
    "extra_key_dict_call": ("def p(save_path, sha):\n    return dict(figures=[dict(path=save_path, caption='c', "
                            "mime_type='m', produced_by='t', created_at='2026-01-01T00:00:00Z', "
                            "sha256=sha, label='x')])\n", "keys FigureRef rejects ['label']"),
    "bad_literal_value": ("def p(save_path, sha):\n    return {'figures': [{'path': save_path, 'caption': '',"
                          " 'mime_type': 'image/png', 'produced_by': 't', 'created_at': 'soon',"
                          " 'sha256': 'abc'}]}\n", "sha256='abc' invalid"),
}


@pytest.mark.parametrize("name", sorted(CLEAN_FIGURE_SHAPES))
def test_figure_shape_clean(name):
    assert scan_figure_shape("lib/x.py", CLEAN_FIGURE_SHAPES[name]) == []


@pytest.mark.parametrize("name", sorted(SEEDED_FIGURE_BREACHES))
def test_figure_shape_breach_is_caught(name):
    src, needle = SEEDED_FIGURE_BREACHES[name]
    out = scan_figure_shape("lib/x.py", src)
    assert out and not any(c.passed for c in out)
    assert all(c.name == "provenance.figure_shape" for c in out)
    assert any(needle in c.detail for c in out), [c.detail for c in out]


def test_every_seeded_breach_is_one_the_runtime_drops():
    """The gate fails exactly what `parse_tool_figures` drops: evaluate each seeded tool's return
    and confirm the runtime keeps none of its figures (or ignores a non-list `figures`)."""
    from auto_research.sandbox.runner import parse_tool_figures

    for name, (src, _needle) in SEEDED_FIGURE_BREACHES.items():
        ns: dict = {}
        exec(compile(src, name, "exec"), ns)
        payload = ns["p"]("/tmp/a.png", "a" * 64)
        figures, notes = parse_tool_figures(payload["figures"])
        assert figures == [] and notes, name


def test_figure_shape_scoped_to_added_lines():
    src, _needle = SEEDED_FIGURE_BREACHES["missing_key"]
    assert scan_figure_shape("lib/x.py", src, added={1}) == []      # the dict is on line 2
    assert scan_figure_shape("lib/x.py", src, added={2})


def test_figure_shape_nested_function_reported_once():
    src = "def outer():\n    def p(q):\n        return {'figures': [{'path': q}]}\n    return p\n"
    assert len(scan_figure_shape("lib/x.py", src)) == 1


def test_toolkit_plotters_are_clean():
    """The real template plotters (`plot_fit`, `plot_iqu`) pass, when the toolkit is on this host."""
    from pathlib import Path

    from auto_research.graph.runtime import load_orchestration_config

    root = load_orchestration_config().get("toolkit_root")
    session = Path(root or "/nonexistent") / "xspec_utils" / "skills" / "session.py"
    if not session.is_file():
        pytest.skip("research_toolkit not on this host")
    assert scan_figure_shape("xspec_utils/skills/session.py", session.read_text()) == []


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
    assert by["structural.changes_allowed"].passed
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


def test_structural_entry_found_despite_descriptive_target_domain(tmp_path):
    """[A12] regression: the manifest search is derived from the touched changeset, so a
    descriptive ``target_domain`` label (what doomed run #12) no longer misdirects the gate to
    a nonexistent path. The entry lands in demo_utils; the brief says something free-filled."""
    repo = _make_repo(tmp_path)
    _write_pkg(repo)  # entry in demo_utils/skills/__init__.py
    targets = collect_targets(repo)
    spec = _spec(target_domain="demo_utils (IXPE polarimetry, freshly authored)")
    out = {c.name: c for c in check_structural(repo, spec, targets)}
    assert out["structural.tool_manifest_entry"].passed


def test_structural_missing_entry_detail_lists_found_keys(tmp_path):
    """The rejection names the keys actually present per touched package, so a name mismatch is
    self-diagnosable in round 1 (part 2 — replaces the misleading 'no entry at <bad path>')."""
    repo = _make_repo(tmp_path)
    _write_pkg(repo, manifest_name="other_tool")
    targets = collect_targets(repo)
    out = {c.name: c for c in check_structural(repo, _spec(), targets)}
    entry = out["structural.tool_manifest_entry"]
    assert not entry.passed
    assert "my_tool" in entry.detail  # the name it looked for
    assert "other_tool" in entry.detail  # the key actually present
    assert "demo_utils" in entry.detail  # the touched package searched


# ── run_gate integration (fake Layer-B runner; real temp git worktree) ──────────


async def _pass_runner(worktree, test_paths, **_kw):
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


def test_run_gate_malformed_figure_fails_with_reason(tmp_path):
    """A tool returning a figure dict the runtime would drop is rejected, naming file and key."""
    repo = _make_repo(tmp_path)
    _write_pkg(repo, lib_body="    return {'figures': [{'path': obsid, 'caption': 'c'}]}\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed
    bad = [c for c in report.layer_a if c.name == "provenance.figure_shape"]
    assert bad and "demo_utils/lib/demo_help.py:2" in bad[0].detail and "sha256" in bad[0].detail


def test_run_gate_test_file_figure_fixtures_are_exempt(tmp_path):
    """A test may build a deliberately partial figure dict to assert on; it is not a tool return."""
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        "def test_my_tool():\n    bad = {'figures': [{'path': 'x'}]}\n"
        "    assert my_tool('synth240611-aa') is not None and bad\n"
    )
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason


def test_run_gate_rng_passes_with_finding(tmp_path):
    # RNG in a tool is mergeable (Monte-Carlo needs randomness inside tools) and is
    # surfaced to the co-lead as a finding on the gate report.
    repo = _make_repo(tmp_path)
    _write_pkg(repo, lib_body="    import numpy as np\n    return np.random.normal()\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed
    assert any(c.name == "no_fabrication.code_pattern_finding" for c in report.findings)


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


# ── determinism by per-test outcomes (junit), not byte-identical output (handoff #2) ──

_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="{t}">
    <testcase classname="test_demo" name="test_pass" time="{t}"/>
    <testcase classname="test_demo" name="test_fail" time="{t}"><failure message="x">trace {t}</failure></testcase>
    <testcase classname="test_demo" name="test_err" time="{t}"><error message="x">boom {t}</error></testcase>
    <testcase classname="test_demo" name="test_skip" time="0.0"><skipped message="x"/></testcase>
  </testsuite>
</testsuites>
"""


def test_parse_junit_outcomes_reads_each_verdict(tmp_path):
    p = tmp_path / "j.xml"
    p.write_text(_JUNIT.format(t="0.12"))
    outcomes = gate._parse_junit_outcomes(p)
    assert outcomes == {
        "test_demo::test_pass": "PASSED",
        "test_demo::test_fail": "FAILED",
        "test_demo::test_err": "ERROR",
        "test_demo::test_skip": "SKIPPED",
    }


def test_parse_junit_outcomes_absent_or_unparseable_is_empty(tmp_path):
    assert gate._parse_junit_outcomes(tmp_path / "nope.xml") == {}
    bad = tmp_path / "bad.xml"
    bad.write_text("<not-xml")
    assert gate._parse_junit_outcomes(bad) == {}


def test_layer_b_verdict_requires_a_passed_test():
    r"""rc==0 alone is not a pass: an all-SKIPPED suite (validation data absent /
    hardcoded / off-host) exited 0 but verified nothing, so it must fail (§9.4)."""
    # >=1 PASSED -> pass.
    ok, detail = gate._layer_b_verdict(0, {"t::a": "PASSED", "t::b": "SKIPPED"}, "out")
    assert ok and detail == ""
    # All skipped -> fail, with a specific "not verified" reason (not a generic failure).
    bad, detail = gate._layer_b_verdict(0, {"t::a": "SKIPPED", "t::b": "SKIPPED"}, "logtail")
    assert not bad
    assert "no test PASSED" in detail and "§9.4" in detail
    # Empty map (e.g. junit unparseable on an rc==0 run) -> fail closed.
    empty_bad, _ = gate._layer_b_verdict(0, {}, "")
    assert not empty_bad


def test_layer_b_verdict_nonzero_rc_fails_with_output():
    """A real failure (rc!=0) fails and surfaces the pytest output tail, unchanged."""
    bad, detail = gate._layer_b_verdict(1, {"t::a": "FAILED"}, "assertion boom")
    assert not bad
    assert "assertion boom" in detail


def test_determinism_same_outcomes_pass():
    m = {"test_demo::test_a": "PASSED", "test_demo::test_b": "FAILED"}
    deterministic, detail = gate._outcomes_determinism(m, dict(m))
    assert deterministic
    assert detail == ""


def test_determinism_outcome_flip_rejected():
    m1 = {"test_demo::test_a": "PASSED"}
    m2 = {"test_demo::test_a": "FAILED"}
    deterministic, detail = gate._outcomes_determinism(m1, m2)
    assert not deterministic
    assert "test_demo::test_a" in detail
    assert "PASSED" in detail and "FAILED" in detail


def test_determinism_same_count_different_tests_rejected():
    # A bare rc1==rc2 would miss this; the outcome-map compare catches the swap.
    m1 = {"test_demo::t1": "PASSED", "test_demo::t2": "FAILED"}
    m2 = {"test_demo::t1": "FAILED", "test_demo::t2": "PASSED"}
    deterministic, _ = gate._outcomes_determinism(m1, m2)
    assert not deterministic


def test_determinism_differing_logs_same_outcomes_pass(tmp_path):
    # Two junit reports that differ only in timing/log text (the volatility that the old
    # byte-compare false-flagged) parse to the same outcome map → deterministic.
    a = tmp_path / "a.xml"
    b = tmp_path / "b.xml"
    a.write_text(_JUNIT.format(t="0.11"))
    b.write_text(_JUNIT.format(t="9.87"))
    map_a = gate._parse_junit_outcomes(a)
    map_b = gate._parse_junit_outcomes(b)
    assert map_a == map_b
    deterministic, detail = gate._outcomes_determinism(map_a, map_b)
    assert deterministic
    assert detail == ""


# ── gate_test_timeout precedence: per-package tool_config > orchestration > 900 ──


def test_gate_test_timeout_global_default_when_no_tool_config(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)  # creates demo_utils/, no tool_config.yaml
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {"gate_test_timeout_s": 1234})
    assert gate._gate_test_timeout(repo, collect_targets(repo)) == 1234


def test_gate_test_timeout_falls_back_to_900_when_unset(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {})
    assert gate._gate_test_timeout(repo, collect_targets(repo)) == 900


def test_gate_test_timeout_per_package_override_wins(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    pkg = _write_pkg(repo)
    (pkg / "tool_config.yaml").write_text("gate_test_timeout_s: 3600\ncall_timeout_s:\n  my_tool: 100\n")
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {"gate_test_timeout_s": 900})
    assert gate._gate_test_timeout(repo, collect_targets(repo)) == 3600


def test_gate_test_timeout_malformed_tool_config_falls_back(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    pkg = _write_pkg(repo)
    (pkg / "tool_config.yaml").write_text("gate_test_timeout_s: [not, an, int]\n")
    monkeypatch.setattr(gate, "_orchestration_config", lambda: {"gate_test_timeout_s": 777})
    assert gate._gate_test_timeout(repo, collect_targets(repo)) == 777


# ── a banned word inside a negation states the discipline (§9.5) ──────────────


def test_a_negated_banned_word_is_a_finding_not_a_hard_fail():
    """Prose stating the no-fabrication discipline is exactly what §9.5 asks for, and the
    vocabulary scan used to reject a tool for saying it.

    All four strings are verbatim from real toolkit diffs; the first cost the first gate
    rejection of a live trial, one of that link's five submissions
    (`xspec_utils/README.md:842 — banned phrase 'fabricate'`).
    """
    lines = [
        '"""The module contains no table of published values, so the tool can never '
        'back-fill or fabricate a benchmark."""\n',
        '"""No ccf_master.csv or qu_slices CSV is fabricated in this path."""\n',
        '"""The check runs on the committed product without any fabricated fixture."""\n',
        '"""Runs on real, already-local data rather than a wholly-fabricated CSV."""\n',
    ]
    for src in lines:
        hard, findings = scan_python_source("lib/x.py", src)
        assert hard == [], f"{src!r} hard-failed: {[c.detail for c in hard]}"
        assert any("negation" in c.detail for c in findings), src


def test_a_negation_governing_another_clause_stays_a_hard_fail():
    """The scope is the clause, not a token window.

    This comment is `gate_bench`'s `fab_shell_script`, one of six planted defects the published
    catch-rate table attributes to the scan: the negation governs *running the reduction*, and
    the banned phrase is then the object of a positive instruction. A proximity rule reads that
    as exculpatory and voids the cell.
    """
    src = '# where the reduction has not run yet, emit fake data for now\nx = 1\n'
    hard, _ = scan_python_source("lib/x.py", src)
    phrases = {c.detail.split("— ")[-1] for c in hard}
    assert any("fake data" in p for p in phrases), [c.detail for c in hard]
    assert any("for now" in p for p in phrases), [c.detail for c in hard]


def test_a_plain_banned_word_still_hard_fails():
    """The demotion is scoped to negation; an unqualified claim is untouched."""
    src = '# TODO: the fabricated fallback below holds until the real binning is settled\nx = 1\n'
    hard, _ = scan_python_source("lib/x.py", src)
    assert any("fabricated" in c.detail for c in hard), [c.detail for c in hard]


def test_the_negation_demotion_applies_to_non_python_files_too():
    """A README or a shell script goes through the text scanner, and that is where the live
    false positive landed (`xspec_utils/README.md`)."""
    src = "The tool can never fabricate a benchmark; a missing reference is left as a gap.\n"
    hard, findings = scan_text_source("README.md", src)
    assert hard == [], [c.detail for c in hard]
    assert any("negation" in c.detail for c in findings)

    src2 = "# where the reduction has not run yet, emit fake data for now\n"
    hard2, _ = scan_text_source("reduce.sh", src2)
    assert any("fake data" in c.detail for c in hard2), [c.detail for c in hard2]


def test_the_negation_rule_on_the_real_lines_from_a_live_authoring_attempt():
    """Verbatim added lines from block 0 attempt 5's saved patches, both directions.

    Run over the six patches the attempt actually left on disk, the rule fires on three
    `placeholder` occurrences: two demoted, one kept hard. The kept one is what matters — the
    same submission still hard-fails, so the demotion cannot turn a rejecting diff into a
    passing one.

    Recorded honestly: **both demotions here are coincidental.** The negation governs the
    *condition* ("only when NEITHER … exist does the annotated placeholder panel appear"), not
    the banned word, so this is the `fab_shell_script` shape with no comma to separate the
    clauses. The rule is crude in both directions by design — a missed negation degrades to
    today's hard fail, a spurious one to a finding the co-lead reads — and this pins the
    measured behaviour rather than an aspiration.
    """
    from auto_research.sandbox.gate import _negated

    # Demoted (cue in-clause, though not governing the word) — README prose.
    condition = (
        "only when NEITHER measured NOR reference Stokes exist does the annotated "
        "placeholder panel appear."
    )
    assert _negated(condition.lower(), "placeholder")

    # Kept hard — a manifest description string in the same submission.
    manifest = '"annotated placeholder panel drawn. RETURN: "'
    assert not _negated(manifest.lower(), "placeholder")

    # So the submission as a whole still carries a hard fail.
    hard, findings = scan_text_source("README.md", condition + "\n")
    assert hard == [] and any("negation" in c.detail for c in findings)
    hard2, _ = scan_python_source("skills/__init__.py", f"D = {manifest}\n")
    assert any("placeholder" in c.detail for c in hard2), [c.detail for c in hard2]


# ── F2: changes_allowed is a real check, over the WIDE enumeration ───────────


def test_f2_a_change_outside_a_utils_package_fails_changes_allowed(tmp_path):
    """THE F2 REGRESSION. The old check filtered its input through the same pattern it then
    tested, so it could not fail for any input.

    Measured live 2026-09-08 (slot 4): 193 changed lines in `obsdb_catalog` were never scanned,
    never tested, never shown to the reviewer, reported as "only allowed paths touched", and
    unstageable — so the commit that landed imports `neutral.advance_many` from a file that was
    never committed and does not run on a clean checkout. `gate_bench`'s own
    `hole_structural_unfalsifiable` mutant was planted against this hole and reached the co-lead
    as the only catcher.
    """
    from auto_research.sandbox.gate import check_structural, collect_all_changes, collect_targets

    repo = _make_repo(tmp_path)
    _write_pkg(repo)                                        # legitimate *_utils work
    (repo / "obsdb_catalog").mkdir(parents=True, exist_ok=True)
    (repo / "obsdb_catalog" / "neutral.py").write_text("def advance_many(*a):\n    return None\n")

    changes = collect_all_changes(repo)
    assert "obsdb_catalog/neutral.py" in changes, "the wide enumeration must see it at all"

    out = {c.name: c for c in check_structural(repo, _spec(), collect_targets(repo), changes)}
    check = out["structural.changes_allowed"]
    assert not check.passed
    assert "obsdb_catalog/neutral.py" in check.detail
    assert "message the lead" in check.detail, "the rejection must name the route, not just the rule"


def test_f2_the_check_defaults_to_asking_the_tree(tmp_path):
    """Called without an explicit changeset, `check_structural` derives the wide enumeration
    itself — so no caller can reintroduce the defect by forgetting to pass it."""
    from auto_research.sandbox.gate import check_structural, collect_targets

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "stray_root_file.py").write_text("x = 1\n")

    out = {c.name: c for c in check_structural(repo, _spec(), collect_targets(repo))}
    assert not out["structural.changes_allowed"].passed
    assert "stray_root_file.py" in out["structural.changes_allowed"].detail


def test_f2_a_pure_utils_changeset_still_passes(tmp_path):
    """The allow-list must not reject legitimate work: a changeset entirely under `*_utils`
    passes, including new packages (§9.2 plans `heasarc_utils`, `lib_utils`)."""
    from auto_research.sandbox.gate import check_structural, collect_all_changes, collect_targets

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    newpkg = repo / "heasarc_utils" / "lib"
    newpkg.mkdir(parents=True)
    (newpkg / "acquire.py").write_text("def acquire(o):\n    return o\n")

    changes = collect_all_changes(repo)
    out = {c.name: c for c in check_structural(repo, _spec(), collect_targets(repo), changes)}
    assert out["structural.changes_allowed"].passed, changes


def test_f2_ignored_files_are_not_changes(tmp_path):
    """`git status` omits ignored paths, so build artifacts cannot fail the allow-list."""
    from auto_research.sandbox.gate import collect_all_changes

    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("junk/\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "ignore junk")
    (repo / "junk").mkdir()
    (repo / "junk" / "a.txt").write_text("noise\n")

    assert not [c for c in collect_all_changes(repo) if c.startswith("junk/")]


def test_n1_layer_b_failure_keeps_far_more_than_the_old_2000_chars(tmp_path):
    """N1: a chatty science suite's last 2,000 characters are entirely the warnings summary, so
    the failing test's traceback was cut and the coder could not reproduce what it was never
    shown (slot 2: 3 h 28 m, $16.26, two named quantities lost)."""
    from auto_research.sandbox.gate import _layer_b_output_cap, _layer_b_verdict

    assert _layer_b_output_cap() >= 100000
    traceback = "E   assert 0.5 == 0.9   <- the line that matters\n"
    noise = "".join(f"  warnings.warn('deprecated {i}')\n" for i in range(4000))
    out = traceback + noise + "FAILED tests/test_t.py::test_x\n1 failed, 21 passed\n"
    assert len(out) > 2000

    passed, detail = _layer_b_verdict(1, {"tests/test_t.py::test_x": "FAILED"}, out)
    assert not passed
    # The traceback comes FIRST in pytest output and the warnings summary last, so a tail-only
    # window keeps the useless end — the actual slot-2 failure. Both ends must survive.
    assert "the line that matters" in detail, "the traceback must survive the window"
    assert "1 failed, 21 passed" in detail, "the summary must survive too"
    assert "elided" in detail, "and an over-cap output must say so rather than look complete"


# ── environment definitions in the changeset (env plan B3c-1) ──────────────────────────────────

_LOCK = ("@EXPLICIT\n"
         "https://conda.anaconda.org/conda-forge/linux-64/python-3.9.18-h1_0.conda#" + "a" * 32 + "\n"
         "https://conda.anaconda.org/conda-forge/noarch/mock-5.1.0-pyhd_0.conda#" + "b" * 32 + "\n")


def _with_env(repo, monkeypatch, *, lock=_LOCK, names_it=True, extra=None):
    from auto_research import env as app_env

    monkeypatch.setattr(app_env, "_orch_config", lambda: {"env_externals": {}})
    # The gate builds the definition it judges (B3c-2): conda faked at the process boundary.
    from tests.test_env_build import _fake_conda

    from auto_research.sandbox import env_build, jail

    fake = _fake_conda([])

    class _Runner(env_build.StoppableRunner):
        def __call__(self, argv, **kwargs):
            return fake(argv, **kwargs)

    monkeypatch.setattr(env_build, "StoppableRunner", _Runner)
    monkeypatch.setattr(jail, "jail_enabled", lambda: False)
    monkeypatch.setattr(app_env, "conda_executable", lambda: "conda")
    _write_pkg(repo)
    if names_it:
        (repo / "demo_utils" / "tool_config.yaml").write_text("env: sherpa\n")
    folder = repo / "envs" / "sherpa"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: sherpa for the demo tools\n")
    (folder / "env.lock").write_text(lock)
    for name, text in (extra or {}).items():
        (folder / name).write_text(text)
    return folder


def test_an_environment_definition_with_its_package_passes_and_is_gated(tmp_path, monkeypatch):
    from auto_research.sandbox.gate import collect_targets

    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch, extra={"activate.sh": "export SHERPA_READY=1\n"})
    gated = {str(p.relative_to(repo)) for p in collect_targets(repo)}
    assert {"envs/sherpa/env.yaml", "envs/sherpa/env.lock", "envs/sherpa/activate.sh"} <= gated
    # A conda package named like a banned word (`mock`) is a pinned lock line, not prose.
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason
    env_check = [c for c in report.layer_a if c.name == "structural.env_definition.sherpa"]
    assert env_check and env_check[0].passed


@pytest.mark.parametrize("case,needle", [
    ("bad_lock", "conda-forge package URL"),
    ("unnamed", "add `env: sherpa`"),
    ("stray", "environment definition"),
])
def test_a_bad_environment_change_fails_with_the_reason(tmp_path, monkeypatch, case, needle):
    repo = _make_repo(tmp_path)
    if case == "bad_lock":
        _with_env(repo, monkeypatch, lock="@EXPLICIT\nhttps://repo.anaconda.com/pkgs/main/x.conda#" + "a" * 32 + "\n")
    elif case == "unnamed":
        _with_env(repo, monkeypatch, names_it=False)
    else:
        _with_env(repo, monkeypatch, extra={"notes.txt": "scratch\n"})
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed and needle in report.reason, report.reason


def test_approve_edit_stages_the_environment_definition_with_the_tool(tmp_path, monkeypatch):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch)
    (repo / "scratch.txt").write_text("not staged\n")
    sha = approve_edit(repo, message="Add my_tool with its sherpa env")
    committed = set(subprocess.run(["git", "show", "--name-only", "--pretty=format:", sha], cwd=repo,
                                   capture_output=True, text=True, check=True).stdout.split())
    assert {"envs/sherpa/env.yaml", "envs/sherpa/env.lock", "demo_utils/tool_config.yaml"} <= committed
    assert "scratch.txt" not in committed


def test_the_reviewer_sees_the_environment_definition(tmp_path, monkeypatch):
    from auto_research.sandbox.review import build_diff_bundle

    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch, extra={"activate.sh": "export SHERPA_READY=1\n"})
    bundle = build_diff_bundle(repo)
    assert "### NEW FILE: envs/sherpa/env.lock" in bundle and "SHERPA_READY" in bundle


def test_a_misspelled_env_name_fails_resolution_with_the_fix(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch)
    (repo / "demo_utils" / "tool_config.yaml").write_text("env: sherpaa\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    resolves = next(c for c in report.layer_a if c.name == "structural.env_resolves")
    assert not resolves.passed and "envs/sherpaa/env.yaml does not exist" in resolves.detail


def test_the_target_package_env_must_resolve_even_untouched(tmp_path, monkeypatch):
    """The brief's target package names the env the session works on; an unresolvable name fails."""
    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed")
    (repo / "demo_utils" / "tool_config.yaml").write_text("env: gone\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "renamed")
    other = repo / "other_utils" / "lib"
    other.mkdir(parents=True)
    (other / "x.py").write_text("def x():\n    return 1\n")
    report = asyncio.run(run_gate(repo, _spec(target_domain="demo_utils"), test_runner=_pass_runner))
    resolves = next(c for c in report.layer_a if c.name == "structural.env_resolves")
    assert not resolves.passed and "'gone'" in resolves.detail


def test_a_deleted_definition_file_is_validated_staged_and_shown(tmp_path, monkeypatch):
    from auto_research.sandbox.merge_flow import approve_edit
    from auto_research.sandbox.review import build_diff_bundle

    repo = _make_repo(tmp_path)
    folder = _with_env(repo, monkeypatch, extra={"activate.sh": "export A=1\n"})
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed env")
    (folder / "activate.sh").unlink()
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        'def test_my_tool():\n    assert my_tool("synth240611-ab") is not None\n')
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason
    assert "### DELETED FILE: envs/sherpa/activate.sh" in build_diff_bundle(repo)
    sha = approve_edit(repo, message="drop the activation")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert "activate.sh" not in status
    shown = subprocess.run(["git", "show", "--name-status", "--pretty=format:", sha], cwd=repo,
                           capture_output=True, text=True).stdout
    assert "D\tenvs/sherpa/activate.sh" in shown


def test_comment_lines_of_a_lock_are_scanned_as_prose(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch, lock="# TODO fake the data here, simulated placeholder values\n" + _LOCK)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed and "envs/sherpa/env.lock" in report.reason


@pytest.mark.parametrize("named", ["../demo_utils", "/abs/outside", "sherpa/", "./sherpa", "[1, 2]", "{"])
def test_an_env_named_like_a_path_or_not_a_name_fails_resolution(tmp_path, monkeypatch, named):
    repo = _make_repo(tmp_path)
    _with_env(repo, monkeypatch)
    (repo / "demo_utils" / "env.yaml").write_text("purpose: smuggled\n")
    (repo / "demo_utils" / "tool_config.yaml").write_text(f"env: {named}\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    resolves = next(c for c in report.layer_a if c.name == "structural.env_resolves")
    assert not resolves.passed, named


def test_a_link_among_the_changes_is_refused(tmp_path, monkeypatch):
    import shutil

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed")
    outside = tmp_path / "outside_lib"
    shutil.copytree(repo / "demo_utils" / "lib", outside)
    (outside / "evil.py").write_text("x = 1\n")
    shutil.rmtree(repo / "demo_utils" / "lib")
    (repo / "demo_utils" / "lib").symlink_to(outside)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and "demo_utils/lib" in allowed.detail


def test_a_non_ascii_file_name_is_judged_by_its_real_name(tmp_path, monkeypatch):
    from auto_research.sandbox.gate import collect_all_changes

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "shé.py").write_text("x = 1\n")
    assert "demo_utils/lib/shé.py" in collect_all_changes(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert next(c for c in report.layer_a if c.name == "structural.changes_allowed").passed


def test_a_nested_repository_in_a_package_is_refused(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    vend = repo / "demo_utils" / "vend"
    vend.mkdir()
    _git(vend, "init", "-q")
    (vend / "helper.py").write_text("x = 1\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and "demo_utils/vend/" in allowed.detail


def test_a_package_inside_a_protected_folder_is_not_a_package(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    inside = repo / "obsdb_catalog" / "evil_utils"
    inside.mkdir(parents=True)
    (inside / "__init__.py").write_text("x = 1\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and "obsdb_catalog/evil_utils/__init__.py" in allowed.detail


def test_tests_that_write_into_the_worktree_fail_the_gate(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)

    async def writing_runner(export, test_paths):
        # Tests run in a clean export; one that reaches the real worktree by its absolute path:
        (repo / "demo_utils" / "lib" / "late.py").write_text("y = 2\n")
        (repo / "scratch.txt").write_text("output\n")
        return await _pass_runner(export, test_paths)

    report = asyncio.run(run_gate(repo, _spec(), test_runner=writing_runner))
    moved = next(c for c in report.layer_a if c.name == "structural.tests_left_tree_unchanged")
    assert not report.passed and not moved.passed
    assert "demo_utils/lib/late.py" in moved.detail and "scratch.txt" in moved.detail


def test_layer_b_runs_on_exactly_the_judged_files(tmp_path):
    """Ignored or planted files in the worktree never reach the tests (B3c-1 review 4)."""
    repo = _make_repo(tmp_path)
    (repo / ".gitignore").write_text("*.pyc\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "ignore bytecode")
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "hidden.pyc").write_bytes(b"planted")
    seen = {}

    async def looking_runner(export, test_paths):
        seen["files"] = sorted(str(p.relative_to(export)) for p in export.rglob("*") if p.is_file())
        return await _pass_runner(export, test_paths)

    report = asyncio.run(run_gate(repo, _spec(), test_runner=looking_runner))
    assert report.passed, report.reason
    assert "demo_utils/lib/demo_help.py" in seen["files"]
    assert not any(f.endswith("hidden.pyc") for f in seen["files"]), seen["files"]
    # A `.gitignore` the coder writes (hiding itself and a module) is a refused change.
    (repo / "demo_utils" / ".gitignore").write_text(".gitignore\nlib/secret.py\n")
    (repo / "demo_utils" / "lib" / "secret.py").write_text("SECRET = 1\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=looking_runner))
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and "demo_utils/.gitignore" in allowed.detail


def test_a_judged_file_swapped_for_a_link_or_made_executable_is_not_committed(tmp_path):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed
    test_file = repo / "demo_utils" / "lib" / "test_demo.py"
    test_file.chmod(0o755)
    with pytest.raises(RuntimeError, match="changed after the gate"):
        approve_edit(repo, message="m", manifest=report.manifest)
    test_file.chmod(0o644)
    helper = repo / "demo_utils" / "lib" / "demo_help.py"
    (tmp_path / "copy.py").write_bytes(helper.read_bytes())
    helper.unlink()
    helper.symlink_to(tmp_path / "copy.py")
    with pytest.raises(RuntimeError, match="changed after the gate"):
        approve_edit(repo, message="m", manifest=report.manifest)


def test_approve_edit_never_commits_what_was_staged_outside_the_manifest(tmp_path):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    (repo / "README.md").write_text("rewritten\n")
    _git(repo, "add", "README.md")
    sha = approve_edit(repo, message="m", manifest=report.manifest)
    committed = set(subprocess.run(["git", "show", "--name-only", "--pretty=format:", sha], cwd=repo,
                                   capture_output=True, text=True, check=True).stdout.split())
    assert "README.md" not in committed


def test_approve_edit_commits_exactly_what_the_gate_judged(tmp_path):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed and "demo_utils/lib/demo_help.py" in report.manifest
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text("def my_tool(obsid):\n    return 0\n")
    with pytest.raises(RuntimeError, match="changed after the gate"):
        approve_edit(repo, message="m", manifest=report.manifest)
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text("def my_tool(obsid):\n    return load(obsid)\n")
    sha = approve_edit(repo, message="m", manifest=report.manifest)
    committed = set(subprocess.run(["git", "show", "--name-only", "--pretty=format:", sha], cwd=repo,
                                   capture_output=True, text=True, check=True).stdout.split())
    assert committed == set(report.manifest)


def test_the_review_bundle_survives_a_binary_file(tmp_path):
    from auto_research.sandbox.review import build_diff_bundle

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "blob.pyc").write_bytes(b"\xff\xfe\x00\x01")
    assert "blob.pyc" in build_diff_bundle(repo)


def test_the_export_is_the_committed_tree_whatever_gitattributes_say(tmp_path):
    """No `export-ignore`/`export-subst`, an outside link kept as a link, a deletion and an
    executable bit carried over: the tests see exactly what the commit holds (review 5)."""
    import os

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    lib = repo / "demo_utils" / "lib"
    (lib / "old.py").write_text("OLD = 1\n")
    (lib / "const.py").write_text("STAMP = '$Format:%H$'\n")
    (lib / "gone.py").write_text("GONE = 1\n")
    (repo / "demo_utils" / ".gitattributes").write_text("lib/old.py export-ignore\nlib/const.py export-subst\n")
    os.symlink("/data/elsewhere", repo / "data")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed by the human")
    (lib / "gone.py").unlink()
    (lib / "demo_help.py").write_text("def my_tool(obsid):\n    return load(obsid) or 1\n")
    (lib / "run.sh").write_text("#!/bin/sh\necho ok\n")
    (lib / "run.sh").chmod(0o755)
    (lib / "test_demo.py").write_text('def test_my_tool():\n    assert my_tool("synth240611-ac") is not None\n')
    seen = {}

    async def looking_runner(export, test_paths):
        seen["old"] = (export / "demo_utils/lib/old.py").is_file()
        seen["const"] = (export / "demo_utils/lib/const.py").read_text()
        seen["link"] = os.readlink(export / "data")
        seen["gone"] = (export / "demo_utils/lib/gone.py").exists()
        seen["exec"] = os.access(export / "demo_utils/lib/run.sh", os.X_OK)
        return await _pass_runner(export, test_paths)

    report = asyncio.run(run_gate(repo, _spec(), test_runner=looking_runner))
    assert report.passed, report.reason
    assert seen == {"old": True, "const": "STAMP = '$Format:%H$'\n", "link": "/data/elsewhere",
                    "gone": False, "exec": True}


def test_a_committed_folder_link_replaced_by_a_folder_never_writes_through(tmp_path):
    """HEAD links a package folder outside; the coder makes it a real folder. The export replaces
    the link as git does, nothing lands outside, and the commit records the change (review 6)."""
    import os

    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), repo / "demo_utils" / "data")
    (repo / "demo_utils" / "lib" / "launch.sh").write_text("#!/bin/sh\n")
    (repo / "demo_utils" / "lib" / "launch.sh").chmod(0o755)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "linked data")
    (repo / "demo_utils" / "data").unlink()
    (repo / "demo_utils" / "data").mkdir()
    (repo / "demo_utils" / "data" / "table.py").write_text("ROWS = 1\n")
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        'def test_my_tool():\n    assert my_tool("synth240611-ad") is not None\n')
    seen = {}

    async def looking_runner(export, test_paths):
        seen["data_is_dir"] = (export / "demo_utils/data").is_dir() and not (export / "demo_utils/data").is_symlink()
        seen["exec"] = os.access(export / "demo_utils/lib/launch.sh", os.X_OK)
        return await _pass_runner(export, test_paths)

    report = asyncio.run(run_gate(repo, _spec(), test_runner=looking_runner))
    assert report.passed, report.reason
    assert list(outside.iterdir()) == [] and seen == {"data_is_dir": True, "exec": True}
    sha = approve_edit(repo, message="m", manifest=report.manifest)
    tree = subprocess.run(["git", "ls-tree", "-r", sha], cwd=repo, capture_output=True, text=True).stdout
    assert "demo_utils/data/table.py" in tree and "120000" not in tree


def test_an_export_refuses_a_file_that_no_longer_matches_its_manifest_entry(tmp_path):
    from auto_research.sandbox.gate import _export_judged, changeset_manifest

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    manifest = changeset_manifest(repo)
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text("def my_tool(o):\n    return 0\n")
    with pytest.raises(ValueError, match="changed while the gate ran"):
        _export_judged(repo, manifest, tmp_path / "export")


def test_the_gate_hides_the_real_worktree_from_its_tests(tmp_path, monkeypatch):
    """The default runner masks the worktree, and the jail argv carries the mask (review 6)."""
    from auto_research.sandbox import gate, jail

    seen = {}
    real = gate._jailed_pytest

    def capturing(argv, *, junit_path, worktree=None, masked=None, extra_ro=()):
        seen["masked"] = masked
        monkeypatch.setattr(jail, "_config", lambda: {"agent_os_jail": True})
        monkeypatch.setattr(jail, "bwrap_path", lambda: "/usr/bin/bwrap")
        seen["argv"] = real(argv, junit_path=junit_path, worktree=worktree, masked=masked, extra_ro=extra_ro)
        return argv  # run unjailed here; the argv above is what a jailed run would get

    monkeypatch.setattr(gate, "_jailed_pytest", capturing)
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    asyncio.run(run_gate(repo, _spec()))
    assert seen["masked"] == repo
    i = seen["argv"].index("--tmpfs")
    assert seen["argv"][i + 1] == str(repo.resolve()) and "--remount-ro" in seen["argv"]


def test_a_glob_character_in_a_name_is_refused(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "[ab].py").write_text("x = 1\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and "[ab].py" in allowed.detail


def test_approve_edit_reads_every_path_literally(tmp_path):
    """A committed `[ab].py` deleted must remove that file only, never `a.py`/`b.py` (review 7)."""
    from auto_research.sandbox.gate import _entry
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    lib = repo / "demo_utils" / "lib"
    lib.mkdir(parents=True)
    for name in ("a.py", "b.py", "[ab].py"):
        (lib / name).write_text(f"# {name}\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "human commit")
    (lib / "[ab].py").unlink()
    (lib / "new.py").write_text("NEW = 1\n")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
    # A glob-named path is never a judged path, so a manifest naming one is refused outright …
    manifest = {"demo_utils/lib/[ab].py": "deleted", "demo_utils/lib/new.py": _entry(lib / "new.py")}
    with pytest.raises(RuntimeError, match="changed after the gate"):
        approve_edit(repo, message="m", manifest=manifest)
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout == head
    # … and staging reads names literally all the same: `[ab].py` restored, `new.py` added alone.
    (lib / "[ab].py").write_text("# [ab].py\n")
    sha = approve_edit(repo, message="m", manifest={"demo_utils/lib/new.py": _entry(lib / "new.py")})
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", sha], cwd=repo,
                          capture_output=True, text=True).stdout.split()
    assert {"demo_utils/lib/a.py", "demo_utils/lib/b.py", "demo_utils/lib/[ab].py",
            "demo_utils/lib/new.py"} <= set(tree)


def test_a_judged_deletion_is_committed_from_the_manifest(tmp_path):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "old.py").write_text("OLD = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed")
    (repo / "demo_utils" / "lib" / "old.py").unlink()
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        'def test_my_tool():\n    assert my_tool("synth240611-ae") is not None\n')
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed and report.manifest["demo_utils/lib/old.py"] == "deleted"
    sha = approve_edit(repo, message="m", manifest=report.manifest)
    shown = subprocess.run(["git", "show", "--name-status", "--pretty=format:", sha], cwd=repo,
                           capture_output=True, text=True).stdout
    assert "D\tdemo_utils/lib/old.py" in shown


def test_a_committed_file_link_replaced_by_a_file_never_writes_through(tmp_path):
    import os

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    outside = tmp_path / "outside.py"
    outside.write_text("ORIGINAL = 1\n")
    os.symlink(str(outside), repo / "demo_utils" / "lib" / "shared.py")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "linked file")
    (repo / "demo_utils" / "lib" / "shared.py").unlink()
    (repo / "demo_utils" / "lib" / "shared.py").write_text("REPLACED = 1\n")
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        'def test_my_tool():\n    assert my_tool("synth240611-af") is not None\n')
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert outside.read_text() == "ORIGINAL = 1\n", report.reason


def test_a_py_file_in_pycache_is_judged_not_skipped(tmp_path):
    from auto_research.sandbox.gate import changeset_manifest

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    hook = repo / "demo_utils" / "sub" / "__pycache__" / "hook.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("x = 1\n")
    assert "demo_utils/sub/__pycache__/hook.py" in changeset_manifest(repo)


def test_a_gitattributes_file_is_not_the_coders(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / ".gitattributes").write_text("lib/demo_help.py ident\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    allowed = next(c for c in report.layer_a if c.name == "structural.changes_allowed")
    assert not allowed.passed and ".gitattributes" in allowed.detail


def test_the_commit_holds_the_judged_bytes_whatever_attributes_apply(tmp_path):
    """A committed `.gitattributes` with an encoding or `ident` never changes what is staged."""
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.py ident\n*.txt working-tree-encoding=UTF-7\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "human attributes")
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text("ID = '$Id: kept $'\ndef my_tool(obsid):\n    return load(obsid)\n")
    (repo / "demo_utils" / "lib" / "note.txt").write_text("# see +AAo-import os\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason
    sha = approve_edit(repo, message="m", manifest=report.manifest, base=report.base)
    for rel in ("demo_utils/lib/demo_help.py", "demo_utils/lib/note.txt"):
        blob = subprocess.run(["git", "cat-file", "blob", f"{sha}:{rel}"], cwd=repo,
                              capture_output=True, check=True).stdout
        assert blob == (repo / rel).read_bytes(), rel


def test_the_commit_lands_on_the_head_the_gate_judged(tmp_path):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "sneak")
    with pytest.raises(RuntimeError, match="HEAD moved"):
        approve_edit(repo, message="m", manifest=report.manifest, base=report.base)


def test_a_folder_replaced_by_a_file_can_pass(tmp_path):
    import shutil

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "x").mkdir()
    (repo / "demo_utils" / "x" / "y.py").write_text("Y = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "folder")
    shutil.rmtree(repo / "demo_utils" / "x")
    (repo / "demo_utils" / "x").write_text("X = 1\n")
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        'def test_my_tool():\n    assert my_tool("synth240611-ag") is not None\n')
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason


def test_a_file_replacing_a_folder_commits(tmp_path):
    import shutil

    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "sub").mkdir()
    (repo / "demo_utils" / "sub" / "a.py").write_text("A = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "folder")
    shutil.rmtree(repo / "demo_utils" / "sub")
    (repo / "demo_utils" / "sub").write_text("SUB = 1\n")
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(
        'def test_my_tool():\n    assert my_tool("synth240611-ah") is not None\n')
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert report.passed, report.reason
    sha = approve_edit(repo, message="m", manifest=report.manifest, base=report.base)
    tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", sha], cwd=repo, capture_output=True, text=True).stdout
    assert "demo_utils/sub\n" in tree + "\n" and "demo_utils/sub/a.py" not in tree


def test_a_latin1_byte_is_a_failed_check_not_a_crash(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed")
    helper = repo / "demo_utils" / "lib" / "demo_help.py"
    helper.write_bytes(helper.read_bytes() + b"# temperature in \xb0C\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed and "undecodable" in report.reason
    import os

    fd = os.open(bytes(repo / "demo_utils" / "lib") + b"/caf\xff.py", os.O_WRONLY | os.O_CREAT, 0o644)
    os.write(fd, b"x = 1\n")
    os.close(fd)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))  # no crash
    assert not report.passed


def test_bytecode_is_purged_from_the_packages_and_nothing_else(tmp_path):
    from auto_research.sandbox.merge_flow import purge_bytecode

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    cache = repo / "demo_utils" / "lib" / "__pycache__"
    cache.mkdir()
    (cache / "demo_help.cpython-313.pyc").write_bytes(b"forged")
    (repo / "demo_utils" / "lib" / "helper.pyc").write_bytes(b"sourceless")
    (repo / "demo_utils" / "sync.sh").write_text("# the human's\n")
    (repo / "obsdb_catalog" / "__pycache__").mkdir(parents=True)
    (repo / "obsdb_catalog" / "__pycache__" / "core.cpython-313.pyc").write_bytes(b"forged")
    (repo / "json.pyc").write_bytes(b"shadows the stdlib")
    (repo / "obsdb_catalog" / "__pycache__").chmod(0o111)  # unlistable, still loadable by Python
    purge_bytecode(repo)
    assert not (cache / "demo_help.cpython-313.pyc").exists() and not (repo / "demo_utils/lib/helper.pyc").exists()
    assert not (repo / "obsdb_catalog/__pycache__/core.cpython-313.pyc").exists() and not (repo / "json.pyc").exists()
    assert (repo / "demo_utils" / "sync.sh").is_file()


def test_commit_runs_no_repository_hook(tmp_path):
    from auto_research.sandbox.merge_flow import approve_edit

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch \"$GIT_DIR/../hook-ran\"\nexit 1\n")
    hook.chmod(0o755)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    approve_edit(repo, message="m", manifest=report.manifest, base=report.base)
    assert not (repo / "hook-ran").exists()


def test_bytecode_is_purged_through_a_linked_toolkit_root(tmp_path):
    import os

    from auto_research.sandbox.merge_flow import purge_bytecode

    repo = _make_repo(tmp_path)
    (repo / "json.pyc").write_bytes(b"x")
    link = tmp_path / "toolkit_link"
    os.symlink(repo, link)
    purge_bytecode(link)
    assert not (repo / "json.pyc").exists()


def test_a_big_changed_file_is_scanned_line_by_line(tmp_path):
    from auto_research.sandbox.gate import _added_lines

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "committed")
    _git(repo, "config", "core.bigFileThreshold", "1k")
    helper = repo / "demo_utils" / "lib" / "demo_help.py"
    helper.write_text(helper.read_text() + "# pad\n" * 400 + "x = 'fabricated'\n")
    assert len(_added_lines(repo, "demo_utils/lib/demo_help.py")) == 401


def test_added_lines_are_exact_without_a_final_newline_and_with_plus_plus(tmp_path):
    from auto_research.sandbox.gate import _added_lines

    repo = _make_repo(tmp_path)
    lib = repo / "demo_utils" / "lib"
    lib.mkdir(parents=True)
    (lib / "a.py").write_text("A = 1\nB = 2\nVALUE = 2")  # no final newline, as 8 real files have
    (lib / "b.py").write_text('"""Doc.\n"""\n')
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    (lib / "a.py").write_text("A = 1\nB = 2\nX = 1\nY = 2\nVALUE = 2\nZ = 3\n")
    (lib / "b.py").write_text('"""Doc.\n++ returns placeholder data\n"""\n')
    assert _added_lines(repo, "demo_utils/lib/a.py") == {3, 4, 5, 6}
    assert _added_lines(repo, "demo_utils/lib/b.py") == {2}


def test_an_untracked_gitattributes_is_removed_before_a_reset(tmp_path):
    from auto_research.sandbox.merge_flow import remove_untracked_ignores

    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "c")
    (repo / "demo_utils" / ".gitattributes").write_text("*.py working-tree-encoding=UTF-16\n")
    assert "demo_utils/.gitattributes" in remove_untracked_ignores(repo)
    assert not (repo / "demo_utils" / ".gitattributes").exists()


@pytest.mark.parametrize("added_text", [
    'MSG = "fit ok\\nusing fake data for now"\n',              # a \n escape inside one added line
    "# note\x0cmore\nX = 1\n# here we use fake data\n",        # a form feed in an added comment
    "# note\u2028more\nX = 1\n# here we use fake data\n",      # U+2028 likewise
    "X = 1  # ok\x0cfabricated values\n",                        # inside one comment: form feed
    "X = 1  # ok\x0bfabricated values\n",                        # vertical tab
    "X = 1  # ok\u2028fabricated values\n",                      # U+2028
    "X = 1  # ok\r# fabricated values\n",                        # a lone CR (Python: two lines)
    'DATA = b"fake data for now"\n',                             # a bytes literal
])
def test_added_text_is_scanned_whatever_line_breaks_it_holds(tmp_path, added_text):
    from auto_research.sandbox.gate import collect_targets, scan_python_source

    repo = _make_repo(tmp_path)
    lib = repo / "demo_utils" / "lib"
    lib.mkdir(parents=True)
    (lib / "mod.py").write_text("A = 1\nB = 2\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    (lib / "mod.py").write_bytes(("A = 1\nB = 2\n" + added_text).encode())
    added = collect_targets(repo)[lib / "mod.py"]
    source = (lib / "mod.py").read_bytes().decode("utf-8")  # as the gate reads it: no newline translation
    hard, _ = scan_python_source("demo_utils/lib/mod.py", source, added=added)
    assert any(c.name == "no_fabrication.vocabulary" for c in hard), (added, hard)


def test_a_lone_carriage_return_fails_the_gate(tmp_path):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "c")
    helper = repo / "demo_utils" / "lib" / "demo_help.py"
    helper.write_bytes(helper.read_bytes() + b'x = 2\rMSG = "fake data for now"\n')
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed and "carriage return" in report.reason


def test_ast_scans_see_added_lines_inside_multi_line_nodes(tmp_path):
    from auto_research.sandbox.gate import collect_targets, scan_python_source

    repo = _make_repo(tmp_path)
    lib = repo / "demo_utils" / "lib"
    lib.mkdir(parents=True)
    (lib / "core.py").write_text(
        "import numpy as np\n\n\ndef fit(x):\n    return compute(x)\n\n\ndef spread(x):\n"
        "    y = (np\n         .mean(x))\n    return y\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "base")
    (lib / "core.py").write_text(
        "import numpy as np\n\n\ndef fit(x):\n    pass\n\n\ndef spread(x):\n"
        "    y = (np\n         .random.normal(x))\n    return y\n")
    added = collect_targets(repo)[lib / "core.py"]
    hard, finds = scan_python_source("demo_utils/lib/core.py", (lib / "core.py").read_text(), added=added)
    assert any(c.name == "no_fabrication.placeholder_body" for c in hard), hard
    assert any("random.normal" in c.detail for c in finds), finds


@pytest.mark.parametrize("body", ["def f():\n    return [" + "-" * 3000 + "1]\n",
                                  "TABLE = " + " + ".join(["1.0"] * 20000) + "\n"])
def test_a_deeply_nested_file_is_a_failed_check_not_a_crash(tmp_path, body):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "deep.py").write_text(body)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert not report.passed and "nested too deeply" in report.reason


@pytest.mark.parametrize("cookie,fails", [("# -*- coding: utf-7 -*-", True), ("# coding: latin-1", True),
                                          ("# -*- coding: utf-8 -*-", False), ("# coding=utf8", False)])
def test_only_a_utf8_source_encoding_is_accepted(tmp_path, cookie, fails):
    repo = _make_repo(tmp_path)
    _write_pkg(repo)
    (repo / "demo_utils" / "lib" / "enc.py").write_text(cookie + "\nX = 1\n")
    report = asyncio.run(run_gate(repo, _spec(), test_runner=_pass_runner))
    assert ("source-encoding" in report.reason) is fails, report.reason



def _derived_package(repo, monkeypatch, *, activate="export STACK_READY=1\n", test_body=None):
    """demo_utils names a derived environment layered on this test's interpreter (a real Python
    with pytest), with an activation of its own."""
    import sys
    from pathlib import Path

    from auto_research import env as app_env
    from auto_research.sandbox.envs import Environment

    base = Environment(name="testbase", prefix=Path(sys.prefix), owner="human")
    monkeypatch.setattr(app_env, "_orch_config", lambda: {"env_externals": {}})
    monkeypatch.setattr(app_env, "analysis_environment", lambda config=None: base)
    _write_pkg(repo)
    (repo / "demo_utils" / "tool_config.yaml").write_text("env: stack\n")
    (repo / "demo_utils" / "lib" / "test_demo.py").write_text(test_body or (
        "import os, sys\n"
        "def test_runs_in_its_environment():\n"
        "    assert os.environ.get('STACK_READY') == '1'\n"
        f"    assert sys.prefix == {str(Path(sys.prefix))!r}\n"
    ))
    folder = repo / "envs" / "stack"
    folder.mkdir(parents=True)
    (folder / "env.yaml").write_text("purpose: the demo tools' stack\nbase: testbase\n")
    if activate is not None:
        (folder / "activate.sh").write_text(activate)
    return folder


def test_layer_b_runs_a_package_tests_in_the_environment_it_names(tmp_path, monkeypatch):
    """B3c-2 through the real gate and its default runner: the judged definition is built, its
    activation sourced in the test process (inside the jail when it is on), its Python used."""
    repo = _make_repo(tmp_path)
    _derived_package(repo, monkeypatch)
    report = asyncio.run(run_gate(repo, _spec()))
    pytest_result = next(c for c in report.layer_b if c.name == "layer_b.pytest")
    assert pytest_result.passed, pytest_result.detail


def test_layer_b_fails_honestly_when_the_named_activation_breaks(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _derived_package(repo, monkeypatch, activate="exit 0\n")
    report = asyncio.run(run_gate(repo, _spec()))
    assert not report.passed
    result = next(c for c in report.layer_b if c.name == "layer_b.pytest")
    assert not result.passed and "did not finish" in result.detail


def test_layer_b_groups_tests_by_environment_and_reports_a_failed_build(tmp_path, monkeypatch):
    seen = []

    async def recording(worktree, test_paths, environment=None):
        seen.append((tuple(test_paths), getattr(environment, "name", None)))
        return {"results": [CheckResult(name="layer_b.pytest", layer="B", passed=True, detail="")],
                "deterministic": True}

    repo = _make_repo(tmp_path)
    _derived_package(repo, monkeypatch)
    report = asyncio.run(run_gate(repo, _spec(), test_runner=recording))
    assert seen == [(("demo_utils/lib/test_demo.py",), "ar-stack")] and report.passed
    (repo / "envs" / "stack" / "env.yaml").write_text("purpose: x\nbase: gone\n")  # no such base
    seen.clear()
    report = asyncio.run(run_gate(repo, _spec(), test_runner=recording))
    assert not report.passed and seen == []


def test_each_environment_group_watches_only_its_own_tools(tmp_path, monkeypatch):
    """Two packages in one changeset, one naming an environment: the analysis group's return
    recorder must not import the other environment's tool (it may not even import there)."""
    repo = _make_repo(tmp_path)
    _derived_package(repo, monkeypatch, test_body=(
        "from demo_utils.lib.demo_help import my_tool\n"
        "def test_my_tool():\n    assert my_tool('x') == {'stdout': 'ok'}\n"))
    # The demo tool imports only where its environment's activation ran.
    (repo / "demo_utils" / "lib" / "demo_help.py").write_text(
        "import os\nassert os.environ['STACK_READY'] == '1'\n"
        "def my_tool(obsid):\n    return {'stdout': 'ok'}\n")
    other = repo / "other_utils"
    for sub in ("lib", "skills"):
        (other / sub).mkdir(parents=True)
    (other / "__init__.py").write_text("from .skills import TOOL_MANIFEST\n")
    (other / "lib" / "__init__.py").write_text("")
    (other / "lib" / "other_help.py").write_text("def other_tool(obsid):\n    return {'stdout': 'fine'}\n")
    (other / "skills" / "__init__.py").write_text(
        'TOOL_MANIFEST = {\n  "research_toolkit.other_utils.other_tool": {\n'
        '    "module": "other_utils.lib.other_help", "fn": "other_tool",\n'
        '    "description": "d", "args_schema": {}}}\n')
    (other / "lib" / "test_other.py").write_text(
        "from other_utils.lib.other_help import other_tool\n"
        "def test_other():\n    assert other_tool('x') == {'stdout': 'fine'}\n")
    from auto_research.sandbox import jail

    # The stand-in analysis environment (this interpreter) is not on the jail's allow-list as the
    # configured one is; the grouping is what this test is about.
    monkeypatch.setattr(jail, "jail_enabled", lambda: False)
    report = asyncio.run(run_gate(repo, _spec()))
    failed = [c for c in report.layer_b if not c.passed]
    assert not failed, failed
    assert not [f for f in report.findings if f.name == "finding.returns_unobserved"]
    watched = [c for c in report.layer_b if c.name.startswith("layer_b.returns")]
    assert len(watched) == 2 and all(c.passed for c in watched), watched
